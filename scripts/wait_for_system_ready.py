#!/usr/bin/env python3
"""Confirm TaskHub is responding before systemd reports system startup complete."""
import argparse
import sys
import time

from bringup_config import load_config


def wait_for_system_ready(timeout=30.0):
    import rclpy
    from inspection_interfaces.srv import GetStatus, StartRoute
    from rclpy.node import NodeNameNonExistentError

    deadline = time.monotonic() + timeout
    print(f"[system] waiting up to {timeout:g}s for TaskHub and /start_route ...", flush=True)
    rclpy.init(args=[])
    node = rclpy.create_node("wait_for_inspection_system_ready")
    start = node.create_client(StartRoute, "/start_route")
    status = node.create_client(GetStatus, "/get_status")
    future = None
    try:
        while rclpy.ok() and time.monotonic() < deadline:
            try:
                services = dict(node.get_service_names_and_types_by_node("task_hub_node", "/"))
            except NodeNameNonExistentError:
                services = {}
            if ("inspection_interfaces/srv/StartRoute" in services.get("/start_route", [])
                    and start.service_is_ready() and status.service_is_ready()):
                if future is None:
                    # Read-only request: stale discovery alone cannot pass readiness.
                    future = status.call_async(GetStatus.Request())
                if future.done():
                    if future.result() is None:
                        raise RuntimeError("/get_status returned no response")
                    print("[system] TaskHub responding; /start_route ready", flush=True)
                    return True
            rclpy.spin_once(node, timeout_sec=min(0.2, max(0.0, deadline - time.monotonic())))
    finally:
        node.destroy_node()
        rclpy.try_shutdown()
    print("[system] startup timed out: TaskHub or /start_route not ready", file=sys.stderr, flush=True)
    return False


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default="")
    parser.add_argument("--task-hub-enabled", default="")
    parser.add_argument("--timeout", type=float, default=30.0)
    args = parser.parse_args()
    try:
        config = load_config("system", args.config, resolve=False)
        enabled = args.task_hub_enabled or config.get("modules", {}).get("task_hub", True)
        if str(enabled).lower() in ("false", "0", "no", "off"):
            print("[system] TaskHub disabled; skipping readiness check", flush=True)
            return 0
        return 0 if wait_for_system_ready(args.timeout) else 1
    except Exception as exc:
        print(f"[system] startup readiness failed: {exc}", file=sys.stderr, flush=True)
        return 1


if __name__ == "__main__":
    sys.exit(main())
