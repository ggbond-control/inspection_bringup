# Navigation Bringup

This document describes the standalone navigation bringup in
`inspection_bringup`.

For model selection and per-robot configuration, see
[配置与多机器人部署](CONFIGURATION.md). The default launch now reads the shared,
model, and robot layers when `robot.yaml` exists. An explicit
`navigate_config_path` continues to load that standalone YAML only.
Nav bridge uses a complete `models/<model>/navigation/param_<model>_nav_bridge.yaml`
selected by `nav_bridge.config`, with no shared default parameter file.
Model launch settings select `robot_type: x30/d1m/cyvet`; per-robot changes use
`navigation.nav_bridge.parameters` in robot.yaml. The child launch still receives
`params_file` and accepts `d1_max` as a legacy alias for `d1m`.
SLAM now uses `config/navigation/param_default_faster_lio.yaml` plus
`config/models/<model>/navigation/param_<model>_faster_lio.yaml` and optional
`robot.yaml` inline parameters. The complete default follows X30/Mid360 and
includes RTK settings with RTK disabled; D1M enables RTK and selects Airy inputs.
Cyvet currently inherits the Mid360 defaults. Bringup merges these layers into
one snapshot passed to the existing SLAM launch.
Multi-map navigation uses `config/navigation/param_default_multi_map_nav.yaml`
with `config/models/<model>/navigation/param_<model>_multi_map_nav.yaml`.
X30/Cyvet retain the original `new_local` settings; D1M retains `straight_local`,
including its own planner plugin and `straightening` parameters. The merged
snapshot is selected by `global_planner.config` and passed to the child launch as `params_path`.
Local planning uses `config/navigation/param_default_local_planner.yaml` plus
`param_<model>_local_planner.yaml` in each model directory. The complete `flat`,
`offroad`, and `initialModel` settings live only in model files. Shared defaults
follow the X30 baseline with omni tracking and `odomTimestampCheckEn: true`.
D1M overrides timestamp checking to false and alone defines `turn_preferred` and `turn_*`
parameters. Cyvet geometry, motion models, and tracking settings follow its
deployed configuration.
Child parameter file paths use the owning ROS package's configuration directory
for all relative paths (including `./`); absolute paths select external files.
For example, `terrain.config: gridmapper_local.yaml` selects the existing
`gridmapper/config/gridmapper_local.yaml`. See the linked guide for each package.

The navigation stack is intentionally decoupled from
`inspection_system.launch.py`. Use `inspection_system.launch.py` for the
inspection task modules, and use `navigation.launch.py` for the robot
navigation stack.

## System Dependencies

Install the system dependencies before running `build_navigation.sh` on Ubuntu
24.04 / ROS 2 Jazzy. The build helper checks dependencies but does not install
apt packages automatically:

```bash
sudo apt update
sudo apt install -y \
  libgoogle-glog-dev \
  libgflags-dev \
  libyaml-cpp-dev \
  libeigen3-dev \
  libpcl-dev \
  libopencv-dev \
  libtbb-dev \
  libgeographiclib-dev \
  opencl-headers \
  ocl-icd-opencl-dev \
  mesa-opencl-icd \
  clinfo \
  ros-jazzy-pcl-ros \
  ros-jazzy-navigation2 \
  ros-jazzy-grid-map \
  ros-jazzy-rviz-2d-overlay-msgs
```

The CMake dependency checks are defined in `config/build/navigation.yaml`.
D1M additionally requires a built `zenoh_ws/install/local_setup.bash` and the
vendored D1 Max SDK in nav_bridge.

### RK3588 OpenCL: Linux 6.1.118 / Mesa Rusticl / Panthor

The dependency list above includes the OpenCL development files, Mesa ICD and
`clinfo`. For this RK3588 kernel/Mesa setup, allow the current user to access
graphics and render device nodes. Missing
permissions can cause OpenCL initialization failures and, with some drivers,
crashes:

```bash
sudo usermod -aG video,render "$USER"
```

Log out and log in again, or close the SSH session and reconnect, before testing.
Opening another terminal within the same desktop login may retain the old groups.

Enable the Rusticl driver and inspect available OpenCL devices:

```bash
export RUSTICL_ENABLE=panthor
clinfo
```

For this setup, Rusticl needs explicit driver enablement. Check that `clinfo`
reports a `rusticl` platform and a `Mali-G610... (Panfrost)` device. This confirms
device discovery; application execution is a separate check. The displayed device
name can contain `Panfrost` even when this setup uses `panthor` for enablement.
The accepted driver name depends on the installed Mesa build; if no device is
listed, verify its supported driver name and device permissions.

To persist the variable in Zsh, add the following line once to `~/.zshrc`:

```bash
echo 'export RUSTICL_ENABLE=panthor' >> ~/.zshrc
source ~/.zshrc
```

For Bash, use `~/.bashrc` instead.
```

## Deployment

Daily navigation builds use one command, with no required path or middleware flags:

```bash
~/Workspace/task_ws/src/inspection_bringup/scripts/build_navigation.sh
```

The helper reads `config/build/navigation.yaml`, then `config/build/<model>.yaml`
selected by `config/robot.yaml`. `INSPECTION_CONFIG_DIR` selects another config
root. `--model d1m` overrides model selection, and `--config PATH` supplies a build
configuration override. CLI paths override configuration. The default root is
`~/Workspace` (`NAV_WORKSPACE_ROOT` or `--workspace-root` can override it).

D1M explicitly loads the Zenoh underlay before compiling any ROS package and
builds the D1 Max nav bridge backend. It follows this order:

1. Check ROS/RMW, system dependencies, source packages and third-party inputs.
2. Build only `inspection_interfaces` in `task_ws`.
3. Initialize missing local third-party libraries, or reuse installed libraries.
4. Build managed driver packages, including `robots_dog_msgs` and `gnss_driver`.
5. Build managed algorithm packages, including `smac_straight_planner`.

The current build profiles explicitly support D1M. X30 and Cyvet runtime
configuration files do not select their CMake backends: dedicated build profiles
are not yet provided. A fresh unconfigured nav_bridge build defaults to X30;
Cyvet needs its backend enabled and the UniUbi dependencies supplied separately.
Do not reuse a D1M build directory for another model with this version: cached
backend switches are not reset when a model profile is absent. Each additional
model should explicitly set all three `NAV_BRIDGE_BUILD_*` switches before it is
considered supported. `--model` changes this build's profile only; it does not
modify runtime `robot.yaml`.

Each stage loads only its declared underlays. Workspace `local_setup.bash` and
the interface package's own setup are used to avoid recorded chains that import
old task/driver/algorithm overlays. Ambient ROS, middleware and library search
variables are cleared. Repositories are discovered with colcon, respecting
`COLCON_IGNORE`; missing/empty package lists stop the build.

**GTSAM is built on first initialization only.** When its installed CMake config,
headers and libraries already exist, the helper does not configure, compile or
install GTSAM again. It also reuses an existing complete Livox-SDK2 install.
Libraries remain under `driver_ws/third_party/install`, with no system install.
A partial GTSAM install fails with a repair command instead of triggering an
unexpected rebuild. Consumer CMake checks still validate the installed GTSAM
configuration; those checks do not compile GTSAM.

```bash
# Inspect the resolved paths, repositories, middleware and build stages.
scripts/build_navigation.sh --plan

# Check existing sources, middleware/system dependencies and library inputs.
# Does not fetch, update or compile. Missing sources must be fetched first.
scripts/build_navigation.sh --check

# Explicitly rebuild GTSAM, or both third-party projects.
scripts/build_navigation.sh --rebuild-gtsam
scripts/build_navigation.sh --rebuild-third-party

# Validate with separate build/install/log directories, reusing third-party libs.
scripts/build_navigation.sh --output-root ~/tmp/navigation-validation

# Increase per-package jobs; packages still run sequentially, Release by default.
scripts/build_navigation.sh -j2

# Alternate layout, or an externally maintained inspection_interfaces underlay.
scripts/build_navigation.sh --workspace-root /workspaces/robot
scripts/build_navigation.sh --interface-underlay /workspaces/common/install/local_setup.bash
```

`BUILD_JOBS` supplies the job default; `-j`/`--jobs` takes precedence. The default
is one. `--output-root` isolates ROS build/install/log outputs; it continues to
reuse the shared third-party prefix. It does not switch services to those outputs.
On D1M-A (8 GiB RAM), one faster_lio compiler process was observed using about
4.3 GiB; retain `-j1` unless memory measurements justify increasing it.
`--interface-underlay` skips the interface build and uses the supplied setup;
prefer an interface-only or controlled `local_setup.bash` over a historical full
workspace setup chain.

Sources are managed by `config/navigation_deps.repos`. Missing repositories are
cloned; existing checkouts and local edits are retained. `--fetch-only` clones
without building; `--build-only` skips cloning. Existing sources are never
updated implicitly. Use these explicit update operations:

```bash
scripts/build_navigation.sh --update-current-branch
scripts/build_navigation.sh --update-only
scripts/build_navigation.sh --check-updates
```

Updates use ordinary `git pull` on the current branch and honor local Git
settings. They do not switch branches, reset files, or stash changes. Pinned
detached tags are retained. A pull failure prevents the build; earlier repositories
may already have been updated. `--check-updates` fetches/reports without pulling.

`--third-party-only`, `--skip-third-party`, `--skip-driver-build` and
`--skip-algor-build` remain available for diagnosis. `--skip-third-party`
requires complete installed libraries. These switches are unnecessary for normal
incremental builds. Successful builds record source commits, dirty state, model,
RMW and package lists in `algor_ws/navigation-build.json` (under `--output-root`
when selected). Branch manifests are development inputs; use the recorded commit
IDs to establish an immutable release baseline.

D1M does not launch Livox, but `faster_lio` currently depends on its messages and
CMake package, so the driver and SDK are still required during compilation.

## Start

Build the navigation workspaces with the deployment helper, then source the
resulting overlays in order. The following example uses D1M/Zenoh; for other
models, use the middleware and underlays printed by the build helper:

```bash
~/Workspace/task_ws/src/inspection_bringup/scripts/build_navigation.sh

source /opt/ros/jazzy/setup.bash
# D1M uses Zenoh; other models use their configured middleware.
source ~/Workspace/zenoh_ws/install/local_setup.bash
source ~/Workspace/task_ws/install/inspection_interfaces/share/inspection_interfaces/local_setup.bash
source ~/Workspace/driver_ws/install/local_setup.bash
source ~/Workspace/algor_ws/install/local_setup.bash
source ~/Workspace/task_ws/install/local_setup.bash
export RMW_IMPLEMENTATION=rmw_zenoh_cpp
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
  {name: 'slam.prior_dir', value: {type: 4, string_value: '/home/cat/Workspace/Maps/company2'}},
  {name: 'global_planner.initial_map', value: {type: 4, string_value: 'map_000'}},
  {name: 'slam.initial_pose', value: {type: 8, double_array_value: [1.0, 2.0, 0.0, 90.0]}}
]}"
```

This example selects `nav` mode, loads the map directory `company2` and initial
map `map_000`, and supplies an initial localization guess of x=1 m, y=2 m,
z=0 m and yaw=90 degrees for this request.

If `global_planner.multi_map_dir` is omitted, it reuses `slam.prior_dir`.

To override the initial localization guess for this request, add this entry to
the service's `parameters` array:

```yaml
{name: 'slam.initial_pose', value: {type: 8, double_array_value: [1.0, 2.0, 0.0, 90.0]}}
```

To force a full restart even when mode and map parameters are unchanged, add:

```yaml
{name: 'restart', value: {type: 1, bool_value: true}}
```

`restart` is an optional request-only boolean. `true` performs a full restart
when navigation is running, so the supplied initial pose is applied when SLAM
starts again. If stopped, it starts normally. Omitted or `false` retains normal
transition selection. The flag is not cached or passed to child workers. Invalid
ROS types fail validation before any workers are started or stopped.

`slam.initial_pose` accepts 2 to 4 finite numbers `[x, y, z, yaw]`, in meters
and degrees. Missing trailing values are zero: `[1.0, 2.0]` means
`[1.0, 2.0, 0.0, 0.0]`. Integer arrays (`type: 7`, `integer_array_value`) are
also accepted. Other types, invalid lengths and non-finite values return
`successful: false` without starting or stopping workers.

When this request starts SLAM, the array overrides both
`laser_mapping.ros__parameters.initial_alignment.position` and `yaw`, above
defaults, model parameters and `robot.yaml`. It is not cached, written back to
configuration files, compared with previous requests or used to trigger a restart.
If SLAM remains running or is not started, the initial pose is not applied.
Omitting it uses the existing configuration layers.

To force a complete navigation stack restart, add a boolean service parameter:

```yaml
{name: 'restart', value: {type: 1, bool_value: true}}
```

`restart` defaults to false and is never cached. With true, the supervisor first
validates all request parameters and map requirements, then stops all managed
navigation workers together. It waits for their live session processes, including
child process groups, to exit, escalating SIGINT to SIGTERM and SIGKILL as needed.
If shutdown cannot be confirmed, the service returns failure and starts no new
nodes; surviving workers remain tracked for subsequent cleanup. Only after all
old workers exit does startup proceed in the requested mode, including readiness
and control actions. The service returns the final startup result.

The supervisor stays alive and retains its successful map cache. This does not
reload bringup configuration or restart the systemd service. A request may combine
`restart: true` with `slam.initial_pose` to apply a new localization guess during
SLAM startup. With no running stack it performs a normal start. Independently
started external nodes are outside this restart's scope.

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

导航首次启动或切换模式需要运动准备时，会先等待
`/inspection_charge_executor/undock` 完成，再执行 `stand` 或释放控制权。
因此需先启动 system 中的充电执行器；执行器不可用、拒绝或超时均阻止继续启动，
不会回退到 STOP 或仅凭 `/charge_manager_state=0` 放行。
退出等待默认 300 秒，MQTT 地图/导航启动等待默认 600 秒；重定位仍可能需要人工处理，
外层超时不表示后台操作已停止。

导航启动允许主动退出充电。首点的 `charge_exit` 若随后执行，则复用有效的完成确认，
不再脱桩、ready 或改变步态；同时请求会等待同一次退出的结果。
退出接口不自行返航。重复请求已经运行的 `nav` 模式且地图参数未变时直接成功，
不重复取控、站立或退出充电；manual 模式保留显式重新取控行为。

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
configuration or explicit `restart=true` triggers a full restart. Otherwise a same-mode request succeeds
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
  config: "${INSPECTION_CONFIG_DIR}/navigation/param_default_multi_map_nav.yaml"
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
    type: nav_bridge
    topics:
      - /battery/level
    topic_timeout_seconds: 10.0
    charge_precheck: true
    charge_executor_exit_service: /inspection_charge_executor/undock
    charge_check_timeout_seconds: 10.0
    charge_exit_timeout_seconds: 300.0

livox:
  config: "${INSPECTION_CONFIG_DIR}/models/x30/navigation/param_x30_livox_ros_driver2.json"
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
python3 scripts/wait_for_ready.py charge_precheck --charge-executor-exit-service /inspection_charge_executor/undock --charge-exit-timeout 300.0
python3 scripts/wait_for_ready.py trigger --name acquire_control --service /nav_bridge_node/stand --timeout 30.0
python3 scripts/wait_for_ready.py trigger --name release_control --service /nav_bridge_node/release_control --timeout 5.0
python3 scripts/wait_for_ready.py localization-init --status-topic /localization_init_status --timeout 0.0 --release-control-on-blocked
```

Use the `topics` subcommand for other modules when node existence is not enough
and the module must prove that a topic is publishing real data.

`livox.config` selects the complete JSON passed to the driver's `user_config_path`.
There is no shared default JSON or bringup `livox.model` selector. X30 selects its
four-lidar configuration; Cyvet defaults to MID360s; D1M starts no Livox driver.
To select the alternate Cyvet MID360 configuration, override only the file path:

```yaml
model: cyvet
robot_id: CYVET-A
navigation:
  livox:
    config: "${INSPECTION_CONFIG_DIR}/models/cyvet/navigation/param_cyvet_livox_ros_driver2_mid360.json"
```

Default readiness checks:

```yaml
nav_bridge:
  readiness:
    type: topics
    topics:
      - /battery/level

livox:
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
