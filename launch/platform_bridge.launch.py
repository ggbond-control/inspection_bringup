import os

import yaml

from ament_index_python.packages import get_package_share_directory
from launch import LaunchDescription
from launch.actions import DeclareLaunchArgument, OpaqueFunction, SetEnvironmentVariable
from launch.conditions import IfCondition
from launch.substitutions import LaunchConfiguration
from launch_ros.actions import Node
from launch_ros.parameter_descriptions import ParameterValue


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
        "acoustic_camera_stream_enabled": ParameterValue(
            as_bool(nested_config_value(config, "live_stream", "acoustic_camera", "enabled", False)),
            value_type=bool,
        ),
        "acoustic_camera_stream_topic": str(
            nested_config_value(config, "live_stream", "acoustic_camera", "topic", "/monitor/acoustic/camera")
        ),
        "acoustic_camera_stream_status_topic": str(
            nested_config_value(
                config,
                "live_stream",
                "acoustic_camera",
                "status_topic",
                "/platform/acoustic_camera_stream/status",
            )
        ),
        "acoustic_camera_stream_id": str(
            nested_config_value(config, "live_stream", "acoustic_camera", "stream_id", "x30_acoustic_camera")
        ),
        "acoustic_camera_stream_fps": ParameterValue(
            nested_config_value(config, "live_stream", "acoustic_camera", "fps", 10.0),
            value_type=float,
        ),
        "acoustic_camera_stream_bitrate": str(
            nested_config_value(config, "live_stream", "acoustic_camera", "bitrate", "1500k")
        ),
        "acoustic_camera_stream_video_codec": str(
            nested_config_value(config, "live_stream", "acoustic_camera", "video_codec", "h264_rkmpp")
        ),
        "acoustic_camera_stream_output_format": str(
            nested_config_value(config, "live_stream", "acoustic_camera", "output_format", "flv")
        ),
        "acoustic_camera_stream_restart_interval_sec": ParameterValue(
            nested_config_value(config, "live_stream", "acoustic_camera", "restart_interval_sec", 5.0),
            value_type=float,
        ),
        "acoustic_heatmap_stream_enabled": ParameterValue(
            as_bool(nested_config_value(config, "live_stream", "acoustic_heatmap", "enabled", False)),
            value_type=bool,
        ),
        "acoustic_heatmap_stream_topic": str(
            nested_config_value(config, "live_stream", "acoustic_heatmap", "topic", "/monitor/acoustic/heatmap")
        ),
        "acoustic_heatmap_stream_status_topic": str(
            nested_config_value(
                config,
                "live_stream",
                "acoustic_heatmap",
                "status_topic",
                "/platform/acoustic_heatmap_stream/status",
            )
        ),
        "acoustic_heatmap_stream_id": str(
            nested_config_value(config, "live_stream", "acoustic_heatmap", "stream_id", "x30_acoustic_heatmap")
        ),
        "acoustic_heatmap_stream_fps": ParameterValue(
            nested_config_value(config, "live_stream", "acoustic_heatmap", "fps", 10.0),
            value_type=float,
        ),
        "acoustic_heatmap_stream_bitrate": str(
            nested_config_value(config, "live_stream", "acoustic_heatmap", "bitrate", "1500k")
        ),
        "acoustic_heatmap_stream_video_codec": str(
            nested_config_value(config, "live_stream", "acoustic_heatmap", "video_codec", "h264_rkmpp")
        ),
        "acoustic_heatmap_stream_output_format": str(
            nested_config_value(config, "live_stream", "acoustic_heatmap", "output_format", "flv")
        ),
        "acoustic_heatmap_stream_restart_interval_sec": ParameterValue(
            nested_config_value(config, "live_stream", "acoustic_heatmap", "restart_interval_sec", 5.0),
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
    enable_mqtt = as_bool_text(
        override_or_config(context, "enable_mqtt", config, "modules", "mqtt", True)
    )

    platform_params = {
        "sn": override_or_config(context, "sn", config, "mqtt", "sn", "x30"),
        "mqtt_host": override_or_config(
            context, "mqtt_host", config, "mqtt", "host", "127.0.0.1"
        ),
        "mqtt_port": ParameterValue(
            override_or_config(context, "mqtt_port", config, "mqtt", "port", 1883),
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
                "/navigation_bringup/start",
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
        "localization_service_wait_timeout_sec": ParameterValue(
            config_value(config, "mqtt", "localization_service_wait_timeout_sec", 5.0),
            value_type=float,
        ),
        "acoustic_start_service_name": str(
            config_value(config, "mqtt", "acoustic_start_service_name", "/monitor/acoustic/start")
        ),
        "acoustic_stop_service_name": str(
            config_value(config, "mqtt", "acoustic_stop_service_name", "/monitor/acoustic/stop")
        ),
        "stand_service_name": str(
            config_value(config, "mqtt", "stand_service_name", "/nav_bridge_node/stand")
        ),
        "lie_service_name": str(
            config_value(config, "mqtt", "lie_service_name", "/nav_bridge_node/lie")
        ),
        "manual_jog_max_duration_ms": ParameterValue(
            config_value(config, "mqtt", "manual_jog_max_duration_ms", 2500),
            value_type=int,
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

    return [platform_mqtt_bridge]
