"""Minimal installed package layouts for offline path tests, including subprocesses."""
import os
from pathlib import Path


def package_prefix(directory):
    prefix = Path(directory) / 'packages'
    files = {
        'nav_bridge': ['config/d1_max_params.yaml', 'config/cyvet_cyclonedds.xml'],
        'faster_lio': ['config/mid360.yaml', 'config/airy.yaml'],
        'local_planner': ['config/d1m.yaml', 'config/x30.yaml'],
        'gridmapper': ['config/gridmapper_local.yaml'],
        'multi_map_nav': ['params/straight_local.yaml'],
        'livox_ros_driver2': ['config/MID360_config.json'],
        'sensor_gimbal': ['config/stub_params.yaml'],
        'inspection_platform_bridge': ['config/live_streams.yaml'],
        'gas_monitor': ['config/gas_params_default.yaml'],
        'sensor_rs485': ['config/sensor_rs485_params.yaml'],
    }
    index = prefix / 'share/ament_index/resource_index/packages'
    index.mkdir(parents=True)
    for package, names in files.items():
        (index / package).touch()
        for name in names:
            path = prefix / 'share' / package / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text('{}\n')
    return prefix


def package_environment(prefix):
    return {'AMENT_PREFIX_PATH': str(prefix) + os.pathsep + os.environ.get('AMENT_PREFIX_PATH', '')}
