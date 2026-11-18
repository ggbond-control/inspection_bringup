#!/usr/bin/env python3
"""Build control-driver/control-navigation/control-task debs using persistent Docker caches."""

import argparse
import copy
import base64
from contextlib import contextmanager, ExitStack, nullcontext
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
import yaml

ROOT = Path(__file__).resolve().parents[1]
CPU_FLAGS = {
    'amd64': '-march=x86-64 -mtune=generic',
    'arm64': '-march=armv8-a -mtune=generic',
}
def load_package_catalog(path):
    class UniqueLoader(yaml.SafeLoader):
        pass

    def mapping(loader, node):
        result = {}
        for key_node, value_node in node.value:
            key = loader.construct_object(key_node, deep=True)
            if key in result:
                raise ValueError(f'packages.yaml: duplicate YAML key: {key}')
            result[key] = loader.construct_object(value_node, deep=True)
        return result

    UniqueLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, mapping)
    with path.open() as stream:
        catalog = yaml.load(stream, Loader=UniqueLoader)
    if not isinstance(catalog, dict) or catalog.get('schema_version') != 1:
        raise ValueError('packages.yaml: expected schema_version: 1')
    components = catalog.get('components')
    if not isinstance(components, dict) or set(components) != {'driver', 'navigation', 'task'}:
        raise ValueError('packages.yaml: expected driver, navigation and task components')
    if not isinstance(catalog.get('repository_base'), str) or not catalog['repository_base'].startswith('https://'):
        raise ValueError('packages.yaml: repository_base must be an HTTPS URL')
    owners, debs, prefixes = set(), set(), set()
    for component, spec in components.items():
        if not isinstance(spec, dict):
            raise ValueError(f'{component}: expected a mapping')
        for key in ('deb_name', 'prefix', 'image', 'dockerfile', 'recipe_revision'):
            if not isinstance(spec.get(key), str) or not spec[key]:
                raise ValueError(f'{component}: missing {key}')
        if not re.fullmatch(r'[a-z0-9][a-z0-9+.-]+', spec['deb_name']):
            raise ValueError(f'{component}: invalid Debian package name')
        prefix = Path(spec['prefix'])
        if not prefix.is_relative_to('/opt/inspection') or '..' in prefix.parts or prefix == Path('/opt/inspection'):
            raise ValueError(f'{component}: invalid install prefix')
        if spec['deb_name'] in debs or any(prefix == p or prefix.is_relative_to(p) or p.is_relative_to(prefix) for p in prefixes):
            raise ValueError(f'{component}: duplicate deb name or overlapping prefix')
        debs.add(spec['deb_name'])
        prefixes.add(prefix)
        dockerfile = Path(spec['dockerfile'])
        if dockerfile.is_absolute() or '..' in dockerfile.parts:
            raise ValueError(f'{component}: invalid Dockerfile path')
        spec['image'].format(architecture='amd64')
        for key in ('packages', 'embedded_packages'):
            entries = spec.get(key, {})
            if not isinstance(entries, dict) or (key == 'packages' and not entries):
                raise ValueError(f'{component}: {key} must be a mapping')
            for name, source in entries.items():
                if not isinstance(name, str) or not re.fullmatch(r'[a-z][a-z0-9_]*', name):
                    raise ValueError(f'{component}: invalid ROS package name')
                if name in owners:
                    raise ValueError(f'duplicate package ownership: {name}')
                owners.add(name)
                if not isinstance(source, str) or not source or Path(source).is_absolute() or '..' in Path(source).parts:
                    raise ValueError(f'{name}: invalid source path')
                if key == 'packages' and not re.fullmatch(r'[A-Za-z0-9_.-]+', source):
                    raise ValueError(f'{name}: expected repository name, not a URL/path')
            if key == 'embedded_packages' and entries and component != 'driver':
                raise ValueError('embedded packages currently require the driver nav_bridge adapter')
        for key in ('cmake_args', 'runtime_dependencies'):
            if not isinstance(spec.get(key, []), list) or not all(isinstance(v, str) for v in spec.get(key, [])):
                raise ValueError(f'{component}: {key} must be a string list')
        for architecture, dependencies in spec.get('architecture_dependencies', {}).items():
            if architecture not in CPU_FLAGS or not isinstance(dependencies, list) or not all(isinstance(v, str) for v in dependencies):
                raise ValueError(f'{component}: invalid architecture dependencies')
        for name, native in spec.get('native_libraries', {}).items():
            if not isinstance(native, dict) or not isinstance(native.get('url'), str) or not native['url'].startswith('https://') or not isinstance(native.get('ref'), str):
                raise ValueError(f'{name}: expected native library HTTPS url and ref')
    return catalog


CATALOG_PATH = Path(os.environ.get('INSPECTION_PACKAGING_CATALOG', ROOT / 'packaging/packages.yaml'))
CATALOG = load_package_catalog(CATALOG_PATH)
COMPONENT_CONFIG = CATALOG['components']
COMPONENTS = ('driver', 'navigation', 'task')
ALL_SOURCES = {name: repo for spec in COMPONENT_CONFIG.values() for name, repo in spec['packages'].items()}
TASK_SOURCES = dict(COMPONENT_CONFIG['task']['packages'])
NAVIGATION_PACKAGES = set(COMPONENT_CONFIG['navigation']['packages'])
EMBEDDED_SOURCES = dict(COMPONENT_CONFIG['driver'].get('embedded_packages', {}))
DRIVER_PACKAGES = set(COMPONENT_CONFIG['driver']['packages']) | EMBEDDED_SOURCES.keys()
DRIVER_PREFIX = Path(COMPONENT_CONFIG['driver']['prefix'])
ALL_NATIVE = {name: (spec['url'], spec['ref'])
              for name, spec in COMPONENT_CONFIG['driver'].get('native_libraries', {}).items()}
NAV_BRIDGE_OPTIONS = COMPONENT_CONFIG['driver'].get('cmake_args', [])
ARM64_RUNTIME_DEPENDS = COMPONENT_CONFIG['navigation'].get('architecture_dependencies', {}).get('arm64', [])
COMPONENT = 'navigation'
PREFIX = Path(COMPONENT_CONFIG[COMPONENT]['prefix'])
PACKAGE_NAME = COMPONENT_CONFIG[COMPONENT]['deb_name']
SOURCES = dict(COMPONENT_CONFIG[COMPONENT]['packages'])
NATIVE = {}
GITHUB_SESSION = None


class GitHubSession:
    def __init__(self, stack):
        self.stack = stack
        self.env = None

    def access(self):
        if self.env is None:
            self.env = self.stack.enter_context(github_access())
        return nullcontext(self.env)


def configure_component(component):
    global COMPONENT, PREFIX, PACKAGE_NAME, SOURCES, NATIVE
    COMPONENT = component
    spec = COMPONENT_CONFIG[component]
    PREFIX = Path(spec['prefix'])
    PACKAGE_NAME = spec['deb_name']
    SOURCES = dict(spec['packages'])
    NATIVE = {name: (native['url'], native['ref']) for name, native in spec.get('native_libraries', {}).items()}


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


def run(args, *, quiet=False, **kwargs):
    if not quiet:
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
    return COMPONENT_CONFIG[COMPONENT]['image'].format(architecture=architecture)


def image_environment(image):
    """Ignore image metadata/history IDs; compare filesystem layers and process env."""
    info = json.loads(output(['docker', 'image', 'inspect', image]))[0]
    config = info.get('Config') or {}
    identity = {key: info.get(key) for key in ('Architecture', 'Os', 'RootFS')}
    identity['Env'] = config.get('Env', [])
    identity['User'] = config.get('User', '')
    identity['WorkingDir'] = config.get('WorkingDir', '')
    return hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()


def recipe_id():
    # Component configuration changes invalidate only that component's selection cache.
    spec = COMPONENT_CONFIG[COMPONENT]
    content = {'component': spec, 'repository_base': CATALOG['repository_base']}
    return spec['recipe_revision'] + ':' + hashlib.sha256(json.dumps(content, sort_keys=True).encode()).hexdigest()


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
    if COMPONENT == 'task':
        cmake = sources / 'sensor_gimbal/CMakeLists.txt'
        text = cmake.read_text()
        text = text.replace('HIK_SDK_LIB_DIR="${HKNETSDK_LIB_DIR}"',
                            'HIK_SDK_LIB_DIR="/opt/inspection/task/vendor/lib"')
        write(cmake, text)
        return
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
    if COMPONENT != 'driver':
        code += f'source {DRIVER_PREFIX}/local_setup.bash; '
    if (PREFIX / 'local_setup.bash').is_file():
        code += f'source {PREFIX}/local_setup.bash; '
    code += 'exec /usr/bin/python3 -c "import os,json; print(json.dumps(dict(os.environ)))"'
    env = json.loads(output(['bash', '-ec', code]).splitlines()[-1])
    vendor = (DRIVER_PREFIX if COMPONENT != 'driver' else PREFIX) / 'vendor'
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


def relocate_gtsam_exports(dest):
    for package in ('GTSAM', 'GTSAM_UNSTABLE'):
        path = dest / f'vendor/lib/cmake/{package}/{package}Config.cmake'
        if not path.is_file():
            continue
        text = path.read_text()
        pattern = (re.escape('if(EXISTS "${OUR_CMAKE_DIR}/CMakeCache.txt")') +
                   r'\n.*?\nelse\(\)\n(.*?)\nendif\(\)')
        match = re.search(pattern, text, re.DOTALL)
        if match:
            installed = match.group(1)
            expected = f'set({package}_INCLUDE_DIR "${{OUR_CMAKE_DIR}}/../../../include"'
            if expected not in installed or '/release/' in installed:
                raise ValueError(f'{package}: installed include layout changed; review release adaptation')
            write(path, text[:match.start()] + installed + text[match.end():])


def task_runtime_libraries(work, dest, architecture):
    vendor = dest / 'vendor/lib'
    cache = work / 'build/sensor_gimbal/CMakeCache.txt'
    features = {}
    if not cache.is_file():
        raise ValueError('sensor_gimbal CMake cache missing; complete the task build first')
    values = dict(re.findall(r'^([^#/:\n][^:\n]*):[^=\n]+=(.*)$', cache.read_text(), re.MULTILINE))
    # Match the AUTO detection in sensor_gimbal CMake, including host ELF type.
    sdk = work / 'sources/sensor_gimbal/third_party'
    groups = {
        'rknn': (sdk / 'rknn/lib', 'librknnrt.so'),
        'hknetsdk': (sdk / 'HCNetSDKV6.1.11.5_build20251204_ArmLinux64_ZH/MakeAll', 'libhcnetsdk.so'),
    }
    machine = {'amd64': 62, 'arm64': 183}[architecture]
    for name, (directory, library) in groups.items():
        path = directory / library
        enabled = False
        if path.is_file():
            header = path.read_bytes()[:20]
            enabled = (header[:4] == b'\x7fELF' and
                       int.from_bytes(header[18:20], 'little' if header[5] == 1 else 'big') == machine)
        mode = values.get('GIMBAL_ENABLE_' + name.upper(), 'AUTO')
        enabled = enabled and mode != 'OFF'
        if name == 'rknn':
            enabled = enabled and (sdk / 'rknn/include/rknn_api.h').is_file()
        else:
            enabled = enabled and (directory.parent / 'incCn/HCNetSDK.h').is_file() and (directory / 'libhpr.so').is_file()
        features[name] = enabled
        if enabled:
            for source in directory.rglob('*'):
                if source.is_file() and '.so' in source.name:
                    target = vendor / source.relative_to(directory)
                    target.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(source.resolve(), target)
    acoustic = work / 'sources/acoustic_monitor/third_party/micarr_sdk'
    selected = 'x64' if architecture == 'amd64' else 'arm64'
    libraries = list((acoustic / selected / 'lib').glob('*.so*'))
    if not libraries:
        raise ValueError('acoustic_monitor SDK runtime libraries missing for ' + architecture)
    for source in libraries:
        vendor.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source.resolve(), vendor / source.name)
    # The source package installs both SDK architectures; keep only this one.
    installed = dest / 'share/acoustic_monitor/third_party/micarr_sdk'
    other = installed / ('arm64' if selected == 'x64' else 'x64')
    if other.exists():
        shutil.rmtree(other)
    return features


def scan_shared_dependencies(args, cwd, log):
    result = subprocess.run([str(v) for v in args], cwd=cwd, capture_output=True, text=True,
                            env=dict(os.environ, LC_ALL='C'))
    write(log, result.stderr)
    # Filter naming diagnostics only on success; retain complete stderr on disk.
    known = re.compile(
        r"^dpkg-shlibdeps: warning: (?:"
        r"/(?:usr/)?lib/(?:aarch64|x86_64)-linux-gnu/"
        r"libsndio\.so\.7\.0 has an unexpected SONAME \(libsndio\.so\.7\)"
        r"|can't extract name and version from library name 'lib[^/'\s]+\.so')$")
    suppressed = 0
    for line in result.stderr.splitlines():
        if result.returncode or not known.fullmatch(line):
            print(line, file=sys.stderr)
        else:
            suppressed += 1
    if result.returncode:
        raise subprocess.CalledProcessError(result.returncode, args)
    if suppressed:
        print(f'[dependencies] omitted {suppressed} known naming warnings; full log: {log}', flush=True)
    return result.stdout.strip()


def assemble(work, version, manifest):
    architecture = manifest['architecture']
    multiarch = {'amd64': 'x86_64-linux-gnu', 'arm64': 'aarch64-linux-gnu'}[architecture]
    ros_arch_lib = f'/opt/ros/jazzy/lib/{multiarch}'
    manifest = dict(manifest, version=version, component=COMPONENT, install_prefix=str(PREFIX),
                    packaging_recipe_sha256=recipe_id(),
                    component_config=copy.deepcopy(COMPONENT_CONFIG[COMPONENT]))
    stage = work / 'stage'
    if stage.exists():
        shutil.rmtree(stage)
    # dpkg-shlibdeps needs this marker before scanning to resolve $ORIGIN.
    control = stage / 'DEBIAN'
    control.mkdir(parents=True)
    dest = stage / PREFIX.relative_to('/')
    shutil.copytree(PREFIX, dest, symlinks=True)
    if COMPONENT == 'driver':
        relocate_gtsam_exports(dest)
    if COMPONENT == 'task':
        manifest['features'] = task_runtime_libraries(work, dest, architecture)
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
    rpath = ':'.join(dict.fromkeys([
        '$ORIGIN', f'{PREFIX}/lib', f'{PREFIX}/vendor/lib', f'{DRIVER_PREFIX}/lib',
        f'{DRIVER_PREFIX}/vendor/lib', '/opt/ros/jazzy/lib', ros_arch_lib]))
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
                run(['strip', '--strip-unneeded', p], quiet=True, stdout=subprocess.DEVNULL)
                run(['patchelf', '--set-rpath', rpath, p], quiet=True, stdout=subprocess.DEVNULL)
                elfs.append(p)
            elif p.suffix in ('.sh', '.bash', '.zsh', '.ps1', '.dsv', '.py', '.cmake', '.pc'):
                text = p.read_text(errors='strict')
                if '/release/' in text:
                    raise ValueError(f'build-only path in installed runtime file: {p}')
    print(f'[package] stripped symbols and set RPATH for {len(elfs)} ELF files', flush=True)
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
          f'{DRIVER_PREFIX}/local_setup.bash, then the installed navigation/task '
          'local_setup.bash files. No services are installed or started.\n'
          'Runtime defaults are package resources; supply actual device settings externally.\n'
          'Data defaults: ${ROS_HOME:-$HOME/.ros}/navigation/<package>.\n')
    debian = work / 'dependency-scan/debian'
    write(debian / 'control', f'Source: {PACKAGE_NAME}\nSection: misc\nPriority: optional\n'
          'Maintainer: Navigation maintainers <robot@example.com>\n\n'
          f'Package: {PACKAGE_NAME}\nArchitecture: {architecture}\nDescription: Navigation runtime\n')
    args = ['dpkg-shlibdeps', '-O', '--ignore-missing-info',
            f'-l{dest}/lib', f'-l{dest}/vendor/lib', f'-l{DRIVER_PREFIX}/lib',
            f'-l{DRIVER_PREFIX}/vendor/lib', '-l/opt/ros/jazzy/lib', f'-l{ros_arch_lib}']
    result = scan_shared_dependencies([*args, *['-e' + str(p) for p in elfs]],
                                      debian.parent, work / 'shlibdeps.log') if elfs else ''
    depends = next((line.split('=', 1)[1] for line in result.splitlines()
                    if line.startswith('shlibs:Depends=')), '')
    spec = COMPONENT_CONFIG[COMPONENT]
    runtime = list(spec.get('runtime_dependencies', []))
    runtime += spec.get('architecture_dependencies', {}).get(architecture, [])
    if COMPONENT != 'driver':
        driver_name = COMPONENT_CONFIG['driver']['deb_name']
        runtime += [f'{driver_name} (>= {manifest["driver_min_version"]})',
                    f'{driver_name} (<< {manifest["driver_max_version"]})']
    depends = ', '.join(filter(None, [depends, *runtime]))
    installed_kib = sum(p.stat().st_size for p in dest.rglob('*') if p.is_file()) // 1024
    write(control / 'control', f'Package: {PACKAGE_NAME}\nVersion: {version}\nArchitecture: {architecture}\n'
          'Maintainer: Navigation maintainers <robot@example.com>\nSection: misc\nPriority: optional\n'
          f'Installed-Size: {installed_kib}\nDepends: {depends}\nRecommends: ros-jazzy-rviz2\n'
          'Conflicts: navigation, fhzn-navigation\nReplaces: navigation, fhzn-navigation\n'
          f'Description: Model-independent ROS 2 Jazzy {COMPONENT} runtime\n'
          ' Robot control, navigation or inspection task components.\n'
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
        cached = json.loads(complete.read_text())
        if cached.get('component_config', COMPONENT_CONFIG[COMPONENT]) != COMPONENT_CONFIG[COMPONENT]:
            raise ValueError('component catalog changed; rebuild before --package-only')
        assemble(work, args.version, cached)
        return
    if args.select and not complete.exists():
        raise ValueError('first release build must be full; omit --select')
    previous = json.loads(complete.read_text()) if complete.exists() else {}
    environment_changed = False
    if previous and previous.get('builder_image') != manifest['builder_image']:
        old_environment = previous.get('builder_environment') or manifest.get('previous_builder_environment')
        environment_stamp = work / 'object-environment.json'
        if environment_stamp.exists():
            old_environment = json.loads(environment_stamp.read_text()).get('builder_environment')
        if old_environment and old_environment == manifest.get('builder_environment'):
            print('[cache] image ID changed, build environment unchanged; reusing objects', flush=True)
        else:
            environment_changed = True
            args.select = None
            print('[cache] build environment changed or cannot be verified; rebuilding this component '
                  'while retaining source caches', flush=True)
    if args.select and previous.get('release_recipe_sha256') != manifest['release_recipe_sha256']:
        raise ValueError('release recipe changed; run without --select to apply it to all packages')
    if COMPONENT != 'driver' and previous and driver_build_id(previous) != driver_build_id(manifest):
        if args.select:
            raise ValueError('driver underlay changed; run without --select to rebuild component consumers')
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
    if environment_changed:
        for directory in ('build', 'native-build'):
            if (work / directory).exists():
                shutil.rmtree(work / directory)
        for name in NATIVE:
            (work / f'{name}.installed').unlink(missing_ok=True)
    if manifest.get('builder_environment'):
        write(work / 'object-environment.json', json.dumps({
            'builder_environment': manifest['builder_environment']}))
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
         *COMPONENT_CONFIG[COMPONENT].get('cmake_args', []),
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
    p.add_argument('--component', choices=(*COMPONENTS, 'all'), default='all')
    p.add_argument('--version', help='default Debian version for selected components')
    p.add_argument('--tag', help='requested ggbond-control release tag, e.g. v0.3.5')
    for component in COMPONENTS:
        p.add_argument(f'--{component}-version', help='component version; overrides --version')
        p.add_argument(f'--{component}-tag', help='component source tag; overrides --tag')
    p.add_argument('--driver-deb', type=Path, help='driver build underlay for navigation-only or task-only builds')
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
    components = COMPONENTS if args.component == 'all' else (args.component,)
    for component in components:
        version = getattr(args, component + '_version') or args.version
        if not version or not re.fullmatch(r'[0-9][A-Za-z0-9.+~:-]*', version):
            p.error(f'valid --version or --{component}-version is required')
        tag = getattr(args, component + '_tag') or args.tag
        if not args.inside and not args.package_only and not tag:
            p.error('--tag is required unless using --package-only (or use component-specific tags)')
    for tag in (args.tag, args.driver_tag, args.navigation_tag, args.task_tag):
        if tag and version_tuple(tag) is None:
            p.error('--tag must be a stable version such as v0.3.5 (or 0.3.5)')
    if args.package_only and any((args.tag, args.driver_tag, args.navigation_tag, args.task_tag)):
        p.error('--package-only uses the previous source snapshot; omit --tag')
    if args.package_only and (args.driver_min_version or args.driver_max_version):
        p.error('--package-only preserves cached driver compatibility bounds')
    if args.driver_deb and (args.component not in ('navigation', 'task') or args.package_only):
        p.error('--driver-deb is only for navigation or task source builds')
    if args.component in ('navigation', 'task') and not args.package_only and not args.inside and not args.driver_deb:
        p.error(f'--component {args.component} requires --driver-deb')
    if args.select and args.component != 'all':
        allowed = DRIVER_PACKAGES if args.component == 'driver' else (
            NAVIGATION_PACKAGES if args.component == 'navigation' else TASK_SOURCES.keys())
        if set(args.select) - allowed:
            p.error('--select contains packages outside the selected component')
    for version in (args.driver_min_version, args.driver_max_version):
        if version and not re.fullmatch(r'[0-9][A-Za-z0-9.+~:-]*', version):
            p.error('invalid driver compatibility version')
    return args


def driver_underlay(args, work, architecture):
    deb = args.driver_deb.expanduser().resolve()
    if output(['dpkg-deb', '-f', deb, 'Package']) != COMPONENT_CONFIG['driver']['deb_name']:
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
            dockerfile = COMPONENT_CONFIG[COMPONENT]['dockerfile']
            run(['docker', 'build', '--platform', platform, '-t', image_name,
                 '-f', ROOT / 'packaging' / dockerfile, (ROOT / 'packaging' / dockerfile).parent])
        image_architecture = output(['docker', 'image', 'inspect', '--format', '{{.Architecture}}', image_name])
        if image_architecture != architecture:
            raise ValueError(f'{image_name} contains {image_architecture}, expected {architecture}; rebuild the image')
        if not args.package_only:
            manifest = {'builder_image': output(['docker', 'image', 'inspect', '--format', '{{.Id}}', image_name]),
                        'packages': {}, 'native': {}, 'architecture': architecture, 'ros': 'jazzy',
                        'release_recipe_sha256': recipe_id()}
            manifest['builder_environment'] = image_environment(image_name)
            state = work / 'build-state.json'
            if state.exists():
                old = json.loads(state.read_text())
                if not old.get('builder_environment') and old.get('builder_image') != manifest['builder_image']:
                    try:
                        manifest['previous_builder_environment'] = image_environment(old['builder_image'])
                    except (subprocess.CalledProcessError, ValueError, KeyError, IndexError):
                        pass  # Missing legacy image: conservatively rebuild objects, retain sources.
            manifest['requested_tag'] = args.tag
            manifest['component'] = COMPONENT
            manifest['component_config'] = copy.deepcopy(COMPONENT_CONFIG[COMPONENT])
            if COMPONENT != 'driver':
                driver, minimum, maximum = driver_underlay(args, work, architecture)
                manifest.update(driver=driver, driver_min_version=minimum, driver_max_version=maximum)
            with (GITHUB_SESSION.access() if GITHUB_SESSION else github_access()) as git_env:
                for name, repository in SOURCES.items():
                    source = work / 'repositories' / name
                    revision = fetch_source(f'{CATALOG["repository_base"].rstrip("/")}/{repository}.git',
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
                   '-e', 'HOME=/release/home',
                   '-e', 'INSPECTION_PACKAGING_CATALOG=/packaging-catalog.yaml',
                   '-v', f'{CATALOG_PATH.resolve()}:/packaging-catalog.yaml:ro', '-v', f'{work}:/release',
                   '-v', f'{work}/install:{PREFIX}', '-v', f'{ROOT}/scripts:/tools:ro',
                   *(['-v', f'{work}/driver-underlay{DRIVER_PREFIX}:{DRIVER_PREFIX}:ro']
                     if COMPONENT != 'driver' else []),
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
    global GITHUB_SESSION
    args = arguments()
    if args.inside:
        configure_component(args.component)
        inside(args)
        return
    with ExitStack() as stack:
        previous_session = GITHUB_SESSION
        GITHUB_SESSION = GitHubSession(stack)
        try:
            build_components(args)
        finally:
            GITHUB_SESSION = previous_session


def build_components(args):
    components = COMPONENTS if args.component == 'all' else (args.component,)
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
            if component != 'driver' and set(args.select) & DRIVER_PACKAGES:
                current.select = None  # Rebuild every consumer of changed driver inputs.
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
