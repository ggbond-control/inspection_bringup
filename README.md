# inspection_bringup

Unified bringup launch files for the inspection task system.

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
colcon build --packages-up-to <target> --symlink-install
```

It does not run a bare global `colcon build`. `nav_bridge` is intentionally not
listed in the managed dependency file because it is not owned by task hub
bringup.

Build parallelism is limited to 4 jobs by default. Override it when needed:

```bash
BUILD_JOBS=2 src/inspection_bringup/scripts/build_inspection.sh
```

Missing repositories are cloned from each remote's default branch, typically
`main` or `master`. Existing repositories are not pulled, checked out, or
branch-validated, so local manual branch changes are preserved.

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
  request_on_startup: true
  enable_push: true
  ffmpeg_bin: ffmpeg
  restart_interval_sec: 5.0
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
  enable_gimbal:=false \
  enable_charge:=true \
  enable_sensors:=true \
  enable_alarm:=true \
  enable_gas:=true \
  enable_thermal:=true \
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

`inspection_bringup` can install and manage two endpoint system services:

```text
inspection-navigation.service -> navigation.launch.py
inspection-system.service     -> inspection_system.launch.py
```

The default service configuration is `config/services.yaml` and targets the
endpoint account and workspace:

```yaml
service:
  user: cat
  workspace_root: /home/cat/task_ws
```

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
src/inspection_bringup/scripts/manage_inspection_services.sh restart navigation
src/inspection_bringup/scripts/manage_inspection_services.sh restart system
src/inspection_bringup/scripts/manage_inspection_services.sh logs navigation
src/inspection_bringup/scripts/manage_inspection_services.sh logs system
src/inspection_bringup/scripts/manage_inspection_services.sh uninstall
```

## Extension Pattern

When a new sensor module is added, add its package dependency to `package.xml`
and include its launch file from `launch/sensors.launch.py` with an `enable_*`
launch argument.
