#!/usr/bin/env python3
"""Exercise both build entry points with isolated workspaces and fake builders."""

import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest


BRINGUP_DIR = Path(__file__).resolve().parents[1]
SCRIPTS = ('build_inspection.sh', 'build_navigation.sh')


class BuildScriptsTest(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory(prefix='bringup-build-test-')
        self.addCleanup(self.temp_dir.cleanup)
        self.root = Path(self.temp_dir.name)
        self.bringup = self.root / 'task_ws/src/inspection_bringup'
        (self.bringup / 'scripts').mkdir(parents=True)
        (self.bringup / 'config').mkdir()
        self.setup_file = self.root / 'setup.bash'
        self.setup_file.write_text('# Isolated test underlay.\n')
        for name in SCRIPTS:
            source = (BRINGUP_DIR / 'scripts' / name).read_text()
            source = source.replace('/opt/ros/jazzy/setup.bash', str(self.setup_file))
            (self.bringup / 'scripts' / name).write_text(source)
        (self.bringup / 'config/inspection_deps.repos').write_text('repositories: {}\n')
        repositories = {}
        for workspace in ('driver_ws', 'algor_ws'):
            relative = f'{workspace}/src/{workspace}_package'
            package_dir = self.root / relative
            package_dir.mkdir(parents=True)
            (package_dir / 'package.xml').write_text(
                f'<package><name>{workspace}_package</name></package>')
            repositories[relative] = {'type': 'git', 'url': 'unused'}
        (self.bringup / 'config/navigation_deps.repos').write_text(
            json.dumps({'repositories': repositories}))
        for name in ('gtsam', 'livox_sdk2'):
            (self.root / 'driver_ws/third_party/src' / name).mkdir(parents=True)

        self.log = self.root / 'commands.jsonl'
        bin_dir = self.root / 'bin'
        bin_dir.mkdir()
        builder = bin_dir / 'builder'
        builder.write_text('''#!/usr/bin/python3
import json
import os
from pathlib import Path
import sys

name = Path(sys.argv[0]).name
with open(os.environ['BUILD_TEST_LOG'], 'a') as stream:
    stream.write(json.dumps({
        'name': name, 'args': sys.argv[1:],
        'makeflags': os.environ.get('MAKEFLAGS'),
        'parallel': os.environ.get('CMAKE_BUILD_PARALLEL_LEVEL'),
    }) + '\\n')
if name == 'colcon':
    install = Path.cwd() / 'install'
    install.mkdir(exist_ok=True)
    (install / 'setup.bash').write_text('# Test workspace overlay.\\n')
elif name == 'ros2':
    print('/test/inspection_interfaces')
''')
        builder.chmod(0o755)
        for name in ('colcon', 'cmake', 'ros2'):
            (bin_dir / name).symlink_to(builder)
        self.env = dict(os.environ)
        self.env.pop('BUILD_JOBS', None)
        self.env.update({
            'USER': 'build_test',
            'PATH': f'{bin_dir}:{self.env.get("PATH", "")}',
            'BUILD_TEST_LOG': str(self.log),
            # Ambient parallel settings must not override the selected jobs.
            'MAKEFLAGS': '-j99',
            'CMAKE_BUILD_PARALLEL_LEVEL': '99',
        })

    def run_build(self, script, jobs_args=(), env_jobs=None):
        self.log.unlink(missing_ok=True)
        args = ['bash', str(self.bringup / 'scripts' / script),
                '--build-only', '--no-branch-check']
        if script == 'build_navigation.sh':
            args += ['--workspace-root', str(self.root),
                     '--interface-underlay', str(self.setup_file)]
        args += list(jobs_args)
        env = dict(self.env)
        if env_jobs is not None:
            env['BUILD_JOBS'] = env_jobs
        result = subprocess.run(args, env=env, capture_output=True, text=True)
        commands = ([json.loads(line) for line in self.log.read_text().splitlines()]
                    if self.log.exists() else [])
        return result, commands

    def assert_build_policy(self, script, args, jobs, env_jobs=None):
        result, commands = self.run_build(script, args, env_jobs)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        colcon_commands = [c for c in commands if c['name'] == 'colcon']
        self.assertEqual(len(colcon_commands), 2 if script == SCRIPTS[1] else 1)
        for command in colcon_commands:
            arguments = command['args']
            self.assertEqual(arguments[arguments.index('--executor') + 1], 'sequential')
            self.assertNotIn('--parallel-workers', arguments)
            self.assertIn('--cmake-force-configure', arguments)
            self.assertIn('-DCMAKE_BUILD_TYPE=Release', arguments)
            self.assertEqual(command['makeflags'], f'-j{jobs}')
            self.assertEqual(command['parallel'], str(jobs))
        if script == SCRIPTS[1]:
            cmake_commands = [c for c in commands if c['name'] == 'cmake']
            self.assertEqual(len(cmake_commands), 6)
            self.assertEqual(
                [c['args'][0] for c in cmake_commands],
                ['-S', '--build', '--install'] * 2)
            for command in cmake_commands:
                if command['args'][0] == '-S':
                    self.assertIn('-DCMAKE_BUILD_TYPE=Release', command['args'])
                elif command['args'][0] == '--build':
                    self.assertIn(f'-j{jobs}', command['args'])
                    self.assertEqual(command['makeflags'], f'-j{jobs}')
                    self.assertEqual(command['parallel'], str(jobs))
            self.assertEqual([c['name'] for c in commands[:8]],
                             ['cmake'] * 6 + ['colcon', 'ros2'])

    def test_defaults_and_job_options(self):
        for script in SCRIPTS:
            for args, jobs in (([], 1), (['-j1'], 1), (['-j4'], 4),
                               (['-j', '4'], 4), (['--jobs', '4'], 4),
                               (['--jobs=4'], 4), (['-j12'], 12)):
                with self.subTest(script=script, args=args):
                    self.assert_build_policy(script, args, jobs)

    def test_environment_default_and_cli_precedence(self):
        for script in SCRIPTS:
            with self.subTest(script=script):
                self.assert_build_policy(script, [], 2, env_jobs='2')
                self.assert_build_policy(script, ['-j4'], 4, env_jobs='2')

    def test_invalid_job_counts_fail_before_building(self):
        for script in SCRIPTS:
            for args in (['-j'], ['--jobs'], ['-j0'], ['-j', '-1'],
                         ['-jfour'], ['--jobs='], ['--jobs', '1.5']):
                with self.subTest(script=script, args=args):
                    result, commands = self.run_build(script, args)
                    self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
                    self.assertIn('positive integer', result.stderr)
                    self.assertEqual(commands, [])
            result, commands = self.run_build(script, env_jobs='0')
            self.assertEqual(result.returncode, 2)
            self.assertEqual(commands, [])


if __name__ == '__main__':
    unittest.main()
