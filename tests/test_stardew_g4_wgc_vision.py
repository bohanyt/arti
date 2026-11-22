from concurrent.futures import Future
from dataclasses import dataclass
import io

from PIL import Image

import arti_stardew_wgc_capture as wgc_capture
from arti_stardew_window_binding import (
    HWND_REUSED,
    ArtiStardewWindowBinding,
    FakeWindowEnumerator,
    WindowRecord,
)
from arti_stardew_wgc_capture import (
    IDENTITY_UNAVAILABLE,
    NATIVE_PRE_REQUEST_BUFFER_BOUND,
    NO_FRESH_FRAME,
    G2CaptureIdentity,
    WindowsWgcNative,
    open_platform_owner,
)
from arti_stardew_wgc_winrt import (
    FakeWgcWinrtInterop,
    WinrtRawFrame,
    _POOL_BUFFERS,
)
from arti_stardew_wgc_vision import (
    FRAME_TOO_LARGE,
    GEOMETRY_UNAVAILABLE,
    IDENTITY_MISMATCH,
    PIXEL_BUFFER_INVALID,
    WindowGeometrySnapshot,
    capture_bound_window_jpeg,
    client_area_transform,
    encode_capture_result,
)


@dataclass
class Capture:
    ok: bool = True
    reason: str | None = None
    pixels: bytes | None = None
    capture_id: str | None = "f-1"
    captured_at_ns: int | None = 200
    freshness_ns: int | None = 5
    content_size: tuple[int, int] | None = (8, 8)
    hwnd: int | None = 11
    pid: int | None = 22
    creation_time: int | None = 33


@dataclass
class Binding:
    hwnd: int = 11
    pid: int = 22
    creation_time: int = 33


class Geometry:
    def __init__(self, snapshot):
        self._snapshot = snapshot

    def snapshot(self, hwnd):
        return self._snapshot


class Owner:
    def __init__(self, capture):
        self.capture = capture

    def capture_now(self, binding, identity):
        future = Future()
        future.set_result(self.capture)
        return future


def _bgra(width, height):
    return bytes([20, 40, 200, 255]) * (width * height)


def _geometry(window=(100, 100, 108, 108), client=(102, 101, 106, 107), dpi=144):
    return WindowGeometrySnapshot(11, window, client, dpi)


def _client_geometry(width, height, dpi=96):
    return WindowGeometrySnapshot(
        11,
        (0, 0, width, height),
        (0, 0, width, height),
        dpi,
    )


def _game(**kw):
    base = dict(
        hwnd=0x100,
        title="Stardew Valley 1.6.15 - running SMAPI 4.5.2 with 3 mods",
        pid=4242,
        creation_time=1_000_001,
        exe_name="StardewModdingAPI.exe",
        visible=True,
        minimized=False,
        client_rect=(0, 0, 1280, 720),
    )
    base.update(kw)
    return WindowRecord(**base)


def _binding(rec=None):
    rec = rec or _game()
    return ArtiStardewWindowBinding(
        hwnd=rec.hwnd,
        pid=rec.pid,
        creation_time=rec.creation_time,
        exe_name=rec.exe_name,
        title=rec.title,
        client_rect=rec.client_rect,
        minimized=rec.minimized,
    )


def _identity():
    return G2CaptureIdentity(
        runtime_instance_id="ri-live-1",
        context_generation=4,
        session_id="sess-9",
        player_id=2,
        screen_id=0,
        location_id="Farm",
        ui_revision=11,
        viewport_transform_revision=1,
    )


def _raw_frame(capture_id, captured_at_ns):
    return WinrtRawFrame(
        capture_id=capture_id,
        captured_at_ns=captured_at_ns,
        content_width=1280,
        content_height=720,
        pixels=b"\x01\x02\x03\x04" * 16,
    )


def test_transform_tracks_client_dpi_and_recomputes_after_resize():
    first = client_area_transform((16, 16), _geometry())
    resized = client_area_transform((32, 16), _geometry())
    assert first.crop_box_px == (4, 2, 12, 14)
    assert resized.crop_box_px == (8, 2, 24, 14)
    assert first.dpi == 144
    assert first.client_size_px == (4, 6)


def test_already_client_sized_surface_is_not_double_cropped():
    geometry = WindowGeometrySnapshot(
        11, (100, 100, 110, 110), (102, 101, 108, 109), 120
    )
    transform = client_area_transform((6, 8), geometry)
    assert transform.crop_box_px == (0, 0, 6, 8)


def test_bgra_becomes_jpeg_and_crops_client_area():
    result = encode_capture_result(Capture(pixels=_bgra(8, 8)), _geometry())
    assert result.ok and result.frame
    assert result.frame.capture_id == "f-1"
    assert result.frame.crop_box_px == (2, 1, 6, 7)
    assert result.frame.dpi == 144
    image = Image.open(io.BytesIO(result.frame.jpeg_bytes))
    try:
        assert image.size == (4, 6)
    finally:
        image.close()
    assert result.frame.jpeg_b64


def test_portrait_frame_resizes_by_long_edge():
    width, height = 400, 2000
    result = encode_capture_result(
        Capture(
            pixels=_bgra(width, height),
            content_size=(width, height),
        ),
        _client_geometry(width, height),
    )
    assert result.ok and result.frame
    image = Image.open(io.BytesIO(result.frame.jpeg_bytes))
    try:
        assert image.size == (384, 1920)
        assert max(image.size) == 1920
    finally:
        image.close()


def test_landscape_frame_resizes_by_long_edge():
    width, height = 2000, 400
    result = encode_capture_result(
        Capture(
            pixels=_bgra(width, height),
            content_size=(width, height),
        ),
        _client_geometry(width, height),
    )
    assert result.ok and result.frame
    image = Image.open(io.BytesIO(result.frame.jpeg_bytes))
    try:
        assert image.size == (1920, 384)
        assert max(image.size) == 1920
    finally:
        image.close()


def test_encoded_jpeg_over_two_mib_is_rejected(monkeypatch):
    encoded_cap = 2 * 1024 * 1024

    def oversized_save(self, fp, *args, **kwargs):
        fp.write(b"x" * (encoded_cap + 1))

    monkeypatch.setattr(Image.Image, "save", oversized_save)
    result = encode_capture_result(
        Capture(pixels=_bgra(8, 8)),
        WindowGeometrySnapshot(11, (0, 0, 8, 8), (0, 0, 8, 8), 96),
    )
    assert not result.ok
    assert result.reason == FRAME_TOO_LARGE


def test_normal_bounded_frame_stays_within_output_contract():
    width, height = 640, 480
    result = encode_capture_result(
        Capture(
            pixels=_bgra(width, height),
            content_size=(width, height),
        ),
        _client_geometry(width, height),
    )
    assert result.ok and result.frame
    image = Image.open(io.BytesIO(result.frame.jpeg_bytes))
    try:
        assert image.size == (640, 480)
        assert max(image.size) <= 1920
    finally:
        image.close()
    assert len(result.frame.jpeg_bytes) <= 2 * 1024 * 1024


def test_jpeg_quality_caller_cannot_raise_above_85(monkeypatch):
    seen = []
    real_save = Image.Image.save

    def recording_save(self, fp, *args, **kwargs):
        seen.append(kwargs.get("quality"))
        return real_save(self, fp, *args, **kwargs)

    monkeypatch.setattr(Image.Image, "save", recording_save)
    result = encode_capture_result(
        Capture(pixels=_bgra(8, 8)),
        WindowGeometrySnapshot(11, (0, 0, 8, 8), (0, 0, 8, 8), 96),
        quality=100,
    )
    assert result.ok and result.frame
    assert seen == [85]


def test_pre_request_pooled_native_frames_are_discarded_before_evidence():
    assert NATIVE_PRE_REQUEST_BUFFER_BOUND == _POOL_BUFFERS == 2
    clock = {"now": 100}
    interop = FakeWgcWinrtInterop(
        auto_frame=_raw_frame("first", captured_at_ns=100)
    )
    native = WindowsWgcNative(interop=interop)
    owner = wgc_capture.StardewWgcCaptureOwner(
        native=native,
        enumerator=FakeWindowEnumerator([_game()]),
        clock_ns=lambda: clock["now"],
        fresh_wait_s=0.0,
        wait_s=lambda _s: None,
    )
    try:
        first = owner.capture_now(_binding(), _identity()).result(timeout=1)
        assert first.ok
        session = interop.sessions[0]

        # Both frames are already buffered before the second request. Their
        # synthetic timestamps intentionally look newer than that request,
        # reproducing the dangerous CPU-copy-time acceptance shape. The fixed
        # two-buffer boundary must discard both before _take_fresh can use one.
        session.push(_raw_frame("pooled-old-a", captured_at_ns=99_999))
        session.push(_raw_frame("pooled-old-b", captured_at_ns=99_999))
        clock["now"] = 200

        second = owner.capture_now(_binding(), _identity()).result(timeout=1)
        assert not second.ok
        assert second.reason == NO_FRESH_FRAME
        assert second.capture_id is None
        assert second.pixels is None
        assert "pooled-old-a" in session.released
        assert "pooled-old-b" in session.released
    finally:
        owner.shutdown()


def test_default_platform_owner_revalidates_every_capture_and_catches_hwnd_reuse(
    monkeypatch,
):
    clock = {"now": 100}
    enumerator = FakeWindowEnumerator([_game()])
    interop = FakeWgcWinrtInterop(
        auto_frame=_raw_frame("first", captured_at_ns=100)
    )
    native = WindowsWgcNative(interop=interop)
    monkeypatch.setattr(
        wgc_capture, "platform_window_enumerator", lambda: enumerator
    )
    owner = open_platform_owner(
        native=native,
        clock_ns=lambda: clock["now"],
        fresh_wait_s=0.0,
        wait_s=lambda _s: None,
    )
    try:
        first = owner.capture_now(_binding(), _identity()).result(timeout=1)
        assert first.ok
        assert interop.created_hwnds == [0x100]

        enumerator.windows = [_game(creation_time=9_999_999)]
        clock["now"] = 200
        reused = owner.capture_now(_binding(), _identity()).result(timeout=1)
        assert not reused.ok
        assert reused.reason == HWND_REUSED
        assert reused.pixels is None
        assert interop.created_hwnds == [0x100]
    finally:
        owner.shutdown()


def test_native_owner_without_revalidator_fails_identity_closed():
    native = WindowsWgcNative(
        interop=FakeWgcWinrtInterop(
            auto_frame=_raw_frame("never-used", captured_at_ns=100)
        )
    )
    owner = wgc_capture.StardewWgcCaptureOwner(
        native=native,
        enumerator=None,
        clock_ns=lambda: 100,
        fresh_wait_s=0.0,
        wait_s=lambda _s: None,
    )
    try:
        result = owner.capture_now(_binding(), _identity()).result(timeout=1)
        assert not result.ok
        assert result.reason == IDENTITY_UNAVAILABLE
        assert result.pixels is None
        assert native.hwnd is None
    finally:
        owner.shutdown()


def test_identity_pin_is_rechecked_and_never_retargets():
    result = capture_bound_window_jpeg(
        Owner(Capture(pixels=_bgra(8, 8), pid=99)),
        Binding(),
        object(),
        Geometry(_geometry()),
    )
    assert not result.ok
    assert result.reason == IDENTITY_MISMATCH


def test_upstream_unavailable_reason_is_preserved():
    result = capture_bound_window_jpeg(
        Owner(Capture(ok=False, reason="STALE_FRAME")),
        Binding(),
        object(),
        Geometry(_geometry()),
    )
    assert not result.ok
    assert result.reason == "STALE_FRAME"


def test_invalid_geometry_and_pixel_bounds_fail_closed():
    invalid = WindowGeometrySnapshot(11, (0, 0, 8, 8), (9, 0, 10, 2), 96)
    assert (
        encode_capture_result(Capture(pixels=_bgra(8, 8)), invalid).reason
        == GEOMETRY_UNAVAILABLE
    )
    assert (
        encode_capture_result(Capture(pixels=b"x"), _geometry()).reason
        == PIXEL_BUFFER_INVALID
    )
    assert (
        encode_capture_result(
            Capture(pixels=_bgra(8, 8)), _geometry(), max_source_bytes=10
        ).reason
        == FRAME_TOO_LARGE
    )


def test_geometry_provider_failure_is_explicit():
    class BrokenGeometry:
        def snapshot(self, hwnd):
            raise OSError("no geometry")

    result = capture_bound_window_jpeg(
        Owner(Capture(pixels=_bgra(8, 8))), Binding(), object(), BrokenGeometry()
    )
    assert not result.ok
    assert result.reason == GEOMETRY_UNAVAILABLE
