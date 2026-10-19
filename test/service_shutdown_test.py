"""Exercise real process trees and ROS readiness on an isolated local domain."""
import importlib.util
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock

os.environ['ROS_DOMAIN_ID'] = '198'
os.environ['ROS_AUTOMATIC_DISCOVERY_RANGE'] = 'LOCALHOST'
os.environ['RMW_IMPLEMENTATION'] = 'rmw_fastrtps_cpp'
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
from process_shutdown import group_alive, stop_processes
import rclpy
from rclpy.executors import SingleThreadedExecutor
spec = importlib.util.spec_from_file_location('shutdown_supervisor', ROOT / 'scripts/navigation_supervisor.py')
supervisor = importlib.util.module_from_spec(spec)
spec.loader.exec_module(supervisor)


class ProcessShutdown(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='bringup-shutdown-')
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)
        self.processes = []
        self.addCleanup(self.cleanup)

    def cleanup(self):
        for process in self.processes:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.wait(timeout=3)

    def launch(self, source, name):
        script = self.directory / f'{name}.py'
        ready = self.directory / f'{name}.ready'
        script.write_text(source)
        process = subprocess.Popen([sys.executable, str(script), str(ready)], start_new_session=True)
        self.processes.append(process)
        deadline = time.monotonic() + 5
        while not ready.exists() and time.monotonic() < deadline:
            time.sleep(.01)
        self.assertTrue(ready.exists())
        return process

    def test_interrupt_is_sent_only_to_launcher(self):
        counts = self.directory / 'child.signals'
        child = self.directory / 'child.py'
        child.write_text('''import signal,time,sys
from pathlib import Path
p=Path(sys.argv[1])
def stop(*_):
    p.write_text(p.read_text()+'I')
    time.sleep(.15)
    raise SystemExit(0)
signal.signal(signal.SIGINT,stop)
p.write_text('')
while True:time.sleep(.05)
''')
        process = self.launch(f'''import subprocess,signal,sys,time
from pathlib import Path
child=subprocess.Popen([sys.executable,{str(child)!r},{str(counts)!r}])
while not Path({str(counts)!r}).exists():time.sleep(.01)
def stop(*_):
    child.send_signal(signal.SIGINT)
    child.wait()
    raise SystemExit(0)
signal.signal(signal.SIGINT,stop)
Path(sys.argv[1]).touch()
while True:time.sleep(.05)
''', 'launcher')
        stop_processes({'launcher': process}, Mock(), interrupt_timeout=1)
        self.assertEqual(process.returncode, 0)
        self.assertEqual(counts.read_text(), 'I')
        self.assertFalse(group_alive(process))

    def test_stubborn_workers_share_one_deadline(self):
        processes = {str(i): self.launch('''import signal,time,sys
from pathlib import Path
signal.signal(signal.SIGINT,signal.SIG_IGN)
signal.signal(signal.SIGTERM,signal.SIG_IGN)
Path(sys.argv[1]).touch()
while True:time.sleep(.1)
''', str(i)) for i in range(3)}
        started = time.monotonic()
        stop_processes(processes, Mock(), interrupt_timeout=.2, terminate_timeout=.2, kill_timeout=1)
        self.assertLess(time.monotonic() - started, 1.2)
        self.assertTrue(all(p.returncode == -signal.SIGKILL for p in processes.values()))

    def test_orphan_child_is_cleaned_up_after_launcher_exits(self):
        process = self.launch('''import subprocess,sys
subprocess.Popen([sys.executable,'-c',"import signal,time,sys; from pathlib import Path; signal.signal(signal.SIGINT,signal.SIG_IGN); signal.signal(signal.SIGTERM,signal.SIG_IGN); Path(sys.argv[1]).touch(); time.sleep(60)",sys.argv[1]])
''', 'orphan')
        process.wait(timeout=3)
        self.assertTrue(group_alive(process))
        stop_processes({'orphan': process}, Mock(), interrupt_timeout=.1, terminate_timeout=.1)
        self.assertFalse(group_alive(process))

    def test_active_readiness_command_is_cancelled_on_shutdown(self):
        node = object.__new__(supervisor.NavigationSupervisor)
        node.shutdown_event = threading.Event()
        node.worker_lock = threading.Lock()
        node.helpers = {}
        result = []
        thread = threading.Thread(target=lambda: result.append(node.run_helper(
            [sys.executable, '-c', 'import time; time.sleep(60)'])))
        thread.start()
        deadline = time.monotonic() + 3
        while not node.helpers and time.monotonic() < deadline:
            time.sleep(.01)
        self.assertTrue(node.helpers)
        processes = {str(pid): process for pid, process in node.helpers.items()}
        self.processes.extend(processes.values())
        node.shutdown_event.set()
        stop_processes(processes, Mock(), interrupt_timeout=1)
        thread.join(timeout=3)
        self.assertFalse(thread.is_alive())
        self.assertNotEqual(result[0].returncode, 0)

    def test_supervisor_cleanup_survives_repeated_signals(self):
        config = self.directory / 'navigate.yaml'
        config.write_text('''bringup:
  actions:
    stand: {type: trigger_service, service: /unused}
  modes:
    manual: {sequence: [nav_bridge], bridge_only_sequence: [nav_bridge]}
''')
        child_ready = self.directory / 'worker.ready'
        worker = self.directory / 'worker.py'
        worker.write_text('''import signal,time,sys
from pathlib import Path
def stop(*_):
    time.sleep(.4)
    raise SystemExit(0)
signal.signal(signal.SIGINT,stop)
Path(sys.argv[1]).touch()
while True:time.sleep(.1)
''')
        process = self.launch(f'''import sys,subprocess,time
from pathlib import Path
from types import SimpleNamespace
sys.path.insert(0,{str(ROOT / 'scripts')!r})
import navigation_supervisor as s
Original=s.NavigationSupervisor
class Supervisor(Original):
    def __init__(self,*args):
        super().__init__(*args)
        self.monitor_timer.cancel()
        child=subprocess.Popen([sys.executable,{str(worker)!r},{str(child_ready)!r}],start_new_session=True)
        self.workers['base']={{'process':child}}
        while not Path({str(child_ready)!r}).exists():time.sleep(.01)
        Path(sys.argv[1]).touch()
s.NavigationSupervisor=Supervisor
raise SystemExit(s.run_node(SimpleNamespace(config={str(config)!r},service_name='/shutdown_test/start',state_dir={str(self.directory / 'state')!r},result_timeout=0)))
''', 'supervisor')
        process.send_signal(signal.SIGINT)
        time.sleep(.1)
        process.send_signal(signal.SIGINT)
        process.send_signal(signal.SIGTERM)
        self.assertEqual(process.wait(timeout=5), 0)

    def test_hardware_wrapper_ignores_repeated_stop_signals(self):
        import json
        counts = self.directory / 'hardware.signals'
        ready = self.directory / 'hardware.ready'
        agent = self.directory / 'agent'
        agent.write_text(f'''#!{sys.executable}
import os,signal,time
from pathlib import Path
assert os.environ.get('SHUTDOWN_TEST_OVERLAY') == 'loaded'
def stop(*_):
    Path({str(counts)!r}).write_text('T')
    time.sleep(.4)
    raise SystemExit(0)
signal.signal(signal.SIGTERM,stop)
Path({str(ready)!r}).touch()
while True:time.sleep(.1)
''')
        agent.chmod(0o755)
        setup = self.directory / 'setup.bash'
        setup.write_text('export SHUTDOWN_TEST_OVERLAY=loaded\n')
        config = self.directory / 'services.yaml'
        config.write_text(json.dumps({'service': {'setup_paths': [str(setup)]}, 'hardware': {
            'processes': [{'name': 'mock', 'command': str(agent)}]}}))
        rendered = subprocess.run(['bash', str(ROOT / 'scripts/manage_inspection_services.sh'),
            '--config', str(config), 'render'], capture_output=True, text=True, check=True)
        import shutil
        output = Path(rendered.stdout.strip())
        self.addCleanup(shutil.rmtree, output)
        process = subprocess.Popen(['bash', str(output / 'wrappers/run_inspection_hardware.sh')],
                                   start_new_session=True)
        self.processes.append(process)
        deadline = time.monotonic() + 5
        while not ready.exists() and time.monotonic() < deadline:
            time.sleep(.01)
        self.assertTrue(ready.exists())
        process.send_signal(signal.SIGTERM)
        deadline = time.monotonic() + 3
        while not counts.exists() and time.monotonic() < deadline:
            time.sleep(.01)
        self.assertTrue(counts.exists())
        process.send_signal(signal.SIGTERM)
        process.send_signal(signal.SIGINT)
        self.assertEqual(process.wait(timeout=5), 0)


class NodeReadiness(unittest.TestCase):
    def test_cancelled_readiness_stays_unsuccessful_without_traceback(self):
        rclpy.init()
        observer = rclpy.create_node('cancel_readiness_observer')
        try:
            for signum in (signal.SIGINT, signal.SIGTERM):
                process = subprocess.Popen([sys.executable, str(ROOT / 'scripts/wait_for_ready.py'),
                    'nodes', '/shutdown_test/missing', '--timeout', '0'],
                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
                try:
                    deadline = time.monotonic() + 5
                    while 'wait_for_navigation_nodes' not in [
                            name for name, _ in observer.get_node_names_and_namespaces()]:
                        self.assertIsNone(process.poll())
                        self.assertLess(time.monotonic(), deadline)
                        rclpy.spin_once(observer, timeout_sec=.05)
                    process.send_signal(signum)
                    output, _ = process.communicate(timeout=5)
                    self.assertNotEqual(process.returncode, 0, output)
                    self.assertNotIn('Traceback', output)
                    self.assertNotIn('nodes ready', output)
                finally:
                    if process.poll() is None:
                        process.kill()
                    process.communicate()
        finally:
            observer.destroy_node()
            rclpy.try_shutdown()

    def test_namespaced_discovery_and_timeout_do_not_spawn_daemon(self):
        rclpy.init()
        node = rclpy.create_node('shutdown_readiness_mock', namespace='/shutdown_test')
        executor = SingleThreadedExecutor()
        executor.add_node(node)
        thread = threading.Thread(target=executor.spin)
        thread.start()
        try:
            for nodes, expected in [('/shutdown_test/shutdown_readiness_mock', 0),
                                    ('/shutdown_test/missing', 1)]:
                result = subprocess.run([sys.executable, str(ROOT / 'scripts/wait_for_ready.py'),
                    'nodes', nodes, '--timeout', '2', '--name', 'shutdown_test'],
                    capture_output=True, text=True, timeout=8)
                self.assertEqual(result.returncode, expected, result.stdout + result.stderr)
            self.assertFalse(any(name.startswith('_ros2cli_daemon')
                                 for name, _ in node.get_node_names_and_namespaces()))
        finally:
            executor.shutdown(timeout_sec=3)
            thread.join(timeout=3)
            node.destroy_node()
            rclpy.shutdown()


if __name__ == '__main__':
    unittest.main()
