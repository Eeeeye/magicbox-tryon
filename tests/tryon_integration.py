"""End-to-end checks for the image upload contract and mock inference boundary."""
import hashlib, json, os, socket, struct, subprocess, sys, tempfile, time, urllib.error, urllib.request, zlib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
os.chdir(ROOT)
BOUNDARY = "----tryon-test-boundary"

def port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0)); return s.getsockname()[1]

def wait_port(p, process):
    end=time.monotonic()+15
    while time.monotonic()<end:
        if process.poll() is not None: raise AssertionError("service exited during startup")
        try:
            with socket.create_connection(("127.0.0.1",p),.1): return
        except OSError: time.sleep(.05)
    raise AssertionError("startup timeout")

def request(url, method="GET", body=None, content_type=None):
    req=urllib.request.Request(url, data=body, method=method)
    if content_type: req.add_header("Content-Type",content_type)
    try:
        with urllib.request.urlopen(req,timeout=3) as r: return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e: return e.code, json.loads(e.read())

def chunk(kind, payload):
    return struct.pack(">I", len(payload))+kind+payload+struct.pack(">I", zlib.crc32(kind+payload)&0xffffffff)

def png(width=2,height=3):
    signature=b"\x89PNG\r\n\x1a\n"
    ihdr=struct.pack(">IIBBBBB",width,height,8,2,0,0,0)
    raw=b"".join(b"\x00"+b"\x11\x22\x33"*width for _ in range(height))
    return signature+chunk(b"IHDR",ihdr)+chunk(b"IDAT",zlib.compress(raw))+chunk(b"IEND",b"")

def multipart(person, garment, fields=("person","garment"), duplicate=False):
    boundary=BOUNDARY
    parts=[]
    for name,data in zip(fields,(person,garment)):
        parts.append((f"--{boundary}\r\nContent-Disposition: form-data; name=\"{name}\"; filename=\"{name}.png\"\r\nContent-Type: image/png\r\n\r\n".encode()+data+b"\r\n"))
    if duplicate: parts.append((f"--{boundary}\r\nContent-Disposition: form-data; name=\"person\"; filename=\"again.png\"\r\nContent-Type: image/png\r\n\r\n".encode()+person+b"\r\n"))
    return b"".join(parts)+f"--{boundary}--\r\n".encode(),"multipart/form-data; boundary="+boundary

def wait_task(base, task_id):
    end=time.monotonic()+8
    while time.monotonic()<end:
        _, data=request(f"{base}/api/tryon/tasks/{task_id}")
        if data.get("status") in ("SUCCEEDED","FAILED"): return data
        time.sleep(.05)
    raise AssertionError("task did not reach terminal state")

def run_queue_overload(person, garment, root):
    """The image endpoint must reject work once its worker and bounded queue are full."""
    jp, mp = port(), port()
    data_dir = Path(root) / "queue-data"
    env = dict(os.environ, TRYON_MODE="mock", PORT=str(jp), MOCK_PORT=str(mp), DATA_DIR=str(data_dir),
               QUEUE_CAPACITY="1", INFERENCE_TIMEOUT_MS="5000",
               TRYON_INFERENCE_URL=f"http://127.0.0.1:{mp}/infer/images")
    mock = subprocess.Popen([sys.executable, "mock/inference.py"],
                            env=dict(env, MOCK_BEHAVIOR="timeout"),
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    java = subprocess.Popen(["sh", "scripts/run-java.sh"], env=env,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        wait_port(mp, mock)
        wait_port(jp, java)
        base = f"http://127.0.0.1:{jp}"
        body, content_type = multipart(person, garment)
        codes = [request(base + "/api/tryon/tasks", "POST", body, content_type)[0]
                 for _ in range(3)]
        assert codes[:2] == [202, 202] and codes[2] == 503, codes
        print("PASS bounded image queue overload", flush=True)
    finally:
        for process in (java, mock):
            process.terminate()
        for process in (java, mock):
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()

def run():
    person=png(); garment=png(4,5)
    with tempfile.TemporaryDirectory(prefix="tryon-test-") as tmp:
        jp,mp=port(),port(); audit=Path(tmp)/"audit.jsonl"
        env=dict(os.environ,TRYON_MODE="mock",PORT=str(jp),MOCK_PORT=str(mp),DATA_DIR=str(Path(tmp)/"data"),MOCK_AUDIT_FILE=str(audit),TRYON_INFERENCE_URL=f"http://127.0.0.1:{mp}/infer/images",INFERENCE_URL=f"http://127.0.0.1:{mp}/infer")
        mock=subprocess.Popen([sys.executable,"mock/inference.py"],env=env,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
        java=subprocess.Popen(["sh","scripts/run-java.sh"],env=env,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
        try:
            wait_port(mp,mock); wait_port(jp,java); base=f"http://127.0.0.1:{jp}"
            health_code, health = request(base + "/health")
            assert health_code == 200 and health["status"] == "UP" and health["mode"] == "mock"
            ready_code, ready = request(base + "/health/ready")
            assert ready_code == 200 and ready["status"] == "READY"
            body,ctype=multipart(person,garment); code,created=request(base+"/api/tryon/tasks","POST",body,ctype)
            assert code==202 and created["status"] in ("QUEUED", "RUNNING") and created["mode"]=="mock"
            list_code, listed = request(base + "/api/tryon/tasks?limit=1")
            assert list_code == 200 and listed["count"] == 1 and len(listed["tasks"]) == 1
            assert request(base + "/api/tryon/tasks?limit=0")[0] == 400
            result=wait_task(base,created["taskId"])
            assert result["status"]=="SUCCEEDED" and result["result"]=="MOCK_ONLY_NO_IMAGE_GENERATED"
            assert isinstance(result["durationMs"], int) and result["durationMs"] >= 0
            timeline = result["timeline"]
            assert timeline["queuedAt"] and timeline["runningAt"] and timeline["completedAt"]
            assert timeline["queuedAt"] <= timeline["runningAt"] <= timeline["completedAt"]
            assert result["inputs"]["person"]["sha256"]==hashlib.sha256(person).hexdigest()
            assert result["inputs"]["garment"]["width"]==4 and result["inputs"]["garment"]["height"]==5
            task_dir=Path(tmp)/"data"/created["taskId"]
            assert (task_dir/"person.bin").read_bytes()==person and (task_dir/"garment.bin").read_bytes()==garment
            audit_rows=[json.loads(line) for line in audit.read_text().splitlines()]
            assert audit_rows and audit_rows[-1]["personSha256"]==hashlib.sha256(person).hexdigest()
            embedded = person + b"\r\n--" + BOUNDARY.encode() + b"-not-a-delimiter"
            embedded_body, embedded_type = multipart(embedded, garment)
            code, embedded_task = request(base+"/api/tryon/tasks", "POST", embedded_body, embedded_type)
            assert code == 202
            assert wait_task(base, embedded_task["taskId"])["status"] == "SUCCEEDED"
            embedded_dir = Path(tmp)/"data"/embedded_task["taskId"]
            assert (embedded_dir/"person.bin").read_bytes() == embedded
            assert request(base+"/api/tryon/tasks/missing")[0]==404
            assert request(base+"/api/tryon/tasks/"+created["taskId"]+"/result")[0]==409
            badbody,badtype=multipart(b"not-an-image",garment)
            code,bad=request(base+"/api/tryon/tasks","POST",badbody,badtype)
            assert code==400 and bad["error"]=="UNSUPPORTED_OR_INVALID_IMAGE" and len(audit.read_text().splitlines())==2
            mismatch_body = body.replace(b"Content-Type: image/png", b"Content-Type: image/jpeg", 1)
            code, mismatch = request(base+"/api/tryon/tasks", "POST", mismatch_body, ctype)
            assert code == 400 and mismatch["error"] == "IMAGE_CONTENT_TYPE_MISMATCH"
            missing,_=request(base+"/api/tryon/tasks","POST",multipart(person,garment,("person",))[0],ctype)
            assert missing==400
            dupbody,duptype=multipart(person,garment,duplicate=True)
            assert request(base+"/api/tryon/tasks","POST",dupbody,duptype)[0]==400
            assert request(base+"/api/tryon/tasks","POST",b"x",None)[0]==400
            huge=png()+b"x"*(5*1024*1024)
            hugebody,hugetype=multipart(huge,garment)
            assert request(base+"/api/tryon/tasks","POST",hugebody,hugetype)[0]==413
            print("PASS upload success, storage/hash boundary, invalid/missing/duplicate/oversize, result gate")
        finally:
            for process in (java,mock):
                process.terminate()
            for process in (java,mock):
                try: process.wait(timeout=5)
                except subprocess.TimeoutExpired: process.kill(); process.wait()
        run_queue_overload(person, garment, tmp)

if __name__=="__main__": run()
