import os

import yaml

from ament_index_python.packages import get_package_share_directory
from launch import LaunchDescription
from launch.actions import (
    DeclareLaunchArgument,
    EmitEvent,
    ExecuteProcess,
    IncludeLaunchDescription,
    OpaqueFunction,
    RegisterEventHandler,
    SetEnvironmentVariable,
    TimerAction,
)
from launch.events import Shutdown
from launch.conditions import IfCondition
from launch.event_handlers import OnProcessExit
from launch.launch_description_sources import PythonLaunchDescriptionSource
from launch.substitutions import LaunchConfiguration, PathJoinSubstitution
from launch_ros.substitutions import FindPackageShare


def default_navigate_config_path():
    return os.path.join(
        get_package_share_directory("inspection_bringup"),
        "config",
        "navigate.yaml",
    )


def readiness_script_path():
    return os.path.abspath(
        os.path.join(os.path.dirname(__file__), "..", "scripts", "wait_for_ready.py")
    )


def load_config(path):
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


def override_or_config_bool(context, name, config, section, key, fallback):
    override = LaunchConfiguration(name).perform(context)
    if override != "":
        return as_bool(override)
    return as_bool(config_value(config, section, key, fallback))


def override_or_config_typed(context, name, config, section, key, fallback, value_type):
    override = LaunchConfiguration(name).perform(context)
    value = override if override != "" else config_value(config, section, key, fallback)
    return value_type(value)


def split_list_text(value):
    if isinstance(value, list):
        return [str(item) for item in value]
    return [item.strip() for item in str(value).split(",") if item.strip()]


def override_or_config_list(context, name, config, section, key, fallback):
    override = LaunchConfiguration(name).perform(context)
    if override != "":
        return split_list_text(override)
    return config_list(config, section, key, fallback)


def config_list(config, section, key, fallback):
    value = config.get(section, {}).get(key, fallback)
    if value is None or value == "":
        return fallback
    if isinstance(value, list):
        return [str(item) for item in value]
    return [str(value)]


def config_sequence(config):
    return config_list(
        config,
        "bringup",
        "sequence",
        ["nav_bridge", "livox", "slam", "terrain", "local_planner", "global_planner"],
    )


def module_config(config, section):
    value = config.get(section, {})
    return value if isinstance(value, dict) else {}


def readiness_config(config, section):
    value = module_config(config, section).get("readiness", {})
    return value if isinstance(value, dict) else {}


def readiness_value(config, section, key, fallback):
    value = readiness_config(config, section).get(key, fallback)
    if value is None or value == "":
        return fallback
    return value


def readiness_list(config, section, key, fallback):
    value = readiness_value(config, section, key, fallback)
    return split_list_text(value)


def include_package_launch(package_name, launch_file, enabled, launch_arguments=None):
    return IncludeLaunchDescription(
        PythonLaunchDescriptionSource(
            PathJoinSubstitution([FindPackageShare(package_name), "launch", launch_file])
        ),
        launch_arguments=(launch_arguments or {}).items(),
        condition=IfCondition(enabled),
    )


def delayed_include(delay_seconds, action):
    if delay_seconds <= 0.0:
        return action
    return TimerAction(period=delay_seconds, actions=[action])


def delayed_actions(delay_seconds, actions):
    if delay_seconds <= 0.0:
        return actions
    return [TimerAction(period=delay_seconds, actions=actions)]


def wait_for_nodes_action(name, nodes, timeout):
    return ExecuteProcess(
        cmd=[
            "python3",
            readiness_script_path(),
            "nodes",
            "--name",
            name,
            "--timeout",
            str(timeout),
            *nodes,
        ],
        name=f"wait_for_{name}_nodes",
        output="screen",
    )


def nav_bridge_ready_action(topics, stand_service, topic_timeout, stand_timeout):
    topic_args = []
    for topic in topics:
        topic_args.extend(["--topic", topic])

    return ExecuteProcess(
        cmd=[
            "python3",
            readiness_script_path(),
            "nav_bridge",
            "--stand-service",
            stand_service,
            "--topic-timeout",
            str(topic_timeout),
            "--stand-timeout",
            str(stand_timeout),
            *topic_args,
        ],
        name="wait_for_nav_bridge_ready",
        output="screen",
    )


def localization_init_ready_action(status_topic, timeout, blocked_is_failure):
    cmd = [
        "python3",
        readiness_script_path(),
        "localization-init",
        "--status-topic",
        status_topic,
        "--timeout",
        str(timeout),
    ]
    if blocked_is_failure:
        cmd.append("--blocked-is-failure")

    return ExecuteProcess(
        cmd=cmd,
        name="wait_for_slam_localization_init",
        output="screen",
    )


def module_readiness_action(context, config, section, fallback_nodes, fallback_timeout):
    ready_type = str(readiness_value(config, section, "type", "nodes"))

    if ready_type == "nodes":
        return wait_for_nodes_action(
            section,
            readiness_list(config, section, "nodes", fallback_nodes),
            float(readiness_value(config, section, "timeout_seconds", fallback_timeout)),
        )

    if ready_type == "topics":
        return ExecuteProcess(
            cmd=[
                "python3",
                readiness_script_path(),
                "topics",
                "--name",
                section,
                "--timeout",
                str(float(readiness_value(config, section, "timeout_seconds", fallback_timeout))),
                *readiness_list(config, section, "topics", []),
            ],
            name=f"wait_for_{section}_topics",
            output="screen",
        )

    if ready_type == "nav_bridge":
        return nav_bridge_ready_action(
            override_or_config_list(
                context,
                "nav_bridge_wait_topics",
                {"nav_bridge": readiness_config(config, "nav_bridge")},
                "nav_bridge",
                "topics",
                readiness_list(config, section, "topics", ["/battery/level"]),
            ),
            override_or_config(
                context,
                "nav_bridge_stand_service",
                {"nav_bridge": readiness_config(config, "nav_bridge")},
                "nav_bridge",
                "stand_service",
                "/nav_bridge_node/stand",
            ),
            override_or_config_typed(
                context,
                "nav_bridge_topic_timeout_seconds",
                {"nav_bridge": readiness_config(config, "nav_bridge")},
                "nav_bridge",
                "topic_timeout_seconds",
                fallback_timeout,
                float,
            ),
            override_or_config_typed(
                context,
                "nav_bridge_stand_timeout_seconds",
                {"nav_bridge": readiness_config(config, "nav_bridge")},
                "nav_bridge",
                "stand_timeout_seconds",
                fallback_timeout,
                float,
            ),
        )

    if ready_type == "localization_init":
        return localization_init_ready_action(
            str(readiness_value(config, section, "status_topic", "/localization_init_status")),
            float(readiness_value(config, section, "timeout_seconds", 120.0)),
            as_bool(readiness_value(config, section, "blocked_is_failure", False)),
        )

    print(f"[navigation] unknown readiness type for {section}: {ready_type}; falling back to nodes")
    return wait_for_nodes_action(section, fallback_nodes, fallback_timeout)


def module_readiness_type(config, section):
    return str(readiness_value(config, section, "type", "nodes"))


def append_navigation_group(
    actions,
    previous_wait,
    launch_action,
    wait_action,
    delay_seconds,
    shutdown_on_readiness_failure,
):
    if previous_wait is None:
        actions.extend([launch_action, wait_action])
        return wait_action

    next_actions = [launch_action, wait_action]
    if delay_seconds > 0.0:
        next_actions = [TimerAction(period=delay_seconds, actions=next_actions)]

    def on_previous_wait_exit(event, _context):
        if event.returncode != 0:
            reason = (
                f"readiness check failed with code {event.returncode}; "
                "not starting the next module"
            )
            print(
                f"[navigation] {reason}"
            )
            if shutdown_on_readiness_failure:
                return [EmitEvent(event=Shutdown(reason=reason))]
            return []
        return next_actions

    actions.append(
        RegisterEventHandler(
            OnProcessExit(
                target_action=previous_wait,
                on_exit=on_previous_wait_exit,
            )
        )
    )
    return wait_action


def generate_launch_description():
    declared_arguments = [
        DeclareLaunchArgument(
            "navigate_config_path",
            default_value=default_navigate_config_path(),
            description="Navigation configuration YAML path.",
        ),
        DeclareLaunchArgument("enable_nav_bridge", default_value="", description="Start nav_bridge."),
        DeclareLaunchArgument(
            "nav_bridge_wait_topics",
            default_value="",
            description="Comma-separated topics used to verify nav_bridge readiness.",
        ),
        DeclareLaunchArgument(
            "nav_bridge_stand_service",
            default_value="",
            description="Trigger service called after nav_bridge topic data is available.",
        ),
        DeclareLaunchArgument(
            "nav_bridge_topic_timeout_seconds",
            default_value="",
            description="Timeout for waiting for nav_bridge topic data.",
        ),
        DeclareLaunchArgument(
            "nav_bridge_stand_timeout_seconds",
            default_value="",
            description="Timeout for the nav_bridge stand service call.",
        ),
        DeclareLaunchArgument("enable_livox", default_value="", description="Start MID360 Livox driver."),
        DeclareLaunchArgument("enable_slam", default_value="", description="Start faster_lio localization."),
        DeclareLaunchArgument("enable_terrain", default_value="", description="Start gridmapper local terrain."),
        DeclareLaunchArgument("enable_local_planner", default_value="", description="Start local planner."),
        DeclareLaunchArgument("enable_global_planner", default_value="", description="Start multi-map navigation."),
        DeclareLaunchArgument(
            "navigation_start_delay_seconds",
            default_value="",
            description="Delay between navigation launch groups.",
        ),
        DeclareLaunchArgument("slam_relocal", default_value="", description="Enable faster_lio relocal mode."),
        DeclareLaunchArgument("slam_prior_dir", default_value="", description="faster_lio prior directory."),
        DeclareLaunchArgument("slam_rviz", default_value="", description="Start faster_lio RViz."),
        DeclareLaunchArgument("slam_pgo", default_value="", description="Start faster_lio PGO."),
        DeclareLaunchArgument("slam_odom_imu", default_value="", description="Enable faster_lio IMU odometry."),
        DeclareLaunchArgument("slam_use_sim_time", default_value="", description="Use sim time for faster_lio."),
        DeclareLaunchArgument("terrain_rviz", default_value="", description="Start gridmapper RViz."),
        DeclareLaunchArgument("terrain_use_gpu", default_value="", description="Enable gridmapper GPU acceleration."),
        DeclareLaunchArgument(
            "local_planner_use_sim_time",
            default_value="",
            description="Use sim time for local planner.",
        ),
        DeclareLaunchArgument(
            "local_planner_start_rviz",
            default_value="",
            description="Start local planner RViz.",
        ),
        DeclareLaunchArgument("global_initial_map", default_value="", description="Initial multi-map map name."),
        DeclareLaunchArgument(
            "global_map_connections_file",
            default_value="",
            description="Multi-map connections file name without extension.",
        ),
        DeclareLaunchArgument(
            "global_use_fake_cmdvel",
            default_value="",
            description="Use cmd_vel_fake in multi-map navigation.",
        ),
        DeclareLaunchArgument(
            "global_params_file",
            default_value="",
            description="Multi-map params file name without extension.",
        ),
        DeclareLaunchArgument(
            "global_use_sim_time",
            default_value="",
            description="Use sim time for multi-map navigation.",
        ),
        DeclareLaunchArgument(
            "global_bidirectional_connections",
            default_value="",
            description="Generate reverse map connections.",
        ),
        DeclareLaunchArgument(
            "global_waypoint_dwell_time",
            default_value="",
            description="Dwell time at through-poses waypoints.",
        ),
        DeclareLaunchArgument("global_patrol_loops", default_value="", description="Patrol loop count."),
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
    config = load_config(LaunchConfiguration("navigate_config_path").perform(context))
    delay = override_or_config_typed(
        context,
        "navigation_start_delay_seconds",
        config,
        "bringup",
        "start_delay_seconds",
        1.0,
        float,
    )
    should_wait_for_readiness = as_bool(
        config_value(
            config,
            "bringup",
            "wait_for_readiness",
            config_value(config, "bringup", "wait_for_nodes", True),
        )
    )
    shutdown_on_readiness_failure = as_bool(
        config_value(config, "bringup", "shutdown_on_readiness_failure", True)
    )
    wait_timeout = float(config_value(config, "bringup", "wait_timeout_seconds", 10.0))

    nav_bridge_launch = include_package_launch(
        "nav_bridge",
        "nav_bridge.launch.py",
        "true",
    )
    nav_bridge_enabled = override_or_config_bool(
        context, "enable_nav_bridge", config, "modules", "nav_bridge", True
    )

    livox_launch = include_package_launch(
        "livox_ros_driver2",
        "msg_multi_MID360_launch.py",
        "true",
    )
    livox_enabled = override_or_config_bool(context, "enable_livox", config, "modules", "livox", True)
    slam_launch = include_package_launch(
        "faster_lio",
        "slam.launch.py",
        "true",
        {
            "relocal": as_bool_text(
                override_or_config_bool(context, "slam_relocal", config, "slam", "relocal", True)
            ),
            "prior_dir": override_or_config(
                context, "slam_prior_dir", config, "slam", "prior_dir", "company2"
            ),
            "rviz": as_bool_text(
                override_or_config_bool(context, "slam_rviz", config, "slam", "rviz", False)
            ),
            "pgo": as_bool_text(
                override_or_config_bool(context, "slam_pgo", config, "slam", "pgo", False)
            ),
            "odom_imu": as_bool_text(
                override_or_config_bool(context, "slam_odom_imu", config, "slam", "odom_imu", True)
            ),
            "use_sim_time": as_bool_text(
                override_or_config_bool(
                    context, "slam_use_sim_time", config, "slam", "use_sim_time", False
                )
            ),
        },
    )
    slam_enabled = override_or_config_bool(context, "enable_slam", config, "modules", "slam", True)
    terrain_launch = include_package_launch(
        "gridmapper",
        "local.launch.py",
        "true",
        {
            "rviz": as_bool_text(
                override_or_config_bool(context, "terrain_rviz", config, "terrain", "rviz", False)
            ),
            "use_gpu": as_bool_text(
                override_or_config_bool(context, "terrain_use_gpu", config, "terrain", "use_gpu", True)
            ),
        },
    )
    terrain_enabled = override_or_config_bool(context, "enable_terrain", config, "modules", "terrain", True)
    local_planner_launch = include_package_launch(
        "local_planner",
        "local_planner.launch.py",
        "true",
        {
            "use_sim_time": as_bool_text(
                override_or_config_bool(
                    context,
                    "local_planner_use_sim_time",
                    config,
                    "local_planner",
                    "use_sim_time",
                    False,
                )
            ),
            "start_rviz": as_bool_text(
                override_or_config_bool(
                    context,
                    "local_planner_start_rviz",
                    config,
                    "local_planner",
                    "start_rviz",
                    False,
                )
            ),
        },
    )
    local_planner_enabled = override_or_config_bool(
        context, "enable_local_planner", config, "modules", "local_planner", True
    )
    global_planner_launch = include_package_launch(
        "multi_map_nav",
        "multi_map_nav.launch.py",
        "true",
        {
            "initial_map": override_or_config(
                context, "global_initial_map", config, "global_planner", "initial_map", "company2"
            ),
            "map_connections_file": override_or_config(
                context,
                "global_map_connections_file",
                config,
                "global_planner",
                "map_connections_file",
                "default",
            ),
            "use_fake_cmdvel": as_bool_text(
                override_or_config_bool(
                    context, "global_use_fake_cmdvel", config, "global_planner", "use_fake_cmdvel", True
                )
            ),
            "params_file": override_or_config(
                context, "global_params_file", config, "global_planner", "params_file", "new_local"
            ),
            "use_sim_time": as_bool_text(
                override_or_config_bool(
                    context, "global_use_sim_time", config, "global_planner", "use_sim_time", False
                )
            ),
            "patrol_loops": str(
                override_or_config_typed(
                    context, "global_patrol_loops", config, "global_planner", "patrol_loops", 1, int
                )
            ),
            "bidirectional_connections": as_bool_text(
                override_or_config_bool(
                    context,
                    "global_bidirectional_connections",
                    config,
                    "global_planner",
                    "bidirectional_connections",
                    True,
                )
            ),
            "waypoint_dwell_time": str(
                override_or_config_typed(
                    context,
                    "global_waypoint_dwell_time",
                    config,
                    "global_planner",
                    "waypoint_dwell_time",
                    2.0,
                    float,
                )
            ),
        },
    )
    global_planner_enabled = override_or_config_bool(
        context, "enable_global_planner", config, "modules", "global_planner", True
    )

    navigation_groups = {
        "nav_bridge": (
            nav_bridge_enabled,
            nav_bridge_launch,
            module_readiness_action(context, config, "nav_bridge", [], wait_timeout),
        ),
        "livox": (
            livox_enabled,
            livox_launch,
            module_readiness_action(context, config, "livox", ["/livox_lidar_publisher"], wait_timeout),
        ),
        "slam": (
            slam_enabled,
            slam_launch,
            module_readiness_action(context, config, "slam", ["/laser_mapping"], wait_timeout),
        ),
        "terrain": (
            terrain_enabled,
            terrain_launch,
            module_readiness_action(context, config, "terrain", ["/gridmapper_node"], wait_timeout),
        ),
        "local_planner": (
            local_planner_enabled,
            local_planner_launch,
            module_readiness_action(
                context,
                config,
                "local_planner",
                ["/localPlanner", "/pathFollower"],
                wait_timeout,
            ),
        ),
        "global_planner": (
            global_planner_enabled,
            global_planner_launch,
            module_readiness_action(
                context,
                config,
                "global_planner",
                ["/planner_server", "/controller_server"],
                wait_timeout,
            ),
        ),
    }

    ordered_navigation_groups = []
    seen_group_names = set()
    for name in config_sequence(config):
        if name in seen_group_names:
            print(f"[navigation] duplicate sequence entry skipped: {name}")
            continue
        seen_group_names.add(name)

        group = navigation_groups.get(name)
        if group is None:
            print(f"[navigation] unknown sequence entry skipped: {name}")
            continue

        enabled, launch_action, ready_action = group
        ordered_navigation_groups.append((enabled, launch_action, name, ready_action))

    if not should_wait_for_readiness:
        actions = []
        launch_index = 0
        for enabled, launch_action, name, ready_action in ordered_navigation_groups:
            if enabled:
                start_delay = delay * launch_index
                if name == "nav_bridge" and module_readiness_type(config, name) == "nav_bridge":
                    actions.extend(delayed_actions(start_delay, [launch_action, ready_action]))
                else:
                    actions.append(delayed_include(start_delay, launch_action))
                launch_index += 1
        return actions

    actions = []
    previous_wait = None
    for enabled, launch_action, _name, ready_action in ordered_navigation_groups:
        if not enabled:
            continue
        previous_wait = append_navigation_group(
            actions,
            previous_wait,
            launch_action,
            ready_action,
            delay,
            shutdown_on_readiness_failure,
        )

    return actions
