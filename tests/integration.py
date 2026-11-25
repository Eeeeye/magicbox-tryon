"""Exercise the two real local HTTP processes, including failure and overload."""
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
os.chdir(ROOT)

def port():
    with socket.socket() as s:
        s.bind(('127.0.0.1', 0))
        return s.getsockname()[1]

def request(url, data=None):
    try:
        with urllib.request.urlopen(urllib.request.Request(url, data=data), timeout=2) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())

def wait_port(p, process):
    end = time.monotonic() + 15
    while time.monotonic() < end:
        if process.poll() is not None:
            raise AssertionError('Service exited during startup')
        try:
            with socket.create_connection(('127.0.0.1', p), timeout=.1):
                return
        except OSError:
            time.sleep(.05)
    raise AssertionError('Service startup timed out')

def case(behavior, expected):
    jp, mp = port(), port()
    processes = []
    data = tempfile.TemporaryDirectory(prefix="tryon-demo-test-")
    try:
        if behavior != 'unavailable':
            mock = subprocess.Popen([sys.executable, 'mock/inference.py'],
                env=dict(os.environ, MOCK_PORT=str(mp), MOCK_BEHAVIOR=behavior),
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            processes.append(mock)
            wait_port(mp, mock)
        java = subprocess.Popen(['sh', 'scripts/run-java.sh'],
            env=dict(os.environ, TRYON_MODE='mock', DATA_DIR=data.name, PORT=str(jp), INFERENCE_URL=f'http://127.0.0.1:{mp}/infer'),
            stdout=subprocess.DEVNULL)
        processes.append(java)
        wait_port(jp, java)
        url = f'http://127.0.0.1:{jp}/api/demo/tasks'
        assert request(url + '/missing')[0] == 404
        assert request(url, b'unexpected body')[0] == 400
        code, task = request(url, b'')
        assert code == 202 and task['status'] == 'QUEUED' and task['mode'] == 'mock'
        if behavior == 'timeout':
            codes = [request(url, b'')[0] for _ in range(7)]
            assert 503 in codes, codes
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            _, state = request(url + '/' + task['taskId'])
            if state['status'] in ('SUCCEEDED', 'FAILED'):
                break
            time.sleep(.05)
        assert state['error'] == expected, state
        assert state['status'] == ('FAILED' if expected else 'SUCCEEDED'), state
        assert state['result'] == (None if expected else 'MOCK_ONLY_NO_IMAGE_GENERATED')
        print('PASS', behavior, state['status'], state['error'], flush=True)
    finally:
        for p in reversed(processes):
            p.terminate()
        for p in reversed(processes):
            try:
                p.wait(timeout=5)
            except subprocess.TimeoutExpired:
                p.kill(); p.wait()
        data.cleanup()

for behavior, expected in [
    ('success', ''), ('error', 'INFERENCE_HTTP_ERROR'),
    ('invalid', 'INVALID_INFERENCE_RESPONSE'),
    ('timeout', 'INFERENCE_TIMEOUT'), ('unavailable', 'INFERENCE_UNAVAILABLE')
]:
    case(behavior, expected)
print('All integration checks passed; test services stopped.')
