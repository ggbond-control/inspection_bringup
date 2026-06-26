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
    deadline = time.monotonic() + timeout
    expected = set(nodes)
    missing = expected
    print(f"[{name}] waiting for nodes: {' '.join(nodes)}", flush=True)

    while time.monotonic() < deadline:
        missing = expected - list_nodes()
        if not missing:
            print(f"[{name}] nodes ready", flush=True)
            return True
        time.sleep(0.5)

    print(f"[{name}] node wait timeout: {' '.join(sorted(missing))}", file=sys.stderr, flush=True)
    return False


def wait_for_topic_message(topic, deadline, name):
    print(f"[{name}] waiting for topic message: {topic}", flush=True)
    start_time = time.monotonic()
    last_returncode = None
    last_stdout = ""
    last_stderr = ""

    while time.monotonic() < deadline:
        remaining = max(0.1, deadline - time.monotonic())
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
    deadline = time.monotonic() + timeout
    for topic in topics:
        if not wait_for_topic_message(topic, deadline, name):
            return False
    return True


def call_trigger_service(service, timeout, name):
    deadline = time.monotonic() + timeout
    print(f"[{name}] calling trigger service: {service}", flush=True)

    while time.monotonic() < deadline:
        remaining = max(0.1, deadline - time.monotonic())
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
        if result.returncode == 0 and re.search(r"success\s*[:=]\s*true", output, re.IGNORECASE):
            print(f"[{name}] trigger service succeeded: {service}", flush=True)
            return True

        time.sleep(0.5)

    print(f"[{name}] trigger service timeout or failure: {service}", file=sys.stderr, flush=True)
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


def main():
    parser = argparse.ArgumentParser(description="Wait for ROS 2 readiness conditions.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    nodes_parser = subparsers.add_parser("nodes", help="Wait for ROS 2 nodes to exist.")
    nodes_parser.add_argument("--name", default="module", help="Name printed in status messages.")
    nodes_parser.add_argument("--timeout", type=float, default=10.0, help="Timeout in seconds.")
    nodes_parser.add_argument("nodes", nargs="+", help="Fully qualified node names to wait for.")
    nodes_parser.set_defaults(func=run_nodes)

    topics_parser = subparsers.add_parser(
        "topics",
        help="Wait until topics publish at least one message.",
    )
    topics_parser.add_argument("--name", default="topics", help="Name printed in status messages.")
    topics_parser.add_argument("--timeout", type=float, default=10.0, help="Total timeout in seconds.")
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
    nav_bridge_parser.add_argument("--topic-timeout", type=float, default=10.0)
    nav_bridge_parser.add_argument("--stand-timeout", type=float, default=10.0)
    nav_bridge_parser.set_defaults(func=run_nav_bridge)

    args = parser.parse_args()
    return 0 if args.func(args) else 1


if __name__ == "__main__":
    sys.exit(main())
