"""Bounded shutdown for launchers started in their own process sessions."""
import os
from pathlib import Path
import signal
import time


def group_alive(process):
    if process.poll() is None:
        return True
    try:
        os.killpg(process.pid, 0)
    except ProcessLookupError:
        return False
    # Orphaned zombies cannot handle signals; do not spend the shutdown budget on them.
    for entry in Path('/proc').iterdir():
        try:
            fields = (entry / 'stat').read_text().rsplit(')', 1)[1].split()
            if int(fields[2]) == process.pid and fields[0] != 'Z':
                return True
        except (OSError, ValueError, IndexError):
            continue
    return False


def stop_processes(processes, logger, interrupt_timeout=10.0, terminate_timeout=5.0,
                   kill_timeout=2.0):
    """Signal every launcher before waiting; escalate remaining process groups together."""
    pending = dict(processes)
    for signum, timeout in ((signal.SIGINT, interrupt_timeout),
                            (signal.SIGTERM, terminate_timeout),
                            (signal.SIGKILL, kill_timeout)):
        pending = {name: process for name, process in pending.items() if group_alive(process)}
        if not pending:
            return
        for name, process in pending.items():
            logger.info(f'stopping {name} pid={process.pid}: {signal.Signals(signum).name}')
            try:
                if signum == signal.SIGINT and process.poll() is None:
                    # The launch process forwards SIGINT to its children exactly once.
                    process.send_signal(signum)
                else:
                    os.killpg(process.pid, signum)
            except ProcessLookupError:
                pass
        deadline = time.monotonic() + timeout
        while pending and time.monotonic() < deadline:
            pending = {name: process for name, process in pending.items() if group_alive(process)}
            if pending:
                time.sleep(0.05)
        if pending:
            logger.warning('still stopping after ' + signal.Signals(signum).name + ': ' + ', '.join(pending))
    if pending:
        raise RuntimeError('process groups did not exit after SIGKILL: ' + ', '.join(pending))
