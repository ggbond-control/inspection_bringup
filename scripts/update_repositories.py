#!/usr/bin/env python3
"""Pull managed repositories using their current branch and local Git settings."""

import argparse
from pathlib import Path
import subprocess

import yaml


def update_repositories(repos_file, workspace_root, algor_ws=None, driver_ws=None):
    with Path(repos_file).open(encoding='utf-8') as stream:
        repositories = (yaml.safe_load(stream) or {}).get('repositories', {})
    if not repositories:
        raise ValueError(f'no repositories found in {repos_file}')

    for relative, spec in repositories.items():
        if spec.get('type', 'git') != 'git':
            raise ValueError(f'{relative}: unsupported repository type')
        parts = relative.split('/', 1)
        roots = {'algor_ws': algor_ws, 'driver_ws': driver_ws}
        override = roots.get(parts[0]) if len(parts) == 2 else None
        path = Path(override) / parts[1] if override else Path(workspace_root) / relative
        # Do not accidentally run pull in a parent repository when a managed
        # path is missing or is only an ordinary directory. Worktrees use a file.
        if not (path / '.git').exists():
            raise ValueError(f'{relative}: not a Git repository: {path}')

        branch = subprocess.run(
            ['git', '-C', str(path), 'symbolic-ref', '--quiet', 'HEAD'],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        version = spec.get('version')
        if branch.returncode and version:
            tag = subprocess.run(
                ['git', '-C', str(path), 'rev-parse', '--verify', f'refs/tags/{version}^{{commit}}'],
                capture_output=True, text=True)
            head = subprocess.run(
                ['git', '-C', str(path), 'rev-parse', 'HEAD'], capture_output=True, text=True)
            if tag.returncode == 0 and head.returncode == 0 and tag.stdout == head.stdout:
                print(f'[update] {relative}: keep pinned tag {version}', flush=True)
                continue

        print(f'[update] {relative}: git pull', flush=True)
        # No cleanliness/ahead/behind gate and no forced pull strategy: Git
        # decides whether and how this repository can be pulled.
        result = subprocess.run(['git', '-C', str(path), 'pull'])
        if result.returncode:
            raise ValueError(
                f'{relative}: git pull failed; build will not start. '
                'Earlier repositories may already have been updated.')
    print('[done] managed repository pull complete', flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--repos-file', required=True)
    parser.add_argument('--workspace-root', required=True)
    parser.add_argument('--algor-ws')
    parser.add_argument('--driver-ws')
    args = parser.parse_args()
    try:
        update_repositories(args.repos_file, args.workspace_root, args.algor_ws, args.driver_ws)
    except (OSError, ValueError, yaml.YAMLError) as exc:
        parser.exit(1, f'[error] {exc}\n')


if __name__ == '__main__':
    main()
