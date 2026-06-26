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
  sequence:
    - nav_bridge
    - livox
    - slam
    - terrain
    - local_planner
    - global_planner
  start_delay_seconds: 1.0
  wait_for_nodes: true
  shutdown_on_readiness_failure: true
  wait_timeout_seconds: 10.0

nav_bridge:
  wait_topics:
    - /battery/level
  stand_service: /nav_bridge_node/stand
  topic_timeout_seconds: 10.0
  stand_timeout_seconds: 10.0

livox:
  wait_nodes:
    - /livox_lidar_publisher

slam:
  relocal: true
  prior_dir: "company2"
  wait_nodes:
    - /laser_mapping
```

`modules` controls whether each module is launched. `bringup.sequence` controls
the launch order. `bringup` also controls the launch timing and readiness wait
behavior. Each module section contains only that module's launch arguments and
readiness checks.

## Node Readiness Wait

When `bringup.wait_for_nodes` is true, the launch file starts one module, waits
for that module's configured `wait_nodes`, then starts the next module after
`bringup.start_delay_seconds`.

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

1. Wait for one message on every topic in `nav_bridge.wait_topics`.
2. Call `nav_bridge.stand_service` as `std_srvs/srv/Trigger`.
3. Continue only when the service response contains `success: true`.

The readiness helper is:

```bash
scripts/wait_for_ready.py
```

It has subcommands for the supported readiness checks:

```bash
python3 scripts/wait_for_ready.py nodes --name livox --timeout 10.0 /livox_lidar_publisher
python3 scripts/wait_for_ready.py topics --name battery --timeout 10.0 /battery/level
python3 scripts/wait_for_ready.py nav_bridge --topic /battery/level --stand-service /nav_bridge_node/stand
```

Use the `topics` subcommand for other modules when node existence is not enough
and the module must prove that a topic is publishing real data.

Default readiness checks:

```yaml
nav_bridge:
  wait_topics:
    - /battery/level
  stand_service: /nav_bridge_node/stand

livox:
  wait_nodes:
    - /livox_lidar_publisher

slam:
  wait_nodes:
    - /laser_mapping

terrain:
  wait_nodes:
    - /gridmapper_node

local_planner:
  wait_nodes:
    - /localPlanner
    - /pathFollower

global_planner:
  wait_nodes:
    - /planner_server
    - /controller_server
```

If a readiness check fails or times out, the timeout is printed. By default,
`bringup.shutdown_on_readiness_failure` shuts down the launch so partially
started modules such as `nav_bridge` are not left running alone.

Disable readiness waiting and use only timed startup:

```yaml
bringup:
  wait_for_nodes: false
  start_delay_seconds: 1.0
```

## Command-Line Overrides

Launch arguments override `config/navigate.yaml` for one run.

Examples:

```bash
ros2 launch inspection_bringup navigation.launch.py \
  enable_nav_bridge:=true \
  enable_livox:=false \
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
livox   -> modules.livox + livox.wait_nodes
nav_bridge -> modules.nav_bridge + nav_bridge.*
slam    -> modules.slam + slam.*
terrain -> modules.terrain + terrain.*
local   -> modules.local_planner + local_planner.*
global  -> modules.global_planner + global_planner.*
```

The old screen sessions are no longer used. Process lifecycle is managed by ROS
2 launch, and logs are printed to the launch terminal.
