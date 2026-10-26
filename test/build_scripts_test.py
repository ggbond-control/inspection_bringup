#!/usr/bin/env python3
"""Exercise both build entry points with isolated workspaces and fake builders."""

import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest


BRINGUP_DIR = Path(__file__).resolve().parents[1]
SCRIPTS = ('build_inspection.sh', 'build_navigation.sh')


def navigation_support(bringup, setup):
    for name in ('navigation_build.py', 'update_repositories.py'):
        source = (BRINGUP_DIR / 'scripts' / name).read_text()
        source = source.replace('/opt/ros/jazzy/setup.bash', str(setup))
        (bringup / 'scripts' / name).write_text(source)
    directory = bringup / 'config/build'
    directory.mkdir(parents=True, exist_ok=True)
    (directory / 'navigation.yaml').write_text(json.dumps({
        'system_cmake_packages': [], 'required_packages': {},
    }))


def installed_libraries(prefix):
    for relative in ('lib/cmake/GTSAM/GTSAMConfig.cmake', 'include/gtsam/base/Matrix.h',
                     'lib/libgtsam.so', 'lib/libgtsam_unstable.so',
                     'include/livox_lidar_api.h', 'lib/liblivox_lidar_sdk_shared.so'):
        target = prefix / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.touch()



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
        navigation_support(self.bringup, self.setup_file)
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
            (self.root / 'driver_ws/third_party/src' / name / 'CMakeLists.txt').touch()

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
if name == 'cmake' and os.environ.get('BUILD_TEST_FAIL_PROBE') and any('navigation-check-' in a for a in sys.argv):
    print('missing middleware', file=sys.stderr)
    sys.exit(1)
with open(os.environ['BUILD_TEST_LOG'], 'a') as stream:
    stream.write(json.dumps({
        'name': name, 'args': sys.argv[1:],
        'makeflags': os.environ.get('MAKEFLAGS'),
        'parallel': os.environ.get('CMAKE_BUILD_PARALLEL_LEVEL'),
        'rmw': os.environ.get('RMW_IMPLEMENTATION'),
        'ament': os.environ.get('AMENT_PREFIX_PATH', ''),
    }) + '\\n')
if name == 'colcon' and 'list' in sys.argv:
    import xml.etree.ElementTree as ET
    roots = sys.argv[sys.argv.index('--base-paths') + 1:sys.argv.index('--names-only')]
    for root in roots:
        manifest = Path(root) / 'package.xml'
        if manifest.exists():
            print(ET.parse(manifest).getroot().findtext('name'))
    sys.exit(0)
if name == 'colcon':
    install = Path(sys.argv[sys.argv.index('--install-base') + 1]) if '--install-base' in sys.argv else Path.cwd() / 'install'
    install.mkdir(exist_ok=True)
    (install / 'setup.bash').write_text('# Test workspace overlay.\\n')
    (install / 'local_setup.bash').write_text('# Explicit local overlay.\\n')
    if 'inspection_interfaces' in sys.argv:
        setup = install / 'inspection_interfaces/share/inspection_interfaces/local_setup.bash'
        setup.parent.mkdir(parents=True, exist_ok=True)
        setup.write_text('# Generated interface package.\\n')
elif name == 'cmake' and '--install' in sys.argv:
    prefix = Path(os.environ['BUILD_TEST_LOG']).parent / 'driver_ws/third_party/install'
    files = ('lib/cmake/GTSAM/GTSAMConfig.cmake', 'include/gtsam/base/Matrix.h',
             'lib/libgtsam.so', 'lib/libgtsam_unstable.so', 'include/livox_lidar_api.h',
             'lib/liblivox_lidar_sdk_shared.so')
    files = files[:4] if Path(sys.argv[-1]).name == 'gtsam' else files[4:]
    for relative in files:
        target = prefix / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.touch()
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

    def run_build(self, script, jobs_args=(), env_jobs=None, fresh=True):
        self.log.unlink(missing_ok=True)
        if fresh:
            shutil.rmtree(self.root / 'driver_ws/third_party/install', ignore_errors=True)
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
        colcon_commands = [c for c in commands if c['name'] == 'colcon' and 'build' in c['args']]
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
            cmake_commands = [c for c in commands if c['name'] == 'cmake' and not any('navigation-check-' in a for a in c['args'])]
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
            self.assertLess(commands.index(cmake_commands[-1]), commands.index(colcon_commands[0]))

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

    def test_navigation_reuses_installed_gtsam_and_requires_explicit_rebuild(self):
        installed_libraries(self.root / 'driver_ws/third_party/install')
        result, commands = self.run_build(SCRIPTS[1], fresh=False)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn('reuse installed gtsam', result.stdout)
        native = [c for c in commands if c['name'] == 'cmake' and
                  any('/third_party/' in a for a in c['args'])]
        self.assertEqual(native, [])
        result, commands = self.run_build(SCRIPTS[1], ['--rebuild-gtsam'], fresh=False)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        native = [c for c in commands if c['name'] == 'cmake' and
                  any('/third_party/' in a for a in c['args'])]
        self.assertEqual([c['args'][0] for c in native], ['-S', '--build', '--install'])
        self.assertTrue(all('gtsam' in ' '.join(c['args']) for c in native))

    def test_incomplete_gtsam_fails_before_build_without_implicit_repair(self):
        prefix = self.root / 'driver_ws/third_party/install'
        installed_libraries(prefix)
        (prefix / 'lib/libgtsam_unstable.so').unlink()
        result, commands = self.run_build(SCRIPTS[1], fresh=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('--rebuild-gtsam', result.stderr)
        self.assertFalse(any('build' in c['args'] or '--build' in c['args'] for c in commands))

    def test_check_and_missing_repository_never_invoke_builders(self):
        installed_libraries(self.root / 'driver_ws/third_party/install')
        result, commands = self.run_build(SCRIPTS[1], ['--check'], fresh=False)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertFalse(any('build' in c['args'] or '--build' in c['args'] for c in commands))
        shutil.rmtree(self.root / 'algor_ws/src/algor_ws_package')
        result, commands = self.run_build(SCRIPTS[1], fresh=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('missing managed repositories', result.stderr)
        self.assertFalse(any(c['name'] == 'cmake' for c in commands))

    def test_default_entry_builds_interfaces_first_without_path_options(self):
        installed_libraries(self.root / 'driver_ws/third_party/install')
        source = self.root / 'task_ws/src/inspection_interfaces'
        source.mkdir()
        (source / 'package.xml').write_text('<package><name>inspection_interfaces</name></package>')
        env = dict(self.env, NAV_WORKSPACE_ROOT=str(self.root),
                   AMENT_PREFIX_PATH='/stale/overlay', RMW_IMPLEMENTATION='rmw_missing_cpp')
        result = subprocess.run(['bash', str(self.bringup / 'scripts' / SCRIPTS[1]),
                                 '--build-only', '--no-branch-check'],
                                env=env, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        commands = [json.loads(line) for line in self.log.read_text().splitlines()]
        builds = [c for c in commands if c['name'] == 'colcon' and 'build' in c['args']]
        names = [c['args'][c['args'].index('--packages-select') + 1] for c in builds]
        self.assertEqual(names, ['inspection_interfaces', 'driver_ws_package', 'algor_ws_package'])
        self.assertTrue(all(c['rmw'] == 'rmw_fastrtps_cpp' for c in builds))
        self.assertTrue(all('/stale/overlay' not in c['ament'] for c in builds))

    def test_failed_environment_probe_stops_before_native_or_ros_builds(self):
        self.env['BUILD_TEST_FAIL_PROBE'] = '1'
        result, commands = self.run_build(SCRIPTS[1])
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('missing middleware', result.stderr)
        self.assertFalse(any('build' in c['args'] or '--build' in c['args'] for c in commands))

    @unittest.skipUnless(Path('/usr/bin/colcon').exists(), 'requires real colcon discovery')
    def test_real_colcon_discovery_respects_ignored_vendor_packages(self):
        for workspace in ('driver_ws', 'algor_ws'):
            package = self.root / f'{workspace}/src/{workspace}_package'
            (package / 'package.xml').write_text(
                f'<package format="3"><name>{workspace}_package</name><version>0.1.0</version>'
                '<description>Discovery fixture</description>'
                '<maintainer email="test@example.com">Test</maintainer><license>MIT</license>'
                '<buildtool_depend>ament_cmake</buildtool_depend>'
                '<export><build_type>ament_cmake</build_type></export></package>')
        ignored = self.root / 'driver_ws/src/driver_ws_package/vendor'
        ignored.mkdir()
        (ignored / 'COLCON_IGNORE').touch()
        (ignored / 'package.xml').write_text('<package><name>ignored_vendor</name></package>')
        builder = self.root / 'bin/builder'
        source = builder.read_text().replace(
            "if name == 'colcon' and 'list' in sys.argv:",
            "if name == 'colcon' and 'list' in sys.argv:\n"
            "    os.execv('/usr/bin/colcon', ['/usr/bin/colcon', *sys.argv[1:]])")
        builder.write_text(source)
        result, commands = self.run_build(SCRIPTS[1])
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        builds = [c for c in commands if c['name'] == 'colcon' and 'build' in c['args']]
        self.assertEqual(len(builds), 2)
        self.assertTrue(all('ignored_vendor' not in c['args'] for c in builds))


class RepositoryUpdatesTest(unittest.TestCase):
    """Real Git pulls against local remotes; never touch deployed repositories."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='bringup pull test ')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.counter = 0
        self.env = dict(os.environ, GIT_CONFIG_NOSYSTEM='1', GIT_CONFIG_GLOBAL='/dev/null',
                        GIT_AUTHOR_NAME='Test', GIT_AUTHOR_EMAIL='test@example.invalid',
                        GIT_COMMITTER_NAME='Test', GIT_COMMITTER_EMAIL='test@example.invalid',
                        GIT_TERMINAL_PROMPT='0', GIT_MERGE_AUTOEDIT='no')

    def git(self, repo, *args):
        result = subprocess.run(['git', '-C', str(repo), *args], env=self.env,
                                capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return result.stdout.strip()

    def fixture(self, script):
        self.counter += 1
        root = self.root / str(self.counter)
        bringup = root / 'task_ws/src/inspection_bringup'
        (bringup / 'scripts').mkdir(parents=True)
        setup = root / 'setup.bash'
        setup.write_text('# local test\n')
        for name in (*SCRIPTS, 'update_repositories.py'):
            source = (BRINGUP_DIR / 'scripts' / name).read_text()
            source = source.replace('/opt/ros/jazzy/setup.bash', f'"{setup}"')
            (bringup / 'scripts' / name).write_text(source)
        navigation_support(bringup, setup)
        remote = root / 'remote.git'
        remote.mkdir()
        self.git(remote, 'init', '--bare', '--initial-branch=main')
        seed = root / 'seed'
        self.git(root, 'clone', str(remote), str(seed))
        (seed / 'upstream.txt').write_text('initial\n')
        (seed / 'local.txt').write_text('initial\n')
        self.git(seed, 'add', '.')
        self.git(seed, 'commit', '-m', 'initial')
        self.git(seed, 'push', 'origin', 'main')
        if script == 'build_inspection.sh':
            relative = 'src/module'
            repo = root / 'task_ws' / relative
            extra = []
        else:
            relative = 'driver_ws/src/module'
            repo = root / 'custom driver/src/module'
            repo.parent.mkdir(parents=True)
            extra = ['--workspace-root', str(root / 'navigation root'),
                     '--driver-ws', str(root / 'custom driver'),
                     '--algor-ws', str(root / 'custom algor'),
                     '--interface-underlay', str(setup)]
        self.git(root, 'clone', 'file://' + str(remote), str(repo))
        manifest = root / 'deps.repos'
        manifest.write_text(json.dumps({'repositories': {
            relative: {'type': 'git', 'url': 'file://' + str(remote), 'version': 'main'}}}))
        command = ['bash', str(bringup / 'scripts' / script), '--repos-file', str(manifest), *extra]
        return root, seed, repo, manifest, command

    def upstream_commit(self, seed, text='remote update\n'):
        (seed / 'upstream.txt').write_text(text)
        self.git(seed, 'commit', '-am', 'remote update')
        self.git(seed, 'push', 'origin', 'main')

    def run_update(self, command, *options):
        return subprocess.run([*command, *(options or ('--update-only',))],
                              env=self.env, capture_output=True, text=True, timeout=20)

    def test_pull_accepts_unstaged_staged_and_untracked_changes(self):
        for script in SCRIPTS:
            for staged in (False, True):
                with self.subTest(script=script, staged=staged):
                    _, seed, repo, _, command = self.fixture(script)
                    self.upstream_commit(seed)
                    (repo / 'local.txt').write_text('local edits\n')
                    (repo / 'untracked.txt').write_text('keep\n')
                    if staged:
                        self.git(repo, 'add', 'local.txt')
                    result = self.run_update(command)
                    self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                    self.assertEqual(self.git(repo, 'rev-parse', 'HEAD'), self.git(seed, 'rev-parse', 'HEAD'))
                    self.assertEqual((repo / 'local.txt').read_text(), 'local edits\n')
                    self.assertTrue((repo / 'untracked.txt').is_file())
                    status = self.git(repo, 'diff', '--cached', '--name-only')
                    self.assertEqual(status, 'local.txt' if staged else '')

    def test_local_ahead_is_allowed(self):
        for script in SCRIPTS:
            with self.subTest(script=script):
                _, _, repo, _, command = self.fixture(script)
                (repo / 'local.txt').write_text('committed locally\n')
                self.git(repo, 'commit', '-am', 'local commit')
                head = self.git(repo, 'rev-parse', 'HEAD')
                result = self.run_update(command)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertEqual(self.git(repo, 'rev-parse', 'HEAD'), head)

    def test_diverged_branch_respects_merge_or_rebase_configuration(self):
        for script in SCRIPTS:
            for strategy in ('false', 'true'):
                with self.subTest(script=script, rebase=strategy):
                    _, seed, repo, _, command = self.fixture(script)
                    (repo / 'local.txt').write_text('local commit\n')
                    self.git(repo, 'commit', '-am', 'local commit')
                    self.git(repo, 'config', 'pull.rebase', strategy)
                    self.upstream_commit(seed)
                    result = self.run_update(command)
                    self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                    self.assertEqual((repo / 'local.txt').read_text(), 'local commit\n')
                    self.assertEqual((repo / 'upstream.txt').read_text(), 'remote update\n')
                    parents = self.git(repo, 'rev-list', '--parents', '-n', '1', 'HEAD').split()
                    self.assertEqual(len(parents), 3 if strategy == 'false' else 2)

    def test_pull_failure_stops_before_build_and_preserves_local_edits(self):
        for script in SCRIPTS:
            with self.subTest(script=script):
                root, seed, repo, _, command = self.fixture(script)
                self.upstream_commit(seed)
                (repo / 'upstream.txt').write_text('overlapping local edit\n')
                head = self.git(repo, 'rev-parse', 'HEAD')
                # Builders are sentinels: even update-and-build must stop at pull.
                binary = root / 'bin'
                binary.mkdir()
                for name in ('cmake', 'colcon'):
                    path = binary / name
                    path.write_text('#!/bin/sh\ntouch "' + str(root / 'built') + '"\n')
                    path.chmod(0o755)
                env_path = self.env['PATH']
                self.env['PATH'] = str(binary) + os.pathsep + env_path
                try:
                    result = self.run_update(command, '--update-current-branch')
                finally:
                    self.env['PATH'] = env_path
                self.assertNotEqual(result.returncode, 0)
                self.assertIn('git pull failed', result.stderr)
                self.assertFalse((root / 'built').exists())
                self.assertEqual(self.git(repo, 'rev-parse', 'HEAD'), head)
                self.assertEqual((repo / 'upstream.txt').read_text(), 'overlapping local edit\n')

    def test_pinned_detached_tag_is_kept(self):
        for script in SCRIPTS:
            with self.subTest(script=script):
                _, seed, repo, manifest, command = self.fixture(script)
                self.git(repo, 'tag', '4.2.0')
                self.git(repo, 'checkout', '--detach', '4.2.0')
                data = json.loads(manifest.read_text())
                next(iter(data['repositories'].values()))['version'] = '4.2.0'
                manifest.write_text(json.dumps(data))
                head = self.git(repo, 'rev-parse', 'HEAD')
                self.upstream_commit(seed)
                result = self.run_update(command)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertIn('keep pinned tag 4.2.0', result.stdout)
                self.assertEqual(self.git(repo, 'rev-parse', 'HEAD'), head)

    def test_git_errors_for_detached_head_and_missing_upstream_are_reported(self):
        for script in SCRIPTS:
            for mode in ('detached', 'no-upstream'):
                with self.subTest(script=script, mode=mode):
                    _, _, repo, _, command = self.fixture(script)
                    if mode == 'detached':
                        self.git(repo, 'checkout', '--detach')
                    else:
                        self.git(repo, 'branch', '--unset-upstream')
                    result = self.run_update(command)
                    self.assertNotEqual(result.returncode, 0)
                    self.assertIn('git pull failed', result.stderr)
                    self.assertIn('not currently on a branch' if mode == 'detached'
                                  else 'no tracking information', result.stderr)

    def test_pull_ff_only_configuration_is_respected(self):
        for script in SCRIPTS:
            with self.subTest(script=script):
                _, seed, repo, _, command = self.fixture(script)
                (repo / 'local.txt').write_text('local commit\n')
                self.git(repo, 'commit', '-am', 'local commit')
                self.git(repo, 'config', 'pull.ff', 'only')
                self.upstream_commit(seed)
                result = self.run_update(command)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn('git pull failed', result.stderr)
                self.assertIn('fast-forward', result.stderr)

    def test_current_branch_is_pulled_despite_manifest_branch_mismatch(self):
        for script in SCRIPTS:
            with self.subTest(script=script):
                _, seed, repo, manifest, command = self.fixture(script)
                data = json.loads(manifest.read_text())
                next(iter(data['repositories'].values()))['version'] = 'jazzy'
                manifest.write_text(json.dumps(data))
                self.upstream_commit(seed)
                result = self.run_update(command)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertEqual(self.git(repo, 'branch', '--show-current'), 'main')
                self.assertEqual(self.git(repo, 'rev-parse', 'HEAD'), self.git(seed, 'rev-parse', 'HEAD'))

    def test_default_fetch_does_not_pull_existing_repository(self):
        for script in SCRIPTS:
            with self.subTest(script=script):
                _, seed, repo, _, command = self.fixture(script)
                head = self.git(repo, 'rev-parse', 'HEAD')
                self.upstream_commit(seed)
                result = self.run_update(command, '--fetch-only')
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertEqual(self.git(repo, 'rev-parse', 'HEAD'), head)

    def test_missing_repositories_are_cloned_then_update_only_exits(self):
        for script in SCRIPTS:
            with self.subTest(script=script):
                _, _, repo, _, command = self.fixture(script)
                shutil.rmtree(repo)
                result = self.run_update(command)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertTrue((repo / '.git').is_dir())
                self.assertIn('update-only complete', result.stdout)

    def test_update_and_build_runs_builder_after_pull_succeeds(self):
        for script in SCRIPTS:
            with self.subTest(script=script):
                root, seed, repo, _, command = self.fixture(script)
                (repo / 'package.xml').write_text('<package><name>module</name></package>')
                self.upstream_commit(seed)
                binary = root / 'bin'
                binary.mkdir()
                builder = binary / 'colcon'
                builder.write_text(
                    '#!/usr/bin/python3\nfrom pathlib import Path\n'
                    f'assert Path({str(repo / "upstream.txt")!r}).read_text() == "remote update\\n"\n'
                    f'Path({str(root / "built")!r}).touch()\n')
                builder.chmod(0o755)
                # Navigation only builds its driver workspace in this fixture.
                options = ['--update-current-branch']
                if script == 'build_navigation.sh':
                    options += ['--skip-third-party', '--skip-algor-build']
                    installed_libraries(root / 'custom driver/third_party/install')
                    (binary / 'cmake').write_text('#!/bin/sh\nexit 0\n')
                    (binary / 'cmake').chmod(0o755)
                    builder.write_text(builder.read_text().replace(
                        'from pathlib import Path',
                        'from pathlib import Path\nimport sys\n'
                        'if "list" in sys.argv:\n print("module"); sys.exit(0)'))
                    (binary / 'ros2').write_text('#!/bin/sh\necho /test/interfaces\n')
                    (binary / 'ros2').chmod(0o755)
                old_path = self.env['PATH']
                self.env['PATH'] = str(binary) + os.pathsep + old_path
                try:
                    result = self.run_update(command, *options)
                finally:
                    self.env['PATH'] = old_path
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertTrue((root / 'built').is_file())

    def test_navigation_updates_shallow_clone_and_both_workspace_paths(self):
        root, seed, repo, manifest, command = self.fixture('build_navigation.sh')
        remote = self.git(repo, 'remote', 'get-url', 'origin')
        shutil.rmtree(repo)
        self.git(root, 'clone', '--depth', '1', remote, str(repo))
        algor_repo = root / 'custom algor/src/algorithm'
        algor_repo.parent.mkdir(parents=True)
        self.git(root, 'clone', remote, str(algor_repo))
        data = json.loads(manifest.read_text())
        data['repositories']['algor_ws/src/algorithm'] = {'type': 'git', 'url': remote}
        manifest.write_text(json.dumps(data))
        self.upstream_commit(seed)
        result = self.run_update(command)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.git(repo, 'rev-parse', '--is-shallow-repository'), 'true')
        for path in (repo, algor_repo):
            self.assertEqual(self.git(path, 'rev-parse', 'HEAD'), self.git(seed, 'rev-parse', 'HEAD'))

    def change_spec(self, manifest, **values):
        data = json.loads(manifest.read_text())
        next(iter(data['repositories'].values())).update(values)
        manifest.write_text(json.dumps(data))

    def publish_branch(self, seed, name):
        self.git(seed, 'push', 'origin', f'HEAD:refs/heads/{name}')

    def test_checkout_creates_remote_tracking_branch_and_is_repeatable(self):
        for script in SCRIPTS:
            with self.subTest(script=script):
                _, seed, repo, _, command = self.fixture(script)
                self.publish_branch(seed, 'dev')
                for _ in range(2):
                    result = self.run_update(command, '--checkout', 'dev', '--update-only')
                    self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                    self.assertEqual(self.git(repo, 'branch', '--show-current'), 'dev')
                    self.assertEqual(self.git(repo, 'rev-parse', '--abbrev-ref', '@{upstream}'), 'origin/dev')
                    self.assertNotIn('expected=main', result.stdout)

    def test_checkout_rejects_missing_remote_branch_despite_stale_local_refs(self):
        for script in SCRIPTS:
            with self.subTest(script=script):
                _, seed, repo, _, command = self.fixture(script)
                self.publish_branch(seed, 'dev')
                self.git(repo, 'fetch', 'origin')
                self.git(repo, 'branch', 'dev', 'origin/dev')
                self.git(seed, 'push', 'origin', '--delete', 'dev')
                result = self.run_update(command, '--checkout', 'dev', '--update-current-branch')
                self.assertNotEqual(result.returncode, 0)
                self.assertIn('origin/dev does not exist', result.stderr)
                self.assertEqual(self.git(repo, 'branch', '--show-current'), 'main')

    def test_checkout_missing_dev_never_creates_branch(self):
        for script in SCRIPTS:
            with self.subTest(script=script):
                _, _, repo, _, command = self.fixture(script)
                result = self.run_update(command, '--checkout', 'dev', '--update-only')
                self.assertNotEqual(result.returncode, 0)
                self.assertNotIn('dev', self.git(repo, 'branch', '--format=%(refname:short)').splitlines())

    def test_checkout_main_resolves_all_names_and_rejects_ambiguity(self):
        for script in SCRIPTS:
            for target in ('main', 'jazzy', 'master'):
                with self.subTest(script=script, target=target):
                    _, seed, repo, manifest, command = self.fixture(script)
                    self.change_spec(manifest, version=None)
                    self.git(repo, 'switch', '-c', 'local-work')
                    if target != 'main':
                        self.publish_branch(seed, target)
                        # Allow deletion of the bare remote's initial default.
                        self.git(seed.parent / 'remote.git', 'config', 'receive.denyDeleteCurrent', 'ignore')
                        self.git(seed, 'push', 'origin', '--delete', 'main')
                    result = self.run_update(command, '--checkout', 'main', '--update-only')
                    self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                    self.assertEqual(self.git(repo, 'branch', '--show-current'), target)
            _, seed, repo, manifest, command = self.fixture(script)
            self.publish_branch(seed, 'jazzy')
            self.change_spec(manifest, version=None)
            result = self.run_update(command, '--checkout', 'main', '--update-only')
            self.assertNotEqual(result.returncode, 0)
            self.assertIn('ambiguous main branches', result.stderr)
            self.change_spec(manifest, version='jazzy')
            result = self.run_update(command, '--checkout', 'main', '--update-only')
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertEqual(self.git(repo, 'branch', '--show-current'), 'jazzy')
            self.change_spec(manifest, main_branch='main')
            result = self.run_update(command, '--checkout', 'main', '--update-only')
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertEqual(self.git(repo, 'branch', '--show-current'), 'main')
            self.change_spec(manifest, main_branch='master')
            result = self.run_update(command, '--checkout', 'main', '--update-only')
            self.assertNotEqual(result.returncode, 0)
            self.assertIn('origin/master does not exist', result.stderr)

    def test_checkout_rejects_unfinished_operations(self):
        for script in SCRIPTS:
            for state in ('merge', 'rebase'):
                with self.subTest(script=script, state=state):
                    _, seed, repo, _, command = self.fixture(script)
                    self.publish_branch(seed, 'dev')
                    if state == 'merge':
                        (repo / '.git/MERGE_HEAD').write_text(self.git(repo, 'rev-parse', 'HEAD') + '\n')
                    else:
                        (repo / '.git/rebase-merge').mkdir()
                    before = self.git(repo, 'status', '--porcelain')
                    result = self.run_update(command, '--checkout', 'dev', '--update-only')
                    self.assertNotEqual(result.returncode, 0)
                    self.assertEqual(self.git(repo, 'branch', '--show-current'), 'main')
                    self.assertEqual(self.git(repo, 'status', '--porcelain'), before)

    def test_checkout_preserves_compatible_edits_and_staging(self):
        for script in SCRIPTS:
            for phase in ('switch', 'fast-forward', 'already-on-target'):
                for state in ('unstaged', 'staged', 'untracked'):
                    with self.subTest(script=script, phase=phase, state=state):
                        _, seed, repo, _, command = self.fixture(script)
                        if phase == 'fast-forward':
                            self.git(repo, 'branch', 'dev')
                        elif phase == 'already-on-target':
                            self.git(repo, 'switch', '-c', 'dev')
                        self.upstream_commit(seed)
                        self.publish_branch(seed, 'dev')
                        local_file = repo / ('new.txt' if state == 'untracked' else 'local.txt')
                        local_file.write_text('keep me\n')
                        if state == 'staged':
                            self.git(repo, 'add', 'local.txt')
                            # Preserve both staged and unstaged edits to one file.
                            local_file.write_text('keep me and my unstaged edits\n')
                        status = self.git(repo, 'status', '--porcelain')
                        staged = self.git(repo, 'diff', '--cached')
                        unstaged = self.git(repo, 'diff')
                        content = local_file.read_bytes()
                        result = self.run_update(command, '--checkout', 'dev', '--update-only')
                        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                        self.assertEqual(self.git(repo, 'branch', '--show-current'), 'dev')
                        self.assertEqual(self.git(repo, 'rev-parse', 'HEAD'), self.git(seed, 'rev-parse', 'HEAD'))
                        self.assertEqual(self.git(repo, 'status', '--porcelain'), status)
                        self.assertEqual(self.git(repo, 'diff', '--cached'), staged)
                        self.assertEqual(self.git(repo, 'diff'), unstaged)
                        self.assertEqual(local_file.read_bytes(), content)
                        # Repeating checkout on a dirty target branch also works.
                        result = self.run_update(command, '--checkout', 'dev', '--update-only')
                        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                        self.assertEqual(local_file.read_bytes(), content)
                        self.assertEqual(self.git(repo, 'status', '--porcelain'), status)

    def test_checkout_overwrite_conflicts_preserve_edits_and_stop_build(self):
        for script in SCRIPTS:
            for phase in ('switch', 'fast-forward'):
                for state in ('unstaged', 'staged', 'untracked'):
                    with self.subTest(script=script, phase=phase, state=state):
                        root, seed, repo, _, command = self.fixture(script)
                        head = self.git(repo, 'rev-parse', 'HEAD')
                        if phase == 'fast-forward':
                            self.git(repo, 'branch', 'dev')
                        relative = 'new.txt' if state == 'untracked' else 'local.txt'
                        (seed / relative).write_text('remote content\n')
                        self.git(seed, 'add', relative)
                        self.git(seed, 'commit', '-m', 'remote conflicting file')
                        self.publish_branch(seed, 'dev')
                        (repo / relative).write_text('local content\n')
                        if state == 'staged':
                            self.git(repo, 'add', relative)
                        status = self.git(repo, 'status', '--porcelain')
                        staged = self.git(repo, 'diff', '--cached')
                        self.git(repo, 'config', 'merge.autoStash', 'true')
                        binary = root / 'bin'
                        binary.mkdir()
                        for name in ('colcon', 'cmake'):
                            builder = binary / name
                            builder.write_text('#!/bin/sh\ntouch "' + str(root / 'built') + '"\n')
                            builder.chmod(0o755)
                        env = dict(self.env, PATH=str(binary) + os.pathsep + self.env['PATH'])
                        result = subprocess.run([*command, '--checkout', 'dev', '--update-current-branch'],
                                                env=env, capture_output=True, text=True, timeout=20)
                        self.assertNotEqual(result.returncode, 0)
                        self.assertIn('would be overwritten', result.stderr)
                        self.assertIn('Checkout stopped; build will not start', result.stderr)
                        self.assertFalse((root / 'built').exists())
                        self.assertEqual(self.git(repo, 'rev-parse', 'HEAD'), head)
                        self.assertEqual(self.git(repo, 'branch', '--show-current'),
                                         'main' if phase == 'switch' else 'dev')
                        self.assertEqual((repo / relative).read_text(), 'local content\n')
                        self.assertEqual(self.git(repo, 'status', '--porcelain'), status)
                        self.assertEqual(self.git(repo, 'diff', '--cached'), staged)
                        self.assertEqual(self.git(repo, 'stash', 'list'), '')

    def test_checkout_preflights_all_repositories_before_switching(self):
        for script in SCRIPTS:
            with self.subTest(script=script):
                root, seed, repo, manifest, command = self.fixture(script)
                self.publish_branch(seed, 'dev')
                bad_remote = root / 'bad.git'
                self.git(root, 'init', '--bare', str(bad_remote))
                self.git(seed, 'push', str(bad_remote), 'HEAD:refs/heads/main')
                second = repo.parent / 'bad'
                self.git(root, 'clone', '--branch', 'main', str(bad_remote), str(second))
                data = json.loads(manifest.read_text())
                relative = next(iter(data['repositories'])).rsplit('/', 1)[0] + '/bad'
                data['repositories'][relative] = {'type': 'git', 'url': str(bad_remote)}
                manifest.write_text(json.dumps(data))
                result = self.run_update(command, '--checkout', 'dev', '--update-only')
                self.assertNotEqual(result.returncode, 0)
                self.assertIn('checkout preflight failed', result.stderr)
                for path in (repo, second):
                    self.assertEqual(self.git(path, 'branch', '--show-current'), 'main')
                    self.assertNotIn('dev', self.git(path, 'branch', '--format=%(refname:short)').splitlines())

    def test_checkout_only_preserves_local_branch_then_update_fast_forwards(self):
        for script in SCRIPTS:
            with self.subTest(script=script):
                root, seed, repo, _, command = self.fixture(script)
                head = self.git(repo, 'rev-parse', 'HEAD')
                self.git(repo, 'branch', 'dev')
                self.upstream_commit(seed)
                self.publish_branch(seed, 'dev')
                (repo / 'local.txt').write_text('staged local edits\n')
                self.git(repo, 'add', 'local.txt')
                (repo / 'local.txt').write_text('unstaged local edits\n')
                (repo / 'untracked.txt').write_text('keep this file\n')
                status = self.git(repo, 'status', '--porcelain')
                staged = self.git(repo, 'diff', '--cached')
                # Real build entry points must forward checkout without update.
                binary = root / 'bin'
                binary.mkdir()
                for name in ('colcon', 'cmake'):
                    builder = binary / name
                    builder.write_text('#!/bin/sh\nexit 17\n')
                    builder.chmod(0o755)
                old_path = self.env['PATH']
                self.env['PATH'] = str(binary) + os.pathsep + old_path
                try:
                    result = self.run_update(command, '--checkout', 'dev')
                finally:
                    self.env['PATH'] = old_path
                # The deliberately incomplete build may fail after checkout.
                self.assertIn('managed repository checkout complete', result.stdout)
                self.assertEqual(self.git(repo, 'branch', '--show-current'), 'dev')
                self.assertEqual(self.git(repo, 'rev-parse', 'HEAD'), head)
                self.assertEqual(self.git(repo, 'status', '--porcelain'), status)
                self.assertEqual(self.git(repo, 'diff', '--cached'), staged)
                result = self.run_update(command, '--checkout', 'dev', '--update-only')
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertEqual(self.git(repo, 'rev-parse', 'HEAD'), self.git(seed, 'rev-parse', 'HEAD'))
                self.assertEqual(self.git(repo, 'status', '--porcelain'), status)
                self.assertEqual(self.git(repo, 'diff', '--cached'), staged)
                self.assertEqual((repo / 'local.txt').read_text(), 'unstaged local edits\n')
                self.assertEqual((repo / 'untracked.txt').read_text(), 'keep this file\n')

    def test_checkout_update_preserves_ahead_and_rejects_divergence_even_with_rebase(self):
        for script in SCRIPTS:
            with self.subTest(script=script):
                _, seed, repo, _, command = self.fixture(script)
                self.publish_branch(seed, 'dev')
                self.git(repo, 'switch', '-c', 'dev')
                (repo / 'local.txt').write_text('local commit\n')
                self.git(repo, 'commit', '-am', 'local commit')
                head = self.git(repo, 'rev-parse', 'HEAD')
                result = self.run_update(command, '--checkout', 'dev', '--update-only')
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertIn('ahead=1 behind=0', result.stdout)
                self.git(repo, 'switch', 'main')
                self.git(repo, 'config', 'pull.rebase', 'true')
                self.upstream_commit(seed)
                self.publish_branch(seed, 'dev')
                result = self.run_update(command, '--checkout', 'dev', '--update-only')
                self.assertNotEqual(result.returncode, 0)
                self.assertIn('fast-forward required', result.stderr)
                self.assertEqual(self.git(repo, 'branch', '--show-current'), 'main')
                self.assertEqual(self.git(repo, 'rev-parse', 'dev'), head)

    def test_checkout_handles_shallow_clone_and_future_plain_pull(self):
        for script in SCRIPTS:
            with self.subTest(script=script):
                root, seed, repo, _, command = self.fixture(script)
                self.upstream_commit(seed)
                self.publish_branch(seed, 'dev')
                url = self.git(repo, 'remote', 'get-url', 'origin')
                shutil.rmtree(repo)
                self.git(root, 'clone', '--depth', '1', '--branch', 'main', url, str(repo))
                result = self.run_update(command, '--checkout', 'dev', '--update-only')
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertEqual(self.git(repo, 'branch', '--show-current'), 'dev')
                self.upstream_commit(seed, 'later update\n')
                self.publish_branch(seed, 'dev')
                result = self.run_update(command)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertEqual(self.git(repo, 'rev-parse', 'HEAD'), self.git(seed, 'rev-parse', 'HEAD'))

    def test_checkout_keeps_annotated_pinned_tags_and_rejects_drift(self):
        for script in SCRIPTS:
            for mode in ('dev', 'main'):
                with self.subTest(script=script, mode=mode):
                    _, seed, repo, manifest, command = self.fixture(script)
                    self.git(seed, 'tag', '-a', '4.2.0', '-m', 'fixed dependency')
                    self.git(seed, 'push', 'origin', '4.2.0')
                    self.git(repo, 'fetch', '--tags', 'origin')
                    self.git(repo, 'checkout', '--detach', '4.2.0')
                    self.change_spec(manifest, version='4.2.0')
                    head = self.git(repo, 'rev-parse', 'HEAD')
                    result = self.run_update(command, '--checkout', mode, '--update-only')
                    self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                    self.assertIn('keep pinned tag 4.2.0', result.stdout)
                    self.assertEqual(self.git(repo, 'rev-parse', 'HEAD'), head)
                    self.assertEqual(self.git(repo, 'branch', '--show-current'), '')
                    self.publish_branch(seed, 'dev')
                    self.git(seed, 'push', 'origin', '--delete', '4.2.0')
                    result = self.run_update(command, '--checkout', mode, '--update-only')
                    self.assertNotEqual(result.returncode, 0)
                    self.assertIn('manifest-pinned tag 4.2.0 is missing', result.stderr)
                    self.assertEqual(self.git(repo, 'branch', '--show-current'), '')
                    self.git(seed, 'push', 'origin', '4.2.0')
                    self.git(repo, 'switch', 'main')
                    result = self.run_update(command, '--checkout', mode, '--update-only')
                    self.assertNotEqual(result.returncode, 0)
                    self.assertIn('expected detached pinned tag', result.stderr)

    def test_checkout_detects_target_in_other_worktree_and_supports_worktree_git_file(self):
        for script in SCRIPTS:
            with self.subTest(script=script):
                root, seed, repo, _, command = self.fixture(script)
                self.publish_branch(seed, 'dev')
                self.git(repo, 'fetch', 'origin')
                other = root / 'other worktree'
                self.git(repo, 'worktree', 'add', '-b', 'dev', str(other), 'origin/dev')
                result = self.run_update(command, '--checkout', 'dev', '--update-only')
                self.assertNotEqual(result.returncode, 0)
                self.assertIn('another worktree', result.stderr)
                self.git(repo, 'worktree', 'remove', str(other))
                # Move the managed branch into a linked worktree at the same path.
                main_repo = root / 'main clone'
                repo.rename(main_repo)
                self.git(main_repo, 'worktree', 'add', '-b', 'temporary', str(repo), 'HEAD')
                result = self.run_update(command, '--checkout', 'dev', '--update-only')
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertTrue((repo / '.git').is_file())
                self.assertEqual(self.git(repo, 'branch', '--show-current'), 'dev')

    def test_checkout_selected_remote_and_missing_clone_resolution(self):
        for script in SCRIPTS:
            with self.subTest(script=script):
                _, seed, repo, manifest, command = self.fixture(script)
                self.publish_branch(seed, 'dev')
                self.change_spec(manifest, version='nonexistent-old-branch')
                shutil.rmtree(repo)
                result = self.run_update(command, '--checkout', 'dev', '--remote', 'gitlab', '--update-only')
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertEqual(self.git(repo, 'rev-parse', '--abbrev-ref', '@{upstream}'), 'gitlab/dev')
                # An existing repository must never silently fall back to origin.
                self.git(repo, 'remote', 'rename', 'gitlab', 'origin')
                result = self.run_update(command, '--checkout', 'dev', '--remote', 'gitlab', '--update-only')
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("remote 'gitlab' does not exist", result.stderr)

    def test_checkout_missing_clone_does_not_create_dev_when_remote_lacks_it(self):
        for script in SCRIPTS:
            with self.subTest(script=script):
                _, _, repo, _, command = self.fixture(script)
                shutil.rmtree(repo)
                result = self.run_update(command, '--checkout', 'dev', '--update-only')
                self.assertNotEqual(result.returncode, 0)
                self.assertFalse(repo.exists())

    def test_checkout_argument_conflicts_fail_before_repository_changes(self):
        for script in SCRIPTS:
            _, _, repo, _, command = self.fixture(script)
            conflicts = [('--checkout', 'invalid'), ('--checkout', 'dev', '--fetch-only'),
                         ('--checkout', 'dev', '--build-only'), ('--remote', 'gitlab'),
                         ('--checkout', 'dev', '--remote='), ('--checkout=',)]
            if script == 'build_navigation.sh':
                conflicts += [('--checkout', 'dev', '--plan'), ('--checkout', 'dev', '--check')]
            for options in conflicts:
                with self.subTest(script=script, options=options):
                    result = self.run_update(command, *options)
                    self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
                    self.assertEqual(self.git(repo, 'branch', '--show-current'), 'main')

    def test_checkout_preserves_ignored_machine_files_on_switch_or_fast_forward(self):
        for script in SCRIPTS:
            for phase in ('switch', 'fast-forward'):
                with self.subTest(script=script, phase=phase):
                    root, seed, repo, _, command = self.fixture(script)
                    (seed / '.gitignore').write_text('machine.yaml\n')
                    self.git(seed, 'add', '.gitignore')
                    self.git(seed, 'commit', '-m', 'ignore machine configuration')
                    self.git(seed, 'push', 'origin', 'main')
                    self.git(repo, 'pull', '--ff-only')
                    head = self.git(repo, 'rev-parse', 'HEAD')
                    if phase == 'fast-forward':
                        self.git(repo, 'branch', 'dev')
                    (repo / 'machine.yaml').write_text('local configuration\n')
                    (seed / 'machine.yaml').write_text('tracked remote configuration\n')
                    self.git(seed, 'add', '--force', 'machine.yaml')
                    self.git(seed, 'commit', '-m', 'track conflicting configuration')
                    self.publish_branch(seed, 'dev')
                    binary = root / 'bin'
                    binary.mkdir()
                    for name in ('cmake', 'colcon'):
                        builder = binary / name
                        builder.write_text('#!/bin/sh\ntouch "' + str(root / 'built') + '"\n')
                        builder.chmod(0o755)
                    old_path = self.env['PATH']
                    self.env['PATH'] = str(binary) + os.pathsep + old_path
                    try:
                        result = self.run_update(command, '--checkout', 'dev', '--update-current-branch')
                    finally:
                        self.env['PATH'] = old_path
                    self.assertNotEqual(result.returncode, 0)
                    self.assertIn('Checkout stopped; build will not start', result.stderr)
                    self.assertIn('Completed repositories: none', result.stderr)
                    self.assertFalse((root / 'built').exists())
                    self.assertEqual(self.git(repo, 'rev-parse', 'HEAD'), head)
                    self.assertEqual((repo / 'machine.yaml').read_text(), 'local configuration\n')


if __name__ == '__main__':
    unittest.main()
