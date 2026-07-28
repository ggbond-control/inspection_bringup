# Navigation Bringup

This document describes the standalone navigation bringup in
`inspection_bringup`.

The navigation stack is intentionally decoupled from
`inspection_system.launch.py`. Use `inspection_system.launch.py` for the
inspection task modules, and use `navigation.launch.py` for the robot
navigation stack.

## Deployment

The navigation deployment helper keeps all managed source and third-party build
artifacts inside user workspaces. It does not run `sudo`, install apt packages,
write sysctl files, or install libraries into `/usr/local`.

By default, run the helper from a navigation deployment rooted at
`~/Workspace`. The helper creates missing workspace directories before cloning
or building:

```text
~/Workspace/
  algor_ws/
  driver_ws/
  task_ws/
    src/inspection_bringup
```

Use `--workspace-root PATH` to place both workspaces under a different root, or
use `--algor-ws PATH` / `--driver-ws PATH` when the two workspaces should not
share the same parent.

Install system packages before running the helper. The exact apt source is
environment-specific, but the navigation stack expects these families to be
available:

```text
libgoogle-glog-dev
libgflags-dev
libyaml-cpp-dev
libeigen3-dev
libpcl-dev
libopencv-dev
libtbb-dev
opencl-headers
ocl-icd-opencl-dev
libapr1-dev
ros-jazzy-navigation2
ros-jazzy-nav2-bringup
ros-jazzy-grid-map
ros-jazzy-rviz-2d-overlay-msgs
```

`inspection_interfaces` is not managed by the navigation helper. Source an
existing underlay that provides it when building `algor_ws`:

```bash
src/inspection_bringup/scripts/build_navigation.sh \
  --interface-underlay ~/Workspace/task_ws/install/setup.bash
```

Managed repositories are listed in:

```text
config/navigation_deps.repos
```

Missing repositories are cloned with `git clone --depth 1`. Existing
repositories are skipped; the helper does not pull, checkout, reset, or overwrite
local changes.

GTSAM and Livox-SDK2 are built locally into:

```text
driver_ws/third_party/install
```

The helper passes this prefix to CMake and sets package RPATHs to
`driver_ws/third_party/install/lib` and
`driver_ws/third_party/install/lib64`, so installed ROS executables can resolve
the local shared libraries without `sudo make install`. It also prints an
`LD_LIBRARY_PATH` fallback for ad-hoc tools or manually built binaries.

Useful commands:

```bash
# Clone missing repositories, build third-party libraries, driver_ws, and algor_ws.
src/inspection_bringup/scripts/build_navigation.sh \
  --interface-underlay ~/Workspace/task_ws/install/setup.bash

# Use a different deployment root.
src/inspection_bringup/scripts/build_navigation.sh \
  --workspace-root /workspaces/navigation \
  --interface-underlay /workspaces/task_ws/install/setup.bash

# Only clone missing repositories.
src/inspection_bringup/scripts/build_navigation.sh --fetch-only

# Only build local third-party libraries.
src/inspection_bringup/scripts/build_navigation.sh --third-party-only

# Rebuild from existing sources.
src/inspection_bringup/scripts/build_navigation.sh \
  --build-only \
  --interface-underlay ~/Workspace/task_ws/install/setup.bash
```

For the merged Livox stream, keep the runtime DDS settings from
`livox_ros_driver2`:

```bash
export CYCLONEDDS_URI=file://$HOME/Workspace/driver_ws/src/livox_ros_driver2/config/cyclonedds_large_message.xml
```

The Linux socket buffer sysctl settings described by `livox_ros_driver2` still
need to be handled by the deployment environment; the helper intentionally does
not change system configuration.

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
manual  Use nav_bridge for manual cmd_vel forwarding. With map information, it also keeps Livox and faster_lio localization running.
```

Manual mode example:

```bash
ros2 service call /navigation_bringup/start rcl_interfaces/srv/SetParameters \
"{parameters: [
  {name: 'mode', value: {type: 4, string_value: 'manual'}},
  {name: 'slam.prior_dir', value: {type: 4, string_value: '/home/cat/Workspace/Maps/company2'}}
]}"
```

Manual mode without `slam.prior_dir` is also valid. When the supervisor has no
successful map path cached, it starts only `nav_bridge`; Livox and SLAM are not
started. This allows manual cmd_vel forwarding before a map is selected:

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

The supervisor keeps its state only while it is running: `stopped`, `manual`,
or `nav`. Manual has two internal profiles while keeping the same public mode:
`bridge_only` runs `nav_bridge` only when no map path is available, while
`localized` runs `nav_bridge + livox + slam`. Nav additionally runs
`terrain + local_planner + global_planner`.

### 状态转换表

| 当前状态 | 调用目标 | 必填输入 | 执行动作 | 返回结果 |
| --- | --- | --- | --- | --- |
| `stopped`，无地图缓存 | `manual` | 无 | 启动 `nav_bridge`，进入 `manual/bridge_only` | 成功；说明定位因无地图被跳过 |
| `stopped`，有地图缓存或传入 `slam.prior_dir` | `manual` | 有缓存时无；否则有效 `slam.prior_dir` | 启动 `nav_bridge + livox + slam`，进入 `manual/localized` | 成功；定位 ready 后返回 |
| `stopped` | `nav` | 无缓存时为 `slam.prior_dir` 和 `global_planner.initial_map`；否则可复用缓存 | 启动定位基础层，再启动 terrain 和规划层 | 成功；所有 readiness 完成后返回 |
| `manual/bridge_only` | `manual`，补传 `slam.prior_dir` | 有效 `slam.prior_dir` | 全量重启为 `manual/localized` | 成功；定位 ready 后返回 |
| `manual/bridge_only` | `nav` | 显式 `slam.prior_dir` 和 `global_planner.initial_map` | 全量重启定位基础层和导航扩展层 | 成功；全部 readiness 完成后返回 |
| `manual/localized` | `nav` | 显式 `global_planner.initial_map` | 保留定位，仅启动 terrain 和规划层 | 成功；扩展层 ready 后返回 |
| `nav` | `manual` | 无 | 停止 terrain 和规划层，保留定位 | 成功；进入 `manual/localized` |
| 任意运行状态 | 同一 mode，且非 `mode` 参数变化 | 变化的参数 | 全量重启为目标配置 | 成功或失败；失败原因在 service result 中返回 |
| 任意状态 | `nav` 缺少所需地图输入 | 缺少的参数 | 不启动或不切换 | 失败；service result 说明缺失参数 |

### State Transition Table

| Current state | Requested mode | Required input | Action | Service result |
| --- | --- | --- | --- | --- |
| `stopped`, no map cache | `manual` | None | Start `nav_bridge`; enter `manual/bridge_only` | Success; localization is skipped because no map is available |
| `stopped`, cached map or supplied `slam.prior_dir` | `manual` | None when cached; otherwise valid `slam.prior_dir` | Start `nav_bridge + livox + slam`; enter `manual/localized` | Success after localization is ready |
| `stopped` | `nav` | `slam.prior_dir` and `global_planner.initial_map` when uncached; otherwise reuse the successful cache | Start localization, then terrain and planners | Success after all readiness checks finish |
| `manual/bridge_only` | `manual` with a supplied map path | Valid `slam.prior_dir` | Fully restart into `manual/localized` | Success after localization is ready |
| `manual/bridge_only` | `nav` | Explicit `slam.prior_dir` and `global_planner.initial_map` | Fully restart localization and navigation extensions | Success after all readiness checks finish |
| `manual/localized` | `nav` | Explicit `global_planner.initial_map` | Keep localization; start terrain and planners only | Success after extension readiness checks finish |
| `nav` | `manual` | None | Stop terrain and planners; retain localization | Success; enter `manual/localized` |
| Any running state | Same mode with a changed non-`mode` parameter | Changed parameter | Fully restart with the requested configuration | Success or failure; reason is returned in the service result |
| Any state | `nav` without required map input | Missing parameter | Do not start or switch | Failure; service result names the missing input |

Pure mode switching preserves localization. `nav -> manual` stops only terrain
and planners. `localized manual -> nav` starts only terrain and planners, but
each such request must explicitly contain `global_planner.initial_map`. A
`bridge_only manual -> nav` request must explicitly contain both
`slam.prior_dir` and `global_planner.initial_map`, and uses a full restart to
start localization before navigation.

An explicit non-mode parameter whose value differs from the active runtime
configuration triggers a full restart. Otherwise a same-mode request succeeds
without restarting modules.

When no map path is cached in the current supervisor process, `slam.prior_dir`
is optional only for a bridge-only manual startup. A first startup directly to
`nav` requires both `slam.prior_dir` and `global_planner.initial_map`. Missing
required map input is returned as a failed result from
`/navigation_bringup/start`.

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
When a service request explicitly changes `slam.prior_dir` but omits
`global_planner.multi_map_dir`, the planner directory is updated to the same
path as well.

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

`config/navigate.yaml` is split by module. The three sequences are the only YAML
source of truth for which modules start:

```yaml
bringup:
  start_mode: service
  start_service: /navigation_bringup/start
  result_timeout_seconds: 0.0
  manual_without_map_sequence:
    - nav_bridge
  manual_sequence:
    - nav_bridge
    - livox
    - slam
  nav_extension_sequence:
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
    type: topics
    topics:
      - /livox/lidar

slam:
  relocal: true
  prior_dir: ""
  readiness:
    type: localization_init
    status_topic: /localization_init_status
    timeout_seconds: 0.0
    blocked_is_failure: true
    release_control_on_blocked: true
    release_control_service: /nav_bridge_node/release_control
    release_control_timeout_seconds: 5.0
```

`bringup.manual_without_map_sequence` controls bridge-only manual startup when
no map path is available. `bringup.manual_sequence` controls localized manual
startup, while
`bringup.nav_extension_sequence` controls the modules added only in nav mode.
Nav mode runs the localized manual and extension sequences in that order. The
sequences must be non-empty and contain exactly their intended modules:
`manual_without_map_sequence` is `nav_bridge`, `manual_sequence` is
`nav_bridge + livox + slam`, and `nav_extension_sequence` is terrain plus both
planners. `bringup` also controls launch timing and readiness wait behavior.
Each module section contains only that module's launch arguments and readiness
checks.

## Readiness Wait

When `bringup.start_mode` is `service`, `navigation.launch.py` starts a
persistent `scripts/navigation_supervisor.py` service. For every accepted
`bringup.start_service` call, the supervisor applies matching `SetParameters`
overrides and uses two independently managed immediate-mode workers: the base
localization layer (`nav_bridge`, Livox, SLAM) and the navigation extension
layer (terrain and planners). Each worker reports its final readiness result
back to the supervisor.

This separation allows mode changes without restarting localization. A full
restart is used only when an explicitly supplied non-mode parameter differs
from the active configuration. The top-level service process remains available
after both successful and failed attempts.

The supervisor also checks both worker processes once per second. If the base
localization worker exits, it stops the navigation extension and changes its
state to `stopped`; if only the extension worker exits, it changes its state to
`manual`. This detects worker-launch failures after startup, but it is not a
replacement for per-module runtime health interfaces.

`bringup.result_timeout_seconds` controls how long the service waits for the
final launch result. Values `<= 0` mean wait without a timeout.

When `bringup.wait_for_readiness` is true, each module starts, waits for that
module's configured `readiness`, then starts the next module after
`bringup.start_delay_seconds`.

Set `bringup.start_mode: immediate` to use the older behavior where launch
starts the sequence immediately without waiting for the service.

The mode-specific module order comes from the three sequences:

```yaml
bringup:
  manual_without_map_sequence:
    - nav_bridge
  manual_sequence:
    - nav_bridge
    - livox
    - slam
  nav_extension_sequence:
    - terrain
    - local_planner
    - global_planner
```

Invalid sequence entries are rejected by `/navigation_bringup/start` with a
clear failure reason instead of starting a partial stack.

`nav_bridge` uses a custom readiness check before the rest of the stack starts:

1. Subscribe once to every topic in `nav_bridge.readiness.topics` with
   `best_effort + volatile + depth 1` QoS and wait for one message.
2. Call `nav_bridge.readiness.stand_service` as `std_srvs/srv/Trigger`.
3. Continue only when the service response contains `success: true`.

For `/battery/level`, the readiness log prints the received `UInt8` value as a
percentage. Topic readiness uses a native `rclpy` subscriber; it does not spawn
`ros2 topic echo --once` processes.

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
python3 scripts/wait_for_ready.py topics --name livox --timeout 10.0 /livox/lidar
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
    type: topics
    topics:
      - /livox/lidar

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

These launch arguments apply only when `bringup.start_mode: immediate`. The
default service mode starts only the supervisor; pass configuration overrides
to `/navigation_bringup/start` instead.

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

Start base localization and terrain without planners:

```bash
ros2 launch inspection_bringup navigation.launch.py \
  enable_nav_bridge:=true \
  enable_livox:=true \
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
livox   -> manual_sequence + livox.readiness
nav_bridge -> manual_without_map_sequence or manual_sequence + nav_bridge.readiness
slam    -> manual_sequence + slam.*
terrain -> nav_extension_sequence + terrain.*
local   -> nav_extension_sequence + local_planner.*
global  -> nav_extension_sequence + global_planner.*
```

The old screen sessions are no longer used. Process lifecycle is managed by ROS
2 launch, and logs are printed to the launch terminal.
