# Control Debian Packages

Run commands from the inspection_bringup repository root. Packaging documentation
belongs here; `NAV_README.md` is not changed. Dockerfile:
[navigation/Dockerfile](navigation/Dockerfile).

Build natively on Ubuntu 24.04 / ROS Jazzy, on amd64 or arm64. Architecture is
detected automatically; run on an ARM board for ARM64 artifacts. Sources come
from remote repositories, not developer workspaces. No remote commits, tags or
branches are modified.

## Package Contents

The authoritative catalog is [packages.yaml](packages.yaml). Package ownership
and repository mappings are read from this file, not duplicated in Python.
Each `components` entry defines:

| Field | Meaning |
| --- | --- |
| `deb_name`, `prefix` | Debian name and isolated installation prefix |
| `packages` | ROS package name to organization Git repository name mapping |
| `embedded_packages` | Driver's UniUbi packages inside nav_bridge's vendored source |
| `native_libraries` | Native dependency URL and fixed ref (organization repos still follow the requested tag policy) |
| `runtime_dependencies` | Explicit apt dependencies, added to ELF-discovered dependencies |
| `architecture_dependencies` | Additional dependencies by architecture, such as arm64 OpenCL |
| `cmake_args` | Component-specific build options |
| `image`, `dockerfile` | Builder image template and Dockerfile relative to `packaging/` |
| `recipe_revision` | Explicit build-rule revision; bump when Python adaptations change |

For example, adding an ordinary task package from the organization requires an
entry under `components.task.packages`:

```yaml
packages:
  inspection_task_hub: inspection_task_hub
  another_task_package: its_repository_name
```

Add required apt runtime dependencies in the same component and build dependencies
in its Dockerfile. Rebuild that component without `--select` after changing its
catalog; object files remain available for incremental compilation. Package names
must match the fetched `package.xml`. Duplicate ownership, duplicate YAML keys,
invalid source paths and overlapping install prefixes are rejected before builds.
The same catalog is mounted read-only inside the container and the selected
component definition is recorded in release manifests.

Existing SDK relocation, backend verification and smoke checks remain explicit
Python adapters. Changing a special SDK layout, install prefix or Debian name
also requires reviewing those adapters and the installation check scripts.
The table below describes the shipped catalog:

| Debian package | Contents | Install prefix |
| --- | --- | --- |
| `control-driver` | `inspection_interfaces`, `robots_dog_msgs`, `control_input_msgs`, `gnss_driver`, `livox_ros_driver2`, `ndt_omp`, `nav_bridge`, embedded `uniubi` and `uniubi_motion_client`, GTSAM and Livox SDK | `/opt/inspection/driver` |
| `control-navigation` | `faster_lio`, `gridmapper`, `local_planner`, `multi_map_nav`, `smac_straight_planner` | `/opt/inspection/navigation` |
| `control-task` | `alarm_manager`, `light_manager`, `acoustic_monitor`, `gas_monitor`, `sensor_rs485`, `sensor_gimbal`, `inspection_charge_executor`, `inspection_platform_bridge`, `inspection_task_hub` | `/opt/inspection/task` |

Driver explicitly enables X30, D1 Max, Cyvet, TANK and R750 bridge executables
on both architectures. An unavailable SDK/backend stops the build instead of
silently omitting that model. Cyvet interfaces/client come from the selected
nav_bridge revision. Runtime model selection remains bringup's responsibility;
packaging does not read `robot.yaml`.

`inspection_bringup`, device-specific configuration, maps and system services
are excluded. Generic launch/config resources and ROS package indexes remain
available. Driver headers, static libraries and CMake exports are retained so
navigation can compile against the exact distributed driver deb. The two
prefixes have no shared file ownership. Task reuses the driver's interfaces;
`inspection_interfaces` and other driver packages are not duplicated. The
optional `mission_execution_agent` and `capability_mission_planner` are excluded.
Keep `system.modules.mission_execution_agent: false` if those packages are absent.

## Prerequisites

Install Docker, Git, Python 3, `python3-yaml` and dpkg on the host. `docker version` must show
a working server accessible without sudo. Docker supplies compiler/ROS build
dependencies. Allow sufficient disk space and start with `-j1` on an 8 GiB board.

Each invocation prompts once for a read-only GitHub token when its first source
fetch begins, in an interactive terminal without echo. All selected components
reuse it in process memory. It is passed only through Git subprocess environments,
not stored in logs, Git configuration, manifests, images or debs. The credential
environment is cleared at process completion or failure. `--package-only` neither reads credentials nor
contacts source repositories.

## Commands

Build all three components in driver, navigation, task order (`--component all`
is the default). The generated driver deb becomes both consumers' build underlay:

```bash
bash scripts/package.sh --version 0.3.5 --tag v0.3.5 -j1
```

Build driver only, including every bridge backend:

```bash
bash scripts/package.sh --component driver \
  --version 0.3.5 --tag v0.3.5 -j2
```

Build navigation alone against an existing driver deb. Driver sources are not
fetched or compiled, and the host need not have that deb installed:

```bash
bash scripts/package.sh --component navigation \
  --version 0.4.2 --tag v0.4.2 \
  --driver-deb ~/.cache/inspection-navigation-deb/amd64/driver/dist/control-driver_0.3.5_amd64.deb \
  --driver-min-version 0.3.5 --driver-max-version 0.4.0 -j2
```

Give the three components independent versions and source tags:

```bash
bash scripts/package.sh --component all \
  --driver-version 0.3.5 --driver-tag v0.3.5 \
  --navigation-version 0.4.2 --navigation-tag v0.4.2 \
  --task-version 0.2.0 --task-tag v0.2.0 -j1
```

Build task independently, without fetching or compiling driver/navigation:

```bash
bash scripts/package.sh --component task --version 0.3.11 --tag v0.3.11 \
  --driver-deb ~/.cache/inspection-navigation-deb/amd64/driver/dist/control-driver_0.3.11_amd64.deb \
  --driver-min-version 0.3.11 --driver-max-version 0.4.0 -j2
```

For later task-only updates, add `--select inspection_task_hub` (or another task
package). Use `--component task --version 0.3.11-1 --package-only` to repackage a
successful task cache without source access or compilation.

After an initial complete component build, rebuild selected ROS packages and
their consumers within that component:

```bash
bash scripts/package.sh --component navigation \
  --version 0.4.2-1 --tag v0.4.2 --select local_planner \
  --driver-deb ~/.cache/inspection-navigation-deb/amd64/driver/dist/control-driver_0.3.5_amd64.deb -j2

bash scripts/package.sh --component driver \
  --version 0.3.5-1 --tag v0.3.5 --select nav_bridge -j2
```

Repackage a successful build without fetching or compiling:

```bash
bash scripts/package.sh --component navigation \
  --version 0.4.2-2 --package-only
```

## Inputs

| Option | Meaning |
| --- | --- |
| `--component driver\|navigation\|task\|all` | Components to build/package; default `all` |
| `--version` | Default Debian version for requested components |
| `--tag` | Default organization source tag, e.g. `v0.3.5` |
| `--driver-version`, `--navigation-version` | Override the corresponding Debian version |
| `--driver-tag`, `--navigation-tag` | Override the corresponding source tag |
| `--task-version`, `--task-tag` | Task Debian version and source tag overrides |
| `--driver-deb` | Required for navigation-only/task-only source builds; must match host architecture |
| `--driver-min-version` | Inclusive lower bound; default build driver version |
| `--driver-max-version` | Exclusive upper bound; default next major, or next minor for `0.x` |
| `--work-dir` | Cache root; component subdirectories are appended automatically |
| `-j`, `--jobs` | Compiler parallelism; default `1` |
| `--select PKG ...` | Rebuild selected packages and consumers; requires complete component cache |
| `--package-only` | Repackage complete cache; incompatible with tags and `--select` |
| `--rebuild-image` | Rerun Docker build, allowing Docker layer caching |

`--select` checks all source repositories within the chosen component. Changes
outside its consumer closure are rejected. In `all` mode a selection confined to
one consumer repackages the other cached components; selecting driver packages
also rebuilds all navigation/task consumers. To avoid publishing unrelated
components, select `--component navigation` or `--component task` with `--driver-deb`.

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
  task/dist/control-task_<version>_amd64.deb
```

Each component retains `repositories/`, adjusted `sources/`, `build/`, `install/`
and `dist/`. Driver also retains native source/build caches; navigation retains
the extracted driver underlay. Each deb has a checksum and source manifest.
Installed manifests are at `<prefix>/share/control_release/manifest.json`.

New component caches do not reuse the old monolithic build directories.
Subsequent builds reuse unchanged objects and native-library commits. Failures
block `--package-only` until compilation succeeds. Stale files recorded by install
manifests are removed. Image filesystem layers, architecture and process environment
form a build-environment fingerprint. Metadata-only image ID changes reuse objects.
Actual environment changes automatically rebuild only the affected component's
ROS/native objects while retaining repositories, source snapshots and other
component caches. Unknown legacy environments are rebuilt conservatively; a
recorded object-environment stamp allows interrupted rebuilds to resume.
Driver/navigation use `inspection-control-builder:jazzy-<architecture>`; task uses
the independent `inspection-task-builder:jazzy-<architecture>` and
`navigation/Dockerfile.task`. Component rule revisions are recorded separately;
adding task does not change the driver/navigation image or delete their caches.
When introducing these revision identifiers into an older cache, omit `--select`
once; existing build objects remain reusable. Future changes must bump only the
affected component's `recipe_revision` in `packages.yaml`. Component configuration
is also hashed automatically; changing task configuration does not alter the
driver/navigation configuration hashes. The first build after this catalog
migration should omit `--select`; existing objects need not be deleted. The images use the
official multi-architecture `ros:jazzy-ros-base`. Docker's storage is separate;
inspect it with `docker info --format '{{.DockerRootDir}}'`.

The build image uses SYSU apt mirrors over HTTPS. Ubuntu noble, noble-updates,
noble-backports and noble-security retain the base image's suites/components:
amd64 uses `https://mirror.sysu.edu.cn/ubuntu/`, arm64 uses
`https://mirror.sysu.edu.cn/ubuntu-ports/`. ROS 2 uses
`https://mirror.sysu.edu.cn/ros2/ubuntu/`. Signing keys and signature verification
are preserved. This does not change host apt sources or the Docker registry.
ROS `deb-src` entries are disabled because this mirror does not publish the ROS
source index; project sources are still fetched from Git.
Existing images are reused until explicitly rebuilt. To apply the mirror change
using the existing cache root:

```bash
bash scripts/package.sh --version 0.3.5 --tag v0.3.5 \
  --rebuild-image -j1
```

If the filesystem or toolchain changes, objects are rebuilt automatically in the
same cache; there is no need to move repositories to a new `--work-dir`.

## Installation and Checks

```bash
sudo apt install ./control-driver_0.3.5_amd64.deb ./control-navigation_0.4.2_amd64.deb
source /opt/ros/jazzy/setup.bash
source /opt/inspection/driver/local_setup.bash
source /opt/inspection/navigation/local_setup.bash
source /opt/inspection/task/local_setup.bash  # If control-task is installed
ros2 pkg executables nav_bridge
ros2 pkg executables local_planner
```

The component debs replace/conflict with the former `navigation` and `fhzn-navigation`
monolithic packages. Supply both new debs in one apt command when migrating.
Configure bringup separately to source driver then navigation instead of developer
overlays. No services are installed, started or restarted automatically.

On arm64, `control-navigation` also depends on `opencl-headers`,
`ocl-icd-opencl-dev`, `mesa-opencl-icd` and `clinfo`; apt installs them automatically.
GPU group permissions and `RUSTICL_ENABLE` remain deployment configuration.

Task depends on a compatible driver range, not on control-navigation. It uses
system ROS messages for navigation/charging actions; navigation can be provided
separately. Task adds Python MQTT/HTTP/S3 libraries, ffmpeg, ALSA utilities and
ROS control/camera messages. RKNN and HCNetSDK follow `build_inspection.sh`'s
`AUTO` behavior: enable only when compatible SDK files exist in the selected
sensor_gimbal source. Enabled features are recorded in the artifact manifest.
Matching SDK runtime libraries are bundled; the acoustic SDK is also bundled for
the target architecture. User/device configuration and service activation remain
external. Packaged systemd templates are resources, not enabled system services.
The task image includes `nlohmann-json3-dev` for `find_package(nlohmann_json)`.
This is a header-only build dependency, so the runtime deb does not require its
development package. After updating the Dockerfile, use `--rebuild-image` to
install new build dependencies; an existing cached image is not rebuilt automatically.
Choose compatible task and driver source releases. For example, task_hub code
that accesses `SetGPSGoal.skip_yaw_alignment` cannot compile against a driver
interface that has replaced that field with `arrival_mode`. Dependency bounds
do not translate protocols; such mismatches stop the build and must be resolved
by selecting compatible releases or updating the business package separately.

Run isolated installation/runtime/removal checks on a matching-architecture host:

```bash
bash scripts/test_navigation_deb.sh DRIVER_DEB
bash scripts/test_navigation_deb.sh DRIVER_DEB NAVIGATION_DEB
bash scripts/test_navigation_deb.sh DRIVER_DEB NAVIGATION_DEB NEWER_NAVIGATION_DEB
bash scripts/test_navigation_deb.sh DRIVER_DEB TASK_DEB
bash scripts/test_navigation_deb.sh DRIVER_DEB NAVIGATION_DEB TASK_DEB
```

Arguments after DRIVER_DEB are installed and checked sequentially, allowing
component upgrades to be checked without replacing the other components.

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
