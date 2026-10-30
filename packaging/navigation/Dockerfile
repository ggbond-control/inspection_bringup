FROM osrf/ros:jazzy-desktop-full

ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential cmake git pkg-config patchelf dpkg-dev python3-colcon-common-extensions \
    python3-yaml python3-numpy python3-serial python3-requests python3-scipy \
    libgoogle-glog-dev libgflags-dev libyaml-cpp-dev libeigen3-dev libpcl-dev \
    libopencv-dev libtbb-dev libgeographiclib-dev libzstd-dev libapr1-dev \
    opencl-headers ocl-icd-opencl-dev \
    ros-jazzy-navigation2 ros-jazzy-grid-map ros-jazzy-pcl-ros \
    ros-jazzy-tf-transformations ros-jazzy-rmw-cyclonedds-cpp \
    && rm -rf /var/lib/apt/lists/*

ENV ROS_DISTRO=jazzy
ENTRYPOINT []
