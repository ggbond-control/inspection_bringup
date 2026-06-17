#!/usr/bin/env python3
import argparse
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


def main():
    parser = argparse.ArgumentParser(description="Wait until all requested ROS 2 nodes exist.")
    parser.add_argument("--name", default="module", help="Name printed in status messages.")
    parser.add_argument("--timeout", type=float, default=10.0, help="Timeout in seconds.")
    parser.add_argument("nodes", nargs="+", help="Fully qualified node names to wait for.")
    args = parser.parse_args()

    deadline = time.monotonic() + args.timeout
    expected = set(args.nodes)
    missing = expected
    print(f"[{args.name}] waiting for nodes: {' '.join(args.nodes)}", flush=True)

    while time.monotonic() < deadline:
        missing = expected - list_nodes()
        if not missing:
            print(f"[{args.name}] nodes ready", flush=True)
            return 0
        time.sleep(0.5)

    print(f"[{args.name}] node wait timeout: {' '.join(sorted(missing))}", file=sys.stderr, flush=True)
    return 1


if __name__ == "__main__":
    sys.exit(main())
