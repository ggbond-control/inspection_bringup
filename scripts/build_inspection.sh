#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BRINGUP_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
DEFAULT_REPOS_FILE="${BRINGUP_DIR}/config/inspection_deps.repos"
WORKSPACE_ROOT="$(cd "${BRINGUP_DIR}/../.." && pwd)"
TARGET_PACKAGE="inspection_bringup"
FETCH_ONLY=0
BUILD_ONLY=0
RUN_ROSDEP=0
REPOS_FILE="${DEFAULT_REPOS_FILE}"
BUILD_JOBS="${BUILD_JOBS:-4}"
CHECK_BRANCHES=1
CHECK_UPDATES=0
UPDATE_CURRENT_BRANCH=0
UPDATE_ONLY=0

usage() {
  cat <<'EOF'
Usage: build_inspection.sh [options] [target_package]

Options:
  --fetch-only          Clone missing repositories and exit.
  --build-only          Skip repository cloning.
  --rosdep             Run rosdep install before building.
  --repos-file PATH    Use an alternate .repos file.
  --no-branch-check    Do not print managed repository branch status.
  --check-updates      Fetch upstream refs and print ahead/behind status.
  --update-current-branch
                      Fast-forward existing managed repositories to their
                      current branch upstream, then build.
  --update-only        Update current branches and exit without building.
  -h, --help           Show this help.

Build behavior:
  Uses only: colcon build --packages-up-to <target> --symlink-install
  It never runs a bare global colcon build.
  Limits build parallelism with BUILD_JOBS, default: 4.
  Missing repositories are cloned from their remote default branch.
  Existing repositories are skipped without branch checks or checkout changes.
  Managed repository branches are printed by default without checkout changes.
  --check-updates adds remote fetch/status checks but never pulls or checks out.
  --update-current-branch is fast-forward only. It does not switch branches,
  stash changes, rebase, or create merge commits.

Default target_package: inspection_bringup
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
    --rosdep)
      RUN_ROSDEP=1
      shift
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
    --update-current-branch)
      UPDATE_CURRENT_BRANCH=1
      CHECK_BRANCHES=1
      shift
      ;;
    --update-only)
      UPDATE_CURRENT_BRANCH=1
      UPDATE_ONLY=1
      CHECK_BRANCHES=1
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
      TARGET_PACKAGE="$1"
      shift
      ;;
  esac
done

if [[ "${FETCH_ONLY}" -eq 1 && "${BUILD_ONLY}" -eq 1 ]]; then
  echo "error: --fetch-only and --build-only cannot be used together" >&2
  exit 2
fi

if [[ ! -f "${REPOS_FILE}" ]]; then
  echo "error: repos file not found: ${REPOS_FILE}" >&2
  exit 2
fi

if [[ ! -d "${WORKSPACE_ROOT}/src" ]]; then
  mkdir -p "${WORKSPACE_ROOT}/src"
fi

fetch_missing_repositories() {
  python3 - "$WORKSPACE_ROOT" "$REPOS_FILE" <<'PY'
import os
import subprocess
import sys

import yaml

workspace_root = sys.argv[1]
repos_file = sys.argv[2]

with open(repos_file, "r", encoding="utf-8") as stream:
    data = yaml.safe_load(stream) or {}

repositories = data.get("repositories", {})
if not repositories:
    raise SystemExit(f"no repositories found in {repos_file}")

for rel_path, spec in repositories.items():
    repo_type = spec.get("type", "git")
    url = spec.get("url")
    version = spec.get("version")
    abs_path = os.path.join(workspace_root, rel_path)

    if repo_type != "git":
        raise SystemExit(f"unsupported repository type for {rel_path}: {repo_type}")
    if not url:
        raise SystemExit(f"missing url for {rel_path}")

    if os.path.exists(abs_path):
        print(f"[fetch] skip existing {rel_path}")
        continue

    os.makedirs(os.path.dirname(abs_path), exist_ok=True)
    cmd = ["git", "clone"]
    if version:
        cmd += ["--branch", str(version)]
    cmd += [url, abs_path]
    print(f"[fetch] clone {url} -> {rel_path}")
    subprocess.run(cmd, check=True)
PY
}

print_repository_branches() {
  python3 - "$WORKSPACE_ROOT" "$REPOS_FILE" "$CHECK_UPDATES" <<'PY'
import os
import subprocess
import sys

import yaml

workspace_root = sys.argv[1]
repos_file = sys.argv[2]
check_updates = sys.argv[3] == "1"

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

    return f"upstream={upstream} {state}"


for rel_path in repositories:
    abs_path = os.path.join(workspace_root, rel_path)

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

update_current_branches() {
  python3 - "$WORKSPACE_ROOT" "$REPOS_FILE" <<'PY'
import os
import subprocess
import sys

import yaml

workspace_root = sys.argv[1]
repos_file = sys.argv[2]

with open(repos_file, "r", encoding="utf-8") as stream:
    data = yaml.safe_load(stream) or {}

repositories = data.get("repositories", {})
if not repositories:
    raise SystemExit(f"no repositories found in {repos_file}")


def git_output(repo_path, args):
    result = subprocess.run(
        ["git", "-C", repo_path] + args,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if result.returncode:
        return None
    return result.stdout.strip()


def fail(rel_path, message):
    print(f"[update] {rel_path}: blocked: {message}", file=sys.stderr)


prepared = []
failed = False

# Check all local safety conditions before fetching or changing any worktree.
for rel_path, spec in repositories.items():
    repo_type = spec.get("type", "git")
    repo_path = os.path.join(workspace_root, rel_path)

    if repo_type != "git":
        fail(rel_path, f"unsupported repository type '{repo_type}'")
        failed = True
        continue
    if not os.path.exists(repo_path):
        fail(rel_path, "repository is missing")
        failed = True
        continue
    if git_output(repo_path, ["rev-parse", "--is-inside-work-tree"]) != "true":
        fail(rel_path, "not a Git worktree")
        failed = True
        continue
    if git_output(repo_path, ["status", "--porcelain"]):
        fail(rel_path, "working tree has local changes")
        failed = True
        continue

    branch = git_output(repo_path, ["symbolic-ref", "--quiet", "--short", "HEAD"])
    if not branch:
        fail(rel_path, "HEAD is detached")
        failed = True
        continue

    upstream = git_output(
        repo_path, ["rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{upstream}"]
    )
    if not upstream:
        fail(rel_path, f"branch '{branch}' has no upstream")
        failed = True
        continue

    remote = upstream.split("/", 1)[0]
    prepared.append((rel_path, repo_path, upstream, remote))

if failed:
    raise SystemExit("[error] update aborted; resolve the blocked repositories first")

# Fetch and compare every repository before updating any checkout.
update_candidates = []
for rel_path, repo_path, upstream, remote in prepared:
    result = subprocess.run(
        ["git", "-C", repo_path, "fetch", "--quiet", remote],
        text=True,
        stderr=subprocess.PIPE,
    )
    if result.returncode:
        message = result.stderr.strip() or f"failed to fetch remote '{remote}'"
        fail(rel_path, message)
        failed = True
        continue

    counts = git_output(repo_path, ["rev-list", "--left-right", "--count", f"HEAD...{upstream}"])
    try:
        ahead_text, behind_text = counts.split() if counts else ("", "")
        ahead = int(ahead_text)
        behind = int(behind_text)
    except ValueError:
        fail(rel_path, f"could not determine status against '{upstream}'")
        failed = True
        continue

    if ahead:
        fail(
            rel_path,
            f"local branch is ahead={ahead} behind={behind}; fast-forward is unsafe",
        )
        failed = True
        continue

    if behind:
        print(f"[update] {rel_path}: {behind} commit(s) behind {upstream}")
        update_candidates.append((rel_path, repo_path, upstream))
    else:
        print(f"[update] {rel_path}: up-to-date ({upstream})")

if failed:
    raise SystemExit("[error] update aborted; no repositories were merged")

for rel_path, repo_path, upstream in update_candidates:
    old_sha = git_output(repo_path, ["rev-parse", "--short", "HEAD"])
    result = subprocess.run(
        ["git", "-C", repo_path, "merge", "--ff-only", upstream],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if result.returncode:
        message = result.stderr.strip() or f"failed to fast-forward from '{upstream}'"
        fail(rel_path, message)
        raise SystemExit("[error] update failed; build will not start")

    new_sha = git_output(repo_path, ["rev-parse", "--short", "HEAD"])
    print(f"[update] {rel_path}: {old_sha} -> {new_sha}")

print(
    f"[done] checked {len(prepared)} managed repositories; "
    f"fast-forwarded {len(update_candidates)}"
)
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

if [[ "${BUILD_ONLY}" -eq 0 ]]; then
  fetch_missing_repositories
fi

if [[ "${UPDATE_CURRENT_BRANCH}" -eq 1 ]]; then
  update_current_branches
fi

if [[ "${CHECK_BRANCHES}" -eq 1 ]]; then
  print_repository_branches
fi

if [[ "${FETCH_ONLY}" -eq 1 ]]; then
  echo "[done] fetch-only complete"
  exit 0
fi

if [[ "${UPDATE_ONLY}" -eq 1 ]]; then
  echo "[done] update-only complete"
  exit 0
fi

source_if_exists /opt/ros/jazzy/setup.bash
source_if_exists "${WORKSPACE_ROOT}/install/setup.bash"

if [[ "${RUN_ROSDEP}" -eq 1 ]]; then
  echo "[rosdep] installing external dependencies"
  rosdep install --from-paths "${WORKSPACE_ROOT}/src" --ignore-src -r -y
fi

echo "[build] target package: ${TARGET_PACKAGE}"
echo "[build] parallel jobs: ${BUILD_JOBS}"
cd "${WORKSPACE_ROOT}"
if ! MAKEFLAGS="-j${BUILD_JOBS}" colcon build \
    --packages-up-to "${TARGET_PACKAGE}" \
    --symlink-install \
    --parallel-workers "${BUILD_JOBS}" \
    --cmake-force-configure \
    --cmake-args \
      -Wno-dev \
      --no-warn-unused-cli \
      -DPython3_EXECUTABLE=/usr/bin/python3 \
      -DPYTHON_EXECUTABLE=/usr/bin/python3 \
      -DGIMBAL_ENABLE_RKNN=AUTO \
      -DGIMBAL_ENABLE_HKNETSDK=AUTO; then
  cat >&2 <<'EOF'

[error] build failed.

If the failure mentions:
  failed to create symbolic link ... because existing path cannot be removed: Is a directory

then the workspace likely contains stale non-symlink build/install artifacts
from an earlier non --symlink-install build. Clean only the affected package
artifacts, then rerun this script. Example:

  rm -rf build/<package> install/<package>

EOF
  exit 1
fi

echo "[done] built packages up to ${TARGET_PACKAGE}"
