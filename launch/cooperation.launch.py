"""Run only cooperation, selected exclusively by the device configuration."""

import os
import sys

from launch import LaunchDescription
from launch.actions import DeclareLaunchArgument, OpaqueFunction
from launch.substitutions import LaunchConfiguration

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "scripts")))
from bringup_config import load_config
from cooperation import launch_actions


def launch_setup(context):
    config = load_config("system", LaunchConfiguration("system_config_path").perform(context))
    return launch_actions(config)


def generate_launch_description():
    return LaunchDescription([
        DeclareLaunchArgument(
            "system_config_path", default_value="",
            description="Standalone system YAML; empty uses the device configuration.",
        ),
        OpaqueFunction(function=launch_setup),
    ])
