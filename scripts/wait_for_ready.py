#!/usr/bin/env python3
import argparse
import re
import subprocess
import sys
import time


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


def wait_for_nodes(nodes, timeout, name):
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

    print(f"[{name}] node wait timeout: {' '.join(sorted(missing))}", file=sys.stderr, flush=True)
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


def wait_for_topic_message(topic, timeout, name):
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
    return False


def wait_for_topics(topics, timeout, name):
    for topic in topics:
        if not wait_for_topic_message(topic, timeout, name):
            return False
    return True


def call_trigger_service(service, timeout, name):
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
    return False


def run_nodes(args):
    return wait_for_nodes(args.nodes, args.timeout, args.name)


def run_topics(args):
    return wait_for_topics(args.topics, args.timeout, args.name)


def run_nav_bridge(args):
    topics = args.topics or ["/battery/level"]
    if not wait_for_topics(topics, args.topic_timeout, "nav_bridge"):
        return False
    return call_trigger_service(args.stand_service, args.stand_timeout, "nav_bridge")


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


def wait_for_localization_init(
    status_topic,
    timeout,
    blocked_is_failure,
    release_control_on_blocked,
    release_control_service,
    release_control_timeout,
):
    try:
        import rclpy
        from inspection_interfaces.msg import LocalizationInitStatus
        from rclpy.qos import DurabilityPolicy, QoSProfile, ReliabilityPolicy
    except ImportError as exc:
        print(f"[slam] failed to import localization status dependencies: {exc}", file=sys.stderr, flush=True)
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
                if blocked_is_failure:
                    print("[slam] localization blocked; failing readiness", file=sys.stderr, flush=True)
                    return False
                if release_control_on_blocked and not release_control_called_for_block:
                    if call_trigger_service(release_control_service, release_control_timeout, "slam"):
                        print("[slam] release_control succeeded after localization blocked", flush=True)
                    else:
                        print(
                            "[slam] release_control failed after localization blocked; "
                            "continuing to wait for external restart",
                            file=sys.stderr,
                            flush=True,
                        )
                    release_control_called_for_block = True
                if not blocked_notice_printed:
                    print("[slam] localization blocked; waiting for external restart", flush=True)
                    blocked_notice_printed = True
                time.sleep(1.0)
            else:
                blocked_notice_printed = False
                release_control_called_for_block = False

        msg = latest_status["msg"]
        if msg is None:
            print(
                f"[slam] localization init timeout after {timeout:.1f}s: no status received",
                file=sys.stderr,
                flush=True,
            )
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
    topics_parser.set_defaults(func=run_topics)

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
    localization_parser.set_defaults(func=run_localization_init)

    args = parser.parse_args()
    return 0 if args.func(args) else 1


if __name__ == "__main__":
    sys.exit(main())
