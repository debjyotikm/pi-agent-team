"""Native workstation commands using the controller's Unix account and access."""
import os
import signal
import subprocess
import threading
from pathlib import Path


class HostExecutor:
    def __init__(self, workspace, source=None):
        self.workspace = Path(workspace).resolve()
        self.source = Path(source).resolve() if source else None
        self.lock = threading.RLock()
        self.children = {}
        self.closed = False

    def environment(self, cwd):
        env = os.environ.copy()
        # Keep the original environment's installed packages, but import this working copy's source.
        if Path(cwd).is_relative_to(self.workspace):
            if self.source and (self.source / '.venv/bin/python').exists():
                env['VIRTUAL_ENV'] = str(self.source / '.venv')
                env['PATH'] = str(self.source / '.venv/bin') + os.pathsep + env.get('PATH', '')
            paths = [str(self.workspace / 'src'), str(self.workspace)]
            if env.get('PYTHONPATH'): paths.append(env['PYTHONPATH'])
            env['PYTHONPATH'] = os.pathsep.join(paths)
        return env

    @staticmethod
    def terminate(process):
        # Only kill the process group created for this tool call, never unrelated host work.
        try: os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError: return
        try: process.wait(timeout=2)
        except subprocess.TimeoutExpired: pass
        # Also stop descendants if the shell exited before its children.
        try: os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError: pass

    def execute(self, command, timeout=600, cwd=None):
        directory = Path(cwd).expanduser() if cwd else self.workspace
        if not directory.is_absolute(): directory = self.workspace / directory
        directory = directory.resolve()
        with self.lock:
            if self.closed: raise RuntimeError('This worker was interrupted; resume or reassign it')
            process = subprocess.Popen(['bash', '-e', '-o', 'pipefail', '-lc', command], cwd=directory,
                env=self.environment(directory), stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                text=True, errors='replace', start_new_session=True)
            self.children[process.pid] = process
        timed_out = False
        try:
            try: stdout, stderr = process.communicate(timeout=timeout)
            except subprocess.TimeoutExpired:
                timed_out = True; self.terminate(process); stdout, stderr = process.communicate()
            return {'exit_code': 124 if timed_out else process.returncode, 'stdout': stdout,
                    'stderr': stderr, 'cwd': str(directory), 'execution_mode': 'host', 'timed_out': timed_out}
        finally:
            with self.lock: self.children.pop(process.pid, None)

    def interrupt(self):
        with self.lock:
            self.closed = True
            children = list(self.children.values())
        for process in children: self.terminate(process)

    close = interrupt
