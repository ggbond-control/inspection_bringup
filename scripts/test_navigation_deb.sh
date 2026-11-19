#!/usr/bin/env bash
set -euo pipefail
if [[ $# -lt 1 || ! -f "$1" ]]; then
  echo "Usage: bash scripts/test_navigation_deb.sh DRIVER_DEB [NAVIGATION_DEB|TASK_DEB|COOPERATION_DEB ...]" >&2
  exit 2
fi
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
HOST_ARCH="$(dpkg --print-architecture)"
[[ "${HOST_ARCH}" == amd64 || "${HOST_ARCH}" == arm64 ]] || exit 2
[[ "$(dpkg-deb -f "$1" Package)" == control-driver ]] || exit 2
MOUNTS=()
INDEX=0
for DEB_PATH in "$@"; do
  [[ -f "${DEB_PATH}" ]] || exit 2
  [[ "$(dpkg-deb -f "${DEB_PATH}" Architecture)" == "${HOST_ARCH}" ]] || exit 2
  case "$(dpkg-deb -f "${DEB_PATH}" Package)" in
    control-driver|control-navigation|control-task|control-cooperation) ;;
    *) exit 2 ;;
  esac
  MOUNTS+=(-v "$(realpath "${DEB_PATH}"):/tmp/package-${INDEX}.deb:ro")
  INDEX=$((INDEX + 1))
done
docker run --rm --platform "linux/${HOST_ARCH}" \
  "${MOUNTS[@]}" \
  -v "${SCRIPT_DIR}/validate_navigation_deb.py:/tmp/validate_navigation_deb.py:ro" \
  --entrypoint bash ros:jazzy-ros-base -ec '
    export DEBIAN_FRONTEND=noninteractive
    apt-get update -qq
    mkdir -p /tmp/navigation-smoke-home
    chmod 777 /tmp/navigation-smoke-home
    for ((index=0; ; index++)); do
      package="/tmp/package-${index}.deb"
      [[ -f "${package}" ]] || break
      apt-get install -y --no-install-recommends "${package}"
      runuser -u nobody -- env HOME=/tmp/navigation-smoke-home bash -ec "
        source /opt/ros/jazzy/setup.bash
        source /opt/inspection/driver/local_setup.bash
        for component in navigation task cooperation; do
          if [[ -f /opt/inspection/\${component}/local_setup.bash ]]; then
            source /opt/inspection/\${component}/local_setup.bash
          fi
        done
        python3 /tmp/validate_navigation_deb.py
      "
    done
    for component in cooperation task navigation; do
      if dpkg-query -W -f="\${db:Status-Status}" "control-${component}" 2>/dev/null | grep -qx installed; then
        apt-get remove -y "control-${component}"
        test -f /opt/inspection/driver/local_setup.bash
      fi
    done
    apt-get remove -y control-driver
    test ! -f /opt/inspection/driver/lib/nav_bridge/r750_nav_bridge_node
    echo "PASS: apt installation, sequential upgrades, unprivileged checks and removal"
  '
