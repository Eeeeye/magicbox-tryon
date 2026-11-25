"""Contract-only inference backend; it never loads a model or generates a try-on image."""
import hashlib
import json
import os
import time
from email import policy
from email.parser import BytesParser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

MOCK_RESULT = b"MOCK_ONLY_NO_IMAGE_GENERATED"


def parse_images(content_type, body):
    if not content_type.lower().startswith("multipart/form-data"):
        raise ValueError("MULTIPART_REQUIRED")
    raw = b"Content-Type: " + content_type.encode("latin1") + b"\r\nMIME-Version: 1.0\r\n\r\n" + body
    message = BytesParser(policy=policy.default).parsebytes(raw)
    if not message.is_multipart():
        raise ValueError("MULTIPART_INVALID")
    found = {}
    for part in message.iter_parts():
        disposition = part.get("Content-Disposition", "")
        name = part.get_param("name", header="content-disposition")
        filename = part.get_filename()
        if name not in ("person", "garment") or not filename or name in found:
            raise ValueError("IMAGE_PARTS_INVALID")
        payload = part.get_payload(decode=True)
        if not payload:
            raise ValueError("EMPTY_IMAGE")
        found[name] = payload
    if set(found) != {"person", "garment"}:
        raise ValueError("TWO_IMAGES_REQUIRED")
    return found


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        if os.environ.get("MOCK_LOG", ""):
            super().log_message(fmt, *args)

    def do_POST(self):
        length = int(self.headers.get("Content-Length", "0"))
        if length < 0 or length > 12 * 1024 * 1024:
            self.respond(413, b'{"error":"REQUEST_BODY_TOO_LARGE"}')
            return
        body = self.rfile.read(length)
        behavior = os.environ.get("MOCK_BEHAVIOR", "success")
        if self.path == "/infer":
            if length > 1024:
                self.respond(413, b'{"error":"LEGACY_BODY_TOO_LARGE"}')
                return
            self.respond_legacy(behavior)
            return
        if self.path != "/infer/images":
            self.respond(404, b'{"error":"NOT_FOUND"}')
            return
        try:
            images = parse_images(self.headers.get("Content-Type", ""), body)
        except ValueError as exc:
            self.respond(400, json.dumps({"error": str(exc)}).encode())
            return
        hashes = {key: hashlib.sha256(value).hexdigest() for key, value in images.items()}
        audit = os.environ.get("MOCK_AUDIT_FILE")
        if audit:
            with open(audit, "a", encoding="utf-8") as stream:
                stream.write(json.dumps({"personSha256": hashes["person"], "garmentSha256": hashes["garment"],
                    "personBytes": len(images["person"]), "garmentBytes": len(images["garment"])}) + "\n")
        if behavior == "timeout":
            time.sleep(5)
        if behavior == "error":
            self.respond(500, b'{"error":"INFERENCE_FAILED"}')
            return
        response = b"INVALID" if behavior == "invalid" else MOCK_RESULT
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", str(len(response)))
        self.send_header("X-Person-SHA256", hashes["person"])
        self.send_header("X-Garment-SHA256", hashes["garment"])
        self.end_headers()
        self.wfile.write(response)

    def respond_legacy(self, behavior):
        if behavior == "timeout":
            time.sleep(5)
        body = b"INVALID" if behavior == "invalid" else MOCK_RESULT
        self.send_response(500 if behavior == "error" else 200)
        self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def respond(self, status, body):
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


if __name__ == "__main__":
    port = int(os.environ.get("MOCK_PORT", "8091"))
    print(f"Mock inference: http://127.0.0.1:{port}; no images generated", flush=True)
    ThreadingHTTPServer(("127.0.0.1", port), Handler).serve_forever()
