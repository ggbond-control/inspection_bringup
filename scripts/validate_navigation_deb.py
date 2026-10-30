#!/usr/bin/env python3
"""Smoke checks inside a container with only the deb installed, no workspace."""

import ctypes
import importlib
import json
import os
from pathlib import Path
import pkgutil
import signal
import subprocess
import time

from ament_index_python.packages import get_package_prefix, get_package_share_directory


def main():
    prefix = Path('/opt/inspection/navigation')
    manifest = json.loads((prefix / 'share/navigation_release/manifest.json').read_text())
    for name in manifest['packages']:
        assert Path(get_package_prefix(name)) == prefix, name
        subprocess.run(['ros2', 'pkg', 'executables', name], check=True)
    assert not (prefix / 'share/ament_index/resource_index/packages/nav_bridge').exists()
    assert not (prefix / 'share/ament_index/resource_index/packages/inspection_bringup').exists()
    for module in ['inspection_interfaces.msg', 'inspection_interfaces.srv', 'inspection_interfaces.action',
                   'robots_dog_msgs.msg', 'control_input_msgs.msg', 'livox_ros_driver2.msg',
                   'faster_lio.msg', 'gridmapper.srv']:
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
    for p in prefix.rglob('*'):
        if not p.is_file() or p.is_symlink():
            continue
        with p.open('rb') as stream:
            if stream.read(4) != b'\x7fELF':
                continue
        result = subprocess.run(['ldd', str(p)], capture_output=True, text=True)
        assert result.returncode == 0 and 'not found' not in result.stdout, (p, result.stdout, result.stderr)
        count += 1
    for name in ['libdummy_controller_plugin.so', 'libsmac_straight_planner.so']:
        ctypes.CDLL(str(prefix / 'lib' / name))
    assert (Path(get_package_share_directory('gridmapper')) / 'kernels/terrain_analysis.cl').is_file()
    paths = Path(get_package_share_directory('local_planner')) / 'paths'
    for name in ('startPaths.ply', 'paths.ply', 'pathList.ply',
                 'correspondences_x30.txt', 'correspondences_cyvet.txt', 'correspondences_d1m.txt'):
        assert (paths / name).is_file(), name
    subprocess.run(['ros2', 'launch', 'faster_lio', 'slam.launch.py', '--show-args'], check=True)
    subprocess.run(['ros2', 'launch', 'gnss_driver', 'driver.launch.py', '--show-args'], check=True)
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
