# inspection_bringup

Unified bringup launch files for the inspection task system.

## Build

```bash
cd ~/task_ws
src/inspection_bringup/scripts/build_inspection.sh
source install/setup.bash
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

Useful variants:

```bash
src/inspection_bringup/scripts/build_inspection.sh --fetch-only
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
  sn: DEVICE_SN
  host: 127.0.0.1
  port: 1883
```

```bash
ros2 launch inspection_bringup inspection_system.launch.py
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

## Extension Pattern

When a new sensor module is added, add its package dependency to `package.xml`
and include its launch file from `launch/sensors.launch.py` with an `enable_*`
launch argument.
