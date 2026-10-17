#!/usr/bin/env python3
"""Fixed defaults -> model -> robot configuration, shared by all entrypoints."""

import argparse
import copy
import os
from pathlib import Path
import re
import tempfile

import yaml


FILENAMES = {"system": "system.yaml", "navigation": "navigate.yaml", "services": "services.yaml"}
# Relative file paths always use the owning package's configuration directory.
# global_planner.params_file remains the legacy extensionless selector.
FILE_FIELDS = {
    "system": {
        ("gimbal", "params_file"): ("sensor_gimbal", "config"),
        ("live_stream", "config_path"): ("inspection_platform_bridge", "config"),
        ("gas", "params_file"): ("gas_monitor", "config"),
        ("sensor_rs485", "params_file"): ("sensor_rs485", "config"),
    },
    "navigation": {
        ("nav_bridge", "params_file"): ("nav_bridge", "config"),
        ("nav_bridge", "cyclonedds_config"): ("nav_bridge", "config"),
        ("livox", "user_config_path"): ("livox_ros_driver2", "config"),
        ("slam", "config"): ("faster_lio", "config"),
        ("terrain", "params_file"): ("gridmapper", "config"),
        ("local_planner", "config"): ("local_planner", "config"),
        ("global_planner", "params_path"): ("multi_map_nav", "params"),
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


def apply_module_switches(sequence, switches):
    """Retain CLI module switches across supervisor/worker process boundaries."""
    result = [name for name in sequence if switches.get(name, True)]
    extensions = ("terrain", "local_planner", "global_planner")
    for name, enabled in switches.items():
        if enabled and name not in result:
            index = next((i for i, value in enumerate(result) if value in extensions), len(result))
            result.insert(index if name not in extensions else len(result), name)
    return result


def file_path(value, package, directory, origin, check_exists=True):
    if not isinstance(value, str):
        raise ValueError(f"{origin}: expected a file path string, got {value!r}")
    expanded = Path(os.path.expandvars(value)).expanduser()
    if expanded.is_absolute():
        path = expanded
    else:
        if package == "inspection_bringup":
            base = default_config_dir()
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


def resolve_files(config, module, origin):
    config = copy.deepcopy(config)
    for parts, (package, directory) in FILE_FIELDS[module].items():
        cursor = config
        for key in parts[:-1]:
            cursor = cursor.get(key, {})
            if not isinstance(cursor, dict):
                raise ValueError(f"{origin}: {'.'.join(parts[:-1])} must be a mapping")
        value = cursor.get(parts[-1])
        if value not in (None, ""):
            cursor[parts[-1]] = file_path(
                value, package, directory, f"{origin}: {'.'.join(parts)}",
                check_exists=(module != "services"))
    return config


def load_config(module, path="", config_dir=None):
    """An explicit old-style path bypasses model and robot overlays entirely."""
    if module not in FILENAMES:
        raise ValueError(f"unknown configuration module: {module}")
    if path:
        origin = Path(os.path.expandvars(str(path))).expanduser().absolute()
        return resolve_files(read_yaml(origin), module, origin)

    root = Path(config_dir) if config_dir is not None else default_config_dir()
    root = root.absolute()
    base_path = root / FILENAMES[module]
    config = read_yaml(base_path)
    robot_path = root / "robot.yaml"
    if not robot_path.exists():
        return resolve_files(config, module, base_path)

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
        config = merge(config, read_yaml(model_path))
    if module == "system":
        config = merge(config, {"mqtt": {"sn": robot_id},
                                "mission_execution_agent": {"robot_id": robot_id}})
    config = merge(config, robot.get(module, {}))
    if module == "services":
        config.setdefault("service", {}).setdefault("environment", {})["INSPECTION_CONFIG_DIR"] = str(root)
    return resolve_files(config, module, root)


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
