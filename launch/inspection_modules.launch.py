"""Existing single-device modules without starting the cooperation stack."""

from pathlib import Path

from launch.launch_description_sources.python_launch_file_utilities import (
    load_python_launch_file_as_module,
)


def generate_launch_description():
    system = load_python_launch_file_as_module(
        Path(__file__).with_name("inspection_system.launch.py")
    )
    return system.generate_launch_description(include_cooperation=False)
