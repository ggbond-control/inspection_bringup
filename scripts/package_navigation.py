#!/usr/bin/env python3
"""Build control-driver/control-navigation debs using persistent Docker caches."""

import argparse
import copy
import base64
from contextlib import contextmanager
import fcntl
import getpass
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
PACKAGE_NAME = 'control-navigation'
DRIVER_PREFIX = Path('/opt/inspection/driver')
CPU_FLAGS = {
    'amd64': '-march=x86-64 -mtune=generic',
    'arm64': '-march=armv8-a -mtune=generic',
}
SOURCES = {
    'inspection_interfaces': 'inspection_interfaces',
    'robots_dog_msgs': 'robots_dog_msgs',
    'control_input_msgs': 'control_intput_msgs',
    'livox_ros_driver2': 'livox_ros_driver2',
    'gnss_driver': 'gnss_driver',
    'nav_bridge': 'nav_bridge',
    'ndt_omp': 'ndt_omp',
    'faster_lio': 'faster_slam',
    'gridmapper': 'gridmapper',
    'local_planner': 'local_planner',
    'multi_map_nav': 'multi_map_nav_ros2',
    'smac_straight_planner': 'smac_straight_planner',
}
NATIVE = {
    'gtsam': ('https://github.com/borglab/gtsam.git', '4.2.0'),
    'livox_sdk2': ('https://github.com/ggbond-control/livox_sdk2.git', 'jazzy'),
}
ALL_SOURCES = dict(SOURCES)
ALL_NATIVE = dict(NATIVE)
DRIVER_PACKAGES = {'inspection_interfaces', 'robots_dog_msgs', 'control_input_msgs',
                   'livox_ros_driver2', 'gnss_driver', 'ndt_omp', 'nav_bridge',
                   'uniubi', 'uniubi_motion_client'}
EMBEDDED_SOURCES = {'uniubi': 'uniubi_robot_msgs/ros2',
                    'uniubi_motion_client': 'uniubi_motion_client'}
COMPONENT = 'navigation'
NAV_BRIDGE_OPTIONS = [f'-DNAV_BRIDGE_BUILD_{name}=ON'
                      for name in ('X30', 'D1_MAX', 'CYVET', 'TANK', 'R750')]
RUNTIME_DEPENDS = [
    'ros-jazzy-ros-base', 'ros-jazzy-navigation2', 'ros-jazzy-grid-map',
    'ros-jazzy-pcl-ros', 'ros-jazzy-tf-transformations',
    'ros-jazzy-example-interfaces',
    'ros-jazzy-rmw-fastrtps-cpp', 'ros-jazzy-rmw-cyclonedds-cpp',
    'python3-yaml', 'python3-numpy', 'python3-scipy', 'python3-serial',
]
ARM64_RUNTIME_DEPENDS = [
    'opencl-headers', 'ocl-icd-opencl-dev', 'mesa-opencl-icd', 'clinfo',
]


def configure_component(component):
    global COMPONENT, PREFIX, PACKAGE_NAME, SOURCES, NATIVE
    COMPONENT = component
    PREFIX = DRIVER_PREFIX if component == 'driver' else Path('/opt/inspection/navigation')
    PACKAGE_NAME = 'control-' + component
    SOURCES = {name: repo for name, repo in ALL_SOURCES.items()
               if (name in DRIVER_PACKAGES) == (component == 'driver')}
    NATIVE = dict(ALL_NATIVE) if component == 'driver' else {}


def prepare_bridge_sources(work, manifest):
    root = work / 'sources/nav_bridge/third_party/uniubi'
    for name, relative in EMBEDDED_SOURCES.items():
        target = work / 'sources' / name
        if target.exists():
            shutil.rmtree(target)
        shutil.copytree(root / relative, target, dirs_exist_ok=True)
        SOURCES[name] = None
        hashes = [(str(p.relative_to(root / relative)), digest(p))
                  for p in sorted((root / relative).rglob('*')) if p.is_file()]
        if name == 'uniubi':
            hashes += [(str(p.relative_to(root)), digest(p))
                       for p in sorted((root / 'uniubi_robot_msgs/idl').rglob('*')) if p.is_file()]
        manifest['packages'][name] = dict(manifest['packages']['nav_bridge'], embedded_path=relative,
            sha256=hashlib.sha256(json.dumps(hashes).encode()).hexdigest())
    if (work / 'sources/idl').exists():
        shutil.rmtree(work / 'sources/idl')
    shutil.copytree(root / 'uniubi_robot_msgs/idl', work / 'sources/idl')
    # nav_bridge's optional Cyvet dependencies are not declared in its manifest.
    path = work / 'sources/nav_bridge/package.xml'
    tree = ET.parse(path)
    for name in EMBEDDED_SOURCES:
        if not any(v.text == name for v in tree.getroot() if 'depend' in v.tag):
            ET.SubElement(tree.getroot(), 'depend').text = name
    write(path, ET.tostring(tree.getroot(), encoding='unicode'))


def run(args, **kwargs):
    print('+ ' + ' '.join(str(a) for a in args), flush=True)
    return subprocess.run([str(a) for a in args], check=True, **kwargs)


def output(args, **kwargs):
    return subprocess.check_output([str(a) for a in args], text=True, **kwargs).strip()


def host_architecture():
    architecture = output(['dpkg', '--print-architecture'])
    if architecture not in CPU_FLAGS:
        raise ValueError(f'unsupported native architecture: {architecture}; use amd64 or arm64')
    return architecture


def builder_image(architecture):
    return f'inspection-control-builder:jazzy-{architecture}'


def check_cache_architecture(work, architecture):
    for filename in ('inputs.json', 'build-state.json'):
        path = work / filename
        if path.exists() and json.loads(path.read_text()).get('architecture') != architecture:
            raise ValueError(f'{filename}: cache architecture differs from {architecture}; '
                             'use a separate --work-dir for each architecture')


def write(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists() or path.read_text() != text:
        path.write_text(text)


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def version_tuple(tag):
    match = re.fullmatch(r'v?(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)', tag)
    return tuple(map(int, match.groups())) if match else None


def resolve_revision(listing, requested):
    """Select exact tag, nearest lower stable version, or a tagless default HEAD."""
    tags, peeled = {}, {}
    default_ref, head = None, None
    for line in listing.splitlines():
        value, ref = line.split('\t', 1)
        if value.startswith('ref: ') and ref == 'HEAD':
            default_ref = value[5:]
        elif ref == 'HEAD':
            head = value
        elif ref.startswith('refs/tags/'):
            tag = ref[len('refs/tags/'):]
            if tag.endswith('^{}'):
                peeled[tag[:-3]] = value
            else:
                tags[tag] = value
    if not tags:
        if not default_ref or not head:
            raise ValueError('tagless repository has no advertised default branch HEAD')
        return {'ref': default_ref, 'branch': default_ref.removeprefix('refs/heads/'),
                'tag': None, 'commit': head, 'selection': 'default-branch'}
    if requested in tags:
        chosen = requested
        selection = 'exact-tag'
    else:
        target = version_tuple(requested)
        candidates = [(version_tuple(tag), tag) for tag in tags
                      if version_tuple(tag) is not None and version_tuple(tag) <= target]
        if not candidates:
            raise ValueError(f'no version tag <= {requested}; repository has tags, '
                             'so default-branch fallback is disabled')
        _, chosen = max(candidates)
        selection = 'lower-tag'
    return {'ref': 'refs/tags/' + chosen, 'tag': chosen, 'branch': None,
            'commit': peeled.get(chosen, tags[chosen]), 'selection': selection}


@contextmanager
def github_access():
    if not sys.stdin.isatty():
        raise ValueError('run in an interactive terminal to enter the GitHub token securely')
    token = getpass.getpass('GitHub read-only token (hidden): ').strip()
    if not token or any(c.isspace() for c in token):
        raise ValueError('a nonempty GitHub token without whitespace is required')
    encoded = base64.b64encode(('x-access-token:' + token).encode()).decode()
    # Git consumes credentials only from this child environment, never argv/config.
    env = {k: v for k, v in os.environ.items() if not k.startswith('GIT_CONFIG_')}
    env.update(GIT_CONFIG_COUNT='3', GIT_CONFIG_KEY_0='credential.helper',
               GIT_CONFIG_VALUE_0='', GIT_CONFIG_KEY_1='http.https://github.com/.extraheader',
               GIT_CONFIG_VALUE_1='Authorization: Basic ' + encoded,
               GIT_CONFIG_KEY_2='core.hooksPath', GIT_CONFIG_VALUE_2='/dev/null',
               GIT_TERMINAL_PROMPT='0', GIT_ASKPASS='/bin/false',
               GIT_CONFIG_NOSYSTEM='1', GIT_CONFIG_GLOBAL='/dev/null')
    for key in list(env):
        if key.startswith(('GIT_TRACE', 'GIT_CURL_VERBOSE')):
            del env[key]
    try:
        yield env
    finally:
        env.clear()


def remote_git(args, env):
    result = subprocess.run(['git', *map(str, args)], env=env, text=True,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if result.returncode:
        # Git diagnostics can contain authentication data. Do not relay them.
        raise ValueError('remote Git operation failed; check token read access, repository URL '
                         'and network (credentials and Git diagnostics suppressed)')
    return result.stdout.strip()


def fetch_source(url, requested, destination, env):
    listing = remote_git(['ls-remote', '--symref', url, 'HEAD', 'refs/tags/*'], env)
    try:
        revision = resolve_revision(listing, requested)
    except ValueError as exc:
        raise ValueError(f'{url}: {exc}') from exc
    print(f'[source] {url}: {requested} -> {revision["tag"] or revision["branch"]} '
          f'({revision["selection"]}, {revision["commit"][:12]})', flush=True)
    if not destination.exists():
        run(['git', 'init', '-q', destination])
    if not (destination / '.git').is_dir():
        raise ValueError(f'not a managed Git source cache: {destination}')
    # Fetch only into our local cache; no remote mutation or developer checkout.
    remote_git(['-C', destination, 'fetch', '--no-tags', '--depth', '1', url, revision['ref']], env)
    actual = output(['git', '-C', destination, 'rev-parse', 'FETCH_HEAD^{commit}'])
    if actual != revision['commit']:
        raise ValueError(f'{url}: remote ref moved during fetch; retry to resolve a consistent revision')
    remote_git(['-C', destination, 'checkout', '--detach', '--force', actual], env)
    if (destination / '.gitmodules').exists():
        raise ValueError(f'{url}: submodules require explicit release support')
    return dict(revision, url=url, requested_tag=requested)


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
        if package not in SOURCES:
            continue
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
    if 'ndt_omp' not in SOURCES:
        return
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
    if COMPONENT == 'navigation':
        code += f'source {DRIVER_PREFIX}/local_setup.bash; '
    if (PREFIX / 'local_setup.bash').is_file():
        code += f'source {PREFIX}/local_setup.bash; '
    code += 'exec /usr/bin/python3 -c "import os,json; print(json.dumps(dict(os.environ)))"'
    env = json.loads(output(['bash', '-ec', code]).splitlines()[-1])
    vendor = (DRIVER_PREFIX if COMPONENT == 'navigation' else PREFIX) / 'vendor'
    for key, value in [('CMAKE_PREFIX_PATH', str(vendor)),
                       ('LD_LIBRARY_PATH', f'{vendor}/lib:{PREFIX}/lib')]:
        env[key] = value + ':' + env.get(key, '')
    return env


def build_native(work, jobs):
    inputs = json.loads((work / 'inputs.json').read_text())
    flags = CPU_FLAGS[inputs['architecture']]
    state = work / 'build-state.json'
    previous = json.loads(state.read_text()) if state.exists() else {}
    for name in NATIVE:
        stamp = work / f'{name}.installed'
        commit = inputs['native'][name]['commit']
        artifact = PREFIX / 'vendor/lib' / ('libgtsam.so' if name == 'gtsam'
                                           else 'liblivox_lidar_sdk_shared.so')
        if stamp.exists() and artifact.is_file():
            installed = stamp.read_text().strip()
            legacy = (installed == 'complete' and
                      previous.get('native', {}).get(name, {}).get('commit') == commit)
            if installed == commit or legacy:
                write(stamp, commit + '\n')
                print(f'[cache] reuse {name} {commit[:12]}', flush=True)
                continue
        stamp.unlink(missing_ok=True)
        args = ['-DCMAKE_BUILD_TYPE=Release', f'-DCMAKE_INSTALL_PREFIX={PREFIX}/vendor',
                '-DCMAKE_INSTALL_LIBDIR=lib', f'-DCMAKE_C_FLAGS={flags}',
                f'-DCMAKE_CXX_FLAGS={flags}',
                '-DCMAKE_INSTALL_RPATH=$ORIGIN']
        if name == 'gtsam':
            args += ['-DGTSAM_USE_SYSTEM_EIGEN=ON', '-DGTSAM_BUILD_TESTS=OFF',
                     '-DGTSAM_BUILD_EXAMPLES_ALWAYS=OFF', '-DGTSAM_BUILD_UNSTABLE=ON',
                     '-DGTSAM_BUILD_WITH_MARCH_NATIVE=OFF']
        build = work / 'native-build' / name
        run(['cmake', '-S', work / 'native-src' / name, '-B', build, *args])
        run(['cmake', '--build', build, '-j', jobs])
        run(['cmake', '--install', build])
        write(stamp, commit + '\n')


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


def driver_build_id(manifest):
    driver = manifest.get('driver', {})
    return driver.get('build_id', driver.get('sha256'))


def assemble(work, version, manifest):
    architecture = manifest['architecture']
    manifest = dict(manifest, version=version, component=COMPONENT, install_prefix=str(PREFIX),
                    packaging_recipe_sha256=digest(Path(__file__)))
    stage = work / 'stage'
    if stage.exists():
        shutil.rmtree(stage)
    dest = stage / PREFIX.relative_to('/')
    shutil.copytree(PREFIX, dest, symlinks=True)
    # Driver exports form the navigation build underlay; navigation is runtime-only.
    for p in sorted(dest.rglob('*'), reverse=True):
        if p.is_file() and ((p.suffix == '.a' and COMPONENT != 'driver') or
                            p.suffix == '.pyc' or p.name == 'COLCON_IGNORE'):
            p.unlink()
        elif p.is_dir() and (p.name == '__pycache__' or (COMPONENT != 'driver' and
                            (p.name == 'include' or
                             (p.name == 'cmake' and p.parent.parent == dest / 'share')))):
            shutil.rmtree(p)
    for p in ([] if COMPONENT == 'driver' else [dest / 'vendor/share', dest / 'vendor/lib/cmake',
              dest / 'vendor/lib/pkgconfig', dest / 'share/multi_map_nav/maps']):
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
                run(['patchelf', '--set-rpath',
                     f'{PREFIX}/lib:{DRIVER_PREFIX}/lib:{DRIVER_PREFIX}/vendor/lib:/opt/ros/jazzy/lib', p],
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
    if COMPONENT == 'driver':
        for model in ('x30', 'd1_max', 'cyvet', 'tank', 'r750'):
            if not (dest / f'lib/nav_bridge/{model}_nav_bridge_node').is_file():
                raise ValueError(f'missing nav_bridge backend: {model}')
    docs = stage / 'usr/share/doc' / PACKAGE_NAME
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
    write(dest / 'share/control_release/manifest.json', json.dumps(manifest, indent=2) + '\n')
    write(docs / 'README', 'Source /opt/ros/jazzy/setup.bash, then '
          f'{DRIVER_PREFIX}/local_setup.bash, then /opt/inspection/navigation/local_setup.bash '
          'when navigation is installed. No services are installed or started.\n'
          'Runtime defaults are package resources; supply actual device settings externally.\n'
          'Data defaults: ${ROS_HOME:-$HOME/.ros}/navigation/<package>.\n')
    debian = work / 'dependency-scan/debian'
    write(debian / 'control', f'Source: {PACKAGE_NAME}\nSection: misc\nPriority: optional\n'
          'Maintainer: Navigation maintainers <robot@example.com>\n\n'
          f'Package: {PACKAGE_NAME}\nArchitecture: {architecture}\nDescription: Navigation runtime\n')
    args = ['dpkg-shlibdeps', '-O', '--ignore-missing-info',
            f'-l{dest}/lib', f'-l{dest}/vendor/lib', f'-l{DRIVER_PREFIX}/lib',
            f'-l{DRIVER_PREFIX}/vendor/lib', '-l/opt/ros/jazzy/lib']
    result = output([*args, *['-e' + str(p) for p in elfs]], cwd=debian.parent) if elfs else ''
    depends = next((line.split('=', 1)[1] for line in result.splitlines()
                    if line.startswith('shlibs:Depends=')), '')
    runtime = list(RUNTIME_DEPENDS)
    if COMPONENT == 'driver':
        runtime = [v for v in runtime if v not in ('ros-jazzy-navigation2', 'ros-jazzy-grid-map')]
        runtime += ['ros-jazzy-rviz-2d-overlay-msgs', 'ros-jazzy-cyclonedds']
    else:
        runtime += [f'control-driver (>= {manifest["driver_min_version"]})',
                    f'control-driver (<< {manifest["driver_max_version"]})']
        if architecture == 'arm64':
            runtime += ARM64_RUNTIME_DEPENDS
    depends = ', '.join(filter(None, [depends, *runtime]))
    control = stage / 'DEBIAN'
    control.mkdir()
    installed_kib = sum(p.stat().st_size for p in dest.rglob('*') if p.is_file()) // 1024
    write(control / 'control', f'Package: {PACKAGE_NAME}\nVersion: {version}\nArchitecture: {architecture}\n'
          'Maintainer: Navigation maintainers <robot@example.com>\nSection: misc\nPriority: optional\n'
          f'Installed-Size: {installed_kib}\nDepends: {depends}\nRecommends: ros-jazzy-rviz2\n'
          'Conflicts: navigation, fhzn-navigation\nReplaces: navigation, fhzn-navigation\n'
          'Description: Model-independent ROS 2 Jazzy navigation runtime\n'
          ' Localization, mapping, planning, sensor drivers and custom interfaces.\n'
          ' Excludes inspection_bringup, maps and system services.\n')
    dist = work / 'dist'
    dist.mkdir(exist_ok=True)
    deb = dist / f'{PACKAGE_NAME}_{version}_{architecture}.deb'
    run(['dpkg-deb', '--root-owner-group', '-Zxz', '-z3', '--build', stage, deb])
    write(dist / (deb.name + '.sha256'), digest(deb) + '  ' + deb.name + '\n')
    write(dist / (deb.name + '.manifest.json'), json.dumps(manifest, indent=2) + '\n')
    print(f'[done] {deb}', flush=True)


def inside(args):
    work = Path('/release')
    check_cache_architecture(work, host_architecture())
    manifest = json.loads((work / 'inputs.json').read_text())
    if COMPONENT == 'driver':
        SOURCES.update({name: None for name in EMBEDDED_SOURCES})
    flags = CPU_FLAGS[manifest['architecture']]
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
    if COMPONENT == 'navigation' and previous and driver_build_id(previous) != driver_build_id(manifest):
        if args.select:
            raise ValueError('driver underlay changed; run without --select to rebuild navigation consumers')
        if (work / 'build').exists():
            shutil.rmtree(work / 'build')
    if args.select and any(previous.get('native', {}).get(n, {}).get('commit') !=
                           manifest['native'][n]['commit'] for n in NATIVE):
        raise ValueError('native dependency revision changed; run without --select '
                         'to rebuild dependencies and all managed consumers')
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
    if NATIVE:
        build_native(work, args.jobs)
    env = environment()
    env['MAKEFLAGS'] = f'-j{args.jobs}'
    env['CMAKE_BUILD_PARALLEL_LEVEL'] = str(args.jobs)
    run(['colcon', '--log-base', work / 'log', 'build', '--merge-install',
         '--base-paths', work / 'sources', '--packages-select', *selected,
         '--build-base', work / 'build', '--install-base', PREFIX,
         '--executor', 'sequential', '--event-handlers', 'console_cohesion+',
         '--cmake-args', '-DCMAKE_BUILD_TYPE=Release', '-DBUILD_TESTING=OFF',
         '-DBUILD_WITH_MARCH_NATIVE=OFF', f'-DCMAKE_C_FLAGS={flags}',
         f'-DCMAKE_CXX_FLAGS={flags}',
         '-DPython3_EXECUTABLE=/usr/bin/python3', '-DPYTHON_EXECUTABLE=/usr/bin/python3',
         *(NAV_BRIDGE_OPTIONS if COMPONENT == 'driver' else []),
         f'-DGTSAM_DIR={DRIVER_PREFIX}/vendor/lib/cmake/GTSAM',
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
    p.add_argument('--component', choices=('driver', 'navigation', 'all'), default='all')
    p.add_argument('--version', help='default Debian version for selected components')
    p.add_argument('--tag', help='requested ggbond-control release tag, e.g. v0.3.5')
    for component in ('driver', 'navigation'):
        p.add_argument(f'--{component}-version', help='component version; overrides --version')
        p.add_argument(f'--{component}-tag', help='component source tag; overrides --tag')
    p.add_argument('--driver-deb', type=Path, help='driver build underlay for navigation-only builds')
    p.add_argument('--driver-min-version', help='inclusive driver dependency lower bound; defaults to build driver version')
    p.add_argument('--driver-max-version', help='exclusive driver dependency upper bound; defaults to next major (next minor for 0.x)')
    p.add_argument('--work-dir', type=Path, help='persistent cache; defaults to ~/.cache/inspection-navigation-deb/<host architecture>')
    p.add_argument('-j', '--jobs', type=int, default=1)
    mode = p.add_mutually_exclusive_group()
    mode.add_argument('--select', nargs='+', choices=list(ALL_SOURCES) + list(EMBEDDED_SOURCES), help='rebuild selected packages and their consumers')
    mode.add_argument('--package-only', action='store_true', help='repackage last complete build, without compiling')
    p.add_argument('--rebuild-image', action='store_true', help='rebuild cached Docker build environment')
    p.add_argument('--inside', action='store_true', help=argparse.SUPPRESS)
    args = p.parse_args()
    if args.jobs < 1:
        p.error('--jobs must be positive')
    if args.inside and args.component == 'all':
        p.error('--inside requires a single component')
    components = ('driver', 'navigation') if args.component == 'all' else (args.component,)
    for component in components:
        version = getattr(args, component + '_version') or args.version
        if not version or not re.fullmatch(r'[0-9][A-Za-z0-9.+~:-]*', version):
            p.error(f'valid --version or --{component}-version is required')
        tag = getattr(args, component + '_tag') or args.tag
        if not args.inside and not args.package_only and not tag:
            p.error('--tag is required unless using --package-only (or use component-specific tags)')
    for tag in (args.tag, args.driver_tag, args.navigation_tag):
        if tag and version_tuple(tag) is None:
            p.error('--tag must be a stable version such as v0.3.5 (or 0.3.5)')
    if args.package_only and any((args.tag, args.driver_tag, args.navigation_tag)):
        p.error('--package-only uses the previous source snapshot; omit --tag')
    if args.package_only and (args.driver_min_version or args.driver_max_version):
        p.error('--package-only preserves cached driver compatibility bounds')
    if args.driver_deb and (args.component != 'navigation' or args.package_only):
        p.error('--driver-deb is only for navigation source builds')
    if args.component == 'navigation' and not args.package_only and not args.inside and not args.driver_deb:
        p.error('--component navigation requires --driver-deb')
    if args.select and args.component != 'all':
        allowed = DRIVER_PACKAGES if args.component == 'driver' else set(ALL_SOURCES) - DRIVER_PACKAGES
        if set(args.select) - allowed:
            p.error('--select contains packages outside the selected component')
    for version in (args.driver_min_version, args.driver_max_version):
        if version and not re.fullmatch(r'[0-9][A-Za-z0-9.+~:-]*', version):
            p.error('invalid driver compatibility version')
    return args


def driver_underlay(args, work, architecture):
    deb = args.driver_deb.expanduser().resolve()
    if output(['dpkg-deb', '-f', deb, 'Package']) != 'control-driver':
        raise ValueError('--driver-deb must be a control-driver package')
    if output(['dpkg-deb', '-f', deb, 'Architecture']) != architecture:
        raise ValueError('driver deb architecture differs from build host')
    version = output(['dpkg-deb', '-f', deb, 'Version'])
    minimum = args.driver_min_version or version
    maximum = args.driver_max_version
    if not maximum:
        match = re.match(r'^(\d+)\.(\d+)\.', version)
        if not match:
            raise ValueError('specify --driver-max-version for non-semver driver versions')
        major, minor = map(int, match.groups())
        maximum = f'{major + 1}.0.0' if major else f'0.{minor + 1}.0'
    for left, op, right in [(version, 'ge', minimum), (version, 'lt', maximum)]:
        if subprocess.run(['dpkg', '--compare-versions', left, op, right]).returncode:
            raise ValueError(f'build driver {version} is outside [{minimum}, {maximum})')
    directory = work / 'driver-underlay'
    checksum = digest(deb)
    stamp = work / 'driver-underlay.sha256'
    if not stamp.exists() or stamp.read_text().strip() != checksum:
        if directory.exists():
            shutil.rmtree(directory)
        directory.parent.mkdir(parents=True, exist_ok=True)
        run(['dpkg-deb', '-x', deb, directory])
        write(stamp, checksum)
    if not (directory / DRIVER_PREFIX.relative_to('/') / 'local_setup.bash').is_file():
        raise ValueError('driver deb does not contain its build underlay')
    provenance = json.loads((directory / DRIVER_PREFIX.relative_to('/') /
                             'share/control_release/manifest.json').read_text())
    identity = {key: provenance.get(key) for key in
                ('architecture', 'packages', 'native', 'builder_image',
                 'release_recipe_sha256', 'packaging_recipe_sha256')}
    build_id = hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()
    return {'version': version, 'sha256': checksum, 'build_id': build_id}, minimum, maximum


def build_component(args):
    architecture = host_architecture()
    image_name = builder_image(architecture)
    platform = f'linux/{architecture}'
    work = (args.work_dir or Path.home() / '.cache/inspection-navigation-deb' / architecture).expanduser().resolve() / COMPONENT
    work.mkdir(parents=True, exist_ok=True)
    with (work / '.lock').open('w') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        check_cache_architecture(work, architecture)
        image = subprocess.run(['docker', 'image', 'inspect', image_name], stdout=subprocess.DEVNULL,
                               stderr=subprocess.DEVNULL).returncode == 0
        if args.rebuild_image or not image:
            run(['docker', 'build', '--platform', platform, '-t', image_name, '-f', ROOT / 'packaging/navigation/Dockerfile',
                 ROOT / 'packaging/navigation'])
        image_architecture = output(['docker', 'image', 'inspect', '--format', '{{.Architecture}}', image_name])
        if image_architecture != architecture:
            raise ValueError(f'{image_name} contains {image_architecture}, expected {architecture}; rebuild the image')
        if not args.package_only:
            manifest = {'builder_image': output(['docker', 'image', 'inspect', '--format', '{{.Id}}', image_name]),
                        'packages': {}, 'native': {}, 'architecture': architecture, 'ros': 'jazzy',
                        'release_recipe_sha256': digest(Path(__file__))}
            manifest['requested_tag'] = args.tag
            manifest['component'] = COMPONENT
            if COMPONENT == 'navigation':
                driver, minimum, maximum = driver_underlay(args, work, architecture)
                manifest.update(driver=driver, driver_min_version=minimum, driver_max_version=maximum)
            with github_access() as git_env:
                for name, repository in SOURCES.items():
                    source = work / 'repositories' / name
                    revision = fetch_source(f'https://github.com/ggbond-control/{repository}.git',
                                            args.tag, source, git_env)
                    if ET.parse(source / 'package.xml').getroot().findtext('name') != name:
                        raise ValueError(f'package name mismatch: {source}')
                    sha = snapshot(source, work / 'sources' / name)
                    manifest['packages'][name] = dict(revision, sha256=sha, dirty=False)
                release_adjustments(work / 'sources')
                if COMPONENT == 'driver':
                    prepare_bridge_sources(work, manifest)
                for name, (url, fixed_ref) in NATIVE.items():
                    source = work / 'native-src' / name
                    if url.startswith('https://github.com/ggbond-control/'):
                        revision = fetch_source(url, args.tag, source, git_env)
                    else:
                        if not source.exists():
                            run(['git', 'clone', '--depth', '1', '--branch', fixed_ref, url, source])
                        commit = output(['git', '-C', source, 'rev-parse', 'HEAD'])
                        fixed_commit = output(['git', '-C', source, 'rev-parse', fixed_ref + '^{commit}'])
                        if commit != fixed_commit:
                            raise ValueError(f'{name}: cached checkout does not match pinned {fixed_ref}')
                        revision = {'url': url, 'tag': fixed_ref, 'commit': commit,
                                    'selection': 'fixed-upstream-tag'}
                    manifest['native'][name] = revision
            write(work / 'inputs.json', json.dumps(manifest, indent=2))
        (work / 'install').mkdir(exist_ok=True)
        (work / 'home').mkdir(exist_ok=True)
        command = ['docker', 'run', '--rm', '--platform', platform, '--user', f'{os.getuid()}:{os.getgid()}',
                   '-e', 'HOME=/release/home', '-v', f'{work}:/release',
                   '-v', f'{work}/install:{PREFIX}', '-v', f'{ROOT}/scripts:/tools:ro',
                   *(['-v', f'{work}/driver-underlay{DRIVER_PREFIX}:{DRIVER_PREFIX}:ro']
                     if COMPONENT == 'navigation' else []),
                   image_name, 'python3', '/tools/package_navigation.py', '--inside', '--component', COMPONENT,
                   '--version', args.version, '-j', args.jobs]
        if args.package_only:
            command += ['--package-only']
        elif args.select:
            command += ['--select', *args.select]
        run(command)
        print(f'[artifact] {work}/dist/{PACKAGE_NAME}_{args.version}_{architecture}.deb', flush=True)
        return work / 'dist' / f'{PACKAGE_NAME}_{args.version}_{architecture}.deb'


def main():
    args = arguments()
    if args.inside:
        configure_component(args.component)
        inside(args)
        return
    components = ('driver', 'navigation') if args.component == 'all' else (args.component,)
    driver_deb = args.driver_deb
    for component in components:
        configure_component(component)
        current = copy.copy(args)
        current.component = component
        current.version = getattr(args, component + '_version') or args.version
        current.tag = getattr(args, component + '_tag') or args.tag
        current.driver_deb = driver_deb
        if args.select:
            current.select = [n for n in args.select if n in SOURCES or
                              (component == 'driver' and n in EMBEDDED_SOURCES)]
            if component == 'navigation' and set(args.select) & DRIVER_PACKAGES:
                current.select = None  # Rebuild all navigation consumers of changed driver inputs.
            elif not current.select:
                current.package_only = True
        artifact = build_component(current)
        if component == 'driver':
            driver_deb = artifact


if __name__ == '__main__':
    try:
        main()
    except (OSError, ValueError, ET.ParseError, subprocess.CalledProcessError) as exc:
        sys.exit(f'[error] {exc}')
