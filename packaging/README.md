# Navigation Debian Release

Run the commands below from the inspection_bringup repository root. Packaging
documentation is maintained here; `NAV_README.md` covers the navigation workflows.
The Dockerfile remains in [navigation/Dockerfile](navigation/Dockerfile).

Build a model-independent Ubuntu 24.04 / ROS Jazzy runtime bundle natively on
either amd64 or arm64. The script detects the host architecture; run it on the
ARM board to produce an arm64 deb. It does not cross-compile or copy sources
from another development machine.
`nav_bridge`, `inspection_bringup`, system services, maps and device configuration
are excluded. ROS sources are fetched from the `ggbond-control` GitHub organization
into a private local cache; developer workspaces are not read. Livox-SDK2 uses the
same release selection policy. GTSAM remains pinned to upstream tag `4.2.0`.
The tool only reads remote repositories; it never pushes commits, tags or branches.

Runtime packages: `inspection_interfaces`, `robots_dog_msgs`, `control_input_msgs`,
`livox_ros_driver2`, `gnss_driver`, `ndt_omp`, `faster_lio`, `gridmapper`,
`local_planner`, `multi_map_nav`, and `smac_straight_planner`. Interface definitions,
launch files, generic package defaults, RViz resources, OpenCL kernels and local
planner path assets remain available through the ROS package index. Host-specific
configuration from inspection_bringup is maintained separately.

Prerequisites on either host: Docker access, Git, Python 3, dpkg, network access
and a GitHub token with read access to the organization's repositories. Each source
build prompts for the token in an interactive terminal without echo. The token is
passed only to Git subprocesses through their environment; it is not stored in
Git configuration, command arguments, logs, manifests, the Docker image or the deb.
`--package-only` does not request a token or access repositories. The cached
Docker image supplies the compiler and build dependencies. No sudo build or host
installation is required. Allow disk space for the image, native libraries and
ROS build cache. Defaults use one compiler job; increase only with sufficient RAM.
The host does not need ROS or navigation compiler dependencies installed; those
are supplied by the build image. `docker version` must show a working server and
the current user must be able to run Docker without sudo.

## ARM64 Board

Install/configure Docker for the board first, then clone or update this repository
using Git. From its root, run the same packaging entrypoint:

```bash
dpkg --print-architecture   # Expected: arm64
docker version
bash scripts/package_navigation.sh --version 0.3.5 --tag v0.3.5 -j1
```

Enter a read-only GitHub token when prompted. The script fetches organization
sources directly, builds the ARM64 Docker image if missing, then compiles GTSAM,
Livox SDK and the ROS packages on the board. For an 8 GiB board, start with `-j1`.
No source archives from another machine, offline cache seeding or board-specific
sandbox backend are required.

The default result is:

```text
~/.cache/inspection-navigation-deb/arm64/dist/navigation_0.3.5_arm64.deb
```

Incremental updates work the same way on both architectures:

```bash
bash scripts/package_navigation.sh --version 0.3.5-1 --tag v0.3.5 \
  --select local_planner -j1
bash scripts/package_navigation.sh --version 0.3.5-2 --package-only
bash scripts/test_navigation_deb.sh \
  ~/.cache/inspection-navigation-deb/arm64/dist/navigation_0.3.5_arm64.deb
```

The verification script must run on a host matching the deb architecture. It
checks each ELF file's machine type as well as installation and runtime loading.

## Build Options

```bash
# First build; also builds the cached Docker image and native libraries.
bash scripts/package_navigation.sh --version 0.3.5 --tag v0.3.5 -j2

# Full incremental build: unchanged object files and native libraries are reused.
bash scripts/package_navigation.sh --version 0.3.6 --tag v0.3.6 -j2

# Rebuild only local_planner and any managed consumers, then package ALL components.
bash scripts/package_navigation.sh --version 0.3.6-1 --tag v0.3.6 --select local_planner -j2

# An interface change automatically selects its managed consumers as well.
bash scripts/package_navigation.sh --version 0.3.6-2 --tag v0.3.6 --select inspection_interfaces -j2

# Repackage the last complete build without compiling or syncing newer sources.
bash scripts/package_navigation.sh --version 0.3.6-3 --package-only

# Maintain an independent release cache.
bash scripts/package_navigation.sh --version 0.3.5 --tag v0.3.5 \
  --work-dir ~/release/navigation-amd64

# Install in an isolated container, check ROS resources and remove it again.
bash scripts/test_navigation_deb.sh \
  ~/.cache/inspection-navigation-deb/amd64/dist/navigation_0.1.0_amd64.deb

# Optionally verify an upgrade in the same disposable container.
bash scripts/test_navigation_deb.sh \
  ~/.cache/inspection-navigation-deb/amd64/dist/navigation_0.1.0_amd64.deb \
  ~/.cache/inspection-navigation-deb/amd64/dist/navigation_0.1.2_amd64.deb
```

Artifacts are in `WORK_DIR/dist`: the deb, SHA-256 checksum and source manifest.
The manifest records the requested tag, each repository URL, actual tag or default
branch, selection reason, commit, source content hash, Docker image ID and recipe
hash. Local uncommitted changes and remote-board checkouts are not included.

For each organization repository independently, `--tag v0.3.5` selects the exact
tag first. Otherwise it selects the highest stable `vMAJOR.MINOR.PATCH` (or
`MAJOR.MINOR.PATCH`) tag not greater than the requested version, such as `v0.3.4`.
Versions are compared numerically. Prerelease and other non-version tags are not
fallback candidates. Only a repository with no tags at all falls back to the
latest commit of its advertised default branch, which need not be named `main`.
If tags exist but none qualify, the build stops rather than selecting a newer
release or a branch. Every resolved revision is printed before compilation.

`--version` identifies the resulting Debian package; `--tag` selects source inputs.
Different repositories may resolve to different tags. Default branches can move
between runs; use the recorded commits to identify the exact inputs of an artifact.
The repository for ROS package `control_input_msgs` is named `control_intput_msgs`;
the package-to-repository mapping is explicit in the script.

`--select` requires an initial complete build. Names come from `package.xml`.
Changes in unselected packages are rejected instead of mixing unnoticed source
versions. Build failures block `--package-only` until the build succeeds. File
timestamps of unchanged copied inputs remain stable. Removing the cache is not
required for ordinary source changes. Obsolete files recorded by CMake/setuptools
install manifests are removed after a successful rebuild. Native library caches
are keyed by their source commit. If Livox-SDK2 changes, run without `--select` so
its library and all managed consumers can be updated. Use a NEW cache when changing
the builder image or toolchain, or making incompatible native dependency changes.
When migrating from the previous local-source script, first run without `--select`
to apply the updated release recipe. The Docker base tag is mutable; retain the
recorded image to reproduce an experiment.

Docker uses `inspection-navigation-builder:jazzy-<architecture>`, building it if
absent and reusing it otherwise. The Dockerfile uses the official multi-architecture
`ros:jazzy-ros-base` image and explicitly selects `linux/amd64` or `linux/arm64`.
Docker manages image/layer storage
(usually `/var/lib/docker`; check `docker info --format '{{.DockerRootDir}}'`).
The default persistent release cache is `~/.cache/inspection-navigation-deb/<architecture>`:
`repositories/` holds ROS Git checkouts, `sources/` holds adjusted build inputs,
`native-src/` and `native-build/` hold GTSAM/Livox sources and objects, `build/`
holds ROS objects, `install/` holds the merged prefix, and `dist/` holds artifacts.
Disposable containers are removed after each invocation; these caches remain.
amd64 and arm64 caches must not be shared, including with `--work-dir`; the script
rejects mismatched cache manifests. Compilation uses baseline x86-64 flags on
amd64 and `-march=armv8-a -mtune=generic` on arm64. Each native library is compiled
for that architecture, and the deb control metadata and filename match it.

The package is complete on every update, not a delta archive: Debian upgrades
replace the installed runtime while the build reuses object files. Applications
using changed interfaces must be rebuilt against that interface version. Restart
running consumers externally after upgrading; package installation does not
automatically stop or start navigation.

## Installation

```bash
sudo apt install ./navigation_0.1.0_amd64.deb
source /opt/ros/jazzy/setup.bash
source /opt/inspection/navigation/local_setup.bash
ros2 pkg executables local_planner
ros2 launch faster_lio slam.launch.py --show-args
```

System ROS, Nav2, PCL, OpenCV and other public libraries are apt dependencies.
Private GTSAM and Livox runtime libraries are installed under the private prefix.
RViz is recommended, not required by the package control file. Kernel/GPU drivers,
device access, maps, network settings and actual sensor configuration remain
deployment responsibilities. Configure inspection_bringup separately to source
the installed prefix instead of the development driver/algorithm overlays.

Release-only adaptations are applied to copied inputs, not developer sources:

- faster_lio/gridmapper legacy `ROOT_DIR` output paths use
  `${ROS_HOME:-$HOME/.ros}/navigation/<package>`.
- ndt_omp uses baseline x86-64 flags in its x86 branch instead of explicit SSE4;
  its ARM branch is retained and native CPU optimization is disabled.
- gnss_driver installs its `driver.launch.py` as well as `*.launch.py` files.
- gnss_driver default calibration output uses the writable
  `${ROS_HOME:-$HOME/.ros}/navigation/gnss_driver` directory.
- Binaries and required runtime resources are copied, never symlink-installed;
  installed ELF RPATHs point only to the release prefix and system ROS.

## Dependency Scan Warnings

`dpkg-shlibdeps` may report `can't extract name and version from library name`
for ROS-generated `__rosidl_typesupport_*.so` and Python interface libraries,
whose names do not follow the usual versioned Debian library naming scheme.
It may also report `binaries to analyze should already be installed in their
package's directory` because the script uses a custom staging directory instead
of the conventional `debian/<package>/` layout. These warnings alone do not mean
that libraries are missing. Validate each new artifact with
`scripts/test_navigation_deb.sh`; missing dependencies and actual scan failures
must still be investigated rather than hiding all warnings.

Smoke checks do not exercise real sensors, chassis movement or OpenCL hardware.
This is an internal experimental bundle; review upstream redistribution licenses
and release metadata before distributing it outside the organization.
