import os

import yaml

from ament_index_python.packages import get_package_share_directory
from launch import LaunchDescription
from launch.actions import DeclareLaunchArgument, IncludeLaunchDescription, OpaqueFunction, SetEnvironmentVariable
from launch.conditions import IfCondition
from launch.launch_description_sources import PythonLaunchDescriptionSource
from launch.substitutions import LaunchConfiguration, PathJoinSubstitution
from launch_ros.substitutions import FindPackageShare


def default_system_config_path():
    return os.path.join(
        get_package_share_directory("inspection_bringup"),
        "config",
        "system.yaml",
    )


def load_system_config(path):
    expanded_path = os.path.expanduser(os.path.expandvars(path))
    if not expanded_path or not os.path.exists(expanded_path):
        return {}

    with open(expanded_path, "r", encoding="utf-8") as config_file:
        return yaml.safe_load(config_file) or {}


def config_value(config, section, key, fallback):
    value = config.get(section, {}).get(key, fallback)
    if value is None or value == "":
        return fallback
    return value


def as_bool(value):
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in ("1", "true", "yes", "on")


def as_bool_text(value):
    return "true" if as_bool(value) else "false"


def override_or_config(context, name, config, section, key, fallback):
    override = LaunchConfiguration(name).perform(context)
    if override != "":
        return override
    return str(config_value(config, section, key, fallback))


def include_package_launch(package_name, launch_file, enabled):
    return IncludeLaunchDescription(
        PythonLaunchDescriptionSource(
            PathJoinSubstitution([FindPackageShare(package_name), "launch", launch_file])
        ),
        condition=IfCondition(enabled),
    )


def generate_launch_description():
    declared_arguments = [
        DeclareLaunchArgument(
            "system_config_path",
            default_value=default_system_config_path(),
            description="Bringup system configuration YAML path.",
        ),
        DeclareLaunchArgument(
            "enable_alarm",
            default_value="",
            description="Start alarm manager.",
        ),
        DeclareLaunchArgument(
            "enable_gas",
            default_value="",
            description="Start gas monitor.",
        ),
        DeclareLaunchArgument(
            "enable_thermal",
            default_value="",
            description="Start thermal camera monitor.",
        ),
        DeclareLaunchArgument(
            "enable_acoustic",
            default_value="",
            description="Start acoustic monitor.",
        ),
    ]

    return LaunchDescription(
        [
            SetEnvironmentVariable(
                "RCUTILS_CONSOLE_OUTPUT_FORMAT",
                "[{time}] [{severity}] [{name}]: {message}",
            ),
            SetEnvironmentVariable("RCUTILS_COLORIZED_OUTPUT", "1"),
            SetEnvironmentVariable("RCUTILS_LOGGING_BUFFERED_STREAM", "0"),
        ]
        + declared_arguments
        + [OpaqueFunction(function=launch_setup)]
    )


def launch_setup(context):
    config = load_system_config(LaunchConfiguration("system_config_path").perform(context))
    enable_alarm = as_bool_text(
        override_or_config(context, "enable_alarm", config, "modules", "alarm", True)
    )
    enable_gas = as_bool_text(
        override_or_config(context, "enable_gas", config, "modules", "gas", True)
    )
    enable_thermal = as_bool_text(
        override_or_config(context, "enable_thermal", config, "modules", "thermal", True)
    )
    enable_acoustic = as_bool_text(
        override_or_config(context, "enable_acoustic", config, "modules", "acoustic", True)
    )

    return [
        include_package_launch("alarm_manager", "alarm_manager.launch.py", enable_alarm),
        include_package_launch("gas_monitor", "gas_monitor.launch.py", enable_gas),
        include_package_launch(
            "thermal_camera_monitor",
            "thermal_camera_monitor.launch.py",
            enable_thermal,
        ),
        include_package_launch("acoustic_monitor", "acoustic_monitor.launch.py", enable_acoustic),
    ]
