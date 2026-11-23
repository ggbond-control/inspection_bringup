#!/usr/bin/env python3
"""Check workspace ownership, or migrate stopped cooperation repositories once."""
import argparse
from datetime import datetime
import json
from pathlib import Path
import shutil
import subprocess
import tarfile

import yaml
from bringup_config import load_config, read_yaml

PACKAGES = (
    'capability_mission_planner', 'fleet_leader_election', 'mission_execution_agent',
    'swarm_protocol', 'swarm_consensus', 'swarm_mission_planner',
    'swarm_execution_agent', 'swarm_mqtt_transport', 'swarm_mission_coordinator',
)
DATA_PACKAGES = {
    'mission_execution_agent': ('coordination', 'agent'),
    'fleet_leader_election': ('coordination', 'election'),
    'swarm_consensus': ('swarm', 'consensus'),
    'swarm_mission_coordinator': ('swarm', 'coordinator'),
    'swarm_mqtt_transport': ('swarm', 'transport'),
    'swarm_execution_agent': ('swarm', 'agent'),
}


def legacy_paths(task):
    return [task / area / name for area in ('src', 'build', 'install')
            for name in PACKAGES if (task / area / name).exists() or (task / area / name).is_symlink()]


def check(workspace, task):
    if workspace == task or task in workspace.parents or workspace in task.parents:
        raise ValueError('task_ws and cooperation_ws must be separate workspaces')
    old = legacy_paths(task)
    old.extend(task / 'install/share/ament_index/resource_index/packages' / name for name in PACKAGES
               if (task / 'install/share/ament_index/resource_index/packages' / name).exists())
    if old:
        raise ValueError('legacy cooperation packages still exist in task_ws: '
                         + ', '.join(map(str, old)) + '; migrate them before building cooperation_ws')


def migrate(args):
    workspace, task = args.workspace, args.task_workspace
    if workspace == task or task in workspace.parents or workspace in task.parents:
        raise ValueError('workspaces must be separate')
    if not task.is_dir() or not (task / 'src').is_dir():
        raise ValueError(f'not a task workspace: {task}')
    if '/' in args.robot_id or args.robot_id in ('.', '..') or not args.robot_id.strip():
        raise ValueError('robot_id must be a nonempty directory name')
    config_root = (args.config_root or task / 'src/inspection_bringup/config').resolve()
    config = load_config('system', config_dir=config_root, resolve=False)
    settings = config.get('cooperation', {})
    if settings.get('robot_id', args.robot_id) != args.robot_id:
        raise ValueError('robot_id does not match device configuration')
    moves = [(task / 'src' / name, workspace / 'src' / name)
             for name in PACKAGES if (task / 'src' / name).exists()]
    for source, destination in moves:
        if source.is_symlink() or destination.exists() or destination.is_symlink():
            raise ValueError(f'cannot move {source}: source symlink or destination exists: {destination}')
        if not (source / '.git').is_dir():
            raise ValueError(f'linked worktrees/submodules need separate Git relocation: {source}')
        actual = subprocess.check_output(['git', '-C', str(source), 'rev-parse', '--show-toplevel'], text=True).strip()
        if Path(actual).resolve() != source.resolve():
            raise ValueError(f'not an independent Git repository: {source}')
    if (task / 'install/.colcon_install_layout').exists():
        if (task / 'install/.colcon_install_layout').read_text().strip() != 'isolated':
            raise ValueError('merged install detected; back up and rebuild task_ws in isolation first')

    # Source-adjacent runtime moves with its repository. Only external/custom
    # roots (including old ~/.ros/swarm data) need a separate relocation.
    data_moves = []
    sources = []
    swarm_base = args.swarm_data or Path('~/.ros/swarm').expanduser()
    for package, (stack, section) in DATA_PACKAGES.items():
        source_repo = task / 'src' / package
        destination = workspace / 'src' / package / 'runtime'
        configured = settings.get(stack, {}).get(section, {}).get('data_root')
        explicit = {'mission_execution_agent': args.agent_data,
                    'fleet_leader_election': args.election_data}.get(package)
        candidates = [explicit] if explicit else [source_repo / 'runtime']
        if not explicit and configured:
            candidates.append(Path(configured).expanduser())
        if stack == 'swarm':
            candidates.append(swarm_base / args.robot_id / section)
        candidates = {path.resolve() for path in candidates if path.exists()}
        if len(candidates) > 1:
            raise ValueError(f'ambiguous runtime copies for {package}: {candidates}')
        if not candidates:
            continue
        source = next(iter(candidates))
        if source == destination.resolve():
            continue
        if destination.exists() or destination.is_symlink():
            raise ValueError(f'runtime destination already exists: {destination}')
        if any(source == root.resolve() or source in root.resolve().parents
               for root in [task, workspace, config_root, *[a for a, _ in moves]]):
            raise ValueError(f'runtime source contains a workspace/repository/config root: {source}')
        sources.append(source)
        if source == (source_repo / 'runtime').resolve() and source_repo.exists():
            continue
        if not source_repo.exists() and not (workspace / 'src' / package / '.git').is_dir():
            raise ValueError(f'missing repository for runtime destination: {package}')
        data_moves.append((source, destination))

    robot_path = config_root / 'robot.yaml'
    config_path = robot_path if robot_path.exists() else config_root / 'system.yaml'
    document = read_yaml(config_path)
    owner = document.setdefault('system', {}) if config_path == robot_path else document
    for package, (stack, section) in DATA_PACKAGES.items():
        if stack != 'coordination':
            continue
        desired = str(workspace / 'src' / package / 'runtime')
        if settings.get(stack, {}).get(section, {}).get('data_root') != desired:
            owner.setdefault('cooperation', {}).setdefault(stack, {}).setdefault(section, {})['data_root'] = desired
    swarm_owner = owner.setdefault('cooperation', {}).setdefault('swarm', {})
    swarm_owner['data_root'] = str(workspace / 'src/swarm_mission_coordinator/runtime')
    for _, (stack, section) in DATA_PACKAGES.items():
        if stack == 'swarm' and isinstance(swarm_owner.get(section), dict):
            swarm_owner[section].pop('data_root', None)
    services = load_config('services', config_dir=config_root, resolve=False)
    if services.get('cooperation', {}).get('workspace_root') != str(workspace):
        service_owner = document.setdefault('services', {}) if config_path == robot_path else None
        if service_owner is None:
            raise ValueError('custom cooperation workspace requires robot.yaml services.cooperation.workspace_root')
        service_owner.setdefault('cooperation', {})['workspace_root'] = str(workspace)
    config_content = yaml.safe_dump(document, sort_keys=False)
    operations = {'config_update': str(config_path),
                  'repositories': [(str(a), str(b)) for a, b in moves],
                  'external_runtime': [(str(a), str(b)) for a, b in data_moves],
                  'archive_old_artifacts': [str(p) for p in legacy_paths(task) if p.parent.name != 'src']}
    print(json.dumps(operations, ensure_ascii=False, indent=2))
    if not args.apply:
        print('Dry run only. End/reset tasks and stop system/cooperation services before --apply.')
        return
    if not args.tasks_stopped:
        raise ValueError('--apply requires --tasks-stopped after ending/resetting the fleet round')
    for unit in (services.get('inspection_system', {}).get('service_name', 'inspection-system.service'),
                 services.get('cooperation', {}).get('service_name', 'inspection-cooperation.service')):
        result = subprocess.run(['systemctl', 'is-active', unit], capture_output=True, text=True)
        if result.stdout.strip() in ('active', 'activating', 'deactivating', 'reloading'):
            raise ValueError(f'stop {unit} before migration')
    processes = subprocess.check_output(['ps', '-eo', 'args='], text=True)
    if any(f'/{name}/lib/{name}/' in processes or f'{name}_node --ros-args' in processes for name in PACKAGES):
        raise ValueError('cooperation processes are still running; stop them before migration')
    if not moves and not data_moves and not operations['archive_old_artifacts']:
        raise ValueError('nothing to migrate')
    backup = workspace.parent / 'cooperation-backups' / datetime.now().strftime('%Y%m%d-%H%M%S-%f')
    backup.mkdir(parents=True, exist_ok=False)
    backup.chmod(0o700)
    with tarfile.open(backup / 'before-migration.tar.gz', 'w:gz') as archive:
        for index, source in enumerate(dict.fromkeys([a for a, _ in moves] + sources + [config_root])):
            archive.add(source, arcname=f'{index}-{source.name}')
    (backup / 'operations.json').write_text(json.dumps(operations, indent=2))

    def relocate(source, destination):
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(source), str(destination))
        with (backup / 'completed.jsonl').open('a') as stream:
            stream.write(json.dumps({'source': str(source), 'destination': str(destination)}) + '\n')

    for source, destination in moves:
        relocate(source, destination)
    for source, destination in data_moves:
        for old_repo, new_repo in moves:
            if source.is_relative_to(old_repo):
                source = new_repo / source.relative_to(old_repo)
                break
        relocate(source, destination)
    for source in legacy_paths(task):
        if source.parent.name != 'src':
            relocate(source, backup / 'old-artifacts' / source.parent.name / source.name)
    temporary = config_path.with_suffix(config_path.suffix + '.migration-tmp')
    temporary.write_text(config_content)
    temporary.replace(config_path)
    print(f'Migration saved. Backup and rollback journal: {backup}')
    print('Rebuild task_ws, then cooperation_ws. Migrate any legacy Swarm runtime with swarm_migrate_runtime before reinstalling services.')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=('check', 'migrate'))
    parser.add_argument('--workspace', required=True, type=Path)
    parser.add_argument('--task-workspace', required=True, type=Path)
    parser.add_argument('--robot-id')
    parser.add_argument('--config-root', type=Path)
    parser.add_argument('--tasks-stopped', action='store_true', help='fleet tasks ended/reset before migration')
    parser.add_argument('--agent-data', type=Path)
    parser.add_argument('--election-data', type=Path)
    parser.add_argument('--swarm-data', type=Path, help='old swarm base directory, before robot_id')
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    args.workspace = args.workspace.expanduser().resolve()
    args.task_workspace = args.task_workspace.expanduser().resolve()
    for name in ('agent_data', 'election_data', 'swarm_data', 'config_root'):
        value = getattr(args, name)
        if value is not None:
            value = value.expanduser().absolute()
            if not value.is_dir():
                parser.error(f'--{name.replace("_", "-")} directory does not exist: {value}')
            setattr(args, name, value)
    try:
        if args.command == 'check':
            check(args.workspace, args.task_workspace)
        else:
            if not args.robot_id:
                parser.error('migrate requires --robot-id')
            migrate(args)
    except (ValueError, OSError, subprocess.CalledProcessError) as exc:
        parser.exit(2, f'cooperation workspace error: {exc}\n')


if __name__ == '__main__':
    main()
