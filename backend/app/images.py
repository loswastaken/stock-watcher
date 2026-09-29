"""Item image handling: download, validate (Pillow), re-encode to WebP, store under DATA_DIR/images."""
from __future__ import annotations

import asyncio
import io
import logging
import re
import shutil
import time
import uuid
from urllib.parse import urlsplit

import httpx
from PIL import Image, ImageOps, UnidentifiedImageError

from .config import get_settings
from .db import SessionLocal
from .models import Item

log = logging.getLogger("stockwatcher.images")

MAX_BYTES = 8 * 1024 * 1024
MAX_DIMENSION = 800
MAX_PIXELS = 50_000_000
ALLOWED_FORMATS = {"JPEG", "PNG", "WEBP", "GIF"}
FILENAME_RE = re.compile(r"^[A-Za-z0-9_-]{8,64}\.(webp|jpg|jpeg|png|gif)$")
USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0 Safari/537.36"
)

Image.MAX_IMAGE_PIXELS = MAX_PIXELS

# Retry backoff for automatic (scheduler-driven) image fetches: item_id -> monotonic time
_auto_attempts: dict[int, float] = {}
AUTO_RETRY_SECONDS = 3600


class ImageError(ValueError):
    pass


def is_safe_filename(name: str) -> bool:
    return bool(FILENAME_RE.match(name or ""))


def image_file(filename: str):
    """Absolute path for a stored image, or None if the name is unsafe."""
    if not is_safe_filename(filename):
        return None
    base = get_settings().images_dir.resolve()
    path = (base / filename).resolve()
    if path.parent != base:
        return None
    return path


def process_image(data: bytes) -> bytes:
    """Validate an uploaded image and return WebP bytes (max 800px, EXIF-rotated)."""
    if len(data) > MAX_BYTES:
        raise ImageError("Image is larger than 8 MB")
    if not data:
        raise ImageError("Empty image")
    try:
        with Image.open(io.BytesIO(data)) as im:
            if im.format not in ALLOWED_FORMATS:
                raise ImageError("Unsupported image type (use JPG, PNG, WebP or GIF)")
            if im.width * im.height > MAX_PIXELS:
                raise ImageError("Image dimensions are too large")
            im.load()  # first frame for animated images
            im = ImageOps.exif_transpose(im)
            has_alpha = im.mode in ("RGBA", "LA", "PA") or "transparency" in im.info
            im = im.convert("RGBA" if has_alpha else "RGB")
            im.thumbnail((MAX_DIMENSION, MAX_DIMENSION), Image.Resampling.LANCZOS)
            out = io.BytesIO()
            im.save(out, format="WEBP", quality=85, method=4)
            return out.getvalue()
    except ImageError:
        raise
    except (UnidentifiedImageError, OSError, ValueError, Image.DecompressionBombError) as e:
        raise ImageError("File is not a valid image") from e


def store_image_bytes(data: bytes) -> str:
    """Process + write image; returns the stored filename."""
    webp = process_image(data)
    images_dir = get_settings().images_dir
    images_dir.mkdir(parents=True, exist_ok=True)
    name = f"{uuid.uuid4().hex}.webp"
    tmp = images_dir / f".{name}.tmp"
    tmp.write_bytes(webp)
    tmp.replace(images_dir / name)
    return name


def delete_image_file(filename: str | None) -> None:
    if not filename:
        return
    path = image_file(filename)
    if path is None:
        return
    try:
        path.unlink(missing_ok=True)
    except OSError:
        log.warning("could not delete image %s", filename)


def copy_image_file(filename: str | None) -> str | None:
    """Duplicate a stored image (each item owns its file, which is deleted with the item)."""
    src = image_file(filename) if filename else None
    if src is None or not src.is_file():
        return None
    name = f"{uuid.uuid4().hex}{src.suffix or '.webp'}"
    try:
        shutil.copyfile(src, src.parent / name)
    except OSError:
        log.warning("could not copy image %s", filename)
        return None
    return name


async def download_image(url: str) -> bytes:
    if url.startswith("//"):
        url = "https:" + url
    parts = urlsplit(url)
    if parts.scheme.lower() not in ("http", "https") or not parts.hostname:
        raise ImageError("Image URL must be http(s)")
    async with httpx.AsyncClient(
        timeout=15, follow_redirects=True, headers={"User-Agent": USER_AGENT, "Accept": "image/*,*/*;q=0.5"}
    ) as client:
        async with client.stream("GET", url) as resp:
            resp.raise_for_status()
            length = resp.headers.get("content-length")
            if length and length.isdigit() and int(length) > MAX_BYTES:
                raise ImageError("Image is larger than 8 MB")
            buf = bytearray()
            async for chunk in resp.aiter_bytes():
                buf += chunk
                if len(buf) > MAX_BYTES:
                    raise ImageError("Image is larger than 8 MB")
    return bytes(buf)


async def fetch_and_store(url: str) -> str | None:
    """Download + store an image; returns filename or None on any failure."""
    try:
        data = await download_image(url)
        return await asyncio.to_thread(store_image_bytes, data)
    except Exception as e:  # noqa: BLE001 - best effort
        log.info("image fetch failed for %s: %s", url, e)
        return None


def set_item_image(item_id: int, filename: str) -> bool:
    """Point the item at a stored file, deleting the previous file. False if item is gone."""
    with SessionLocal() as db:
        item = db.get(Item, item_id)
        if item is None:
            delete_image_file(filename)
            return False
        old = item.image_path
        item.image_path = filename
        db.commit()
    if old and old != filename:
        delete_image_file(old)
    return True


async def attach_image_from_url(item_id: int, url: str) -> bool:
    filename = await fetch_and_store(url)
    if not filename:
        return False
    return await asyncio.to_thread(set_item_image, item_id, filename)


def should_auto_fetch(item_id: int) -> bool:
    now = time.monotonic()
    last = _auto_attempts.get(item_id)
    if last is not None and now - last < AUTO_RETRY_SECONDS:
        return False
    _auto_attempts[item_id] = now
    return True
