#!/usr/bin/env python3
"""Build navigation with explicit underlays and reusable local third-party libraries."""

import argparse
from contextlib import contextmanager
import fcntl
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
try:
    import yaml
except ImportError:
    sys.exit('[error] PyYAML is required; install python3-yaml before building.')

from update_repositories import clone_repository, update_repositories


BRINGUP = Path(__file__).resolve().parents[1]
STAGE = 'configuration'


def info(message):
    print(message, flush=True)


def run(command, *, cwd=None, env=None, capture=False):
    result = subprocess.run([str(x) for x in command], cwd=cwd, env=env,
                            text=True, stdout=subprocess.PIPE if capture else None,
                            check=True)
    return result.stdout.strip() if capture else ''


def read_yaml(path):
    value = yaml.safe_load(Path(path).read_text()) or {}
    if not isinstance(value, dict):
        raise ValueError(f'{path}: expected a YAML mapping')
    return value


def merge(base, override):
    result = dict(base)
    for key, value in override.items():
        result[key] = (merge(result[key], value)
                       if isinstance(result.get(key), dict) and isinstance(value, dict) else value)
    return result


def positive_jobs(value):
    if not re.fullmatch(r'[1-9][0-9]*', str(value)):
        raise argparse.ArgumentTypeError('build jobs must be a positive integer')
    return int(value)


def arguments():
    parser = argparse.ArgumentParser(prog='build_navigation.sh', description=__doc__)
    parser.add_argument('-j', '--jobs', type=positive_jobs, nargs='?', const='',
                        help='jobs per package; packages run sequentially (default: 1)')
    parser.add_argument('--config', type=Path, help='build YAML overrides for the selected model')
    parser.add_argument('--model', help='override model read from config/robot.yaml')
    parser.add_argument('--workspace-root', type=Path)
    parser.add_argument('--driver-ws', type=Path)
    parser.add_argument('--algor-ws', type=Path)
    parser.add_argument('--task-ws', type=Path)
    parser.add_argument('--repos-file', type=Path)
    parser.add_argument('--interface-underlay', type=Path,
                        help='use an already built interface setup instead of building interfaces')
    parser.add_argument('--output-root', type=Path,
                        help='isolated build/install/log directories under ROOT/<workspace>')
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument('--fetch-only', action='store_true')
    modes.add_argument('--build-only', action='store_true')
    inspect = parser.add_mutually_exclusive_group()
    inspect.add_argument('--check', action='store_true', help='check inputs/environment without building')
    inspect.add_argument('--plan', action='store_true', help='print resolved plan without fetching/building')
    third = parser.add_mutually_exclusive_group()
    third.add_argument('--third-party-only', action='store_true')
    third.add_argument('--skip-third-party', action='store_true', help='require existing third-party installs')
    parser.add_argument('--rebuild-gtsam', action='store_true', help='explicitly reconfigure/build/install GTSAM')
    parser.add_argument('--rebuild-third-party', action='store_true', help='explicitly rebuild both libraries')
    parser.add_argument('--skip-driver-build', action='store_true')
    parser.add_argument('--skip-algor-build', action='store_true')
    parser.add_argument('--no-branch-check', action='store_true')
    parser.add_argument('--check-updates', action='store_true')
    parser.add_argument('--update-current-branch', action='store_true')
    parser.add_argument('--update-only', action='store_true')
    parser.add_argument('--checkout', choices=('dev', 'main'),
                        help='switch remote-backed branches and exit without building; '
                             'main uses manifest preference or unique main/jazzy/master; '
                             'add an update option to fast-forward')
    parser.add_argument('--remote', help='checkout remote (default: origin); requires --checkout')
    args = parser.parse_args()
    if args.jobs is None:
        try:
            args.jobs = positive_jobs(os.environ.get('BUILD_JOBS', '1'))
        except argparse.ArgumentTypeError as exc:
            parser.error(str(exc))
    if args.skip_third_party and (args.rebuild_gtsam or args.rebuild_third_party):
        parser.error('--skip-third-party conflicts with --rebuild-gtsam/--rebuild-third-party')
    if (args.plan or args.check) and (args.fetch_only or args.update_only or
                                    args.update_current_branch or args.check_updates or args.checkout):
        parser.error('--plan/--check cannot fetch or update repositories')
    if args.checkout and (args.fetch_only or args.build_only):
        parser.error('--checkout conflicts with --fetch-only/--build-only')
    if args.remote is not None and not args.checkout:
        parser.error('--remote requires --checkout')
    if args.remote is not None and (not args.remote or args.remote.startswith('-')):
        parser.error('invalid --remote')
    args.remote = args.remote or 'origin'
    return args


def path(value):
    return Path(os.path.expandvars(str(value))).expanduser().absolute()


class Build:
    def __init__(self, args):
        self.args = args
        self.config_root = path(os.environ.get('INSPECTION_CONFIG_DIR', BRINGUP / 'config'))
        robot = self.config_root / 'robot.yaml'
        self.model = args.model or (read_yaml(robot).get('model', '') if robot.exists() else '')
        if not isinstance(self.model, str) or (self.model and not re.fullmatch(r'[a-zA-Z0-9_-]+', self.model)):
            raise ValueError('invalid robot model')
        self.config = {}
        defaults = self.config_root / 'build/navigation.yaml'
        if defaults.exists():
            self.config = merge(self.config, read_yaml(defaults))
        model_config = self.config_root / f'build/{self.model}.yaml'
        if self.model and model_config.exists():
            self.config = merge(self.config, read_yaml(model_config))
        if args.config:
            self.config = merge(self.config, read_yaml(args.config))
        for key in ('underlays', 'system_cmake_packages'):
            if key in self.config and (not isinstance(self.config[key], list) or
                                       any(not isinstance(v, str) for v in self.config[key])):
                raise ValueError(f'{key}: expected a list of strings')
        for key in ('required_packages', 'package_cmake_args'):
            value = self.config.get(key, {})
            if not isinstance(value, dict) or any(
                    not isinstance(v, list) or any(not isinstance(item, str) for item in v)
                    for v in value.values()):
                raise ValueError(f'{key}: expected a mapping of string lists')
        self.root = path(args.workspace_root or os.environ.get('NAV_WORKSPACE_ROOT') or
                         self.config.get('workspace_root', '~/Workspace'))
        self.workspaces = {name: path(getattr(args, name) or self.root / name)
                           for name in ('driver_ws', 'algor_ws', 'task_ws')}
        self.repos_file = path(args.repos_file or self.config_root / 'navigation_deps.repos')
        self.repositories = read_yaml(self.repos_file).get('repositories', {})
        if not isinstance(self.repositories, dict) or not self.repositories:
            raise ValueError(f'no repositories found in {self.repos_file}')
        self.third = self.workspaces['driver_ws'] / 'third_party'
        self.prefix = self.third / 'install'
        self.rmw = self.config.get('rmw_implementation', 'rmw_fastrtps_cpp')
        if not isinstance(self.rmw, str) or not re.fullmatch(r'rmw_[a-zA-Z0-9_]+', self.rmw):
            raise ValueError('invalid rmw_implementation')
        self.ros_setup = path(self.config.get('ros_setup', '/opt/ros/jazzy/setup.bash'))
        self.underlays = [self.ros_setup]
        for value in self.config.get('underlays', []):
            self.underlays.append(self.resolve(value))
        self.interface = path(args.interface_underlay) if args.interface_underlay else (
            self.output('task_ws') / 'install/inspection_interfaces/share/inspection_interfaces/local_setup.bash')
        self.packages = {}
        self.repo_paths = {}
        self.base_env = None
        self.env = None
        self.checkout_targets = {}

    def resolve(self, value):
        return path(str(value).replace('${WORKSPACE_ROOT}', str(self.root)))

    def output(self, workspace):
        return (path(self.args.output_root) / workspace if self.args.output_root
                else self.workspaces[workspace])

    def repo_path(self, relative):
        parts = Path(relative).parts
        if Path(relative).is_absolute() or '..' in parts or len(parts) < 2:
            raise ValueError(f'invalid managed repository path: {relative}')
        return self.workspaces.get(parts[0], self.root / parts[0]).joinpath(*parts[1:])

    def branches(self):
        for relative, spec in self.repositories.items():
            repo = self.repo_path(relative)
            def git(*args):
                result = subprocess.run(['git', '-C', str(repo), *args], text=True,
                                        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
                return result.stdout.strip() if result.returncode == 0 else ''
            if not (repo / '.git').exists():
                info(f'[branch] {relative}: missing or not-git')
                continue
            branch = git('branch', '--show-current') or 'detached@' + git('rev-parse', '--short', 'HEAD')
            details = [branch]
            if git('status', '--porcelain'):
                details.append('dirty')
            expected = self.checkout_targets.get(relative, str(spec.get('version', '')))
            if expected and branch != expected and git('describe', '--tags', '--exact-match', 'HEAD') != expected:
                details.append('expected=' + expected)
            actual = git('remote', 'get-url', self.args.remote)
            if actual and actual != spec.get('url'):
                details.append('remote-mismatch=' + actual)
            if self.args.check_updates:
                result = subprocess.run(['git', '-C', str(repo), 'fetch', '--quiet'])
                upstream = git('rev-parse', '--abbrev-ref', '@{upstream}')
                if result.returncode:
                    details.append('fetch-failed')
                elif upstream:
                    counts = git('rev-list', '--left-right', '--count', 'HEAD...' + upstream).split()
                    details.append('upstream=' + upstream)
                    if counts == ['0', '0']:
                        details.append('up-to-date')
                    elif len(counts) == 2:
                        details.extend(['ahead=' + counts[0], 'behind=' + counts[1]])
                else:
                    details.append('upstream=none')
            info(f"[branch] {relative}: {' '.join(details)}")

    def fetch(self):
        for relative, spec in self.repositories.items():
            repo = self.repo_path(relative)
            if spec.get('type', 'git') != 'git' or not spec.get('url'):
                raise ValueError(f'invalid repository specification: {relative}')
            if repo.exists():
                info(f'[fetch] skip existing {relative}')
                continue
            info(f'[fetch] {relative}')
            clone_repository(repo, spec, checkout=self.args.checkout, remote=self.args.remote, depth=1)

    def plan(self):
        info(f'[plan] model={self.model or "default"}; RMW={self.rmw}; Release; sequential; jobs={self.args.jobs}')
        for name in self.workspaces:
            info(f'[plan] {name}: source={self.workspaces[name]}; output={self.output(name)}')
        info(f'[plan] underlays: {", ".join(str(p) for p in self.underlays)}')
        info(f'[plan] interfaces: {self.interface}' + (' (external)' if self.args.interface_underlay else ' (build first)'))
        info(f'[plan] third-party: {self.prefix}; installed GTSAM is reused unless --rebuild-gtsam')
        info('[plan] stages: environment/dependencies -> interfaces -> third-party -> drivers -> algorithms')
        for relative in self.repositories:
            info(f'[plan] repository: {relative} -> {self.repo_path(relative)}')
        if self.config.get('package_cmake_args'):
            info('[plan] package CMake settings: ' + json.dumps(self.config['package_cmake_args'], sort_keys=True))

    def environment(self, setups):
        env = dict(os.environ)
        for key in list(env):
            if key.startswith(('AMENT_', 'COLCON_', 'ROS_', 'RMW_', 'ZENOH_')) or key in (
                    'CMAKE_PREFIX_PATH', 'CMAKE_LIBRARY_PATH', 'CMAKE_INCLUDE_PATH',
                    'LD_LIBRARY_PATH', 'PYTHONPATH', 'PKG_CONFIG_PATH', 'CYCLONEDDS_URI'):
                env.pop(key, None)
        env['RMW_IMPLEMENTATION'] = self.rmw
        env['MAKEFLAGS'] = f'-j{self.args.jobs}'
        env['CMAKE_BUILD_PARALLEL_LEVEL'] = str(self.args.jobs)
        for setup in setups:
            if not setup.is_file():
                raise ValueError(f'underlay setup not found: {setup}')
        # Pass paths as argv, never interpolate a path/config value into shell code.
        code = 'set -e; for setup in "$@"; do source "$setup"; done; exec /usr/bin/python3 -c "import os,json; print(json.dumps(dict(os.environ)))"'
        result = run(['bash', '--noprofile', '--norc', '-c', code, 'navigation-build', *setups],
                     env=env, capture=True)
        return json.loads(result.splitlines()[-1])

    def discover(self, workspace):
        repos = [self.repo_path(p) for p in self.repositories if p.startswith(workspace + '/src/')]
        if not repos:
            raise ValueError(f'no managed repositories for {workspace}')
        missing = [str(p) for p in repos if not p.is_dir()]
        if missing:
            raise ValueError('missing managed repositories: ' + ', '.join(missing))
        with tempfile.TemporaryDirectory(prefix='navigation-discovery-') as logs:
            names = run(['colcon', '--log-base', logs, 'list', '--base-paths', *repos, '--names-only'],
                        env=self.base_env, capture=True).splitlines()
        if not names or any(not re.fullmatch(r'[a-zA-Z0-9_]+', n) for n in names):
            raise ValueError(f'{workspace}: empty or invalid colcon package list')
        if len(names) != len(set(names)):
            raise ValueError(f'{workspace}: duplicate packages')
        self.repo_paths[workspace] = repos
        self.packages[workspace] = sorted(names)
        info(f'[check] {workspace} packages: {" ".join(sorted(names))}')

    def probe(self, body, env, label):
        with tempfile.TemporaryDirectory(prefix='navigation-check-') as directory:
            source = Path(directory)
            (source / 'CMakeLists.txt').write_text(
                'cmake_minimum_required(VERSION 3.16)\nproject(navigation_dependency_check)\n' + body)
            result = subprocess.run(['cmake', '-S', directory, '-B', str(source / 'build')],
                                    env=env, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
            if result.returncode:
                raise ValueError(f'{label} failed before build:\n{result.stdout}')
        info(f'[check] {label}: OK')

    def preflight(self):
        for tool in ('git', 'cmake', 'colcon'):
            if not shutil.which(tool):
                raise ValueError(f'required command not found: {tool}')
        self.base_env = self.environment(self.underlays)
        self.env = dict(self.base_env)
        if self.args.third_party_only:
            return
        if not self.args.skip_driver_build:
            self.discover('driver_ws')
        if not self.args.skip_algor_build:
            self.discover('algor_ws')
        if not self.args.interface_underlay:
            source = self.workspaces['task_ws'] / 'src/inspection_interfaces'
            if not (source / 'package.xml').is_file():
                raise ValueError(f'interface sources not found: {source}; fetch dependencies first or use --interface-underlay')
        elif not self.interface.is_file():
            raise ValueError(f'interface underlay not found: {self.interface}')
        packages = self.config.get('system_cmake_packages', ['ament_cmake', 'rclcpp'])
        for name in packages:
            if not isinstance(name, str) or not re.fullmatch(r'[a-zA-Z0-9_-]+', name):
                raise ValueError(f'invalid system CMake package: {name}')
        body = ''.join(f'find_package({name} REQUIRED)\n' for name in packages)
        if self.config.get('check_geographiclib', False):
            body += ('find_path(GEOGRAPHICLIB_INCLUDE_DIR GeographicLib/LocalCartesian.hpp REQUIRED)\n'
                     'find_library(GEOGRAPHICLIB_LIBRARY GeographicLib REQUIRED)\n')
        self.probe(body, self.base_env, f'ROS/RMW ({self.rmw}) and system dependencies')
        for workspace, required in self.config.get('required_packages', {}).items():
            if workspace in self.packages:
                missing = sorted(set(required) - set(self.packages[workspace]))
                if missing:
                    raise ValueError(f'{workspace}: required packages absent from manifest/discovery: {", ".join(missing)}')
        bridge_args = self.config.get('package_cmake_args', {}).get('nav_bridge', [])
        if not self.args.skip_driver_build and '-DNAV_BRIDGE_BUILD_TANK=ON' in bridge_args:
            self.probe('find_package(CURL REQUIRED)\nfind_package(PkgConfig REQUIRED)\n'
                       'pkg_check_modules(TANK_JSONCPP REQUIRED IMPORTED_TARGET jsoncpp)\n',
                       self.base_env, 'TANK CURL/JsonCpp/pkg-config dependencies')
        if not self.args.skip_driver_build and '-DNAV_BRIDGE_BUILD_D1_MAX=ON' in bridge_args:
            arch = {'arm64': 'aarch64', 'amd64': 'x86_64'}.get(platform.machine(), platform.machine())
            sdk = self.workspaces['driver_ws'] / f'src/nav_bridge/third_party/robot_sdk/lib/{arch}/librobot_sdk.so.0.2.1'
            if not sdk.is_file():
                raise ValueError(f'D1 Max SDK library not found: {sdk}')
        if self.args.interface_underlay:
            env = self.environment(self.underlays + [self.interface])
            run(['ros2', 'pkg', 'prefix', 'inspection_interfaces'], env=env, capture=True)
        # Validate ALL third-party inputs before beginning any expensive build.
        self.check_third_party_inputs()

    def artifacts(self, library):
        if library == 'gtsam':
            files = ['lib/cmake/GTSAM/GTSAMConfig.cmake', 'include/gtsam/base/Matrix.h',
                     'lib/libgtsam.so', 'lib/libgtsam_unstable.so']
        else:
            files = ['include/livox_lidar_api.h', 'lib/liblivox_lidar_sdk_shared.so']
        return [self.prefix / f for f in files]

    def reuse(self, library):
        installed = all(p.is_file() for p in self.artifacts(library))
        force = self.args.rebuild_third_party or (library == 'gtsam' and self.args.rebuild_gtsam)
        return installed and not force

    def check_third_party_inputs(self):
        for library in ('gtsam', 'livox_sdk2'):
            if self.reuse(library):
                info(f'[third_party] reuse installed {library}: {self.prefix}')
                continue
            if self.args.skip_third_party:
                raise ValueError(f'{library} install incomplete in {self.prefix}; remove --skip-third-party to initialize')
            if library == 'gtsam' and any(p.exists() for p in self.artifacts(library)) and not (
                    self.args.rebuild_gtsam or self.args.rebuild_third_party):
                raise ValueError('GTSAM install is incomplete; it will not be rebuilt implicitly. Use --rebuild-gtsam to repair it.')
            if not (self.third / 'src' / library / 'CMakeLists.txt').is_file():
                raise ValueError(f'{library} source missing: {self.third / "src" / library}')

    def third_party(self):
        self.third.mkdir(parents=True, exist_ok=True)
        # Different ROS output roots still share this prefix. Serialize initial
        # native installs so simultaneous validations cannot both build GTSAM.
        with (self.third / '.build.lock').open('w') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            self.install_third_party()

    def install_third_party(self):
        self.check_third_party_inputs()
        for library in ('gtsam', 'livox_sdk2'):
            if self.reuse(library):
                continue
            info(f'[third_party] initialize/rebuild {library}')
            options = ['-DCMAKE_BUILD_TYPE=Release', '-DCMAKE_EXPORT_COMPILE_COMMANDS=ON',
                       f'-DCMAKE_INSTALL_PREFIX={self.prefix}']
            if library == 'gtsam':
                options += ['-DGTSAM_USE_SYSTEM_EIGEN=ON', '-DGTSAM_BUILD_TESTS=OFF',
                            '-DGTSAM_BUILD_EXAMPLES_ALWAYS=OFF', '-DGTSAM_BUILD_UNSTABLE=ON',
                            '-DGTSAM_BUILD_WITH_MARCH_NATIVE=OFF']
            build = self.third / 'build' / library
            run(['cmake', '-S', self.third / 'src' / library, '-B', build, *options], env=self.base_env)
            run(['cmake', '--build', build, f'-j{self.args.jobs}'], env=self.base_env)
            run(['cmake', '--install', build], env=self.base_env)
            if not all(p.is_file() for p in self.artifacts(library)):
                raise ValueError(f'{library} installed without required files: {self.prefix}')
        self.use_third_party()
        # No GTSAM configure/build/install here: check only the installed consumer interface.
        self.probe(f'find_package(GTSAM 4.2 REQUIRED PATHS "{self.prefix}/lib/cmake/GTSAM" NO_DEFAULT_PATH)\n',
                   self.env, 'installed GTSAM')

    def use_third_party(self):
        for key, values in {
            'CMAKE_PREFIX_PATH': [self.prefix],
            'CMAKE_LIBRARY_PATH': [self.prefix / 'lib', self.prefix / 'lib64'],
            'CMAKE_INCLUDE_PATH': [self.prefix / 'include'],
            'LD_LIBRARY_PATH': [self.prefix / 'lib', self.prefix / 'lib64'],
            'PKG_CONFIG_PATH': [self.prefix / 'lib/pkgconfig', self.prefix / 'lib64/pkgconfig'],
        }.items():
            self.env[key] = ':'.join([*(str(v) for v in values), *filter(None, [self.env.get(key)])])

    def cmake_args(self):
        args = ['-Wno-dev', '-DCMAKE_BUILD_TYPE=Release', '-DCMAKE_EXPORT_COMPILE_COMMANDS=ON',
                '-DBUILD_TESTING=OFF', '--no-warn-unused-cli',
                '-DPython3_EXECUTABLE=/usr/bin/python3', '-DPYTHON_EXECUTABLE=/usr/bin/python3',
                f'-DRMW_IMPLEMENTATION={self.rmw}']
        for key in ('CMAKE_PREFIX_PATH', 'CMAKE_LIBRARY_PATH', 'CMAKE_INCLUDE_PATH'):
            if self.env.get(key):
                args.append(f'-D{key}=' + self.env[key].replace(':', ';'))
        rpath = f'{self.prefix}/lib;{self.prefix}/lib64'
        return args + [f'-DCMAKE_BUILD_RPATH={rpath}', f'-DCMAKE_INSTALL_RPATH={rpath}',
                       '-DCMAKE_INSTALL_RPATH_USE_LINK_PATH=ON',
                       f'-DGTSAM_DIR={self.prefix}/lib/cmake/GTSAM',
                       f'-DLIVOX_LIDAR_SDK_LIBRARY={self.prefix}/lib/liblivox_lidar_sdk_shared.so',
                       f'-DLIVOX_LIDAR_SDK_INCLUDE_DIR={self.prefix}/include']

    def colcon(self, workspace, packages, sources):
        output = self.output(workspace)
        output.mkdir(parents=True, exist_ok=True)
        # Package-specific switches do not leak into unrelated CMake projects.
        with tempfile.NamedTemporaryFile(mode='w', suffix='.meta') as meta:
            json.dump({'names': {name: {'cmake-args': values}
                                 for name, values in self.config.get('package_cmake_args', {}).items()
                                 if name in packages}}, meta)
            meta.flush()
            command = ['colcon', '--log-base', output / 'log', 'build',
                       '--base-paths', *sources, '--packages-select', *packages,
                       '--build-base', output / 'build', '--install-base', output / 'install',
                       '--symlink-install', '--executor', 'sequential', '--cmake-force-configure',
                       '--metas', meta.name, '--cmake-args', *self.cmake_args()]
            info(f'[build] {workspace}: {" ".join(packages)}')
            run(command, cwd=self.workspaces[workspace], env=self.env)

    def interfaces(self):
        if not self.args.interface_underlay:
            self.colcon('task_ws', ['inspection_interfaces'],
                        [self.workspaces['task_ws'] / 'src/inspection_interfaces'])
        self.env = self.environment(self.underlays + [self.interface])
        run(['ros2', 'pkg', 'prefix', 'inspection_interfaces'], env=self.env, capture=True)

    @contextmanager
    def lock(self):
        root = path(self.args.output_root) if self.args.output_root else self.workspaces['driver_ws']
        root.mkdir(parents=True, exist_ok=True)
        with (root / '.navigation-build.lock').open('w') as stream:
            try:
                fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise ValueError(f'another navigation build is using {root}') from exc
            yield

    def execute(self):
        global STAGE
        self.plan()
        if self.args.plan:
            return
        if not self.args.build_only and not self.args.check and not self.args.checkout:
            STAGE = 'fetch'
            self.fetch()
        if self.args.update_current_branch or self.args.update_only or self.args.checkout:
            STAGE = 'checkout' if self.args.checkout else 'update'
            self.checkout_targets = update_repositories(
                self.repos_file, self.root, self.workspaces['algor_ws'],
                self.workspaces['driver_ws'], self.workspaces['task_ws'],
                checkout=self.args.checkout, remote=self.args.remote,
                update=self.args.update_current_branch or self.args.update_only, clone_depth=1) or {}
        if not self.args.no_branch_check or self.args.check_updates:
            self.branches()
        if self.args.checkout:
            info('[done] checkout complete; no build performed')
            return
        if self.args.fetch_only or self.args.update_only:
            info('[done] ' + ('fetch-only' if self.args.fetch_only else 'update-only') + ' complete')
            return
        STAGE = 'preflight'
        self.preflight()
        if self.args.third_party_only:
            self.check_third_party_inputs()
        if self.args.check:
            info('[done] preflight complete; no builds or repository updates performed')
            return
        with self.lock():
            if not self.args.third_party_only:
                STAGE = 'interfaces'
                self.interfaces()
            STAGE = 'third-party'
            self.third_party()
            if not self.args.third_party_only:
                if not self.args.skip_driver_build:
                    STAGE = 'drivers'
                    self.colcon('driver_ws', self.packages['driver_ws'], self.repo_paths['driver_ws'])
                if not self.args.skip_algor_build:
                    STAGE = 'algorithms'
                    driver = self.output('driver_ws') / 'install/local_setup.bash'
                    self.env = self.environment(self.underlays + [self.interface, driver])
                    self.use_third_party()
                    self.colcon('algor_ws', self.packages['algor_ws'], self.repo_paths['algor_ws'])
            self.write_metadata()
        info('[done] navigation build complete' if not self.args.third_party_only else '[done] third-party-only complete')
        info('[env] Explicit runtime underlays (bringup/task modules must also be installed):')
        for setup in self.underlays + [self.interface, self.output('driver_ws') / 'install/local_setup.bash',
                                     self.output('algor_ws') / 'install/local_setup.bash']:
            info('  source ' + shlex.quote(str(setup)))
        info('  export RMW_IMPLEMENTATION=' + self.rmw)
        info(f'  export LD_LIBRARY_PATH={shlex.quote(str(self.prefix / "lib"))}:"${{LD_LIBRARY_PATH:-}}"')

    def write_metadata(self):
        repositories = {}
        for relative in self.repositories:
            repo = self.repo_path(relative)
            result = subprocess.run(['git', '-C', str(repo), 'rev-parse', 'HEAD'],
                                    text=True, capture_output=True)
            dirty = subprocess.run(['git', '-C', str(repo), 'status', '--porcelain'],
                                   text=True, capture_output=True)
            repositories[relative] = {'commit': result.stdout.strip(), 'dirty': bool(dirty.stdout)}
        metadata = {'model': self.model, 'rmw': self.rmw, 'jobs': self.args.jobs,
                    'repositories': repositories, 'packages': self.packages,
                    'underlays': [str(p) for p in self.underlays], 'third_party_prefix': str(self.prefix),
                    'third_party_sha256': {str(p): hashlib.sha256(p.read_bytes()).hexdigest()
                                           for name in ('gtsam', 'livox_sdk2')
                                           for p in self.artifacts(name) if p.is_file()}}
        destination = self.output('algor_ws') / 'navigation-build.json'
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(json.dumps(metadata, indent=2) + '\n')
        info(f'[build] source revisions and settings: {destination}')


def main():
    started = time.monotonic()
    try:
        Build(arguments()).execute()
    except (OSError, ValueError, yaml.YAMLError, subprocess.CalledProcessError) as exc:
        print(f'[error] stage={STAGE}: {exc}', file=sys.stderr, flush=True)
        return 1
    finally:
        info(f'[time] elapsed={time.monotonic() - started:.1f}s')
    return 0


if __name__ == '__main__':
    sys.exit(main())
