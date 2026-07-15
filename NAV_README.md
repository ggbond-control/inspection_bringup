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
  {name: 'mode', value: {type: 4, string_value: 'nav'}},
  {name: 'livox.model', value: {type: 4, string_value: 'mid360'}},
  {name: 'slam.prior_dir', value: {type: 4, string_value: '/home/cat/Workspace/Maps/company2'}},
  {name: 'global_planner.initial_map', value: {type: 4, string_value: 'map_000'}}
]}"
```

If `global_planner.multi_map_dir` is omitted, it reuses `slam.prior_dir`.

`mode` accepts:

```text
nav     Start the configured navigation stack normally.
manual  Start only nav_bridge for cmd_vel forwarding; all sensors and navigation algorithms stay off.
```

Manual mode example:

```bash
ros2 service call /navigation_bringup/start rcl_interfaces/srv/SetParameters \
"{parameters: [
  {name: 'mode', value: {type: 4, string_value: 'manual'}}
]}"
```

Use an empty parameter list to start with `config/navigate.yaml` unchanged:

```bash
ros2 service call /navigation_bringup/start rcl_interfaces/srv/SetParameters \
"{parameters: []}"
```

The service returns after the configured startup sequence and readiness checks
finish. If a module readiness check fails, the service result contains the
failure reason. The supervisor remains running after either outcome.

Every later call first stops the currently active navigation worker (if any),
then starts a new worker using the new parameters. This makes `nav` and
`manual` mode switching explicit: call the same service again with the desired
`mode`; no second top-level launch is needed.

Only one startup request may run at a time. While a request is waiting for
readiness, another call is rejected immediately with
`navigation startup already in progress`; it is not queued.

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

Required runtime values (fill these explicitly):

```yaml
slam:
  prior_dir: "/home/chen/Workspace/Maps/company2"

global_planner:
  initial_map: "map_000"
  params_file: "new_local"
  use_fake_cmdvel: true
  patrol_loops: 1
```

`global_planner.multi_map_dir` can be omitted and will reuse
`slam.prior_dir`.

## Multi-Map Data

`global_planner.multi_map_dir` must point to the gridmapper output directory:

```text
multi_maps/
  map_000.yaml
  map_000.png
  map_001.yaml
  map_001.png
  map_relations.csv
  transition_points.csv
  states/*.gridmap.bin
```

`map_relations.csv` uses `from_map,to_map,dx,dy` to describe each map frame
relative to ROOT/world. `transition_points.csv` uses
`transition_id,from_map,to_map,world_x,world_y,world_z,world_yaw_rad,bidirectional,type`;
the world pose fields are already ROOT/world coordinates, not local map or yaml
origin coordinates.

When sending an external navigation goal to `multi_map_nav`, set
`PoseStamped.header.frame_id` to the target local map name such as `map_001`,
and put `pose.position` in that local map frame. Legal map names are inferred
from `map_*.yaml` under `multi_map_dir`.

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
  prior_dir: ""
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

When `bringup.start_mode` is `service`, `navigation.launch.py` starts a
persistent `scripts/navigation_supervisor.py` service. For every accepted
`bringup.start_service` call, the supervisor applies matching `SetParameters`
overrides, writes an isolated resolved runtime YAML, and starts one worker
instance of `navigation.launch.py` in immediate mode. The worker starts the
configured sequence and reports its final readiness result back to the
supervisor.

Before the next accepted call, the supervisor stops the previous worker and
its launched modules, then creates a new worker. The top-level service process
therefore remains available after both successful and failed startup attempts.

`bringup.result_timeout_seconds` controls how long the service waits for the
final launch result. Values `<= 0` mean wait without a timeout.

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
    type: health
    topic: /multi_map_nav/health
```

If a readiness check fails or times out, the timeout is printed. By default,
`bringup.shutdown_on_readiness_failure` shuts down the launch so partially
started modules such as `nav_bridge` are not left running alone.

For topic readiness, the timeout applies to each topic individually. For
localization readiness, `timeout_seconds <= 0` means wait indefinitely.

`global_planner` expects `multi_map_nav` to publish
`diagnostic_msgs/msg/DiagnosticStatus` on `/multi_map_nav/health` with
transient local + reliable QoS. `level == OK` means ready. `level == WARN`
means still initializing. `level == ERROR` or `level == STALE` fails readiness
immediately and the diagnostic message is returned through
`/navigation_bringup/start`.

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
  global_initial_map:=map_000 \
  global_multi_map_dir:=~/Workspace/algor_ws/src/gridmapper/data/Output/multi_maps
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
