# Navigation Bringup

This document describes the standalone navigation bringup in
`inspection_bringup`.

The navigation stack is intentionally decoupled from
`inspection_system.launch.py`. Use `inspection_system.launch.py` for the
inspection task modules, and use `navigation.launch.py` for the robot
navigation stack.

## Start

Build and source the workspace first:

```bash
cd ~/Workspace/algor_ws
colcon build --packages-select inspection_bringup --symlink-install
source install/setup.zsh
```

Start the default navigation stack:

```bash
ros2 launch inspection_bringup navigation.launch.py
```

By default, `navigation.launch.py` starts a service-gated supervisor and waits
for:

```text
/navigation_bringup/start
rcl_interfaces/srv/SetParameters
```

Call the service to apply runtime overrides and start the stack:

```bash
ros2 service call /navigation_bringup/start rcl_interfaces/srv/SetParameters \
"{parameters: [
  {name: 'livox.model', value: {type: 4, string_value: 'mid360'}},
  {name: 'slam.prior_dir', value: {type: 4, string_value: 'company2'}},
  {name: 'global_planner.initial_map', value: {type: 4, string_value: 'company2'}}
]}"
```

Use an empty parameter list to start with `config/navigate.yaml` unchanged:

```bash
ros2 service call /navigation_bringup/start rcl_interfaces/srv/SetParameters \
"{parameters: []}"
```

The service returns after the configured startup sequence and readiness checks
finish. If a module readiness check fails, the service result contains the
failure reason.

The default configuration is:

```bash
config/navigate.yaml
```

Use another configuration file when needed:

```bash
ros2 launch inspection_bringup navigation.launch.py \
  navigate_config_path:=/path/to/navigate.yaml
```

## Default Stack

The default `config/navigate.yaml` matches the old `navigate.sh` mode
`x30-company2`.

The launch sequence is:

1. `nav_bridge/nav_bridge.launch.py`
2. `livox_ros_driver2/msg_multi_MID360_launch.py`
3. `faster_lio/slam.launch.py`
4. `gridmapper/local.launch.py`
5. `local_planner/local_planner.launch.py`
6. `multi_map_nav/multi_map_nav.launch.py`

Default map and prior settings:

```yaml
slam:
  prior_dir: "company2"

global_planner:
  initial_map: "company2"
  map_connections_file: "default"
  params_file: "new_local"
  use_fake_cmdvel: true
  patrol_loops: 1
```

## Configuration Layout

`config/navigate.yaml` is split by module:

```yaml
modules:
  nav_bridge: true
  livox: true
  slam: true
  terrain: true
  local_planner: true
  global_planner: true

bringup:
  start_mode: service
  start_service: /navigation_bringup/start
  start_timeout_seconds: 0.0
  result_timeout_seconds: 0.0
  sequence:
    - nav_bridge
    - livox
    - slam
    - terrain
    - local_planner
    - global_planner
  start_delay_seconds: 1.0
  wait_for_readiness: true
  shutdown_on_readiness_failure: true
  wait_timeout_seconds: 10.0

nav_bridge:
  readiness:
    type: nav_bridge
    topics:
      - /battery/level
    stand_service: /nav_bridge_node/stand
    topic_timeout_seconds: 10.0
    stand_timeout_seconds: 30.0

livox:
  model: mid360
  readiness:
    type: nodes
    nodes:
      - /livox_lidar_publisher

slam:
  relocal: true
  prior_dir: "company2"
  readiness:
    type: localization_init
    status_topic: /localization_init_status
    timeout_seconds: 0.0
    blocked_is_failure: false
    release_control_on_blocked: true
    release_control_service: /nav_bridge_node/release_control
    release_control_timeout_seconds: 5.0
```

`modules` controls whether each module is launched. `bringup.sequence` controls
the launch order. `bringup` also controls the launch timing and readiness wait
behavior. Each module section contains only that module's launch arguments and
readiness checks.

## Readiness Wait

When `bringup.start_mode` is `service`, `navigation.launch.py` first starts
`scripts/navigation_supervisor.py` and waits until the supervisor accepts
`bringup.start_service`. The supervisor applies matching `SetParameters`
overrides to `config/navigate.yaml` and writes a resolved runtime YAML. The
launch file then reads that resolved YAML and starts the configured sequence.

The supervisor does not launch navigation modules itself; it only gates startup
and waits for the final result reported by `navigation.launch.py`.

`bringup.start_timeout_seconds` controls how long launch waits for a start
request. `bringup.result_timeout_seconds` controls how long the service waits
for the final launch result. Values `<= 0` mean wait without a timeout.

When `bringup.wait_for_readiness` is true, each module starts, waits for that
module's configured `readiness`, then starts the next module after
`bringup.start_delay_seconds`.

Set `bringup.start_mode: immediate` to use the older behavior where launch
starts the sequence immediately without waiting for the service.

The module order comes from `bringup.sequence`:

```yaml
bringup:
  sequence:
    - nav_bridge
    - livox
    - slam
    - terrain
    - local_planner
    - global_planner
```

Unknown or duplicate sequence entries are skipped with a console message.

`nav_bridge` uses a custom readiness check before the rest of the stack starts:

1. Wait for one message on every topic in `nav_bridge.readiness.topics`.
2. Call `nav_bridge.readiness.stand_service` as `std_srvs/srv/Trigger`.
3. Continue only when the service response contains `success: true`.

When `bringup.wait_for_readiness` is false, `nav_bridge` still runs this
activation step after its launch. The step no longer gates the later modules in
that mode, but the required `stand_service` call is not skipped.

`slam` uses the faster_lio localization status interface:

1. Subscribe to `/localization_init_status` with transient local + reliable QoS.
2. Continue only when `state == TRACKING`.
3. If `state == INITIAL_REGISTRATION_BLOCKED`, wait for an external supervisor
   or UI to restart initial alignment. This bringup launch does not directly
   call `/restart_initial_alignment`.
4. If `release_control_on_blocked` is true, call
   `/nav_bridge_node/release_control` once when entering blocked state. This
   explicitly stops the nav_bridge heartbeat and releases control, but does not
   change the external restart policy.

Set `slam.readiness.timeout_seconds` to `0.0` or a negative value to wait
without a timeout. This is the default because blocked relocalization may need
manual or external-service intervention before faster_lio can return to
`TRACKING`.

The readiness helper is:

```bash
scripts/wait_for_ready.py
```

It has subcommands for the supported readiness checks:

```bash
python3 scripts/wait_for_ready.py nodes --name livox --timeout 10.0 /livox_lidar_publisher
python3 scripts/wait_for_ready.py topics --name battery --timeout 10.0 /battery/level
python3 scripts/wait_for_ready.py nav_bridge --topic /battery/level --stand-service /nav_bridge_node/stand
python3 scripts/wait_for_ready.py localization-init --status-topic /localization_init_status --timeout 0.0 --release-control-on-blocked
```

Use the `topics` subcommand for other modules when node existence is not enough
and the module must prove that a topic is publishing real data.

`livox.model` is passed to `livox_ros_driver2/msg_multi_MID360_launch.py`:

```yaml
livox:
  model: mid360   # multi_MID360_config.json
  # model: mid360s  # multi_MID360s_config.json
```

Default readiness checks:

```yaml
nav_bridge:
  readiness:
    type: nav_bridge
    topics:
      - /battery/level
    stand_service: /nav_bridge_node/stand

livox:
  model: mid360
  readiness:
    type: nodes
    nodes:
      - /livox_lidar_publisher

slam:
  readiness:
    type: localization_init
    status_topic: /localization_init_status
    release_control_on_blocked: true
    release_control_service: /nav_bridge_node/release_control

terrain:
  readiness:
    type: nodes
    nodes:
      - /gridmapper_node

local_planner:
  readiness:
    type: nodes
    nodes:
      - /localPlanner
      - /pathFollower

global_planner:
  readiness:
    type: nodes
    nodes:
      - /planner_server
      - /controller_server
```

If a readiness check fails or times out, the timeout is printed. By default,
`bringup.shutdown_on_readiness_failure` shuts down the launch so partially
started modules such as `nav_bridge` are not left running alone.

For topic readiness, the timeout applies to each topic individually. For
localization readiness, `timeout_seconds <= 0` means wait indefinitely.

Disable readiness waiting and use only timed startup:

```yaml
bringup:
  wait_for_readiness: false
  start_delay_seconds: 1.0
```

## Command-Line Overrides

Launch arguments override `config/navigate.yaml` for one run.

Examples:

```bash
ros2 launch inspection_bringup navigation.launch.py \
  enable_nav_bridge:=true \
  enable_livox:=false \
  livox_model:=mid360 \
  slam_prior_dir:=company2 \
  global_initial_map:=company2
```

Start only localization and terrain:

```bash
ros2 launch inspection_bringup navigation.launch.py \
  enable_livox:=false \
  enable_slam:=true \
  enable_terrain:=true \
  enable_local_planner:=false \
  enable_global_planner:=false
```

Open RViz for SLAM or terrain:

```bash
ros2 launch inspection_bringup navigation.launch.py \
  slam_rviz:=true \
  terrain_rviz:=true
```

## Mapping From navigate.sh

The old `x30-company2` commands map to the new launch like this:

```text
livox   -> modules.livox + livox.readiness
nav_bridge -> modules.nav_bridge + nav_bridge.readiness
slam    -> modules.slam + slam.*
terrain -> modules.terrain + terrain.*
local   -> modules.local_planner + local_planner.*
global  -> modules.global_planner + global_planner.*
```

The old screen sessions are no longer used. Process lifecycle is managed by ROS
2 launch, and logs are printed to the launch terminal.
