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

After a successful build, the helper prints this optional command for systems
that require access to GPU or video devices. Run it manually, then log out and
back in for the membership change to take effect:

```bash
sudo usermod -aG video,render $USER
```

By default, run the helper from a navigation deployment rooted at
`~/Workspace`. The helper creates missing workspace directories before cloning
or building:

```text
~/Workspace/
  algor_ws/
  driver_ws/
  task_ws/
    src/inspection_bringup
    install/  # Existing inspection_interfaces and inspection_bringup overlays
```

Use `--workspace-root PATH` to place both workspaces under a different root, or
use `--algor-ws PATH` / `--driver-ws PATH` when the two workspaces should not
share the same parent. `inspection_bringup` may live in `task_ws`, `algor_ws`,
or another workspace. Its location does not affect the deployment helper: the
helper never builds `inspection_bringup` and only builds packages from
`config/navigation_deps.repos`.

Install system packages before running the helper. The exact apt source is
environment-specific, but the navigation stack expects these families to be
available:

```text
sudo apt install libgoogle-glog-dev \
libgflags-dev \
libyaml-cpp-dev \
libeigen3-dev \
libpcl-dev \
libopencv-dev \
libtbb-dev \
opencl-headers \
ocl-icd-opencl-dev \
ros-jazzy-pcl-ros \
ros-jazzy-navigation2 \
ros-jazzy-grid-map \
ros-jazzy-rviz-2d-overlay-msgs \
opencl-headers \
ocl-icd-opencl-dev \
mesa-opencl-icd \
clinfo
```

`inspection_interfaces` is not managed by the navigation helper. Source an
existing underlay that provides it when building `algor_ws`:

```bash
task_ws/src/inspection_bringup/scripts/build_navigation.sh \
  --interface-underlay ~/Workspace/task_ws/install/setup.bash
```

Managed repositories are listed in:

```text
config/navigation_deps.repos
```

Missing repositories are cloned with `git clone --depth 1`. Navigation ROS
repositories are pinned to their `jazzy` branches; GTSAM remains pinned to the
`4.2.0` tag because it is not a ROS Jazzy repository. Existing repositories are
skipped; the helper does not pull, checkout, reset, or overwrite local changes.
It reports an existing repository whose origin or checked-out branch/tag does
not match the manifest.

GTSAM and Livox-SDK2 are built locally into:

```text
driver_ws/third_party/install
```

The helper passes this prefix to CMake and sets package RPATHs to
`driver_ws/third_party/install/lib` and
`driver_ws/third_party/install/lib64`, so installed ROS executables can resolve
the local shared libraries without `sudo make install`. It also prints an
`LD_LIBRARY_PATH` fallback for ad-hoc tools or manually built binaries.

Before building, the helper clears inherited ROS overlay variables and sources
only ROS Jazzy, then `driver_ws`, and the supplied `inspection_interfaces`
underlay before building `algor_ws`. This prevents old workspaces from
accidentally satisfying dependencies. It also converts Linux colon-separated
prefix paths to CMake's semicolon-separated list format before passing `-D`
CMake options. It discovers package manifests only inside repositories listed
in `config/navigation_deps.repos`, so unrelated packages in either workspace,
including `inspection_bringup`, are not built.

Every ROS package contained in the managed repositories receives these CMake
settings:

```text
-Wno-dev
-DCMAKE_BUILD_TYPE=Release
-DCMAKE_EXPORT_COMPILE_COMMANDS=1
--symlink-install
```

GTSAM and Livox-SDK2 remain native CMake projects and receive the equivalent
Release and compile-command options.

Useful commands:

```bash
# Clone missing repositories, then build managed third-party, driver, and algor packages.
task_ws/src/inspection_bringup/scripts/build_navigation.sh \
  --interface-underlay ~/Workspace/task_ws/install/setup.bash

# Use a different deployment root.
task_ws/src/inspection_bringup/scripts/build_navigation.sh \
  --workspace-root /workspaces/navigation \
  --interface-underlay /workspaces/task_ws/install/setup.bash

# Only clone missing repositories.
task_ws/src/inspection_bringup/scripts/build_navigation.sh --fetch-only

# Only build local third-party libraries.
task_ws/src/inspection_bringup/scripts/build_navigation.sh --third-party-only

# Rebuild from existing sources.
task_ws/src/inspection_bringup/scripts/build_navigation.sh \
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

Build the navigation workspaces with the deployment helper, then source the
resulting overlays in order:

```bash
~/Workspace/task_ws/src/inspection_bringup/scripts/build_navigation.sh \
  --interface-underlay ~/Workspace/task_ws/install/setup.bash

source /opt/ros/jazzy/setup.bash
source ~/Workspace/driver_ws/install/setup.bash
source ~/Workspace/task_ws/install/setup.bash
source ~/Workspace/algor_ws/install/setup.bash
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
add     Start nav_bridge, Livox, SLAM, and the global planner, then release nav_bridge control.
```

`add` always requires explicit `slam.prior_dir` and
`global_planner.initial_map`, even when the supervisor has cached map values:

```bash
ros2 service call /navigation_bringup/start rcl_interfaces/srv/SetParameters \
"{parameters: [
  {name: 'mode', value: {type: 4, string_value: 'add'}},
  {name: 'slam.prior_dir', value: {type: 4, string_value: '/home/cat/Workspace/Maps/company2'}},
  {name: 'global_planner.initial_map', value: {type: 4, string_value: 'map_000'}}
]}"
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
`nav`, or `add`. Manual has two internal profiles while keeping the same public mode:
`bridge_only` runs `nav_bridge` only when no map path is available, while
`localized` runs `nav_bridge + livox + slam`. Nav additionally runs
`terrain + local_planner + global_planner`; add runs only `global_planner` as
its extension. On a cold add startup, nav_bridge control is acquired first and
then explicitly released.

### 状态转换表

| 当前状态 | 调用目标 | 必填输入 | 执行动作 | 返回结果 |
| --- | --- | --- | --- | --- |
| `stopped`，无地图缓存 | `manual` | 无 | 启动 `nav_bridge`，进入 `manual/bridge_only` | 成功；说明定位因无地图被跳过 |
| `stopped`，有地图缓存或传入 `slam.prior_dir` | `manual` | 有缓存时无；否则有效 `slam.prior_dir` | 启动 `nav_bridge + livox + slam`，进入 `manual/localized` | 成功；定位 ready 后返回 |
| `stopped` | `nav` | 无缓存时为 `slam.prior_dir` 和 `global_planner.initial_map`；否则可复用缓存 | 启动定位基础层，再启动 terrain 和规划层 | 成功；所有 readiness 完成后返回 |
| `manual/bridge_only` | `manual`，补传 `slam.prior_dir` | 有效 `slam.prior_dir` | 全量重启为 `manual/localized` | 成功；定位 ready 后返回 |
| `manual/bridge_only` | `nav` | 显式 `slam.prior_dir` 和 `global_planner.initial_map` | 全量重启定位基础层和导航扩展层 | 成功；全部 readiness 完成后返回 |
| `manual/localized` | `nav` | 显式 `global_planner.initial_map` | 保留定位，仅启动 terrain 和规划层 | 成功；扩展层 ready 后返回 |
| `nav` | `manual` | 无 | 停止 terrain、local planner 和 global planner，保留定位 | 成功；进入 `manual/localized` |
| 任意状态 | `add` | 显式 `slam.prior_dir`、`global_planner.initial_map` | 启动或保留定位及 global planner；冷启动时先接管再释放控制权 | 成功；不启动 terrain/local planner |
| `nav` | `add` | 显式 `slam.prior_dir`、`global_planner.initial_map` | 仅停止 terrain 和 local planner，保留 global planner，再释放控制权 | 成功；global planner 不重启 |
| `add` | `nav` | 无额外地图参数 | 显式调用 `/nav_bridge_node/stand` 成功后，仅启动 terrain 和 local planner | 成功；global planner 保留；控制权接管失败则不启动 local planner |
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
| `nav` | `manual` | None | Stop terrain, local planner, and global planner; retain localization | Success; enter `manual/localized` |
| Any state | `add` | Explicit `slam.prior_dir` and `global_planner.initial_map` | Start or retain localization and global planner; a cold startup acquires then releases control | Success; terrain and local planner remain stopped |
| `nav` | `add` | Explicit `slam.prior_dir` and `global_planner.initial_map` | Stop only terrain and local planner, retain global planner, then release control | Success; global planner is not restarted |
| `add` | `nav` | No extra map input | Call `/nav_bridge_node/stand`, then start only terrain and local planner | Failure if control acquisition fails; local planner is not started; global planner is retained |
| Any running state | Same mode with a changed non-`mode` parameter | Changed parameter | Fully restart with the requested configuration | Success or failure; reason is returned in the service result |
| Any state | `nav` without required map input | Missing parameter | Do not start or switch | Failure; service result names the missing input |

Pure mode switching preserves localization. `nav -> manual` stops terrain and
all planners. `localized manual -> nav` starts only terrain and planners, but
each such request must explicitly contain `global_planner.initial_map`. A
`bridge_only manual -> nav` request must explicitly contain both
`slam.prior_dir` and `global_planner.initial_map`, and uses a full restart to
start localization before navigation. `nav -> add` stops only terrain and
local planner, retaining global planner. `add -> nav` first explicitly
reacquires control with `/nav_bridge_node/stand`, then starts only terrain and
local planner; it never relies on local planner cmd_vel publication to acquire
nav_bridge control.

An explicit non-mode parameter whose value differs from the active runtime
configuration triggers a full restart. Otherwise a same-mode request succeeds
without restarting modules. The supervisor has one base worker and one worker
per extension (`terrain`, `local_planner`, `global_planner`), so a warm mode
switch stops and starts only the extension-module difference.

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

`config/navigate.yaml` is split into reusable module settings and mode-specific
ordered sequences. Actions are `std_srvs/srv/Trigger` calls and are executed in
the same sequence as modules:

```yaml
bringup:
  start_mode: service
  start_service: /navigation_bringup/start
  result_timeout_seconds: 0.0
  actions:
    acquire_control:
      type: trigger_service
      service: /nav_bridge_node/stand
      timeout_seconds: 30.0
    release_control:
      type: trigger_service
      service: /nav_bridge_node/release_control
      timeout_seconds: 5.0
  modes:
    manual:
      control_action: acquire_control
      bridge_only_sequence: [nav_bridge, acquire_control]
      sequence: [nav_bridge, acquire_control, livox, slam]
    nav:
      control_action: acquire_control
      sequence: [nav_bridge, acquire_control, livox, slam, terrain, local_planner, global_planner]
    add:
      startup_control_action: acquire_control
      control_action: release_control
      required_parameters: [slam.prior_dir, global_planner.initial_map]
      sequence: [nav_bridge, acquire_control, release_control, livox, slam, global_planner]
  start_delay_seconds: 1.0
  wait_for_readiness: true
  shutdown_on_readiness_failure: true
  wait_timeout_seconds: 10.0

nav_bridge:
  readiness:
    type: topics
    topics:
      - /battery/level
    timeout_seconds: 10.0

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

`manual.bridge_only_sequence` is selected only without a map path. Every mode
declares the complete ordered stack it needs. `startup_control_action` must
immediately follow `nav_bridge`. `control_action` is repeated on a warm mode
switch, so add can acquire control at cold startup and release it afterward
while still using only `release_control` for `nav -> add`. `bringup` also
controls launch timing and readiness wait
behavior. Each module section contains only that module's launch arguments and
readiness checks. New modes can be added in YAML by composing the existing
module names and configured actions. Base modules and actions must precede
terrain/planner extensions; adding a new launchable module still requires launch
support in `navigation.launch.py`.

## Readiness Wait

When `bringup.start_mode` is `service`, `navigation.launch.py` starts a
persistent `scripts/navigation_supervisor.py` service. For every accepted
`bringup.start_service` call, the supervisor applies matching `SetParameters`
overrides and uses independently managed immediate-mode workers: one base
localization worker (`nav_bridge`, Livox, SLAM) and one worker per navigation
extension (`terrain`, `local_planner`, `global_planner`). Each worker reports
its final readiness result back to the supervisor.

This separation allows mode changes without restarting localization. A full
restart is used only when an explicitly supplied non-mode parameter differs
from the active configuration. The top-level service process remains available
after both successful and failed attempts.

The supervisor also checks every worker process once per second. If the base
localization worker exits, it stops all extensions and changes its state to
`stopped`; if any expected extension exits, it stops the remaining extensions
and changes its state to the configured fallback mode (default `manual`). This
detects worker-launch failures after startup, but it is not a replacement for
per-module runtime health interfaces.

`bringup.result_timeout_seconds` controls how long the service waits for the
final launch result. Values `<= 0` mean wait without a timeout.

When `bringup.wait_for_readiness` is true, each module starts, waits for that
module's configured `readiness`, then starts the next module after
`bringup.start_delay_seconds`.

Set `bringup.start_mode: immediate` to use the older behavior where launch
starts the sequence immediately without waiting for the service.

For `bringup.start_mode: service`, supervisor workers always force readiness
waiting. This ensures a module or control action failure is returned by
`/navigation_bringup/start`, even if the source configuration sets
`wait_for_readiness: false`.

The mode-specific order comes from `bringup.modes`:

```yaml
bringup:
  modes:
    add:
      startup_control_action: acquire_control
      control_action: release_control
      sequence: [nav_bridge, acquire_control, release_control, livox, slam, global_planner]
```

Invalid sequence entries are rejected by `/navigation_bringup/start` with a
clear failure reason instead of starting a partial stack.

`nav_bridge` readiness verifies that its battery topic has real data before the
next sequence step starts:

1. Subscribe once to every topic in `nav_bridge.readiness.topics` with
   `best_effort + volatile + depth 1` QoS and wait for one message.
2. Continue to the next mode step only after a message arrives.

`acquire_control` and `release_control` are separate Trigger actions. Each
requires `success: true`; an action failure fails `/navigation_bringup/start`
with the service output as the reason.

For `/battery/level`, the readiness log prints the received `UInt8` value as a
percentage. Topic readiness uses a native `rclpy` subscriber; it does not spawn
`ros2 topic echo --once` processes.

When `bringup.wait_for_readiness` is false, actions are still launched in their
configured order, but modules no longer wait for preceding readiness checks.

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
python3 scripts/wait_for_ready.py trigger --name acquire_control --service /nav_bridge_node/stand --timeout 30.0
python3 scripts/wait_for_ready.py trigger --name release_control --service /nav_bridge_node/release_control --timeout 5.0
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
    type: topics
    topics:
      - /battery/level

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
livox   -> bringup.modes.<mode>.sequence + livox.readiness
nav_bridge -> bringup.modes.<mode>.sequence + nav_bridge.readiness
slam    -> bringup.modes.<mode>.sequence + slam.*
terrain -> bringup.modes.nav.sequence + terrain.*
local   -> bringup.modes.nav.sequence + local_planner.*
global  -> bringup.modes.nav.sequence or bringup.modes.add.sequence + global_planner.*
```

The old screen sessions are no longer used. Process lifecycle is managed by ROS
2 launch, and logs are printed to the launch terminal.
