from dataclasses import dataclass
import threading
import time


LIDAR_OK = 1
RTK_DEGRADED = 2
UNAVAILABLE = 3
MAX_AGE = 0.5
LOSS_CONFIRMATION = 2.0
WAIT_TIMEOUT = 3.0


@dataclass(frozen=True)
class HealthSample:
    source: str
    sequence: int
    mode: int
    reason: str
    received_at: float
    loss_since: float | None


class LocalizationHealth:
    def __init__(self, clock, shutdown_event):
        self.clock = clock
        self.shutdown_event = shutdown_event
        self.condition = threading.Condition()
        self.sample = None
        self.retired_sources = set()

    def observe(self, msg):
        statuses = [status for status in msg.status if status.name == "localization/navigation_health"]
        if len(statuses) != 1:
            return
        values = {item.key: item.value for item in statuses[0].values}
        if len(values) != len(statuses[0].values) or values.get("schema_version") != "1":
            return
        mode = values.get("mode")
        reason = values.get("reason")
        source = values.get("source_instance")
        sequence = values.get("sequence", "")
        if mode not in ("0", "1", "2", "3") or not reason or not source:
            return
        if not sequence.isascii() or not sequence.isdecimal() or len(sequence) > 20:
            return
        sequence = int(sequence)
        if not 0 < sequence < 2**64:
            return
        stamp = msg.header.stamp
        published = stamp.sec * 10**9 + stamp.nanosec
        age = (self.clock.now().nanoseconds - published) / 10**9
        if published <= 0 or stamp.nanosec >= 10**9 or not -0.05 <= age <= MAX_AGE:
            return
        mode = int(mode)
        lidar_reason = values.get("lidar_reason") or reason
        loss = (mode == UNAVAILABLE and not values.get("watchdog_reasons") and
                values.get("localization_reset") != "true" and
                not reason.startswith("scan_stamp_") and reason != "processing_timeout" and
                reason != "localization_reset" and
                (reason == "state_nonfinite" or values.get("finite_state") == "false" or
                 lidar_reason.startswith(("tracking_", "recovery_")) or
                 lidar_reason in ("no_effective_correspondences", "position_correction_exceeded",
                                  "rotation_correction_exceeded", "lidar_recovering")))
        with self.condition:
            previous = self.sample
            if source in self.retired_sources:
                return
            if previous and source == previous.source and sequence <= previous.sequence:
                return
            if previous and source != previous.source:
                self.retired_sources.add(previous.source)
            now = time.monotonic()
            loss_since = now if loss else None
            if (loss and previous and source == previous.source and previous.loss_since is not None and
                    now - previous.received_at <= MAX_AGE):
                loss_since = previous.loss_since
            self.sample = HealthSample(source, sequence, mode, reason, now, loss_since)
            self.condition.notify_all()

    def before_start(self):
        with self.condition:
            return time.monotonic(), self.sample.source if self.sample else None

    def wait(self, start=None):
        """Return usable, lost, or unconfirmed health for a task preparation."""
        deadline = time.monotonic() + WAIT_TIMEOUT
        with self.condition:
            while not self.shutdown_event.is_set():
                now = time.monotonic()
                sample = self.sample
                reason = "no fresh /localization_health message"
                if sample and now - sample.received_at <= MAX_AGE:
                    reason = sample.reason
                    if start is None:
                        if sample.mode in (LIDAR_OK, RTK_DEGRADED):
                            return "usable", reason
                        if sample.loss_since is not None and now - sample.loss_since >= LOSS_CONFIRMATION:
                            return "lost", reason
                    elif sample.received_at >= start[0] and sample.source != start[1]:
                        if sample.mode == LIDAR_OK:
                            return "usable", reason
                    else:
                        reason = "waiting for a new SLAM instance"
                remaining = deadline - now
                if remaining <= 0:
                    return "unconfirmed", reason
                self.condition.wait(min(0.2, remaining))
        return "unconfirmed", "navigation is shutting down"
