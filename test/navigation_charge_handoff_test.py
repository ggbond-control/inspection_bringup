"""Read-only local ROS mocks; never start navigation or connect to a robot."""
import importlib.util
import os
from pathlib import Path
import subprocess
import sys
import threading
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

import yaml

os.environ['ROS_DOMAIN_ID'] = '197'
os.environ['ROS_AUTOMATIC_DISCOVERY_RANGE'] = 'LOCALHOST'
os.environ['RMW_IMPLEMENTATION'] = 'rmw_fastrtps_cpp'

import rclpy
from rcl_interfaces.srv import SetParameters
from rcl_interfaces.msg import Parameter, ParameterValue
from rclpy.executors import MultiThreadedExecutor
from std_msgs.msg import Int32
from std_srvs.srv import Trigger

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('navigation_supervisor', ROOT / 'scripts/navigation_supervisor.py')
supervisor = importlib.util.module_from_spec(spec)
spec.loader.exec_module(supervisor)


class InitialPose(unittest.TestCase):
    def test_padding_and_validation(self):
        for value, expected in (([1, 2], [1., 2., 0., 0.]),
                                ([1, 2, 3], [1., 2., 3., 0.]),
                                ([1, 2, 3, 90], [1., 2., 3., 90.])):
            self.assertEqual(supervisor.normalize_initial_pose(value), expected)
        for value in ([], [1], [1, 2, 3, 4, 5], [True, 2], ['1', 2],
                      [float('nan'), 2], [1, float('inf')], '1,2'):
            with self.subTest(value=value), self.assertRaises(ValueError):
                supervisor.normalize_initial_pose(value)

    def test_request_is_not_a_transition_parameter(self):
        node = SimpleNamespace(active_config={'mode': 'nav'}, base_config={},
                               current_state='nav', allowed_paths={}, result=supervisor.NavigationSupervisor.result)
        request = SetParameters.Request(parameters=[Parameter(name='slam.initial_pose',
            value=ParameterValue(type=8, double_array_value=[1., 2.]))])
        with patch.object(supervisor, 'resolve_files', side_effect=lambda config, *_: config):
            config, paths, results = supervisor.NavigationSupervisor.resolve_request(node, request)
        self.assertTrue(all(result.successful for result in results))
        self.assertEqual(paths, set())
        self.assertEqual(supervisor.NavigationSupervisor.select_transition(node, config, 'nav', paths), 'noop')
        request.parameters[0].value.double_array_value = [1.]
        with patch.object(supervisor, 'resolve_files', side_effect=lambda config, *_: config):
            _, _, results = supervisor.NavigationSupervisor.resolve_request(node, request)
        self.assertFalse(results[0].successful)

    def test_snapshot_override_does_not_leak_to_next_start(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / 'slam.yaml'
            original = {'laser_mapping': {'ros__parameters': {'initial_alignment': {
                'position': [9., 8., 7.], 'yaw': 45., 'score_threshold': .5}}}}
            source.write_text(yaml.safe_dump(original))
            config = {'slam': {'config': str(source)}, supervisor.REQUEST_INITIAL_POSE: [1., 2., 0., 0.]}
            runtime = supervisor.NavigationSupervisor.layer_config(None, config, ('slam',))
            alignment = yaml.safe_load(Path(runtime['slam']['config']).read_text())['laser_mapping']['ros__parameters']['initial_alignment']
            self.assertEqual(alignment, {'position': [1., 2., 0.], 'yaw': 0., 'score_threshold': .5})
            self.assertEqual(yaml.safe_load(source.read_text()), original)
            node = SimpleNamespace(map_context={})
            supervisor.NavigationSupervisor.commit_success(node, config, 'nav', 'localized')
            self.assertNotIn(supervisor.REQUEST_INITIAL_POSE, node.active_config)
            next_runtime = supervisor.NavigationSupervisor.layer_config(None, node.active_config, ('slam',))
            self.assertEqual(next_runtime['slam']['config'], str(source))
            bridge = supervisor.NavigationSupervisor.layer_config(None, config, ('nav_bridge',))
            self.assertEqual(bridge['slam']['config'], str(source))
            self.assertNotIn(supervisor.REQUEST_INITIAL_POSE, bridge)


class StartupOrdering(unittest.TestCase):
    def test_same_nav_start_does_not_take_control_or_exit_again(self):
        node = SimpleNamespace(run_control_action=Mock(side_effect=AssertionError('unexpected stand')))
        self.assertTrue(supervisor.NavigationSupervisor.execute_transition(
            node, {}, 'nav', 'noop', None)[0])
        node.run_control_action.assert_not_called()

    def test_failed_base_handoff_does_not_start_navigation_extensions(self):
        node = SimpleNamespace(
            layer_sequence=Mock(return_value=('nav_bridge', 'acquire_control', 'livox', 'slam')),
            start_layer=Mock(return_value=(False, 'undock failed')),
            stop_worker=Mock(), start_extensions=Mock(), commit_success=Mock())
        result = supervisor.NavigationSupervisor.execute_transition(node, {}, 'nav', 'full_start', None)
        self.assertFalse(result[0])
        node.start_extensions.assert_not_called()
        node.commit_success.assert_not_called()

    def test_failed_warm_handoff_does_not_start_new_planners(self):
        node = SimpleNamespace(
            current_state='manual', manual_profile='localized', active_config={},
            active_extension_sequence=Mock(return_value=()),
            layer_sequence=Mock(return_value=('terrain', 'local_planner', 'global_planner')),
            run_control_action=Mock(return_value=(False, 'undock unconfirmed')),
            restore_previous_mode=Mock(return_value=(True, 'restored')),
            start_extensions=Mock(), commit_success=Mock())
        result = supervisor.NavigationSupervisor.execute_transition(node, {}, 'nav', 'switch_mode', None)
        self.assertFalse(result[0])
        node.start_extensions.assert_not_called()
        node.commit_success.assert_not_called()


class ChargePrecheck(unittest.TestCase):
    def setUp(self):
        rclpy.init()
        self.node = rclpy.create_node('navigation_handoff_mock')
        self.direct_commands = []
        self.node.create_service(SetParameters, '/nav_bridge_node/charge_command', self.direct)
        publisher = self.node.create_publisher(Int32, '/charge_manager_state', 10)
        self.node.create_timer(.05, lambda: publisher.publish(Int32(data=0)))
        self.executor = MultiThreadedExecutor(num_threads=2)
        self.executor.add_node(self.node)
        self.thread = threading.Thread(target=self.executor.spin)
        self.thread.start()

    def tearDown(self):
        self.executor.shutdown(timeout_sec=5)
        self.thread.join(timeout=5)
        self.node.destroy_node()
        rclpy.shutdown()

    def direct(self, request, response):
        self.direct_commands.append(request)
        return response

    def run_check(self, service='/inspection_charge_executor/undock'):
        return subprocess.run([
            sys.executable, str(ROOT / 'scripts/wait_for_ready.py'), 'charge_precheck',
            '--charge-executor-exit-service', service,
            # Allow DDS discovery on embedded boards before checking the handoff result.
            '--charge-check-timeout', '3', '--charge-exit-timeout', '3'],
            capture_output=True, text=True, timeout=12)

    def test_idle_with_missing_executor_cannot_fall_back_to_stop(self):
        result = self.run_check('/missing_undock')
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.direct_commands, [])

    def test_rejected_handoff_cannot_fall_back_to_stop(self):
        def reject(_, response):
            response.success = False
            response.message = 'SDK undock failed'
            return response
        self.node.create_service(Trigger, '/inspection_charge_executor/undock', reject)
        result = self.run_check()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('SDK undock failed', result.stderr)
        self.assertEqual(self.direct_commands, [])

    def test_completed_handoff_allows_start(self):
        def complete(_, response):
            response.success = True
            response.message = 'Undock and ready complete'
            return response
        self.node.create_service(Trigger, '/inspection_charge_executor/undock', complete)
        result = self.run_check()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.direct_commands, [])


if __name__ == '__main__':
    unittest.main()
