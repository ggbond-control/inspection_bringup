#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BRINGUP_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
DEFAULT_REPOS_FILE="${BRINGUP_DIR}/config/navigation_deps.repos"
DEFAULT_WORKSPACE_ROOT="${NAV_WORKSPACE_ROOT:-${HOME}/Workspace}"

WORKSPACE_ROOT="${DEFAULT_WORKSPACE_ROOT}"
ALGOR_WS=""
DRIVER_WS=""
REPOS_FILE="${DEFAULT_REPOS_FILE}"
INTERFACE_UNDERLAY=""
BUILD_JOBS="${BUILD_JOBS:-4}"
FETCH_ONLY=0
BUILD_ONLY=0
THIRD_PARTY_ONLY=0
SKIP_THIRD_PARTY=0
SKIP_DRIVER_BUILD=0
SKIP_ALGOR_BUILD=0
CHECK_BRANCHES=1
CHECK_UPDATES=0

usage() {
  cat <<'EOF'
Usage: build_navigation.sh [options]

Options:
  --fetch-only              Clone missing repositories and exit.
  --build-only              Skip repository cloning.
  --third-party-only        Build only local third-party libraries.
  --skip-third-party        Skip third-party library build.
  --skip-driver-build       Skip driver_ws colcon build.
  --skip-algor-build        Skip algor_ws colcon build.
  --repos-file PATH         Use an alternate .repos file.
  --workspace-root PATH     Workspace root. Default: ~/Workspace.
  --algor-ws PATH           algor_ws path. Default: WORKSPACE_ROOT/algor_ws.
  --driver-ws PATH          driver_ws path. Default: WORKSPACE_ROOT/driver_ws.
  --interface-underlay PATH Source an existing inspection_interfaces underlay.
  --no-branch-check         Do not print managed repository branch status.
  --check-updates           Fetch upstream refs and print ahead/behind status.
  -h, --help                Show this help.

Build behavior:
  Clones missing repositories with git clone --depth 1.
  Existing repositories are skipped without pull, checkout, or reset.
  Builds GTSAM and Livox-SDK2 into driver_ws/third_party/install.
  Does not run apt, sudo, sysctl, or any system-wide install step.
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --fetch-only)
      FETCH_ONLY=1
      shift
      ;;
    --build-only)
      BUILD_ONLY=1
      shift
      ;;
    --third-party-only)
      THIRD_PARTY_ONLY=1
      shift
      ;;
    --skip-third-party)
      SKIP_THIRD_PARTY=1
      shift
      ;;
    --skip-driver-build)
      SKIP_DRIVER_BUILD=1
      shift
      ;;
    --skip-algor-build)
      SKIP_ALGOR_BUILD=1
      shift
      ;;
    --repos-file)
      if [[ $# -lt 2 ]]; then
        echo "error: --repos-file requires a path" >&2
        exit 2
      fi
      REPOS_FILE="$2"
      shift 2
      ;;
    --workspace-root)
      if [[ $# -lt 2 ]]; then
        echo "error: --workspace-root requires a path" >&2
        exit 2
      fi
      WORKSPACE_ROOT="$2"
      shift 2
      ;;
    --algor-ws)
      if [[ $# -lt 2 ]]; then
        echo "error: --algor-ws requires a path" >&2
        exit 2
      fi
      ALGOR_WS="$2"
      shift 2
      ;;
    --driver-ws)
      if [[ $# -lt 2 ]]; then
        echo "error: --driver-ws requires a path" >&2
        exit 2
      fi
      DRIVER_WS="$2"
      shift 2
      ;;
    --interface-underlay)
      if [[ $# -lt 2 ]]; then
        echo "error: --interface-underlay requires a path" >&2
        exit 2
      fi
      INTERFACE_UNDERLAY="$2"
      shift 2
      ;;
    --no-branch-check)
      CHECK_BRANCHES=0
      shift
      ;;
    --check-updates)
      CHECK_UPDATES=1
      CHECK_BRANCHES=1
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

if [[ "${FETCH_ONLY}" -eq 1 && "${BUILD_ONLY}" -eq 1 ]]; then
  echo "error: --fetch-only and --build-only cannot be used together" >&2
  exit 2
fi

if [[ "${THIRD_PARTY_ONLY}" -eq 1 && "${SKIP_THIRD_PARTY}" -eq 1 ]]; then
  echo "error: --third-party-only and --skip-third-party cannot be used together" >&2
  exit 2
fi

REPOS_FILE="$(realpath -m "${REPOS_FILE}")"
WORKSPACE_ROOT="$(realpath -m "${WORKSPACE_ROOT}")"
if [[ -z "${ALGOR_WS}" ]]; then
  ALGOR_WS="${WORKSPACE_ROOT}/algor_ws"
fi
if [[ -z "${DRIVER_WS}" ]]; then
  DRIVER_WS="${WORKSPACE_ROOT}/driver_ws"
fi
ALGOR_WS="$(realpath -m "${ALGOR_WS}")"
DRIVER_WS="$(realpath -m "${DRIVER_WS}")"
THIRD_PARTY_DIR="${DRIVER_WS}/third_party"
THIRD_PARTY_PREFIX="${THIRD_PARTY_DIR}/install"

if [[ -n "${INTERFACE_UNDERLAY}" ]]; then
  INTERFACE_UNDERLAY="$(realpath -m "${INTERFACE_UNDERLAY}")"
fi

if [[ ! -f "${REPOS_FILE}" ]]; then
  echo "error: repos file not found: ${REPOS_FILE}" >&2
  exit 2
fi

mkdir -p "${ALGOR_WS}/src" "${DRIVER_WS}/src" "${THIRD_PARTY_DIR}/src"

fetch_missing_repositories() {
  python3 - "$WORKSPACE_ROOT" "$ALGOR_WS" "$DRIVER_WS" "$REPOS_FILE" <<'PY'
import os
import subprocess
import sys

import yaml

workspace_root = sys.argv[1]
algor_ws = sys.argv[2]
driver_ws = sys.argv[3]
repos_file = sys.argv[4]


def resolve_repo_path(rel_path):
    parts = rel_path.split("/", 1)
    if len(parts) == 2 and parts[0] == "algor_ws":
        return os.path.join(algor_ws, parts[1])
    if len(parts) == 2 and parts[0] == "driver_ws":
        return os.path.join(driver_ws, parts[1])
    return os.path.join(workspace_root, rel_path)

with open(repos_file, "r", encoding="utf-8") as stream:
    data = yaml.safe_load(stream) or {}

repositories = data.get("repositories", {})
if not repositories:
    raise SystemExit(f"no repositories found in {repos_file}")

for rel_path, spec in repositories.items():
    repo_type = spec.get("type", "git")
    url = spec.get("url")
    version = spec.get("version")
    abs_path = resolve_repo_path(rel_path)

    if repo_type != "git":
        raise SystemExit(f"unsupported repository type for {rel_path}: {repo_type}")
    if not url:
        raise SystemExit(f"missing url for {rel_path}")

    if os.path.exists(abs_path):
        print(f"[fetch] skip existing {rel_path}")
        continue

    os.makedirs(os.path.dirname(abs_path), exist_ok=True)
    cmd = ["git", "clone", "--depth", "1"]
    if version:
        cmd += ["--branch", str(version)]
    cmd += [url, abs_path]
    print(f"[fetch] clone {url} -> {rel_path}")
    subprocess.run(cmd, check=True)
PY
}

print_repository_branches() {
  python3 - "$WORKSPACE_ROOT" "$ALGOR_WS" "$DRIVER_WS" "$REPOS_FILE" "$CHECK_UPDATES" <<'PY'
import os
import subprocess
import sys

import yaml

workspace_root = sys.argv[1]
algor_ws = sys.argv[2]
driver_ws = sys.argv[3]
repos_file = sys.argv[4]
check_updates = sys.argv[5] == "1"


def resolve_repo_path(rel_path):
    parts = rel_path.split("/", 1)
    if len(parts) == 2 and parts[0] == "algor_ws":
        return os.path.join(algor_ws, parts[1])
    if len(parts) == 2 and parts[0] == "driver_ws":
        return os.path.join(driver_ws, parts[1])
    return os.path.join(workspace_root, rel_path)

with open(repos_file, "r", encoding="utf-8") as stream:
    data = yaml.safe_load(stream) or {}

repositories = data.get("repositories", {})
if not repositories:
    raise SystemExit(f"no repositories found in {repos_file}")


def git_output(repo_path, args):
    try:
        return subprocess.check_output(
            ["git", "-C", repo_path] + args,
            stderr=subprocess.DEVNULL,
            text=True,
        ).strip()
    except subprocess.CalledProcessError:
        return ""


def git_ok(repo_path, args):
    try:
        subprocess.run(
            ["git", "-C", repo_path] + args,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=True,
        )
        return True
    except subprocess.CalledProcessError:
        return False


def update_status(repo_path):
    shallow = git_output(repo_path, ["rev-parse", "--is-shallow-repository"])
    upstream = git_output(repo_path, ["rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{upstream}"])
    if not upstream:
        return "upstream=none"

    if not git_ok(repo_path, ["fetch", "--quiet"]):
        return f"upstream={upstream} fetch-failed"

    counts = git_output(repo_path, ["rev-list", "--left-right", "--count", f"HEAD...{upstream}"])
    try:
        ahead_text, behind_text = counts.split()
        ahead = int(ahead_text)
        behind = int(behind_text)
    except ValueError:
        return f"upstream={upstream} status-unknown"

    if ahead == 0 and behind == 0:
        state = "up-to-date"
    elif ahead > 0 and behind > 0:
        state = f"ahead={ahead} behind={behind}"
    elif ahead > 0:
        state = f"ahead={ahead}"
    else:
        state = f"behind={behind}"

    if shallow == "true":
        state = f"{state} shallow"
    return f"upstream={upstream} {state}"


for rel_path in repositories:
    abs_path = resolve_repo_path(rel_path)

    if not os.path.exists(abs_path):
        print(f"[branch] {rel_path}: missing")
        continue
    if not os.path.isdir(os.path.join(abs_path, ".git")) and not git_output(abs_path, ["rev-parse", "--git-dir"]):
        print(f"[branch] {rel_path}: not-git")
        continue

    branch = git_output(abs_path, ["branch", "--show-current"])
    if not branch:
        short_sha = git_output(abs_path, ["rev-parse", "--short", "HEAD"])
        branch = f"detached@{short_sha}" if short_sha else "unknown"

    parts = [branch]
    if git_output(abs_path, ["status", "--porcelain"]):
        parts.append("dirty")
    if check_updates:
        parts.append(update_status(abs_path))
    print(f"[branch] {rel_path}: {' '.join(parts)}")
PY
}

source_if_exists() {
  local setup_file="$1"
  if [[ -f "${setup_file}" ]]; then
    set +u
    # shellcheck disable=SC1090
    source "${setup_file}"
    set -u
  fi
}

source_required() {
  local setup_file="$1"
  local label="$2"
  if [[ ! -f "${setup_file}" ]]; then
    echo "error: ${label} setup not found: ${setup_file}" >&2
    exit 2
  fi
  source_if_exists "${setup_file}"
}

use_third_party_prefix() {
  export CMAKE_PREFIX_PATH="${THIRD_PARTY_PREFIX}:${CMAKE_PREFIX_PATH:-}"
  export CMAKE_LIBRARY_PATH="${THIRD_PARTY_PREFIX}/lib:${THIRD_PARTY_PREFIX}/lib64:${CMAKE_LIBRARY_PATH:-}"
  export CMAKE_INCLUDE_PATH="${THIRD_PARTY_PREFIX}/include:${CMAKE_INCLUDE_PATH:-}"
  export LD_LIBRARY_PATH="${THIRD_PARTY_PREFIX}/lib:${THIRD_PARTY_PREFIX}/lib64:${LD_LIBRARY_PATH:-}"
  export PKG_CONFIG_PATH="${THIRD_PARTY_PREFIX}/lib/pkgconfig:${THIRD_PARTY_PREFIX}/lib64/pkgconfig:${PKG_CONFIG_PATH:-}"
}

cmake_prefix_arg() {
  local value="${THIRD_PARTY_PREFIX}"
  if [[ -n "${CMAKE_PREFIX_PATH:-}" ]]; then
    value="${value};${CMAKE_PREFIX_PATH}"
  fi
  echo "${value}"
}

third_party_rpath_arg() {
  echo "${THIRD_PARTY_PREFIX}/lib;${THIRD_PARTY_PREFIX}/lib64"
}

build_cmake_project() {
  local name="$1"
  local src_dir="$2"
  local build_dir="$3"
  shift 3

  if [[ ! -d "${src_dir}" ]]; then
    echo "error: ${name} source not found: ${src_dir}" >&2
    exit 2
  fi

  echo "[third_party] configure ${name}"
  cmake -S "${src_dir}" -B "${build_dir}" "$@"
  echo "[third_party] build ${name}"
  cmake --build "${build_dir}" -j"${BUILD_JOBS}"
  echo "[third_party] install ${name}"
  cmake --install "${build_dir}"
}

build_third_party() {
  mkdir -p "${THIRD_PARTY_DIR}/build" "${THIRD_PARTY_PREFIX}"

  build_cmake_project \
    gtsam \
    "${THIRD_PARTY_DIR}/src/gtsam" \
    "${THIRD_PARTY_DIR}/build/gtsam" \
    -DCMAKE_BUILD_TYPE=Release \
    -DCMAKE_INSTALL_PREFIX="${THIRD_PARTY_PREFIX}" \
    -DGTSAM_USE_SYSTEM_EIGEN=ON \
    -DGTSAM_BUILD_TESTS=OFF \
    -DGTSAM_BUILD_EXAMPLES_ALWAYS=OFF \
    -DGTSAM_BUILD_UNSTABLE=ON

  build_cmake_project \
    livox_sdk2 \
    "${THIRD_PARTY_DIR}/src/livox_sdk2" \
    "${THIRD_PARTY_DIR}/build/livox_sdk2" \
    -DCMAKE_BUILD_TYPE=Release \
    -DCMAKE_INSTALL_PREFIX="${THIRD_PARTY_PREFIX}"
}

print_environment_summary() {
  cat <<EOF

[env] Navigation runtime environment:
  source /opt/ros/jazzy/setup.bash
  source ${DRIVER_WS}/install/setup.bash
EOF
  if [[ -n "${INTERFACE_UNDERLAY}" ]]; then
    echo "  source ${INTERFACE_UNDERLAY}"
  else
    echo "  # source an underlay that provides inspection_interfaces"
  fi
  cat <<EOF
  source ${ALGOR_WS}/install/setup.bash
  # Built packages include an RPATH for ${THIRD_PARTY_PREFIX}/lib and lib64.
  # Keep LD_LIBRARY_PATH as a fallback for ad-hoc tools or manually built binaries.
  export LD_LIBRARY_PATH=${THIRD_PARTY_PREFIX}/lib:${THIRD_PARTY_PREFIX}/lib64:\$LD_LIBRARY_PATH
  export CYCLONEDDS_URI=file://${DRIVER_WS}/src/livox_ros_driver2/config/cyclonedds_large_message.xml
  ros2 launch inspection_bringup navigation.launch.py

EOF
}

if [[ "${BUILD_ONLY}" -eq 0 ]]; then
  fetch_missing_repositories
fi

if [[ "${CHECK_BRANCHES}" -eq 1 ]]; then
  print_repository_branches
fi

if [[ "${FETCH_ONLY}" -eq 1 ]]; then
  echo "[done] fetch-only complete"
  exit 0
fi

source_required /opt/ros/jazzy/setup.bash "ROS 2 Jazzy"

if [[ "${SKIP_THIRD_PARTY}" -eq 0 ]]; then
  build_third_party
fi

use_third_party_prefix

if [[ "${THIRD_PARTY_ONLY}" -eq 1 ]]; then
  echo "[done] third-party-only complete"
  print_environment_summary
  exit 0
fi

if [[ "${SKIP_DRIVER_BUILD}" -eq 0 ]]; then
  echo "[build] driver_ws packages"
  cd "${DRIVER_WS}"
  MAKEFLAGS="-j${BUILD_JOBS}" colcon build \
    --packages-select control_input_msgs ndt_omp livox_ros_driver2 \
    --symlink-install \
    --parallel-workers "${BUILD_JOBS}" \
    --cmake-force-configure \
    --cmake-args \
      -Wno-dev \
      --no-warn-unused-cli \
      -DCMAKE_PREFIX_PATH="$(cmake_prefix_arg)" \
      -DCMAKE_LIBRARY_PATH="${THIRD_PARTY_PREFIX}/lib;${THIRD_PARTY_PREFIX}/lib64" \
      -DCMAKE_INCLUDE_PATH="${THIRD_PARTY_PREFIX}/include" \
      -DCMAKE_BUILD_RPATH="$(third_party_rpath_arg)" \
      -DCMAKE_INSTALL_RPATH="$(third_party_rpath_arg)" \
      -DCMAKE_INSTALL_RPATH_USE_LINK_PATH=ON \
      -DLIVOX_LIDAR_SDK_LIBRARY="${THIRD_PARTY_PREFIX}/lib/liblivox_lidar_sdk_shared.so" \
      -DLIVOX_LIDAR_SDK_INCLUDE_DIR="${THIRD_PARTY_PREFIX}/include"
fi

source_required "${DRIVER_WS}/install/setup.bash" "driver_ws"

if [[ -n "${INTERFACE_UNDERLAY}" ]]; then
  source_required "${INTERFACE_UNDERLAY}" "inspection_interfaces underlay"
fi

if [[ "${SKIP_ALGOR_BUILD}" -eq 0 ]]; then
  echo "[build] algor_ws packages"
  cd "${ALGOR_WS}"
  MAKEFLAGS="-j${BUILD_JOBS}" colcon build \
    --packages-select nav_bridge faster_lio gridmapper local_planner multi_map_nav inspection_bringup \
    --symlink-install \
    --parallel-workers "${BUILD_JOBS}" \
    --cmake-force-configure \
    --cmake-args \
      -Wno-dev \
      --no-warn-unused-cli \
      -DCMAKE_PREFIX_PATH="$(cmake_prefix_arg)" \
      -DCMAKE_LIBRARY_PATH="${THIRD_PARTY_PREFIX}/lib;${THIRD_PARTY_PREFIX}/lib64" \
      -DCMAKE_INCLUDE_PATH="${THIRD_PARTY_PREFIX}/include" \
      -DCMAKE_BUILD_RPATH="$(third_party_rpath_arg)" \
      -DCMAKE_INSTALL_RPATH="$(third_party_rpath_arg)" \
      -DCMAKE_INSTALL_RPATH_USE_LINK_PATH=ON \
      -DGTSAM_DIR="${THIRD_PARTY_PREFIX}/lib/cmake/GTSAM"
fi

echo "[done] navigation build complete"
print_environment_summary
