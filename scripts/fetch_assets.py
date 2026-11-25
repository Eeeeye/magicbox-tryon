"""Fetch pinned model files and public demo inputs. Reruns verify and resume."""
import concurrent.futures
import fcntl
import hashlib
import json
import os
from pathlib import Path
import subprocess
from urllib.parse import quote


ROOT = Path(__file__).resolve().parents[1]


def verified(path, asset):
    if not path.is_file() or path.stat().st_size != asset["bytes"]:
        return False
    if "sha256" in asset:
        digest = hashlib.sha256()
        expected = asset["sha256"]
    else:
        digest = hashlib.sha1(f"blob {asset['bytes']}\0".encode())
        expected = asset["gitBlob"]
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(4 * 1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest() == expected


def fetch(asset):
    target = ROOT / asset["destination"]
    target.parent.mkdir(parents=True, exist_ok=True)
    if verified(target, asset):
        print(f"Verified {asset['destination']}", flush=True)
        return
    partial = target.with_suffix(target.suffix + ".partial")
    prefix = "spaces/" if asset["space"] else ""
    url = f"https://huggingface.co/{prefix}{asset['repo']}/resolve/{asset['revision']}/{quote(asset['source'])}?download=true"
    if partial.exists() and partial.stat().st_size >= asset["bytes"]:
        if verified(partial, asset):
            partial.replace(target)
            return
        partial.unlink()
    subprocess.run(["curl", "--fail", "--location", "--silent", "--show-error",
                    "--retry", "3", "--retry-delay", "2", "--connect-timeout", "18",
                    "--speed-time", "70", "--speed-limit", "1024", "--continue-at", "-",
                    "--output", str(partial), url], check=True)
    if not verified(partial, asset):
        partial.unlink(missing_ok=True)
        raise ValueError(f"Integrity check failed for {target.name}")
    os.replace(partial, target)
    print(f"Downloaded and verified {asset['destination']}", flush=True)


if __name__ == "__main__":
    assets = json.loads((ROOT / "inference/assets.lock.json").read_text())
    cache = ROOT / "models"
    cache.mkdir(exist_ok=True)
    with (cache / ".download.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
            list(pool.map(fetch, assets))
