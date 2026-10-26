# inspection_bringup

Unified bringup launch files for the inspection task system.

For multiple robots, keep shared defaults in `config/*.yaml`, model differences
in `config/models/<model>/`, and machine identity/overrides in the Git-ignored
`config/robot.yaml`. See [配置与多机器人部署](CONFIGURATION.md) for examples,
path resolution, and compatibility with standalone configuration files.

## Build

```bash
cd ~/task_ws
src/inspection_bringup/scripts/build_inspection.sh
source install/setup.zsh
```

The build helper downloads missing managed repositories from
`config/inspection_deps.repos`, skips repositories that already exist, and
builds only with:

```bash
MAKEFLAGS=-j1 CMAKE_BUILD_PARALLEL_LEVEL=1 colcon build \
  --packages-up-to <target> --symlink-install --executor sequential \
  --cmake-force-configure --cmake-args -DCMAKE_BUILD_TYPE=Release
```

It does not run a bare global `colcon build`. `nav_bridge` is intentionally not
listed in the managed dependency file because it is not owned by task hub
bringup.

All CMake packages are explicitly built in Release mode. Packages are built one
at a time, with one build job per package by default (`-j1`). Increase jobs within
each package when needed; packages still run sequentially:

```bash
src/inspection_bringup/scripts/build_inspection.sh -j4
src/inspection_bringup/scripts/build_inspection.sh -j 4
```

`--jobs 4` and `--jobs=4` are also supported. `BUILD_JOBS=4` remains supported as
an environment default; an explicit command-line jobs option takes precedence.
Job counts must be positive integers.

Missing repositories are cloned from each remote's default branch, typically
`main` or `master`. Existing repositories are not updated by default; update
options below pull the currently checked-out branch without switching branches.

Managed repository branches are printed by default before build:

```text
[branch] src/gas_monitor: main
```

Remote update checks are opt-in because they fetch upstream refs:

```bash
src/inspection_bringup/scripts/build_inspection.sh --check-updates
```

Example output:

```text
[branch] src/gas_monitor: main upstream=origin/main up-to-date
[branch] src/inspection_task_hub: main dirty upstream=origin/main behind=2
```

To run ordinary `git pull` in each managed repository before building, run:

```bash
src/inspection_bringup/scripts/build_inspection.sh --update-current-branch
```

The update uses the repository's own upstream and Git settings, including
`pull.rebase` and `pull.ff`. Local edits, local commits and diverged history do
not trigger an extra rejection: if ordinary `git pull` succeeds, the helper
continues. It does not switch branches, force reset, or explicitly stash edits.
A pull failure stops the command before building; earlier repositories may
already have been updated. Fix the Git error and rerun. Detached checkouts at
an explicitly manifest-pinned tag are kept unchanged. To update without building:

```bash
src/inspection_bringup/scripts/build_inspection.sh --update-only
```

Both `build_inspection.sh` and `build_navigation.sh` also support explicit checkout:
`--checkout` always exits after branch operations, without building. Add an update
option for fast-forward synchronization; run a separate command to build afterward.

```bash
# Switch to existing remote dev branches and exit (no merge into existing branches).
src/inspection_bringup/scripts/build_inspection.sh --checkout dev

# Switch to each repository's main branch, fast-forward, then exit.
src/inspection_bringup/scripts/build_navigation.sh --checkout main --update-current-branch

# Switch and fast-forward only; do not build.
src/inspection_bringup/scripts/build_inspection.sh --checkout dev --update-only

# Select a remote explicitly; existing repositories must have this remote configured.
src/inspection_bringup/scripts/build_navigation.sh --checkout main --remote gitlab --update-only

# Build separately when ready.
src/inspection_bringup/scripts/build_navigation.sh
```

Checkout rules (only repositories in the selected `.repos` file are managed;
`inspection_bringup` itself is not implicitly switched):

- Repositories are processed independently in manifest order, with no batch
  preflight. `dev` switches repositories that have a real remote `dev` branch;
  repositories without it keep their current checkout and print a skip reason.
  No branch is pushed or invented by the helper, and no extra option is needed.
- `main` is a mode, not necessarily a literal branch name. An entry's
  `main_branch` wins; otherwise an existing manifest `version` of `main`, `jazzy`
  or `master` is preferred. Without a usable preference, exactly one of those
  branches must exist remotely. Multiple candidates require an explicit choice:

  ```yaml
  repositories:
    src/example:
      type: git
      url: https://github.com/example/example.git
      main_branch: jazzy
  ```

- An explicitly configured `main_branch` must exist; a missing one is an error.
- Manifest-pinned tags, including GTSAM `4.2.0`, stay detached at the pinned
  commit. An attached branch or different commit at that path is an error.
- Fresh remote heads are checked even if a local branch or stale remote-tracking
  ref already exists. Missing local targets are created with remote tracking.
  Existing targets retain their local commits and track the selected remote.
- Shallow/single-branch clones fetch the target explicitly and extend their fetch
  mapping when needed, so later ordinary `git pull` also works on the target.
- Missing repositories are cloned when their turn is reached, directly at the
  resolved branch or pinned tag. If remote `dev` is absent, cloning uses the
  manifest version or remote default branch and dev checkout is skipped.
  They use the manifest URL and name that remote according to `--remote`.
  Unfinished Git operations and branches occupied in other worktrees fail that
  repository; other repositories are still processed.
  Local edits (including staged changes and untracked files) are allowed when
  Git can preserve them. Git checks overwrite conflicts during switch/update;
  a conflict is reported for that repository and processing continues with the
  next. Successfully switched/cloned repositories remain changed.
  Ignored local files are also protected against overwrites during switch/update.
- Checkout alone leaves existing branch commits unchanged. With an update option,
  synchronization uses `merge --ff-only` against the fetched remote ref, regardless
  of `pull.rebase` settings. Divergence fails that repository; local ahead commits are
  preserved and reported. There is no reset, automatic stash or automatic rollback.
  The final summary reports switched, skipped and failed counts. Missing dev
  branches are normal skips. Network/authentication errors and Git conflicts are
  failures, not skips: after all repositories are processed, any failure returns
  a nonzero exit status. Completed repositories are listed; inspect failed repositories too
  before retrying.
- `--checkout` conflicts with `--fetch-only` and `--build-only`, and navigation's
  read-only `--plan`/`--check`. `--remote` requires `--checkout` (default: `origin`).

The `inspection_task_hub` branch rename from `jazzy_dev` to `dev` is a one-time
repository migration, not part of a build. Rename the branch on each maintained
remote first (prefer the hosting platform's native rename), then update the clone:

```bash
git branch -m jazzy_dev dev
git fetch --prune origin
git branch --set-upstream-to=origin/dev dev
git pull --ff-only
```

Check for an existing `dev`, uncommitted work and differing commits before the
migration. Update CI, branch protections and other consumers of `jazzy_dev` as
needed. The build helper only recognizes the final `dev` name.

Disable the branch status output when needed:

```bash
src/inspection_bringup/scripts/build_inspection.sh --no-branch-check
```

Useful variants:

```bash
src/inspection_bringup/scripts/build_inspection.sh --fetch-only
src/inspection_bringup/scripts/build_inspection.sh --fetch-only --check-updates
src/inspection_bringup/scripts/build_inspection.sh --build-only inspection_bringup
src/inspection_bringup/scripts/build_inspection.sh --rosdep inspection_bringup
```

## Start All Modules

Edit `config/system.yaml` for normal defaults such as module switches, device
serial number, and MQTT broker settings:

```yaml
modules:
  gimbal: true
  gas: true
  thermal: true
  mqtt: true

mqtt:
  sn: x30
  host: 127.0.0.1
  port: 1883
  topic_vendor_prefix: fh
  topic_root: device

live_stream:
  config_path: ""
  push_policy: always  # disabled | always | task_scoped
  request_on_startup: true
  ffmpeg_bin: ffmpeg
  restart_interval_sec: 5.0
  gimbal:
    enabled: true
  ffmpeg:
    loglevel: warning
    realtime_input: true
    rtsp_transport: tcp
    video_codec: copy
    audio_codec: copy
    output_format: flv
```

```bash
ros2 launch inspection_bringup inspection_system.launch.py


ros2 launch inspection_bringup navigation.launch.py
```

The launch file prints node logs to the terminal and keeps normal ROS logs under
`~/.ros/log`. Set `ROS_LOG_DIR` before launch if the log directory should be
centralized:

```bash
export ROS_LOG_DIR=~/inspection_logs
```

## Common Switches

Temporary command-line values override `config/system.yaml`:

```bash
ros2 launch inspection_bringup inspection_system.launch.py \
  enable_sensor_gimbal:=true \
  sensor_gimbal_enable_gimbal:=true \
  sensor_gimbal_enable_thermal:=true \
  enable_charge:=true \
  enable_sensors:=true \
  enable_alarm:=true \
  enable_gas:=true \
  enable_mqtt:=true
```

Start only sensors:

```bash
ros2 launch inspection_bringup sensors.launch.py
```

Start only the platform bridge:

```bash
ros2 launch inspection_bringup platform_bridge.launch.py
```

## Debug With Tmux

For field debugging, start enabled modules from the same `config/system.yaml`
in separate tmux windows:

```bash
src/inspection_bringup/scripts/start_inspection_tmux.sh
```

Each enabled module gets its own window, for example `task_hub`, `gimbal`,
`charge`, `alarm`, `gas`, `thermal`, and `mqtt`. The script reuses the existing
launch files, so the normal launch-based startup remains unchanged.

Useful options:

```bash
src/inspection_bringup/scripts/start_inspection_tmux.sh --dry-run
src/inspection_bringup/scripts/start_inspection_tmux.sh --no-attach
src/inspection_bringup/scripts/start_inspection_tmux.sh --kill-existing
src/inspection_bringup/scripts/start_inspection_tmux.sh --session inspection_debug
```

Attach later with:

```bash
tmux attach -t inspection
```

## Systemd Services

`inspection_bringup` can install and manage three endpoint system services:

```text
inspection-navigation.service -> navigation.launch.py
inspection-hardware.service   -> acoustic, light, and gas pump hardware processes
inspection-system.service     -> inspection_system.launch.py
```

The hardware service runs as root and replaces the package-specific
`acoustic_monitor_agent.service`, `light_manager_agent.service`, and
`gas_monitor_pump_agent.service` units. Installing the bringup services stops
and disables those legacy units to prevent hardware and Unix socket conflicts.

The default service configuration is `config/services.yaml` and targets the
endpoint account and workspace:

```yaml
service:
  user: cat
  workspace_root: /home/cat/Workspace/task_ws
  library_paths:
    - /home/cat/Workspace/driver_ws/third_party/install/lib
    - /home/cat/Workspace/driver_ws/third_party/install/lib64
```

`library_paths` are prepended to the `LD_LIBRARY_PATH` produced by the ROS and
workspace setup files. Do not set `LD_LIBRARY_PATH` in `service.environment`,
because that would replace the ROS library search paths.

The navigation wrapper sources the driver, task, and algor overlays in that
order. Its `library_paths` are prepended to the `LD_LIBRARY_PATH` produced by
those setup files so local GTSAM and Livox-SDK2 libraries installed under
`driver_ws/third_party/install` are available to systemd as well.

Install the services after the workspace has been built on the endpoint:

```bash
src/inspection_bringup/scripts/manage_inspection_services.sh install
src/inspection_bringup/scripts/manage_inspection_services.sh enable
src/inspection_bringup/scripts/manage_inspection_services.sh start
```

The management script first tries sudo with the default endpoint password from
`config/services.yaml`, which is `cat`. If that fails, it falls back to the
normal sudo password prompt.

Useful commands:

```bash
src/inspection_bringup/scripts/manage_inspection_services.sh status
src/inspection_bringup/scripts/manage_inspection_services.sh install hardware
src/inspection_bringup/scripts/manage_inspection_services.sh uninstall hardware
src/inspection_bringup/scripts/manage_inspection_services.sh restart navigation
src/inspection_bringup/scripts/manage_inspection_services.sh restart hardware
src/inspection_bringup/scripts/manage_inspection_services.sh restart system
src/inspection_bringup/scripts/manage_inspection_services.sh enable hardware
src/inspection_bringup/scripts/manage_inspection_services.sh disable hardware
src/inspection_bringup/scripts/manage_inspection_services.sh logs navigation
src/inspection_bringup/scripts/manage_inspection_services.sh logs hardware
src/inspection_bringup/scripts/manage_inspection_services.sh logs system
src/inspection_bringup/scripts/manage_inspection_services.sh uninstall
```

## Extension Pattern

When a new sensor module is added, add its package dependency to `package.xml`
and include its launch file from `launch/sensors.launch.py` with an `enable_*`
launch argument.
