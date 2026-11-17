"""Release-cache and dependency selection tests without Docker or compilation."""

import importlib.util
import copy
from contextlib import nullcontext
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
    def test_image_environment_ignores_metadata_id_but_checks_layers(self):
        info = dict(Id='old', Architecture='arm64', Os='linux', RootFS={'Layers': ['layer']},
                    Config={'Env': ['PATH=/bin']})
        with patch.object(release, 'output', side_effect=lambda *a, **k: json.dumps([info])):
            first = release.image_environment('image')
            info['Id'] = 'new'
            self.assertEqual(release.image_environment('image'), first)
            info['RootFS']['Layers'].append('new-layer')
            self.assertNotEqual(release.image_environment('image'), first)

    def test_token_is_prompted_once_and_cleared_on_failure(self):
        argv = ['pack', '--version', '0.3.11', '--tag', 'v0.3.11']
        environments = []
        def build(args):
            with release.GITHUB_SESSION.access() as env:
                environments.append(env)
            if args.component == 'task':
                raise ValueError('build failed')
            return Path('/tmp/driver.deb')
        with patch.object(release.sys, 'argv', argv), \
                patch.object(release.sys.stdin, 'isatty', return_value=True), \
                patch.object(release.getpass, 'getpass', return_value='test-token') as prompt, \
                patch.object(release, 'build_component', side_effect=build):
            with self.assertRaisesRegex(ValueError, 'build failed'):
                release.main()
            prompt.assert_called_once()
        self.assertEqual(len(environments), 3)
        self.assertTrue(all(v is environments[0] for v in environments))
        self.assertEqual(environments[0], {})
        self.assertIsNone(release.GITHUB_SESSION)

    def test_environment_changes_rebuild_component_and_keep_sources(self):
        for same_environment in (True, False):
            with self.subTest(same=same_environment), tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                manifest = dict(architecture='amd64', builder_image='new', builder_environment='env',
                                release_recipe_sha256='recipe', packages={n: {'sha256': n} for n in release.SOURCES})
                previous = dict(manifest, builder_image='old',
                                builder_environment='env' if same_environment else 'old-env')
                release.write(root / 'inputs.json', json.dumps(manifest))
                release.write(root / 'build-state.json', json.dumps(previous))
                release.write(root / 'build/old-object', 'object')
                release.write(root / 'repositories/local_planner/source', 'source')
                args = type('Args', (), dict(package_only=False, select=['local_planner'], jobs=1, version='0.3.11'))()
                with patch.object(release, 'Path', return_value=root), \
                        patch.object(release, 'host_architecture', return_value='amd64'), \
                        patch.object(release, 'selected_packages', return_value=['local_planner']), \
                        patch.object(release, 'environment', return_value={}), \
                        patch.object(release, 'run'), patch.object(release, 'assemble'):
                    release.inside(args)
                self.assertEqual((root / 'build/old-object').exists(), same_environment)
                self.assertTrue((root / 'repositories/local_planner/source').exists())
                self.assertEqual(args.select is None, not same_environment)

    def test_interrupted_environment_rebuild_reuses_new_objects(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            manifest = dict(architecture='amd64', builder_image='new', builder_environment='env',
                            release_recipe_sha256='recipe', packages={n: {'sha256': n} for n in release.SOURCES})
            release.write(root / 'inputs.json', json.dumps(manifest))
            release.write(root / 'build-state.json', json.dumps(dict(manifest, builder_image='old', builder_environment='old-env')))
            args = type('Args', (), dict(package_only=False, select=None, jobs=1, version='0.3.11'))()
            with patch.object(release, 'Path', return_value=root), \
                    patch.object(release, 'host_architecture', return_value='amd64'), \
                    patch.object(release, 'environment', return_value={}), \
                    patch.object(release, 'run', side_effect=ValueError('compile failed')):
                with self.assertRaisesRegex(ValueError, 'compile failed'):
                    release.inside(args)
                release.write(root / 'build/new-object', 'partial progress')
                with self.assertRaisesRegex(ValueError, 'compile failed'):
                    release.inside(args)
                self.assertTrue((root / 'build/new-object').is_file())

    def setUp(self):
        release.configure_component('navigation')
        self.addCleanup(release.configure_component, 'navigation')

    def make_driver_deb(self, root, architecture='amd64'):
        release.configure_component('driver')
        release.SOURCES.update({n: None for n in release.EMBEDDED_SOURCES})
        prefix = root / 'install'
        for name in release.SOURCES:
            release.write(prefix / 'share/ament_index/resource_index/packages' / name, '')
            (root / 'sources' / name).mkdir(parents=True, exist_ok=True)
        for name in release.NATIVE:
            (root / 'native-src' / name).mkdir(parents=True, exist_ok=True)
        for model in ('x30', 'd1_max', 'cyvet', 'tank', 'r750'):
            path = prefix / f'lib/nav_bridge/{model}_nav_bridge_node'
            release.write(path, '#!/bin/sh\nexit 0\n')
            path.chmod(0o755)
        release.write(prefix / 'local_setup.bash', '# underlay\n')
        release.write(prefix / 'include/inspection_interfaces/example.hpp', '// interface\n')
        release.write(prefix / 'vendor/lib/cmake/GTSAM/GTSAMConfig.cmake', '# gtsam\n')
        release.write(prefix / 'vendor/lib/libgtsam.a', 'archive')
        with patch.object(release, 'PREFIX', prefix):
            release.assemble(root, '0.3.5', {'architecture': architecture})
        return root / 'dist' / f'control-driver_0.3.5_{architecture}.deb'

    def test_driver_payload_keeps_build_exports_all_backends_and_no_navigation(self):
        for arch in ('amd64', 'arm64'):
            with self.subTest(arch=arch), tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                deb = self.make_driver_deb(root, arch)
                contents = subprocess.check_output(['dpkg-deb', '-c', deb], text=True)
                self.assertIn('/include/inspection_interfaces/example.hpp', contents)
                self.assertIn('/vendor/lib/cmake/GTSAM/GTSAMConfig.cmake', contents)
                self.assertIn('/vendor/lib/libgtsam.a', contents)
                self.assertNotIn('resource_index/packages/local_planner', contents)
                deps = subprocess.check_output(['dpkg-deb', '-f', deb, 'Depends'], text=True)
                for value in release.ARM64_RUNTIME_DEPENDS + ['ros-jazzy-navigation2', 'ros-jazzy-grid-map']:
                    self.assertNotIn(value, deps)

    def test_driver_underlay_validates_architecture_and_compatibility_bounds(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            deb = self.make_driver_deb(root / 'driver')
            args = type('Args', (), dict(driver_deb=deb, driver_min_version=None,
                                        driver_max_version=None))()
            # The fixture prefix is arbitrary, so tell the extractor where it lives.
            with patch.object(release, 'DRIVER_PREFIX', root / 'driver/install'):
                record, minimum, maximum = release.driver_underlay(args, root / 'nav', 'amd64')
                self.assertEqual((minimum, maximum), ('0.3.5', '0.4.0'))
                self.assertEqual(record['version'], '0.3.5')
                with self.assertRaisesRegex(ValueError, 'architecture'):
                    release.driver_underlay(args, root / 'nav', 'arm64')
                args.driver_max_version = '0.3.5'
                with self.assertRaisesRegex(ValueError, 'outside'):
                    release.driver_underlay(args, root / 'nav', 'amd64')

    def test_default_all_dispatches_independent_versions_and_passes_driver_artifact(self):
        argv = ['pack', '--driver-version', '0.3.5', '--navigation-version', '0.4.2',
                '--driver-tag', 'v0.3.5', '--navigation-tag', 'v0.4.2',
                '--task-version', '0.2.0', '--task-tag', 'v0.2.0']
        calls = []
        def build(args):
            calls.append(copy_args := dict(vars(args)))
            return Path('/tmp/control-' + copy_args['component'] + '.deb')
        with patch.object(release.sys, 'argv', argv), patch.object(release, 'build_component', side_effect=build):
            release.main()
        self.assertEqual([v['component'] for v in calls], ['driver', 'navigation', 'task'])
        self.assertEqual([v['version'] for v in calls], ['0.3.5', '0.4.2', '0.2.0'])
        self.assertEqual(calls[1]['driver_deb'], Path('/tmp/control-driver.deb'))
        self.assertEqual(calls[2]['driver_deb'], Path('/tmp/control-driver.deb'))

    def test_task_dispatch_is_isolated_and_uses_driver_only(self):
        argv = ['pack', '--component', 'task', '--version', '0.3.11', '--tag', 'v0.3.11',
                '--driver-deb', '/tmp/driver.deb']
        with patch.object(release.sys, 'argv', argv), patch.object(release, 'build_component') as build:
            release.main()
        build.assert_called_once()
        self.assertEqual(release.PREFIX, Path('/opt/inspection/task'))
        self.assertEqual(set(release.SOURCES), set(release.TASK_SOURCES))
        self.assertFalse(release.NATIVE)
        self.assertEqual(release.builder_image('arm64'), 'inspection-task-builder:jazzy-arm64')
        self.assertNotIn('inspection_interfaces', release.SOURCES)
        self.assertNotIn('inspection_bringup', release.SOURCES)

    def test_component_recipe_and_image_isolation(self):
        records = {}
        for component in release.COMPONENTS:
            release.configure_component(component)
            records[component] = (release.recipe_id(), release.builder_image('amd64'))
        self.assertEqual(len({v[0] for v in records.values()}), 3)
        self.assertEqual(records['driver'][1], records['navigation'][1])
        self.assertNotEqual(records['task'][1], records['driver'][1])

    def test_catalog_is_the_source_for_package_mapping_and_component_metadata(self):
        catalog = copy.deepcopy(release.CATALOG)
        catalog['components']['task']['packages']['inspection_task_hub'] = 'task-hub-release'
        catalog['components']['task']['deb_name'] = 'custom-task'
        catalog['components']['task']['prefix'] = '/opt/inspection/custom-task'
        with patch.object(release, 'COMPONENT_CONFIG', catalog['components']):
            release.configure_component('task')
            self.assertEqual(release.SOURCES['inspection_task_hub'], 'task-hub-release')
            self.assertEqual(release.PACKAGE_NAME, 'custom-task')
            self.assertEqual(release.PREFIX, Path('/opt/inspection/custom-task'))

    def test_catalog_rejects_duplicate_ownership_unsafe_paths_and_missing_fields(self):
        cases = [
            (lambda c: c['components']['task']['packages'].update(inspection_interfaces='inspection_interfaces'), 'duplicate package'),
            (lambda c: c['components']['driver'].update(prefix='/tmp/driver'), 'invalid install prefix'),
            (lambda c: c['components']['task'].update(dockerfile='../Dockerfile'), 'invalid Dockerfile'),
            (lambda c: c['components']['task'].pop('deb_name'), 'missing deb_name'),
            (lambda c: c['components']['task']['packages'].update(example='../example'), 'invalid source'),
        ]
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'packages.yaml'
            for mutation, message in cases:
                catalog = copy.deepcopy(release.CATALOG)
                mutation(catalog)
                path.write_text(release.yaml.safe_dump(catalog))
                with self.assertRaisesRegex(ValueError, message):
                    release.load_package_catalog(path)
            path.write_text('schema_version: 1\nschema_version: 1\n')
            with self.assertRaisesRegex(ValueError, 'duplicate YAML key'):
                release.load_package_catalog(path)

    def test_task_catalog_changes_do_not_invalidate_other_component_recipes(self):
        baseline = {}
        for component in release.COMPONENTS:
            release.configure_component(component)
            baseline[component] = release.recipe_id()
        catalog = copy.deepcopy(release.COMPONENT_CONFIG)
        catalog['task']['runtime_dependencies'].append('example-dependency')
        with patch.object(release, 'COMPONENT_CONFIG', catalog):
            for component in release.COMPONENTS:
                release.configure_component(component)
                self.assertEqual(release.recipe_id() == baseline[component], component != 'task')

    def test_task_requires_driver_and_rejects_foreign_package_selection(self):
        for extra, message in [([], 'requires --driver-deb'),
                               (['--driver-deb', '/tmp/d.deb', '--select', 'local_planner'], 'outside')]:
            result = subprocess.run(['/usr/bin/python3', release.__file__, '--component', 'task',
                                     '--version', '0.3.11', '--tag', 'v0.3.11', *extra],
                                    capture_output=True, text=True)
            self.assertEqual(result.returncode, 2)
            self.assertIn(message, result.stderr)

    def test_task_payload_and_architecture_filtered_sdk_libraries(self):
        for arch, selected, machine in [('amd64', 'x64', 62), ('arm64', 'arm64', 183)]:
            with self.subTest(arch=arch), tempfile.TemporaryDirectory() as temp:
                release.configure_component('task')
                root = Path(temp)
                prefix = root / 'install'
                for name in release.SOURCES:
                    release.write(prefix / 'share/ament_index/resource_index/packages' / name, '')
                    (root / 'sources' / name).mkdir(parents=True)
                release.write(root / 'build/sensor_gimbal/CMakeCache.txt',
                              'GIMBAL_ENABLE_RKNN:STRING=AUTO\nGIMBAL_ENABLE_HKNETSDK:STRING=AUTO\n')
                sdk = root / 'sources/sensor_gimbal/third_party/rknn/lib/librknnrt.so'
                sdk.parent.mkdir(parents=True)
                sdk.write_bytes(b'\x7fELF' + b'\x00\x01' + bytes(12) + machine.to_bytes(2, 'little'))
                release.write(root / 'sources/sensor_gimbal/third_party/rknn/include/rknn_api.h', '// sdk')
                release.write(root / f'sources/acoustic_monitor/third_party/micarr_sdk/{selected}/lib/libmicArrSDK.so', 'sdk')
                for other in ('x64', 'arm64'):
                    release.write(prefix / f'share/acoustic_monitor/third_party/micarr_sdk/{other}/lib/libmicArrSDK.so', 'sdk')
                with patch.object(release, 'PREFIX', prefix), patch.object(release, 'run', wraps=release.run) as run, \
                        patch.object(release, 'output', return_value='shlibs:Depends=libc6'):
                    # Fixture ELF has only a header, so intercept ELF manipulation.
                    def execute(args, **kwargs):
                        if args[0] in ('strip', 'patchelf'):
                            return None
                        return release.subprocess.run([str(v) for v in args], check=True, **kwargs)
                    run.side_effect = execute
                    release.assemble(root, '0.3.11', dict(architecture=arch,
                                     driver_min_version='0.3.5', driver_max_version='0.4.0'))
                deb = root / 'dist' / f'control-task_0.3.11_{arch}.deb'
                deps = subprocess.check_output(['dpkg-deb', '-f', deb, 'Depends'], text=True)
                self.assertIn('control-driver (>= 0.3.5)', deps)
                self.assertNotIn('control-navigation', deps)
                self.assertNotIn('mesa-opencl-icd', deps)
                self.assertIn('python3-paho-mqtt', deps)
                manifest = json.loads(Path(str(deb) + '.manifest.json').read_text())
                self.assertTrue(manifest['features']['rknn'])
                self.assertFalse(manifest['features']['hknetsdk'])
                contents = subprocess.check_output(['dpkg-deb', '-c', deb], text=True)
                self.assertIn('/vendor/lib/libmicArrSDK.so', contents)
                self.assertNotIn(f'micarr_sdk/{"arm64" if selected == "x64" else "x64"}/', contents)

    def test_navigation_only_does_not_dispatch_driver_build(self):
        argv = ['pack', '--component', 'navigation', '--version', '0.4.2', '--tag', 'v0.4.2',
                '--driver-deb', '/tmp/driver.deb']
        with patch.object(release.sys, 'argv', argv), patch.object(release, 'build_component') as build:
            release.main()
        build.assert_called_once()
        self.assertEqual(build.call_args.args[0].component, 'navigation')
        self.assertFalse(release.NATIVE)
        self.assertFalse(set(release.SOURCES) & release.DRIVER_PACKAGES)

    def test_navigation_fetches_only_navigation_repositories_and_mounts_driver(self):
        with tempfile.TemporaryDirectory() as temp:
            def fetched(url, tag, path, env):
                release.write(path / 'package.xml', f'<package><name>{path.name}</name></package>')
                return {'commit': 'abc'}
            argv = ['pack', '--component', 'navigation', '--version', '0.4.2', '--tag', 'v0.4.2',
                    '--driver-deb', '/tmp/driver.deb', '--work-dir', temp]
            with patch.object(release.sys, 'argv', argv), \
                    patch.object(release, 'host_architecture', return_value='amd64'), \
                    patch.object(release, 'output', return_value='amd64'), \
                    patch.object(release, 'image_environment', return_value='environment'), \
                    patch.object(release.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0)), \
                    patch.object(release, 'driver_underlay', return_value=({'version': '0.3.5'}, '0.3.5', '0.4.0')), \
                    patch.object(release, 'github_access', return_value=nullcontext({})), \
                    patch.object(release, 'fetch_source', side_effect=fetched) as fetch, \
                    patch.object(release, 'snapshot', return_value='hash'), \
                    patch.object(release, 'release_adjustments'), patch.object(release, 'run') as run:
                release.main()
            self.assertEqual(fetch.call_count, 5)
            self.assertFalse(any('/nav_bridge.git' in call.args[0] for call in fetch.call_args_list))
            command = run.call_args.args[0]
            self.assertTrue(any('driver-underlay/opt/inspection/driver:/opt/inspection/driver:ro' in str(v)
                                for v in command))

    def test_cli_rejects_cross_component_selection_and_missing_driver(self):
        cases = [(['--component', 'driver', '--select', 'local_planner'], 'outside'),
                 (['--component', 'navigation'], 'requires --driver-deb'),
                 (['--component', 'navigation', '--driver-deb', '/tmp/d.deb', '--select', 'nav_bridge'], 'outside')]
        for extra, message in cases:
            result = subprocess.run(['/usr/bin/python3', release.__file__, '--version', '0.3.5',
                                     '--tag', 'v0.3.5', *extra], capture_output=True, text=True)
            self.assertEqual(result.returncode, 2)
            self.assertIn(message, result.stderr)

    def test_driver_build_enables_every_backend_and_embedded_packages(self):
        release.configure_component('driver')
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            packages = set(release.SOURCES) | release.EMBEDDED_SOURCES.keys()
            manifest = {'architecture': 'amd64', 'builder_image': 'test',
                        'packages': {n: {'sha256': n} for n in packages}}
            release.write(root / 'inputs.json', json.dumps(manifest))
            args = type('Arguments', (), dict(package_only=False, select=None, jobs=2, version='0.3.5'))()
            with patch.object(release, 'Path', return_value=root), \
                    patch.object(release, 'host_architecture', return_value='amd64'), \
                    patch.object(release, 'build_native') as native, \
                    patch.object(release, 'environment', return_value={}), \
                    patch.object(release, 'run') as run, patch.object(release, 'assemble'):
                release.inside(args)
            native.assert_called_once()
            command = run.call_args.args[0]
            for flag in release.NAV_BRIDGE_OPTIONS:
                self.assertIn(flag, command)
            self.assertIn('uniubi', command)
            self.assertIn('uniubi_motion_client', command)
            self.assertNotIn('local_planner', command)

    def test_embedded_bridge_sources_add_dependencies_and_remove_stale_files(self):
        release.configure_component('driver')
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            bridge = root / 'sources/nav_bridge'
            release.write(bridge / 'package.xml', '<package><name>nav_bridge</name></package>')
            for name, relative in release.EMBEDDED_SOURCES.items():
                release.write(bridge / 'third_party/uniubi' / relative / 'package.xml',
                              f'<package><name>{name}</name></package>')
            release.write(bridge / 'third_party/uniubi/uniubi_robot_msgs/idl/MotorState.idl', 'idl')
            release.write(root / 'sources/uniubi/obsolete.txt', 'old')
            manifest = {'packages': {'nav_bridge': {'sha256': 'bridge', 'commit': 'abc'}}}
            release.prepare_bridge_sources(root, manifest)
            self.assertFalse((root / 'sources/uniubi/obsolete.txt').exists())
            self.assertTrue((root / 'sources/idl/MotorState.idl').exists())
            tree = release.ET.parse(bridge / 'package.xml').getroot()
            self.assertEqual({v.text for v in tree.findall('depend')}, set(release.EMBEDDED_SOURCES))
            before = manifest['packages']['uniubi']['sha256']
            release.write(bridge / 'third_party/uniubi/uniubi_robot_msgs/idl/MotorState.idl', 'changed')
            release.prepare_bridge_sources(root, manifest)
            self.assertNotEqual(before, manifest['packages']['uniubi']['sha256'])

    def test_changed_driver_blocks_selected_navigation_rebuild(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            manifest = {'architecture': 'amd64', 'builder_image': 'test',
                        'release_recipe_sha256': 'recipe', 'driver': {'sha256': 'new'}}
            release.write(root / 'inputs.json', json.dumps(manifest))
            release.write(root / 'build-state.json', json.dumps(dict(manifest, driver={'sha256': 'old'})))
            args = type('Arguments', (), dict(package_only=False, select=['local_planner'],
                                             jobs=1, version='0.4.2'))()
            with patch.object(release, 'Path', return_value=root), \
                    patch.object(release, 'host_architecture', return_value='amd64'), \
                    patch.object(release, 'run') as run:
                with self.assertRaisesRegex(ValueError, 'driver underlay changed'):
                    release.inside(args)
                run.assert_not_called()

    def test_driver_repackaging_preserves_build_identity(self):
        self.assertEqual(
            release.driver_build_id({'driver': {'version': '0.3.5', 'sha256': 'archive-a', 'build_id': 'same'}}),
            release.driver_build_id({'driver': {'version': '0.3.5-1', 'sha256': 'archive-b', 'build_id': 'same'}}))

    def test_gtsam_exports_keep_installed_paths_without_build_tree_references(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            for package in ('GTSAM', 'GTSAM_UNSTABLE'):
                path = root / f'vendor/lib/cmake/{package}/{package}Config.cmake'
                release.write(path, '# Compute paths\n'
                    'get_filename_component(OUR_CMAKE_DIR "${CMAKE_CURRENT_LIST_FILE}" PATH)\n'
                    'if(EXISTS "${OUR_CMAKE_DIR}/CMakeCache.txt")\n'
                    '  # In build tree\n'
                    f'  set({package}_INCLUDE_DIR /release/native-src/gtsam CACHE PATH "include")\n'
                    'else()\n  # Find installed library\n'
                    f'  set({package}_INCLUDE_DIR "${{OUR_CMAKE_DIR}}/../../../include" CACHE PATH "include")\n'
                    'endif()\ninclude(${OUR_CMAKE_DIR}/exports.cmake)\n')
            release.relocate_gtsam_exports(root)
            for package in ('GTSAM', 'GTSAM_UNSTABLE'):
                path = root / f'vendor/lib/cmake/{package}/{package}Config.cmake'
                text = path.read_text()
                self.assertNotIn('/release/', text)
                self.assertNotIn('CMakeCache.txt', text)
                self.assertIn('${OUR_CMAKE_DIR}/../../../include', text)
                self.assertIn('include(${OUR_CMAKE_DIR}/exports.cmake)', text)
            release.relocate_gtsam_exports(root)

    def test_arm64_host_uses_arm_container_and_rejects_amd64_cache(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            release.write(root / 'driver/inputs.json', '{"architecture": "arm64"}')
            argv = ['package_navigation.py', '--component', 'driver', '--version', '0.3.5', '--package-only', '--work-dir', str(root)]
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
                self.assertIn('inspection-control-builder:jazzy-arm64', command)
                self.assertIn('--package-only', command)
                auth.assert_not_called()
                run.reset_mock()
                inspect.return_value = subprocess.CompletedProcess([], 1)
                release.main()
                build = run.call_args_list[0].args[0]
                self.assertEqual(build[:2], ['docker', 'build'])
                self.assertIn('linux/arm64', build)
                run.reset_mock()
                release.write(root / 'driver/inputs.json', '{"architecture": "amd64"}')
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
                    release.assemble(root, '0.3.5', {'architecture': architecture,
                                     'driver_min_version': '0.3.0', 'driver_max_version': '0.4.0'})
                deb = root / 'dist' / f'control-navigation_0.3.5_{architecture}.deb'
                actual = subprocess.check_output(['dpkg-deb', '-f', str(deb), 'Architecture'], text=True).strip()
                self.assertEqual(actual, architecture)
                def field(name):
                    return subprocess.check_output(['dpkg-deb', '-f', str(deb), name], text=True).strip()
                self.assertEqual(field('Package'), 'control-navigation')
                self.assertEqual(field('Conflicts'), 'navigation, fhzn-navigation')
                self.assertEqual(field('Replaces'), 'navigation, fhzn-navigation')
                dependencies = field('Depends').split(', ')
                self.assertIn('control-driver (>= 0.3.0)', dependencies)
                self.assertIn('control-driver (<< 0.4.0)', dependencies)
                for dependency in ('opencl-headers', 'ocl-icd-opencl-dev', 'mesa-opencl-icd', 'clinfo'):
                    self.assertEqual(dependency in dependencies, architecture == 'arm64')
                contents = subprocess.check_output(['dpkg-deb', '-c', str(deb)], text=True)
                self.assertIn('./usr/share/doc/control-navigation/README', contents)
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
        self.enterContext(patch.object(release, 'SOURCES', release.ALL_SOURCES))
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
