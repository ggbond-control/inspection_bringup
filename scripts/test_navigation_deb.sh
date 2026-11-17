#!/usr/bin/env bash
set -euo pipefail
if [[ $# -lt 1 || $# -gt 3 || ! -f "$1" ]]; then
  echo "Usage: bash scripts/test_navigation_deb.sh DRIVER_DEB [NAVIGATION_DEB [NEWER_NAVIGATION_DEB]]" >&2
  exit 2
fi
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DEB_PATH="$(realpath "$1")"
DEB_ARCH="$(dpkg-deb -f "${DEB_PATH}" Architecture)"
[[ "$(dpkg-deb -f "${DEB_PATH}" Package)" == control-driver ]] || exit 2
HOST_ARCH="$(dpkg --print-architecture)"
if [[ "${DEB_ARCH}" != "${HOST_ARCH}" || ( "${HOST_ARCH}" != amd64 && "${HOST_ARCH}" != arm64 ) ]]; then
  echo "Run this check on the matching amd64/arm64 host (deb=${DEB_ARCH}, host=${HOST_ARCH})" >&2
  exit 2
fi
UPGRADE_MOUNT=()
NAVIGATION_MOUNT=()
if [[ $# -ge 2 ]]; then
  [[ -f "$2" ]] || exit 2
  [[ "$(dpkg-deb -f "$2" Architecture)" == "${DEB_ARCH}" ]] || exit 2
  [[ "$(dpkg-deb -f "$2" Package)" == control-navigation ]] || exit 2
  NAVIGATION_MOUNT=(-v "$(realpath "$2"):/tmp/navigation.deb:ro")
fi
if [[ $# -eq 3 ]]; then
  [[ -f "$3" ]] || exit 2
  [[ "$(dpkg-deb -f "$3" Architecture)" == "${DEB_ARCH}" ]] || exit 2
  [[ "$(dpkg-deb -f "$3" Package)" == control-navigation ]] || exit 2
  UPGRADE_MOUNT=(-v "$(realpath "$3"):/tmp/navigation-upgrade.deb:ro")
fi
docker run --rm --platform "linux/${DEB_ARCH}" \
  "${UPGRADE_MOUNT[@]}" "${NAVIGATION_MOUNT[@]}" \
  -v "${DEB_PATH}:/tmp/driver.deb:ro" \
  -v "${SCRIPT_DIR}/validate_navigation_deb.py:/tmp/validate_navigation_deb.py:ro" \
  --entrypoint bash ros:jazzy-ros-base -ec '
    export DEBIAN_FRONTEND=noninteractive
    apt-get update -qq
    apt-get install -y --no-install-recommends /tmp/driver.deb
    if [[ -f /tmp/navigation.deb ]]; then
      apt-get install -y --no-install-recommends /tmp/navigation.deb
    fi
    mkdir -p /tmp/navigation-smoke-home
    chmod 777 /tmp/navigation-smoke-home
    runuser -u nobody -- env HOME=/tmp/navigation-smoke-home bash -ec "
      source /opt/ros/jazzy/setup.bash
      source /opt/inspection/driver/local_setup.bash
      if [[ -f /opt/inspection/navigation/local_setup.bash ]]; then
        source /opt/inspection/navigation/local_setup.bash
      fi
      python3 /tmp/validate_navigation_deb.py
    "
    if [[ -f /tmp/navigation-upgrade.deb ]]; then
      apt-get install -y --no-install-recommends /tmp/navigation-upgrade.deb
      runuser -u nobody -- env HOME=/tmp/navigation-smoke-home bash -ec "
        source /opt/ros/jazzy/setup.bash
        source /opt/inspection/driver/local_setup.bash
        source /opt/inspection/navigation/local_setup.bash
        python3 /tmp/validate_navigation_deb.py
      "
      echo "PASS: package upgrade"
    fi
    if dpkg-query -W -f="\${db:Status-Status}" control-navigation 2>/dev/null | grep -qx installed; then
      apt-get remove -y control-navigation
      test -f /opt/inspection/driver/local_setup.bash
    fi
    test ! -f /opt/inspection/navigation/lib/local_planner/pathFollower
    apt-get remove -y control-driver
    test ! -f /opt/inspection/driver/lib/nav_bridge/r750_nav_bridge_node
    echo "PASS: apt install, unprivileged runtime checks and removal"
  '
