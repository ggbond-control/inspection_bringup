#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BRINGUP_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
WORKSPACE_ROOT="$(cd "${BRINGUP_DIR}/../.." && pwd)"
DEFAULT_CONFIG="${BRINGUP_DIR}/config/system.yaml"

SESSION_NAME="inspection"
CONFIG_PATH="${DEFAULT_CONFIG}"
ATTACH=1
KILL_EXISTING=0
DRY_RUN=0

usage() {
  cat <<'EOF'
Usage: start_inspection_tmux.sh [options]

Options:
  --session NAME       tmux session name. Default: inspection
  --config PATH        Bringup system YAML. Default: inspection_bringup/config/system.yaml
  --kill-existing      Kill an existing session with the same name before starting.
  --no-attach          Start the session but do not attach or switch to it.
  --dry-run            Print planned tmux windows and commands without starting them.
  -h, --help           Show this help.

Behavior:
  Starts enabled modules from config/system.yaml in separate tmux windows.
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

CONFIG_PATH="$(realpath -m "${CONFIG_PATH}")"

if [[ ! -f "${CONFIG_PATH}" ]]; then
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

readarray -t WINDOW_SPECS < <(
  python3 - "${CONFIG_PATH}" <<'PY'
import sys

import yaml

config_path = sys.argv[1]

with open(config_path, "r", encoding="utf-8") as stream:
    config = yaml.safe_load(stream) or {}

modules = config.get("modules", {})


def enabled(name, default=True):
    value = modules.get(name, default)
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in ("1", "true", "yes", "on")


windows = []
if enabled("task_hub", True):
    windows.append(("task_hub", "system", "enable_task_hub:=true enable_gimbal:=false enable_charge:=false enable_sensors:=false enable_mqtt:=false"))
if enabled("gimbal", True):
    windows.append(("gimbal", "system", "enable_task_hub:=false enable_gimbal:=true enable_charge:=false enable_sensors:=false enable_mqtt:=false"))
if enabled("charge", True):
    windows.append(("charge", "system", "enable_task_hub:=false enable_gimbal:=false enable_charge:=true enable_sensors:=false enable_mqtt:=false"))
if enabled("sensors", True) and enabled("alarm", True):
    windows.append(("alarm", "sensors", "enable_alarm:=true enable_gas:=false enable_thermal:=false"))
if enabled("sensors", True) and enabled("gas", True):
    windows.append(("gas", "sensors", "enable_alarm:=false enable_gas:=true enable_thermal:=false"))
if enabled("sensors", True) and enabled("thermal", True):
    windows.append(("thermal", "sensors", "enable_alarm:=false enable_gas:=false enable_thermal:=true"))
if enabled("mqtt", True):
    windows.append(("mqtt", "platform_bridge", "enable_mqtt:=true"))

for name, launch_kind, args in windows:
    print(f"{name}\t{launch_kind}\t{args}")
PY
)

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
  printf "exec ros2 launch inspection_bringup %s system_config_path:=%s %s" \
    "${launch_file}" "$(quote "${CONFIG_PATH}")" "${launch_args}"
}

if [[ "${DRY_RUN}" -eq 1 ]]; then
  echo "[tmux] session: ${SESSION_NAME}"
  echo "[tmux] config: ${CONFIG_PATH}"
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
