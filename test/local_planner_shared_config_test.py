"""Shared local-planner parameters retain layer priority and ROS node semantics."""
import copy
import os
from pathlib import Path
import shutil
import sys
import tempfile
import unittest
from unittest.mock import patch

import yaml
from config_fixtures import package_environment, package_prefix

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
import bringup_config as cfg

D1M_CAPABILITY = {
    'tracking_mode': 'turn_feasible',
    'turn_forward_speed_samples': [0.0],
    'turn_lateral_speed_samples': [0.5],
    'turn_lateral_zero_above_speed': 1.0,
    'turn_yaw_forward_speed_samples': [0.0, 1.0, 2.0],
    'turn_yaw_rate_samples': [1.5, 1.0, 0.5],
}


class SharedPlannerConfiguration(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='shared planner ')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / 'config'
        shutil.copytree(ROOT / 'config', self.root, ignore=shutil.ignore_patterns('robot.yaml'))
        prefix = package_prefix(self.temp.name)
        env = patch.dict(os.environ, package_environment(prefix), INSPECTION_CONFIG_DIR=str(self.root))
        env.start()
        self.addCleanup(env.stop)

    def load(self, parameters=None, model='d1m'):
        robot = {'model': model, 'robot_id': 'LOCAL-CONFIG-TEST'}
        if parameters is not None:
            robot['navigation'] = {'local_planner': {'parameters': parameters}}
        (self.root / 'robot.yaml').write_text(yaml.safe_dump(robot))
        resolved = cfg.load_config('navigation')
        return cfg.read_yaml(resolved['local_planner']['config']), resolved

    @staticmethod
    def effective(data, node):
        return cfg.merge(data.get('/**', {}).get('ros__parameters', {}),
                         data.get(node, {}).get('ros__parameters', {}))

    def test_d1m_defaults_are_shared_once_and_effective_for_both_nodes(self):
        actual, _ = self.load()
        raw = cfg.read_yaml(self.root / 'models/d1m/navigation/param_d1m_local_planner.yaml')
        for key, expected in D1M_CAPABILITY.items():
            self.assertEqual(raw['/**']['ros__parameters'][key], expected)
            self.assertEqual(actual['/**']['ros__parameters'][key], expected)
            for node in ('localPlanner', 'pathFollower'):
                self.assertNotIn(key, raw.get(node, {}).get('ros__parameters', {}))
                self.assertNotIn(key, actual[node]['ros__parameters'])
                self.assertEqual(self.effective(actual, node)[key], expected)
        for node in ('localPlanner', 'pathFollower'):
            self.assertEqual(self.effective(actual, node)['offroad']['maxSpeed'], 2.2)
            self.assertEqual(self.effective(actual, node)['flat']['maxYawRate'], .8)

    def test_robot_shared_mode_replaces_inherited_explicit_node_modes(self):
        model_path = self.root / 'models/d1m/navigation/param_d1m_local_planner.yaml'
        model = cfg.read_yaml(model_path)
        for node in ('localPlanner', 'pathFollower'):
            model[node]['ros__parameters']['tracking_mode'] = 'turn_preferred'
        model_path.write_text(yaml.safe_dump(model))
        actual, _ = self.load({'/**': {'ros__parameters': {'tracking_mode': 'omni'}}})
        for node in ('localPlanner', 'pathFollower'):
            self.assertEqual(self.effective(actual, node)['tracking_mode'], 'omni')
            self.assertNotIn('tracking_mode', actual[node]['ros__parameters'])

    def test_robot_shared_table_overrides_lower_node_table_without_duplicates(self):
        model_path = self.root / 'models/d1m/navigation/param_d1m_local_planner.yaml'
        model = cfg.read_yaml(model_path)
        model['pathFollower']['ros__parameters']['turn_yaw_rate_samples'] = [1.5, 1., .9]
        model_path.write_text(yaml.safe_dump(model))
        actual, _ = self.load({'/**': {'ros__parameters': {'turn_yaw_rate_samples': [1.2, .8, .4]}}})
        for node in ('localPlanner', 'pathFollower'):
            self.assertEqual(self.effective(actual, node)['turn_yaw_rate_samples'], [1.2, .8, .4])
            self.assertNotIn('turn_yaw_rate_samples', actual[node]['ros__parameters'])

    def test_robot_shared_overrides_model_inline_parameters(self):
        nav = self.root / 'models/d1m/navigate.yaml'
        model = cfg.read_yaml(nav)
        model['local_planner'] = {'parameters': {
            'pathFollower': {'ros__parameters': {'tracking_mode': 'turn_preferred'}}}}
        nav.write_text(yaml.safe_dump(model))
        actual, _ = self.load({'/**': {'ros__parameters': {'tracking_mode': 'omni'}}})
        for node in ('localPlanner', 'pathFollower'):
            self.assertEqual(self.effective(actual, node)['tracking_mode'], 'omni')

    def test_same_layer_node_specific_value_still_wins(self):
        actual, _ = self.load({
            '/**': {'ros__parameters': {'tracking_mode': 'omni'}},
            'pathFollower': {'ros__parameters': {'tracking_mode': 'turn_preferred'}}})
        self.assertEqual(self.effective(actual, 'localPlanner')['tracking_mode'], 'omni')
        self.assertEqual(self.effective(actual, 'pathFollower')['tracking_mode'], 'turn_preferred')

    def test_other_robot_profiles_keep_their_modes_and_no_d1m_capabilities(self):
        for model in ('x30', 'cyvet', 'tank'):
            with self.subTest(model=model):
                actual, _ = self.load(model=model)
                self.assertEqual(self.effective(actual, 'pathFollower')['tracking_mode'],
                                 'differential' if model == 'tank' else 'omni')
                for key in D1M_CAPABILITY.keys() - {'tracking_mode'}:
                    self.assertNotIn(key, self.effective(actual, 'pathFollower'))

    def test_nested_shared_override_preserves_unrelated_leaves_and_inputs(self):
        base = {'/**': {'ros__parameters': {'flat': {'maxSpeed': 2., 'maxYawRate': 1.}}},
                'pathFollower': {'ros__parameters': {'flat': {'maxSpeed': 3., 'maxAccelX': 4.},
                                                   'private_value': 7}}}
        override = {'/**': {'ros__parameters': {'flat': {'maxSpeed': 1.5}}}}
        saved = copy.deepcopy(base)
        actual = cfg.merge_local_planner_parameters(base, override)
        self.assertEqual(base, saved)
        self.assertEqual(self.effective(actual, 'pathFollower')['flat'],
                         {'maxSpeed': 1.5, 'maxYawRate': 1., 'maxAccelX': 4.})
        self.assertEqual(actual['pathFollower']['ros__parameters']['private_value'], 7)

    def test_false_zero_and_unrelated_guard_node_are_preserved(self):
        base = {'localPlanner': {'ros__parameters': {'flag': True, 'limit': 3}},
                'cmd_vel_guard': {'ros__parameters': {'limit': 7}}}
        actual = cfg.merge_local_planner_parameters(base, {'/**': {'ros__parameters': {'flag': False, 'limit': 0}}})
        self.assertIs(self.effective(actual, 'localPlanner')['flag'], False)
        self.assertEqual(self.effective(actual, 'localPlanner')['limit'], 0)
        self.assertEqual(actual['cmd_vel_guard']['ros__parameters'], {'limit': 7})

    def test_direct_model_path_uses_the_same_shared_override_rules(self):
        base = {'pathFollower': {'ros__parameters': {'tracking_mode': 'turn_preferred'}}}
        model = self.root / 'models/d1m/navigation/complete.yaml'
        model.write_text(yaml.safe_dump(base))
        path = cfg.layered_parameter_file('${INSPECTION_CONFIG_DIR}/models/d1m/navigation/complete.yaml',
            self.root, 'd1m', {'/**': {'ros__parameters': {'tracking_mode': 'turn_feasible'}}},
            'test', cfg.merge_local_planner_parameters)
        self.assertEqual(self.effective(cfg.read_yaml(path), 'pathFollower')['tracking_mode'], 'turn_feasible')

    def test_snapshot_is_stable_and_sources_are_not_rewritten(self):
        source = self.root / 'models/d1m/navigation/param_d1m_local_planner.yaml'
        original = source.read_bytes()
        actual, resolved = self.load()
        snapshot = cfg.write_snapshot(resolved, self.temp.name)
        restored = cfg.load_config('navigation', snapshot)
        self.assertEqual(restored['local_planner']['config'], resolved['local_planner']['config'])
        self.assertEqual(cfg.read_yaml(restored['local_planner']['config']), actual)
        self.assertEqual(source.read_bytes(), original)


if __name__ == '__main__':
    unittest.main()
