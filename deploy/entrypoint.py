"""Supervise one bridge/MCP pair; crash either side and restart the whole unit."""
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

os.umask(0o077)
root = Path(os.environ.get('WHATSAPP_STORE_DIR', '/data'))
root.mkdir(mode=0o700, exist_ok=True)
(root / 'outbox').mkdir(mode=0o700, exist_ok=True)
children = []
stopping = False
failed = False

def stop(*_):
    global stopping
    stopping = True
    for process in children:
        if process.poll() is None:
            process.terminate()

signal.signal(signal.SIGTERM, stop)
signal.signal(signal.SIGINT, stop)
try:
    children.append(subprocess.Popen(['/app/whatsapp-bridge', '--store-dir', str(root),
        '--port', '8753', '--log-level', 'WARN', '--no-pair']))
    children.append(subprocess.Popen([sys.executable, '/app/server/main.py', '--hosted-http', '--port', '8754']))
    while not stopping and all(p.poll() is None for p in children):
        time.sleep(0.25)
    failed = not stopping
finally:
    stop()
    for process in children:
        try:
            process.wait(timeout=15)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
sys.exit(1 if failed else 0)
