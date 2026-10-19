"""Inspect real launch actions and supervisor configuration without executing nodes."""
import importlib.util
import os
from pathlib import Path
import shutil
import sys
import tempfile
import unittest
from unittest.mock import patch

import yaml
from config_fixtures import package_environment, package_prefix
from launch import LaunchContext
from launch.actions import DeclareLaunchArgument, ExecuteProcess
from launch.utilities import perform_substitutions
from launch_ros.utilities import evaluate_parameters, normalize_parameters

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
import bringup_config as cfg


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, ROOT / path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


navigation = load('navigation_launch', 'launch/navigation.launch.py')
sensors = load('sensors_launch', 'launch/sensors.launch.py')
system = load('system_launch', 'launch/inspection_system.launch.py')
platform = load('platform_launch', 'launch/platform_bridge.launch.py')
supervisor = load('navigation_supervisor', 'scripts/navigation_supervisor.py')


def context_for(module, **overrides):
    context = LaunchContext()
    context.launch_configurations.update(overrides)
    for action in module.generate_launch_description().entities:
        if isinstance(action, DeclareLaunchArgument):
            action.execute(context)
    return context


class LaunchConfiguration(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='inspection launch ')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / 'config'
        shutil.copytree(ROOT / 'config', self.root, ignore=shutil.ignore_patterns('robot.yaml'))
        self.prefix = package_prefix(self.temp.name)
        self.env = patch.dict(os.environ, package_environment(self.prefix),
                              INSPECTION_CONFIG_DIR=str(self.root))
        self.env.start()
        self.addCleanup(self.env.stop)
        self.robot = {'model': 'd1m', 'robot_id': 'D1M-TEST',
                      'system': {'modules': {'sensor_gimbal': False, 'mission_execution_agent': False}}}
        self.write_robot()

    def write_robot(self):
        (self.root / 'robot.yaml').write_text(yaml.safe_dump(self.robot))

    def package_file(self, package, name):
        return str(self.prefix / 'share' / package / 'config' / name)

    def test_symlink_install_finds_robot_beside_source_config(self):
        share = Path(self.temp.name) / 'share'
        installed = share / 'config'
        installed.mkdir(parents=True)
        (installed / 'system.yaml').symlink_to(self.root / 'system.yaml')
        with patch.dict(os.environ, clear=True), \
                patch('ament_index_python.packages.get_package_share_directory', return_value=str(share)):
            self.assertEqual(cfg.default_config_dir(), self.root)
            self.assertEqual(cfg.load_config('system')['mqtt']['sn'], 'D1M-TEST')

    def test_normal_install_uses_installed_config_root(self):
        share = Path(self.temp.name) / 'share'
        installed = share / 'config'
        installed.mkdir(parents=True)
        (installed / 'system.yaml').write_text('{}\n')
        with patch.dict(os.environ, clear=True), \
                patch('ament_index_python.packages.get_package_share_directory', return_value=str(share)):
            self.assertEqual(cfg.default_config_dir(), installed)

    def test_service_mode_snapshot_contains_model_and_cli_values(self):
        context = context_for(navigation, local_planner_config='x30.yaml', terrain_use_gpu='false',
                              slam_prior_dir='/tmp/inspection-map', enable_livox='false',
                              global_patrol_loops='3')
        actions = navigation.launch_setup(context)
        process = next(action for action in actions if isinstance(action, ExecuteProcess))
        command = [perform_substitutions(context, part) for part in process.cmd]
        snapshot_path = command[command.index('--config') + 1]
        self.addCleanup(os.unlink, snapshot_path)
        config = supervisor.load_yaml(snapshot_path)
        self.assertEqual(config['nav_bridge']['robot_type'], 'd1_max')
        self.assertEqual(config['local_planner']['config'], self.package_file('local_planner', 'x30.yaml'))
        self.assertEqual(config['slam']['prior_dir'], '/tmp/inspection-map')
        self.assertIs(config['terrain']['use_gpu'], False)
        self.assertEqual(config['global_planner']['patrol_loops'], 3)
        self.assertIs(config['launch_module_switches']['livox'], False)
        node = object.__new__(supervisor.NavigationSupervisor)
        self.assertNotIn('livox', node.mode_sequence(config, 'nav'))
        layer = node.layer_config(config, ('terrain',))
        self.assertNotIn('launch_module_switches', layer)
        self.assertEqual(navigation.config_sequence(layer), ['terrain'])
        self.assertEqual(layer['bringup']['start_mode'], 'immediate')

    def test_cli_file_path_is_absolute_before_supervisor_snapshot(self):
        custom = Path(self.temp.name) / 'custom.yaml'
        custom.write_text('{}\n')
        context = context_for(navigation, terrain_params_file=str(custom))
        config = navigation.apply_launch_overrides(context, cfg.load_config('navigation'))
        self.assertEqual(config['terrain']['params_file'], str(custom))

    def test_cli_relative_paths_use_the_same_package_directories_as_yaml(self):
        context = context_for(navigation, terrain_params_file='./gridmapper_local.yaml',
                              local_planner_config='./d1m.yaml',
                              global_params_path='straight_local.yaml',
                              livox_user_config_path='MID360_config.json')
        config = navigation.apply_launch_overrides(context, cfg.load_config('navigation'))
        self.assertEqual(config['terrain']['params_file'], self.package_file('gridmapper', 'gridmapper_local.yaml'))
        self.assertEqual(config['local_planner']['config'], self.package_file('local_planner', 'd1m.yaml'))
        self.assertEqual(config['livox']['user_config_path'], self.package_file('livox_ros_driver2', 'MID360_config.json'))
        self.assertEqual(config['global_planner']['params_path'],
                         str(self.prefix / 'share/multi_map_nav/params/straight_local.yaml'))

    def test_runtime_request_resolves_relative_files_before_transition(self):
        from rclpy.parameter import Parameter
        from rcl_interfaces.srv import SetParameters
        config = cfg.load_config('navigation')
        node = object.__new__(supervisor.NavigationSupervisor)
        node.active_config = None
        node.base_config = config
        node.current_state = supervisor.STOPPED
        node.map_context = {}
        node.allowed_paths = supervisor.flatten_leaves(config)
        request = SetParameters.Request(parameters=[
            Parameter('local_planner.config', value='./x30.yaml').to_parameter_msg()])
        value, provided, results = node.resolve_request(request)
        self.assertEqual(value['local_planner']['config'], self.package_file('local_planner', 'x30.yaml'))
        self.assertEqual(provided, {'local_planner.config'})
        self.assertTrue(all(result.successful for result in results))

    def test_d1m_sequences_and_control_actions_pass_supervisor_validation(self):
        config = cfg.load_config('navigation')
        node = object.__new__(supervisor.NavigationSupervisor)
        node.module_names = supervisor.SUPPORTED_MODULES
        node.validate_mode_sequences(config)
        for mode in ('manual', 'nav', 'add'):
            with self.subTest(mode=mode):
                self.assertNotIn('livox', node.mode_sequence(config, mode))
                self.assertEqual(node.startup_control_action(config, mode), 'acquire_control')
        self.assertEqual(node.control_action(config, 'nav'), 'acquire_control')
        self.assertEqual(node.control_action(config, 'add'), '')
        self.assertEqual(node.run_control_action(config, 'add'), (True, 'no control action configured'))
        self.assertEqual(node.layer_sequence(config, 'add', supervisor.MANUAL_LOCALIZED, 'base'),
                         ('nav_bridge', 'acquire_control', 'release_control', 'slam'))
        self.assertEqual(node.layer_sequence(config, 'add', supervisor.MANUAL_LOCALIZED, 'navigation'),
                         ('terrain', 'local_planner', 'global_planner'))

    def test_cli_legacy_global_selector_replaces_managed_path(self):
        context = context_for(navigation, global_params_file='new_local')
        value = navigation.apply_launch_overrides(context, cfg.load_config('navigation'))
        self.assertEqual(value['global_planner']['params_file'], 'new_local')
        self.assertNotIn('params_path', value['global_planner'])

    def test_runtime_selectors_do_not_inherit_incompatible_files(self):
        from rclpy.parameter import Parameter
        from rcl_interfaces.srv import SetParameters
        node = object.__new__(supervisor.NavigationSupervisor)
        node.base_config = cfg.load_config('navigation')
        node.active_config = None
        node.current_state = supervisor.STOPPED
        node.map_context = {}
        node.allowed_paths = supervisor.flatten_leaves(node.base_config)
        request = SetParameters.Request(parameters=[
            Parameter('global_planner.params_file', value='new_local').to_parameter_msg(),
            Parameter('nav_bridge.robot_type', value='cyvet').to_parameter_msg()])
        value, _, results = node.resolve_request(request)
        self.assertTrue(all(result.successful for result in results))
        self.assertNotIn('params_path', value['global_planner'])
        self.assertNotIn('params_file', value['nav_bridge'])

    def test_cli_model_file_uses_layers_and_switching_robot_type_uses_child_default(self):
        context = context_for(navigation, terrain_params_file=
            '${INSPECTION_CONFIG_DIR}/navigation/gridmapper/gridmapper_global.yaml',
            nav_bridge_robot_type='cyvet')
        value = navigation.apply_launch_overrides(context, cfg.load_config('navigation'))
        self.assertEqual(cfg.read_yaml(value['terrain']['params_file'])['gridmapper_node']
                         ['ros__parameters']['global_map_width'], 500.0)
        self.assertNotIn('params_file', value['nav_bridge'])

    def test_immediate_mode_forwards_resolved_cli_file_path(self):
        custom = Path(self.temp.name) / 'custom.yaml'
        custom.write_text('{}\n')
        self.robot['navigation'] = {'bringup': {'start_mode': 'immediate',
                                                'wait_for_readiness': False,
                                                'start_delay_seconds': 0.0}}
        self.write_robot()
        context = context_for(navigation, slam_config=str(custom))
        captured = {}

        def include(package, filename, enabled, launch_arguments=None):
            captured[package] = launch_arguments or {}
            return DeclareLaunchArgument('mock_' + package, default_value='')

        with patch.object(navigation, 'include_package_launch', side_effect=include):
            navigation.launch_setup(context)
        self.assertEqual(Path(captured['faster_lio']['config']).resolve(), custom)
        self.assertTrue(Path(captured['faster_lio']['config']).is_absolute())

    def test_child_configuration_arguments_are_forwarded(self):
        external = self.root / 'external.yaml'
        external.write_text('{}\n')
        config = cfg.load_config('navigation')
        for module, field in [('nav_bridge', 'params_file'), ('terrain', 'params_file'),
                              ('global_planner', 'params_path'), ('livox', 'user_config_path')]:
            config[module][field] = str(external)
        config['livox']['launch_file'] = 'msg_MID360_launch.py'
        config['bringup']['wait_for_readiness'] = False
        config['bringup']['start_delay_seconds'] = 0.0
        captured = {}

        def include(package, filename, enabled, launch_arguments=None):
            captured[package] = (filename, launch_arguments or {})
            return DeclareLaunchArgument('mock_' + package, default_value='')

        context = context_for(navigation)
        with patch.object(navigation, 'include_package_launch', side_effect=include):
            navigation.build_navigation_actions(context, config, use_launch_overrides=False)
        self.assertEqual(captured['nav_bridge'][1]['robot_type'], 'd1_max')
        self.assertEqual(captured['nav_bridge'][1]['params_file'], str(external))
        self.assertEqual(captured['faster_lio'][1]['config'], config['slam']['config'])
        self.assertEqual(captured['gridmapper'][1]['params_file'], str(external))
        self.assertEqual(captured['local_planner'][1]['config'], config['local_planner']['config'])
        self.assertEqual(captured['multi_map_nav'][1]['params_file'], 'straight_local')
        self.assertEqual(captured['multi_map_nav'][1]['params_path'], str(external))
        self.assertEqual(captured['livox_ros_driver2'][0], 'msg_MID360_launch.py')
        self.assertEqual(captured['livox_ros_driver2'][1]['user_config_path'], str(external))

    def test_legacy_config_does_not_send_new_optional_arguments(self):
        config = cfg.read_yaml(self.root / 'navigate.yaml')
        for section, field in [('nav_bridge', 'params_file'), ('slam', 'config'),
                               ('terrain', 'params_file'), ('local_planner', 'config'),
                               ('global_planner', 'params_path')]:
            config[section].pop(field)
        legacy = self.root / 'legacy.yaml'
        legacy.write_text(yaml.safe_dump(config))
        config = cfg.load_config('navigation', str(legacy))
        context = context_for(navigation)
        for section in ('nav_bridge', 'slam', 'terrain', 'local_planner', 'global_planner', 'livox'):
            self.assertEqual(navigation.optional_module_arguments(context, config, section, False), {})

    def test_sensor_file_selections_are_forwarded(self):
        custom = self.root / 'gas.yaml'
        custom.write_text('{}\n')
        self.robot['system']['gas'] = {'params_file': str(custom)}
        self.robot['system']['sensor_rs485'] = {'params_file': str(custom)}
        self.write_robot()
        context = context_for(sensors)
        actions = sensors.launch_setup(context)
        arguments = [dict(action.launch_arguments) for action in actions]
        self.assertIn({'gas_params_file': str(custom)}, arguments)
        self.assertIn({'params_file': str(custom)}, arguments)

    def test_gimbal_cli_relative_path_uses_sensor_gimbal_config_directory(self):
        context = context_for(system, enable_sensor_gimbal='true', gimbal_params_file='./stub_params.yaml')
        with patch.object(system, 'get_package_share_directory',
                          side_effect=lambda name: str(self.prefix / 'share' / name)):
            actions = system.launch_setup(context)
        arguments = [dict(action.launch_arguments) for action in actions
                     if hasattr(action, 'launch_arguments')]
        gimbal = next(value for value in arguments if 'gimbal_params_file' in value)
        self.assertEqual(gimbal['gimbal_params_file'], self.package_file('sensor_gimbal', 'stub_params.yaml'))

    def test_system_and_standalone_platform_receive_same_identity(self):
        for module in (system, platform):
            context = context_for(module)
            # Construct node descriptions only; do not execute any actions.
            with patch.object(system, 'get_package_share_directory', return_value=str(self.root)), \
                    patch.object(module, 'Node', wraps=module.Node) as factory:
                module.launch_setup(context)
            node = next(call.kwargs for call in factory.call_args_list
                        if call.kwargs.get('name') == 'platform_mqtt_bridge_node')
            params = evaluate_parameters(context, normalize_parameters(node['parameters']))
            self.assertEqual(params[0]['sn'], 'D1M-TEST')

    def test_only_d1m_extends_charge_command_timeout(self):
        for model, expected in [('x30', {}), ('d1m', {'charge_command_timeout_sec': '180.0'})]:
            with self.subTest(model=model):
                self.robot['model'] = model
                self.write_robot()
                context = context_for(system)
                with patch.object(system, 'get_package_share_directory', return_value=str(self.root)), \
                        patch.object(system, 'include_package_launch', wraps=system.include_package_launch) as include:
                    system.launch_setup(context)
                call = next(call for call in include.call_args_list
                            if call.args[0] == 'inspection_charge_executor')
                self.assertEqual(call.args[3], expected)

    def test_all_tmux_generated_arguments_are_declared(self):
        output = __import__('subprocess').run(
            ['bash', str(ROOT / 'scripts/start_inspection_tmux.sh'), '--dry-run'],
            capture_output=True, text=True, check=True).stdout
        mapping = {'inspection_system.launch.py': system, 'sensors.launch.py': sensors}
        import shlex
        for line in output.splitlines():
            if 'exec ros2 launch' not in line:
                continue
            command = shlex.split(line.split('exec ros2 launch ', 1)[1])
            module = mapping[command[1]]
            declared = {action.name for action in module.generate_launch_description().entities
                        if isinstance(action, DeclareLaunchArgument)}
            supplied = {part.split(':=', 1)[0] for part in command[2:] if ':=' in part}
            self.assertLessEqual(supplied, declared)


if __name__ == '__main__':
    unittest.main()
