#!/usr/bin/env bash
set -euo pipefail
if [[ $# -lt 1 || $# -gt 2 || ! -f "$1" ]]; then
  echo "Usage: bash scripts/test_navigation_deb.sh DEB [NEWER_DEB]" >&2
  exit 2
fi
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DEB_PATH="$(realpath "$1")"
UPGRADE_MOUNT=()
if [[ $# -eq 2 ]]; then
  [[ -f "$2" ]] || exit 2
  UPGRADE_MOUNT=(-v "$(realpath "$2"):/tmp/navigation-upgrade.deb:ro")
fi
docker run --rm \
  "${UPGRADE_MOUNT[@]}" \
  -v "${DEB_PATH}:/tmp/navigation.deb:ro" \
  -v "${SCRIPT_DIR}/validate_navigation_deb.py:/tmp/validate_navigation_deb.py:ro" \
  --entrypoint bash osrf/ros:jazzy-desktop-full -ec '
    export DEBIAN_FRONTEND=noninteractive
    apt-get update -qq
    apt-get install -y --no-install-recommends /tmp/navigation.deb
    mkdir -p /tmp/navigation-smoke-home
    chmod 777 /tmp/navigation-smoke-home
    runuser -u nobody -- env HOME=/tmp/navigation-smoke-home bash -ec "
      source /opt/ros/jazzy/setup.bash
      source /opt/inspection/navigation/local_setup.bash
      python3 /tmp/validate_navigation_deb.py
    "
    if [[ -f /tmp/navigation-upgrade.deb ]]; then
      apt-get install -y --no-install-recommends /tmp/navigation-upgrade.deb
      runuser -u nobody -- env HOME=/tmp/navigation-smoke-home bash -ec "
        source /opt/ros/jazzy/setup.bash
        source /opt/inspection/navigation/local_setup.bash
        python3 /tmp/validate_navigation_deb.py
      "
      echo "PASS: package upgrade"
    fi
    apt-get remove -y navigation
    test ! -f /opt/inspection/navigation/lib/local_planner/pathFollower
    echo "PASS: apt install, unprivileged runtime checks and removal"
  '
