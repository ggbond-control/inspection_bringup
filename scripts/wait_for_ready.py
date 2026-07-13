#!/usr/bin/env python3
import argparse
import os
import re
import subprocess
import sys
import time

import yaml




def write_failure_detail(path, module, reason, category="READINESS_FAILED", **fields):
    if not path:
        return
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp_path = f"{path}.tmp"
        data = {
            "module": module,
            "category": category,
            "reason": reason,
            "reported_at": time.time(),
        }
        data.update(fields)
        with open(tmp_path, "w", encoding="utf-8") as stream:
            yaml.safe_dump(data, stream, sort_keys=False)
        os.replace(tmp_path, path)
    except Exception as exc:
        print(f"[{module}] failed to write readiness detail: {exc}", file=sys.stderr, flush=True)

def list_nodes():
    result = subprocess.run(
        ["ros2", "node", "list"],
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
    )
    if result.returncode != 0:
        return set()
    return {line.strip() for line in result.stdout.splitlines() if line.strip()}


def wait_for_nodes(nodes, timeout, name, failure_detail=None):
    deadline = deadline_from_timeout(timeout)
    expected = set(nodes)
    missing = expected
    print(f"[{name}] waiting for nodes {timeout_text(timeout)}: {' '.join(nodes)}", flush=True)

    while before_deadline(deadline):
        missing = expected - list_nodes()
        if not missing:
            print(f"[{name}] nodes ready", flush=True)
            return True
        time.sleep(0.5)

    reason = f"node wait timeout: {' '.join(sorted(missing))}"
    print(f"[{name}] {reason}", file=sys.stderr, flush=True)
    write_failure_detail(failure_detail, name, reason, missing_nodes=sorted(missing))
    return False


def deadline_from_timeout(timeout):
    if timeout is None or timeout <= 0.0:
        return None
    return time.monotonic() + timeout


def before_deadline(deadline):
    return deadline is None or time.monotonic() < deadline


def remaining_timeout(deadline, fallback):
    if deadline is None:
        return fallback
    return max(0.1, deadline - time.monotonic())


def timeout_text(timeout):
    if timeout is None or timeout <= 0.0:
        return "without timeout"
    return f"for up to {timeout:.1f}s"


def wait_for_topic_message(topic, timeout, name, failure_detail=None):
    print(f"[{name}] waiting for topic message {timeout_text(timeout)}: {topic}", flush=True)
    deadline = deadline_from_timeout(timeout)
    start_time = time.monotonic()
    last_returncode = None
    last_stdout = ""
    last_stderr = ""

    while before_deadline(deadline):
        remaining = remaining_timeout(deadline, 2.0)
        process = subprocess.Popen(
            ["ros2", "topic", "echo", "--once", topic],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )

        try:
            stdout, stderr = process.communicate(timeout=min(2.0, remaining))
        except subprocess.TimeoutExpired:
            process.terminate()
            try:
                process.wait(timeout=0.5)
            except subprocess.TimeoutExpired:
                process.kill()
            last_returncode = "timeout"
            continue

        last_returncode = process.returncode
        last_stdout = stdout.strip()
        last_stderr = stderr.strip()
        if process.returncode == 0 and stdout.strip():
            print(f"[{name}] topic message received: {topic}", flush=True)
            return True

        time.sleep(0.5)

    elapsed = time.monotonic() - start_time
    print(f"[{name}] topic message timeout after {elapsed:.1f}s: {topic}", file=sys.stderr, flush=True)
    if last_returncode is not None:
        print(f"[{name}] last topic echo return: {last_returncode}", file=sys.stderr, flush=True)
    if last_stderr:
        print(f"[{name}] last topic echo stderr: {last_stderr}", file=sys.stderr, flush=True)
    if last_stdout:
        print(f"[{name}] last topic echo stdout: {last_stdout}", file=sys.stderr, flush=True)

    info = subprocess.run(
        ["ros2", "topic", "info", topic],
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    info_output = (info.stdout or info.stderr).strip()
    if info_output:
        print(f"[{name}] topic info for {topic}:\n{info_output}", file=sys.stderr, flush=True)
    else:
        print(f"[{name}] topic info for {topic}: unavailable", file=sys.stderr, flush=True)
    write_failure_detail(
        failure_detail,
        name,
        f"topic message timeout after {elapsed:.1f}s: {topic}",
        topic=topic,
        timeout_seconds=timeout,
        last_returncode=str(last_returncode),
        last_stdout=last_stdout,
        last_stderr=last_stderr,
        topic_info=info_output,
    )
    return False


def wait_for_topics(topics, timeout, name, failure_detail=None):
    for topic in topics:
        if not wait_for_topic_message(topic, timeout, name, failure_detail):
            return False
    return True


def call_trigger_service(service, timeout, name, failure_detail=None):
    deadline = deadline_from_timeout(timeout)
    print(f"[{name}] calling trigger service {timeout_text(timeout)}: {service}", flush=True)

    last_output = ""
    while before_deadline(deadline):
        remaining = remaining_timeout(deadline, 5.0)
        try:
            result = subprocess.run(
                ["ros2", "service", "call", service, "std_srvs/srv/Trigger", "{}"],
                check=False,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=min(5.0, remaining),
            )
        except subprocess.TimeoutExpired:
            continue

        output = f"{result.stdout}\n{result.stderr}"
        last_output = output.strip()
        if result.returncode == 0 and re.search(r"success\s*[:=]\s*true", output, re.IGNORECASE):
            print(f"[{name}] trigger service succeeded: {service}", flush=True)
            return True

        time.sleep(0.5)

    print(f"[{name}] trigger service timeout or failure: {service}", file=sys.stderr, flush=True)
    if last_output:
        print(f"[{name}] last trigger service output:\n{last_output}", file=sys.stderr, flush=True)
    write_failure_detail(
        failure_detail,
        name,
        f"trigger service timeout or failure: {service}",
        service=service,
        timeout_seconds=timeout,
        last_output=last_output,
    )
    return False


def run_nodes(args):
    return wait_for_nodes(args.nodes, args.timeout, args.name, args.failure_detail)


def run_topics(args):
    return wait_for_topics(args.topics, args.timeout, args.name, args.failure_detail)


def run_nav_bridge(args):
    topics = args.topics or ["/battery/level"]
    if not wait_for_topics(topics, args.topic_timeout, "nav_bridge", args.failure_detail):
        return False
    return call_trigger_service(args.stand_service, args.stand_timeout, "nav_bridge", args.failure_detail)


def diagnostic_level_name(level):
    names = {
        0: "OK",
        1: "WARN",
        2: "ERROR",
        3: "STALE",
    }
    return names.get(level, f"UNKNOWN({level})")


def diagnostic_values(msg):
    values = {}
    for item in getattr(msg, "values", []):
        values[str(getattr(item, "key", ""))] = str(getattr(item, "value", ""))
    return values


def diagnostic_reason(name, topic, msg, prefix):
    level = int(getattr(msg, "level", 255))
    hardware_id = str(getattr(msg, "hardware_id", ""))
    message = str(getattr(msg, "message", ""))
    values = diagnostic_values(msg)
    value_text = ", ".join(f"{key}={value}" for key, value in values.items() if key)
    extra = f" values=({value_text})" if value_text else ""
    hardware = f" hardware_id={hardware_id}" if hardware_id else ""
    return (
        f"{prefix}: topic={topic} level={diagnostic_level_name(level)} "
        f"message=\"{message}\"{hardware}{extra}"
    )


def wait_for_health(topic, timeout, name, failure_detail=None):
    try:
        import rclpy
        from diagnostic_msgs.msg import DiagnosticStatus
        from rclpy.qos import DurabilityPolicy, QoSProfile, ReliabilityPolicy
    except ImportError as exc:
        reason = f"failed to import health status dependencies: {exc}"
        print(f"[{name}] {reason}", file=sys.stderr, flush=True)
        write_failure_detail(failure_detail, name, reason, category="IMPORT_ERROR")
        return False

    rclpy.init(args=None)
    node = rclpy.create_node(f"wait_for_{name}_health")
    qos = QoSProfile(depth=1)
    qos.reliability = ReliabilityPolicy.RELIABLE
    qos.durability = DurabilityPolicy.TRANSIENT_LOCAL
    latest_status = {"msg": None}

    def on_status(msg):
        latest_status["msg"] = msg

    node.create_subscription(DiagnosticStatus, topic, on_status, qos)
    deadline = deadline_from_timeout(timeout)
    last_printed = None

    print(f"[{name}] waiting for health status {timeout_text(timeout)}: {topic}", flush=True)
    try:
        while before_deadline(deadline):
            rclpy.spin_once(node, timeout_sec=0.2)
            msg = latest_status["msg"]
            if msg is None:
                continue

            level = int(getattr(msg, "level", 255))
            status_key = (
                level,
                str(getattr(msg, "name", "")),
                str(getattr(msg, "message", "")),
                tuple(sorted(diagnostic_values(msg).items())),
            )
            if status_key != last_printed:
                last_printed = status_key
                print(
                    f"[{name}] health level={diagnostic_level_name(level)} "
                    f"message=\"{getattr(msg, 'message', '')}\" "
                    f"values={diagnostic_values(msg)}",
                    flush=True,
                )

            if level == 0:
                print(f"[{name}] health ready: {topic}", flush=True)
                return True

            if level in (2, 3):
                reason = diagnostic_reason(name, topic, msg, "health failed")
                print(f"[{name}] {reason}", file=sys.stderr, flush=True)
                write_failure_detail(
                    failure_detail,
                    name,
                    reason,
                    category=diagnostic_level_name(level),
                    topic=topic,
                    level=level,
                    level_name=diagnostic_level_name(level),
                    status_name=str(getattr(msg, "name", "")),
                    message=str(getattr(msg, "message", "")),
                    hardware_id=str(getattr(msg, "hardware_id", "")),
                    values=diagnostic_values(msg),
                )
                return False

        msg = latest_status["msg"]
        if msg is None:
            reason = f"health timeout after {timeout:.1f}s: no status received on {topic}"
            print(f"[{name}] {reason}", file=sys.stderr, flush=True)
            write_failure_detail(failure_detail, name, reason, category="TIMEOUT_NO_STATUS", topic=topic)
        else:
            reason = diagnostic_reason(name, topic, msg, f"health timeout after {timeout:.1f}s")
            print(f"[{name}] {reason}", file=sys.stderr, flush=True)
            write_failure_detail(
                failure_detail,
                name,
                reason,
                category="TIMEOUT_NOT_READY",
                topic=topic,
                level=int(getattr(msg, "level", 255)),
                level_name=diagnostic_level_name(int(getattr(msg, "level", 255))),
                status_name=str(getattr(msg, "name", "")),
                message=str(getattr(msg, "message", "")),
                values=diagnostic_values(msg),
            )
        return False
    finally:
        node.destroy_node()
        rclpy.shutdown()


def run_health(args):
    return wait_for_health(args.topic, args.timeout, args.name, args.failure_detail)


def state_name(state):
    names = {
        0: "WAITING_FOR_DATA",
        1: "GRAVITY_ALIGNING",
        2: "INITIAL_REGISTRATION",
        3: "TRACKING",
        4: "INITIAL_REGISTRATION_BLOCKED",
    }
    return names.get(state, f"UNKNOWN({state})")


def format_score(score):
    try:
        return f"{float(score):.4f}"
    except (TypeError, ValueError):
        return str(score)




def localization_category(state, message):
    if state == 3:
        return "READY"
    if "Fusion keyframe seed data unavailable" in message:
        return "WARNING_DEGRADED"
    if state == 4 and ("prior map load failed" in message or "prior map is not loaded" in message):
        return "FATAL_CONFIG_ERROR"
    if state == 4 and "max attempts reached" in message:
        return "RETRYABLE_BLOCKED"
    if state == 4:
        return "BLOCKED"
    if state in (0, 1, 2):
        return "PROGRESS"
    return "UNKNOWN"


def localization_reason(msg, prefix):
    state = int(getattr(msg, "state", 255))
    message = str(getattr(msg, "message", ""))
    category = localization_category(state, message)
    return (
        f"{prefix}: {category}: state={state_name(state)} "
        f"attempts={getattr(msg, 'attempt_count', 0)}/{getattr(msg, 'max_attempts', 0)} "
        f"score={format_score(getattr(msg, 'last_score', 'nan'))} "
        f"last_success={getattr(msg, 'last_success', False)} "
        f"message=\"{message}\""
    )


def write_localization_failure_detail(path, msg, reason):
    state = int(getattr(msg, "state", 255))
    message = str(getattr(msg, "message", ""))
    write_failure_detail(
        path,
        "slam",
        reason,
        category=localization_category(state, message),
        state=state,
        state_name=state_name(state),
        attempt_count=int(getattr(msg, "attempt_count", 0)),
        max_attempts=int(getattr(msg, "max_attempts", 0)),
        last_score=format_score(getattr(msg, "last_score", "nan")),
        last_success=bool(getattr(msg, "last_success", False)),
        message=message,
    )

def wait_for_localization_init(
    status_topic,
    timeout,
    blocked_is_failure,
    release_control_on_blocked,
    release_control_service,
    release_control_timeout,
    failure_detail=None,
):
    try:
        import rclpy
        from inspection_interfaces.msg import LocalizationInitStatus
        from rclpy.qos import DurabilityPolicy, QoSProfile, ReliabilityPolicy
    except ImportError as exc:
        reason = f"failed to import localization status dependencies: {exc}"
        print(f"[slam] {reason}", file=sys.stderr, flush=True)
        write_failure_detail(failure_detail, "slam", reason, category="IMPORT_ERROR")
        return False

    rclpy.init(args=None)
    node = rclpy.create_node("wait_for_localization_init")
    qos = QoSProfile(depth=1)
    qos.reliability = ReliabilityPolicy.RELIABLE
    qos.durability = DurabilityPolicy.TRANSIENT_LOCAL

    latest_status = {"msg": None}

    def on_status(msg):
        latest_status["msg"] = msg

    node.create_subscription(LocalizationInitStatus, status_topic, on_status, qos)
    deadline = deadline_from_timeout(timeout)
    last_printed = None
    blocked_notice_printed = False
    release_control_called_for_block = False

    print(
        f"[slam] waiting for localization init status {timeout_text(timeout)}: {status_topic}",
        flush=True,
    )
    try:
        while before_deadline(deadline):
            rclpy.spin_once(node, timeout_sec=0.2)
            msg = latest_status["msg"]
            if msg is None:
                continue

            state = int(getattr(msg, "state", 255))
            status_key = (
                state,
                int(getattr(msg, "attempt_count", 0)),
                int(getattr(msg, "max_attempts", 0)),
                format_score(getattr(msg, "last_score", "nan")),
                bool(getattr(msg, "last_success", False)),
                str(getattr(msg, "message", "")),
            )
            if status_key != last_printed:
                last_printed = status_key
                print(
                    "[slam] localization "
                    f"state={state_name(state)} "
                    f"attempts={getattr(msg, 'attempt_count', 0)}/{getattr(msg, 'max_attempts', 0)} "
                    f"score={format_score(getattr(msg, 'last_score', 'nan'))} "
                    f"last_success={getattr(msg, 'last_success', False)} "
                    f"message=\"{getattr(msg, 'message', '')}\"",
                    flush=True,
                )

            if state == 3:
                print("[slam] localization ready: TRACKING", flush=True)
                return True

            if state == 4:
                if release_control_on_blocked and not release_control_called_for_block:
                    if call_trigger_service(release_control_service, release_control_timeout, "slam", failure_detail):
                        print("[slam] release_control succeeded after localization blocked", flush=True)
                    else:
                        print(
                            "[slam] release_control failed after localization blocked; "
                            "continuing to wait for external restart",
                            file=sys.stderr,
                            flush=True,
                        )
                    release_control_called_for_block = True
                if blocked_is_failure:
                    reason = localization_reason(msg, "localization blocked; failing readiness")
                    print(f"[slam] {reason}", file=sys.stderr, flush=True)
                    write_localization_failure_detail(failure_detail, msg, reason)
                    return False
                if not blocked_notice_printed:
                    print("[slam] localization blocked; waiting for external restart", flush=True)
                    blocked_notice_printed = True
                time.sleep(1.0)
            else:
                blocked_notice_printed = False
                release_control_called_for_block = False

        msg = latest_status["msg"]
        if msg is None:
            reason = f"localization init timeout after {timeout:.1f}s: no status received"
            print(f"[slam] {reason}", file=sys.stderr, flush=True)
            write_failure_detail(failure_detail, "slam", reason, category="TIMEOUT_NO_STATUS")
        else:
            print(
                "[slam] localization init timeout after "
                f"{timeout:.1f}s: last_state={state_name(int(getattr(msg, 'state', 255)))} "
                f"attempts={getattr(msg, 'attempt_count', 0)}/{getattr(msg, 'max_attempts', 0)} "
                f"score={format_score(getattr(msg, 'last_score', 'nan'))} "
                f"message=\"{getattr(msg, 'message', '')}\"",
                file=sys.stderr,
                flush=True,
            )
        return False
    finally:
        node.destroy_node()
        rclpy.shutdown()


def run_localization_init(args):
    return wait_for_localization_init(
        args.status_topic,
        args.timeout,
        args.blocked_is_failure,
        args.release_control_on_blocked,
        args.release_control_service,
        args.release_control_timeout,
        args.failure_detail,
    )


def main():
    parser = argparse.ArgumentParser(description="Wait for ROS 2 readiness conditions.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    nodes_parser = subparsers.add_parser("nodes", help="Wait for ROS 2 nodes to exist.")
    nodes_parser.add_argument("--name", default="module", help="Name printed in status messages.")
    nodes_parser.add_argument(
        "--timeout",
        type=float,
        default=10.0,
        help="Timeout in seconds. Use 0 or a negative value to wait forever.",
    )
    nodes_parser.add_argument("nodes", nargs="+", help="Fully qualified node names to wait for.")
    nodes_parser.add_argument("--failure-detail", default="")
    nodes_parser.set_defaults(func=run_nodes)

    topics_parser = subparsers.add_parser(
        "topics",
        help="Wait until topics publish at least one message.",
    )
    topics_parser.add_argument("--name", default="topics", help="Name printed in status messages.")
    topics_parser.add_argument(
        "--timeout",
        type=float,
        default=10.0,
        help="Per-topic timeout in seconds. Use 0 or a negative value to wait forever.",
    )
    topics_parser.add_argument("topics", nargs="+", help="Topic names to wait for.")
    topics_parser.add_argument("--failure-detail", default="")
    topics_parser.set_defaults(func=run_topics)

    health_parser = subparsers.add_parser(
        "health",
        help="Wait for diagnostic_msgs/msg/DiagnosticStatus to report OK.",
    )
    health_parser.add_argument("--name", default="health", help="Name printed in status messages.")
    health_parser.add_argument("--topic", required=True, help="DiagnosticStatus topic to monitor.")
    health_parser.add_argument(
        "--timeout",
        type=float,
        default=10.0,
        help="Timeout in seconds. Use 0 or a negative value to wait forever.",
    )
    health_parser.add_argument("--failure-detail", default="")
    health_parser.set_defaults(func=run_health)

    nav_bridge_parser = subparsers.add_parser(
        "nav_bridge",
        help="Wait for nav_bridge topic data, then call stand service.",
    )
    nav_bridge_parser.add_argument(
        "--topic",
        action="append",
        dest="topics",
        help="Topic that must publish at least one message before stand is called.",
    )
    nav_bridge_parser.add_argument("--stand-service", default="/nav_bridge_node/stand")
    nav_bridge_parser.add_argument(
        "--topic-timeout",
        type=float,
        default=10.0,
        help="Per-topic timeout in seconds. Use 0 or a negative value to wait forever.",
    )
    nav_bridge_parser.add_argument(
        "--stand-timeout",
        type=float,
        default=10.0,
        help="Trigger service timeout in seconds. Use 0 or a negative value to wait forever.",
    )
    nav_bridge_parser.add_argument("--failure-detail", default="")
    nav_bridge_parser.set_defaults(func=run_nav_bridge)

    localization_parser = subparsers.add_parser(
        "localization-init",
        help="Wait for faster_lio localization initialization to reach TRACKING.",
    )
    localization_parser.add_argument("--status-topic", default="/localization_init_status")
    localization_parser.add_argument(
        "--timeout",
        type=float,
        default=120.0,
        help="Timeout in seconds. Use 0 or a negative value to wait forever.",
    )
    localization_parser.add_argument("--blocked-is-failure", action="store_true")
    localization_parser.add_argument(
        "--release-control-on-blocked",
        action="store_true",
        help="Call nav_bridge release_control once when localization enters BLOCKED.",
    )
    localization_parser.add_argument(
        "--release-control-service",
        default="/nav_bridge_node/release_control",
        help="std_srvs/srv/Trigger service used to release nav_bridge control.",
    )
    localization_parser.add_argument(
        "--release-control-timeout",
        type=float,
        default=5.0,
        help="release_control timeout in seconds. Use 0 or a negative value to wait forever.",
    )
    localization_parser.add_argument("--failure-detail", default="")
    localization_parser.set_defaults(func=run_localization_init)

    args = parser.parse_args()
    return 0 if args.func(args) else 1


if __name__ == "__main__":
    sys.exit(main())
