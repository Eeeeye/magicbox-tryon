"""Verify real-mode protocol failures and durable state using a controlled worker test double."""
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import os
import subprocess
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from uuid import uuid4

from tryon_integration import multipart, png, port, wait_port, wait_task

PERSON, GARMENT, OUTPUT = png(2, 3), png(4, 5), png(16, 16)


class Worker(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()

    def do_POST(self):
        self.rfile.read(int(self.headers["Content-Length"]))
        self.server.entered.set()
        behavior = self.server.behavior
        if behavior == "hold":
            self.server.release.wait(10)
        if behavior == "memory":
            body, code = b'{"detail":"MODEL_MEMORY_LIMIT"}', 507
        elif behavior == "rejected":
            body, code = b'{"detail":"GARMENT_REGION_NOT_FOUND"}', 422
        else:
            body, code = (b"damaged" if behavior == "badpng" else OUTPUT), 200
        self.send_response(code)
        self.send_header("X-Request-Id", self.headers["X-Request-Id"])
        self.send_header("Content-Type", "image/png")
        self.send_header("X-Person-SHA256", "wrong" if behavior == "badhash" else hashlib.sha256(PERSON).hexdigest())
        self.send_header("X-Garment-SHA256", hashlib.sha256(GARMENT).hexdigest())
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if behavior == "slowbody":
            time.sleep(1.5)
        try:
            self.wfile.write(body)
        except OSError:
            pass

    def log_message(self, *args):
        pass


class ResultContract(unittest.TestCase):
    def setUp(self):
        self.worker = ThreadingHTTPServer(("127.0.0.1", 0), Worker)
        self.worker.behavior = "ok"
        self.worker.entered = threading.Event()
        self.worker.release = threading.Event()
        threading.Thread(target=self.worker.serve_forever, daemon=True).start()
        self.tmp = tempfile.TemporaryDirectory()
        self.java = None
        self.settings = {}
        self.start()

    def start(self):
        public = port()
        self.base = f"http://127.0.0.1:{public}"
        env = dict(os.environ, TRYON_MODE="catvton", DATA_DIR=self.tmp.name, PORT=str(public),
                   TRYON_INFERENCE_URL=f"http://127.0.0.1:{self.worker.server_port}/infer/images",
                   INFERENCE_TIMEOUT_MS="850")
        env.update(self.settings)
        self.java = subprocess.Popen(["sh", "scripts/run-java.sh"], env=env,
                                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        wait_port(public, self.java)

    def stop(self):
        self.java.kill()
        self.java.wait(timeout=5)

    def tearDown(self):
        self.worker.release.set()
        if self.java.poll() is None:
            self.stop()
        self.worker.shutdown()
        self.worker.server_close()
        self.tmp.cleanup()

    def call(self, path, create=False, category="upper", key=None):
        body, mime = multipart(PERSON, GARMENT)
        request = Request(self.base + path, data=body if create else None,
                          headers={"Content-Type": mime, "X-Garment-Category": category})
        if key is not None:
            request.add_header("Idempotency-Key", key)
        try:
            response = urlopen(request, timeout=3)
        except HTTPError as error:
            response = error
        with response:
            raw = response.read()
            data = json.loads(raw) if response.headers.get_content_type() == "application/json" else raw
            return response.status, data

    def submit(self):
        code, task = self.call("/api/tryon/tasks", create=True)
        self.assertEqual(code, 202)
        return task["taskId"]

    def test_concurrent_replay_conflict_and_persistence(self):
        def create():
            return self.call("/api/tryon/tasks", create=True, key="same-request")
        with ThreadPoolExecutor(max_workers=4) as pool:
            replies = list(pool.map(lambda _: create(), range(4)))
        self.assertTrue(all(code == 202 for code, _ in replies))
        ids = {reply["taskId"] for _, reply in replies}
        self.assertEqual(len(ids), 1)
        task_id = ids.pop()
        self.assertEqual(wait_task(self.base, task_id)["status"], "SUCCEEDED")
        self.assertEqual(self.call("/api/tryon/tasks", create=True, category="lower", key="same-request")[0], 409)
        self.stop()
        self.start()
        self.assertEqual(create()[1]["taskId"], task_id)
        self.assertEqual(self.call("/health")[1]["taskCount"], 1)

    def test_expiry_releases_history_and_idempotency_key(self):
        self.stop()
        self.settings.update(MAX_TASK_HISTORY="1", TASK_RETENTION_SECONDS="2")
        self.start()
        route = "/api/tryon/tasks"
        code, original = self.call(route, create=True, key="reusable")
        self.assertEqual(code, 202)
        old_id = original["taskId"]
        old = wait_task(self.base, old_id)
        self.assertEqual(self.call(old["result"]), (200, OUTPUT))
        self.assertEqual(self.call(route, create=True, key="reusable")[1]["taskId"], old_id)
        self.assertEqual(self.call(route, create=True, key="different"), (503, {"error": "TASK_HISTORY_FULL"}))

        deadline = time.monotonic() + 10
        while self.call(f"{route}/{old_id}")[0] != 404 and time.monotonic() < deadline:
            time.sleep(.05)
        self.assertEqual(self.call(old["result"]), (404, {"error": "TASK_NOT_FOUND"}))
        code, replacement = self.call(route, create=True, key="reusable")
        self.assertEqual(code, 202)
        self.assertNotEqual(replacement["taskId"], old_id)
        self.assertEqual(wait_task(self.base, replacement["taskId"])["status"], "SUCCEEDED")

        root = Path(self.tmp.name)
        while ((root / old_id).exists() or (root / ".expired" / old_id).exists()) and time.monotonic() < deadline:
            time.sleep(.05)
        self.assertFalse((root / old_id).exists())
        self.assertFalse((root / ".expired" / old_id).exists())

    def test_expiry_skips_running_and_queued_tasks(self):
        self.stop()
        self.settings.update(TASK_RETENTION_SECONDS="1", INFERENCE_TIMEOUT_MS="12000")
        self.start()
        self.worker.behavior = "hold"
        running = self.submit()
        self.assertTrue(self.worker.entered.wait(2))
        queued = self.submit()
        time.sleep(2.1)  # Cross two sweep intervals while the model worker is held.
        self.assertEqual(self.call(f"/api/tryon/tasks/{running}")[1]["status"], "RUNNING")
        self.assertEqual(self.call(f"/api/tryon/tasks/{queued}")[1]["status"], "QUEUED")
        self.worker.release.set()
        for task_id in (running, queued):
            self.assertEqual(wait_task(self.base, task_id)["status"], "SUCCEEDED")

    def test_restart_recovers_interrupted_cleanup_and_old_failed_tasks(self):
        route = "/api/tryon/tasks"
        _, old_success = self.call(route, create=True, key="previous-key")
        success_id = old_success["taskId"]
        self.assertEqual(wait_task(self.base, success_id)["status"], "SUCCEEDED")
        self.worker.behavior = "badpng"
        failed_id = self.submit()
        self.assertEqual(wait_task(self.base, failed_id)["status"], "FAILED")
        self.stop()

        root = Path(self.tmp.name)
        (root / success_id).rename(root / ".expired" / success_id)
        partial = root / str(uuid4())
        partial.mkdir()
        (partial / "person.bin").write_bytes(PERSON)
        (partial / ".task-12345.tmp").write_bytes(b"partial journal")
        unrelated = root / str(uuid4())
        unrelated.mkdir()
        (unrelated / "keep.txt").write_text("not an upload")
        journal = root / failed_id / "task.properties"
        # Older terminal records can contain only the last update timestamp.
        rows = [row for row in journal.read_text().splitlines()
                if not row.startswith(("completedAt=", "updatedAt="))]
        journal.write_text("\n".join(rows + ["updatedAt=2000-01-01T00:00:00Z"]) + "\n")
        self.worker.behavior = "ok"
        self.start()

        self.assertFalse(partial.exists())
        self.assertFalse((root / ".expired" / partial.name).exists())
        self.assertTrue((unrelated / "keep.txt").is_file())
        for task_id in (success_id, failed_id):
            self.assertEqual(self.call(f"{route}/{task_id}")[0], 404)
            self.assertFalse((root / task_id).exists())
            self.assertFalse((root / ".expired" / task_id).exists())
        code, replacement = self.call(route, create=True, key="previous-key")
        self.assertEqual(code, 202)
        self.assertNotEqual(replacement["taskId"], success_id)
        self.assertEqual(wait_task(self.base, replacement["taskId"])["status"], "SUCCEEDED")

    def test_protocol_and_restart(self):
        self.assertEqual(self.call("/health/ready")[0], 200)
        self.assertEqual(self.call("/api/tryon/tasks", create=True, category="invalid")[0], 400)
        for behavior, error in (("badpng", "INVALID_RESULT_IMAGE"), ("badhash", "INFERENCE_INPUT_HASH_MISMATCH"),
                                ("rejected", "GARMENT_REGION_NOT_FOUND"), ("memory", "MODEL_MEMORY_LIMIT"),
                                ("slowbody", "INFERENCE_TIMEOUT")):
            with self.subTest(behavior=behavior):
                self.worker.behavior = behavior
                task_id = self.submit()
                task = wait_task(self.base, task_id)
                self.assertEqual((task["status"], task["error"]), ("FAILED", error))
                self.assertEqual(self.call(f"/api/tryon/tasks/{task_id}/result")[0], 409)
        self.worker.behavior = "ok"
        task_id = self.submit()
        done = wait_task(self.base, task_id)
        self.assertEqual(done["mode"], "catvton")
        self.assertEqual((done["status"], done["category"]), ("SUCCEEDED", "upper"))
        self.assertEqual(self.call(done["result"]), (200, OUTPUT))
        self.worker.entered.clear()
        self.worker.behavior = "hold"
        interrupted = self.submit()
        self.assertTrue(self.worker.entered.wait(2))
        queued = self.submit()
        self.stop()
        self.worker.release.set()
        self.start()
        self.assertEqual(self.call(done["result"]), (200, OUTPUT))
        self.assertEqual(self.call(f"/api/tryon/tasks/{task_id}")[1], done)
        for unfinished in (interrupted, queued):
            self.assertEqual(wait_task(self.base, unfinished)["error"], "SERVER_RESTARTED")
            self.assertFalse((Path(self.tmp.name) / unfinished / "result.png").exists())


if __name__ == "__main__":
    unittest.main()
