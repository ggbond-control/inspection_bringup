#!/usr/bin/env python3
import argparse
import copy
import os
import sys
import time

import yaml

import rclpy
from rcl_interfaces.msg import ParameterType, SetParametersResult
from rcl_interfaces.srv import SetParameters
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


def write_text_atomic(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp_path = f"{path}.tmp"
    with open(tmp_path, "w", encoding="utf-8") as stream:
        stream.write(text)
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
        set_by_path(runtime_config, "modules.local_planner", False)
    return mode


class NavigationSupervisor(Node):
    def __init__(self, config_path, service_name, state_dir, run_id, result_timeout):
        super().__init__("navigation_supervisor")
        self.config_path = os.path.abspath(os.path.expanduser(os.path.expandvars(config_path)))
        self.state_dir = os.path.abspath(os.path.expanduser(os.path.expandvars(state_dir)))
        self.run_id = str(run_id)
        self.result_timeout = result_timeout
        self.resolved_config_path = os.path.join(self.state_dir, "resolved.yaml")
        self.start_signal_path = os.path.join(self.state_dir, "start.yaml")
        self.result_path = os.path.join(self.state_dir, "result.yaml")
        self.base_config = load_yaml(self.config_path)
        self.allowed_paths = flatten_leaves(self.base_config)
        self.in_progress = False
        os.makedirs(self.state_dir, exist_ok=True)
        remove_if_exists(self.start_signal_path)
        remove_if_exists(self.resolved_config_path)
        remove_if_exists(self.result_path)
        self.service = self.create_service(SetParameters, service_name, self.on_start_request)
        self.get_logger().info(f"waiting for navigation start service: {service_name}")
        self.get_logger().info(f"navigation supervisor state dir: {self.state_dir}")
        self.get_logger().info(f"navigation supervisor run id: {self.run_id}")

    def on_start_request(self, request, response):
        if self.in_progress:
            response.results.append(self.result(False, "navigation startup already in progress"))
            return response

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

        try:
            mode = apply_start_mode(runtime_config)
        except Exception as exc:
            response.results.append(self.result(False, str(exc)))
            return response
        response.results.append(self.result(True, f"resolved navigation mode: {mode}"))

        self.in_progress = True
        remove_if_exists(self.result_path)
        write_yaml_atomic(self.resolved_config_path, runtime_config)
        write_yaml_atomic(
            self.start_signal_path,
            {
                "run_id": self.run_id,
                "config_path": self.resolved_config_path,
                "requested_at": time.time(),
            },
        )
        self.get_logger().info("navigation start request accepted; waiting for launch result")

        result = self.wait_for_result()
        success = bool(result.get("success", False))
        if not success:
            self.in_progress = False
        response.results.append(
            self.result(
                success,
                str(result.get("reason", "navigation startup result missing reason")),
            )
        )
        return response

    def wait_for_result(self):
        deadline = None if self.result_timeout <= 0.0 else time.monotonic() + self.result_timeout
        while deadline is None or time.monotonic() < deadline:
            if os.path.exists(self.result_path):
                try:
                    result = load_yaml(self.result_path)
                except Exception as exc:
                    return {"success": False, "reason": f"failed to read launch result: {exc}"}
                if str(result.get("run_id", "")) != self.run_id:
                    time.sleep(0.2)
                    continue
                return result
            time.sleep(0.2)
        return {"success": False, "reason": f"navigation launch result timeout after {self.result_timeout:.1f}s"}

    @staticmethod
    def result(successful, reason):
        result = SetParametersResult()
        result.successful = bool(successful)
        result.reason = str(reason)
        return result


def wait_for_start(state_dir, run_id, timeout):
    start_signal_path = os.path.join(state_dir, "start.yaml")
    resolved_config_path = os.path.join(state_dir, "resolved.yaml")
    deadline = None if timeout <= 0.0 else time.monotonic() + timeout
    print(f"[navigation_supervisor] waiting for start signal: {start_signal_path}", flush=True)
    while deadline is None or time.monotonic() < deadline:
        if os.path.exists(start_signal_path) and os.path.exists(resolved_config_path):
            try:
                start_signal = load_yaml(start_signal_path)
            except Exception as exc:
                print(
                    f"[navigation_supervisor] failed to read start signal: {exc}",
                    file=sys.stderr,
                    flush=True,
                )
                time.sleep(0.2)
                continue
            if str(start_signal.get("run_id", "")) == str(run_id):
                print(f"[navigation_supervisor] start accepted: {resolved_config_path}", flush=True)
                return 0
        time.sleep(0.2)
    print(
        f"[navigation_supervisor] start wait timeout after {timeout:.1f}s",
        file=sys.stderr,
        flush=True,
    )
    return 1


def report_result(state_dir, run_id, success, reason):
    result_path = os.path.join(state_dir, "result.yaml")
    write_yaml_atomic(
        result_path,
        {
            "run_id": str(run_id),
            "success": bool(success),
            "reason": reason,
            "reported_at": time.time(),
        },
    )
    print(f"[navigation_supervisor] reported result: success={bool(success)} reason={reason}", flush=True)
    return 0


def run_node(args):
    rclpy.init()
    node = NavigationSupervisor(
        args.config,
        args.service_name,
        args.state_dir,
        args.run_id,
        args.result_timeout,
    )
    try:
        rclpy.spin(node)
    except KeyboardInterrupt:
        pass
    finally:
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
    node_parser.add_argument("--run-id", required=True)
    node_parser.add_argument(
        "--result-timeout",
        type=float,
        default=0.0,
        help="Timeout for final launch result. Use 0 or negative to wait forever.",
    )
    node_parser.set_defaults(func=run_node)

    wait_parser = subparsers.add_parser("wait-start", help="Wait until the start service is accepted.")
    wait_parser.add_argument("--state-dir", required=True)
    wait_parser.add_argument("--run-id", required=True)
    wait_parser.add_argument("--timeout", type=float, default=0.0)
    wait_parser.set_defaults(func=lambda args: wait_for_start(args.state_dir, args.run_id, args.timeout))

    result_parser = subparsers.add_parser("report-result", help="Report final launch result to supervisor.")
    result_parser.add_argument("--state-dir", required=True)
    result_parser.add_argument("--run-id", required=True)
    result_parser.add_argument("--success", action="store_true")
    result_parser.add_argument("--reason", required=True)
    result_parser.set_defaults(
        func=lambda args: report_result(args.state_dir, args.run_id, args.success, args.reason)
    )

    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
