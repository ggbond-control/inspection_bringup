# Navigation Debian Release

Build a model-independent Ubuntu 24.04 / ROS Jazzy / amd64 runtime bundle.
`nav_bridge`, `inspection_bringup`, system services, maps and device configuration
are excluded. Sources are read from the three existing workspaces, never fetched
or updated by this tool. GTSAM 4.2.0 and Livox-SDK2 are cloned once into the cache;
their exact revisions are recorded in each artifact manifest.

Runtime packages: `inspection_interfaces`, `robots_dog_msgs`, `control_input_msgs`,
`livox_ros_driver2`, `gnss_driver`, `ndt_omp`, `faster_lio`, `gridmapper`,
`local_planner`, `multi_map_nav`, and `smac_straight_planner`. Interface definitions,
launch files, generic package defaults, RViz resources, OpenCL kernels and local
planner path assets remain available through the ROS package index. Host-specific
configuration from inspection_bringup is maintained separately.

Prerequisites on the amd64 host: Docker access, Git, Python 3, and dpkg. The cached
Docker image supplies the compiler and build dependencies. No sudo build or host
installation is required. Allow disk space for the image, native libraries and
ROS build cache. Defaults use one compiler job; increase only with sufficient RAM.

```bash
# First build; also builds the cached Docker image and native libraries.
bash scripts/package_navigation.sh --version 0.1.0 -j2

# Full incremental build: unchanged object files and native libraries are reused.
bash scripts/package_navigation.sh --version 0.1.1 -j2

# Rebuild only local_planner and any managed consumers, then package ALL components.
bash scripts/package_navigation.sh --version 0.1.2 --select local_planner -j2

# An interface change automatically selects its managed consumers as well.
bash scripts/package_navigation.sh --version 0.1.3 --select inspection_interfaces -j2

# Repackage the last complete build without compiling or syncing newer sources.
bash scripts/package_navigation.sh --version 0.1.4 --package-only

# Override source root or maintain an independent release cache.
bash scripts/package_navigation.sh --version 0.1.0 \
  --workspace-root ~/Workspace --work-dir ~/release/navigation-amd64

# Install in an isolated container, check ROS resources and remove it again.
bash scripts/test_navigation_deb.sh \
  ~/.cache/inspection-navigation-deb/amd64/dist/navigation_0.1.0_amd64.deb

# Optionally verify an upgrade in the same disposable container.
bash scripts/test_navigation_deb.sh \
  ~/.cache/inspection-navigation-deb/amd64/dist/navigation_0.1.0_amd64.deb \
  ~/.cache/inspection-navigation-deb/amd64/dist/navigation_0.1.2_amd64.deb
```

Artifacts are in `WORK_DIR/dist`: the deb, SHA-256 checksum and source manifest.
The manifest records Git revisions, dirty state, source content hashes, Docker
image ID and release recipe hash. The experiment includes local uncommitted
changes; it does not silently replace local sources with remote-board versions.

`--select` requires an initial complete build. Names come from `package.xml`.
Changes in unselected packages are rejected instead of mixing unnoticed source
versions. Build failures block `--package-only` until the build succeeds. File
timestamps of unchanged copied inputs remain stable. Removing the cache is not
required for ordinary source changes. Obsolete files recorded by CMake/setuptools
install manifests are removed after a successful rebuild. Use a NEW cache when changing the builder
image or toolchain; native library changes also require a new cache. The Docker
base tag is mutable; retain the recorded image to reproduce an experiment.

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
- ndt_omp uses baseline x86-64 compilation instead of explicit SSE4/native flags.
- gnss_driver installs its `driver.launch.py` as well as `*.launch.py` files.
- gnss_driver default calibration output uses the writable
  `${ROS_HOME:-$HOME/.ros}/navigation/gnss_driver` directory.
- Binaries and required runtime resources are copied, never symlink-installed;
  installed ELF RPATHs point only to the release prefix and system ROS.

Smoke checks do not exercise real sensors, chassis movement or OpenCL hardware.
This is an internal experimental bundle; review upstream redistribution licenses
and release metadata before distributing it outside the organization.
