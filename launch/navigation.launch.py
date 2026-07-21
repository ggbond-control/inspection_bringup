import hashlib
import json
import os
import time

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


def supervisor_script_path():
    return os.path.abspath(
        os.path.join(os.path.dirname(__file__), "..", "scripts", "navigation_supervisor.py")
    )


def navigation_state_dir(config_path):
    expanded_path = os.path.abspath(os.path.expanduser(os.path.expandvars(config_path)))
    basename = os.path.splitext(os.path.basename(expanded_path))[0] or "navigate"
    path_hash = hashlib.sha1(expanded_path.encode("utf-8")).hexdigest()[:12]
    return os.path.join("/tmp", "inspection_bringup", "navigation", f"{basename}-{path_hash}")


def load_config(path):
    expanded_path = os.path.expanduser(os.path.expandvars(path))
    if not expanded_path or not os.path.exists(expanded_path):
        return {}

    with open(expanded_path, "r", encoding="utf-8") as config_file:
        return yaml.safe_load(config_file) or {}


def active_map_from_index(index_path):
    """Return the active map directory and initial frame recorded by the map manager."""
    expanded_path = os.path.abspath(os.path.expanduser(os.path.expandvars(index_path)))
    try:
        with open(expanded_path, "r", encoding="utf-8") as index_file:
            index = json.load(index_file)
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"unable to read map index '{expanded_path}': {exc}") from exc

    if not isinstance(index, dict):
        raise RuntimeError(f"map index '{expanded_path}' must contain an object")

    active_maps = []
    for key, entry in index.items():
        if not isinstance(entry, dict) or not bool(entry.get("ifActivate")):
            continue
        code = str(entry.get("mapCode") or key).strip()
        init_frame = str(entry.get("initFrame") or "").strip()
        if code and init_frame:
            active_maps.append((code, init_frame))

    if len(active_maps) != 1:
        raise RuntimeError(
            f"map index '{expanded_path}' must contain exactly one active map with mapCode and initFrame"
        )

    code, init_frame = active_maps[0]
    return {
        "code": code,
        "directory": os.path.join(os.path.dirname(expanded_path), code),
        "init_frame": init_frame,
    }


def active_map_settings(config):
    if not as_bool(config_value(config, "maps", "sync_active_map", True)):
        return None
    index_path = config_value(
        config,
        "maps",
        "index_path",
        os.environ.get("INSPECTION_MAP_INDEX_PATH", "/home/cat/Workspace/Maps/index.json"),
    )
    return active_map_from_index(str(index_path))


def write_yaml_atomic(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp_path = f"{path}.tmp"
    with open(tmp_path, "w", encoding="utf-8") as output_file:
        yaml.safe_dump(data, output_file, sort_keys=False)
    os.replace(tmp_path, path)


def load_yaml_file(path):
    expanded_path = os.path.expanduser(os.path.expandvars(path))
    with open(expanded_path, "r", encoding="utf-8") as input_file:
        return yaml.safe_load(input_file) or {}


def readiness_failure_detail_path(state_dir, run_id, section):
    return os.path.join(state_dir, f"{run_id}_{section}_failure.yaml")


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


def override_or_config(context, name, config, section, key, fallback, use_launch_overrides=True):
    if not use_launch_overrides:
        return str(config_value(config, section, key, fallback))
    override = LaunchConfiguration(name).perform(context)
    if override != "":
        return override
    return str(config_value(config, section, key, fallback))


def override_or_config_bool(context, name, config, section, key, fallback, use_launch_overrides=True):
    if not use_launch_overrides:
        return as_bool(config_value(config, section, key, fallback))
    override = LaunchConfiguration(name).perform(context)
    if override != "":
        return as_bool(override)
    return as_bool(config_value(config, section, key, fallback))


def override_or_config_typed(
    context,
    name,
    config,
    section,
    key,
    fallback,
    value_type,
    use_launch_overrides=True,
):
    if not use_launch_overrides:
        return value_type(config_value(config, section, key, fallback))
    override = LaunchConfiguration(name).perform(context)
    value = override if override != "" else config_value(config, section, key, fallback)
    return value_type(value)


def split_list_text(value):
    if isinstance(value, list):
        return [str(item) for item in value]
    return [item.strip() for item in str(value).split(",") if item.strip()]


def override_or_config_list(context, name, config, section, key, fallback, use_launch_overrides=True):
    if not use_launch_overrides:
        return config_list(config, section, key, fallback)
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


def wait_for_nodes_action(name, nodes, timeout, failure_detail=None):
    return ExecuteProcess(
        cmd=[
            "python3",
            readiness_script_path(),
            "nodes",
            "--name",
            name,
            "--timeout",
            str(timeout),
            *( ["--failure-detail", failure_detail] if failure_detail else [] ),
            *nodes,
        ],
        name=f"wait_for_{name}_nodes",
        output="screen",
    )


def wait_for_health_action(name, topic, timeout, failure_detail=None):
    return ExecuteProcess(
        cmd=[
            "python3",
            readiness_script_path(),
            "health",
            "--name",
            name,
            "--topic",
            topic,
            "--timeout",
            str(timeout),
            *( ["--failure-detail", failure_detail] if failure_detail else [] ),
        ],
        name=f"wait_for_{name}_health",
        output="screen",
    )


def nav_bridge_ready_action(topics, stand_service, topic_timeout, stand_timeout, failure_detail=None):
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
            *( ["--failure-detail", failure_detail] if failure_detail else [] ),
            *topic_args,
        ],
        name="wait_for_nav_bridge_ready",
        output="screen",
    )


def localization_init_ready_action(
    status_topic,
    timeout,
    blocked_is_failure,
    release_control_on_blocked,
    release_control_service,
    release_control_timeout,
    failure_detail=None,
):
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
    if release_control_on_blocked:
        cmd.extend(
            [
                "--release-control-on-blocked",
                "--release-control-service",
                release_control_service,
                "--release-control-timeout",
                str(release_control_timeout),
            ]
        )
    if failure_detail:
        cmd.extend(["--failure-detail", failure_detail])

    return ExecuteProcess(
        cmd=cmd,
        name="wait_for_slam_localization_init",
        output="screen",
    )


def module_readiness_action(context, config, section, fallback_nodes, fallback_timeout, failure_detail=None):
    return module_readiness_action_with_overrides(
        context,
        config,
        section,
        fallback_nodes,
        fallback_timeout,
        True,
        failure_detail,
    )


def module_readiness_action_with_overrides(
    context,
    config,
    section,
    fallback_nodes,
    fallback_timeout,
    use_launch_overrides,
    failure_detail=None,
):
    ready_type = str(readiness_value(config, section, "type", "nodes"))

    if ready_type == "nodes":
        return wait_for_nodes_action(
            section,
            readiness_list(config, section, "nodes", fallback_nodes),
            float(readiness_value(config, section, "timeout_seconds", fallback_timeout)),
            failure_detail,
        )

    if ready_type == "topics":
        cmd = [
                "python3",
                readiness_script_path(),
                "topics",
                "--name",
                section,
                "--timeout",
                str(float(readiness_value(config, section, "timeout_seconds", fallback_timeout))),
            ]
        if failure_detail:
            cmd.extend(["--failure-detail", failure_detail])
        cmd.extend(readiness_list(config, section, "topics", []))
        return ExecuteProcess(
            cmd=cmd,
            name=f"wait_for_{section}_topics",
            output="screen",
        )

    if ready_type == "health":
        return wait_for_health_action(
            section,
            str(readiness_value(config, section, "topic", f"/{section}/health")),
            float(readiness_value(config, section, "timeout_seconds", fallback_timeout)),
            failure_detail,
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
                use_launch_overrides,
            ),
            override_or_config(
                context,
                "nav_bridge_stand_service",
                {"nav_bridge": readiness_config(config, "nav_bridge")},
                "nav_bridge",
                "stand_service",
                "/nav_bridge_node/stand",
                use_launch_overrides,
            ),
            override_or_config_typed(
                context,
                "nav_bridge_topic_timeout_seconds",
                {"nav_bridge": readiness_config(config, "nav_bridge")},
                "nav_bridge",
                "topic_timeout_seconds",
                fallback_timeout,
                float,
                use_launch_overrides,
            ),
            override_or_config_typed(
                context,
                "nav_bridge_stand_timeout_seconds",
                {"nav_bridge": readiness_config(config, "nav_bridge")},
                "nav_bridge",
                "stand_timeout_seconds",
                fallback_timeout,
                float,
                use_launch_overrides,
            ),
            failure_detail,
        )

    if ready_type == "localization_init":
        return localization_init_ready_action(
            str(readiness_value(config, section, "status_topic", "/localization_init_status")),
            float(readiness_value(config, section, "timeout_seconds", 120.0)),
            as_bool(readiness_value(config, section, "blocked_is_failure", False)),
            as_bool(readiness_value(config, section, "release_control_on_blocked", False)),
            str(readiness_value(config, section, "release_control_service", "/nav_bridge_node/release_control")),
            float(readiness_value(config, section, "release_control_timeout_seconds", 5.0)),
            failure_detail,
        )

    print(f"[navigation] unknown readiness type for {section}: {ready_type}; falling back to nodes")
    return wait_for_nodes_action(section, fallback_nodes, fallback_timeout, failure_detail)


def module_readiness_type(config, section):
    return str(readiness_value(config, section, "type", "nodes"))


def read_failure_detail(path):
    if not path or not os.path.exists(path):
        return {}
    try:
        return load_yaml_file(path)
    except Exception as exc:
        return {"reason": f"failed to read failure detail: {exc}"}


def failure_reason_with_detail(base_reason, failure_detail):
    detail = read_failure_detail(failure_detail)
    detail_reason = str(detail.get("reason", "")).strip()
    if detail_reason:
        return f"{base_reason}; detail={detail_reason}"
    return base_reason


def module_readiness_failure_reason(config, section, fallback_nodes, fallback_timeout, failure_detail=None):
    ready_type = module_readiness_type(config, section)

    if ready_type == "nav_bridge":
        topics = readiness_list(config, section, "topics", ["/battery/level"])
        topic_timeout = float(readiness_value(config, section, "topic_timeout_seconds", fallback_timeout))
        stand_service = str(readiness_value(config, section, "stand_service", "/nav_bridge_node/stand"))
        stand_timeout = float(readiness_value(config, section, "stand_timeout_seconds", fallback_timeout))
        base_reason = (
            f"{section} readiness failed: no message on topics {topics} within "
            f"{topic_timeout:.1f}s or {stand_service} did not return success within {stand_timeout:.1f}s"
        )
        return base_reason

    if ready_type == "localization_init":
        status_topic = str(readiness_value(config, section, "status_topic", "/localization_init_status"))
        timeout = float(readiness_value(config, section, "timeout_seconds", 120.0))
        blocked_is_failure = as_bool(readiness_value(config, section, "blocked_is_failure", False))
        blocked_text = "entered INITIAL_REGISTRATION_BLOCKED or " if blocked_is_failure else ""
        base_reason = (
            f"{section} readiness failed: localization init {blocked_text}"
            f"did not reach TRACKING on {status_topic} {timeout_text_for_reason(timeout)}"
        )
        return base_reason

    if ready_type == "health":
        topic = str(readiness_value(config, section, "topic", f"/{section}/health"))
        timeout = float(readiness_value(config, section, "timeout_seconds", fallback_timeout))
        return f"{section} readiness failed: health topic {topic} did not report OK {timeout_text_for_reason(timeout)}"

    if ready_type == "topics":
        topics = readiness_list(config, section, "topics", [])
        timeout = float(readiness_value(config, section, "timeout_seconds", fallback_timeout))
        base_reason = f"{section} readiness failed: no message on topics {topics} {timeout_text_for_reason(timeout)}"
        return base_reason

    nodes = readiness_list(config, section, "nodes", fallback_nodes)
    timeout = float(readiness_value(config, section, "timeout_seconds", fallback_timeout))
    base_reason = f"{section} readiness failed: nodes {nodes} not ready {timeout_text_for_reason(timeout)}"
    return base_reason


def timeout_text_for_reason(timeout):
    if timeout <= 0.0:
        return "before shutdown"
    return f"within {timeout:.1f}s"


def append_navigation_group(
    actions,
    previous_wait,
    previous_failure_reason,
    previous_failure_detail,
    launch_action,
    wait_action,
    delay_seconds,
    shutdown_on_readiness_failure,
    state_dir=None,
    run_id=None,
):
    if previous_wait is None:
        actions.extend([launch_action, wait_action])
        return wait_action

    next_actions = [launch_action, wait_action]
    if delay_seconds > 0.0:
        next_actions = [TimerAction(period=delay_seconds, actions=next_actions)]

    def on_previous_wait_exit(event, _context):
        if event.returncode != 0:
            reason = failure_reason_with_detail(previous_failure_reason, previous_failure_detail)
            reason = f"{reason}; exit_code={event.returncode}; not starting the next module"
            print(
                f"[navigation] {reason}"
            )
            return report_result_actions(
                state_dir,
                run_id,
                False,
                reason,
                shutdown_after=(state_dir is not None or shutdown_on_readiness_failure),
            )
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


def report_result_actions(state_dir, run_id, success, reason, shutdown_after=False):
    if not state_dir:
        if shutdown_after:
            return [EmitEvent(event=Shutdown(reason=reason))]
        return []

    def write_result(_context):
        result_path = os.path.join(state_dir, "result.yaml")
        write_yaml_atomic(
            result_path,
            {
                "run_id": str(run_id or ""),
                "success": bool(success),
                "reason": reason,
                "reported_at": time.time(),
            },
        )
        print(f"[navigation] reported result: success={bool(success)} reason={reason}")
        if shutdown_after:
            return [TimerAction(period=0.5, actions=[EmitEvent(event=Shutdown(reason=reason))])]
        return []

    return [OpaqueFunction(function=write_result)]


def append_final_result_handler(
    actions,
    previous_wait,
    previous_failure_reason,
    previous_failure_detail,
    state_dir,
    run_id,
    shutdown_on_readiness_failure,
):
    if previous_wait is None:
        actions.extend(report_result_actions(state_dir, run_id, True, "navigation startup complete"))
        return

    def on_final_wait_exit(event, _context):
        if event.returncode != 0:
            reason = failure_reason_with_detail(previous_failure_reason, previous_failure_detail)
            reason = f"{reason}; exit_code={event.returncode}"
            print(f"[navigation] {reason}")
            return report_result_actions(
                state_dir,
                run_id,
                False,
                reason,
                shutdown_after=(state_dir is not None or shutdown_on_readiness_failure),
            )
        return report_result_actions(state_dir, run_id, True, "navigation startup complete")

    actions.append(
        RegisterEventHandler(
            OnProcessExit(
                target_action=previous_wait,
                on_exit=on_final_wait_exit,
            )
        )
    )


def generate_launch_description():
    declared_arguments = [
        DeclareLaunchArgument(
            "navigate_config_path",
            default_value=default_navigate_config_path(),
            description="Navigation configuration YAML path.",
        ),
        DeclareLaunchArgument(
            "navigation_state_dir",
            default_value="",
            description="Internal per-request directory used to report startup results.",
        ),
        DeclareLaunchArgument(
            "navigation_run_id",
            default_value="",
            description="Internal per-request identifier used to report startup results.",
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
        DeclareLaunchArgument(
            "livox_model",
            default_value="",
            description="Livox model passed to msg_multi_MID360_launch.py: mid360 or mid360s.",
        ),
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
            "global_multi_map_dir",
            default_value="",
            description="Path to gridmapper data/Output/multi_maps directory.",
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
    navigate_config_path = LaunchConfiguration("navigate_config_path").perform(context)
    config = load_config(navigate_config_path)
    start_mode = str(config_value(config, "bringup", "start_mode", "immediate")).strip().lower()
    if start_mode == "service":
        state_dir = navigation_state_dir(navigate_config_path)
        supervisor = ExecuteProcess(
            cmd=[
                "python3",
                supervisor_script_path(),
                "node",
                "--config",
                navigate_config_path,
                "--service-name",
                str(config_value(config, "bringup", "start_service", "/navigation_bringup/start")),
                "--state-dir",
                state_dir,
                "--result-timeout",
                str(float(config_value(config, "bringup", "result_timeout_seconds", 0.0))),
            ],
            name="navigation_supervisor",
            output="screen",
        )
        return [supervisor]

    state_dir = LaunchConfiguration("navigation_state_dir").perform(context).strip()
    run_id = LaunchConfiguration("navigation_run_id").perform(context).strip()
    if bool(state_dir) != bool(run_id):
        raise RuntimeError("navigation_state_dir and navigation_run_id must be provided together")
    return build_navigation_actions(
        context,
        config,
        state_dir=state_dir or None,
        run_id=run_id or None,
    )




def load_failure_reason(state_dir, run_id, section):
    if not state_dir or not run_id:
        return ""
    path = readiness_failure_detail_path(state_dir, run_id, section)
    if not os.path.exists(path):
        return ""
    try:
        detail = load_yaml_file(path)
    except Exception as exc:
        return f"failed to read readiness detail: {exc}"
    reason = str(detail.get("reason", "")).strip()
    return reason


def build_navigation_actions(context, config, state_dir=None, run_id=None, use_launch_overrides=True):
    active_map = active_map_settings(config)
    if active_map:
        print(
            "[navigation] using active map from index: "
            f"code={active_map['code']} directory={active_map['directory']} "
            f"initFrame={active_map['init_frame']}"
        )

    slam_prior_dir = (
        active_map["directory"]
        if active_map
        else override_or_config(
            context, "slam_prior_dir", config, "slam", "prior_dir", "", use_launch_overrides
        )
    )
    global_initial_map = (
        active_map["init_frame"]
        if active_map
        else override_or_config(
            context, "global_initial_map", config, "global_planner", "initial_map", "map_000", use_launch_overrides
        )
    )
    global_multi_map_dir = (
        active_map["directory"]
        if active_map
        else override_or_config(
            context,
            "global_multi_map_dir",
            config,
            "global_planner",
            "multi_map_dir",
            slam_prior_dir,
            use_launch_overrides,
        )
    )

    delay = override_or_config_typed(
        context,
        "navigation_start_delay_seconds",
        config,
        "bringup",
        "start_delay_seconds",
        1.0,
        float,
        use_launch_overrides,
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
        context, "enable_nav_bridge", config, "modules", "nav_bridge", True, use_launch_overrides
    )

    livox_launch = include_package_launch(
        "livox_ros_driver2",
        "msg_multi_MID360_launch.py",
        "true",
        {
            "model": override_or_config(
                context, "livox_model", config, "livox", "model", "mid360", use_launch_overrides
            ),
        },
    )
    livox_enabled = override_or_config_bool(
        context, "enable_livox", config, "modules", "livox", True, use_launch_overrides
    )
    slam_launch = include_package_launch(
        "faster_lio",
        "slam.launch.py",
        "true",
        {
            "relocal": as_bool_text(
                override_or_config_bool(
                    context, "slam_relocal", config, "slam", "relocal", True, use_launch_overrides
                )
            ),
            "prior_dir": slam_prior_dir,
            "rviz": as_bool_text(
                override_or_config_bool(
                    context, "slam_rviz", config, "slam", "rviz", False, use_launch_overrides
                )
            ),
            "pgo": as_bool_text(
                override_or_config_bool(
                    context, "slam_pgo", config, "slam", "pgo", False, use_launch_overrides
                )
            ),
            "odom_imu": as_bool_text(
                override_or_config_bool(
                    context, "slam_odom_imu", config, "slam", "odom_imu", True, use_launch_overrides
                )
            ),
            "use_sim_time": as_bool_text(
                override_or_config_bool(
                    context, "slam_use_sim_time", config, "slam", "use_sim_time", False, use_launch_overrides
                )
            ),
        },
    )
    slam_enabled = override_or_config_bool(
        context, "enable_slam", config, "modules", "slam", True, use_launch_overrides
    )
    terrain_launch = include_package_launch(
        "gridmapper",
        "local.launch.py",
        "true",
        {
            "rviz": as_bool_text(
                override_or_config_bool(
                    context, "terrain_rviz", config, "terrain", "rviz", False, use_launch_overrides
                )
            ),
            "use_gpu": as_bool_text(
                override_or_config_bool(
                    context, "terrain_use_gpu", config, "terrain", "use_gpu", True, use_launch_overrides
                )
            ),
        },
    )
    terrain_enabled = override_or_config_bool(
        context, "enable_terrain", config, "modules", "terrain", True, use_launch_overrides
    )
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
                    use_launch_overrides,
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
                    use_launch_overrides,
                )
            ),
        },
    )
    local_planner_enabled = override_or_config_bool(
        context, "enable_local_planner", config, "modules", "local_planner", True, use_launch_overrides
    )
    global_planner_launch = include_package_launch(
        "multi_map_nav",
        "multi_map_nav.launch.py",
        "true",
        {
            "multi_map_dir": os.path.expanduser(os.path.expandvars(global_multi_map_dir)),
            "initial_map": global_initial_map,
            "use_fake_cmdvel": as_bool_text(
                override_or_config_bool(
                    context,
                    "global_use_fake_cmdvel",
                    config,
                    "global_planner",
                    "use_fake_cmdvel",
                    True,
                    use_launch_overrides,
                )
            ),
            "params_file": override_or_config(
                context,
                "global_params_file",
                config,
                "global_planner",
                "params_file",
                "new_local",
                use_launch_overrides,
            ),
            "patrol_loops": str(
                override_or_config_typed(
                    context,
                    "global_patrol_loops",
                    config,
                    "global_planner",
                    "patrol_loops",
                    1,
                    int,
                    use_launch_overrides,
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
                    use_launch_overrides,
                )
            ),
        },
    )
    global_planner_enabled = override_or_config_bool(
        context, "enable_global_planner", config, "modules", "global_planner", True, use_launch_overrides
    )

    def detail_path(section):
        if state_dir and run_id:
            return readiness_failure_detail_path(state_dir, run_id, section)
        return None

    navigation_groups = {
        "nav_bridge": (
            nav_bridge_enabled,
            nav_bridge_launch,
            module_readiness_action_with_overrides(
                context, config, "nav_bridge", [], wait_timeout, use_launch_overrides, detail_path("nav_bridge")
            ),
            module_readiness_failure_reason(config, "nav_bridge", [], wait_timeout, detail_path("nav_bridge")),
            detail_path("nav_bridge"),
        ),
        "livox": (
            livox_enabled,
            livox_launch,
            module_readiness_action_with_overrides(
                context,
                config,
                "livox",
                ["/livox_lidar_publisher"],
                wait_timeout,
                use_launch_overrides,
                detail_path("livox"),
            ),
            module_readiness_failure_reason(config, "livox", ["/livox_lidar_publisher"], wait_timeout, detail_path("livox")),
            detail_path("livox"),
        ),
        "slam": (
            slam_enabled,
            slam_launch,
            module_readiness_action_with_overrides(
                context, config, "slam", ["/laser_mapping"], wait_timeout, use_launch_overrides, detail_path("slam")
            ),
            module_readiness_failure_reason(config, "slam", ["/laser_mapping"], wait_timeout, detail_path("slam")),
            detail_path("slam"),
        ),
        "terrain": (
            terrain_enabled,
            terrain_launch,
            module_readiness_action_with_overrides(
                context,
                config,
                "terrain",
                ["/gridmapper_node"],
                wait_timeout,
                use_launch_overrides,
                detail_path("terrain"),
            ),
            module_readiness_failure_reason(config, "terrain", ["/gridmapper_node"], wait_timeout, detail_path("terrain")),
            detail_path("terrain"),
        ),
        "local_planner": (
            local_planner_enabled,
            local_planner_launch,
            module_readiness_action_with_overrides(
                context,
                config,
                "local_planner",
                ["/localPlanner", "/pathFollower"],
                wait_timeout,
                use_launch_overrides,
                detail_path("local_planner"),
            ),
            module_readiness_failure_reason(
                config,
                "local_planner",
                ["/localPlanner", "/pathFollower"],
                wait_timeout,
                detail_path("local_planner"),
            ),
            detail_path("local_planner"),
        ),
        "global_planner": (
            global_planner_enabled,
            global_planner_launch,
            module_readiness_action_with_overrides(
                context,
                config,
                "global_planner",
                ["/multi_map_nav_node", "/planner_server", "/controller_server"],
                wait_timeout,
                use_launch_overrides,
                detail_path("global_planner"),
            ),
            module_readiness_failure_reason(
                config,
                "global_planner",
                ["/multi_map_nav_node", "/planner_server", "/controller_server"],
                wait_timeout,
                detail_path("global_planner"),
            ),
            detail_path("global_planner"),
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

        enabled, launch_action, ready_action, failure_reason, failure_detail = group
        ordered_navigation_groups.append((enabled, launch_action, name, ready_action, failure_reason, failure_detail))

    if not should_wait_for_readiness:
        actions = []
        launch_index = 0
        for enabled, launch_action, name, ready_action, _failure_reason, _failure_detail in ordered_navigation_groups:
            if enabled:
                start_delay = delay * launch_index
                if name == "nav_bridge" and module_readiness_type(config, name) == "nav_bridge":
                    actions.extend(delayed_actions(start_delay, [launch_action, ready_action]))
                else:
                    actions.append(delayed_include(start_delay, launch_action))
                launch_index += 1
        if state_dir:
            actions.extend(
                delayed_actions(
                    delay * launch_index,
                    report_result_actions(
                        state_dir,
                        run_id,
                        True,
                        "navigation startup launched without readiness waiting",
                    ),
                )
            )
        return actions

    actions = []
    previous_wait = None
    previous_failure_reason = None
    previous_failure_detail = None
    for enabled, launch_action, _name, ready_action, failure_reason, failure_detail in ordered_navigation_groups:
        if not enabled:
            continue
        previous_wait = append_navigation_group(
            actions,
            previous_wait,
            previous_failure_reason,
            previous_failure_detail,
            launch_action,
            ready_action,
            delay,
            shutdown_on_readiness_failure,
            state_dir=state_dir,
            run_id=run_id,
        )
        previous_failure_reason = failure_reason
        previous_failure_detail = failure_detail

    if state_dir:
        append_final_result_handler(
            actions,
            previous_wait,
            previous_failure_reason,
            previous_failure_detail,
            state_dir,
            run_id,
            shutdown_on_readiness_failure,
        )

    return actions
