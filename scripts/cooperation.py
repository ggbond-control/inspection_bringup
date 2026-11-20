"""One configuration-only selector for mutually exclusive cooperation stacks."""
from device_description import device_status, device_status_snapshot

MODES = {'disabled', 'coordination', 'swarm'}
REMOVED = {'mission_execution_agent', 'fleet_leader_election'}


def validate(config):
    obsolete = REMOVED & (set(config) | set(config.get('modules', {})))
    if obsolete:
        raise ValueError('Removed cooperation configuration: ' + ', '.join(sorted(obsolete))
                         + '; use cooperation.mode and cooperation.coordination')
    settings = config.get('cooperation', {})
    if not isinstance(settings, dict):
        raise ValueError('cooperation must be a mapping')
    mode = settings.get('mode', 'disabled')
    if not isinstance(mode, str) or mode not in MODES:
        raise ValueError('cooperation.mode must be disabled, coordination or swarm')
    if 'config_path' in settings:
        raise ValueError('cooperation.config_path was removed; configure swarm inline')
    if 'device_status_path' in settings:
        raise ValueError('cooperation.device_status_path was removed; configure system.device_status in robot.yaml')
    if mode != 'disabled' and not settings.get('robot_id'):
        raise ValueError('cooperation.robot_id is required; set robot.yaml robot_id')
    for key in ('coordination', 'swarm'):
        if not isinstance(settings.get(key, {}), dict):
            raise ValueError(f'cooperation.{key} must be a mapping')
    for stack, sections in [('coordination', ('agent', 'election')),
                            ('swarm', ('agent', 'consensus', 'coordinator', 'planner', 'transport'))]:
        for section in sections:
            if not isinstance(settings.get(stack, {}).get(section, {}), dict):
                raise ValueError(f'cooperation.{stack}.{section} must be a mapping')
    agent = settings.get('swarm', {}).get('agent', {})
    if 'allow_motion' in agent:
        raise ValueError('allow_motion was removed; cooperation.mode selects the control stack')
    return mode


def swarm_configuration(config):
    """Project merged Bringup settings into node parameters, without a second YAML."""
    # Optional dependency: single-device and coordination modes do not import swarm.
    from swarm_protocol.topics import (
        DEFAULT_CLUSTER_ID, DEFAULT_PEER_PREFIX, DEFAULT_PLATFORM_PREFIX,
    )

    validate(config)
    settings = config['cooperation']
    swarm = settings.get('swarm', {})
    mqtt = config.get('mqtt', {})
    robot = settings.get('robot_id', '')
    if not isinstance(robot, str) or not robot:
        raise ValueError('robot_id is required for swarm')
    status = device_status(config)
    capabilities = status['capabilities']
    speed = status['nominal_speed_mps']
    result = {
        'robot_id': robot,
        'cluster_id': swarm.get('cluster_id', DEFAULT_CLUSTER_ID),
        'data_root': swarm.get('data_root', '~/.ros/swarm'),
        'transport': {
            'mqtt_host': mqtt.get('host', '127.0.0.1'),
            'mqtt_port': mqtt.get('port', 1883),
            'mqtt_username': mqtt.get('username', ''),
            'mqtt_password': mqtt.get('password', ''),
            'mqtt_tls': mqtt.get('tls', False),
            'mqtt_ca': mqtt.get('ca', ''),
            'mqtt_prefix': swarm.get('mqtt_prefix', DEFAULT_PEER_PREFIX),
            'platform_prefix': swarm.get('platform_prefix', DEFAULT_PLATFORM_PREFIX),
        },
        'agent': {
            **swarm.get('agent', {}),
            'capabilities': sorted(set(capabilities) | {'device'}),
            'nominal_speed': float(speed),
        },
        'consensus': swarm.get('consensus', {}),
        'coordinator': dict(swarm.get('coordinator', {})),
        'planner': dict(swarm.get('planner', {})),
    }
    if 'data_root' in swarm.get('transport', {}):
        result['transport']['data_root'] = swarm['transport']['data_root']
    # These affect the map fingerprint and must never differ between preflight/planning.
    for key, default in [('footprint_radius', 0.3), ('free_thresh', 0.05)]:
        if any(key in result[part] for part in ('coordinator', 'planner')):
            raise ValueError(f'configure swarm.{key} once, outside coordinator/planner')
        for part in ('coordinator', 'planner'):
            result[part][key] = swarm.get(key, default)
    return result


def _value(config, section, key, fallback):
    value = config.get(section, {}).get(key, fallback)
    return fallback if value is None or value == "" else value


def launch_actions(config):
    """Build only the configured cooperation stack, shared by both launch entries."""
    # Configuration CLI helpers also import this module without a ROS environment.
    from ament_index_python.packages import get_package_share_directory
    from launch.actions import LogInfo
    from launch_ros.actions import Node

    cooperation_mode = validate(config)
    coordination = config.get("cooperation", {}).get("coordination", {})
    actions = [LogInfo(msg=(
        f"Cooperation mode={cooperation_mode}; "
        f"robot={config.get('cooperation', {}).get('robot_id', 'unset')}"
    ))]
    required = {
        'coordination': ('fleet_leader_election', 'capability_mission_planner', 'mission_execution_agent'),
        'swarm': ('swarm_protocol', 'swarm_consensus', 'swarm_mission_coordinator',
                  'swarm_mqtt_transport', 'swarm_mission_planner', 'swarm_execution_agent'),
    }.get(cooperation_mode, ())
    for package in required:
        try:
            get_package_share_directory(package)
        except LookupError as exc:
            raise RuntimeError(f'Cooperation package {package} is unavailable; '
                               'build and source cooperation_ws first') from exc
    if cooperation_mode == "coordination":
        actions.append(Node(
            package="fleet_leader_election",
            executable="fleet_leader_election_node",
            name="fleet_leader_election",
            output="screen",
            parameters=[{
                "robot_id": str(config["cooperation"]["robot_id"]),
                "cluster_id": str(_value(coordination, "election", "cluster_id", "inspection-fleet")),
                "data_root": str(_value(coordination, "election", "data_root", "~/fleet_leader_election/runtime")),
                "heartbeat_interval_seconds": float(_value(coordination, "election", "heartbeat_interval_seconds", 0.25)),
                "election_timeout_seconds": float(_value(coordination, "election", "election_timeout_seconds", 1.0)),
                "election_timeout_step_seconds": float(_value(coordination, "election", "election_timeout_step_seconds", 0.15)),
                "quorum_timeout_seconds": float(_value(coordination, "election", "quorum_timeout_seconds", 0.75)),
                "recovery_observation_seconds": float(_value(coordination, "election", "recovery_observation_seconds", 0.5)),
            }],
        ))
        actions.append(Node(
            package="capability_mission_planner",
            executable="capability_mission_planner_node",
            name="capability_mission_planner",
            output="screen",
        ))
        actions.append(Node(
            package="mission_execution_agent",
            executable="mission_execution_agent_node",
            name="mission_execution_agent",
            output="screen",
            parameters=[{
                "robot_id": str(config["cooperation"]["robot_id"]),
                "device_status_path": device_status_snapshot(config),
                "heartbeat_topic": str(_value(
                    coordination, "agent", "heartbeat_topic", "/mission_execution/heartbeat"
                )),
                "odometry_topic": str(_value(
                    coordination, "agent", "odometry_topic", "/odometry_multi_maps"
                )),
                "task_hub_status_service": str(_value(
                    coordination, "agent", "task_hub_status_service", "/get_status"
                )),
                "planning_leader": bool(_value(coordination, "agent", "planning_leader", False)),
                "leader_epoch": int(_value(coordination, "agent", "leader_epoch", 0)),
                "heartbeat_period_seconds": float(_value(
                    coordination, "agent", "heartbeat_period_seconds", 1.0
                )),
                "odometry_max_age_seconds": float(_value(
                    coordination, "agent", "odometry_max_age_seconds", 3.0
                )),
                "map_root_directory": str(_value(
                    config, "mqtt", "map_root_directory", "/home/cat/Workspace/Maps"
                )),
                "auto_execute": bool(_value(
                    coordination, "agent", "auto_execute", False
                )),
                "task_hub_start_service": str(_value(
                    coordination, "agent", "task_hub_start_service", "/start_route"
                )),
                "task_hub_pause_service": str(_value(
                    coordination, "agent", "task_hub_pause_service", "/pause_route"
                )),
                "task_hub_cancel_service": str(_value(
                    coordination, "agent", "task_hub_cancel_service", "/cancel_route"
                )),
                "task_event_topic": str(_value(
                    coordination, "agent", "task_event_topic",
                    "/mission_execution/task_event"
                )),
                "task_event_ack_topic": str(_value(
                    coordination, "agent", "task_event_ack_topic",
                    "/mission_execution/task_event_ack"
                )),
                "mission_control_topic": str(_value(
                    coordination, "agent", "mission_control_topic",
                    "/mission_execution/mission_control"
                )),
                "mission_control_ack_topic": str(_value(
                    coordination, "agent", "mission_control_ack_topic",
                    "/mission_execution/mission_control_ack"
                )),
                "task_addition_topic": str(_value(
                    coordination, "agent", "task_addition_topic",
                    "/mission_execution/task_addition"
                )),
                "task_addition_status_topic": str(_value(
                    coordination, "agent", "task_addition_status_topic",
                    "/mission_execution/task_addition_status"
                )),
                "task_addition_retry_seconds": float(_value(
                    coordination, "agent", "task_addition_retry_seconds", 2.0
                )),
                "task_addition_response_timeout_seconds": float(_value(
                    coordination, "agent", "task_addition_response_timeout_seconds", 60.0
                )),
                "task_event_resend_seconds": float(_value(
                    coordination, "agent", "task_event_resend_seconds", 2.0
                )),
                "data_root": str(_value(
                    coordination,
                    "agent",
                    "data_root",
                    "~/.ros/mission_execution_agent",
                )),
            }],
        ))
    if cooperation_mode == "swarm":
        # Import only for this mode; coordination/disabled do not require swarm packages.
        from swarm_mission_coordinator.launching import device_nodes
        actions.extend(device_nodes(swarm_configuration(config)))
    return actions
