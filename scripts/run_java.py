"""Build Java 21 once per source revision. Concurrent launches use the same immutable classes."""
import fcntl
import hashlib
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
os.chdir(ROOT)
home = Path(os.environ["JAVA_HOME"]) if os.getenv("JAVA_HOME") else ROOT / ".tools/jdk/Contents/Home"
javac = str(home / "bin/javac") if (home / "bin/javac").exists() else shutil.which("javac")
java = str(home / "bin/java") if (home / "bin/java").exists() else shutil.which("java")
if not javac or not java:
    raise SystemExit("Install JDK 21 and set JAVA_HOME")

sources = sorted(Path("src").rglob("*.java"))
digest = hashlib.sha256(subprocess.check_output([javac, "-version"]))
for source in sources:
    digest.update(str(source).encode() + b"\0" + source.read_bytes())
cache = ROOT / ".build/java"
cache.mkdir(parents=True, exist_ok=True)
classes = cache / digest.hexdigest()
with (cache / "compile.lock").open("a") as lock:
    fcntl.flock(lock, fcntl.LOCK_EX)
    if not classes.exists():
        with tempfile.TemporaryDirectory(dir=cache) as temp:
            output = Path(temp) / "classes"
            output.mkdir()
            subprocess.run([javac, "--release", "21", "--add-modules", "jdk.httpserver",
                            "-encoding", "UTF-8", "-d", str(output), *sources], check=True)
            output.rename(classes)
os.execv(java, [java, "-Djava.awt.headless=true", "--add-modules", "jdk.httpserver",
                "-cp", str(classes), "TryOnServer"])
