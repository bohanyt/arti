"""Screenshot capture for vision chain (mss + JPEG resize)."""

from __future__ import annotations

import base64
import io
from typing import Any

from PIL import Image


def capture_jpeg_b64(config: dict | None = None) -> tuple[str, bytes]:
    """Grab primary monitor; return (base64 str, raw jpeg bytes)."""
    import mss

    cfg = config or {}
    max_width = int(cfg.get("vision_capture_max_width", 1280))
    quality = int(cfg.get("vision_capture_jpeg_quality", 75))

    with mss.mss() as sct:
        monitor = sct.monitors[1]
        shot = sct.grab(monitor)
        img = Image.frombytes("RGB", shot.size, shot.bgra, "raw", "BGRX")

    if img.width > max_width:
        ratio = max_width / img.width
        img = img.resize((max_width, max(1, int(img.height * ratio))), Image.LANCZOS)

    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=quality, optimize=True)
    jpeg = buf.getvalue()
    return base64.b64encode(jpeg).decode("ascii"), jpeg


def probe_capture(config: dict | None = None) -> tuple[bool, str]:
    """Lightweight health probe — non-empty JPEG."""
    try:
        b64, raw = capture_jpeg_b64(config)
        if not b64 or len(raw) < 100:
            return False, "empty capture"
        return True, f"{len(raw)} bytes"
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"
