#!/usr/bin/env python3
"""Build cooperation_ws without changing inspection/navigation build behavior."""
import argparse
import os
from pathlib import Path
import subprocess
import sys

from cooperation_workspace import PACKAGES, check
from update_repositories import (clone_repository, git, managed_repositories,
                                 update_repositories)

SCRIPTS = Path(__file__).resolve().parent


def arguments():
    task = SCRIPTS.parent.parent.parent
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('target_package', nargs='?', help='default: all nine cooperation packages')
    parser.add_argument('--workspace-root', type=Path, default=task.parent / 'cooperation_ws')
    parser.add_argument('--task-workspace', type=Path, default=task)
    parser.add_argument('--repos-file', type=Path, default=SCRIPTS.parent / 'config/cooperation_deps.repos')
    parser.add_argument('-j', '--jobs', type=int, default=os.environ.get('BUILD_JOBS', '1'))
    group = parser.add_mutually_exclusive_group()
    group.add_argument('--fetch-only', action='store_true')
    group.add_argument('--build-only', action='store_true')
    parser.add_argument('--rosdep', action='store_true')
    parser.add_argument('--no-branch-check', action='store_true')
    parser.add_argument('--check-updates', action='store_true')
    parser.add_argument('--update-current-branch', action='store_true')
    parser.add_argument('--update-only', action='store_true')
    parser.add_argument('--checkout', choices=('dev', 'main'))
    parser.add_argument('--remote')
    args = parser.parse_args()
    if args.jobs < 1:
        parser.error('build jobs must be a positive integer')
    if args.checkout and (args.fetch_only or args.build_only):
        parser.error('--checkout conflicts with --fetch-only/--build-only')
    if args.remote and (not args.checkout or args.remote.startswith('-')):
        parser.error('--remote requires --checkout and a valid remote name')
    for name in ('workspace_root', 'task_workspace', 'repos_file'):
        setattr(args, name, getattr(args, name).expanduser().resolve())
    return args


def repository_status(entries, fetch):
    for relative, _spec, path in entries:
        if not path.exists():
            print(f'[branch] {relative}: missing', flush=True)
            continue
        try:
            branch = git(path, 'branch', '--show-current') or 'detached@' + git(path, 'rev-parse', '--short', 'HEAD')
            if git(path, 'status', '--porcelain'):
                branch += ' dirty'
            if fetch:
                try:
                    upstream = git(path, 'rev-parse', '--abbrev-ref', '@{upstream}')
                except ValueError:
                    branch += ' upstream=none'
                else:
                    git(path, 'fetch', '--quiet')
                    ahead, behind = git(path, 'rev-list', '--left-right', '--count', 'HEAD...' + upstream).split()
                    branch += f' upstream={upstream} ahead={ahead} behind={behind}'
            print(f'[branch] {relative}: {branch}', flush=True)
        except ValueError as exc:
            raise ValueError(f'{relative}: {exc}') from exc


def main():
    args = arguments()
    try:
        check(args.workspace_root, args.task_workspace)
        entries = list(managed_repositories(args.repos_file, args.workspace_root, require_git=False))
        if not args.build_only and not args.checkout:
            for relative, spec, path in entries:
                if not path.exists():
                    print(f'[fetch] clone {relative}', flush=True)
                    clone_repository(path, spec)
        update = args.update_current_branch or args.update_only
        if update or args.checkout:
            update_repositories(args.repos_file, args.workspace_root,
                                checkout=args.checkout, remote=args.remote or 'origin',
                                update=update if args.checkout else True)
        if not args.no_branch_check or args.check_updates or update or args.checkout:
            repository_status(entries, args.check_updates)
        if args.fetch_only or args.update_only or args.checkout:
            print('[done] repository operation complete; no build performed', flush=True)
            return 0
        (args.workspace_root / 'src').mkdir(parents=True, exist_ok=True)
        command = ['colcon', 'build', '--base-paths', str(args.workspace_root / 'src'),
                   '--packages-up-to', *([args.target_package] if args.target_package else PACKAGES),
                   '--symlink-install', '--executor', 'sequential', '--cmake-force-configure',
                   '--cmake-args', '-Wno-dev', '-DCMAKE_BUILD_TYPE=Release',
                   '--no-warn-unused-cli', '-DPython3_EXECUTABLE=/usr/bin/python3',
                   '-DPYTHON_EXECUTABLE=/usr/bin/python3']
        # Fixed shell text; paths and arguments are positional, never interpolated.
        environment = dict(os.environ, MAKEFLAGS=f'-j{args.jobs}', CMAKE_BUILD_PARALLEL_LEVEL=str(args.jobs))
        shell = r'''
set -eo pipefail
unset AMENT_PREFIX_PATH COLCON_PREFIX_PATH CMAKE_PREFIX_PATH PYTHONPATH LD_LIBRARY_PATH
source /opt/ros/jazzy/setup.bash
if [[ ! -f "$1/install/local_setup.bash" ]]; then
  echo "error: build task_ws first: missing $1/install/local_setup.bash" >&2
  exit 2
fi
source "$1/install/local_setup.bash"
python3 - "$1" <<'CHECK'
from pathlib import Path
import sys
from ament_index_python.packages import get_package_prefix
if not Path(get_package_prefix('inspection_interfaces')).resolve().is_relative_to(Path(sys.argv[1]) / 'install'):
    raise SystemExit('error: inspection_interfaces must come from the selected task_ws')
CHECK
if [[ "$3" == rosdep ]]; then
  rosdep install --from-paths "$2/src" "$1/src/inspection_interfaces" --ignore-src -r -y
fi
cd "$2"
shift 3
exec "$@"
'''
        return subprocess.run(['bash', '-c', shell, 'cooperation-build', str(args.task_workspace),
                               str(args.workspace_root), 'rosdep' if args.rosdep else 'build', *command],
                              env=environment).returncode
    except (OSError, ValueError, subprocess.CalledProcessError) as exc:
        print(f'cooperation build error: {exc}', file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
