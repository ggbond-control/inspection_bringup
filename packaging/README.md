# Control Debian Packages

Run commands from the inspection_bringup repository root. Packaging documentation
belongs here; `NAV_README.md` is not changed. Dockerfile:
[navigation/Dockerfile](navigation/Dockerfile).

Build natively on Ubuntu 24.04 / ROS Jazzy, on amd64 or arm64. Architecture is
detected automatically; run on an ARM board for ARM64 artifacts. Sources come
from remote repositories, not developer workspaces. No remote commits, tags or
branches are modified.

## Package Contents

| Debian package | Contents | Install prefix |
| --- | --- | --- |
| `control-driver` | `inspection_interfaces`, `robots_dog_msgs`, `control_input_msgs`, `gnss_driver`, `livox_ros_driver2`, `ndt_omp`, `nav_bridge`, embedded `uniubi` and `uniubi_motion_client`, GTSAM and Livox SDK | `/opt/inspection/driver` |
| `control-navigation` | `faster_lio`, `gridmapper`, `local_planner`, `multi_map_nav`, `smac_straight_planner` | `/opt/inspection/navigation` |

Driver explicitly enables X30, D1 Max, Cyvet, TANK and R750 bridge executables
on both architectures. An unavailable SDK/backend stops the build instead of
silently omitting that model. Cyvet interfaces/client come from the selected
nav_bridge revision. Runtime model selection remains bringup's responsibility;
packaging does not read `robot.yaml`.

`inspection_bringup`, device-specific configuration, maps and system services
are excluded. Generic launch/config resources and ROS package indexes remain
available. Driver headers, static libraries and CMake exports are retained so
navigation can compile against the exact distributed driver deb. The two
prefixes have no shared file ownership.

## Prerequisites

Install Docker, Git, Python 3 and dpkg on the host. `docker version` must show
a working server accessible without sudo. Docker supplies compiler/ROS build
dependencies. Allow sufficient disk space and start with `-j1` on an 8 GiB board.

Each component source build prompts for a read-only GitHub token in an interactive
terminal without echo. It is passed only through Git subprocess environments,
not stored in logs, Git configuration, manifests, images or debs. An `all` source
build prompts once per component. `--package-only` neither reads credentials nor
contacts source repositories.

## Commands

Build both components in order (`--component all` is the default). The generated
driver deb automatically becomes navigation's build underlay:

```bash
bash scripts/package_navigation.sh --version 0.3.5 --tag v0.3.5 -j1
```

Build driver only, including every bridge backend:

```bash
bash scripts/package_navigation.sh --component driver \
  --version 0.3.5 --tag v0.3.5 -j2
```

Build navigation alone against an existing driver deb. Driver sources are not
fetched or compiled, and the host need not have that deb installed:

```bash
bash scripts/package_navigation.sh --component navigation \
  --version 0.4.2 --tag v0.4.2 \
  --driver-deb ~/.cache/inspection-navigation-deb/amd64/driver/dist/control-driver_0.3.5_amd64.deb \
  --driver-min-version 0.3.5 --driver-max-version 0.4.0 -j2
```

Give the two components independent versions and source tags:

```bash
bash scripts/package_navigation.sh --component all \
  --driver-version 0.3.5 --driver-tag v0.3.5 \
  --navigation-version 0.4.2 --navigation-tag v0.4.2 -j1
```

After an initial complete component build, rebuild selected ROS packages and
their consumers within that component:

```bash
bash scripts/package_navigation.sh --component navigation \
  --version 0.4.2-1 --tag v0.4.2 --select local_planner \
  --driver-deb ~/.cache/inspection-navigation-deb/amd64/driver/dist/control-driver_0.3.5_amd64.deb -j2

bash scripts/package_navigation.sh --component driver \
  --version 0.3.5-1 --tag v0.3.5 --select nav_bridge -j2
```

Repackage a successful build without fetching or compiling:

```bash
bash scripts/package_navigation.sh --component navigation \
  --version 0.4.2-2 --package-only
```

## Inputs

| Option | Meaning |
| --- | --- |
| `--component driver\|navigation\|all` | Components to build/package; default `all` |
| `--version` | Default Debian version for requested components |
| `--tag` | Default organization source tag, e.g. `v0.3.5` |
| `--driver-version`, `--navigation-version` | Override the corresponding Debian version |
| `--driver-tag`, `--navigation-tag` | Override the corresponding source tag |
| `--driver-deb` | Required for navigation-only source builds; must match host architecture |
| `--driver-min-version` | Inclusive lower bound; default build driver version |
| `--driver-max-version` | Exclusive upper bound; default next major, or next minor for `0.x` |
| `--work-dir` | Cache root; component subdirectories are appended automatically |
| `-j`, `--jobs` | Compiler parallelism; default `1` |
| `--select PKG ...` | Rebuild selected packages and consumers; requires complete component cache |
| `--package-only` | Repackage complete cache; incompatible with tags and `--select` |
| `--rebuild-image` | Rerun Docker build, allowing Docker layer caching |

`--select` checks all source repositories within the chosen component. Changes
outside its consumer closure are rejected. In `all` mode a selection confined to
navigation repackages the cached driver; selecting driver packages also rebuilds
all navigation consumers. To avoid publishing a driver release at all, use
`--component navigation` and `--driver-deb`.

## Versions and Dependencies

Debian versions are independent of source tags and independent between components.
Navigation `0.4.2` can depend on driver `>= 0.3.5` and `<< 0.4.0`, for example.
The build driver must fall inside the requested range. Defaults are a convenience
policy, not an ABI test: maintainers must update bounds and rebuild consumers
when interfaces or native-library ABI change.

The actual build driver version/SHA-256, build identity, dependency bounds, source revisions,
Docker image ID and recipe hash are recorded in the navigation manifest. Changing
the driver build identity requires building navigation without `--select`; navigation
object files are then rebuilt. `--package-only` preserves the previous build
driver and compatibility bounds. Repackaging unchanged driver inputs does not
invalidate navigation objects merely because the deb archive checksum changed.

For organization repositories, select the exact requested stable tag first,
otherwise the highest stable tag not greater than it. Compare numerically.
Only repositories with no tags fall back to their latest advertised default
branch. If tags exist but none qualify, fail instead of selecting a newer version.
Livox SDK follows this policy; upstream GTSAM stays pinned to `4.2.0`.
`control_input_msgs` maps to repository `control_intput_msgs`.

## Cache and Artifacts

Default layout (replace `amd64` with `arm64` on the board):

```text
~/.cache/inspection-navigation-deb/amd64/
  driver/dist/control-driver_<version>_amd64.deb
  navigation/dist/control-navigation_<version>_amd64.deb
```

Each component retains `repositories/`, adjusted `sources/`, `build/`, `install/`
and `dist/`. Driver also retains native source/build caches; navigation retains
the extracted driver underlay. Each deb has a checksum and source manifest.
Installed manifests are at `<prefix>/share/control_release/manifest.json`.

New component caches do not reuse the old monolithic build directories.
Subsequent builds reuse unchanged objects and native-library commits. Failures
block `--package-only` until compilation succeeds. Stale files recorded by install
manifests are removed. Use a new cache when changing the builder image/toolchain.
The image is `inspection-control-builder:jazzy-<architecture>` and uses the
official multi-architecture `ros:jazzy-ros-base`. Docker's storage is separate;
inspect it with `docker info --format '{{.DockerRootDir}}'`.

## Installation and Checks

```bash
sudo apt install ./control-driver_0.3.5_amd64.deb ./control-navigation_0.4.2_amd64.deb
source /opt/ros/jazzy/setup.bash
source /opt/inspection/driver/local_setup.bash
source /opt/inspection/navigation/local_setup.bash
ros2 pkg executables nav_bridge
ros2 pkg executables local_planner
```

Both debs replace/conflict with the former `navigation` and `fhzn-navigation`
monolithic packages. Supply both new debs in one apt command when migrating.
Configure bringup separately to source driver then navigation instead of developer
overlays. No services are installed, started or restarted automatically.

On arm64, `control-navigation` also depends on `opencl-headers`,
`ocl-icd-opencl-dev`, `mesa-opencl-icd` and `clinfo`; apt installs them automatically.
GPU group permissions and `RUSTICL_ENABLE` remain deployment configuration.

Run isolated installation/runtime/removal checks on a matching-architecture host:

```bash
bash scripts/test_navigation_deb.sh DRIVER_DEB
bash scripts/test_navigation_deb.sh DRIVER_DEB NAVIGATION_DEB
bash scripts/test_navigation_deb.sh DRIVER_DEB NAVIGATION_DEB NEWER_NAVIGATION_DEB
```

Checks cover ROS interfaces, all bridge executable files, ELF architecture and
dynamic dependencies. Pair checks also cover navigation plugins/resources and
isolated follower startup. Real sensors, chassis movement and OpenCL hardware are
not exercised. Bridge executables are never started by these checks.

Release adaptations apply only to copied inputs: writable runtime data uses
`${ROS_HOME:-$HOME/.ros}/navigation/<package>`, GNSS launch files are installed,
and CPU flags target baseline x86-64 or ARMv8-A. Driver build exports are retained;
navigation remains a runtime payload. RPATHs reference the release prefixes and
system ROS, not developer workspaces.

`dpkg-shlibdeps` can warn about ROS-generated unversioned library names and the
custom staging layout. Warnings alone do not prove missing libraries; inspect
actual scan errors and run installation checks. Review redistribution licenses
and vendor SDK terms before distributing artifacts.
