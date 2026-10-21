#!/usr/bin/env python3
"""Fixed defaults -> model -> robot configuration, shared by all entrypoints."""

import argparse
import copy
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile

import yaml


FILENAMES = {"system": "system.yaml", "navigation": "navigate.yaml", "services": "services.yaml"}
# Relative file paths always use the owning package's configuration directory.
FILE_FIELDS = {
    "system": {
        ("gimbal", "params_file"): ("sensor_gimbal", "config"),
        ("live_stream", "config_path"): ("inspection_platform_bridge", "config"),
        ("gas", "params_file"): ("gas_monitor", "config"),
        ("sensor_rs485", "params_file"): ("sensor_rs485", "config"),
    },
    "navigation": {
        ("nav_bridge", "config"): ("nav_bridge", "config"),
        ("nav_bridge", "cyclonedds_config"): ("nav_bridge", "config"),
        ("livox", "config"): ("livox_ros_driver2", "config"),
        ("slam", "config"): ("faster_lio", "config"),
        ("terrain", "config"): ("gridmapper", "config"),
        ("local_planner", "config"): ("local_planner", "config"),
        ("global_planner", "config"): ("multi_map_nav", "params"),
    },
    "services": {
        ("navigation", "launch_args", "navigate_config_path"): ("inspection_bringup", "config"),
        ("inspection_system", "launch_args", "system_config_path"): ("inspection_bringup", "config"),
    },
}


def default_config_dir():
    """One root for ROS launch, shell helpers and their child processes."""
    override = os.environ.get("INSPECTION_CONFIG_DIR", "")
    if override:
        return Path(os.path.expandvars(override)).expanduser().absolute()
    try:
        from ament_index_python.packages import get_package_share_directory, PackageNotFoundError
    except ImportError:
        pass
    else:
        try:
            # colcon --symlink-install links individual files, not the config
            # directory. Resolve the base file so a newly created robot.yaml
            # is visible immediately beside its source, without rebuilding.
            return (Path(get_package_share_directory("inspection_bringup")) /
                    "config" / "system.yaml").resolve().parent
        except PackageNotFoundError:
            pass
    return Path(__file__).absolute().parent.parent / "config"


def read_yaml(path):
    path = Path(path)
    with path.open(encoding="utf-8") as stream:
        value = yaml.safe_load(stream)
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ValueError(f"{path}: expected a YAML mapping")
    return value


def merge(base, override):
    result = copy.deepcopy(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = merge(result[key], value)
        else:
            result[key] = copy.deepcopy(value)
    return result


def merge_module_config(base, override, module):
    result = merge(base, override)
    bridge = override.get('nav_bridge', {}) if module == 'navigation' else {}
    if (isinstance(bridge, dict) and 'robot_type' in bridge and 'config' not in bridge
            and bridge['robot_type'] != base.get('nav_bridge', {}).get('robot_type', 'x30')):
        result.get('nav_bridge', {}).pop('config', None)
    return result


def apply_module_switches(sequence, switches):
    """Retain CLI module switches across supervisor/worker process boundaries."""
    result = [name for name in sequence if switches.get(name, True)]
    extensions = ("terrain", "local_planner", "global_planner")
    for name, enabled in switches.items():
        if enabled and name not in result:
            index = next((i for i, value in enumerate(result) if value in extensions), len(result))
            result.insert(index if name not in extensions else len(result), name)
    return result


def file_path(value, package, directory, origin, check_exists=True, config_dir=None):
    if not isinstance(value, str):
        raise ValueError(f"{origin}: expected a file path string, got {value!r}")
    root = Path(config_dir) if config_dir is not None else default_config_dir()
    # The selected root wins over a stale shell variable, including --config
    # and callers passing config_dir directly.
    value = value.replace('${INSPECTION_CONFIG_DIR}', str(root)).replace(
        '$INSPECTION_CONFIG_DIR/', str(root) + '/')
    expanded = Path(os.path.expandvars(value)).expanduser()
    if expanded.is_absolute():
        path = expanded
    else:
        if package == "inspection_bringup":
            base = root
        else:
            try:
                from ament_index_python.packages import get_package_share_directory
                base = Path(get_package_share_directory(package)) / directory
            except (ImportError, LookupError) as exc:
                raise ValueError(f"{origin}: cannot locate ROS package {package}; source its workspace") from exc
        path = base / expanded
    path = Path(os.path.abspath(path))
    if check_exists and not path.is_file():
        raise ValueError(f"{origin}: referenced file does not exist: {path}")
    return str(path)


def read_parameter_file(path):
    if Path(path).suffix == '.json':
        with Path(path).open(encoding='utf-8') as stream:
            value = json.load(stream)
        if not isinstance(value, dict):
            raise ValueError(f'{path}: expected a JSON object')
        return value
    return read_yaml(path)


def parameter_snapshot(value, suffix):
    """Stable runtime files keep reloading a supervisor snapshot idempotent."""
    text = (json.dumps(value, ensure_ascii=False, indent=2) + '\n' if suffix == '.json'
            else yaml.safe_dump(value, sort_keys=False, allow_unicode=True))
    directory = Path(tempfile.gettempdir()) / f'inspection-parameters-{os.getuid()}'
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    path = directory / (hashlib.sha256(text.encode()).hexdigest() + suffix)
    if not path.exists() or path.read_text(encoding='utf-8') != text:
        with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=directory,
                                         delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(text)
        try:
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)
    return str(path)


def layered_parameter_file(value, root, model, overrides, origin):
    """Only an explicit root reference opts into managed parameter layers."""
    for prefix in ('${INSPECTION_CONFIG_DIR}/models/', '$INSPECTION_CONFIG_DIR/models/'):
        if value.startswith(prefix):
            relative = Path(value[len(prefix):])
            if relative.is_absolute() or '..' in relative.parts or relative.suffix not in ('.yaml', '.yml', '.json'):
                raise ValueError(f'{origin}: invalid model parameter path: {value}')
            result = read_parameter_file(root / 'models' / relative)
            return parameter_snapshot(merge(result, overrides), relative.suffix)
    prefix = next((p for p in ('${INSPECTION_CONFIG_DIR}/navigation/',
                              '$INSPECTION_CONFIG_DIR/navigation/')
                   if value.startswith(p)), None)
    if prefix is None:
        return None
    relative = Path(value[len(prefix):])
    if relative.is_absolute() or '..' in relative.parts or not relative.parts:
        raise ValueError(f'{origin}: invalid managed parameter path: {value}')
    if relative.suffix not in ('.yaml', '.yml', '.json'):
        raise ValueError(f'{origin}: managed parameters require YAML or JSON')
    base = root / 'navigation' / relative
    result = read_parameter_file(base)
    if model is None:
        robot_path = root / 'robot.yaml'
        model = read_yaml(robot_path).get('model', '') if robot_path.exists() else ''
    if model:
        if not isinstance(model, str) or not re.fullmatch(r'[A-Za-z0-9_-]+', model):
            raise ValueError(f'{origin}: invalid robot model')
        model_relative = relative
        if relative.name.startswith('param_default_'):
            model_relative = relative.with_name(
                relative.name.replace('param_default_', f'param_{model}_', 1))
        model_path = root / 'models' / model / 'navigation' / model_relative
        if model_path.exists():
            result = merge(result, read_parameter_file(model_path))
    return parameter_snapshot(merge(result, overrides), relative.suffix)


def resolve_files(config, module, origin, config_dir=None, model=None):
    config = copy.deepcopy(config)
    root = (Path(config_dir) if config_dir is not None else default_config_dir()).absolute()
    consumed = set()
    for parts, (package, directory) in FILE_FIELDS[module].items():
        cursor = config
        for key in parts[:-1]:
            cursor = cursor.get(key, {})
            if not isinstance(cursor, dict):
                raise ValueError(f"{origin}: {'.'.join(parts[:-1])} must be a mapping")
        value = cursor.get(parts[-1])
        if value not in (None, ""):
            if module == 'navigation' and parts[-1] != 'cyclonedds_config':
                if not isinstance(value, str):
                    raise ValueError(f'{origin}: {".".join(parts)}: expected a file path string')
                overrides = cursor.get('parameters', {})
                if not isinstance(overrides, dict):
                    raise ValueError(f'{origin}: {parts[0]}.parameters must be a mapping')
                managed = layered_parameter_file(value, root, model, overrides, origin)
                if managed is not None:
                    cursor[parts[-1]] = managed
                    consumed.add(parts[0])
                    continue
                if overrides:
                    raise ValueError(f'{origin}: {parts[0]}.parameters requires a managed '
                                     '${INSPECTION_CONFIG_DIR}/navigation/ or models/ file; '
                                     'standalone paths use complete files')
            cursor[parts[-1]] = file_path(
                value, package, directory, f"{origin}: {'.'.join(parts)}",
                check_exists=(module != "services"), config_dir=root)
    if module == 'navigation':
        for section, values in config.items():
            if isinstance(values, dict) and 'parameters' in values:
                if section not in consumed:
                    raise ValueError(f'{origin}: {section}.parameters has no managed parameter file')
                values.pop('parameters')
    return config


def load_config(module, path="", config_dir=None):
    """An explicit old-style path bypasses model and robot overlays entirely."""
    if module not in FILENAMES:
        raise ValueError(f"unknown configuration module: {module}")
    if path:
        origin = Path(os.path.expandvars(str(path))).expanduser().absolute()
        return resolve_files(read_yaml(origin), module, origin, config_dir=config_dir, model='')

    root = Path(config_dir) if config_dir is not None else default_config_dir()
    root = root.absolute()
    base_path = root / FILENAMES[module]
    config = read_yaml(base_path)
    robot_path = root / "robot.yaml"
    if not robot_path.exists():
        return resolve_files(config, module, base_path, config_dir=root, model='')

    robot = read_yaml(robot_path)
    unknown = set(robot) - {"model", "robot_id", *FILENAMES}
    if unknown:
        raise ValueError(f"{robot_path}: unknown fields: {sorted(unknown)}")
    model = robot.get("model")
    if not isinstance(model, str) or not re.fullmatch(r"[A-Za-z0-9_-]+", model):
        raise ValueError(f"{robot_path}: model must be a nonempty model directory name")
    model_dir = root / "models" / model
    if not model_dir.is_dir():
        raise ValueError(f"{robot_path}: unknown model: {model}")
    robot_id = robot.get("robot_id")
    if not isinstance(robot_id, str) or not robot_id.strip():
        raise ValueError(f"{robot_path}: robot_id must be a nonempty string")
    for key in FILENAMES:
        if key in robot and not isinstance(robot[key], dict):
            raise ValueError(f"{robot_path}: {key} must be a mapping")

    # Old services.yaml pins source files. In robot mode, remove only these
    # inherited paths so workers use the same automatic configuration root.
    if module == "services":
        for section, argument in (("navigation", "navigate_config_path"),
                                  ("inspection_system", "system_config_path")):
            config.get(section, {}).get("launch_args", {}).pop(argument, None)
    model_path = model_dir / FILENAMES[module]
    if model_path.exists():
        config = merge_module_config(config, read_yaml(model_path), module)
    if module == "system":
        config = merge(config, {"mqtt": {"sn": robot_id},
                                "mission_execution_agent": {"robot_id": robot_id}})
    config = merge_module_config(config, robot.get(module, {}), module)
    if module == "services":
        config.setdefault("service", {}).setdefault("environment", {})["INSPECTION_CONFIG_DIR"] = str(root)
    return resolve_files(config, module, root, config_dir=root, model=model)


def write_snapshot(config, directory=None):
    """Snapshots are runtime artifacts, never user-maintained configuration."""
    with tempfile.NamedTemporaryFile(mode="w", prefix="inspection-config-", suffix=".yaml",
                                     dir=directory, delete=False, encoding="utf-8") as stream:
        yaml.safe_dump(config, stream, sort_keys=False)
        return stream.name


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("module", choices=(*FILENAMES, "root"))
    parser.add_argument("--config", default="", help="Explicit standalone YAML; bypass robot.yaml.")
    parser.add_argument("--output", help="Write effective YAML to this file.")
    args = parser.parse_args()
    try:
        if args.module == "root":
            print(default_config_dir())
            return
        config = load_config(args.module, args.config)
        if args.output:
            with open(args.output, "w", encoding="utf-8") as stream:
                yaml.safe_dump(config, stream, sort_keys=False)
        else:
            print(yaml.safe_dump(config, sort_keys=False), end="")
    except (OSError, ValueError, yaml.YAMLError) as exc:
        parser.exit(2, f"configuration error: {exc}\n")


if __name__ == "__main__":
    main()
