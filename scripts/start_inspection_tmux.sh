#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BRINGUP_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
WORKSPACE_ROOT="$(cd "${BRINGUP_DIR}/../.." && pwd)"

SESSION_NAME="inspection"
CONFIG_PATH=""
ATTACH=1
KILL_EXISTING=0
DRY_RUN=0

usage() {
  cat <<'EOF'
Usage: start_inspection_tmux.sh [options]

Options:
  --session NAME       tmux session name. Default: inspection
  --config PATH        Standalone system YAML; bypass robot.yaml. Default: automatic config.
  --kill-existing      Kill an existing session with the same name before starting.
  --no-attach          Start the session but do not attach or switch to it.
  --dry-run            Print planned tmux windows and commands without starting them.
  -h, --help           Show this help.

Behavior:
  Starts enabled modules from the effective system configuration in separate tmux windows.
  The original ROS 2 launch files are preserved and reused.
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --session)
      if [[ $# -lt 2 ]]; then
        echo "error: --session requires a value" >&2
        exit 2
      fi
      SESSION_NAME="$2"
      shift 2
      ;;
    --config)
      if [[ $# -lt 2 ]]; then
        echo "error: --config requires a path" >&2
        exit 2
      fi
      CONFIG_PATH="$2"
      shift 2
      ;;
    --kill-existing)
      KILL_EXISTING=1
      shift
      ;;
    --no-attach)
      ATTACH=0
      shift
      ;;
    --dry-run)
      DRY_RUN=1
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    --*)
      echo "error: unknown option '$1'" >&2
      usage >&2
      exit 2
      ;;
    *)
      echo "error: unexpected argument '$1'" >&2
      usage >&2
      exit 2
      ;;
  esac
done

if [[ -n "${CONFIG_PATH}" ]]; then
  CONFIG_PATH="$(realpath -m "${CONFIG_PATH}")"
fi

if [[ -n "${CONFIG_PATH}" && ! -f "${CONFIG_PATH}" ]]; then
  echo "error: config file not found: ${CONFIG_PATH}" >&2
  exit 2
fi

if [[ "${DRY_RUN}" -eq 0 ]] && ! command -v tmux >/dev/null 2>&1; then
  echo "error: tmux is not installed or not in PATH" >&2
  exit 127
fi

if ! command -v python3 >/dev/null 2>&1; then
  echo "error: python3 is not installed or not in PATH" >&2
  exit 127
fi

CONFIG_ROOT="$(python3 "${SCRIPT_DIR}/bringup_config.py" root)"
WINDOW_OUTPUT="$(
  python3 - "${CONFIG_PATH}" "${SCRIPT_DIR}" <<'PYCODE'
import sys
sys.path.insert(0, sys.argv[2])
from bringup_config import load_config

config = load_config("system", sys.argv[1])
modules = config.get("modules", {})


def enabled(name, default=True):
    value = modules.get(name, default)
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in ("1", "true", "yes", "on")


system_modules = ("task_hub", "sensor_gimbal", "charge", "sensors", "mqtt", "algorithm_transport",
                  "mission_execution_agent")
for module in ("task_hub", "sensor_gimbal", "charge", "mission_execution_agent"):
    if enabled(module, module not in ("mission_execution_agent", "sensor_gimbal")):
        args = " ".join(f"enable_{name}:={'true' if name == module else 'false'}"
                        for name in system_modules)
        print(f"{module}\tsystem\t{args}")
if enabled("sensors"):
    for module in ("alarm", "light", "gas", "sensor_rs485", "acoustic"):
        if enabled(module):
            args = " ".join(f"enable_{name}:={'true' if name == module else 'false'}"
                            for name in ("alarm", "light", "gas", "sensor_rs485", "acoustic"))
            print(f"{module}\tsensors\t{args}")
if enabled("mqtt") or enabled("algorithm_transport"):
    args = " ".join(f"enable_{name}:={'true' if name in ('mqtt', 'algorithm_transport') and enabled(name) else 'false'}"
                    for name in system_modules)
    print(f"platform\tsystem\t{args}")
PYCODE
)"
WINDOW_SPECS=()
if [[ -n "${WINDOW_OUTPUT}" ]]; then
  readarray -t WINDOW_SPECS <<<"${WINDOW_OUTPUT}"
fi

if [[ "${#WINDOW_SPECS[@]}" -eq 0 ]]; then
  echo "error: no modules are enabled in ${CONFIG_PATH}" >&2
  exit 2
fi

quote() {
  printf "%q" "$1"
}

source_lines() {
  local lines=()
  lines+=("source /opt/ros/jazzy/setup.bash")
  if [[ -f "${WORKSPACE_ROOT}/install/setup.bash" ]]; then
    lines+=("source $(quote "${WORKSPACE_ROOT}/install/setup.bash")")
  else
    lines+=("echo '[warn] workspace setup not found: ${WORKSPACE_ROOT}/install/setup.bash'")
  fi
  printf "%s; " "${lines[@]}"
}

launch_file_for_kind() {
  case "$1" in
    system)
      echo "inspection_system.launch.py"
      ;;
    sensors)
      echo "sensors.launch.py"
      ;;
    platform_bridge)
      echo "platform_bridge.launch.py"
      ;;
    *)
      echo "error: unknown launch kind '$1'" >&2
      exit 2
      ;;
  esac
}

build_command() {
  local launch_kind="$1"
  local launch_args="$2"
  local launch_file
  launch_file="$(launch_file_for_kind "${launch_kind}")"

  printf "set -e; "
  source_lines
  printf "export RCUTILS_CONSOLE_OUTPUT_FORMAT='[{time}] [{severity}] [{name}]: {message}'; "
  printf "export RCUTILS_COLORIZED_OUTPUT=1; "
  printf "export RCUTILS_LOGGING_BUFFERED_STREAM=0; "
  printf "export INSPECTION_CONFIG_DIR=%s; " "$(quote "${CONFIG_ROOT}")"
  printf "exec ros2 launch inspection_bringup %s " "${launch_file}"
  if [[ -n "${CONFIG_PATH}" ]]; then
    printf "system_config_path:=%s " "$(quote "${CONFIG_PATH}")"
  fi
  printf "%s" "${launch_args}"
}

if [[ "${DRY_RUN}" -eq 1 ]]; then
  echo "[tmux] session: ${SESSION_NAME}"
  echo "[tmux] config: ${CONFIG_PATH:-${CONFIG_ROOT} (automatic)}"
  for spec in "${WINDOW_SPECS[@]}"; do
    IFS=$'\t' read -r window_name launch_kind launch_args <<<"${spec}"
    echo
    echo "[window] ${window_name}"
    build_command "${launch_kind}" "${launch_args}"
    echo
  done
  exit 0
fi

if tmux has-session -t "${SESSION_NAME}" 2>/dev/null; then
  if [[ "${KILL_EXISTING}" -eq 1 ]]; then
    tmux kill-session -t "${SESSION_NAME}"
  else
    cat >&2 <<EOF
error: tmux session '${SESSION_NAME}' already exists.

Attach to it:
  tmux attach -t ${SESSION_NAME}

Or restart it:
  $0 --kill-existing
EOF
    exit 2
  fi
fi

FIRST_WINDOW=1
for spec in "${WINDOW_SPECS[@]}"; do
  IFS=$'\t' read -r window_name launch_kind launch_args <<<"${spec}"
  command="$(build_command "${launch_kind}" "${launch_args}")"

  echo "[tmux] start ${SESSION_NAME}:${window_name}"
  if [[ "${FIRST_WINDOW}" -eq 1 ]]; then
    tmux new-session -d -s "${SESSION_NAME}" -n "${window_name}" "bash -lc $(quote "${command}")"
    FIRST_WINDOW=0
  else
    tmux new-window -t "${SESSION_NAME}" -n "${window_name}" "bash -lc $(quote "${command}")"
  fi
done

tmux set-option -t "${SESSION_NAME}" remain-on-exit on >/dev/null
tmux select-window -t "${SESSION_NAME}:0"

echo "[tmux] session '${SESSION_NAME}' started with ${#WINDOW_SPECS[@]} window(s)"

if [[ "${ATTACH}" -eq 1 ]]; then
  if [[ -n "${TMUX:-}" ]]; then
    tmux switch-client -t "${SESSION_NAME}"
  else
    tmux attach-session -t "${SESSION_NAME}"
  fi
else
  echo "[tmux] attach later with: tmux attach -t ${SESSION_NAME}"
fi
