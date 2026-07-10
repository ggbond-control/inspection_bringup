import os

import yaml

from ament_index_python.packages import get_package_share_directory
from launch import LaunchDescription
from launch.actions import (
    DeclareLaunchArgument,
    IncludeLaunchDescription,
    OpaqueFunction,
    SetEnvironmentVariable,
)
from launch.conditions import IfCondition
from launch.launch_description_sources import PythonLaunchDescriptionSource
from launch.substitutions import LaunchConfiguration, PathJoinSubstitution
from launch_ros.actions import Node
from launch_ros.parameter_descriptions import ParameterValue
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


def nested_config_value(config, section, subsection, key, fallback):
    value = config.get(section, {}).get(subsection, {}).get(key, fallback)
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


def override_or_config_bool(context, name, config, section, key, fallback):
    override = LaunchConfiguration(name).perform(context)
    if override != "":
        return as_bool(override)
    return as_bool(config_value(config, section, key, fallback))


def override_or_config_typed(context, name, config, section, key, fallback, value_type):
    override = LaunchConfiguration(name).perform(context)
    value = override if override != "" else config_value(config, section, key, fallback)
    return value_type(value)


def include_package_launch(package_name, launch_file, condition_name=None, launch_arguments=None):
    condition = None
    if condition_name is not None:
        condition = IfCondition(LaunchConfiguration(condition_name))

    return IncludeLaunchDescription(
        PythonLaunchDescriptionSource(
            PathJoinSubstitution([FindPackageShare(package_name), "launch", launch_file])
        ),
        launch_arguments=(launch_arguments or {}).items(),
        condition=condition,
    )


def append_if_enabled(actions, enabled, action):
    if as_bool(enabled):
        actions.append(action)


def mqtt_base_prefix(config):
    vendor_prefix = str(config_value(config, "mqtt", "topic_vendor_prefix", "fh")).strip("/")
    topic_root = str(config_value(config, "mqtt", "topic_root", "device")).strip("/")
    return f"{vendor_prefix}/{topic_root}"


def live_stream_params(config):
    return {
        "live_stream_config_path": str(config_value(config, "live_stream", "config_path", "")),
        "live_stream_request_on_startup": ParameterValue(
            as_bool(config_value(config, "live_stream", "request_on_startup", True)),
            value_type=bool,
        ),
        "live_stream_enable_push": ParameterValue(
            as_bool(config_value(config, "live_stream", "enable_push", True)),
            value_type=bool,
        ),
        "live_stream_ffmpeg_bin": str(config_value(config, "live_stream", "ffmpeg_bin", "ffmpeg")),
        "live_stream_restart_interval_sec": ParameterValue(
            config_value(config, "live_stream", "restart_interval_sec", 5.0),
            value_type=float,
        ),
        "acoustic_overlay_stream_enabled": ParameterValue(
            as_bool(nested_config_value(config, "live_stream", "acoustic_overlay", "enabled", False)),
            value_type=bool,
        ),
        "acoustic_overlay_stream_topic": str(
            nested_config_value(config, "live_stream", "acoustic_overlay", "topic", "/monitor/acoustic/overlay")
        ),
        "acoustic_overlay_stream_status_topic": str(
            nested_config_value(
                config,
                "live_stream",
                "acoustic_overlay",
                "status_topic",
                "/platform/acoustic_overlay_stream/status",
            )
        ),
        "acoustic_overlay_stream_id": str(
            nested_config_value(config, "live_stream", "acoustic_overlay", "stream_id", "x30_acoustic_overlay")
        ),
        "acoustic_overlay_stream_fps": ParameterValue(
            nested_config_value(config, "live_stream", "acoustic_overlay", "fps", 10.0),
            value_type=float,
        ),
        "acoustic_overlay_stream_bitrate": str(
            nested_config_value(config, "live_stream", "acoustic_overlay", "bitrate", "1500k")
        ),
        "acoustic_overlay_stream_video_codec": str(
            nested_config_value(config, "live_stream", "acoustic_overlay", "video_codec", "h264_rkmpp")
        ),
        "acoustic_overlay_stream_output_format": str(
            nested_config_value(config, "live_stream", "acoustic_overlay", "output_format", "flv")
        ),
        "acoustic_overlay_stream_restart_interval_sec": ParameterValue(
            nested_config_value(config, "live_stream", "acoustic_overlay", "restart_interval_sec", 5.0),
            value_type=float,
        ),
        "live_stream_ffmpeg_loglevel": str(
            nested_config_value(config, "live_stream", "ffmpeg", "loglevel", "warning")
        ),
        "live_stream_ffmpeg_realtime_input": ParameterValue(
            as_bool(nested_config_value(config, "live_stream", "ffmpeg", "realtime_input", True)),
            value_type=bool,
        ),
        "live_stream_ffmpeg_rtsp_transport": str(
            nested_config_value(config, "live_stream", "ffmpeg", "rtsp_transport", "tcp")
        ),
        "live_stream_ffmpeg_video_codec": str(
            nested_config_value(config, "live_stream", "ffmpeg", "video_codec", "copy")
        ),
        "live_stream_ffmpeg_audio_codec": str(
            nested_config_value(config, "live_stream", "ffmpeg", "audio_codec", "copy")
        ),
        "live_stream_ffmpeg_output_format": str(
            nested_config_value(config, "live_stream", "ffmpeg", "output_format", "flv")
        ),
    }


def generate_launch_description():
    declared_arguments = [
        DeclareLaunchArgument(
            "system_config_path",
            default_value=default_system_config_path(),
            description="Bringup system configuration YAML path.",
        ),
        DeclareLaunchArgument(
            "enable_task_hub",
            default_value="",
            description="Start inspection task hub.",
        ),
        DeclareLaunchArgument(
            "enable_gimbal",
            default_value="",
            description="Start gimbal control stub.",
        ),
        DeclareLaunchArgument(
            "enable_charge",
            default_value="",
            description="Start charge executor.",
        ),
        DeclareLaunchArgument(
            "enable_sensors",
            default_value="",
            description="Start sensor and alarm modules.",
        ),
        DeclareLaunchArgument(
            "enable_alarm",
            default_value="",
            description="Start alarm manager when sensors are enabled.",
        ),
        DeclareLaunchArgument(
            "enable_gas",
            default_value="",
            description="Start gas monitor when sensors are enabled.",
        ),
        DeclareLaunchArgument(
            "enable_thermal",
            default_value="",
            description="Start thermal camera monitor when sensors are enabled.",
        ),
        DeclareLaunchArgument(
            "enable_acoustic",
            default_value="",
            description="Start acoustic monitor when sensors are enabled.",
        ),
        DeclareLaunchArgument(
            "enable_mqtt",
            default_value="",
            description="Start ROS 2 <-> MQTT platform bridge.",
        ),
        DeclareLaunchArgument(
            "sn",
            default_value="",
            description="Device serial number reported to the platform.",
        ),
        DeclareLaunchArgument(
            "mqtt_host",
            default_value="",
            description="MQTT broker host.",
        ),
        DeclareLaunchArgument(
            "mqtt_port",
            default_value="",
            description="MQTT broker port.",
        ),
        DeclareLaunchArgument(
            "default_route_config_path",
            default_value="",
            description="Default route config YAML path for task hub.",
        ),
        DeclareLaunchArgument(
            "runtime_log_directory",
            default_value="",
            description="Task hub runtime event log directory.",
        ),
        DeclareLaunchArgument(
            "stand_service_name",
            default_value="",
            description="Trigger service name for stand task.",
        ),
        DeclareLaunchArgument(
            "lie_service_name",
            default_value="",
            description="Trigger service name for lie task.",
        ),
        DeclareLaunchArgument(
            "set_gait_service_name",
            default_value="",
            description="SetParameters service name for set_gait task.",
        ),
        DeclareLaunchArgument(
            "navigation_action_name",
            default_value="",
            description="Navigation action name.",
        ),
        DeclareLaunchArgument(
            "gimbal_action_name",
            default_value="",
            description="Gimbal action name.",
        ),
        DeclareLaunchArgument(
            "capture_action_name",
            default_value="",
            description="Capture media action name.",
        ),
        DeclareLaunchArgument(
            "navigation_validate_waypoints_service",
            default_value="",
            description="Service name for waypoint passability validation.",
        ),
        DeclareLaunchArgument(
            "require_waypoint_validation_success",
            default_value="",
            description="Whether start_route fails when waypoint validation fails.",
        ),
        DeclareLaunchArgument(
            "navigation_heartbeat_topic",
            default_value="",
            description="Heartbeat topic for navigation module.",
        ),
        DeclareLaunchArgument(
            "gimbal_heartbeat_topic",
            default_value="",
            description="Heartbeat topic for gimbal module.",
        ),
        DeclareLaunchArgument(
            "odometry_topic",
            default_value="",
            description="Odometry topic used by dual_coordinate gimbal command.",
        ),
        DeclareLaunchArgument(
            "heartbeat_timeout_seconds",
            default_value="",
            description="Heartbeat timeout threshold in seconds.",
        ),
        DeclareLaunchArgument(
            "trigger_service_timeout_seconds",
            default_value="",
            description="Timeout in seconds for stand/lie trigger calls.",
        ),
        DeclareLaunchArgument(
            "gimbal_params_file",
            default_value="",
            description="Gimbal stub parameter file.",
        ),
        DeclareLaunchArgument(
            "camera_backend",
            default_value="",
            description="Gimbal stub camera backend.",
        ),
        DeclareLaunchArgument(
            "launch_post_waypoint_home_bridge",
            default_value="",
            description="Start gimbal post-waypoint home bridge.",
        ),
        DeclareLaunchArgument(
            "gimbal_hk_use_http_isapi_absolute_ptz",
            default_value="",
            description="Use HTTP ISAPI absolute PTZ control in gimbal stub.",
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

    default_route_config_path = os.path.join(
        get_package_share_directory("inspection_task_hub"),
        "config",
        "routes.yaml",
    )
    default_gimbal_params_file = os.path.join(
        get_package_share_directory("gimbal_control_stub"),
        "config",
        "stub_params.yaml",
    )

    enable_task_hub = as_bool_text(override_or_config(
        context, "enable_task_hub", config, "modules", "task_hub", True
    ))
    enable_gimbal = as_bool_text(override_or_config(
        context, "enable_gimbal", config, "modules", "gimbal", True
    ))
    enable_charge = as_bool_text(override_or_config(
        context, "enable_charge", config, "modules", "charge", True
    ))
    enable_sensors = as_bool_text(override_or_config(
        context, "enable_sensors", config, "modules", "sensors", True
    ))
    enable_alarm = as_bool_text(override_or_config(
        context, "enable_alarm", config, "modules", "alarm", True
    ))
    enable_gas = as_bool_text(override_or_config(context, "enable_gas", config, "modules", "gas", True))
    enable_thermal = as_bool_text(override_or_config(
        context, "enable_thermal", config, "modules", "thermal", True
    ))
    enable_acoustic = as_bool_text(override_or_config(
        context, "enable_acoustic", config, "modules", "acoustic", True
    ))
    enable_mqtt = as_bool_text(override_or_config(context, "enable_mqtt", config, "modules", "mqtt", True))

    task_hub_params = {
        "default_route_config_path": os.path.expanduser(
            override_or_config(
                context,
                "default_route_config_path",
                config,
                "task_hub",
                "default_route_config_path",
                default_route_config_path,
            )
        ),
        "stand_service_name": override_or_config(
            context, "stand_service_name", config, "task_hub", "stand_service_name", "/nav_bridge_node/stand"
        ),
        "lie_service_name": override_or_config(
            context, "lie_service_name", config, "task_hub", "lie_service_name", "/nav_bridge_node/lie"
        ),
        "set_gait_service_name": override_or_config(
            context,
            "set_gait_service_name",
            config,
            "task_hub",
            "set_gait_service_name",
            "/nav_bridge_node/set_gait",
        ),
        "navigation_action_name": override_or_config(
            context,
            "navigation_action_name",
            config,
            "task_hub",
            "navigation_action_name",
            "multi_map_navigate_to_pose",
        ),
        "gimbal_action_name": override_or_config(
            context, "gimbal_action_name", config, "task_hub", "gimbal_action_name", "follow_joint_trajectory"
        ),
        "navigation_validate_waypoints_service": override_or_config(
            context,
            "navigation_validate_waypoints_service",
            config,
            "task_hub",
            "navigation_validate_waypoints_service",
            "/validate_route_waypoints",
        ),
        "require_waypoint_validation_success": ParameterValue(
            override_or_config_bool(
                context,
                "require_waypoint_validation_success",
                config,
                "task_hub",
                "require_waypoint_validation_success",
                False,
            ),
            value_type=bool,
        ),
        "capture_action_name": override_or_config(
            context, "capture_action_name", config, "task_hub", "capture_action_name", "capture_media"
        ),
        "navigation_heartbeat_topic": override_or_config(
            context,
            "navigation_heartbeat_topic",
            config,
            "task_hub",
            "navigation_heartbeat_topic",
            "/inspection_task_hub/heartbeat/navigation",
        ),
        "gimbal_heartbeat_topic": override_or_config(
            context,
            "gimbal_heartbeat_topic",
            config,
            "task_hub",
            "gimbal_heartbeat_topic",
            "/inspection_task_hub/heartbeat/gimbal",
        ),
        "odometry_topic": override_or_config(
            context, "odometry_topic", config, "task_hub", "odometry_topic", "/odometry_horizon"
        ),
        "robot_basic_state_topic": override_or_config(
            context,
            "robot_basic_state_topic",
            config,
            "task_hub",
            "robot_basic_state_topic",
            "/robot_basic_state",
        ),
        "charge_manager_state_topic": override_or_config(
            context,
            "charge_manager_state_topic",
            config,
            "task_hub",
            "charge_manager_state_topic",
            "/charge_manager_state",
        ),
        "heartbeat_timeout_seconds": ParameterValue(
            override_or_config_typed(
                context,
                "heartbeat_timeout_seconds",
                config,
                "task_hub",
                "heartbeat_timeout_seconds",
                3.0,
                float,
            ),
            value_type=float,
        ),
        "robot_state_timeout_seconds": ParameterValue(
            override_or_config_typed(
                context,
                "robot_state_timeout_seconds",
                config,
                "task_hub",
                "robot_state_timeout_seconds",
                3.0,
                float,
            ),
            value_type=float,
        ),
        "trigger_service_timeout_seconds": ParameterValue(
            override_or_config_typed(
                context,
                "trigger_service_timeout_seconds",
                config,
                "task_hub",
                "trigger_service_timeout_seconds",
                10.0,
                float,
            ),
            value_type=float,
        ),
        "runtime_log_directory": os.path.expanduser(
            override_or_config(
                context,
                "runtime_log_directory",
                config,
                "task_hub",
                "runtime_log_directory",
                "~/runtime_logs",
            )
        ),
    }

    task_hub_node = Node(
        package="inspection_task_hub",
        executable="task_hub_node",
        name="task_hub_node",
        output="screen",
        emulate_tty=True,
        prefix=["stdbuf -o L -e L"],
        condition=IfCondition(enable_task_hub),
        parameters=[task_hub_params],
    )

    platform_params = {
        "sn": override_or_config(context, "sn", config, "mqtt", "sn", "x30"),
        "mqtt_host": override_or_config(
            context, "mqtt_host", config, "mqtt", "host", "127.0.0.1"
        ),
        "mqtt_port": ParameterValue(
            override_or_config_typed(context, "mqtt_port", config, "mqtt", "port", 1883, int),
            value_type=int,
        ),
        "mqtt_base_prefix": mqtt_base_prefix(config),
        "map_root_directory": str(
            config_value(config, "mqtt", "map_root_directory", "/home/cat/Workspace/Maps")
        ),
        "localization_set_parameters_service": str(
            config_value(
                config,
                "mqtt",
                "localization_set_parameters_service",
                "/rai_inspection_agent/set_parameters",
            )
        ),
        "localization_map_parameter_name": str(
            config_value(config, "mqtt", "localization_map_parameter_name", "slam.prior_dir")
        ),
        "localization_initial_map_parameter_name": str(
            config_value(
                config,
                "mqtt",
                "localization_initial_map_parameter_name",
                "global_planner.initial_map",
            )
        ),
        "localization_set_parameter_timeout_sec": ParameterValue(
            config_value(config, "mqtt", "localization_set_parameter_timeout_sec", 60.0),
            value_type=float,
        ),
    }
    platform_params.update(live_stream_params(config))

    platform_mqtt_bridge = Node(
        package="inspection_platform_bridge",
        executable="platform_mqtt_bridge_node",
        name="platform_mqtt_bridge_node",
        output="screen",
        emulate_tty=True,
        prefix=["stdbuf -o L -e L"],
        condition=IfCondition(enable_mqtt),
        parameters=[platform_params],
    )

    gimbal_params_file = os.path.expanduser(
        override_or_config(
            context, "gimbal_params_file", config, "gimbal", "params_file", default_gimbal_params_file
        )
    )

    actions = [task_hub_node]
    append_if_enabled(
        actions,
        enable_gimbal,
        include_package_launch(
            "gimbal_control_stub",
            "gimbal_stub.launch.py",
            None,
            {
                "params_file": gimbal_params_file,
                "camera_backend": override_or_config(
                    context, "camera_backend", config, "gimbal", "camera_backend", "gimbal_hk"
                ),
                "launch_post_waypoint_home_bridge": as_bool_text(
                    override_or_config_bool(
                        context,
                        "launch_post_waypoint_home_bridge",
                        config,
                        "gimbal",
                        "launch_post_waypoint_home_bridge",
                        False,
                    )
                ),
                "inspection_route_config_path": task_hub_params["default_route_config_path"],
                "gimbal_hk_use_http_isapi_absolute_ptz": as_bool_text(
                    override_or_config_bool(
                        context,
                        "gimbal_hk_use_http_isapi_absolute_ptz",
                        config,
                        "gimbal",
                        "use_http_isapi_absolute_ptz",
                        True,
                    )
                ),
            },
        ),
    )
    append_if_enabled(
        actions,
        enable_charge,
        include_package_launch(
            "inspection_charge_executor",
            "inspection_charge_executor.launch.py",
            None,
        ),
    )
    append_if_enabled(
        actions,
        enable_sensors,
        include_package_launch(
            "inspection_bringup",
            "sensors.launch.py",
            None,
            {
                "enable_alarm": enable_alarm,
                "enable_gas": enable_gas,
                "enable_thermal": enable_thermal,
                "enable_acoustic": enable_acoustic,
            },
        ),
    )
    actions.append(platform_mqtt_bridge)
    return actions
