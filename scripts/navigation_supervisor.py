#!/usr/bin/env python3
import argparse
import copy
import os
import signal
import subprocess
import sys
import threading
import time
import uuid

import yaml

import rclpy
from rcl_interfaces.msg import ParameterType, SetParametersResult
from rcl_interfaces.srv import SetParameters
from rclpy.callback_groups import ReentrantCallbackGroup
from rclpy.executors import MultiThreadedExecutor
from rclpy.node import Node


def as_bool(value):
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in ("1", "true", "yes", "on")


def split_list_text(value):
    if isinstance(value, list):
        return [str(item) for item in value]
    return [item.strip() for item in str(value).split(",") if item.strip()]


def load_yaml(path):
    expanded = os.path.expanduser(os.path.expandvars(path))
    with open(expanded, "r", encoding="utf-8") as stream:
        return yaml.safe_load(stream) or {}


def write_yaml_atomic(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp_path = f"{path}.tmp"
    with open(tmp_path, "w", encoding="utf-8") as stream:
        yaml.safe_dump(data, stream, sort_keys=False)
    os.replace(tmp_path, path)


def remove_if_exists(path):
    try:
        os.remove(path)
    except FileNotFoundError:
        pass


def flatten_leaves(value, prefix=""):
    leaves = {}
    if isinstance(value, dict):
        for key, child in value.items():
            child_prefix = f"{prefix}.{key}" if prefix else str(key)
            leaves.update(flatten_leaves(child, child_prefix))
    else:
        leaves[prefix] = value
    return leaves


def set_by_path(config, dotted_path, value):
    parts = dotted_path.split(".")
    cursor = config
    for part in parts[:-1]:
        cursor = cursor[part]
    cursor[parts[-1]] = value


def get_by_path(config, dotted_path, fallback=None):
    cursor = config
    for part in dotted_path.split("."):
        if not isinstance(cursor, dict) or part not in cursor:
            return fallback
        cursor = cursor[part]
    return cursor


def parameter_value_to_python(parameter_value):
    value_type = parameter_value.type
    if value_type == ParameterType.PARAMETER_BOOL:
        return parameter_value.bool_value
    if value_type == ParameterType.PARAMETER_INTEGER:
        return parameter_value.integer_value
    if value_type == ParameterType.PARAMETER_DOUBLE:
        return parameter_value.double_value
    if value_type == ParameterType.PARAMETER_STRING:
        return parameter_value.string_value
    if value_type == ParameterType.PARAMETER_BOOL_ARRAY:
        return list(parameter_value.bool_array_value)
    if value_type == ParameterType.PARAMETER_INTEGER_ARRAY:
        return list(parameter_value.integer_array_value)
    if value_type == ParameterType.PARAMETER_DOUBLE_ARRAY:
        return list(parameter_value.double_array_value)
    if value_type == ParameterType.PARAMETER_STRING_ARRAY:
        return list(parameter_value.string_array_value)
    raise ValueError("unsupported or unset parameter type")


def coerce_to_existing_type(value, existing):
    if isinstance(existing, bool):
        return as_bool(value)
    if isinstance(existing, int) and not isinstance(existing, bool):
        return int(value)
    if isinstance(existing, float):
        return float(value)
    if isinstance(existing, list):
        if isinstance(value, list):
            return value
        return split_list_text(value)
    if existing is None:
        return value
    return str(value)


def apply_start_mode(runtime_config):
    mode = str(get_by_path(runtime_config, "mode", "nav")).strip().lower()
    if mode not in ("nav", "manual"):
        raise ValueError(f"unsupported navigation mode: {mode}; expected nav or manual")

    set_by_path(runtime_config, "mode", mode)
    if mode == "manual":
        manual_modules = {
            "nav_bridge": True,
            "livox": False,
            "slam": False,
            "terrain": False,
            "local_planner": False,
            "global_planner": False,
        }
        for module_name, enabled in manual_modules.items():
            set_by_path(runtime_config, f"modules.{module_name}", enabled)
    return mode


class NavigationSupervisor(Node):
    def __init__(self, config_path, service_name, state_dir, result_timeout):
        super().__init__("navigation_supervisor")
        self.config_path = os.path.abspath(os.path.expanduser(os.path.expandvars(config_path)))
        self.state_dir = os.path.abspath(os.path.expanduser(os.path.expandvars(state_dir)))
        self.result_timeout = result_timeout
        self.base_config = load_yaml(self.config_path)
        self.allowed_paths = flatten_leaves(self.base_config)
        self.in_progress = False
        self.request_lock = threading.Lock()
        self.worker_process = None
        self.worker_run_id = None
        os.makedirs(self.state_dir, exist_ok=True)
        self.service = self.create_service(
            SetParameters,
            service_name,
            self.on_start_request,
            callback_group=ReentrantCallbackGroup(),
        )
        self.get_logger().info(f"waiting for navigation start service: {service_name}")
        self.get_logger().info(f"navigation supervisor state dir: {self.state_dir}")

    def on_start_request(self, request, response):
        with self.request_lock:
            if self.in_progress:
                response.results.append(self.result(False, "navigation startup already in progress"))
                return response
            self.in_progress = True

        try:
            runtime_config = copy.deepcopy(self.base_config)
            validation_results = []
            for parameter in request.parameters:
                try:
                    if parameter.name not in self.allowed_paths:
                        raise ValueError(f"unknown navigation parameter: {parameter.name}")
                    raw_value = parameter_value_to_python(parameter.value)
                    value = coerce_to_existing_type(raw_value, self.allowed_paths[parameter.name])
                    set_by_path(runtime_config, parameter.name, value)
                    validation_results.append(self.result(True, f"accepted {parameter.name}"))
                except Exception as exc:
                    validation_results.append(self.result(False, str(exc)))

            response.results.extend(validation_results)
            if any(not result.successful for result in validation_results):
                return response

            mode = apply_start_mode(runtime_config)
            response.results.append(self.result(True, f"resolved navigation mode: {mode}"))

            self.stop_worker()
            request_state_dir, request_run_id, result_path = self.start_worker(runtime_config)
            self.get_logger().info(
                f"navigation start request accepted; waiting for worker result: {request_run_id}"
            )
            result = self.wait_for_result(result_path, request_run_id)
            success = bool(result.get("success", False))
            reason = str(result.get("reason", "navigation startup result missing reason"))
            if not success:
                self.stop_worker()
            response.results.append(self.result(success, reason))
        except Exception as exc:
            reason = f"failed to start navigation worker: {exc}"
            self.get_logger().error(reason)
            response.results.append(self.result(False, reason))
        finally:
            # A finished request must never make the service one-shot. A later call
            # replaces the active worker with a fresh stack using its own config.
            with self.request_lock:
                self.in_progress = False
        return response

    def start_worker(self, runtime_config):
        request_run_id = uuid.uuid4().hex
        request_state_dir = os.path.join(self.state_dir, request_run_id)
        resolved_config_path = os.path.join(request_state_dir, "resolved.yaml")
        result_path = os.path.join(request_state_dir, "result.yaml")

        runtime_config.setdefault("bringup", {})["start_mode"] = "immediate"
        os.makedirs(request_state_dir, exist_ok=True)
        remove_if_exists(result_path)
        write_yaml_atomic(resolved_config_path, runtime_config)
        self.worker_process = subprocess.Popen(
            [
                "ros2",
                "launch",
                "inspection_bringup",
                "navigation.launch.py",
                f"navigate_config_path:={resolved_config_path}",
                f"navigation_state_dir:={request_state_dir}",
                f"navigation_run_id:={request_run_id}",
            ],
            start_new_session=True,
        )
        self.worker_run_id = request_run_id
        self.get_logger().info(
            f"started navigation worker pid={self.worker_process.pid} run_id={request_run_id}"
        )
        return request_state_dir, request_run_id, result_path

    def stop_worker(self):
        if self.worker_process is None:
            return
        if self.worker_process.poll() is None:
            self.get_logger().info(
                f"stopping active navigation worker pid={self.worker_process.pid} "
                f"run_id={self.worker_run_id}"
            )
            try:
                os.killpg(self.worker_process.pid, signal.SIGINT)
            except ProcessLookupError:
                pass
            try:
                self.worker_process.wait(timeout=10.0)
            except subprocess.TimeoutExpired:
                self.get_logger().warning("navigation worker did not stop after SIGINT; sending SIGTERM")
                try:
                    os.killpg(self.worker_process.pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
                try:
                    self.worker_process.wait(timeout=5.0)
                except subprocess.TimeoutExpired:
                    self.get_logger().error("navigation worker did not stop after SIGTERM; sending SIGKILL")
                    try:
                        os.killpg(self.worker_process.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                    self.worker_process.wait()
        self.worker_process = None
        self.worker_run_id = None

    def wait_for_result(self, result_path, run_id):
        deadline = None if self.result_timeout <= 0.0 else time.monotonic() + self.result_timeout
        while deadline is None or time.monotonic() < deadline:
            if os.path.exists(result_path):
                try:
                    result = load_yaml(result_path)
                except Exception as exc:
                    return {"success": False, "reason": f"failed to read launch result: {exc}"}
                if str(result.get("run_id", "")) != run_id:
                    time.sleep(0.2)
                    continue
                return result
            if self.worker_process is not None:
                returncode = self.worker_process.poll()
                if returncode is not None:
                    return {
                        "success": False,
                        "reason": (
                            "navigation worker exited before reporting startup result "
                            f"(exit_code={returncode})"
                        ),
                    }
            time.sleep(0.2)
        return {"success": False, "reason": f"navigation launch result timeout after {self.result_timeout:.1f}s"}

    @staticmethod
    def result(successful, reason):
        result = SetParametersResult()
        result.successful = bool(successful)
        result.reason = str(reason)
        return result


def run_node(args):
    rclpy.init()
    node = NavigationSupervisor(
        args.config,
        args.service_name,
        args.state_dir,
        args.result_timeout,
    )
    executor = MultiThreadedExecutor(num_threads=2)
    executor.add_node(node)
    try:
        executor.spin()
    except KeyboardInterrupt:
        pass
    finally:
        node.stop_worker()
        executor.shutdown()
        node.destroy_node()
        if rclpy.ok():
            rclpy.shutdown()
    return 0


def main():
    parser = argparse.ArgumentParser(description="Navigation launch gate supervisor.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    node_parser = subparsers.add_parser("node", help="Run the supervisor ROS node.")
    node_parser.add_argument("--config", required=True, help="navigate.yaml path")
    node_parser.add_argument("--service-name", default="/navigation_bringup/start")
    node_parser.add_argument("--state-dir", required=True)
    node_parser.add_argument(
        "--result-timeout",
        type=float,
        default=0.0,
        help="Timeout for final launch result. Use 0 or negative to wait forever.",
    )
    node_parser.set_defaults(func=run_node)

    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
