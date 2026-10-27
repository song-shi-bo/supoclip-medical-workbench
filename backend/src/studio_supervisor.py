"""Keep the member's existing Studio services alive independently of chat turns."""
import os
from pathlib import Path
import signal
import socket
import subprocess
import time

from studio_runtime import process_lock

ROOT = Path(__file__).resolve().parents[2]
DOMAIN = ROOT.parent
LOGS = ROOT / 'backend/logs/services'
LOGS.mkdir(parents=True, exist_ok=True)
stop = False
children = {}


def listening(port):
    try:
        with socket.create_connection(('127.0.0.1', port), timeout=1):
            return True
    except OSError:
        return False


def running_module(marker):
    for path in Path('/proc').glob('[0-9]*/cmdline'):
        try:
            argv = [part.decode(errors='replace') for part in path.read_bytes().split(b'\0') if part]
            if marker in argv and argv and 'python' in Path(argv[0]).name and '--job' not in argv:
                return True
        except OSError:
            continue
    return False


def shutdown(*_):
    global stop
    stop = True


def main():
    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)
    env = dict(os.environ, PYTHONUNBUFFERED='1',
               STUDIO_TASKS_ROOT=str(ROOT / 'tasks'), VIDEO_BRAIN_URL='http://127.0.0.1:8940',
               STUDIO_ASR_LOCK=str(DOMAIN / '.studio-resources/asr.lock'))
    services = [
        ('postgres', DOMAIN / '.tools', [str(DOMAIN / '.tools/pg16/bin/postgres'), '-D', 'pgdata', '-p', '5432', '-c', 'listen_addresses=127.0.0.1'], lambda: listening(5432)),
        ('redis', ROOT, [str(DOMAIN / '.tools/redis-src/src/redis-server'), '--bind', '127.0.0.1', '--port', '6379', '--save', '', '--appendonly', 'no'], lambda: listening(6379)),
        ('frontend', ROOT / 'frontend', [str(DOMAIN / '.tools/bin/pnpm'), 'dev', '--port', '3107'], lambda: listening(3107)),
        ('frontend-proxy', ROOT / 'backend', ['.venv/bin/python', '-m', 'src.studio_frontend_proxy'], lambda: running_module('src.studio_frontend_proxy')),
        ('backend', ROOT / 'backend', ['.venv/bin/python', '-m', 'src.studio_server'], lambda: listening(8000)),
        ('video-brain', DOMAIN / 'video-brain', ['.venv/bin/uvicorn', 'app.main:app', '--host', '127.0.0.1', '--port', '8940'], lambda: listening(8940)),
        ('autoedit', ROOT, ['python3', 'backend/src/autoedit_runner_video_brain.py'], lambda: running_module('backend/src/autoedit_runner_video_brain.py')),
        ('worker', ROOT / 'backend', ['.venv/bin/python', '-m', 'src.studio_job_worker'], lambda: running_module('src.studio_job_worker')),
    ]
    with process_lock(ROOT / 'tasks/.supervisor.lock', blocking=False):
        print('Studio supervisor ready', flush=True)
        try:
            while not stop:
                for name, cwd, command, healthy in services:
                    child = children.get(name)
                    if child is not None and child.poll() is None:
                        continue
                    if healthy():
                        continue
                    if name == 'backend' and not listening(5432):
                        continue
                    if name == 'worker' and not (listening(8000) and listening(8940)):
                        continue
                    with (LOGS / f'{name}.log').open('ab') as output:
                        children[name] = subprocess.Popen(command, cwd=cwd, env=env,
                            stdout=output, stderr=subprocess.STDOUT, start_new_session=True)
                    print(f'Started {name} pid={children[name].pid}', flush=True)
                time.sleep(5)
        finally:
            for child in children.values():
                if child.poll() is None:
                    os.killpg(child.pid, signal.SIGTERM)
            for child in children.values():
                try:
                    child.wait(timeout=20)
                except subprocess.TimeoutExpired:
                    os.killpg(child.pid, signal.SIGKILL)


if __name__ == '__main__':
    main()
