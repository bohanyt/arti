"""Cloud-safe G4 transform/encoding for the single per-HWND WGC owner.

This module never captures a monitor/desktop and never falls back to mss,
PrintWindow, or a backbuffer. Native WGC success is deliberately outside the
cloud evidence boundary; callers inject the already-owned G4 capture result.
"""
from __future__ import annotations

import base64
import io
import sys
from dataclasses import dataclass
from typing import Protocol

from PIL import Image

GEOMETRY_UNAVAILABLE = "GEOMETRY_UNAVAILABLE"
IDENTITY_MISMATCH = "IDENTITY_MISMATCH"
FRAME_UNAVAILABLE = "FRAME_UNAVAILABLE"
FRAME_TOO_LARGE = "FRAME_TOO_LARGE"
PIXEL_BUFFER_INVALID = "PIXEL_BUFFER_INVALID"
ENCODE_UNAVAILABLE = "ENCODE_UNAVAILABLE"

_MAX_SOURCE_BYTES = 64 * 1024 * 1024
_MAX_OUTPUT_PIXELS = 16_000_000
_MAX_LONG_EDGE = 1920
_MAX_ENCODED_JPEG_BYTES = 2 * 1024 * 1024
_MAX_JPEG_QUALITY = 85


@dataclass(frozen=True)
class WindowGeometrySnapshot:
    hwnd: int
    window_rect_px: tuple[int, int, int, int]
    client_rect_px: tuple[int, int, int, int]
    dpi: int


@dataclass(frozen=True)
class ClientAreaTransform:
    crop_box_px: tuple[int, int, int, int]
    source_size: tuple[int, int]
    client_size_px: tuple[int, int]
    dpi: int


@dataclass(frozen=True)
class EncodedWgcVisionFrame:
    jpeg_bytes: bytes
    jpeg_b64: str
    hwnd: int
    pid: int
    creation_time: int
    capture_id: str
    captured_at_ns: int
    freshness_ns: int
    source_size: tuple[int, int]
    client_size_px: tuple[int, int]
    crop_box_px: tuple[int, int, int, int]
    dpi: int


@dataclass(frozen=True)
class WgcVisionResult:
    ok: bool
    frame: EncodedWgcVisionFrame | None = None
    reason: str | None = None
    detail: str | None = None


class GeometryProvider(Protocol):
    def snapshot(self, hwnd: int) -> WindowGeometrySnapshot: ...


def client_area_transform(
    source_size: tuple[int, int], geometry: WindowGeometrySnapshot
) -> ClientAreaTransform:
    """Map physical client coordinates into this frame's WGC window surface.

    Ratios are recomputed for every frame, so resize/DPI changes never reuse a
    stale transform. Invalid or empty crops fail closed.
    """
    sw, sh = map(int, source_size)
    if sw <= 0 or sh <= 0 or geometry.hwnd <= 0 or geometry.dpi <= 0:
        raise ValueError(GEOMETRY_UNAVAILABLE)
    wl, wt, wr, wb = geometry.window_rect_px
    cl, ct, cr, cb = geometry.client_rect_px
    ww, wh = wr - wl, wb - wt
    cw, ch = cr - cl, cb - ct
    if ww <= 0 or wh <= 0 or cw <= 0 or ch <= 0:
        raise ValueError(GEOMETRY_UNAVAILABLE)
    if cl < wl or ct < wt or cr > wr or cb > wb:
        raise ValueError(GEOMETRY_UNAVAILABLE)

    # The shared owner currently accepts a frame only when WGC already reports
    # the physical client size. Cropping non-client offsets a second time would
    # remove valid game pixels, so that surface maps 1:1 to the client area.
    if (sw, sh) == (cw, ch):
        return ClientAreaTransform(
            crop_box_px=(0, 0, sw, sh),
            source_size=(sw, sh),
            client_size_px=(cw, ch),
            dpi=int(geometry.dpi),
        )

    sx, sy = sw / ww, sh / wh
    left = max(0, min(sw, round((cl - wl) * sx)))
    top = max(0, min(sh, round((ct - wt) * sy)))
    right = max(0, min(sw, round((cr - wl) * sx)))
    bottom = max(0, min(sh, round((cb - wt) * sy)))
    if right <= left or bottom <= top:
        raise ValueError(GEOMETRY_UNAVAILABLE)
    return ClientAreaTransform(
        crop_box_px=(left, top, right, bottom),
        source_size=(sw, sh),
        client_size_px=(cw, ch),
        dpi=int(geometry.dpi),
    )


def encode_capture_result(
    capture,
    geometry: WindowGeometrySnapshot,
    *,
    quality: int = 85,
    max_long_edge: int = _MAX_LONG_EDGE,
    max_source_bytes: int = _MAX_SOURCE_BYTES,
    max_output_pixels: int = _MAX_OUTPUT_PIXELS,
) -> WgcVisionResult:
    if not getattr(capture, "ok", False):
        return WgcVisionResult(
            False, reason=getattr(capture, "reason", None) or FRAME_UNAVAILABLE
        )
    hwnd = int(getattr(capture, "hwnd", 0) or 0)
    pid = int(getattr(capture, "pid", 0) or 0)
    if hwnd <= 0 or pid <= 0 or hwnd != int(geometry.hwnd):
        return WgcVisionResult(False, reason=IDENTITY_MISMATCH)
    try:
        requested_long_edge = int(max_long_edge)
    except (TypeError, ValueError):
        return WgcVisionResult(False, reason=FRAME_TOO_LARGE)
    if requested_long_edge <= 0:
        return WgcVisionResult(False, reason=FRAME_TOO_LARGE)
    long_edge_limit = min(requested_long_edge, _MAX_LONG_EDGE)

    size = getattr(capture, "content_size", None)
    pixels = getattr(capture, "pixels", None)
    capture_id = getattr(capture, "capture_id", None)
    captured_at_ns = getattr(capture, "captured_at_ns", None)
    freshness_ns = getattr(capture, "freshness_ns", None)
    if (
        not size
        or not pixels
        or not capture_id
        or captured_at_ns is None
        or freshness_ns is None
    ):
        return WgcVisionResult(False, reason=FRAME_UNAVAILABLE)
    sw, sh = int(size[0]), int(size[1])
    if sw <= 0 or sh <= 0:
        return WgcVisionResult(False, reason=GEOMETRY_UNAVAILABLE)
    expected = sw * sh * 4
    if expected > max_source_bytes:
        return WgcVisionResult(False, reason=FRAME_TOO_LARGE)
    if len(pixels) != expected:
        return WgcVisionResult(False, reason=PIXEL_BUFFER_INVALID)
    try:
        transform = client_area_transform((sw, sh), geometry)
        image = Image.frombytes("RGBA", (sw, sh), pixels, "raw", "BGRA").convert("RGB")
        try:
            cropped = image.crop(transform.crop_box_px)
            try:
                if cropped.width * cropped.height > max_output_pixels:
                    return WgcVisionResult(False, reason=FRAME_TOO_LARGE)
                long_edge = max(cropped.width, cropped.height)
                if long_edge > long_edge_limit:
                    ratio = long_edge_limit / long_edge
                    resized = cropped.resize(
                        (
                            max(1, round(cropped.width * ratio)),
                            max(1, round(cropped.height * ratio)),
                        ),
                        Image.LANCZOS,
                    )
                    cropped.close()
                    cropped = resized
                buf = io.BytesIO()
                cropped.save(
                    buf,
                    format="JPEG",
                    quality=max(1, min(_MAX_JPEG_QUALITY, int(quality))),
                    optimize=True,
                )
                jpeg = buf.getvalue()
                if len(jpeg) > _MAX_ENCODED_JPEG_BYTES:
                    return WgcVisionResult(False, reason=FRAME_TOO_LARGE)
            finally:
                cropped.close()
        finally:
            image.close()
    except ValueError:
        return WgcVisionResult(False, reason=GEOMETRY_UNAVAILABLE)
    except Exception as exc:
        return WgcVisionResult(
            False, reason=ENCODE_UNAVAILABLE, detail=type(exc).__name__
        )
    return WgcVisionResult(
        True,
        frame=EncodedWgcVisionFrame(
            jpeg_bytes=jpeg,
            jpeg_b64=base64.b64encode(jpeg).decode("ascii"),
            hwnd=hwnd,
            pid=pid,
            creation_time=int(getattr(capture, "creation_time", 0) or 0),
            capture_id=str(capture_id),
            captured_at_ns=int(captured_at_ns),
            freshness_ns=max(0, int(freshness_ns)),
            source_size=(sw, sh),
            client_size_px=transform.client_size_px,
            crop_box_px=transform.crop_box_px,
            dpi=transform.dpi,
        ),
    )


def capture_bound_window_jpeg(
    owner,
    binding,
    identity,
    geometry_provider: GeometryProvider,
    *,
    timeout_s: float = 1.0,
    quality: int = 85,
    max_long_edge: int = _MAX_LONG_EDGE,
) -> WgcVisionResult:
    """Capture only through the existing WGC owner; never use screen fallback."""
    try:
        outcome = owner.capture_now(binding, identity).result(
            timeout=max(0.01, float(timeout_s))
        )
    except Exception as exc:
        return WgcVisionResult(
            False, reason=FRAME_UNAVAILABLE, detail=type(exc).__name__
        )
    if not getattr(outcome, "ok", False):
        return WgcVisionResult(
            False, reason=getattr(outcome, "reason", None) or FRAME_UNAVAILABLE
        )
    pins = (
        ("hwnd", int(getattr(binding, "hwnd", 0) or 0)),
        ("pid", int(getattr(binding, "pid", 0) or 0)),
        ("creation_time", int(getattr(binding, "creation_time", 0) or 0)),
    )
    for field, expected in pins:
        if expected <= 0 or int(getattr(outcome, field, 0) or 0) != expected:
            return WgcVisionResult(False, reason=IDENTITY_MISMATCH)
    try:
        geometry = geometry_provider.snapshot(int(binding.hwnd))
    except Exception as exc:
        return WgcVisionResult(
            False, reason=GEOMETRY_UNAVAILABLE, detail=type(exc).__name__
        )
    return encode_capture_result(
        outcome, geometry, quality=quality, max_long_edge=max_long_edge
    )


class WindowsClientGeometryProvider:
    """GetWindowRect + physical client screen rect + GetDpiForWindow metadata."""

    def snapshot(self, hwnd: int) -> WindowGeometrySnapshot:
        if sys.platform != "win32":
            raise RuntimeError(GEOMETRY_UNAVAILABLE)
        import ctypes
        from ctypes import wintypes

        user32 = ctypes.windll.user32

        class RECT(ctypes.Structure):
            _fields_ = [
                ("left", wintypes.LONG),
                ("top", wintypes.LONG),
                ("right", wintypes.LONG),
                ("bottom", wintypes.LONG),
            ]

        class POINT(ctypes.Structure):
            _fields_ = [("x", wintypes.LONG), ("y", wintypes.LONG)]

        if int(hwnd) <= 0 or not user32.IsWindow(int(hwnd)):
            raise RuntimeError(GEOMETRY_UNAVAILABLE)
        wr, cr = RECT(), RECT()
        if not user32.GetWindowRect(int(hwnd), ctypes.byref(wr)):
            raise RuntimeError(GEOMETRY_UNAVAILABLE)
        if not user32.GetClientRect(int(hwnd), ctypes.byref(cr)):
            raise RuntimeError(GEOMETRY_UNAVAILABLE)
        tl, br = POINT(cr.left, cr.top), POINT(cr.right, cr.bottom)
        if not user32.ClientToScreen(int(hwnd), ctypes.byref(tl)):
            raise RuntimeError(GEOMETRY_UNAVAILABLE)
        if not user32.ClientToScreen(int(hwnd), ctypes.byref(br)):
            raise RuntimeError(GEOMETRY_UNAVAILABLE)
        get_dpi = getattr(user32, "GetDpiForWindow", None)
        dpi = int(get_dpi(int(hwnd))) if get_dpi is not None else 0
        if dpi <= 0:
            raise RuntimeError(GEOMETRY_UNAVAILABLE)
        return WindowGeometrySnapshot(
            hwnd=int(hwnd),
            window_rect_px=(
                int(wr.left),
                int(wr.top),
                int(wr.right),
                int(wr.bottom),
            ),
            client_rect_px=(int(tl.x), int(tl.y), int(br.x), int(br.y)),
            dpi=dpi,
        )
