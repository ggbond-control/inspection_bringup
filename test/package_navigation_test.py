"""Release-cache and dependency selection tests without Docker or compilation."""

import importlib.util
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

SPEC = importlib.util.spec_from_file_location(
    'package_navigation', Path(__file__).resolve().parents[1] / 'scripts/package_navigation.py')
release = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(release)


class Packaging(unittest.TestCase):
    def test_arm64_host_uses_arm_container_and_rejects_amd64_cache(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            release.write(root / 'inputs.json', '{"architecture": "arm64"}')
            argv = ['package_navigation.py', '--version', '0.3.5', '--package-only', '--work-dir', str(root)]
            def output(args, **kwargs):
                if args[:2] == ['dpkg', '--print-architecture']:
                    return 'arm64'
                if '{{.Architecture}}' in args:
                    return 'arm64'
                self.fail(f'unexpected output command: {args}')
            with patch.object(release.sys, 'argv', argv), \
                    patch.object(release, 'output', side_effect=output), \
                    patch.object(release.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0)) as inspect, \
                    patch.object(release, 'run') as run, \
                    patch.object(release, 'github_access') as auth:
                release.main()
                command = run.call_args.args[0]
                self.assertIn('linux/arm64', command)
                self.assertIn('inspection-navigation-builder:jazzy-arm64', command)
                self.assertIn('--package-only', command)
                auth.assert_not_called()
                run.reset_mock()
                inspect.return_value = subprocess.CompletedProcess([], 1)
                release.main()
                build = run.call_args_list[0].args[0]
                self.assertEqual(build[:2], ['docker', 'build'])
                self.assertIn('linux/arm64', build)
                run.reset_mock()
                release.write(root / 'inputs.json', '{"architecture": "amd64"}')
                with self.assertRaisesRegex(ValueError, 'cache architecture differs'):
                    release.main()
                run.assert_not_called()

    def test_arm64_colcon_uses_arm_flags_and_rejects_foreign_snapshot_before_building(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            manifest = {'architecture': 'arm64', 'builder_image': 'test',
                        'packages': {name: {'sha256': name} for name in release.SOURCES}}
            release.write(root / 'inputs.json', json.dumps(manifest))
            args = type('Arguments', (), {'package_only': False, 'select': None,
                                         'jobs': 1, 'version': '0.3.5'})()
            with patch.object(release, 'Path', return_value=root), \
                    patch.object(release, 'host_architecture', return_value='arm64'), \
                    patch.object(release, 'build_native') as native, \
                    patch.object(release, 'environment', return_value={}), \
                    patch.object(release, 'run') as run, \
                    patch.object(release, 'assemble'):
                release.inside(args)
                command = run.call_args.args[0]
                self.assertEqual(command[0], 'colcon')
                self.assertIn('-DCMAKE_CXX_FLAGS=-march=armv8-a -mtune=generic', command)
                self.assertFalse(any('x86-64' in str(arg) for arg in command))
                native.reset_mock()
                run.reset_mock()
                manifest['architecture'] = 'amd64'
                release.write(root / 'inputs.json', json.dumps(manifest))
                with self.assertRaisesRegex(ValueError, 'cache architecture differs'):
                    release.inside(args)
                native.assert_not_called()
                run.assert_not_called()

    def test_debian_control_and_artifact_names_follow_manifest_architecture(self):
        for architecture in ('amd64', 'arm64'):
            with self.subTest(architecture=architecture), tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                prefix = root / 'install'
                for name in release.SOURCES:
                    release.write(prefix / 'share/ament_index/resource_index/packages' / name, '')
                    (root / 'sources' / name).mkdir(parents=True)
                for name in release.NATIVE:
                    (root / 'native-src' / name).mkdir(parents=True)
                with patch.object(release, 'PREFIX', prefix), \
                        patch.object(release, 'output', return_value='shlibs:Depends=libc6'):
                    release.assemble(root, '0.3.5', {'architecture': architecture})
                deb = root / 'dist' / f'navigation_0.3.5_{architecture}.deb'
                actual = subprocess.check_output(['dpkg-deb', '-f', str(deb), 'Architecture'], text=True).strip()
                self.assertEqual(actual, architecture)
                recorded = json.loads(Path(str(deb) + '.manifest.json').read_text())
                self.assertEqual(recorded['architecture'], architecture)

    def test_unsupported_host_architecture_is_rejected(self):
        with patch.object(release, 'output', return_value='armhf'):
            with self.assertRaisesRegex(ValueError, 'unsupported native architecture'):
                release.host_architecture()

    def test_cli_rejects_missing_invalid_or_irrelevant_tag_before_build(self):
        script = Path(release.__file__)
        for extra, message in [([], '--tag is required'),
                               (['--tag', 'v0.3.5-rc1'], '--tag must be'),
                               (['--package-only', '--tag', 'v0.3.5'], 'omit --tag')]:
            result = subprocess.run(['/usr/bin/python3', str(script), '--version', '0.3.5', *extra],
                                    capture_output=True, text=True)
            self.assertEqual(result.returncode, 2)
            self.assertIn(message, result.stderr)

    def test_package_only_never_reads_remote_sources_or_credentials(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            release.write(root / 'inputs.json', '{"architecture": "amd64"}')
            previous = {'requested_tag': 'v0.3.4', 'architecture': 'amd64'}
            release.write(root / 'build-state.json', json.dumps(previous))
            args = type('Arguments', (), {'package_only': True, 'version': '0.3.5'})()
            with patch.object(release, 'Path', return_value=root), \
                    patch.object(release, 'host_architecture', return_value='amd64'), \
                    patch.object(release, 'assemble') as assemble, \
                    patch.object(release, 'github_access') as auth, \
                    patch.object(release, 'fetch_source') as fetch:
                release.inside(args)
                assemble.assert_called_once_with(root, '0.3.5', previous)
                auth.assert_not_called()
                fetch.assert_not_called()
                release.write(root / 'build-in-progress', 'incomplete')
                with self.assertRaisesRegex(ValueError, 'no complete build snapshot'):
                    release.inside(args)

    def test_version_selection_exact_nearest_lower_and_numeric_order(self):
        listing = '\n'.join(f'{i:040x}\trefs/tags/{tag}' for i, tag in enumerate(
            ['v0.3.4', 'v0.3.9', 'v0.3.10', 'v0.4.0', 'v0.3.11-rc1'], 1))
        exact = release.resolve_revision(listing, 'v0.3.10')
        self.assertEqual(exact['tag'], 'v0.3.10')
        self.assertEqual(exact['selection'], 'exact-tag')
        self.assertEqual(release.resolve_revision(listing, 'v0.3.12')['tag'], 'v0.3.10')
        self.assertEqual(release.resolve_revision(listing, 'v0.3.5')['tag'], 'v0.3.4')
        with self.assertRaisesRegex(ValueError, 'no version tag <='):
            release.resolve_revision(listing, 'v0.3.0')
        with self.assertRaisesRegex(ValueError, 'no version tag <='):
            release.resolve_revision('abc\trefs/tags/legacy', 'v0.3.5')

    def test_tagless_default_branch_and_annotated_tag(self):
        branch = release.resolve_revision('ref: refs/heads/jazzy\tHEAD\nabc\tHEAD', 'v0.3.5')
        self.assertEqual(branch['branch'], 'jazzy')
        self.assertEqual(branch['commit'], 'abc')
        self.assertIsNone(branch['tag'])
        tag = release.resolve_revision('tagobj\trefs/tags/v0.3.5\n'
                                       'commit\trefs/tags/v0.3.5^{}', 'v0.3.5')
        self.assertEqual(tag['commit'], 'commit')
        with self.assertRaisesRegex(ValueError, 'no advertised default branch'):
            release.resolve_revision('', 'v0.3.5')

    def test_git_fetches_immutable_snapshot_and_does_not_modify_remote(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            remote, cache, copied = root / 'upstream', root / 'cache', root / 'sources'
            def git(*args):
                return subprocess.check_output(['git', '-C', str(remote), *args],
                                               text=True, stderr=subprocess.DEVNULL).strip()
            subprocess.run(['git', 'init', '-q', '-b', 'main', str(remote)], check=True)
            git('config', 'user.name', 'Test')
            git('config', 'user.email', 'test@example.com')
            release.write(remote / 'source.cpp', 'first')
            git('add', '.')
            git('commit', '-qm', 'first')
            first = git('rev-parse', 'HEAD')
            # With no tags, fetch the default branch, including a later update.
            env = dict(os.environ, GIT_TERMINAL_PROMPT='0')
            rev = release.fetch_source(str(remote), 'v0.3.5', cache, env)
            self.assertEqual(rev['selection'], 'default-branch')
            release.snapshot(cache, copied)
            release.write(copied / 'source.cpp', 'release adjustment')
            mtime = (copied / 'source.cpp').stat().st_mtime_ns
            release.fetch_source(str(remote), 'v0.3.5', cache, env)
            release.snapshot(cache, copied)
            self.assertEqual((copied / 'source.cpp').stat().st_mtime_ns, mtime)
            release.write(remote / 'source.cpp', 'second')
            git('commit', '-qam', 'second')
            second = git('rev-parse', 'HEAD')
            self.assertEqual(release.fetch_source(str(remote), 'v0.3.5', cache, env)['commit'], second)
            release.snapshot(cache, copied)
            self.assertEqual((copied / 'source.cpp').read_text(), 'second')
            git('tag', '-a', 'v0.3.4', first, '-m', 'release')
            before = git('show-ref')
            rev = release.fetch_source(str(remote), 'v0.3.5', cache, env)
            self.assertEqual(rev['tag'], 'v0.3.4')
            self.assertEqual(rev['commit'], first)
            git('tag', 'v0.3.5', second)
            before = git('show-ref')
            rev = release.fetch_source(str(remote), 'v0.3.5', cache, env)
            self.assertEqual(rev['commit'], second)
            self.assertEqual(rev['selection'], 'exact-tag')
            self.assertEqual(git('show-ref'), before)
            self.assertEqual(git('status', '--porcelain'), '')
            self.assertNotIn('Authorization', (cache / '.git/config').read_text())

    def test_token_is_scoped_to_git_environment_and_diagnostics_are_suppressed(self):
        with patch.object(release.sys.stdin, 'isatty', return_value=True), \
                patch.object(release.getpass, 'getpass', return_value='test-secret'), \
                patch.dict(os.environ, {'GIT_TRACE': '1', 'GIT_CURL_VERBOSE': '1'}):
            with release.github_access() as env:
                self.assertNotIn('GIT_TRACE', env)
                self.assertNotIn('GIT_CURL_VERBOSE', env)
                self.assertEqual(env['GIT_CONFIG_VALUE_0'], '')
                self.assertNotIn('test-secret', env['GIT_CONFIG_VALUE_1'])
                with patch.object(release.subprocess, 'run', return_value=
                                  subprocess.CompletedProcess([], 1, '', 'test-secret')):
                    with self.assertRaises(ValueError) as error:
                        release.remote_git(['ls-remote', 'https://github.com/test'], env)
                    self.assertNotIn('test-secret', str(error.exception))
            self.assertEqual(env, {})

    def test_native_cache_is_keyed_by_commit(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            prefix = root / 'install'
            release.write(root / 'inputs.json', json.dumps({'architecture': 'arm64', 'native': {'gtsam': {'commit': 'new'}}}))
            release.write(root / 'gtsam.installed', 'old\n')
            release.write(prefix / 'vendor/lib/libgtsam.so', 'library')
            with patch.object(release, 'PREFIX', prefix), \
                    patch.object(release, 'NATIVE', {'gtsam': None}), \
                    patch.object(release, 'run') as run:
                release.build_native(root, 2)
                self.assertEqual(run.call_count, 3)
                configure = run.call_args_list[0].args[0]
                self.assertIn('-DCMAKE_CXX_FLAGS=-march=armv8-a -mtune=generic', configure)
                self.assertFalse(any('x86-64' in str(arg) for arg in configure))
                self.assertEqual((root / 'gtsam.installed').read_text(), 'new\n')
                run.reset_mock()
                release.build_native(root, 2)
                run.assert_not_called()

    def test_obsolete_installed_resources_are_removed_without_deleting_current_files(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            prefix = root / 'install'
            obsolete = prefix / 'share/local_planner/old.yaml'
            current = prefix / 'share/local_planner/new.yaml'
            release.write(obsolete, 'old')
            release.write(current, 'new')
            release.write(root / 'build/local_planner/install_manifest.txt', str(current) + '\n')
            with patch.object(release, 'PREFIX', prefix):
                installed = release.installed_files(root)
                self.assertEqual(installed, {str(current)})
                release.prune_obsolete({str(obsolete), str(current)}, installed)
                self.assertFalse(obsolete.exists())
                self.assertTrue(current.exists())
                with self.assertRaisesRegex(ValueError, 'invalid cached install path'):
                    release.prune_obsolete({str(root / 'outside')}, set())

    def test_selection_includes_transitive_consumers_but_not_unrelated_packages(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            for name in release.SOURCES:
                dependencies = {'local_planner': ['inspection_interfaces'],
                                'multi_map_nav': ['inspection_interfaces'],
                                'faster_lio': ['livox_ros_driver2', 'ndt_omp', 'inspection_interfaces'],
                                'gridmapper': ['control_input_msgs']}.get(name, [])
                release.write(root / name / 'package.xml', '<package><name>' + name + '</name>' +
                              ''.join('<depend>' + v + '</depend>' for v in dependencies) + '</package>')
            self.assertEqual(release.selected_packages(['local_planner'], root), ['local_planner'])
            self.assertEqual(release.selected_packages(['livox_ros_driver2'], root),
                             ['faster_lio', 'livox_ros_driver2'])
            self.assertEqual(release.selected_packages(['inspection_interfaces'], root),
                             ['faster_lio', 'inspection_interfaces', 'local_planner', 'multi_map_nav'])

    def test_snapshot_keeps_unchanged_release_edits_and_handles_changed_deleted_inputs(self):
        with tempfile.TemporaryDirectory() as temp:
            source, target = Path(temp) / 'source', Path(temp) / 'copy'
            source.mkdir()
            subprocess.run(['git', 'init', '-q', str(source)], check=True)
            release.write(source / 'CMakeLists.txt', 'original')
            release.write(source / '.gitignore', 'secret.yaml\n')
            release.write(source / 'secret.yaml', 'excluded')
            first = release.snapshot(source, target)
            release.write(target / 'CMakeLists.txt', 'release adaptation')
            mtime = (target / 'CMakeLists.txt').stat().st_mtime_ns
            self.assertEqual(release.snapshot(source, target), first)
            self.assertEqual((target / 'CMakeLists.txt').stat().st_mtime_ns, mtime)
            self.assertEqual((target / 'CMakeLists.txt').read_text(), 'release adaptation')
            self.assertFalse((target / 'secret.yaml').exists())
            release.write(source / 'CMakeLists.txt', 'upstream change')
            self.assertNotEqual(release.snapshot(source, target), first)
            self.assertEqual((target / 'CMakeLists.txt').read_text(), 'upstream change')
            (source / 'CMakeLists.txt').unlink()
            release.snapshot(source, target)
            self.assertFalse((target / 'CMakeLists.txt').exists())

    def test_external_source_symlinks_are_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            source = Path(temp) / 'source'
            source.mkdir()
            subprocess.run(['git', 'init', '-q', str(source)], check=True)
            outside = Path(temp) / 'outside'
            outside.write_text('outside')
            (source / 'external').symlink_to(outside)
            with self.assertRaisesRegex(ValueError, 'symlink requires review'):
                release.snapshot(source, Path(temp) / 'copy')


if __name__ == '__main__':
    unittest.main()
