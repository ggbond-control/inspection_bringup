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


STOPPED = "stopped"
MANUAL = "manual"
NAV = "nav"
ADD = "add"
MANUAL_BRIDGE_ONLY = "bridge_only"
MANUAL_LOCALIZED = "localized"
MANUAL_WITHOUT_MAP_MODULES = ("nav_bridge",)
MANUAL_MODULES = (
    "nav_bridge",
    "livox",
    "slam",
)
NAV_EXTENSION_MODULES = (
    "terrain",
    "local_planner",
    "global_planner",
)
SUPPORTED_MODULES = MANUAL_MODULES + NAV_EXTENSION_MODULES


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
        return value if isinstance(value, list) else split_list_text(value)
    if existing is None:
        return value
    return str(value)


class NavigationSupervisor(Node):
    def __init__(self, config_path, service_name, state_dir, result_timeout):
        super().__init__("navigation_supervisor")
        self.config_path = os.path.abspath(os.path.expanduser(os.path.expandvars(config_path)))
        self.state_dir = os.path.abspath(os.path.expanduser(os.path.expandvars(state_dir)))
        self.result_timeout = result_timeout
        self.base_config = load_yaml(self.config_path)
        self.allowed_paths = flatten_leaves(self.base_config)
        self.module_names = SUPPORTED_MODULES
        self.validate_mode_sequences(self.base_config)
        self.current_state = STOPPED
        self.manual_profile = None
        self.active_config = None
        self.map_context = {}
        self.in_progress = False
        self.busy_reason = ""
        self.request_lock = threading.Lock()
        self.state_lock = threading.Lock()
        self.workers = {"base": None, **{name: None for name in NAV_EXTENSION_MODULES}}
        os.makedirs(self.state_dir, exist_ok=True)
        self.service = self.create_service(
            SetParameters,
            service_name,
            self.on_start_request,
            callback_group=ReentrantCallbackGroup(),
        )
        self.monitor_timer = self.create_timer(1.0, self.monitor_workers)
        self.get_logger().info(f"waiting for navigation start service: {service_name}")
        self.get_logger().info(f"navigation supervisor state dir: {self.state_dir}")

    def on_start_request(self, request, response):
        with self.request_lock:
            if self.in_progress:
                response.results.append(self.result(False, self.busy_reason))
                return response
            self.in_progress = True
            self.busy_reason = "navigation startup already in progress"

        try:
            with self.state_lock:
                self.refresh_state()
                runtime_config, provided_paths, validation_results = self.resolve_request(request)
                response.results.extend(validation_results)
                if any(not result.successful for result in validation_results):
                    return response

                target_state = self.target_state(runtime_config)
                self.validate_mode_sequences(runtime_config)
                manual_profile = self.validate_map_requirements(
                    runtime_config, target_state, provided_paths
                )
                self.normalize_map_config(runtime_config, provided_paths)

                transition = self.select_transition(runtime_config, target_state, provided_paths)
                self.get_logger().info(
                    f"navigation request: current={self.current_state} target={target_state} transition={transition}"
                )
                success, reason = self.execute_transition(
                    runtime_config, target_state, transition, manual_profile
                )
                response.results.append(self.result(success, reason))
        except Exception as exc:
            reason = str(exc)
            self.get_logger().error(f"navigation request failed: {reason}")
            response.results.append(self.result(False, reason))
        finally:
            with self.request_lock:
                self.in_progress = False
                self.busy_reason = ""
        return response

    def resolve_request(self, request):
        config = copy.deepcopy(self.active_config or self.base_config)
        if self.current_state == STOPPED:
            self.apply_map_context(config)

        provided_paths = set()
        results = []
        for parameter in request.parameters:
            try:
                if parameter.name not in self.allowed_paths:
                    raise ValueError(f"unknown navigation parameter: {parameter.name}")
                raw_value = parameter_value_to_python(parameter.value)
                value = coerce_to_existing_type(raw_value, self.allowed_paths[parameter.name])
                set_by_path(config, parameter.name, value)
                provided_paths.add(parameter.name)
                results.append(self.result(True, f"accepted {parameter.name}"))
            except Exception as exc:
                results.append(self.result(False, str(exc)))
        return config, provided_paths, results

    def target_state(self, config):
        mode = str(get_by_path(config, "mode", NAV)).strip().lower()
        modes = get_by_path(config, "bringup.modes", {})
        if not isinstance(modes, dict) or mode not in modes:
            available = ", ".join(sorted(modes)) if isinstance(modes, dict) else "none"
            raise ValueError(f"unsupported navigation mode: {mode}; configured modes: {available}")
        return mode

    def validate_map_requirements(self, config, target_state, provided_paths):
        has_initial = bool(str(self.map_context.get("global_planner.initial_map", "")).strip())
        prior_dir = str(get_by_path(config, "slam.prior_dir", "")).strip()
        manual_profile = self.manual_profile_for(target_state, prior_dir)

        if target_state == MANUAL and manual_profile == MANUAL_BRIDGE_ONLY:
            return manual_profile

        explicit_parameters = get_by_path(
            config, f"bringup.modes.{target_state}.required_parameters", []
        )
        if explicit_parameters:
            if not isinstance(explicit_parameters, list):
                raise ValueError(f"bringup.modes.{target_state}.required_parameters must be a list")
            missing = [str(path) for path in explicit_parameters if str(path) not in provided_paths]
            if missing:
                raise ValueError(f"{target_state} mode requires explicit " + ", ".join(missing))

        if self.current_state == STOPPED and target_state == NAV:
            if not has_initial and "global_planner.initial_map" not in provided_paths:
                raise ValueError(
                    "missing required map information: global_planner.initial_map must be provided because no cached initial map exists"
                )

        if self.current_state == MANUAL and target_state == NAV:
            missing = []
            if self.manual_profile == MANUAL_BRIDGE_ONLY and "slam.prior_dir" not in provided_paths:
                missing.append("slam.prior_dir")
            if "global_planner.initial_map" not in provided_paths:
                missing.append("global_planner.initial_map")
            if missing:
                raise ValueError(
                    "manual bridge-only to nav requires explicit " + ", ".join(missing)
                    if self.manual_profile == MANUAL_BRIDGE_ONLY
                    else "manual to nav requires explicit global_planner.initial_map; cached values are not trusted"
                )

        target_modules = set(self.mode_sequence(config, target_state, manual_profile))
        if target_modules & {"slam", "global_planner"}:
            if not prior_dir:
                raise ValueError("missing required map information: slam.prior_dir is empty")
            if not os.path.isdir(os.path.expanduser(os.path.expandvars(prior_dir))):
                raise ValueError(f"slam.prior_dir does not exist or is not a directory: {prior_dir}")
        if "global_planner" in target_modules:
            initial_map = str(get_by_path(config, "global_planner.initial_map", "")).strip()
            if not initial_map:
                raise ValueError("missing required map information: global_planner.initial_map is empty")
        return manual_profile

    @staticmethod
    def manual_profile_for(target_state, prior_dir):
        if target_state != MANUAL:
            return MANUAL_LOCALIZED
        return MANUAL_LOCALIZED if prior_dir else MANUAL_BRIDGE_ONLY

    def normalize_map_config(self, config, provided_paths):
        prior_dir = str(get_by_path(config, "slam.prior_dir", "")).strip()
        multi_map_dir = str(get_by_path(config, "global_planner.multi_map_dir", "")).strip()
        if "slam.prior_dir" in provided_paths and "global_planner.multi_map_dir" not in provided_paths:
            set_by_path(config, "global_planner.multi_map_dir", prior_dir)
        elif not multi_map_dir:
            set_by_path(config, "global_planner.multi_map_dir", prior_dir)

    @staticmethod
    def action_specs(config):
        actions = get_by_path(config, "bringup.actions", {})
        return actions if isinstance(actions, dict) else {}

    def mode_sequence(self, config, mode, manual_profile=MANUAL_LOCALIZED):
        key = "bridge_only_sequence" if mode == MANUAL and manual_profile == MANUAL_BRIDGE_ONLY else "sequence"
        sequence = get_by_path(config, f"bringup.modes.{mode}.{key}", [])
        if not isinstance(sequence, list) or not sequence:
            raise ValueError(f"bringup.modes.{mode}.{key} must be a non-empty list")
        return tuple(str(name) for name in sequence)

    def control_action(self, config, mode):
        action = str(get_by_path(config, f"bringup.modes.{mode}.control_action", "")).strip()
        return action

    def startup_control_action(self, config, mode):
        action = str(
            get_by_path(
                config,
                f"bringup.modes.{mode}.startup_control_action",
                self.control_action(config, mode),
            )
        ).strip()
        return action

    def validate_mode_sequences(self, config):
        actions = self.action_specs(config)
        if not actions:
            raise ValueError("bringup.actions must define navigation control actions")
        for name, spec in actions.items():
            if not isinstance(spec, dict) or str(spec.get("type", "")).strip() != "trigger_service":
                raise ValueError(f"bringup.actions.{name} must use type trigger_service")
            if not str(spec.get("service", "")).strip():
                raise ValueError(f"bringup.actions.{name}.service must be configured")

        modes = get_by_path(config, "bringup.modes", {})
        if not isinstance(modes, dict) or not modes:
            raise ValueError("bringup.modes must define at least one mode")
        for mode in modes:
            profiles = (MANUAL_BRIDGE_ONLY, MANUAL_LOCALIZED) if mode == MANUAL else (MANUAL_LOCALIZED,)
            for profile in profiles:
                self.validate_mode_sequence(config, mode, profile, actions)

    def validate_mode_sequence(self, config, mode, profile, actions):
        names = self.mode_sequence(config, mode, profile)
        duplicates = sorted({name for name in names if names.count(name) > 1})
        if duplicates:
            raise ValueError(f"bringup.modes.{mode} contains duplicate entries: {', '.join(duplicates)}")
        modules = {name for name in names if name in self.module_names}
        unknown = sorted(set(names) - modules - set(actions))
        if unknown:
            raise ValueError(f"bringup.modes.{mode} contains unknown entries: {', '.join(unknown)}")
        control = self.control_action(config, mode)
        startup_control = self.startup_control_action(config, mode)
        for action_name, field_name in ((control, "control_action"), (startup_control, "startup_control_action")):
            if action_name and action_name not in actions:
                raise ValueError(f"bringup.modes.{mode}.{field_name} references unknown action {action_name}")
        if startup_control:
            if startup_control not in names:
                raise ValueError(f"bringup.modes.{mode} must include startup control action {startup_control}")
            if "nav_bridge" not in names or names.index("nav_bridge") + 1 != names.index(startup_control):
                raise ValueError(
                    f"bringup.modes.{mode} startup control action {startup_control} must immediately follow nav_bridge"
                )
        if control and control not in names:
            raise ValueError(f"bringup.modes.{mode} must include control action {control}")
        first_extension = next((index for index, name in enumerate(names) if name in NAV_EXTENSION_MODULES), len(names))
        if any(name in MANUAL_MODULES or name in actions for name in names[first_extension + 1 :]):
            raise ValueError(f"bringup.modes.{mode} must start all base modules and actions before extensions")

    def layer_sequence(self, config, mode, manual_profile, layer):
        sequence = self.mode_sequence(config, mode, manual_profile)
        if layer == "base":
            return tuple(name for name in sequence if name in MANUAL_MODULES or name in self.action_specs(config))
        if layer == "navigation":
            return tuple(name for name in sequence if name in NAV_EXTENSION_MODULES)
        raise ValueError(f"unknown worker layer: {layer}")

    def mode_has_navigation_layer(self):
        if self.current_state == STOPPED or self.active_config is None:
            return False
        return bool(self.layer_sequence(self.active_config, self.current_state, self.manual_profile, "navigation"))

    def active_extension_sequence(self):
        if self.current_state == STOPPED or self.active_config is None:
            return ()
        configured = self.layer_sequence(
            self.active_config, self.current_state, self.manual_profile, "navigation"
        )
        return tuple(name for name in configured if self.worker_alive(name))

    def extension_failure_mode(self):
        fallback = str(
            get_by_path(
                self.active_config,
                f"bringup.modes.{self.current_state}.extension_failure_mode",
                MANUAL,
            )
        ).strip()
        modes = get_by_path(self.active_config, "bringup.modes", {})
        if not isinstance(modes, dict) or fallback not in modes:
            raise ValueError(f"invalid extension_failure_mode for {self.current_state}: {fallback}")
        if self.layer_sequence(self.active_config, fallback, MANUAL_LOCALIZED, "navigation"):
            raise ValueError(f"extension_failure_mode for {self.current_state} must not start navigation extensions")
        return fallback

    def select_transition(self, config, target_state, provided_paths):
        if self.current_state == STOPPED:
            return "full_start"

        changed_paths = {
            path
            for path in provided_paths
            if path != "mode" and get_by_path(config, path) != get_by_path(self.active_config, path)
        }
        if self.current_state == MANUAL and target_state == NAV:
            changed_paths.discard("global_planner.initial_map")

        if changed_paths:
            return "full_restart"
        if self.current_state == target_state:
            if target_state == MANUAL:
                return "refresh_control"
            return "noop"
        if self.current_state == MANUAL and self.manual_profile == MANUAL_BRIDGE_ONLY:
            return "full_restart"
        return "switch_mode"

    def execute_transition(self, config, target_state, transition, manual_profile):
        if transition == "refresh_control":
            success, reason = self.run_control_action(config, target_state)
            if not success:
                return False, f"failed to reacquire control for {target_state}: {reason}"
            return True, f"navigation already running in {target_state} mode; control reacquired"
        if transition == "noop":
            success, reason = self.run_control_action(config, target_state)
            if not success:
                return False, f"failed to refresh control for {target_state}: {reason}"
            return True, f"navigation already running in {target_state} mode; control refreshed"

        if transition == "switch_mode":
            previous_state = self.current_state
            previous_profile = self.manual_profile
            previous_config = copy.deepcopy(self.active_config)
            previous_extensions = self.active_extension_sequence()
            target_extensions = self.layer_sequence(config, target_state, manual_profile, "navigation")
            stopped_extensions = tuple(
                name for name in reversed(previous_extensions) if name not in target_extensions
            )
            for name in stopped_extensions:
                self.stop_worker(name)
            success, reason = self.run_control_action(config, target_state)
            if not success:
                rollback_success, rollback_reason = self.restore_previous_mode(
                    previous_config, previous_state, previous_profile, previous_extensions
                )
                if rollback_success:
                    return False, (
                        f"control transition to {target_state} failed: {reason}; "
                        f"restored {previous_state}: {rollback_reason}"
                    )
                return False, (
                    f"control transition to {target_state} failed: {reason}; "
                    f"rollback failed: {rollback_reason}; navigation stopped"
                )
            extensions_to_start = tuple(name for name in target_extensions if name not in previous_extensions)
            if extensions_to_start:
                success, reason = self.start_extensions(config, extensions_to_start)
                if not success:
                    for name in reversed(extensions_to_start):
                        self.stop_worker(name)
                    rollback_success, rollback_reason = self.restore_previous_mode(
                        previous_config, previous_state, previous_profile, previous_extensions
                    )
                    if rollback_success:
                        return False, (
                            f"switch to {target_state} failed: {reason}; "
                            f"restored {previous_state}: {rollback_reason}"
                        )
                    return False, (
                        f"switch to {target_state} failed: {reason}; "
                        f"rollback failed: {rollback_reason}; navigation stopped"
                    )
            self.commit_success(config, target_state, manual_profile)
            return True, f"switched navigation mode: {previous_state} -> {target_state}"

        if transition == "full_restart":
            self.stop_all_workers()
            self.current_state = STOPPED
            self.manual_profile = None
            self.active_config = None

        base_sequence = self.layer_sequence(config, target_state, manual_profile, "base")
        success, reason = self.start_layer("base", config, base_sequence)
        if not success:
            self.stop_worker("base")
            if manual_profile == MANUAL_BRIDGE_ONLY:
                return False, f"manual bridge-only startup failed: {reason}"
            return False, f"base localization startup failed: {reason}"

        navigation_sequence = self.layer_sequence(config, target_state, manual_profile, "navigation")
        if navigation_sequence:
            success, reason = self.start_extensions(config, navigation_sequence)
            if not success:
                self.stop_extensions(navigation_sequence)
                self.stop_worker("base")
                return False, f"navigation startup failed: {reason}"

        self.commit_success(config, target_state, manual_profile)
        if manual_profile == MANUAL_BRIDGE_ONLY:
            return True, "manual bridge-only started; localization skipped because no slam.prior_dir is available"
        return True, f"navigation started in {target_state} mode"

    def commit_success(self, config, state, manual_profile):
        self.active_config = copy.deepcopy(config)
        self.current_state = state
        self.manual_profile = manual_profile if state == MANUAL else MANUAL_LOCALIZED
        if self.manual_profile == MANUAL_BRIDGE_ONLY:
            return
        for path in ("slam.prior_dir", "global_planner.multi_map_dir", "global_planner.initial_map"):
            value = str(get_by_path(config, path, "")).strip()
            if value:
                self.map_context[path] = value

    def restore_previous_mode(self, config, state, manual_profile, previous_extensions=None):
        if config is None:
            self.stop_worker("base")
            self.current_state = STOPPED
            self.manual_profile = None
            self.active_config = None
            return False, "previous runtime configuration is unavailable"

        success, reason = self.run_control_action(config, state)
        if not success:
            self.stop_worker("base")
            self.current_state = STOPPED
            self.manual_profile = None
            self.active_config = None
            return False, f"failed to restore control: {reason}"

        navigation_sequence = previous_extensions
        if navigation_sequence is None:
            navigation_sequence = self.layer_sequence(config, state, manual_profile, "navigation")
        extensions_to_restore = tuple(
            name for name in navigation_sequence if not self.worker_alive(name)
        )
        if extensions_to_restore:
            success, reason = self.start_extensions(config, extensions_to_restore)
            if not success:
                self.stop_extensions(navigation_sequence)
                self.stop_worker("base")
                self.current_state = STOPPED
                self.manual_profile = None
                self.active_config = None
                return False, f"failed to restore navigation extensions: {reason}"

        self.commit_success(config, state, manual_profile)
        return True, "previous mode restored"

    def apply_map_context(self, config):
        for path, value in self.map_context.items():
            if path in self.allowed_paths:
                set_by_path(config, path, value)

    def layer_config(self, config, sequence):
        runtime_config = copy.deepcopy(config)
        runtime_config.setdefault("bringup", {})["start_mode"] = "immediate"
        # A service request cannot report success until module readiness and every
        # configured control action have completed successfully.
        runtime_config["bringup"]["wait_for_readiness"] = True
        runtime_config["bringup"]["sequence"] = list(sequence)
        return runtime_config

    def run_control_action(self, config, mode):
        action_name = self.control_action(config, mode)
        if not action_name:
            return True, "no control action configured"
        success, reason = self.run_charge_precheck(config, mode)
        if not success:
            return False, reason
        spec = self.action_specs(config)[action_name]
        service = str(spec["service"])
        timeout = float(spec.get("timeout_seconds", 5.0))
        command = [
            "python3",
            os.path.join(os.path.dirname(__file__), "wait_for_ready.py"),
            "trigger",
            "--name",
            action_name,
            "--service",
            service,
            "--timeout",
            str(timeout),
        ]
        self.get_logger().info(f"mode {mode}: running control action {action_name} ({service})")
        completed = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        output = completed.stdout.strip()
        if output:
            for line in output.splitlines():
                self.get_logger().info(f"{action_name}: {line}")
        if completed.returncode == 0:
            return True, f"{action_name} succeeded"
        return False, output or f"{action_name} failed with exit code {completed.returncode}"

    def run_charge_precheck(self, config, mode):
        readiness = get_by_path(config, "nav_bridge.readiness", {})
        if not isinstance(readiness, dict) or not bool(readiness.get("charge_precheck", True)):
            return True, "charge precheck disabled"
        command = [
            "python3",
            os.path.join(os.path.dirname(__file__), "wait_for_ready.py"),
            "charge_precheck",
            "--charge-state-topic",
            str(readiness.get("charge_state_topic", "/charge_manager_state")),
            "--charge-command-service",
            str(readiness.get("charge_command_service", "/nav_bridge_node/charge_command")),
            "--charge-executor-exit-service",
            str(
                readiness.get(
                    "charge_executor_exit_service",
                    "/inspection_charge_executor/exit_charge",
                )
            ),
            "--charge-check-timeout",
            str(float(readiness.get("charge_check_timeout_seconds", 10.0))),
            "--charge-exit-timeout",
            str(float(readiness.get("charge_exit_timeout_seconds", 30.0))),
            "--charge-poll-interval",
            str(float(readiness.get("charge_poll_interval_seconds", 0.5))),
        ]
        self.get_logger().info(f"mode {mode}: checking charge state before control action")
        completed = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        output = completed.stdout.strip()
        if output:
            for line in output.splitlines():
                self.get_logger().info(f"charge_precheck: {line}")
        if completed.returncode == 0:
            return True, "charge state is idle"
        return False, output or f"charge precheck failed with exit code {completed.returncode}"

    def start_layer(self, layer, config, sequence):
        run_id = uuid.uuid4().hex
        worker_state_dir = os.path.join(self.state_dir, run_id, layer)
        resolved_config_path = os.path.join(worker_state_dir, "resolved.yaml")
        result_path = os.path.join(worker_state_dir, "result.yaml")
        os.makedirs(worker_state_dir, exist_ok=True)
        write_yaml_atomic(resolved_config_path, self.layer_config(config, sequence))

        process = subprocess.Popen(
            [
                "ros2",
                "launch",
                "inspection_bringup",
                "navigation.launch.py",
                f"navigate_config_path:={resolved_config_path}",
                f"navigation_state_dir:={worker_state_dir}",
                f"navigation_run_id:={run_id}",
            ],
            start_new_session=True,
        )
        worker = {"process": process, "run_id": run_id, "result_path": result_path}
        self.workers[layer] = worker
        self.get_logger().info(f"started {layer} worker pid={process.pid} run_id={run_id}")
        result = self.wait_for_result(worker)
        return bool(result.get("success", False)), str(result.get("reason", "worker result missing reason"))

    def start_extensions(self, config, sequence):
        for name in sequence:
            if self.worker_alive(name):
                continue
            success, reason = self.start_layer(name, config, (name,))
            if not success:
                return False, f"{name} startup failed: {reason}"
        return True, "navigation extensions started"

    def stop_extensions(self, sequence=None):
        names = tuple(sequence) if sequence is not None else NAV_EXTENSION_MODULES
        for name in reversed(names):
            self.stop_worker(name)

    def stop_worker(self, layer):
        worker = self.workers.get(layer)
        if worker is None:
            return
        process = worker["process"]
        if process.poll() is None:
            self.get_logger().info(f"stopping {layer} worker pid={process.pid}")
            try:
                os.killpg(process.pid, signal.SIGINT)
            except ProcessLookupError:
                pass
            try:
                process.wait(timeout=10.0)
            except subprocess.TimeoutExpired:
                self.get_logger().warning(f"{layer} worker did not stop after SIGINT; sending SIGTERM")
                try:
                    os.killpg(process.pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
                try:
                    process.wait(timeout=5.0)
                except subprocess.TimeoutExpired:
                    self.get_logger().error(f"{layer} worker did not stop after SIGTERM; sending SIGKILL")
                    try:
                        os.killpg(process.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                    process.wait()
        self.workers[layer] = None

    def stop_all_workers(self):
        self.stop_extensions()
        self.stop_worker("base")

    def wait_for_result(self, worker):
        deadline = None if self.result_timeout <= 0.0 else time.monotonic() + self.result_timeout
        while deadline is None or time.monotonic() < deadline:
            if os.path.exists(worker["result_path"]):
                try:
                    result = load_yaml(worker["result_path"])
                except Exception as exc:
                    return {"success": False, "reason": f"failed to read worker result: {exc}"}
                if str(result.get("run_id", "")) == worker["run_id"]:
                    return result
            returncode = worker["process"].poll()
            if returncode is not None:
                return {
                    "success": False,
                    "reason": f"worker exited before reporting startup result (exit_code={returncode})",
                }
            time.sleep(0.2)
        return {"success": False, "reason": f"worker result timeout after {self.result_timeout:.1f}s"}

    def refresh_state(self):
        base_alive = self.worker_alive("base")
        if not base_alive:
            self.stop_extensions()
            self.workers["base"] = None
            self.current_state = STOPPED
            self.manual_profile = None
            self.active_config = None
        elif self.mode_has_navigation_layer():
            expected_extensions = self.layer_sequence(
                self.active_config, self.current_state, self.manual_profile, "navigation"
            )
            missing_extensions = tuple(
                name for name in expected_extensions if not self.worker_alive(name)
            )
            if not missing_extensions:
                return
            self.get_logger().error(
                "navigation extension worker exited: " + ", ".join(missing_extensions)
            )
            self.stop_extensions(expected_extensions)
            fallback = self.extension_failure_mode()
            if self.active_config is not None:
                success, reason = self.run_control_action(self.active_config, fallback)
                if not success:
                    self.get_logger().error(
                        f"extension worker exited and fallback control action failed: {reason}; stopping localization"
                    )
                    self.stop_worker("base")
                    self.current_state = STOPPED
                    self.manual_profile = None
                    self.active_config = None
                    return
            set_by_path(self.active_config, "mode", fallback)
            self.current_state = fallback
            self.manual_profile = (
                self.manual_profile_for(fallback, str(get_by_path(self.active_config, "slam.prior_dir", "")))
                if fallback == MANUAL
                else MANUAL_LOCALIZED
            )

    def monitor_workers(self):
        # A worker can exit after startup has reported success. Keep the in-memory
        # mode honest even when no later service request arrives.
        with self.request_lock:
            if self.in_progress:
                return
            base_alive = self.worker_alive("base")
            expected_extensions = self.layer_sequence(
                self.active_config, self.current_state, self.manual_profile, "navigation"
            ) if self.active_config is not None and self.current_state != STOPPED else ()
            missing_extensions = tuple(name for name in expected_extensions if not self.worker_alive(name))
            needs_cleanup = (
                (not base_alive and (self.current_state != STOPPED or any(self.worker_alive(name) for name in NAV_EXTENSION_MODULES)))
                or bool(missing_extensions)
            )
            if not needs_cleanup:
                return
            self.in_progress = True
            self.busy_reason = "navigation worker cleanup in progress"

        try:
            with self.state_lock:
                previous_state = self.current_state
                self.refresh_state()
        finally:
            with self.request_lock:
                self.in_progress = False
                self.busy_reason = ""

        if previous_state == self.current_state:
            return
        if not base_alive:
            self.get_logger().error(
                "base localization worker exited; navigation state changed to stopped"
            )
        elif self.current_state != previous_state and missing_extensions:
            self.get_logger().error(
                "navigation extension worker exited; navigation state changed to fallback mode"
            )

    def worker_alive(self, layer):
        worker = self.workers.get(layer)
        return worker is not None and worker["process"].poll() is None

    @staticmethod
    def result(successful, reason):
        result = SetParametersResult()
        result.successful = bool(successful)
        result.reason = str(reason)
        return result


def run_node(args):
    rclpy.init()
    node = NavigationSupervisor(args.config, args.service_name, args.state_dir, args.result_timeout)
    executor = MultiThreadedExecutor(num_threads=2)
    executor.add_node(node)
    try:
        executor.spin()
    except KeyboardInterrupt:
        pass
    finally:
        node.stop_all_workers()
        executor.shutdown()
        node.destroy_node()
        if rclpy.ok():
            rclpy.shutdown()
    return 0


def main():
    parser = argparse.ArgumentParser(description="Navigation launch supervisor.")
    subparsers = parser.add_subparsers(dest="command", required=True)
    node_parser = subparsers.add_parser("node", help="Run the supervisor ROS node.")
    node_parser.add_argument("--config", required=True, help="navigate.yaml path")
    node_parser.add_argument("--service-name", default="/navigation_bringup/start")
    node_parser.add_argument("--state-dir", required=True)
    node_parser.add_argument("--result-timeout", type=float, default=0.0)
    node_parser.set_defaults(func=run_node)
    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
