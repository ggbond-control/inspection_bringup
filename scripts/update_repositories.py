#!/usr/bin/env python3
"""Checkout remote-backed branches, or pull current branches using local Git settings."""

import argparse
from pathlib import Path
import subprocess

import yaml


MAIN_BRANCHES = ('main', 'jazzy', 'master')


def managed_repositories(repos_file, workspace_root, algor_ws=None, driver_ws=None, task_ws=None):
    with Path(repos_file).open(encoding='utf-8') as stream:
        repositories = (yaml.safe_load(stream) or {}).get('repositories', {})
    if not isinstance(repositories, dict) or not repositories:
        raise ValueError(f'no repositories found in {repos_file}')
    entries = []
    seen = set()
    for relative, spec in repositories.items():
        relative_path = Path(relative)
        if relative_path.is_absolute() or '..' in relative_path.parts or len(relative_path.parts) < 2:
            raise ValueError(f'invalid managed repository path: {relative}')
        if not isinstance(spec, dict):
            raise ValueError(f'{relative}: expected a repository mapping')
        if spec.get('type', 'git') != 'git':
            raise ValueError(f'{relative}: unsupported repository type')
        parts = relative.split('/', 1)
        roots = {'algor_ws': algor_ws, 'driver_ws': driver_ws, 'task_ws': task_ws}
        override = roots.get(parts[0]) if len(parts) == 2 else None
        path = Path(override) / parts[1] if override else Path(workspace_root) / relative
        # Do not accidentally run pull in a parent repository when a managed
        # path is missing or is only an ordinary directory. Worktrees use a file.
        if not (path / '.git').exists():
            raise ValueError(f'{relative}: not a Git repository: {path}')
        top = git(path, 'rev-parse', '--show-toplevel')
        if Path(top).resolve() != path.resolve():
            raise ValueError(f'{relative}: not a Git repository root: {path}')
        if path.resolve() in seen:
            raise ValueError(f'{relative}: duplicate managed repository: {path}')
        seen.add(path.resolve())
        entries.append((relative, spec, path))
    return entries


def git(path, *args):
    result = subprocess.run(['git', '-C', str(path), *args], capture_output=True, text=True)
    if result.returncode:
        raise ValueError(f'{path}: git {" ".join(args)} failed: {result.stderr.strip()}')
    return result.stdout.strip()


def ref_exists(path, ref):
    return subprocess.run(['git', '-C', str(path), 'show-ref', '--verify', '--quiet', ref],
                          stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode == 0


def remote_refs(path, remote):
    refs = {}
    for line in git(path, 'ls-remote', '--heads', '--tags', remote).splitlines():
        sha, ref = line.split('\t', 1)
        refs[ref] = sha
    return refs


def checkout_target(spec, refs, mode, remote):
    """None denotes a manifest-pinned tag; branches must exist on the server."""
    version = str(spec.get('version') or '')
    if version and f'refs/heads/{version}' not in refs and f'refs/tags/{version}' in refs:
        return None
    heads = {ref.removeprefix('refs/heads/') for ref in refs if ref.startswith('refs/heads/')}
    if mode == 'dev':
        if 'dev' not in heads:
            raise ValueError(f'remote branch {remote}/dev does not exist; no local branch created')
        return 'dev'
    preferred = str(spec.get('main_branch') or version)
    if spec.get('main_branch') and preferred not in MAIN_BRANCHES:
        raise ValueError('main_branch must be main, jazzy or master')
    candidates = [branch for branch in MAIN_BRANCHES if branch in heads]
    if preferred in MAIN_BRANCHES and preferred in heads:
        return preferred
    if spec.get('main_branch'):
        raise ValueError(f'remote branch {remote}/{preferred} does not exist')
    if len(candidates) == 1:
        return candidates[0]
    if not candidates:
        raise ValueError(f'no main branch on {remote}: expected main, jazzy or master')
    raise ValueError(f'ambiguous main branches: {", ".join(candidates)}; '
                     'set main_branch in the .repos entry')


def clone_repository(path, spec, *, checkout=None, remote='origin', depth=None):
    """Clone the resolved branch directly; a stale manifest branch cannot block checkout."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    version = spec.get('version')
    if checkout:
        refs = remote_refs(path.parent, spec['url'])
        target = checkout_target(spec, refs, checkout, remote)
        version = target if target is not None else version
    command = ['git', 'clone', '--origin', remote]
    if depth:
        command += ['--depth', str(depth)]
    if version:
        command += ['--branch', str(version)]
    result = subprocess.run([*command, spec['url'], str(path)])
    if result.returncode:
        raise ValueError(f'clone failed: {path}')


def checkout_repositories(entries, mode, remote, update):
    """Resolve and preflight every repository before changing any local branch.

    Fetching only changes remote refs/objects. Execution is intentionally not a
    transaction: on a later Git error we report completed repositories, never
    reset branches or discard changes to manufacture a rollback.
    """
    if mode not in ('dev', 'main'):
        raise ValueError('--checkout must be dev or main')
    if not remote or remote.startswith('-'):
        raise ValueError('invalid --remote')
    plans = []
    errors = []
    for relative, spec, path in entries:
        try:
            if remote not in git(path, 'remote').splitlines():
                raise ValueError(f'remote {remote!r} does not exist')
            for marker in ('MERGE_HEAD', 'rebase-merge', 'rebase-apply', 'CHERRY_PICK_HEAD',
                           'REVERT_HEAD', 'sequencer', 'BISECT_START'):
                marker_path = Path(git(path, 'rev-parse', '--git-path', marker))
                if not marker_path.is_absolute():
                    marker_path = path / marker_path
                if marker_path.exists():
                    raise ValueError(f'unfinished Git operation ({marker})')
            refs = remote_refs(path, remote)
            version = str(spec.get('version') or '')
            current = git(path, 'branch', '--show-current')
            if (version and f'refs/heads/{version}' not in refs
                    and ref_exists(path, f'refs/tags/{version}')
                    and f'refs/tags/{version}' not in refs):
                raise ValueError(f'manifest-pinned tag {version} is missing on {remote}; '
                                 'refusing to switch the pinned dependency')
            target = checkout_target(spec, refs, mode, remote)
            # A manifest tag is immutable input, not a branch-mode candidate.
            # Validate the actual commit as well, rather than silently skipping
            # a repository that has drifted away from its pinned dependency.
            if target is None:
                pinned = refs.get(f'refs/tags/{version}^{{}}', refs[f'refs/tags/{version}'])
                if current or git(path, 'rev-parse', 'HEAD') != pinned:
                    raise ValueError(f'expected detached pinned tag {version}; restore it before checkout')
                plans.append((relative, path, None, None))
                print(f'[checkout-plan] {relative}: keep pinned tag {version}', flush=True)
                continue
            remote_ref = f'refs/remotes/{remote}/{target}'
            # An explicit refspec works for --depth 1 / single-branch clones,
            # whose configured fetch refspec may not include the target at all.
            git(path, 'fetch', '--no-tags', remote, f'+refs/heads/{target}:{remote_ref}')
            local_ref = f'refs/heads/{target}'
            exists = ref_exists(path, local_ref)
            worktree = None
            for line in git(path, 'worktree', 'list', '--porcelain').splitlines():
                if line.startswith('worktree '):
                    worktree = line[len('worktree '):]
                elif line == f'branch {local_ref}' and Path(worktree).resolve() != path.resolve():
                    raise ValueError(f'{target} is checked out in another worktree: {worktree}')
            counts = ('0', '0')
            if exists:
                counts = tuple(git(path, 'rev-list', '--left-right', '--count',
                                   f'{local_ref}...{remote_ref}').split())
                if update and all(int(count) > 0 for count in counts):
                    raise ValueError(f'{target} diverged from {remote}/{target}; fast-forward required')
            action = 'switch' if exists else 'create tracking branch'
            print(f'[checkout-plan] {relative}: {current or "detached"} -> {target}; '
                  f'{action}; upstream={remote}/{target}; ahead={counts[0]} behind={counts[1]}', flush=True)
            plans.append((relative, path, target, remote_ref))
        except ValueError as exc:
            errors.append(f'{relative}: {exc}')
    if errors:
        raise ValueError('checkout preflight failed; no local branches changed:\n  ' + '\n  '.join(errors))

    completed = []
    try:
        for relative, path, target, remote_ref in plans:
            if target is None:
                continue
            # An explicit fetch does not extend a single-branch clone's fetch
            # configuration. Add the target so future ordinary pull works too.
            refspec = f'+refs/heads/{target}:{remote_ref}'
            mapped = subprocess.run(['git', '-C', str(path), 'config', '--get-all', f'remote.{remote}.fetch'],
                                    capture_output=True, text=True).stdout.splitlines()
            if refspec not in mapped and f'+refs/heads/*:refs/remotes/{remote}/*' not in mapped:
                git(path, 'config', '--add', f'remote.{remote}.fetch', refspec)
            # Let Git preserve compatible index/worktree edits and reject only
            # overwrites. A dirty worktree alone is not a checkout conflict.
            if ref_exists(path, f'refs/heads/{target}'):
                git(path, 'switch', '--no-guess', '--no-overwrite-ignore', target)
            else:
                git(path, 'switch', '--no-overwrite-ignore', '--track=direct', '-c', target, remote_ref)
            git(path, 'branch', f'--set-upstream-to={remote}/{target}', target)
            if update:
                git(path, 'merge', '--ff-only', '--no-autostash', '--no-overwrite-ignore', remote_ref)
            counts = git(path, 'rev-list', '--left-right', '--count', f'HEAD...{remote_ref}').split()
            print(f'[checkout] {relative}: {target} upstream={remote}/{target} '
                  f'ahead={counts[0]} behind={counts[1]}', flush=True)
            completed.append(relative)
    except (OSError, ValueError) as exc:
        raise ValueError(f'{exc}\nCheckout stopped; build will not start. '
                         f'Completed repositories: {", ".join(completed) or "none"}. '
                         'The failing repository may also have changed; no automatic rollback.') from exc
    print('[done] managed repository checkout complete', flush=True)
    return {relative: target for relative, _, target, _ in plans if target is not None}


def update_repositories(repos_file, workspace_root, algor_ws=None, driver_ws=None, task_ws=None,
                        *, checkout=None, remote='origin', update=True):
    entries = managed_repositories(repos_file, workspace_root, algor_ws, driver_ws, task_ws)
    if checkout:
        return checkout_repositories(entries, checkout, remote, update)

    for relative, spec, path in entries:

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
    parser.add_argument('--task-ws')
    parser.add_argument('--checkout', choices=('dev', 'main'))
    parser.add_argument('--remote', default='origin')
    parser.add_argument('--no-update', action='store_true', help='checkout without merging remote updates')
    args = parser.parse_args()
    if args.no_update and not args.checkout:
        parser.error('--no-update requires --checkout')
    if args.remote != 'origin' and not args.checkout:
        parser.error('--remote requires --checkout')
    try:
        update_repositories(args.repos_file, args.workspace_root, args.algor_ws, args.driver_ws, args.task_ws,
                            checkout=args.checkout, remote=args.remote, update=not args.no_update)
    except (OSError, ValueError, yaml.YAMLError) as exc:
        parser.exit(1, f'[error] {exc}\n')


if __name__ == '__main__':
    main()
