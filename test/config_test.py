"""Configuration and deployment rendering tests; no ROS processes or hardware."""
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import yaml
from config_fixtures import package_environment, package_prefix

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
import bringup_config as cfg


class Configuration(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='inspection config ')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / 'config'
        shutil.copytree(ROOT / 'config', self.root, ignore=shutil.ignore_patterns('robot.yaml'))
        self.prefix = package_prefix(self.temp.name)
        self.env = patch.dict(os.environ, package_environment(self.prefix),
                              INSPECTION_CONFIG_DIR=str(self.root))
        self.env.start()
        self.addCleanup(self.env.stop)

    def write(self, relative, data):
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(yaml.safe_dump(data, sort_keys=False))
        return path

    def package_file(self, package, name, directory='config'):
        return str(self.prefix / 'share' / package / directory / name)

    def robot(self, **overrides):
        data = {'model': 'd1m', 'robot_id': 'D1M-001'}
        data.update(overrides)
        return self.write('robot.yaml', data)

    def run_script(self, name, *args):
        return subprocess.run(['bash', str(ROOT / 'scripts' / name), *args],
                              text=True, capture_output=True, timeout=15)

    def test_no_robot_preserves_all_legacy_defaults(self):
        for module, filename in cfg.FILENAMES.items():
            with self.subTest(module=module):
                raw = cfg.read_yaml(self.root / filename)
                resolved = cfg.load_config(module)
                if module == 'navigation':
                    for section, field, profile in [
                        ('nav_bridge', 'params_file', 'nav_bridge/x30_params.yaml'),
                        ('slam', 'config', 'faster_lio/mid360.yaml'),
                        ('terrain', 'params_file', 'gridmapper/gridmapper_local.yaml'),
                        ('local_planner', 'config', 'local_planner/x30.yaml'),
                        ('global_planner', 'params_path', 'multi_map_nav/new_local.yaml')]:
                        self.assertEqual(cfg.read_yaml(resolved[section][field]),
                                         cfg.read_yaml(self.root / 'navigation' / profile))
                        raw[section][field] = resolved[section][field]
                self.assertEqual(resolved, raw)

    def test_defaults_model_robot_merge_and_identity(self):
        self.write('models/d1m/system.yaml', {'mqtt': {'port': 2345}, 'modules': {'gas': False}})
        self.robot(system={'mqtt': {'host': 'unit-test.invalid'}})
        value = cfg.load_config('system')
        self.assertEqual(value['mqtt']['host'], 'unit-test.invalid')
        self.assertEqual(value['mqtt']['port'], 2345)
        self.assertEqual(value['mqtt']['sn'], 'D1M-001')
        self.assertEqual(value['mission_execution_agent']['robot_id'], 'D1M-001')
        self.assertFalse(value['modules']['gas'])
        self.assertTrue(value['modules']['charge'])

    def test_independent_identity_override(self):
        self.robot(system={'mqtt': {'sn': 'platform-device'}})
        value = cfg.load_config('system')
        self.assertEqual(value['mqtt']['sn'], 'platform-device')
        self.assertEqual(value['mission_execution_agent']['robot_id'], 'D1M-001')

    def test_false_zero_empty_list_and_empty_string_are_preserved(self):
        self.robot(system={'modules': {'gas': False}, 'mqtt': {'port': 0, 'password': ''},
                           'fleet_leader_election': {'robot_ids': []}})
        value = cfg.load_config('system')
        self.assertIs(value['modules']['gas'], False)
        self.assertEqual(value['mqtt']['port'], 0)
        self.assertEqual(value['mqtt']['password'], '')
        self.assertEqual(value['fleet_leader_election']['robot_ids'], [])

    def test_lists_replace_instead_of_append(self):
        self.robot(navigation={'bringup': {'modes': {'nav': {'sequence': ['nav_bridge']}}}})
        self.assertEqual(cfg.load_config('navigation')['bringup']['modes']['nav']['sequence'], ['nav_bridge'])

    def test_model_selection(self):
        for model, robot_type, planner in [('d1m', 'd1_max', 'd1m.yaml'), ('x30', 'x30', 'x30.yaml')]:
            self.robot(model=model)
            value = cfg.load_config('navigation')
            self.assertEqual(value['nav_bridge']['robot_type'], robot_type)
            expected = cfg.read_yaml(self.root / 'navigation/local_planner' / planner)
            if model == 'd1m':
                expected['localPlanner']['ros__parameters']['motionBlockNoPathTimeout'] = 8.0
            self.assertEqual(cfg.read_yaml(value['local_planner']['config']), expected)

    def test_d1m_system_matches_stored_deployment_with_local_identity(self):
        self.robot(robot_id='D1M-002')
        expected = cfg.read_yaml(ROOT / 'store/d1m_zenoh/system_d1m.yaml')
        expected['mqtt']['sn'] = 'D1M-002'
        expected['mission_execution_agent']['robot_id'] = 'D1M-002'
        expected['task_hub']['obstacle_snapshot_enabled'] = False
        expected['task_hub']['obstacle_photo_enabled'] = False
        expected['task_hub']['route_start_prepare_service_name'] = '/nav_bridge_node/ready'
        expected['task_hub']['trigger_service_timeout_seconds'] = 30.0
        expected['charge']['charge_command_timeout_sec'] = 180.0
        self.assertEqual(cfg.load_config('system'), expected)

    def test_d1m_navigation_matches_stored_sequences_and_inherits_defaults(self):
        self.robot()
        expected = cfg.read_yaml(ROOT / 'store/d1m_zenoh/navigate.yaml')
        # The historical nav key is unsupported. Keep the public control action.
        expected['bringup']['modes']['nav'].pop('start_control_action')
        expected['bringup']['modes']['nav']['control_action'] = 'acquire_control'
        # A comment does not override the inherited release_control action.
        expected['bringup']['modes']['add']['control_action'] = ''
        # Retain inherited Livox settings, but no D1M sequence starts its driver.
        expected['livox'] = cfg.read_yaml(self.root / 'navigate.yaml')['livox']
        expected['nav_bridge']['robot_type'] = 'd1_max'
        actual = cfg.load_config('navigation')
        # File selection has moved to bringup-managed snapshots; startup behavior
        # must still match the historical deployment.
        for section, field in [('nav_bridge', 'params_file'), ('slam', 'config'),
                               ('terrain', 'params_file'), ('local_planner', 'config'),
                               ('global_planner', 'params_path')]:
            expected[section][field] = actual[section][field]
        self.assertEqual(actual, expected)

    def test_relative_file_paths_are_independent_of_layer_and_dot_prefix(self):
        for name in ('gridmapper_local.yaml', './gridmapper_local.yaml'):
            for layer in ('navigate.yaml', 'models/d1m/navigate.yaml', 'robot.yaml'):
                with self.subTest(name=name, layer=layer):
                    override = {'terrain': {'params_file': name}}
                    if layer == 'robot.yaml':
                        self.robot(navigation=override)
                    else:
                        self.write(layer, override)
                        self.robot()
                    # A same-named file beside the YAML must never win.
                    self.write('models/d1m/gridmapper_local.yaml', {'wrong': True})
                    self.assertEqual(cfg.load_config('navigation')['terrain']['params_file'],
                                     self.package_file('gridmapper', 'gridmapper_local.yaml'))

    def test_all_relative_file_fields_use_the_owning_package(self):
        for module, specs in cfg.FILE_FIELDS.items():
            if module == 'services':
                continue
            for parts, (package, directory) in specs.items():
                with self.subTest(module=module, field=parts):
                    existing = next((self.prefix / 'share' / package / directory).iterdir())
                    value = {parts[0]: {parts[1]: './' + existing.name}}
                    resolved = cfg.resolve_files(value, module, 'test YAML')
                    self.assertEqual(resolved[parts[0]][parts[1]], str(existing))

    def test_relative_subdirectories_use_package_config_directory(self):
        path = Path(self.package_file('gridmapper', 'models/gridmapper_local.yaml'))
        path.parent.mkdir()
        path.write_text('{}\n')
        value = cfg.resolve_files({'terrain': {'params_file': 'models/gridmapper_local.yaml'}},
                                  'navigation', 'test YAML')
        self.assertEqual(value['terrain']['params_file'], str(path))

    def test_absolute_external_file_does_not_require_its_package(self):
        path = self.write('calibration/planner.yaml', {})
        with patch('ament_index_python.packages.get_package_share_directory',
                   side_effect=AssertionError('absolute paths must not look up packages')):
            value = cfg.resolve_files({'local_planner': {'config': str(path)}}, 'navigation', 'test YAML')
        self.assertEqual(value['local_planner']['config'], str(path))

    def test_missing_package_reports_actionable_error(self):
        from ament_index_python.packages import PackageNotFoundError
        with patch('ament_index_python.packages.get_package_share_directory',
                   side_effect=PackageNotFoundError('gridmapper')):
            with self.assertRaisesRegex(ValueError, 'gridmapper; source its workspace'):
                cfg.resolve_files({'terrain': {'params_file': 'gridmapper_local.yaml'}},
                                  'navigation', 'test YAML')

    def test_file_paths_survive_runtime_snapshot(self):
        path = self.write('calibration/planner.yaml', {})
        self.robot(navigation={'local_planner': {'config': str(path)}})
        value = cfg.load_config('navigation')
        snapshot = cfg.write_snapshot(value, self.temp.name)
        self.assertEqual(cfg.load_config('navigation', snapshot), value)
        self.assertNotIn('model', value)
        self.assertNotIn('robot_id', value)

    def test_file_names_resolve_but_legacy_global_selector_remains_plain(self):
        self.robot()
        value = cfg.load_config('navigation')
        self.assertEqual(cfg.read_yaml(value['slam']['config'])['laser_mapping']['ros__parameters']
                         ['initial_alignment']['position'], [0.0, 0.0, 0.0])
        self.assertEqual(value['global_planner']['params_file'], 'straight_local')

    def test_explicit_file_bypasses_robot_even_if_it_is_the_default_path(self):
        self.robot(model='does-not-exist')
        path = self.root / 'system.yaml'
        self.assertEqual(cfg.load_config('system', str(path)), cfg.read_yaml(path))

    def test_missing_explicit_file_fails(self):
        with self.assertRaises(FileNotFoundError):
            cfg.load_config('system', str(self.root / 'missing.yaml'))

    def test_unknown_model_and_path_traversal_fail(self):
        for model in ('absent', '../x30', '', 42):
            self.robot(model=model)
            with self.subTest(model=model), self.assertRaises(ValueError):
                cfg.load_config('navigation')

    def test_unknown_robot_key_and_wrong_module_type_fail(self):
        for extra in ({'navigaton': {}}, {'system': []}, {'services': None}, {'robot_id': ''}):
            self.robot(**extra)
            with self.subTest(extra=extra), self.assertRaises(ValueError):
                cfg.load_config('system')

    def test_non_mapping_yaml_fails(self):
        path = self.root / 'robot.yaml'
        path.write_text('false\n')
        with self.assertRaisesRegex(ValueError, 'mapping'):
            cfg.load_config('system')

    def test_missing_referenced_file_fails(self):
        self.robot(navigation={'terrain': {'params_file': './absent.yaml'}})
        with self.assertRaisesRegex(ValueError, 'referenced file does not exist'):
            cfg.load_config('navigation')

    def test_base_configuration_changes_are_inherited_without_copying(self):
        self.robot()
        base = cfg.read_yaml(self.root / 'system.yaml')
        base['mqtt']['port'] = 4567
        self.write('system.yaml', base)
        self.assertEqual(cfg.load_config('system')['mqtt']['port'], 4567)
        self.assertNotIn('port', cfg.read_yaml(self.root / 'robot.yaml'))

    def test_services_robot_mode_preserves_the_root_in_workers(self):
        self.robot(services={'service': {'environment': {'ROS_DOMAIN_ID': '198'}}})
        value = cfg.load_config('services')
        self.assertEqual(value['service']['environment']['INSPECTION_CONFIG_DIR'], str(self.root))
        self.assertEqual(value['service']['environment']['ROS_DOMAIN_ID'], '198')
        self.assertNotIn('navigate_config_path', value['navigation']['launch_args'])
        self.assertNotIn('system_config_path', value['inspection_system']['launch_args'])

    def test_submodule_public_model_robot_fields_merge_without_copying(self):
        self.robot(navigation={'local_planner': {'parameters': {
            'localPlanner': {'ros__parameters': {'motionBlockNoPathTimeout': 9.0}}}}})
        value = cfg.load_config('navigation')
        data = cfg.read_yaml(value['local_planner']['config'])
        public = cfg.read_yaml(self.root / 'navigation/local_planner/d1m.yaml')
        self.assertEqual(public['localPlanner']['ros__parameters']['motionBlockNoPathTimeout'], 3.0)
        public['localPlanner']['ros__parameters']['motionBlockNoPathTimeout'] = 9.0
        self.assertEqual(data, public)
        self.assertNotIn('parameters', value['local_planner'])
        # Editing an inherited field must appear without refreshing model/robot files.
        public['pathFollower']['ros__parameters']['lookAheadDis'] = 0.42
        self.write('navigation/local_planner/d1m.yaml', public)
        again = cfg.load_config('navigation')
        self.assertEqual(cfg.read_yaml(again['local_planner']['config']), public)

    def test_submodule_lists_false_zero_empty_and_null_are_preserved(self):
        self.write('navigation/gridmapper/gridmapper_local.yaml',
                   {'node': {'ros__parameters': {'enabled': True, 'limit': 3,
                      'names': ['a', 'b'], 'text': 'default', 'optional': 'value'}}})
        expected = {'node': {'ros__parameters': {'enabled': False, 'limit': 0,
                     'names': [], 'text': '', 'optional': None}}}
        self.robot(navigation={'terrain': {'parameters': expected}})
        value = cfg.load_config('navigation')
        self.assertEqual(cfg.read_yaml(value['terrain']['params_file']), expected)

    def test_explicit_root_beats_stale_environment_and_needs_no_packages(self):
        self.robot()
        with patch.dict(os.environ, INSPECTION_CONFIG_DIR='/nonexistent/stale-root'), \
                patch('ament_index_python.packages.get_package_share_directory',
                      side_effect=AssertionError('managed files must not look up child packages')):
            value = cfg.load_config('navigation', config_dir=self.root)
        self.assertEqual(cfg.read_yaml(value['slam']['config'])['laser_mapping']['ros__parameters']
                         ['initial_alignment']['position'], [0.0, 0.0, 0.0])

    def test_standalone_child_file_bypasses_model_and_robot_parameter_layers(self):
        complete = self.write('calibration/local_planner.yaml', {'only_external': True})
        self.robot(navigation={'local_planner': {'config': str(complete)}})
        value = cfg.load_config('navigation')
        self.assertEqual(value['local_planner']['config'], str(complete))
        self.assertEqual(cfg.read_yaml(value['local_planner']['config']), {'only_external': True})
        self.robot(navigation={'local_planner': {'config': str(complete), 'parameters': {'x': 1}}})
        with self.assertRaisesRegex(ValueError, 'standalone paths use complete files'):
            cfg.load_config('navigation')

    def test_explicit_bringup_file_bypasses_model_parameter_layers(self):
        self.robot()
        standalone = self.write('standalone.yaml', {'slam': {'config':
            '${INSPECTION_CONFIG_DIR}/navigation/faster_lio/airy.yaml'}})
        value = cfg.load_config('navigation', str(standalone))
        self.assertEqual(cfg.read_yaml(value['slam']['config']),
                         cfg.read_yaml(self.root / 'navigation/faster_lio/airy.yaml'))

    def test_managed_json_keeps_driver_format_and_replaces_device_lists(self):
        # Livox accepts JSON rather than a ROS parameter YAML.
        target = self.root / 'models/d1m/navigation/livox_ros_driver2/multi_MID360_config.json'
        target.parent.mkdir(parents=True)
        target.write_text('{"lidar_configs": []}')
        self.robot(navigation={'livox': {'user_config_path':
            '${INSPECTION_CONFIG_DIR}/navigation/livox_ros_driver2/multi_MID360_config.json'}})
        value = cfg.load_config('navigation')
        path = value['livox']['user_config_path']
        self.assertTrue(path.endswith('.json'))
        expected = cfg.read_parameter_file(self.root / 'navigation/livox_ros_driver2/multi_MID360_config.json')
        expected['lidar_configs'] = []
        self.assertEqual(cfg.read_parameter_file(path), expected)

    def test_snapshots_freeze_parameters_and_repeated_loads_reuse_files(self):
        self.robot()
        value = cfg.load_config('navigation')
        self.assertEqual(cfg.load_config('navigation'), value)
        snapshot = cfg.write_snapshot(value, self.temp.name)
        before = cfg.read_yaml(value['slam']['config'])
        self.write('models/d1m/navigation/faster_lio/airy.yaml',
                   {'laser_mapping': {'ros__parameters': {'initial_alignment': {'position': [1, 2, 3]}}}})
        self.assertEqual(cfg.load_config('navigation', snapshot), value)
        self.assertEqual(cfg.read_yaml(value['slam']['config']), before)
        self.assertNotEqual(cfg.load_config('navigation')['slam']['config'], value['slam']['config'])

    def test_gridmapper_global_diff_is_available_without_changing_local_navigation(self):
        self.robot()
        local = cfg.read_yaml(cfg.load_config('navigation')['terrain']['params_file'])
        self.assertEqual(local, cfg.read_yaml(self.root / 'navigation/gridmapper/gridmapper_local.yaml'))
        self.robot(navigation={'terrain': {'params_file':
            '${INSPECTION_CONFIG_DIR}/navigation/gridmapper/gridmapper_global.yaml'}})
        global_params = cfg.read_yaml(cfg.load_config('navigation')['terrain']['params_file'])
        self.assertEqual(global_params['gridmapper_node']['ros__parameters']['global_map_width'], 500.0)
        self.assertEqual(global_params['gridmapper_node']['ros__parameters']['global_map_height'], 500.0)

    def test_managed_missing_files_traversal_and_bad_parameter_types_fail(self):
        for name in ['missing.yaml', '../system.yaml', '/tmp/file.yaml']:
            self.robot(navigation={'slam': {'config': '${INSPECTION_CONFIG_DIR}/navigation/' + name}})
            with self.subTest(name=name), self.assertRaises((ValueError, FileNotFoundError)):
                cfg.load_config('navigation')
        self.robot(navigation={'slam': {'parameters': []}})
        with self.assertRaisesRegex(ValueError, 'parameters must be a mapping'):
            cfg.load_config('navigation')

    def test_robot_legacy_global_selector_overrides_managed_default(self):
        self.robot(navigation={'global_planner': {'params_file': 'straight_local'}})
        value = cfg.load_config('navigation')
        self.assertNotIn('params_path', value['global_planner'])
        self.assertEqual(value['global_planner']['params_file'], 'straight_local')

    def test_robot_bridge_type_switch_does_not_inherit_other_robot_parameters(self):
        self.robot(navigation={'nav_bridge': {'robot_type': 'cyvet'}})
        value = cfg.load_config('navigation')
        self.assertNotIn('params_file', value['nav_bridge'])

    def test_parameter_snapshot_recovers_from_a_truncated_runtime_file(self):
        self.robot(navigation={'local_planner': {'parameters': {
            'pathFollower': {'ros__parameters': {'lookAheadDis': 0.314159265}}}}})
        value = cfg.load_config('navigation')
        path = Path(value['local_planner']['config'])
        content = path.read_text()
        try:
            path.write_text('')
            self.assertEqual(cfg.load_config('navigation'), value)
            self.assertEqual(path.read_text(), content)
        finally:
            path.write_text(content)

    def test_d1m_services_matches_stored_deployment_without_editing_defaults(self):
        self.robot()
        expected = cfg.read_yaml(ROOT / 'store/d1m_zenoh/services.yaml')
        expected['navigation']['launch_args'].pop('navigate_config_path')
        expected['inspection_system']['launch_args'].pop('system_config_path')
        expected['service']['environment']['CYCLONEDDS_URI'] = ''
        expected['service']['environment']['ZENOH_CONFIG_OVERRIDE'] = 'queries_default_timeout=600000'
        expected['service']['environment']['INSPECTION_CONFIG_DIR'] = str(self.root)
        self.assertEqual(cfg.load_config('services'), expected)

    def test_services_explicit_launch_paths_use_bringup_config_directory(self):
        self.write('models/d1m/services.yaml',
                   {'navigation': {'launch_args': {'navigate_config_path': './navigate.yaml'}}})
        self.robot()
        value = cfg.load_config('services')
        self.assertEqual(value['navigation']['launch_args']['navigate_config_path'],
                         str(self.root / 'navigate.yaml'))

    def test_rendered_wrappers_use_robot_mode_and_valid_shell(self):
        self.robot()
        result = self.run_script('manage_inspection_services.sh', 'render')
        self.assertEqual(result.returncode, 0, result.stderr)
        output = Path(result.stdout.strip())
        self.addCleanup(shutil.rmtree, output)
        for unit in (output / 'systemd').glob('*.service'):
            self.assertIn('KillMode=mixed\n', unit.read_text())
        for name in ('run_navigation.sh', 'run_inspection_system.sh',
                     'run_inspection_hardware.sh'):
            path = output / 'wrappers' / name
            content = path.read_text()
            self.assertIn('export INSPECTION_CONFIG_DIR=', content)
            self.assertIn(str(self.root), content)
            self.assertNotIn('_config_path:=', content)
            self.assertIn('source /home/cat/Workspace/zenoh_ws/install/setup.bash', content)
            self.assertIn('export RMW_IMPLEMENTATION=rmw_zenoh_cpp', content)
            self.assertIn('export ROS_DOMAIN_ID=24', content)
            self.assertIn('export ZENOH_CONFIG_OVERRIDE=queries_default_timeout=600000', content)
            self.assertIn("export CYCLONEDDS_URI=''", content)
            check = subprocess.run(['bash', '-n', str(path)], capture_output=True, text=True)
            self.assertEqual(check.returncode, 0, check.stderr)

    def test_render_explicit_services_bypasses_robot(self):
        self.robot(model='absent')
        result = self.run_script('manage_inspection_services.sh', '--config',
                                 str(self.root / 'services.yaml'), 'render')
        self.assertEqual(result.returncode, 0, result.stderr)
        output = Path(result.stdout.strip())
        self.addCleanup(shutil.rmtree, output)
        content = (output / 'wrappers/run_navigation.sh').read_text()
        self.assertIn('navigate_config_path:=', content)
        self.assertNotIn('export INSPECTION_CONFIG_DIR=', content)

    def test_tmux_uses_merged_switches_and_shared_root(self):
        self.robot(system={'modules': {'gas': False, 'sensor_gimbal': False}})
        result = self.run_script('start_inspection_tmux.sh', '--dry-run')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn('[window] gas\n', result.stdout)
        self.assertNotIn('[window] sensor_gimbal\n', result.stdout)
        self.assertIn('[window] task_hub\n', result.stdout)
        self.assertIn('export INSPECTION_CONFIG_DIR=', result.stdout)
        self.assertNotIn('system_config_path:=', result.stdout)
        self.assertNotIn('enable_gimbal:=', result.stdout)

    def test_tmux_explicit_config_bypasses_robot(self):
        self.robot(model='absent')
        result = self.run_script('start_inspection_tmux.sh', '--config',
                                 str(self.root / 'system.yaml'), '--dry-run')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('system_config_path:=', result.stdout)

    def test_bad_robot_config_stops_both_shell_helpers(self):
        self.robot(model='absent')
        for script, args in [('start_inspection_tmux.sh', ['--dry-run']),
                             ('manage_inspection_services.sh', ['render'])]:
            result = self.run_script(script, *args)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn('unknown model', result.stderr)


if __name__ == '__main__':
    unittest.main()
