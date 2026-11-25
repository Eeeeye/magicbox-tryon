"""Loopback-only image inference worker used by the Java task service."""
import hashlib
import io
import logging
import os
import threading
import time
from contextlib import asynccontextmanager
from pathlib import Path
from uuid import UUID, uuid4

from fastapi import FastAPI, HTTPException, Request, Response
from PIL import Image, ImageOps, UnidentifiedImageError
from starlette.concurrency import run_in_threadpool
from starlette.datastructures import UploadFile

from .engine import Settings, TryOnEngine
from .masking import GarmentRegionMissing


ROOT = Path(__file__).resolve().parents[1]
MAX_FILE = 5 * 1024 * 1024
MAX_PIXELS = 16_000_000
LOG = logging.getLogger("uvicorn.error")
slot = threading.BoundedSemaphore(1)


@asynccontextmanager
async def lifespan(app):
    settings = Settings(
        width=int(os.getenv("TRYON_WIDTH", "384")), height=int(os.getenv("TRYON_HEIGHT", "512")),
        steps=int(os.getenv("TRYON_STEPS", "25")), seed=int(os.getenv("TRYON_SEED", "42")),
        guidance=float(os.getenv("TRYON_GUIDANCE", "2.5"))
    )
    engine = TryOnEngine(ROOT / "models", os.getenv("TRYON_DEVICE", "auto"), settings)
    app.state.engine = engine
    LOG.info("CatVTON loaded: device=%s size=%sx%s steps=%s", engine.device, settings.width, settings.height, settings.steps)
    yield


app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)


@app.get("/ready")
def readiness(request: Request):
    engine = request.app.state.engine
    return {"status": "READY", "model": "CatVTON-Mix", "device": engine.device,
            "width": engine.settings.width, "height": engine.settings.height, "steps": engine.settings.steps,
            "seed": engine.settings.seed, "guidance": engine.settings.guidance}


def decode(raw):
    try:
        with Image.open(io.BytesIO(raw)) as image:
            if image.format not in {"PNG", "JPEG"} or image.width * image.height > MAX_PIXELS:
                raise HTTPException(422, "INVALID_IMAGE")
            return ImageOps.exif_transpose(image).convert("RGB")
    except (UnidentifiedImageError, OSError, Image.DecompressionBombError) as error:
        raise HTTPException(422, "INVALID_IMAGE") from error


def render(engine, person, garment, category):
    result = engine.generate(decode(person), decode(garment), category)
    encoded = io.BytesIO()
    result.save(encoded, format="PNG")
    return encoded.getvalue()


@app.post("/infer/images")
async def infer(request: Request):
    length = request.headers.get("content-length", "")
    if not length.isdecimal():
        raise HTTPException(411, "LENGTH_REQUIRED")
    if int(length) > 2 * MAX_FILE + 512 * 1024:
        raise HTTPException(413, "REQUEST_TOO_LARGE")
    category = request.headers.get("x-garment-category", "upper")
    if category not in {"upper", "lower", "dress"}:
        raise HTTPException(422, "INVALID_CATEGORY")
    try:
        task_id = UUID(request.headers.get("x-request-id", "")) if "x-request-id" in request.headers else uuid4()
    except ValueError as error:
        raise HTTPException(422, "INVALID_REQUEST_ID") from error
    if not slot.acquire(blocking=False):
        raise HTTPException(503, "WORKER_BUSY")
    started = time.monotonic()
    try:
        async with request.form(max_files=2, max_fields=0) as form:
            if sorted(key for key, _ in form.multi_items()) != ["garment", "person"]:
                raise HTTPException(422, "TWO_IMAGES_REQUIRED")
            images = {}
            for key in ("person", "garment"):
                file = form[key]
                if not isinstance(file, UploadFile):
                    raise HTTPException(422, "IMAGE_FILE_REQUIRED")
                raw = await file.read(MAX_FILE + 1)
                if len(raw) > MAX_FILE:
                    raise HTTPException(413, "IMAGE_TOO_LARGE")
                images[key] = raw
        try:
            output = await run_in_threadpool(render, request.app.state.engine, images["person"], images["garment"], category)
        except GarmentRegionMissing as error:
            raise HTTPException(422, "GARMENT_REGION_NOT_FOUND") from error
        except RuntimeError as problem:
            if "out of memory" not in str(problem).lower():
                raise
            LOG.warning("Model memory limit reached for task=%s", task_id)
            raise HTTPException(507, "MODEL_MEMORY_LIMIT") from None
        elapsed = time.monotonic() - started
        LOG.info("Completed task=%s seconds=%.3f", task_id, elapsed)
        return Response(output, media_type="image/png", headers={
            "X-Person-SHA256": hashlib.sha256(images["person"]).hexdigest(),
            "X-Garment-SHA256": hashlib.sha256(images["garment"]).hexdigest(),
            "X-Request-Id": str(task_id), "Cache-Control": "no-store",
        })
    finally:
        slot.release()
