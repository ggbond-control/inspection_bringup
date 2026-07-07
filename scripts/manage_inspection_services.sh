#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BRINGUP_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
DEFAULT_CONFIG="${BRINGUP_DIR}/config/services.yaml"
TEMPLATE_DIR="${BRINGUP_DIR}/systemd"
SYSTEMD_DIR="/etc/systemd/system"

CONFIG_PATH="${DEFAULT_CONFIG}"
DEFAULT_SUDO_PASSWORD="cat"

usage() {
  cat <<'EOF'
Usage: manage_inspection_services.sh [--config PATH] <command> [service]

Commands:
  install             Generate and install wrappers and systemd units.
  uninstall           Disable and remove installed systemd units and wrappers.
  enable              Enable services configured with enabled_on_boot: true.
  disable             Disable both services.
  start [service]     Start both services or one service: navigation|system.
  stop [service]      Stop both services or one service: navigation|system.
  restart [service]   Restart both services or one service: navigation|system.
  status [service]    Show systemd status.
  logs <service>      Follow journal logs for navigation|system.
  render              Render files to a temporary directory without installing.

Options:
  --config PATH       Service configuration YAML. Default: config/services.yaml
  -h, --help          Show this help.

The script tries sudo with the default endpoint password from services.yaml
first. If that fails, it falls back to the normal sudo prompt.
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --config)
      if [[ $# -lt 2 ]]; then
        echo "error: --config requires a path" >&2
        exit 2
      fi
      CONFIG_PATH="$2"
      shift 2
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
      break
      ;;
  esac
done

COMMAND="${1:-}"
TARGET="${2:-}"

if [[ -z "${COMMAND}" ]]; then
  usage >&2
  exit 2
fi

CONFIG_PATH="$(realpath -m "${CONFIG_PATH}")"

if [[ ! -f "${CONFIG_PATH}" ]]; then
  echo "error: config file not found: ${CONFIG_PATH}" >&2
  exit 2
fi

if ! command -v python3 >/dev/null 2>&1; then
  echo "error: python3 is not installed or not in PATH" >&2
  exit 127
fi

CONFIG_VALUE_SCRIPT='
import sys
import yaml

path, dotted, fallback = sys.argv[1:4]
with open(path, "r", encoding="utf-8") as stream:
    config = yaml.safe_load(stream) or {}

value = config
for part in dotted.split("."):
    if not isinstance(value, dict) or part not in value:
        print(fallback)
        raise SystemExit
    value = value[part]

if isinstance(value, bool):
    print("true" if value else "false")
elif value is None:
    print(fallback)
else:
    print(value)
'

config_value() {
  python3 -c "${CONFIG_VALUE_SCRIPT}" "${CONFIG_PATH}" "$1" "$2"
}

DEFAULT_SUDO_PASSWORD="$(config_value service.sudo_default_password "${DEFAULT_SUDO_PASSWORD}")"
INSTALL_DIR="$(config_value service.install_dir /opt/inspection_bringup)"
NAV_SERVICE="$(config_value navigation.service_name inspection-navigation.service)"
SYSTEM_SERVICE="$(config_value inspection_system.service_name inspection-system.service)"

require_sudo() {
  if [[ "${EUID}" -eq 0 ]]; then
    return 0
  fi
  if sudo -n true 2>/dev/null; then
    return 0
  fi
  if [[ -n "${DEFAULT_SUDO_PASSWORD}" ]] && printf '%s\n' "${DEFAULT_SUDO_PASSWORD}" | sudo -S -p '' true 2>/dev/null; then
    return 0
  fi
  echo "[sudo] default password failed; please enter sudo password if prompted" >&2
  sudo -v
}

sudo_run() {
  if [[ "${EUID}" -eq 0 ]]; then
    "$@"
  else
    sudo "$@"
  fi
}

service_for_target() {
  case "$1" in
    navigation)
      echo "${NAV_SERVICE}"
      ;;
    system|inspection_system)
      echo "${SYSTEM_SERVICE}"
      ;;
    "")
      return 1
      ;;
    *)
      echo "error: unknown service target '$1' (expected navigation or system)" >&2
      exit 2
      ;;
  esac
}

systemctl_for_target() {
  local action="$1"
  local target="${2:-}"

  require_sudo
  if [[ -n "${target}" ]]; then
    sudo_run systemctl "${action}" "$(service_for_target "${target}")"
    return
  fi

  if [[ "${action}" == "start" || "${action}" == "restart" ]]; then
    sudo_run systemctl "${action}" "${NAV_SERVICE}"
    sudo_run systemctl "${action}" "${SYSTEM_SERVICE}"
  elif [[ "${action}" == "stop" ]]; then
    sudo_run systemctl "${action}" "${SYSTEM_SERVICE}" || true
    sudo_run systemctl "${action}" "${NAV_SERVICE}" || true
  else
    sudo_run systemctl "${action}" "${NAV_SERVICE}" "${SYSTEM_SERVICE}"
  fi
}

render_files() {
  local output_dir="$1"
  mkdir -p "${output_dir}/wrappers" "${output_dir}/systemd"
  python3 - "${CONFIG_PATH}" "${TEMPLATE_DIR}" "${output_dir}" <<'PY'
import os
import shlex
import stat
import sys

import yaml

config_path, template_dir, output_dir = sys.argv[1:4]

with open(config_path, "r", encoding="utf-8") as stream:
    config = yaml.safe_load(stream) or {}

service = config.get("service", {})
user = str(service.get("user", "cat"))
home_dir = str(service.get("home_dir", f"/home/{user}"))
workspace_root = str(service.get("workspace_root", f"{home_dir}/task_ws"))
ros_distro = str(service.get("ros_distro", "jazzy"))
ros_log_dir = str(service.get("ros_log_dir", f"{home_dir}/inspection_logs"))
install_dir = str(service.get("install_dir", "/opt/inspection_bringup"))
restart = str(service.get("restart", "on-failure"))
restart_sec = str(service.get("restart_sec", 5))
timeout_stop_sec = str(service.get("timeout_stop_sec", 30))


def as_bool(value, default=False):
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in ("1", "true", "yes", "on")


def launch_args_text(args):
    if not isinstance(args, dict):
        return ""
    parts = []
    for key, value in args.items():
        if isinstance(value, bool):
            value_text = "true" if value else "false"
        else:
            value_text = str(value)
        parts.append(shlex.quote(f"{key}:={value_text}"))
    return " ".join(parts)


def write_executable(path, content):
    with open(path, "w", encoding="utf-8") as stream:
        stream.write(content)
    mode = os.stat(path).st_mode
    os.chmod(path, mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def wrapper_content(kind, launch_file, args):
    return f"""#!/usr/bin/env bash
set -euo pipefail

source /opt/ros/{shlex.quote(ros_distro)}/setup.bash

if [[ ! -f {shlex.quote(workspace_root)}/install/setup.bash ]]; then
  echo "error: workspace setup not found: {workspace_root}/install/setup.bash" >&2
  exit 1
fi
source {shlex.quote(workspace_root)}/install/setup.bash

mkdir -p {shlex.quote(ros_log_dir)}
export ROS_LOG_DIR={shlex.quote(ros_log_dir)}
export RCUTILS_CONSOLE_OUTPUT_FORMAT='[{{time}}] [{{severity}}] [{{name}}]: {{message}}'
export RCUTILS_COLORIZED_OUTPUT=0
export RCUTILS_LOGGING_BUFFERED_STREAM=0

cd {shlex.quote(workspace_root)}
exec ros2 launch inspection_bringup {shlex.quote(launch_file)} {args}
"""


navigation = config.get("navigation", {})
inspection_system = config.get("inspection_system", {})

write_executable(
    os.path.join(output_dir, "wrappers", "run_navigation.sh"),
    wrapper_content(
        "navigation",
        "navigation.launch.py",
        launch_args_text(navigation.get("launch_args", {})),
    ),
)
write_executable(
    os.path.join(output_dir, "wrappers", "run_inspection_system.sh"),
    wrapper_content(
        "inspection_system",
        "inspection_system.launch.py",
        launch_args_text(inspection_system.get("launch_args", {})),
    ),
)

replacements = {
    "@USER@": user,
    "@HOME_DIR@": home_dir,
    "@WORKSPACE_ROOT@": workspace_root,
    "@INSTALL_DIR@": install_dir,
    "@RESTART@": restart,
    "@RESTART_SEC@": restart_sec,
    "@TIMEOUT_STOP_SEC@": timeout_stop_sec,
}

for template_name, section, default_service in (
    ("inspection-navigation.service.in", navigation, "inspection-navigation.service"),
    ("inspection-system.service.in", inspection_system, "inspection-system.service"),
):
    with open(os.path.join(template_dir, template_name), "r", encoding="utf-8") as stream:
        content = stream.read()
    for placeholder, value in replacements.items():
        content = content.replace(placeholder, str(value))

    service_name = str(section.get("service_name", default_service))
    with open(os.path.join(output_dir, "systemd", service_name), "w", encoding="utf-8") as stream:
        stream.write(content)

with open(os.path.join(output_dir, "enabled_services"), "w", encoding="utf-8") as stream:
    if as_bool(navigation.get("enabled_on_boot"), True):
        stream.write(str(navigation.get("service_name", "inspection-navigation.service")) + "\n")
    if as_bool(inspection_system.get("enabled_on_boot"), True):
        stream.write(str(inspection_system.get("service_name", "inspection-system.service")) + "\n")
PY
}

install_services() {
  local tmp_dir
  tmp_dir="$(mktemp -d)"
  trap 'rm -rf "${tmp_dir}"' RETURN

  render_files "${tmp_dir}"
  require_sudo
  sudo_run install -d -m 0755 "${INSTALL_DIR}"
  sudo_run install -m 0755 "${tmp_dir}/wrappers/run_navigation.sh" "${INSTALL_DIR}/run_navigation.sh"
  sudo_run install -m 0755 "${tmp_dir}/wrappers/run_inspection_system.sh" "${INSTALL_DIR}/run_inspection_system.sh"
  sudo_run install -m 0644 "${tmp_dir}/systemd/${NAV_SERVICE}" "${SYSTEMD_DIR}/${NAV_SERVICE}"
  sudo_run install -m 0644 "${tmp_dir}/systemd/${SYSTEM_SERVICE}" "${SYSTEMD_DIR}/${SYSTEM_SERVICE}"
  sudo_run systemctl daemon-reload
  echo "[install] installed ${NAV_SERVICE}, ${SYSTEM_SERVICE}"
}

uninstall_services() {
  require_sudo
  sudo_run systemctl disable --now "${SYSTEM_SERVICE}" "${NAV_SERVICE}" >/dev/null 2>&1 || true
  sudo_run rm -f "${SYSTEMD_DIR}/${SYSTEM_SERVICE}" "${SYSTEMD_DIR}/${NAV_SERVICE}"
  sudo_run rm -f "${INSTALL_DIR}/run_inspection_system.sh" "${INSTALL_DIR}/run_navigation.sh"
  sudo_run systemctl daemon-reload
  echo "[uninstall] removed ${NAV_SERVICE}, ${SYSTEM_SERVICE}"
}

enable_services() {
  local tmp_dir
  tmp_dir="$(mktemp -d)"
  trap 'rm -rf "${tmp_dir}"' RETURN

  render_files "${tmp_dir}"
  require_sudo
  if [[ -s "${tmp_dir}/enabled_services" ]]; then
    while IFS= read -r service_name; do
      [[ -n "${service_name}" ]] || continue
      sudo_run systemctl enable "${service_name}"
    done < "${tmp_dir}/enabled_services"
  fi
}

case "${COMMAND}" in
  install)
    install_services
    ;;
  uninstall)
    uninstall_services
    ;;
  enable)
    enable_services
    ;;
  disable)
    systemctl_for_target disable "${TARGET}"
    ;;
  start|stop|restart|status)
    systemctl_for_target "${COMMAND}" "${TARGET}"
    ;;
  logs)
    if [[ -z "${TARGET}" ]]; then
      echo "error: logs requires service target: navigation or system" >&2
      exit 2
    fi
    journalctl -u "$(service_for_target "${TARGET}")" -f
    ;;
  render)
    tmp_dir="$(mktemp -d)"
    render_files "${tmp_dir}"
    echo "${tmp_dir}"
    ;;
  *)
    echo "error: unknown command '${COMMAND}'" >&2
    usage >&2
    exit 2
    ;;
esac
