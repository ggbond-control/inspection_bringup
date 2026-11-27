"""Split-workspace deployment contracts; isolated directories, no robot/system changes."""
import json
import os
from pathlib import Path
import importlib.util
import shutil
import shlex
import subprocess
import sys
import tarfile
import tempfile
import unittest
from unittest.mock import patch

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
import bringup_config as cfg
import cooperation_workspace as workspace


class CooperationWorkspace(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='cooperation workspace ')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.task = self.root / 'task_ws'
        self.coop = self.root / 'cooperation_ws'
        self.config = self.task / 'src/inspection_bringup/config'
        shutil.copytree(ROOT / 'config', self.config, ignore=shutil.ignore_patterns('robot.yaml'))
        self.robot = {'model': 'x30', 'robot_id': 'TEST-A',
                      'system': {'cooperation': {'mode': 'coordination'}}}
        self.save_robot()
        self.env = dict(os.environ, INSPECTION_CONFIG_DIR=str(self.config))

    def save_robot(self):
        (self.config / 'robot.yaml').write_text(yaml.safe_dump(self.robot))

    def command(self, *args):
        return subprocess.run([sys.executable, str(ROOT / 'scripts/cooperation_workspace.py'), *args,
                               '--workspace', str(self.coop), '--task-workspace', str(self.task)],
                              env=self.env, capture_output=True, text=True)

    def test_check_rejects_legacy_artifacts_and_nested_workspaces(self):
        workspace.check(self.coop, self.task)
        for area in ('src', 'build', 'install'):
            path = self.task / area / 'mission_execution_agent'
            path.mkdir(parents=True)
            with self.assertRaisesRegex(ValueError, 'legacy cooperation'):
                workspace.check(self.coop, self.task)
            shutil.rmtree(path)
        with self.assertRaises(ValueError):
            workspace.check(self.task / 'cooperation_ws', self.task)

    def test_disabled_condition_needs_no_ros_workspace_or_sensor_packages(self):
        self.robot['system']['cooperation']['mode'] = 'disabled'
        self.save_robot()
        env = {k: v for k, v in self.env.items() if k not in ('AMENT_PREFIX_PATH', 'PYTHONPATH')}
        args = [sys.executable, str(ROOT / 'scripts/check_cooperation.py'),
                '--workspace', str(self.coop), '--task-workspace', str(self.task)]
        result = subprocess.run(args, env=env, capture_output=True, text=True)
        self.assertEqual(result.returncode, 1, result.stderr)
        self.robot['system']['cooperation']['mode'] = 'coordination'
        self.save_robot()
        result = subprocess.run(args, env=env, capture_output=True, text=True)
        self.assertEqual(result.returncode, 255, result.stderr)
        self.assertIn('build cooperation_ws first', result.stderr)
        self.robot['system']['cooperation']['mode'] = 'typo'
        self.save_robot()
        result = subprocess.run(args, env=env, capture_output=True, text=True)
        self.assertEqual(result.returncode, 255)

    def test_swarm_has_one_data_root_and_coordination_has_package_roots(self):
        config = cfg.load_config('system', config_dir=self.config, resolve=False)
        for package, (stack, section) in workspace.DATA_PACKAGES.items():
            if stack == 'coordination':
                path = config['cooperation'][stack][section]['data_root']
                self.assertTrue(path.endswith('/cooperation_ws/src/' + package + '/runtime'))
            else:
                self.assertNotIn('data_root', config['cooperation']['swarm'].get(section, {}))
        self.assertTrue(config['cooperation']['swarm']['data_root'].endswith(
            '/cooperation_ws/src/swarm_mission_coordinator/runtime'))

    def repo(self, name):
        path = self.task / 'src' / name
        path.mkdir()
        subprocess.run(['git', 'init', '-q', str(path)], check=True)
        (path / 'local.txt').write_text('keep uncommitted work')
        return path

    def test_migration_backs_up_state_git_and_config_and_archives_old_install(self):
        repo = self.repo('fleet_leader_election')
        data = repo / 'runtime'
        data.mkdir()
        (data / 'election_state.json').write_text('{"term": 17, "voted_for": "TEST-A"}')
        artifact = self.task / 'install/fleet_leader_election'
        artifact.mkdir(parents=True)
        (artifact / 'old.txt').write_text('old build')
        before = (self.config / 'robot.yaml').read_text()
        result = self.command('migrate', '--robot-id', 'TEST-A')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(repo.exists())
        self.assertEqual((self.config / 'robot.yaml').read_text(), before)
        args = ['migrate', '--robot-id', 'TEST-A', '--apply', '--tasks-stopped']
        # Process state is simulated; never query/stop host services.
        with patch.object(sys, 'argv', ['migration', *args, '--workspace', str(self.coop),
                                       '--task-workspace', str(self.task)]), \
                patch.object(workspace.subprocess, 'run', return_value=type('Result', (), {'stdout': 'inactive'})()), \
                patch.object(workspace.subprocess, 'check_output', side_effect=lambda cmd, **_: str(repo) if cmd[0] == 'git' else ''):
            workspace.main()
        self.assertFalse(repo.exists())
        self.assertTrue((self.coop / 'src/fleet_leader_election/.git').exists())
        self.assertEqual((self.coop / 'src/fleet_leader_election/local.txt').read_text(), 'keep uncommitted work')
        state = self.coop / 'src/fleet_leader_election/runtime/election_state.json'
        self.assertEqual(json.loads(state.read_text())['term'], 17)
        self.assertFalse(artifact.exists())
        backup, = (self.root / 'cooperation-backups').iterdir()
        self.assertEqual(backup.stat().st_mode & 0o777, 0o700)
        with tarfile.open(backup / 'before-migration.tar.gz') as archive:
            self.assertTrue(any(name.endswith('/.git/HEAD') for name in archive.getnames()))
        self.assertEqual(cfg.read_yaml(self.config / 'robot.yaml')['system']['cooperation']['coordination']['election']['data_root'], str(self.coop / 'src/fleet_leader_election/runtime'))
        workspace.check(self.coop, self.task)
        # Also support cleanup after a repository was already moved manually.
        artifact.mkdir(parents=True)
        (artifact / 'old.txt').write_text('leftover install')
        with patch.object(sys, 'argv', ['migration', *args, '--workspace', str(self.coop),
                                       '--task-workspace', str(self.task)]), \
                patch.object(workspace.subprocess, 'run', return_value=type('Result', (), {'stdout': 'inactive'})()), \
                patch.object(workspace.subprocess, 'check_output', return_value=''):
            workspace.main()
        self.assertFalse(artifact.exists())
        self.assertEqual(json.loads(state.read_text())['term'], 17)
        workspace.check(self.coop, self.task)

    def test_conflicts_fail_without_moving_or_rewriting(self):
        repo = self.repo('mission_execution_agent')
        (self.coop / 'src/mission_execution_agent').mkdir(parents=True)
        result = self.command('migrate', '--robot-id', 'TEST-A', '--apply', '--tasks-stopped')
        self.assertNotEqual(result.returncode, 0)
        self.assertTrue(repo.exists())
        self.assertFalse((self.root / 'cooperation-backups').exists())

    def test_external_swarm_state_moves_to_its_package_runtime(self):
        repo = self.repo('swarm_consensus')
        old = self.root / 'old-swarm/TEST-A/consensus'
        old.mkdir(parents=True)
        (old / 'state.json').write_text('{"term": 23}')
        args = ['migration', 'migrate', '--robot-id', 'TEST-A', '--apply', '--tasks-stopped',
                '--swarm-data', str(self.root / 'old-swarm'), '--workspace', str(self.coop),
                '--task-workspace', str(self.task)]
        with patch.object(sys, 'argv', args), \
                patch.object(workspace.subprocess, 'run', return_value=type('Result', (), {'stdout': 'inactive'})()), \
                patch.object(workspace.subprocess, 'check_output', side_effect=lambda cmd, **kw: str(repo) if cmd[0] == 'git' else ''):
            workspace.main()
        self.assertFalse(repo.exists())
        state = self.coop / 'src/swarm_consensus/runtime/state.json'
        self.assertEqual(json.loads(state.read_text())['term'], 23)
        self.assertFalse(old.exists())

    def test_cooperation_builder_fetch_and_update_preserve_local_files(self):
        remote = self.root / 'remote.git'
        subprocess.run(['git', 'init', '-q', '--bare', '--initial-branch=main', str(remote)], check=True)
        seed = self.root / 'seed'
        subprocess.run(['git', 'clone', '-q', str(remote), str(seed)], check=True)
        env = dict(self.env, GIT_AUTHOR_NAME='Test', GIT_AUTHOR_EMAIL='test@example.invalid',
                   GIT_COMMITTER_NAME='Test', GIT_COMMITTER_EMAIL='test@example.invalid')
        def publish(text):
            (seed / 'version.txt').write_text(text)
            for args in (['add', '.'], ['commit', '-qm', text], ['push', '-q', 'origin', 'main']):
                subprocess.run(['git', '-C', str(seed), *args], env=env, check=True)
        publish('first')
        manifest = self.root / 'test.repos'
        manifest.write_text(yaml.safe_dump({'repositories': {'src/module': {'type': 'git', 'url': str(remote)}}}))
        command = ['bash', str(ROOT / 'scripts/build_cooperation.sh'), '--workspace-root', str(self.coop),
                   '--task-workspace', str(self.task), '--repos-file', str(manifest), '--no-branch-check']
        result = subprocess.run([*command, '--fetch-only'], env=env, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        repo = self.coop / 'src/module'
        (repo / 'local.txt').write_text('preserve me')
        publish('second')
        result = subprocess.run([*command, '--update-only'], env=env, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((repo / 'version.txt').read_text(), 'second')
        self.assertEqual((repo / 'local.txt').read_text(), 'preserve me')
        self.assertFalse((self.coop / 'build').exists())

    def test_missing_explicit_source_is_not_silently_migrated(self):
        result = self.command('migrate', '--robot-id', 'TEST-A', '--agent-data', str(self.root / 'missing'))
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('directory does not exist', result.stderr)

    def test_custom_config_root_reaches_installed_condition_before_ros_setup(self):
        services = cfg.read_yaml(self.config / 'services.yaml')
        install = self.root / 'installed'
        services['service'].update(workspace_root=str(self.task), install_dir=str(install))
        services['cooperation']['workspace_root'] = str(self.coop)
        (self.config / 'services.yaml').write_text(yaml.safe_dump(services))
        self.robot['system']['cooperation']['mode'] = 'disabled'
        self.save_robot()
        result = subprocess.run(['bash', str(ROOT / 'scripts/manage_inspection_services.sh'), 'render'],
                                env=self.env, text=True, capture_output=True, check=True)
        output = Path(result.stdout.strip())
        self.addCleanup(shutil.rmtree, output)
        install.mkdir()
        for name in ('bringup_config.py', 'cooperation.py', 'cooperation_workspace.py', 'check_cooperation.py'):
            shutil.copy(ROOT / 'scripts' / name, install / name)
        command = ['python3', str(install / 'check_cooperation.py'), '--workspace', str(self.coop),
                   '--task-workspace', str(self.task)]
        self.assertEqual(subprocess.run(command, env=self.env, capture_output=True).returncode, 1)
        wrapper = (output / 'wrappers/run_inspection_cooperation.sh').read_text()
        self.assertLess(wrapper.index('export INSPECTION_CONFIG_DIR='), wrapper.index('python3 '))
        self.assertLess(wrapper.index('python3 '), wrapper.index('source /opt/ros/'))

    @unittest.skipUnless(importlib.util.find_spec('swarm_mission_coordinator'), 'optional swarm overlay is not installed')
    def test_swarm_launch_uses_exact_runtime_directory(self):
        from swarm_mission_coordinator.launching import device_nodes
        from launch import LaunchContext
        from launch_ros.actions import Node
        from launch_ros.utilities import evaluate_parameters
        root = self.coop / 'src/swarm_mission_coordinator/runtime'
        config = {'robot_id': 'TEST-A', 'data_root': str(root)}
        actions = device_nodes(config)
        nodes = [a for a in actions[0].get_sub_entities() if isinstance(a, Node)]
        self.assertEqual(len(nodes), 5)
        for node in nodes:
            params = evaluate_parameters(LaunchContext(), node._Node__parameters)[0]
            sections = {package: section for package, (stack, section) in workspace.DATA_PACKAGES.items()
                        if stack == 'swarm'}
            if node.node_package in sections:
                self.assertEqual(params['data_root'], str(root / sections[node.node_package]))
            if node.node_package == 'swarm_mission_coordinator':
                self.assertEqual(params['agent_data_root'], str(root / 'agent'))
                self.assertEqual(params['report_root'], str(root.parent / 'reports'))

    def test_swarm_start_checks_unmigrated_state(self):
        from cooperation import check_swarm_runtime

        runtime = self.coop / 'src/swarm_mission_coordinator/runtime'
        old = self.coop / 'src/swarm_consensus/runtime'
        old.mkdir(parents=True)
        (old / 'raft.json').write_text('{}')
        settings = {'robot_id': 'TEST-A', 'swarm': {'data_root': str(runtime)}}
        with self.assertRaisesRegex(ValueError, 'swarm_migrate_runtime'):
            check_swarm_runtime(settings, self.coop)
        runtime.mkdir(parents=True)
        (runtime / 'migration.json').write_text('{}')
        check_swarm_runtime(settings, self.coop)

    def test_service_units_ordering_and_separate_overlay(self):
        result = subprocess.run(['bash', str(ROOT / 'scripts/manage_inspection_services.sh'), 'render'],
                                env=self.env, text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        output = Path(result.stdout.strip())
        self.addCleanup(shutil.rmtree, output)
        unit = (output / 'systemd/inspection-cooperation.service').read_text()
        self.assertIn('BindsTo=inspection-system.service', unit)
        self.assertIn('PartOf=inspection-system.service', unit)
        self.assertIn('ExecCondition=', unit)
        self.assertNotIn('inspection-navigation.service', unit)
        system = (output / 'systemd/inspection-system.service').read_text()
        self.assertIn('inspection-cooperation.service', system)
        content = (output / 'wrappers/run_inspection_cooperation.sh').read_text()
        self.assertIn('cooperation_ws/install/local_setup.bash', content)
        self.assertNotIn('/install/setup.bash', content)
        for wrapper in (output / 'wrappers').glob('*.sh'):
            self.assertEqual(subprocess.run(['bash', '-n', str(wrapper)]).returncode, 0)
        content = (output / 'wrappers/run_inspection_system.sh').read_text()
        self.assertNotIn('cooperation_ws', content)
        self.assertIn('inspection_modules.launch.py', content)

    def test_service_install_uninstall_only_changes_selected_service(self):
        # Real file operations in a private deployment; never call host sudo/systemctl.
        package = self.root / 'bringup'
        for directory in ('scripts', 'systemd'):
            shutil.copytree(ROOT / directory, package / directory)
        unit_dir, install = self.root / 'units', self.root / 'installed'
        unit_dir.mkdir()
        script = package / 'scripts/manage_inspection_services.sh'
        script.write_text(script.read_text().replace(
            'SYSTEMD_DIR="/etc/systemd/system"', 'SYSTEMD_DIR=' + shlex.quote(str(unit_dir))))
        services = cfg.read_yaml(self.config / 'services.yaml')
        services['service']['install_dir'] = str(install)
        (self.config / 'services.yaml').write_text(yaml.safe_dump(services))
        bin_dir = self.root / 'bin'
        bin_dir.mkdir()
        calls = self.root / 'systemctl.jsonl'
        (bin_dir / 'systemctl').write_text(
            '#!/usr/bin/python3\nimport json,sys\n'
            f'with open({str(calls)!r}, "a") as f: f.write(json.dumps(sys.argv[1:])+"\\n")\n')
        (bin_dir / 'sudo').write_text('#!/bin/bash\nif [[ "$1" == -n ]]; then exit 0; fi\nexec "$@"\n')
        for command in bin_dir.iterdir():
            command.chmod(0o755)
        env = dict(self.env, PATH=str(bin_dir) + ':' + os.environ['PATH'])

        def run(action, *targets):
            calls.write_text('')
            result = subprocess.run(['bash', str(script), action, *targets],
                                    env=env, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            selected = targets or ('navigation', 'hardware', 'system', 'cooperation')
            label = 'installed' if action == 'install' else 'removed'
            self.assertIn(f'[{action}] {label}: ' + ' '.join(selected), result.stdout)
            return [json.loads(row) for row in calls.read_text().splitlines()]

        run('install', 'cooperation')
        cooperation_unit = unit_dir / 'inspection-cooperation.service'
        cooperation_wrapper = install / 'run_inspection_cooperation.sh'
        original = cooperation_unit.read_bytes(), cooperation_wrapper.read_bytes()
        invoked = run('install', 'system')
        self.assertEqual(invoked, [['daemon-reload']])
        self.assertEqual(original, (cooperation_unit.read_bytes(), cooperation_wrapper.read_bytes()))
        invoked = run('uninstall', 'system')
        self.assertEqual(invoked, [['disable', '--now', 'inspection-system.service'], ['daemon-reload']])
        self.assertFalse((unit_dir / 'inspection-system.service').exists())
        self.assertEqual(original, (cooperation_unit.read_bytes(), cooperation_wrapper.read_bytes()))
        run('install')
        self.assertEqual(len(list(unit_dir.glob('*.service'))), 4)
        invoked = run('uninstall', 'cooperation')
        self.assertEqual(invoked, [['disable', '--now', 'inspection-cooperation.service'], ['daemon-reload']])
        self.assertTrue((unit_dir / 'inspection-system.service').exists())
        self.assertFalse(cooperation_wrapper.exists())
        run('uninstall')
        self.assertFalse(list(unit_dir.glob('*.service')))
        self.assertFalse(list(install.glob('run_*.sh')))

    def test_system_restart_waits_and_stops_on_errors(self):
        # Exercise the management script without touching host services or sudo.
        bin_dir = self.root / 'bin'
        bin_dir.mkdir()
        calls = self.root / 'calls.jsonl'
        (bin_dir / 'systemctl').write_text(
            '#!/usr/bin/python3\nimport json,os,sys,time\n'
            f'with open({str(calls)!r}, "a") as f: f.write(json.dumps([sys.argv[1:],time.monotonic()])+"\\n")\n'
            'sys.exit(7 if sys.argv[1] == os.environ.get("FAIL_ACTION") else 0)\n')
        (bin_dir / 'sudo').write_text('#!/bin/bash\nif [[ "$1" == -n ]]; then exit 0; fi\nexec "$@"\n')
        for command in bin_dir.iterdir():
            command.chmod(0o755)
        env = dict(self.env, PATH=str(bin_dir) + ':' + os.environ['PATH'])
        for failure, expected in (('', ['stop', 'start']), ('stop', ['stop']),
                                  ('start', ['stop', 'start'])):
            with self.subTest(failure=failure):
                calls.write_text('')
                result = subprocess.run(['bash', str(ROOT / 'scripts/manage_inspection_services.sh'),
                    'restart', 'system'], env=dict(env, FAIL_ACTION=failure),
                    capture_output=True, text=True, timeout=15)
                self.assertEqual(result.returncode, 7 if failure else 0, result.stdout + result.stderr)
                recorded = [json.loads(row) for row in calls.read_text().splitlines()]
                self.assertEqual([args for args, _ in recorded],
                                 [[action, 'inspection-system.service'] for action in expected])
                if len(recorded) == 2:
                    self.assertGreaterEqual(recorded[1][1] - recorded[0][1], 3.0)

    def test_build_selects_nine_packages_in_new_root_with_clean_underlay(self):
        # Actual ROS underlay and minimal installed interfaces; fake colcon only.
        prefix = self.task / 'install/inspection_interfaces'
        index = prefix / 'share/ament_index/resource_index/packages'
        index.mkdir(parents=True)
        (index / 'inspection_interfaces').touch()
        (self.task / 'install/local_setup.bash').write_text('export AMENT_PREFIX_PATH=' + shlex.quote(str(prefix)) + '\n')
        bin_dir = self.root / 'bin'
        bin_dir.mkdir()
        log = self.root / 'build.json'
        builder = bin_dir / 'colcon'
        builder.write_text('#!/usr/bin/python3\nimport os, json, sys\nfrom pathlib import Path\n'
                           f'Path({str(log)!r}).write_text(json.dumps(dict(args=sys.argv[1:], cwd=os.getcwd(), ament=os.environ.get("AMENT_PREFIX_PATH"), py=os.environ.get("PYTHONPATH"))))\n')
        builder.chmod(0o755)
        env = dict(self.env, PATH=str(bin_dir) + ':' + os.environ['PATH'], PYTHONPATH='/stale/cooperation', AMENT_PREFIX_PATH='/stale/task')
        result = subprocess.run(['bash', str(ROOT / 'scripts/build_cooperation.sh'), '--build-only', '--no-branch-check',
                                 '--workspace-root', str(self.coop), '--task-workspace', str(self.task), '-j2'],
                                env=env, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        command = json.loads(log.read_text())
        self.assertEqual(command['cwd'], str(self.coop))
        for package in workspace.PACKAGES:
            self.assertIn(package, command['args'])
        self.assertNotIn('/stale', command['ament'])
        self.assertNotIn('/stale', command['py'])
        self.assertFalse((self.coop / 'src/build').exists())


if __name__ == '__main__':
    unittest.main()
