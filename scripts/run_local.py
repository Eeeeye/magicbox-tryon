"""Launch the local model and public Java API together; Ctrl-C stops both."""
import json
import os
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path
from urllib.error import URLError
from urllib.request import urlopen

ROOT = Path(__file__).resolve().parents[1]
os.chdir(ROOT)
port = int(os.getenv("PORT", "8780"))
model_port = int(os.getenv("MODEL_PORT", "8781"))
if port == model_port:
    raise SystemExit("PORT and MODEL_PORT must differ")
for candidate in (port, model_port):
    with socket.socket() as probe:
        probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        probe.bind(("127.0.0.1", candidate))
env = dict(os.environ, TRYON_MODE="catvton", PORT=str(port), HF_HUB_OFFLINE="1",
           HF_HUB_DISABLE_TELEMETRY="1", TOKENIZERS_PARALLELISM="false",
           TRYON_INFERENCE_URL=f"http://127.0.0.1:{model_port}/infer/images")
children = []


def stop(*_):
    raise SystemExit(0)


def ensure_running():
    for child in children:
        if child.poll() is not None:
            raise SystemExit(f"Service exited unexpectedly with status {child.returncode}")


def wait_ready(url, timeout):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        ensure_running()
        try:
            with urlopen(url, timeout=2) as response:
                info = json.load(response)
                if info.get("status") == "READY":
                    return info
        except (URLError, TimeoutError, OSError):
            pass
        time.sleep(0.5)
    raise SystemExit(f"Startup timed out waiting for {url}")


signal.signal(signal.SIGTERM, stop)
signal.signal(signal.SIGINT, stop)
try:
    children.append(subprocess.Popen([sys.executable, "-m", "uvicorn", "inference.server:app",
        "--host", "127.0.0.1", "--port", str(model_port), "--no-access-log"], env=env))
    model = wait_ready(f"http://127.0.0.1:{model_port}/ready", 180)
    children.append(subprocess.Popen(["sh", "scripts/run-java.sh"], env=env))
    wait_ready(f"http://127.0.0.1:{port}/health/ready", 45)
    print(f"Try-on ready at http://127.0.0.1:{port}/  {model}", flush=True)
    while True:
        ensure_running()
        time.sleep(1)
finally:
    for child in reversed(children):
        if child.poll() is None:
            child.terminate()
    for child in reversed(children):
        try:
            child.wait(timeout=8)
        except subprocess.TimeoutExpired:
            child.kill()
            child.wait()
