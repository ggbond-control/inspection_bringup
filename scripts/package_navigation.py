#!/usr/bin/env python3
"""Build a model-independent navigation deb using a persistent Docker build cache."""

import argparse
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parents[1]
PREFIX = Path('/opt/inspection/navigation')
IMAGE = 'inspection-navigation-builder:jazzy-amd64'
SOURCES = {
    'inspection_interfaces': 'task_ws/src/inspection_interfaces',
    'robots_dog_msgs': 'driver_ws/src/robots_dog_msgs',
    'control_input_msgs': 'driver_ws/src/control_input_msgs',
    'livox_ros_driver2': 'driver_ws/src/livox_ros_driver2',
    'gnss_driver': 'driver_ws/src/gnss_driver',
    'ndt_omp': 'driver_ws/src/ndt_omp',
    'faster_lio': 'algor_ws/src/faster-slam',
    'gridmapper': 'algor_ws/src/gridmapper',
    'local_planner': 'algor_ws/src/local_planner',
    'multi_map_nav': 'algor_ws/src/multi_map_nav_ros2',
    'smac_straight_planner': 'algor_ws/src/smac_straight_planner',
}
NATIVE = {
    'gtsam': ('https://github.com/borglab/gtsam.git', '4.2.0'),
    'livox_sdk2': ('https://github.com/ggbond-control/livox_sdk2.git', 'jazzy'),
}
RUNTIME_DEPENDS = [
    'ros-jazzy-ros-base', 'ros-jazzy-navigation2', 'ros-jazzy-grid-map',
    'ros-jazzy-pcl-ros', 'ros-jazzy-tf-transformations',
    'ros-jazzy-rmw-fastrtps-cpp', 'ros-jazzy-rmw-cyclonedds-cpp',
    'python3-yaml', 'python3-numpy', 'python3-scipy', 'python3-serial',
]


def run(args, **kwargs):
    print('+ ' + ' '.join(str(a) for a in args), flush=True)
    return subprocess.run([str(a) for a in args], check=True, **kwargs)


def output(args, **kwargs):
    return subprocess.check_output([str(a) for a in args], text=True, **kwargs).strip()


def write(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists() or path.read_text() != text:
        path.write_text(text)


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def snapshot(source, destination):
    """Copy source inputs only; retain unchanged timestamps and release-only edits."""
    names = output(['git', '-C', source, 'ls-files', '-c', '-o', '--exclude-standard', '-z']).split('\0')
    names = sorted(set(n for n in names if n and (source / n).is_file()))
    state_file = destination / '.navigation-inputs.json'
    old = json.loads(state_file.read_text()) if state_file.exists() else {}
    state = {}
    for name in names:
        original = source / name
        if original.is_symlink() and not original.resolve().is_relative_to(source.resolve()):
            raise ValueError(f'source symlink requires review before redistribution: {original}')
        state[name] = digest(original)
        target = destination / name
        if old.get(name) != state[name] or not target.exists():
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(original, target)
    for name in old.keys() - state.keys():
        (destination / name).unlink(missing_ok=True)
    write(state_file, json.dumps(state, sort_keys=True))
    return hashlib.sha256(json.dumps(state, sort_keys=True).encode()).hexdigest()


def release_adjustments(sources):
    # Restrict changes to release copies. Runtime data belongs to the invoking user,
    # never the read-only installed prefix or the original developer's workspace.
    for package in ('faster_lio', 'gridmapper'):
        directory = sources / package
        cmake = directory / 'CMakeLists.txt'
        text = cmake.read_text()
        definition = 'add_definitions(-DROOT_DIR="${CMAKE_CURRENT_SOURCE_DIR}/")'
        old_replacement = 'add_compile_options(-include "${CMAKE_CURRENT_SOURCE_DIR}/include/navigation_release_runtime.hpp")'
        generic = ('add_compile_options("$<$<COMPILE_LANGUAGE:CXX>:-include>" '
                   '"$<$<COMPILE_LANGUAGE:CXX>:${CMAKE_CURRENT_SOURCE_DIR}/include/navigation_release_runtime.hpp>")')
        replacement = (generic if package == 'faster_lio' else
                       generic.replace('add_compile_options(', 'target_compile_options(gridmapper_node PRIVATE '))
        text = text.replace(old_replacement, replacement)
        text = text.replace(generic, replacement)
        if definition not in text and replacement not in text:
            raise ValueError(f'{package}: ROOT_DIR definition changed; review release adaptation')
        write(cmake, text.replace(definition, replacement))
        write(directory / 'include/navigation_release_runtime.hpp', '''#pragma once
#include <cstdlib>
#include <filesystem>
#include <stdexcept>
#include <string>
namespace navigation_release {
inline const std::string &runtime_root() {
    static const std::string root = [] {
        const char *ros_home = std::getenv("ROS_HOME");
        const char *home = std::getenv("HOME");
        if ((!ros_home || !*ros_home) && (!home || !*home))
            throw std::runtime_error("Set ROS_HOME or HOME for navigation runtime data");
        std::filesystem::path p = ros_home && *ros_home ? ros_home : std::string(home) + "/.ros";
        p /= "navigation/PACKAGE";
        std::filesystem::create_directories(p / "Log");
        return p.string() + "/";
    }();
    return root;
}
}
#define ROOT_DIR navigation_release::runtime_root().c_str()
'''.replace('PACKAGE', package))
    ndt = sources / 'ndt_omp/CMakeLists.txt'
    write(ndt, ndt.read_text().replace('-msse -msse2 -msse3 -msse4 -msse4.1 -msse4.2',
                                     '-march=x86-64 -mtune=generic'))
    gnss = sources / 'gnss_driver/setup.py'
    write(gnss, gnss.read_text().replace("glob('launch/*.launch.py')", "glob('launch/*.py')"))
    transform = sources / 'gnss_driver/gnss_driver/transform_io.py'
    original = "data_directory = Path(get_package_share_directory('gnss_driver')) / 'data'"
    replacement = ("data_directory = Path(os.environ.get('ROS_HOME') or "
                   "str(Path.home() / '.ros')) / 'navigation/gnss_driver'")
    text = transform.read_text()
    if original not in text and replacement not in text:
        raise ValueError('gnss_driver: default data path changed; review release adaptation')
    write(transform, text.replace(original, replacement))


def selected_packages(requested, sources):
    dependencies = {}
    for name in SOURCES:
        xml = ET.parse(sources / name / 'package.xml').getroot()
        dependencies[name] = {v.text.strip() for v in xml if 'depend' in v.tag and v.text} & SOURCES.keys()
    selected = set(requested)
    while True:
        consumers = {name for name, deps in dependencies.items() if deps & selected}
        if consumers <= selected:
            return sorted(selected)
        selected |= consumers


def environment():
    # This process runs inside the builder, with no developer workspace mounted.
    code = 'source /opt/ros/jazzy/setup.bash; '
    if (PREFIX / 'local_setup.bash').is_file():
        code += f'source {PREFIX}/local_setup.bash; '
    code += 'exec /usr/bin/python3 -c "import os,json; print(json.dumps(dict(os.environ)))"'
    env = json.loads(output(['bash', '-ec', code]).splitlines()[-1])
    vendor = PREFIX / 'vendor'
    for key, value in [('CMAKE_PREFIX_PATH', str(vendor)),
                       ('LD_LIBRARY_PATH', f'{vendor}/lib:{PREFIX}/lib')]:
        env[key] = value + ':' + env.get(key, '')
    return env


def build_native(work, jobs):
    for name in NATIVE:
        stamp = work / f'{name}.installed'
        artifact = PREFIX / 'vendor/lib' / ('libgtsam.so' if name == 'gtsam'
                                           else 'liblivox_lidar_sdk_shared.so')
        if stamp.exists() and artifact.is_file():
            print(f'[cache] reuse {name}', flush=True)
            continue
        args = ['-DCMAKE_BUILD_TYPE=Release', f'-DCMAKE_INSTALL_PREFIX={PREFIX}/vendor',
                '-DCMAKE_INSTALL_LIBDIR=lib', '-DCMAKE_C_FLAGS=-march=x86-64 -mtune=generic',
                '-DCMAKE_CXX_FLAGS=-march=x86-64 -mtune=generic',
                '-DCMAKE_INSTALL_RPATH=$ORIGIN']
        if name == 'gtsam':
            args += ['-DGTSAM_USE_SYSTEM_EIGEN=ON', '-DGTSAM_BUILD_TESTS=OFF',
                     '-DGTSAM_BUILD_EXAMPLES_ALWAYS=OFF', '-DGTSAM_BUILD_UNSTABLE=ON',
                     '-DGTSAM_BUILD_WITH_MARCH_NATIVE=OFF']
        build = work / 'native-build' / name
        run(['cmake', '-S', work / 'native-src' / name, '-B', build, *args])
        run(['cmake', '--build', build, '-j', jobs])
        run(['cmake', '--install', build])
        write(stamp, 'complete\n')


def installed_files(work):
    """Collect CMake/setuptools ownership without including shared colcon hooks."""
    files = set()
    for name in SOURCES:
        for filename in ('install_manifest.txt', 'install.log'):
            manifest = work / 'build' / name / filename
            if not manifest.is_file():
                continue
            for line in manifest.read_text().splitlines():
                path = Path(line.strip())
                if path.is_absolute() and path.is_relative_to(PREFIX) and '..' not in path.parts:
                    files.add(str(path))
    return files


def prune_obsolete(previous, current):
    for name in sorted(previous - current):
        path = Path(name)
        if not path.is_relative_to(PREFIX) or '..' in path.parts:
            raise ValueError(f'invalid cached install path: {path}')
        if path.is_symlink() or path.is_file():
            path.unlink()


def assemble(work, version, manifest):
    manifest = dict(manifest, version=version, install_prefix=str(PREFIX),
                    packaging_recipe_sha256=digest(Path(__file__)))
    stage = work / 'stage'
    if stage.exists():
        shutil.rmtree(stage)
    dest = stage / PREFIX.relative_to('/')
    shutil.copytree(PREFIX, dest, symlinks=True)
    # Keep this runtime-only bundle independent of source/build exports.
    for p in sorted(dest.rglob('*'), reverse=True):
        if p.is_file() and (p.suffix in ('.a', '.pyc') or p.name == 'COLCON_IGNORE'):
            p.unlink()
        elif p.is_dir() and (p.name in ('include', '__pycache__') or
                             (p.name == 'cmake' and p.parent.parent == dest / 'share')):
            shutil.rmtree(p)
    for p in [dest / 'vendor/share', dest / 'vendor/lib/cmake', dest / 'vendor/lib/pkgconfig',
              dest / 'share/multi_map_nav/maps']:
        if p.exists():
            shutil.rmtree(p)
    elfs = []
    for p in dest.rglob('*'):
        if p.is_symlink():
            target = p.resolve()
            # Runtime library SONAME symlinks must stay inside the payload.
            if not target.is_relative_to(dest) or not target.exists():
                raise ValueError(f'external or broken install symlink: {p} -> {p.readlink()}')
        elif p.is_file():
            with p.open('rb') as stream:
                magic = stream.read(4)
            if magic == b'\x7fELF':
                run(['strip', '--strip-unneeded', p], stdout=subprocess.DEVNULL)
                run(['patchelf', '--set-rpath', f'{PREFIX}/lib:{PREFIX}/vendor/lib:/opt/ros/jazzy/lib', p],
                    stdout=subprocess.DEVNULL)
                elfs.append(p)
            elif p.suffix in ('.sh', '.bash', '.zsh', '.ps1', '.dsv', '.py', '.cmake', '.pc'):
                text = p.read_text(errors='strict')
                if '/release/' in text:
                    raise ValueError(f'build-only path in installed runtime file: {p}')
    indexes = dest / 'share/ament_index/resource_index/packages'
    actual = {p.name for p in indexes.iterdir()}
    if actual != set(SOURCES):
        raise ValueError(f'unexpected packaged ROS package list: {sorted(actual)}')
    docs = stage / 'usr/share/doc/navigation'
    docs.mkdir(parents=True)
    licenses = docs / 'licenses'
    for name, directory in [(n, work / 'sources' / n) for n in SOURCES] + [
            (n, work / 'native-src' / n) for n in NATIVE]:
        for p in directory.iterdir():
            if p.is_file() and p.name.lower().startswith(('license', 'copying', 'copyright', 'notice')):
                target = licenses / name / p.name
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(p, target)
    write(docs / 'manifest.json', json.dumps(manifest, indent=2) + '\n')
    write(dest / 'share/navigation_release/manifest.json', json.dumps(manifest, indent=2) + '\n')
    write(docs / 'README', 'Source /opt/ros/jazzy/setup.bash, then '
          '/opt/inspection/navigation/local_setup.bash. No services are installed or started.\n'
          'Runtime defaults are package resources; supply actual device settings externally.\n'
          'Data defaults: ${ROS_HOME:-$HOME/.ros}/navigation/<package>.\n')
    debian = work / 'dependency-scan/debian'
    write(debian / 'control', 'Source: navigation\nSection: misc\nPriority: optional\n'
          'Maintainer: Navigation maintainers <robot@example.com>\n\n'
          'Package: navigation\nArchitecture: amd64\nDescription: Navigation runtime\n')
    args = ['dpkg-shlibdeps', '-O', '--ignore-missing-info',
            f'-l{dest}/lib', f'-l{dest}/vendor/lib', '-l/opt/ros/jazzy/lib']
    result = output([*args, *['-e' + str(p) for p in elfs]], cwd=debian.parent)
    depends = next(line.split('=', 1)[1] for line in result.splitlines() if line.startswith('shlibs:Depends='))
    depends = ', '.join([depends, *RUNTIME_DEPENDS])
    control = stage / 'DEBIAN'
    control.mkdir()
    installed_kib = sum(p.stat().st_size for p in dest.rglob('*') if p.is_file()) // 1024
    write(control / 'control', f'Package: navigation\nVersion: {version}\nArchitecture: amd64\n'
          'Maintainer: Navigation maintainers <robot@example.com>\nSection: misc\nPriority: optional\n'
          f'Installed-Size: {installed_kib}\nDepends: {depends}\nRecommends: ros-jazzy-rviz2\n'
          'Description: Model-independent ROS 2 Jazzy navigation runtime\n'
          ' Localization, mapping, planning, sensor drivers and custom interfaces.\n'
          ' Excludes nav_bridge, inspection_bringup, maps and system services.\n')
    dist = work / 'dist'
    dist.mkdir(exist_ok=True)
    deb = dist / f'navigation_{version}_amd64.deb'
    run(['dpkg-deb', '--root-owner-group', '-Zxz', '-z3', '--build', stage, deb])
    write(dist / (deb.name + '.sha256'), digest(deb) + '  ' + deb.name + '\n')
    write(dist / (deb.name + '.manifest.json'), json.dumps(manifest, indent=2) + '\n')
    print(f'[done] {deb}', flush=True)


def inside(args):
    work = Path('/release')
    manifest = json.loads((work / 'inputs.json').read_text())
    complete = work / 'build-state.json'
    if args.package_only:
        if (work / 'build-in-progress').exists() or not complete.exists():
            raise ValueError('no complete build snapshot; finish the build before --package-only')
        assemble(work, args.version, json.loads(complete.read_text()))
        return
    if args.select and not complete.exists():
        raise ValueError('first release build must be full; omit --select')
    previous = json.loads(complete.read_text()) if complete.exists() else {}
    if previous and previous.get('builder_image') != manifest['builder_image']:
        raise ValueError('builder image changed; use a new --work-dir for an ABI-clean build')
    if args.select and previous.get('release_recipe_sha256') != manifest['release_recipe_sha256']:
        raise ValueError('release recipe changed; run without --select to apply it to all packages')
    selected = selected_packages(args.select, work / 'sources') if args.select else list(SOURCES)
    changed = {n for n in SOURCES if manifest['packages'][n]['sha256'] !=
               previous.get('packages', {}).get(n, {}).get('sha256')}
    if args.select and changed - set(selected):
        raise ValueError('unselected packages also changed: ' + ', '.join(sorted(changed - set(selected))) +
                         '; include them in --select or run without --select')
    print('[build] selected packages and consumers: ' + ' '.join(selected), flush=True)
    ownership = work / 'installed-files.json'
    if not ownership.exists():
        write(ownership, json.dumps(sorted(installed_files(work))))
    previous_files = set(json.loads(ownership.read_text()))
    write(work / 'build-in-progress', 'incomplete\n')
    build_native(work, args.jobs)
    env = environment()
    env['MAKEFLAGS'] = f'-j{args.jobs}'
    env['CMAKE_BUILD_PARALLEL_LEVEL'] = str(args.jobs)
    run(['colcon', '--log-base', work / 'log', 'build', '--merge-install',
         '--base-paths', work / 'sources', '--packages-select', *selected,
         '--build-base', work / 'build', '--install-base', PREFIX,
         '--executor', 'sequential', '--event-handlers', 'console_cohesion+',
         '--cmake-args', '-DCMAKE_BUILD_TYPE=Release', '-DBUILD_TESTING=OFF',
         '-DBUILD_WITH_MARCH_NATIVE=OFF', '-DCMAKE_C_FLAGS=-march=x86-64 -mtune=generic',
         '-DCMAKE_CXX_FLAGS=-march=x86-64 -mtune=generic',
         '-DPython3_EXECUTABLE=/usr/bin/python3', '-DPYTHON_EXECUTABLE=/usr/bin/python3',
         f'-DGTSAM_DIR={PREFIX}/vendor/lib/cmake/GTSAM',
         f'-DLIVOX_LIDAR_SDK_LIBRARY={PREFIX}/vendor/lib/liblivox_lidar_sdk_shared.so',
         f'-DLIVOX_LIDAR_SDK_INCLUDE_DIR={PREFIX}/vendor/include',
         f'-DCMAKE_INSTALL_RPATH={PREFIX}/lib;{PREFIX}/vendor/lib;/opt/ros/jazzy/lib'],
        cwd=work, env=env)
    current_files = installed_files(work)
    prune_obsolete(previous_files, current_files)
    write(ownership, json.dumps(sorted(current_files)))
    write(complete, json.dumps(manifest, indent=2))
    (work / 'build-in-progress').unlink()
    assemble(work, args.version, manifest)


def arguments():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--version', required=True, help='Debian version, e.g. 0.1.0 or 0.1.1')
    p.add_argument('--workspace-root', type=Path, default=Path.home() / 'Workspace')
    p.add_argument('--work-dir', type=Path, default=Path.home() / '.cache/inspection-navigation-deb/amd64')
    p.add_argument('-j', '--jobs', type=int, default=1)
    mode = p.add_mutually_exclusive_group()
    mode.add_argument('--select', nargs='+', choices=list(SOURCES), help='rebuild selected packages and their consumers')
    mode.add_argument('--package-only', action='store_true', help='repackage last complete build, without compiling')
    p.add_argument('--rebuild-image', action='store_true', help='rebuild cached Docker build environment')
    p.add_argument('--inside', action='store_true', help=argparse.SUPPRESS)
    args = p.parse_args()
    if args.jobs < 1:
        p.error('--jobs must be positive')
    if not re.fullmatch(r'[0-9][A-Za-z0-9.+~:-]*', args.version):
        p.error('invalid Debian version')
    return args


def main():
    args = arguments()
    if args.inside:
        inside(args)
        return
    if output(['dpkg', '--print-architecture']) != 'amd64':
        raise ValueError('this experimental release builder requires an amd64 host')
    work = args.work_dir.expanduser().resolve()
    work.mkdir(parents=True, exist_ok=True)
    with (work / '.lock').open('w') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        image = subprocess.run(['docker', 'image', 'inspect', IMAGE], stdout=subprocess.DEVNULL,
                               stderr=subprocess.DEVNULL).returncode == 0
        if args.rebuild_image or not image:
            run(['docker', 'build', '-t', IMAGE, '-f', ROOT / 'packaging/navigation/Dockerfile',
                 ROOT / 'packaging/navigation'])
        if not args.package_only:
            manifest = {'builder_image': output(['docker', 'image', 'inspect', '--format', '{{.Id}}', IMAGE]),
                        'packages': {}, 'native': {}, 'architecture': 'amd64', 'ros': 'jazzy',
                        'release_recipe_sha256': digest(Path(__file__))}
            for name, relative in SOURCES.items():
                source = args.workspace_root.expanduser().resolve() / relative
                if ET.parse(source / 'package.xml').getroot().findtext('name') != name:
                    raise ValueError(f'package name mismatch: {source}')
                sha = snapshot(source, work / 'sources' / name)
                manifest['packages'][name] = {'sha256': sha,
                    'commit': output(['git', '-C', source, 'rev-parse', 'HEAD']),
                    'dirty': bool(output(['git', '-C', source, 'status', '--porcelain']))}
            release_adjustments(work / 'sources')
            for name, (url, branch) in NATIVE.items():
                source = work / 'native-src' / name
                if not source.exists():
                    run(['git', 'clone', '--depth', '1', '--branch', branch, url, source])
                manifest['native'][name] = {'url': url, 'commit': output(['git', '-C', source, 'rev-parse', 'HEAD'])}
            write(work / 'inputs.json', json.dumps(manifest, indent=2))
        (work / 'install').mkdir(exist_ok=True)
        (work / 'home').mkdir(exist_ok=True)
        command = ['docker', 'run', '--rm', '--user', f'{os.getuid()}:{os.getgid()}',
                   '-e', 'HOME=/release/home', '-v', f'{work}:/release',
                   '-v', f'{work}/install:{PREFIX}', '-v', f'{ROOT}/scripts:/tools:ro',
                   IMAGE, 'python3', '/tools/package_navigation.py', '--inside',
                   '--version', args.version, '-j', args.jobs]
        if args.package_only:
            command += ['--package-only']
        elif args.select:
            command += ['--select', *args.select]
        run(command)
        print(f'[artifact] {work}/dist/navigation_{args.version}_amd64.deb', flush=True)


if __name__ == '__main__':
    try:
        main()
    except (OSError, ValueError, ET.ParseError, subprocess.CalledProcessError) as exc:
        sys.exit(f'[error] {exc}')
