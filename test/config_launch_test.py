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
modules = load('modules_launch', 'launch/inspection_modules.launch.py')
cooperation = load('cooperation_launch', 'launch/cooperation.launch.py')
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
                      'system': {'modules': {'sensor_gimbal': False}}}
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
        self.assertEqual(config['nav_bridge']['robot_type'], 'd1m')
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
        context = context_for(navigation, terrain_config=str(custom))
        config = navigation.apply_launch_overrides(context, cfg.load_config('navigation'))
        self.assertEqual(config['terrain']['config'], str(custom))

    def test_global_startup_budget_survives_supervisor_layering_and_robot_override(self):
        for mode in ('periodic', 'event_driven'):
            for override, expected in ((None, 60.0), (90.0, 90.0)):
                with self.subTest(mode=mode, override=override):
                    self.robot['navigation'] = {'global_planner': {
                        'parameters': {'multi_map_nav_node': {'ros__parameters': {
                            'global_replanning_mode': mode}}}}}
                    if override is not None:
                        self.robot['navigation']['global_planner']['readiness'] = {
                            'timeout_seconds': override}
                    self.write_robot()
                    config = cfg.load_config('navigation')
                    node = object.__new__(supervisor.NavigationSupervisor)
                    layer = node.layer_config(config, ('global_planner',))
                    context = context_for(navigation)
                    action = navigation.module_readiness_action_with_overrides(
                        context, layer, 'global_planner', [], 10.0, False)
                    command = [perform_substitutions(context, part) for part in action.cmd]
                    self.assertEqual(command[command.index('--timeout') + 1], str(expected))
                    self.assertIn('/multi_map_nav/health', command)
                    self.assertEqual(config['local_planner']['readiness']['timeout_seconds'], 10.0)
                    self.assertEqual(config['bringup']['wait_timeout_seconds'], 10.0)

    def test_cli_relative_paths_use_the_same_package_directories_as_yaml(self):
        context = context_for(navigation, terrain_config='./gridmapper_local.yaml',
                              local_planner_config='./d1m.yaml',
                              global_config='straight_local.yaml',
                              livox_config='MID360_config.json')
        config = navigation.apply_launch_overrides(context, cfg.load_config('navigation'))
        self.assertEqual(config['terrain']['config'], self.package_file('gridmapper', 'gridmapper_local.yaml'))
        self.assertEqual(config['local_planner']['config'], self.package_file('local_planner', 'd1m.yaml'))
        self.assertEqual(config['livox']['config'], self.package_file('livox_ros_driver2', 'MID360_config.json'))
        self.assertEqual(config['global_planner']['config'],
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
            Parameter('local_planner.config', value='./x30.yaml').to_parameter_msg(),
            Parameter('terrain.config', value='./gridmapper_local.yaml').to_parameter_msg()])
        value, provided, results = node.resolve_request(request)
        self.assertEqual(value['local_planner']['config'], self.package_file('local_planner', 'x30.yaml'))
        self.assertEqual(value['terrain']['config'], self.package_file('gridmapper', 'gridmapper_local.yaml'))
        self.assertEqual(provided, {'local_planner.config', 'terrain.config'})
        self.assertTrue(all(result.successful for result in results))

    def test_d1m_sequences_and_control_actions_pass_supervisor_validation(self):
        config = cfg.load_config('navigation')
        node = object.__new__(supervisor.NavigationSupervisor)
        node.module_names = supervisor.SUPPORTED_MODULES
        node.validate_mode_sequences(config)
        for mode in ('manual', 'nav', 'add'):
            with self.subTest(mode=mode):
                self.assertNotIn('livox', node.mode_sequence(config, mode))
                sequence = node.mode_sequence(config, mode)
                self.assertEqual(sequence[sequence.index('slam') + 1], 'gnss')
                self.assertEqual(node.startup_control_action(config, mode), 'acquire_control')
        self.assertEqual(node.control_action(config, 'nav'), 'acquire_control')
        self.assertEqual(node.control_action(config, 'add'), '')
        self.assertEqual(node.run_control_action(config, 'add'), (True, 'no control action configured'))
        self.assertEqual(node.layer_sequence(config, 'add', supervisor.MANUAL_LOCALIZED, 'base'),
                         ('nav_bridge', 'acquire_control', 'release_control', 'slam', 'gnss'))
        self.assertEqual(node.layer_sequence(config, 'add', supervisor.MANUAL_LOCALIZED, 'navigation'),
                         ('terrain', 'local_planner', 'global_planner'))

    def test_gnss_two_parameter_layers_and_model_scope(self):
        self.robot['navigation'] = {'gnss': {
            'parameters': {'gnss_device': {'ros__parameters': {'baud': 230400}}},
            'ntrip_parameters': {'ntrip_client': {'ros__parameters': {'username': 'test-user'}}}}}
        self.write_robot()
        config = cfg.load_config('navigation')
        receiver = cfg.read_yaml(config['gnss']['config'])['gnss_device']['ros__parameters']
        ntrip = cfg.read_yaml(config['gnss']['ntrip_config'])['ntrip_client']['ros__parameters']
        self.assertEqual(receiver['baud'], 230400)
        self.assertEqual(receiver['port'], '/dev/wheeltec_gnss')
        self.assertEqual(ntrip['username'], 'test-user')
        self.assertEqual(ntrip['rtk_baud'], 115200)
        self.assertEqual(ntrip['fix_topic'], receiver['fix_topic'])
        self.assertNotIn('parameters', config['gnss'])
        self.assertNotIn('ntrip_parameters', config['gnss'])
        node = object.__new__(supervisor.NavigationSupervisor)
        node.module_names = supervisor.SUPPORTED_MODULES
        self.assertNotIn('gnss', node.mode_sequence(config, 'manual', supervisor.MANUAL_BRIDGE_ONLY))
        self.robot.pop('navigation')
        for model in ('x30', 'cyvet'):
            self.robot['model'] = model
            self.write_robot()
            config = cfg.load_config('navigation')
            for mode in ('manual', 'nav', 'add'):
                self.assertNotIn('gnss', node.mode_sequence(config, mode))

    def test_tank_geometry_and_differential_profile_layers(self):
        profiles = {}
        for model in ('cyvet', 'tank', 'r750'):
            self.robot = {'model': model, 'robot_id': model.upper() + '-A'}
            self.write_robot()
            config = cfg.load_config('navigation')
            profiles[model] = {section: cfg.read_yaml(config[section]['config'])
                               for section in ('slam', 'terrain', 'local_planner', 'global_planner')}
        preprocess = profiles['tank']['slam']['laser_mapping']['ros__parameters']['preprocess']
        self.assertEqual(preprocess['self_box_half_size'], [0.55, 0.35, 0.5])
        self.assertEqual(preprocess['self_box_offset'], [-0.45, 0.0, 0.0])
        cyvet_preprocess = profiles['cyvet']['slam']['laser_mapping']['ros__parameters']['preprocess']
        self.assertEqual(cyvet_preprocess['self_box_offset'], [0.0, 0.0, 0.0])
        motion = profiles['tank']['local_planner']['/**']['ros__parameters']
        self.assertEqual(motion['sensorOffsetX'], 0.45)
        self.assertEqual(motion['sensorOffsetY'], 0.0)
        local = profiles['tank']['local_planner']['localPlanner']['ros__parameters']
        self.assertEqual(local['vehicleLength'], 1.10)
        self.assertEqual(local['vehicleWidth'], 0.70)
        self.assertFalse(local['gaitSwitchEn'])
        self.assertEqual(local['correspondencesFile'], 'correspondences_tank.txt')
        follower = profiles['tank']['local_planner']['pathFollower']['ros__parameters']
        self.assertEqual(follower['tracking_mode'], 'differential')
        self.assertFalse(follower['twoWayDrive'])
        global_params = profiles['tank']['global_planner']
        for section in ('global_costmap', 'local_costmap'):
            footprint = yaml.safe_load(global_params[section][section]['ros__parameters']['footprint'])
            expected = [[0.10, 0.35], [0.10, -0.35], [-1.00, -0.35], [-1.00, 0.35]]
            self.assertEqual(footprint, expected)
            self.assertAlmostEqual(max(p[0] for p in footprint) - min(p[0] for p in footprint),
                                   local['vehicleLength'])
            self.assertAlmostEqual(max(p[1] for p in footprint) - min(p[1] for p in footprint),
                                   local['vehicleWidth'])
        r750_local = profiles['r750']['local_planner']['localPlanner']['ros__parameters']
        self.assertEqual(r750_local['correspondencesFile'], 'correspondences_r750.txt')
        for section in ('slam', 'terrain', 'local_planner', 'global_planner'):
            r750_section = profiles['r750'][section]
            tank_section = profiles['tank'][section]
            if section == 'local_planner':
                r750_section['localPlanner']['ros__parameters']['correspondencesFile'] = 'correspondences_tank.txt'
            self.assertEqual(r750_section, tank_section)

    def test_tank_bridge_parameters_and_service_snapshot(self):
        for model in ('tank', 'r750'):
            with self.subTest(model=model):
                overrides = {'host_ip': '192.168.1.20', 'host_port': 10051} if model == 'tank' \
                    else {'serial_port': '/dev/ttyACM1'}
                self.robot = {'model': model, 'robot_id': model.upper() + '-A', 'navigation': {
                    'nav_bridge': {'parameters': {'nav_bridge_node': {'ros__parameters': {
                        **overrides, 'enable_motion_output': False}}}}}}
                self.write_robot()
                config = cfg.load_config('navigation')
                self.assertEqual(config['nav_bridge']['robot_type'], model)
                self.assertFalse(config['nav_bridge']['readiness']['charge_precheck'])
                parameters = cfg.read_yaml(config['nav_bridge']['config'])['nav_bridge_node']['ros__parameters']
                for key, value in overrides.items():
                    self.assertEqual(parameters[key], value)
                self.assertFalse(parameters['enable_motion_output'])
                if model == 'tank':
                    self.assertEqual(parameters['max_linear_speed'], 1.2)
                else:
                    self.assertEqual(parameters['serial_baud'], 230400)
                    self.assertEqual(parameters['max_linear_speed'], 0.7)
                    self.assertEqual(parameters['telemetry_timeout_ms'], 1000)
                node = object.__new__(supervisor.NavigationSupervisor)
                snapshot = node.layer_config(config, ('nav_bridge', 'acquire_control'))
                self.assertEqual(cfg.resolve_files(snapshot, 'navigation', 'test')['nav_bridge'], config['nav_bridge'])
                captured = {}
                def include(package, filename, enabled, launch_arguments=None):
                    captured[package] = launch_arguments or {}
                    return DeclareLaunchArgument('mock_' + package, default_value='')
                config['bringup']['sequence'] = ['nav_bridge', 'acquire_control']
                with patch.object(navigation, 'include_package_launch', side_effect=include):
                    navigation.build_navigation_actions(context_for(navigation), config, use_launch_overrides=False)
                self.assertEqual(captured['nav_bridge']['robot_type'], model)
                self.assertEqual(captured['nav_bridge']['params_file'], config['nav_bridge']['config'])

    def test_cli_global_config_replaces_managed_path(self):
        context = context_for(navigation, global_config='straight_local.yaml')
        value = navigation.apply_launch_overrides(context, cfg.load_config('navigation'))
        self.assertEqual(value['global_planner']['config'],
                         str(self.prefix / 'share/multi_map_nav/params/straight_local.yaml'))
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
            Parameter('global_planner.config', value='straight_local.yaml').to_parameter_msg(),
            Parameter('nav_bridge.robot_type', value='cyvet').to_parameter_msg()])
        value, _, results = node.resolve_request(request)
        self.assertTrue(all(result.successful for result in results))
        self.assertNotIn('params_path', value['global_planner'])
        self.assertEqual(value['global_planner']['config'],
                         str(self.prefix / 'share/multi_map_nav/params/straight_local.yaml'))
        self.assertNotIn('config', value['nav_bridge'])

    def test_cli_model_file_uses_layers_and_switching_robot_type_uses_child_default(self):
        context = context_for(navigation, terrain_config=
            '${INSPECTION_CONFIG_DIR}/navigation/param_default_gridmapper_global.yaml',
            nav_bridge_robot_type='cyvet')
        value = navigation.apply_launch_overrides(context, cfg.load_config('navigation'))
        self.assertEqual(cfg.read_yaml(value['terrain']['config'])['gridmapper_node']
                         ['ros__parameters']['global_map_width'], 500.0)
        self.assertNotIn('config', value['nav_bridge'])

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
        for module, field in [('nav_bridge', 'config'), ('terrain', 'config'),
                              ('global_planner', 'config'), ('livox', 'config')]:
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
        self.assertEqual(captured['nav_bridge'][1]['robot_type'], 'd1m')
        self.assertEqual(captured['nav_bridge'][1]['params_file'], str(external))
        self.assertEqual(captured['faster_lio'][1]['config'], config['slam']['config'])
        self.assertEqual(captured['gnss_driver'][1], {
            'config': config['gnss']['config'], 'ntrip_config': config['gnss']['ntrip_config'],
            'device': 'g90', 'ntrip': 'true', 'export_polyline': 'false'})
        self.assertEqual(captured['gridmapper'][1]['params_file'], str(external))
        self.assertEqual(captured['local_planner'][1]['config'], config['local_planner']['config'])
        self.assertNotIn('params_file', captured['multi_map_nav'][1])
        self.assertEqual(captured['multi_map_nav'][1]['params_path'], str(external))
        self.assertEqual(captured['livox_ros_driver2'][0], 'msg_MID360_launch.py')
        self.assertEqual(captured['livox_ros_driver2'][1]['user_config_path'], str(external))
        self.assertNotIn('model', captured['livox_ros_driver2'][1])

    def test_legacy_config_does_not_send_new_optional_arguments(self):
        config = cfg.read_yaml(self.root / 'navigate.yaml')
        for section, field in [('nav_bridge', 'config'), ('slam', 'config'),
                               ('terrain', 'config'), ('local_planner', 'config'),
                               ('global_planner', 'config'), ('livox', 'config')]:
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

    def test_cooperation_modes_select_exactly_one_stack(self):
        from launch.actions import GroupAction
        old = {'mission_execution_agent', 'fleet_leader_election', 'capability_mission_planner'}
        new = {'swarm_consensus', 'swarm_execution_agent', 'swarm_mqtt_transport',
               'swarm_mission_coordinator', 'swarm_mission_planner'}
        for mode, expected in [('disabled', set()), ('coordination', old), ('swarm', new)]:
            with self.subTest(mode=mode):
                self.robot['system']['cooperation'] = {'mode': mode}
                self.write_robot()
                context = context_for(system)
                with patch.object(system, 'get_package_share_directory', return_value=str(self.root)), \
                        patch.object(system, 'Node', wraps=system.Node) as factory:
                    actions = system.launch_setup(context)
                def flatten(items):
                    for item in items:
                        if isinstance(item, GroupAction):
                            yield from flatten(item.get_sub_entities())
                        else:
                            yield item
                from launch_ros.actions import Node
                nodes = [item for item in flatten(actions) if isinstance(item, Node)]
                packages = {item.node_package for item in nodes}
                self.assertEqual(packages & (old | new), expected)
                bridge = next(call.kwargs for call in factory.call_args_list
                              if call.kwargs.get('name') == 'platform_mqtt_bridge_node')
                params = evaluate_parameters(context, normalize_parameters(bridge['parameters']))[0]
                self.assertEqual(params['enable_mission_execution_agent'], mode == 'coordination')

    def test_all_launch_entries_share_merged_device_description(self):
        import json
        from device_description import device_status
        from launch_ros.actions import Node
        from swarm_mission_coordinator import launching

        self.robot['system']['device_status'] = {
            'capabilities': ['gimbal.vis'], 'nominal_speed_mps': 1.7,
            'Battery_RC': 30.5, 'Battery_RC_unit': 'Ah',
            'subDevices': [{'identifier': 'gimbal', 'subDevices': [{'identifier': 'vis'}]}]}
        snapshots = set()

        def parameters(factory, context):
            return {call.kwargs['executable']: evaluate_parameters(
                context, normalize_parameters(call.kwargs['parameters']))[0]
                for call in factory.call_args_list if 'parameters' in call.kwargs}

        for mode in ('disabled', 'coordination', 'swarm'):
            with self.subTest(mode=mode):
                self.robot['system']['cooperation'] = {'mode': mode}
                self.write_robot()
                expected = device_status(cfg.load_config('system'))
                context = context_for(system)
                with patch.object(system, 'get_package_share_directory', return_value=str(self.root)), \
                        patch.object(system, 'Node', wraps=Node) as factory:
                    system.launch_setup(context)
                values = parameters(factory, context)
                path = values['platform_mqtt_bridge_node']['device_status_config_path']
                snapshots.add(path)
                self.assertEqual(json.loads(Path(path).read_text()), expected)
                self.assertEqual(values['task_hub_node']['inspection_report_device_status_path'], path)

                context = context_for(platform)
                with patch.object(platform, 'Node', wraps=Node) as factory:
                    platform.launch_setup(context)
                values = parameters(factory, context)
                self.assertEqual(values['platform_mqtt_bridge_node']['device_status_config_path'], path)

                context = context_for(cooperation)
                with patch('launch_ros.actions.Node', wraps=Node) as old_factory, \
                        patch.object(launching, 'Node', wraps=Node) as new_factory:
                    cooperation.launch_setup(context)
                if mode == 'coordination':
                    values = parameters(old_factory, context)
                    agent = values['mission_execution_agent_node']
                    self.assertEqual(agent['device_status_path'], path)
                    self.assertEqual(agent['robot_id'], 'D1M-TEST')
                elif mode == 'swarm':
                    agent = parameters(new_factory, context)['swarm_execution_agent_node']
                    self.assertEqual(list(agent['capabilities']), ['device', 'gimbal.vis'])
                    self.assertEqual(agent['nominal_speed'], 1.7)
                else:
                    self.assertFalse(old_factory.called or new_factory.called)
        self.assertEqual(len(snapshots), 1)

    def test_removed_cooperation_switches_and_unknown_modes_are_rejected(self):
        for change in ({'modules': {'mission_execution_agent': False}},
                       {'modules': {'fleet_leader_election': True}},
                       {'mission_execution_agent': {}}, {'fleet_leader_election': {}},
                       {'cooperation': {'mode': 'legacy'}},
                       {'cooperation': {'device_status_path': '/old/device_status.json'}},
                       {'cooperation': {'mode': ['swarm']}},
                       {'cooperation': {'mode': 'swarm', 'swarm': {'agent': None}}},
                       {'cooperation': {'coordination': {'election': []}}},
                       {'cooperation': {'mode': 'swarm', 'config_path': '/tmp/old.yaml'}},
                       {'cooperation': {'mode': 'swarm', 'swarm': {'agent': {'allow_motion': True}}}}):
            self.robot['system'] = change
            self.write_robot()
            with self.assertRaises(ValueError):
                system.launch_setup(context_for(system))
        names = {a.name for a in system.generate_launch_description().entities
                 if isinstance(a, DeclareLaunchArgument)}
        self.assertFalse(names & {'cooperation_mode', 'swarm_config_path', 'enable_mission_execution_agent'})

    def test_gimbal_cli_relative_path_uses_sensor_gimbal_config_directory(self):
        context = context_for(system, enable_sensor_gimbal='true', gimbal_params_file='./stub_params.yaml')
        with patch.object(system, 'get_package_share_directory',
                          side_effect=lambda name: str(self.prefix / 'share' / name)):
            actions = system.launch_setup(context)
        arguments = [dict(action.launch_arguments) for action in actions
                     if hasattr(action, 'launch_arguments')]
        gimbal = next(value for value in arguments if 'gimbal_params_file' in value)
        self.assertEqual(gimbal['gimbal_params_file'], self.package_file('sensor_gimbal', 'stub_params.yaml'))

    def test_gimbal_pose_parameters_are_separate_and_support_config_and_cli(self):
        self.robot['system']['task_hub'] = {
            'gimbal_odometry_topic': '/horizon_custom',
            'gimbal_odometry_timeout_seconds': 2.5,
            'gimbal_map_relations_file': '/tmp/maps/map_relations.csv',
        }
        self.write_robot()
        for overrides, topic, timeout in [({}, '/horizon_custom', 2.5),
                ({'gimbal_odometry_topic': '/horizon_cli',
                  'gimbal_odometry_timeout_seconds': '0.75'}, '/horizon_cli', .75)]:
            context = context_for(system, **overrides)
            with patch.object(system, 'get_package_share_directory', return_value=str(self.root)), \
                    patch.object(system, 'Node', wraps=system.Node) as factory:
                system.launch_setup(context)
            node = next(call.kwargs for call in factory.call_args_list
                        if call.kwargs.get('name') == 'task_hub_node')
            params = evaluate_parameters(context, normalize_parameters(node['parameters']))[0]
            self.assertEqual(params['gimbal_odometry_topic'], topic)
            self.assertEqual(params['gimbal_odometry_timeout_seconds'], timeout)
            self.assertEqual(params['gimbal_map_relations_file'], '/tmp/maps/map_relations.csv')
            self.assertEqual(params['odometry_topic'], '/odometry_multi_maps')

    def test_obstacle_switches_default_off_and_are_forwarded_as_bool(self):
        for enabled, photo in [(None, None), (False, False), (False, True),
                               (True, False), (True, True)]:
            self.robot['system']['task_hub'] = {} if enabled is None else {
                'obstacle_snapshot_enabled': enabled, 'obstacle_photo_enabled': photo}
            self.write_robot()
            context = context_for(system)
            with patch.object(system, 'get_package_share_directory', return_value=str(self.root)), \
                    patch.object(system, 'Node', wraps=system.Node) as factory:
                system.launch_setup(context)
            node = next(call.kwargs for call in factory.call_args_list
                        if call.kwargs.get('name') == 'task_hub_node')
            params = evaluate_parameters(context, normalize_parameters(node['parameters']))
            self.assertIs(params[0]['obstacle_snapshot_enabled'], bool(enabled))
            self.assertIs(params[0]['obstacle_photo_enabled'], bool(photo))

    def test_obstacle_switch_reaches_planner_and_survives_supervisor_snapshot(self):
        for enabled in (None, False, True):
            self.robot['system']['task_hub'] = {} if enabled is None else {
                'obstacle_snapshot_enabled': enabled}
            self.write_robot()
            context = context_for(navigation)
            actions = navigation.launch_setup(context)
            process = next(a for a in actions if isinstance(a, ExecuteProcess))
            command = [perform_substitutions(context, part) for part in process.cmd]
            snapshot_path = command[command.index('--config') + 1]
            self.addCleanup(os.unlink, snapshot_path)
            config = supervisor.load_yaml(snapshot_path)
            self.assertIs(config['local_planner']['obstacle_snapshot_enabled'], bool(enabled))

            # A later robot.yaml edit must not change an already captured supervisor run.
            self.robot['system']['task_hub']['obstacle_snapshot_enabled'] = not bool(enabled)
            self.write_robot()
            config['bringup'].update(start_mode='immediate', wait_for_readiness=False)
            worker_path = cfg.write_snapshot(config)
            self.addCleanup(os.unlink, worker_path)
            context = context_for(navigation, navigate_config_path=worker_path)
            captured = {}

            def include(package, filename, switch, launch_arguments=None):
                captured[package] = launch_arguments or {}
                return DeclareLaunchArgument('mock_' + package, default_value='')

            with patch.object(navigation, 'include_package_launch', side_effect=include):
                navigation.launch_setup(context)
            self.assertEqual(captured['local_planner']['obstacle_snapshot_enabled'],
                             str(bool(enabled)).lower())

    def test_obstacle_switch_reaches_planner_in_immediate_mode(self):
        for enabled in (False, True):
            self.robot['system']['task_hub'] = {'obstacle_snapshot_enabled': enabled}
            self.robot['navigation'] = {'bringup': {'start_mode': 'immediate',
                                                    'wait_for_readiness': False}}
            self.write_robot()
            with patch.object(navigation, 'include_package_launch',
                              wraps=navigation.include_package_launch) as include:
                navigation.launch_setup(context_for(navigation))
            planner = next(c for c in include.call_args_list if c.args[0] == 'local_planner')
            self.assertEqual(planner.args[3]['obstacle_snapshot_enabled'], str(enabled).lower())

    def test_route_start_preparation_is_only_enabled_for_d1m(self):
        for model, expected in [('x30', ''), ('d1m', '/nav_bridge_node/ready')]:
            with self.subTest(model=model):
                self.robot['model'] = model
                self.write_robot()
                context = context_for(system)
                with patch.object(system, 'get_package_share_directory', return_value=str(self.root)), \
                        patch.object(system, 'Node', wraps=system.Node) as factory:
                    system.launch_setup(context)
                node = next(call.kwargs for call in factory.call_args_list
                            if call.kwargs.get('name') == 'task_hub_node')
                params = evaluate_parameters(context, normalize_parameters(node['parameters']))
                self.assertEqual(params[0]['route_start_prepare_service_name'], expected)

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

    def test_both_platform_launches_forward_single_point_journal_directory(self):
        self.robot['system'].setdefault('mqtt', {})['navigation_to_pose_directory'] = '/tmp/point-journal'
        self.write_robot()
        for module in (system, platform):
            context = context_for(module)
            with patch.object(system, 'get_package_share_directory', return_value=str(self.root)), \
                    patch.object(module, 'Node', wraps=module.Node) as factory:
                module.launch_setup(context)
            node = next(call.kwargs for call in factory.call_args_list
                        if call.kwargs.get('name') == 'platform_mqtt_bridge_node')
            params = evaluate_parameters(context, normalize_parameters(node['parameters']))
            self.assertEqual(params[0]['navigation_to_pose_directory'], '/tmp/point-journal')

    def test_both_platform_launches_forward_algorithm_result_upload_settings(self):
        values = {
            'mode': 'local_http',
            'algorithm_result_upload_directory': '/tmp/algorithm-result-queue',
            'algorithm_result_upload_max_attempts': 4,
            'algorithm_result_upload_retry_seconds': 2.5,
        }
        self.robot['system']['media_upload'] = values
        self.write_robot()
        for module in (system, platform):
            context = context_for(module)
            with patch.object(system, 'get_package_share_directory', return_value=str(self.root)), \
                    patch.object(module, 'Node', wraps=module.Node) as factory:
                module.launch_setup(context)
            node = next(call.kwargs for call in factory.call_args_list
                        if call.kwargs.get('name') == 'platform_mqtt_bridge_node')
            params = evaluate_parameters(context, normalize_parameters(node['parameters']))[0]
            self.assertEqual(params['media_upload_mode'], 'local_http')
            for name, value in values.items():
                if name != 'mode':
                    self.assertEqual(params[name], value)

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
        mapping = {'inspection_modules.launch.py': modules, 'sensors.launch.py': sensors,
                   'cooperation.launch.py': cooperation}
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

    def test_tmux_launch_entries_keep_modules_and_cooperation_separate(self):
        import shlex
        import subprocess
        from launch.actions import GroupAction, IncludeLaunchDescription, OpaqueFunction
        from launch_ros.actions import Node

        old = {'mission_execution_agent', 'fleet_leader_election', 'capability_mission_planner'}
        new = {'swarm_consensus', 'swarm_execution_agent', 'swarm_mqtt_transport',
               'swarm_mission_coordinator', 'swarm_mission_planner'}

        def flatten(actions):
            for action in actions:
                if isinstance(action, GroupAction):
                    yield from flatten(action.get_sub_entities())
                else:
                    yield action

        for mode, expected in [('disabled', set()), ('coordination', old), ('swarm', new)]:
            with self.subTest(mode=mode):
                self.robot['system'] = {'modules': {'sensors': True, 'sensor_gimbal': True},
                                       'cooperation': {'mode': mode}}
                self.write_robot()
                output = subprocess.run(
                    ['bash', str(ROOT / 'scripts/start_inspection_tmux.sh'), '--dry-run'],
                    capture_output=True, text=True, check=True).stdout
                window = None
                cooperation_packages = []
                for line in output.splitlines():
                    if line.startswith('[window] '):
                        window = line.split(' ', 1)[1]
                    if 'exec ros2 launch' not in line:
                        continue
                    command = shlex.split(line.split('exec ros2 launch ', 1)[1])
                    entry = {'inspection_modules.launch.py': modules,
                             'sensors.launch.py': sensors,
                             'cooperation.launch.py': cooperation}[command[1]]
                    overrides = dict(part.split(':=', 1) for part in command[2:] if ':=' in part)
                    # Load the real wrapper and evaluate its OpaqueFunction, without executing nodes.
                    with patch('ament_index_python.packages.get_package_share_directory',
                               side_effect=lambda name: str(
                                   self.prefix / 'share' / name if name == 'sensor_gimbal'
                                   else self.root)):
                        description = entry.generate_launch_description()
                        context = context_for(entry, **overrides)
                        actions = next(a for a in description.entities
                                       if isinstance(a, OpaqueFunction)).execute(context)
                    selected = [a for a in flatten(actions)
                                if a.condition is None or a.condition.evaluate(context)]
                    packages = [a.node_package for a in selected if isinstance(a, Node)]
                    includes = [a for a in selected if isinstance(a, IncludeLaunchDescription)]
                    if window == 'cooperation':
                        self.assertEqual(set(packages), expected)
                        self.assertFalse(includes)
                        cooperation_packages.extend(packages)
                    elif window == 'task_hub':
                        self.assertEqual(packages, ['inspection_task_hub'])
                        self.assertFalse(includes)
                    elif window in ('sensor_gimbal', 'charge'):
                        self.assertFalse(packages)
                        self.assertEqual(len(includes), 1)
                    elif window in ('alarm', 'light', 'gas', 'sensor_rs485', 'acoustic'):
                        self.assertFalse(packages)
                        self.assertEqual(len(includes), 1)
                    elif window == 'platform':
                        self.assertTrue(packages)
                        self.assertEqual(set(packages), {'inspection_platform_bridge'})
                        self.assertFalse(includes)
                    else:
                        self.fail(f'Unexpected tmux window: {window}')
                    if window != 'cooperation':
                        self.assertFalse(set(packages) & (old | new))
                self.assertEqual(set(cooperation_packages), expected)
                self.assertEqual(len(cooperation_packages), len(expected))


if __name__ == '__main__':
    unittest.main()
