#!/usr/bin/env python3
"""Smoke checks inside a container with only the deb installed, no workspace."""

import ctypes
import importlib
import json
import os
from pathlib import Path
import pkgutil
import signal
import struct
import subprocess
import time

from ament_index_python.packages import get_package_prefix, get_package_share_directory


def main():
    prefix = Path('/opt/inspection/navigation')
    driver = Path('/opt/inspection/driver')
    manifests = [json.loads((driver / 'share/control_release/manifest.json').read_text())]
    for component in ('navigation', 'task', 'cooperation'):
        path = Path('/opt/inspection') / component / 'share/control_release/manifest.json'
        if path.exists():
            manifests.append(json.loads(path.read_text()))
    has_navigation = any(v['component'] == 'navigation' for v in manifests)
    has_task = any(v['component'] == 'task' for v in manifests)
    has_cooperation = any(v['component'] == 'cooperation' for v in manifests)
    for item in manifests:
        component_prefix = Path(item['install_prefix'])
        for name in item['packages']:
            assert Path(get_package_prefix(name)) == component_prefix, name
            subprocess.run(['ros2', 'pkg', 'executables', name], check=True)
        assert not (component_prefix / 'share/ament_index/resource_index/packages/inspection_bringup').exists()
        assert subprocess.check_output(['dpkg', '--print-architecture'], text=True).strip() == item['architecture']
    manifest = manifests[-1]
    assert subprocess.check_output(['dpkg', '--print-architecture'], text=True).strip() == manifest['architecture']
    for model in ('x30', 'd1_max', 'cyvet', 'tank', 'r750'):
        assert os.access(driver / f'lib/nav_bridge/{model}_nav_bridge_node', os.X_OK), model
    for module in ['inspection_interfaces.msg', 'inspection_interfaces.srv', 'inspection_interfaces.action',
                   'robots_dog_msgs.msg', 'control_input_msgs.msg', 'livox_ros_driver2.msg',
                   'uniubi.msg'] + (['faster_lio.msg', 'gridmapper.srv'] if has_navigation else []):
        imported = importlib.import_module(module)
        for name in dir(imported):
            cls = getattr(imported, name)
            if isinstance(cls, type) and hasattr(type(cls), '__import_type_support__'):
                type(cls).__import_type_support__()
    import gnss_driver
    for info in pkgutil.walk_packages(gnss_driver.__path__, 'gnss_driver.'):
        importlib.import_module(info.name)
    from gnss_driver.transform_io import default_data_path
    calibration = Path(default_data_path('smoke-transform.txt'))
    assert calibration.is_relative_to(Path(os.environ['HOME']) / '.ros/navigation/gnss_driver')
    calibration.write_text('writable runtime directory\n')
    count = 0
    for p in [p for item in manifests for p in Path(item['install_prefix']).rglob('*')]:
        if not p.is_file() or p.is_symlink():
            continue
        with p.open('rb') as stream:
            header = stream.read(20)
            if header[:4] != b'\x7fELF':
                continue
            machine = struct.unpack('<H' if header[5] == 1 else '>H', header[18:20])[0]
            assert machine == {'amd64': 62, 'arm64': 183}[manifest['architecture']], (p, machine)
        result = subprocess.run(['ldd', str(p)], capture_output=True, text=True)
        assert result.returncode == 0 and 'not found' not in result.stdout, (p, result.stdout, result.stderr)
        count += 1
    subprocess.run(['ros2', 'launch', 'gnss_driver', 'driver.launch.py', '--show-args'], check=True)
    if has_task:
        importlib.import_module('inspection_platform_bridge.platform_mqtt_bridge_node')
        task = Path('/opt/inspection/task')
        for package, executable in [('alarm_manager', 'alarm_aggregator_node'),
                                    ('sensor_gimbal', 'sensor_gimbal_node'),
                                    ('inspection_task_hub', 'task_hub_node')]:
            assert os.access(task / 'lib' / package / executable, os.X_OK), executable
    if has_cooperation:
        for module in ('fleet_election.ros_node', 'mission_execution_agent.node',
                       'swarm_protocol.ros', 'swarm_consensus.node', 'swarm_execution_agent.node',
                       'swarm_mission_coordinator.node', 'swarm_mission_planner.node',
                       'swarm_mqtt_transport.node'):
            importlib.import_module(module)
        assert os.access('/opt/inspection/cooperation/lib/capability_mission_planner/'
                         'capability_mission_planner_node', os.X_OK)
    if not has_navigation:
        print(f'PASS: {len(manifests)} components, bridge executables, interfaces and {count} ELF files')
        return
    for name in ['libdummy_controller_plugin.so', 'libsmac_straight_planner.so']:
        ctypes.CDLL(str(prefix / 'lib' / name))
    assert (Path(get_package_share_directory('gridmapper')) / 'kernels/terrain_analysis.cl').is_file()
    paths = Path(get_package_share_directory('local_planner')) / 'paths'
    for name in ('startPaths.ply', 'paths.ply', 'pathList.ply',
                 'correspondences_x30.txt', 'correspondences_cyvet.txt', 'correspondences_d1m.txt'):
        assert (paths / name).is_file(), name
    subprocess.run(['ros2', 'launch', 'faster_lio', 'slam.launch.py', '--show-args'], check=True)
    # No sensors, chassis or live ROS domain: exercise startup and orderly shutdown only.
    env = dict(os.environ, ROS_DOMAIN_ID='217', ROS_AUTOMATIC_DISCOVERY_RANGE='LOCALHOST')
    with open('/tmp/navigation-follower-smoke.log', 'w+') as log:
        child = subprocess.Popen([str(prefix / 'lib/local_planner/pathFollower'), '--ros-args',
                                  '-p', 'tracking_mode:=omni', '-r', '/cmd_vel_nav:=/test/cmd_vel'],
                                 env=env, stdout=log, stderr=subprocess.STDOUT)
        try:
            time.sleep(2)
            running = child.poll() is None
        finally:
            if child.poll() is None:
                child.send_signal(signal.SIGINT)
            code = child.wait(timeout=10)
        log.seek(0)
        text = log.read()
        print(text)
        assert running, 'pathFollower exited before shutdown'
        assert code == 0, code
    print(f'PASS: {len(manifest["packages"])} ROS packages, interfaces, GNSS imports, '
          f'{count} ELF files, plugins, launch resources and isolated follower startup')


if __name__ == '__main__':
    main()
