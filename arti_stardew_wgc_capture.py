"""G4 Stardew Windows Graphics Capture adapter (capture-adapter-only).

One owner for the WGC device / frame-pool / session lifecycle. Later G6 ROI
must reuse this owner and its pool token rather than constructing a second
WGC stack.

CreateForWindow(HWND) semantics only. No monitor, desktop, mss, PrintWindow,
or backbuffer fallback. No VTuber vision capture, privacy, or provider path.

G2 inspect_context fields are copy-only identity inputs. This module does
not invent visual_generation / wgc_revision, does not bump
viewport_transform_revision, and never advertises image->pointer mapping
or ui_pointer. Occlusion remains E4 UNKNOWN.

Evidence ceiling of this slice: UNIT_TESTED / CLOUD_VERIFIED.
Native WGC/DPI/occlusion/pixels remain unproved until a local gate.
"""

from __future__ import annotations

import queue
import sys
import threading
import time
import uuid
import weakref
from concurrent.futures import Future
from dataclasses import dataclass
from typing import Callable, Protocol

from arti_stardew_window_binding import (
    ArtiStardewWindowBinding,
    PLATFORM_UNAVAILABLE,
    WindowEnumerator,
    platform_window_enumerator,
    resolve_arti_stardew_window,
)
from arti_stardew_wgc_winrt import (
    WgcWinrtInterop,
    platform_winrt_interop,
)

# Fail-closed reason tokens.
IDENTITY_UNAVAILABLE = "IDENTITY_UNAVAILABLE"
STALE_FRAME = "STALE_FRAME"
BLANK_FRAME = "BLANK_FRAME"
PROTECTED_CONTENT = "PROTECTED_CONTENT"
MINIMIZED = "MINIMIZED"
GEOMETRY_MISMATCH = "GEOMETRY_MISMATCH"
DEVICE_LOSS = "DEVICE_LOSS"
HWND_INVALID = "HWND_INVALID"
SHUTTING_DOWN = "SHUTTING_DOWN"
CAPTURE_UNAVAILABLE = "CAPTURE_UNAVAILABLE"
NO_FRESH_FRAME = "NO_FRESH_FRAME"
SECOND_WGC_STACK_FORBIDDEN = "SECOND_WGC_STACK_FORBIDDEN"
COM_APARTMENT_REQUIRED = "COM_APARTMENT_REQUIRED"

OCCLUSION_UNKNOWN = "UNKNOWN"
PLACEHOLDER_VIEWPORT_TRANSFORM_REVISION = 1

# Bound wait on the owner thread only — never on the caller / asyncio / voice loop.
DEFAULT_FRESH_WAIT_S = 0.2
DEFAULT_MAX_AGE_NS = 250_000_000  # 250 ms
# The production WinRT frame pool is fixed at two buffers. Before reusing a
# native session for a new request, discard at most those two frames so no
# frame that was already buffered before the request can become its evidence.
NATIVE_PRE_REQUEST_BUFFER_BOUND = 2

_LIVE_NATIVE_OWNERS: list[weakref.ref] = []


class SecondWgcStackError(RuntimeError):
    """A second native WGC stack would split G6 ROI off this owner."""


@dataclass(frozen=True)
class G2CaptureIdentity:
    """Copy-only G2 inspect_context identity. Do not mint a second generation."""

    runtime_instance_id: str
    context_generation: int
    session_id: str
    player_id: int
    screen_id: int
    location_id: str
    ui_revision: int
    viewport_transform_revision: int

    def valid(self) -> bool:
        return bool(
            self.runtime_instance_id
            and self.session_id
            and self.location_id
            and self.context_generation >= 1
            and self.player_id >= 0
            and self.screen_id >= 0
            and self.ui_revision >= 0
            and self.viewport_transform_revision >= 1
        )


@dataclass(frozen=True)
class NativeWgcFrame:
    capture_id: str
    captured_at_ns: int
    content_width: int
    content_height: int
    pixels: bytes
    blank: bool = False
    protected: bool = False
    device_lost: bool = False
    hwnd_invalid: bool = False


@dataclass(frozen=True)
class CaptureNowResult:
    ok: bool
    reason: str | None
    pixels: bytes | None
    capture_id: str | None
    requested_at_ns: int
    captured_at_ns: int | None
    freshness_ns: int | None
    content_size: tuple[int, int] | None
    client_rect: tuple[int, int, int, int] | None
    hwnd: int | None
    pid: int | None
    creation_time: int | None
    identity: G2CaptureIdentity | None
    viewport_transform_revision: int | None
    ui_pointer_available: bool
    image_to_pointer_available: bool
    occlusion: str
    owner_thread_ident: int | None
    frame_pool_token: str | None

    def __repr__(self) -> str:
        px = None if self.pixels is None else f"<{len(self.pixels)} bytes>"
        return (
            "CaptureNowResult("
            f"ok={self.ok!r}, reason={self.reason!r}, pixels={px}, "
            f"capture_id={self.capture_id!r}, content_size={self.content_size!r}, "
            f"occlusion={self.occlusion!r}, ui_pointer_available={self.ui_pointer_available!r})"
        )


class WgcNativeSession(Protocol):
    """CreateForWindow(HWND) session. No monitor capture method exists here."""

    is_native: bool

    def create_for_window(self, hwnd: int) -> None: ...

    def try_take_next_frame(self) -> NativeWgcFrame | None: ...

    def release_frame(self, frame: NativeWgcFrame) -> None: ...

    def close(self) -> None: ...

    def pool_token(self) -> str: ...

    def recreate_pool(self) -> None: ...


class UnavailableWgcNative:
    """Default off Windows / when WinRT WGC APIs cannot be loaded."""

    is_native = False

    def __init__(self, reason: str = PLATFORM_UNAVAILABLE) -> None:
        self.reason = reason
        self.closed = False
        self._token = "unavailable-pool"

    def create_for_window(self, hwnd: int) -> None:
        raise RuntimeError(self.reason)

    def try_take_next_frame(self) -> NativeWgcFrame | None:
        return None

    def release_frame(self, frame: NativeWgcFrame) -> None:
        return None

    def close(self) -> None:
        self.closed = True

    def pool_token(self) -> str:
        return self._token

    def recreate_pool(self) -> None:
        return None


class FakeWgcNative:
    """In-process CreateForWindow double. Cloud tests never call real WGC."""

    is_native = False

    def __init__(self, pool_token: str = "fake-pool") -> None:
        self._token = pool_token
        self.hwnd: int | None = None
        self.closed = False
        self.created_for_window: list[int] = []
        self.released: list[str] = []
        self.recreated = 0
        self.frames: list[NativeWgcFrame] = []
        self.take_hook = None
        self.close_hook = None

    def create_for_window(self, hwnd: int) -> None:
        if self.closed:
            raise RuntimeError(SHUTTING_DOWN)
        self.hwnd = int(hwnd)
        self.created_for_window.append(int(hwnd))

    def try_take_next_frame(self) -> NativeWgcFrame | None:
        if self.take_hook is not None:
            self.take_hook()
        if self.closed:
            return None
        if not self.frames:
            return None
        return self.frames.pop(0)

    def release_frame(self, frame: NativeWgcFrame) -> None:
        self.released.append(frame.capture_id)

    def close(self) -> None:
        self.closed = True
        if self.close_hook is not None:
            self.close_hook()

    def pool_token(self) -> str:
        return self._token

    def recreate_pool(self) -> None:
        self.recreated += 1

    def push(self, frame: NativeWgcFrame) -> None:
        self.frames.append(frame)


class WindowsWgcNative:
    """HWND-only WGC backend. Fail closed when WinRT/WGC cannot be loaded.

    This type is marked native so two live instances are forbidden. The
    CreateForWindow entry is the only item constructor; there is no monitor
    path. Injected interop covers Cloud constructor seams. Real ctypes/WinRT
    construction is owned by arti_stardew_wgc_winrt and is not Cloud-proved.
    """

    is_native = True

    def __init__(self, interop: WgcWinrtInterop | None = None) -> None:
        self.closed = False
        self.hwnd: int | None = None
        self._token = f"wgc-native-{uuid.uuid4().hex[:12]}"
        self._interop = interop
        self._session = None
        self._apartment_ident: int | None = None
        self._reason: str | None
        if interop is None:
            self._reason = _probe_wgc_create_for_window()
        elif not interop.available():
            self._reason = PLATFORM_UNAVAILABLE
        else:
            self._reason = None

    def create_for_window(self, hwnd: int) -> None:
        if self.closed:
            raise RuntimeError(SHUTTING_DOWN)
        if self._reason:
            raise RuntimeError(self._reason)
        interop = self._interop
        if interop is None:
            interop = platform_winrt_interop()
            self._interop = interop
        if not interop.available():
            raise RuntimeError(PLATFORM_UNAVAILABLE)
        self._apartment_ident = interop.ensure_owner_apartment()
        if int(hwnd) <= 0:
            raise RuntimeError(HWND_INVALID)
        if self._session is not None:
            try:
                self._session.close()
            except Exception:
                pass
            self._session = None
            self.hwnd = None
        self._session = interop.create_session_for_window(int(hwnd))
        self.hwnd = int(hwnd)
        self._token = self._session.pool_token()

    def try_take_next_frame(self) -> NativeWgcFrame | None:
        if self.closed or self._session is None:
            return None
        raw = self._session.try_take_frame()
        if raw is None:
            return None
        if isinstance(raw, NativeWgcFrame):
            return raw
        return NativeWgcFrame(
            capture_id=raw.capture_id,
            captured_at_ns=raw.captured_at_ns,
            content_width=raw.content_width,
            content_height=raw.content_height,
            pixels=raw.pixels,
            blank=raw.blank,
            protected=raw.protected,
            device_lost=raw.device_lost,
            hwnd_invalid=raw.hwnd_invalid,
        )

    def release_frame(self, frame: NativeWgcFrame) -> None:
        if self._session is None:
            return None
        try:
            self._session.release_frame(frame)
        except Exception:
            pass
        return None

    def close(self) -> None:
        self.closed = True
        session = self._session
        self._session = None
        if session is not None:
            try:
                session.close()
            except Exception:
                pass
        if (
            self._interop is not None
            and self._apartment_ident is not None
            and threading.get_ident() == self._apartment_ident
        ):
            try:
                self._interop.release_apartment_if_owner_thread()
            except Exception:
                pass
            self._apartment_ident = None

    def pool_token(self) -> str:
        if self._session is not None:
            return self._session.pool_token()
        return self._token

    def recreate_pool(self) -> None:
        if self._session is None:
            return None
        self._session.recreate_pool()
        self._token = self._session.pool_token()
        return None


def _probe_wgc_create_for_window() -> str | None:
    """Return a fail-closed reason, or None if CreateForWindow APIs load.

    A successful probe is not laptop capture proof.
    """
    try:
        if not platform_winrt_interop().available():
            return PLATFORM_UNAVAILABLE
    except Exception:
        return PLATFORM_UNAVAILABLE
    return None


def platform_wgc_native() -> WgcNativeSession:
    if sys.platform != "win32":
        return UnavailableWgcNative(PLATFORM_UNAVAILABLE)
    return WindowsWgcNative()


@dataclass
class _CaptureRequest:
    binding: ArtiStardewWindowBinding
    identity: G2CaptureIdentity
    requested_at_ns: int
    future: Future


class StardewWgcCaptureOwner:
    """Single owner of the Stardew WGC device/pool/session.

    Public capture_now is nonblocking: it queues work onto the owner thread
    and returns a Future. Later G6 ROI must call shared_owner_for_roi() /
    frame_pool_token() on THIS instance.
    """

    def __init__(
        self,
        *,
        native: WgcNativeSession | None = None,
        enumerator: WindowEnumerator | None = None,
        clock_ns: Callable[[], int] | None = None,
        max_age_ns: int = DEFAULT_MAX_AGE_NS,
        fresh_wait_s: float = DEFAULT_FRESH_WAIT_S,
        wait_s: Callable[[float], None] | None = None,
    ) -> None:
        self._native: WgcNativeSession = (
            native if native is not None else platform_wgc_native()
        )
        self._enumerator = enumerator
        self._clock_ns = clock_ns or time.perf_counter_ns
        self._max_age_ns = int(max_age_ns)
        self._fresh_wait_s = float(fresh_wait_s)
        self._wait_s = wait_s or time.sleep
        self._q: queue.Queue[_CaptureRequest | None] = queue.Queue()
        self._stop = threading.Event()
        self._hwnd: int | None = None
        self._alive = True
        self._lock = threading.Lock()
        self._register_native_stack()
        self._thread = threading.Thread(
            target=self._owner_loop,
            name="arti-stardew-wgc-owner",
            daemon=True,
        )
        self._thread.start()
        self.owner_thread_ident = self._thread.ident

    def _register_native_stack(self) -> None:
        if not getattr(self._native, "is_native", False):
            return
        live = [r() for r in _LIVE_NATIVE_OWNERS]
        live = [o for o in live if o is not None and o._alive]
        if live:
            raise SecondWgcStackError(SECOND_WGC_STACK_FORBIDDEN)
        _LIVE_NATIVE_OWNERS.append(weakref.ref(self))

    def __enter__(self) -> StardewWgcCaptureOwner:
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.shutdown()

    def frame_pool_token(self) -> str:
        return self._native.pool_token()

    def shared_owner_for_roi(self) -> StardewWgcCaptureOwner:
        """G6 temporal ROI must reuse this owner, not open another WGC stack."""
        return self

    def capture_now(
        self,
        binding: ArtiStardewWindowBinding,
        identity: G2CaptureIdentity,
    ) -> Future:
        """Nonblocking fresh-frame request. Does not wait on the caller thread."""
        fut: Future = Future()
        requested_at_ns = int(self._clock_ns())
        if not self._alive or self._stop.is_set():
            fut.set_result(
                self._fail(
                    SHUTTING_DOWN,
                    requested_at_ns,
                    identity=identity,
                    binding=binding,
                )
            )
            return fut
        if not identity.valid():
            fut.set_result(
                self._fail(
                    IDENTITY_UNAVAILABLE,
                    requested_at_ns,
                    identity=identity,
                    binding=binding,
                )
            )
            return fut
        req = _CaptureRequest(
            binding=binding,
            identity=identity,
            requested_at_ns=requested_at_ns,
            future=fut,
        )
        self._q.put(req)
        return fut

    def shutdown(self, timeout: float = 2.0) -> None:
        """Cancel/finish outstanding work, then tear down pool/session."""
        with self._lock:
            if not self._alive:
                return
            self._alive = False
            self._stop.set()
        # Unblock a native take that is waiting.
        try:
            self._native.close()
        except Exception:
            pass
        self._q.put(None)
        self._thread.join(timeout=timeout)
        # Anything still queued after the loop stopped is fail-closed.
        while True:
            try:
                item = self._q.get_nowait()
            except queue.Empty:
                break
            if item is None:
                continue
            if not item.future.done():
                item.future.set_result(
                    self._fail(
                        SHUTTING_DOWN,
                        item.requested_at_ns,
                        identity=item.identity,
                        binding=item.binding,
                    )
                )

    def _owner_loop(self) -> None:
        while True:
            try:
                item = self._q.get(timeout=0.05)
            except queue.Empty:
                if self._stop.is_set():
                    break
                continue
            if item is None:
                break
            if item.future.done():
                continue
            try:
                result = self._fulfill(item)
            except Exception:
                result = self._fail(
                    CAPTURE_UNAVAILABLE,
                    item.requested_at_ns,
                    identity=item.identity,
                    binding=item.binding,
                )
            if not item.future.done():
                item.future.set_result(result)
        # COM apartment teardown must run on this owner thread.
        try:
            self._native.close()
        except Exception:
            pass

    def _fulfill(self, req: _CaptureRequest) -> CaptureNowResult:
        if self._stop.is_set():
            return self._fail(
                SHUTTING_DOWN,
                req.requested_at_ns,
                identity=req.identity,
                binding=req.binding,
            )
        if self._enumerator is not None:
            verified = resolve_arti_stardew_window(
                self._enumerator, pinned=req.binding
            )
            if not verified.ok or verified.binding is None:
                return self._fail(
                    verified.reason or IDENTITY_UNAVAILABLE,
                    req.requested_at_ns,
                    identity=req.identity,
                    binding=req.binding,
                )
            binding = verified.binding
        elif getattr(self._native, "is_native", False):
            # Production/native capture may never trust the earlier binding
            # when the OS identity cannot be revalidated for this request.
            return self._fail(
                IDENTITY_UNAVAILABLE,
                req.requested_at_ns,
                identity=req.identity,
                binding=req.binding,
            )
        else:
            # Synthetic non-native tests may inject a fake without an OS
            # enumerator. This path is not a production WGC capability.
            binding = req.binding

        if binding.minimized:
            return self._fail(
                MINIMIZED,
                req.requested_at_ns,
                identity=req.identity,
                binding=binding,
            )

        if isinstance(self._native, UnavailableWgcNative):
            return self._fail(
                self._native.reason,
                req.requested_at_ns,
                identity=req.identity,
                binding=binding,
            )

        reused_native_session = bool(
            getattr(self._native, "is_native", False)
            and self._hwnd == binding.hwnd
        )
        try:
            if self._hwnd != binding.hwnd:
                self._native.create_for_window(binding.hwnd)
                self._hwnd = binding.hwnd
        except Exception as exc:
            known = {
                PLATFORM_UNAVAILABLE,
                SHUTTING_DOWN,
                DEVICE_LOSS,
                HWND_INVALID,
                COM_APARTMENT_REQUIRED,
            }
            text = str(exc) or CAPTURE_UNAVAILABLE
            if text in known:
                reason = text
            elif isinstance(self._native, WindowsWgcNative):
                reason = PLATFORM_UNAVAILABLE
            else:
                reason = CAPTURE_UNAVAILABLE
            return self._fail(
                reason,
                req.requested_at_ns,
                identity=req.identity,
                binding=binding,
            )

        if reused_native_session:
            discarded = self._discard_pre_request_native_buffer()
            if not discarded:
                return self._fail(
                    SHUTTING_DOWN if self._stop.is_set() else CAPTURE_UNAVAILABLE,
                    req.requested_at_ns,
                    identity=req.identity,
                    binding=binding,
                )

        frame = self._take_fresh(req.requested_at_ns)
        if self._stop.is_set():
            if frame is not None:
                try:
                    self._native.release_frame(frame)
                except Exception:
                    pass
            return self._fail(
                SHUTTING_DOWN,
                req.requested_at_ns,
                identity=req.identity,
                binding=binding,
            )
        if frame is None:
            return self._fail(
                NO_FRESH_FRAME,
                req.requested_at_ns,
                identity=req.identity,
                binding=binding,
            )
        try:
            return self._accept_or_reject(req, binding, frame)
        finally:
            try:
                self._native.release_frame(frame)
            except Exception:
                pass

    def _discard_pre_request_native_buffer(self) -> bool:
        """Discard the fixed two-frame native pool before reusing a session.

        The request timestamp is taken on the caller before the request reaches
        this owner thread. The WinRT backend creates a two-buffer frame pool;
        therefore at most two frames can already be buffered from before this
        request. Draining that exact bound before the fresh wait means any
        subsequently returned frame was queued after this request boundary.
        Frames discarded here are never evidence and are always released.
        """
        for _ in range(NATIVE_PRE_REQUEST_BUFFER_BOUND):
            if self._stop.is_set():
                return False
            try:
                frame = self._native.try_take_next_frame()
            except Exception:
                return False
            if frame is None:
                return True
            try:
                self._native.release_frame(frame)
            except Exception:
                pass
        return True

    def _take_fresh(self, requested_at_ns: int) -> NativeWgcFrame | None:
        deadline = time.monotonic() + self._fresh_wait_s
        latest: NativeWgcFrame | None = None
        while True:
            if self._stop.is_set():
                return None
            frame = self._native.try_take_next_frame()
            if frame is not None:
                if self._stop.is_set():
                    try:
                        self._native.release_frame(frame)
                    except Exception:
                        pass
                    return None
                if frame.captured_at_ns >= requested_at_ns:
                    if latest is not None:
                        try:
                            self._native.release_frame(latest)
                        except Exception:
                            pass
                    return frame
                if latest is not None:
                    try:
                        self._native.release_frame(latest)
                    except Exception:
                        pass
                latest = frame
            if time.monotonic() >= deadline:
                return latest
            self._wait_s(0.0)

    def _accept_or_reject(
        self,
        req: _CaptureRequest,
        binding: ArtiStardewWindowBinding,
        frame: NativeWgcFrame,
    ) -> CaptureNowResult:
        now_ns = int(self._clock_ns())
        if frame.hwnd_invalid:
            self._hwnd = None
            return self._fail(
                HWND_INVALID,
                req.requested_at_ns,
                identity=req.identity,
                binding=binding,
            )
        if frame.device_lost:
            self._hwnd = None
            try:
                self._native.recreate_pool()
            except Exception:
                pass
            return self._fail(
                DEVICE_LOSS,
                req.requested_at_ns,
                identity=req.identity,
                binding=binding,
            )
        if frame.protected:
            return self._fail(
                PROTECTED_CONTENT,
                req.requested_at_ns,
                identity=req.identity,
                binding=binding,
            )
        if frame.blank or not frame.pixels or _pixels_blank(frame.pixels):
            return self._fail(
                BLANK_FRAME,
                req.requested_at_ns,
                identity=req.identity,
                binding=binding,
            )
        if frame.captured_at_ns < req.requested_at_ns:
            return self._fail(
                STALE_FRAME,
                req.requested_at_ns,
                identity=req.identity,
                binding=binding,
                captured_at_ns=frame.captured_at_ns,
                capture_id=frame.capture_id,
            )
        age = now_ns - frame.captured_at_ns
        if age > self._max_age_ns:
            return self._fail(
                STALE_FRAME,
                req.requested_at_ns,
                identity=req.identity,
                binding=binding,
                captured_at_ns=frame.captured_at_ns,
                capture_id=frame.capture_id,
                freshness_ns=age,
            )
        client_w, client_h = _client_size(binding.client_rect)
        if (
            frame.content_width <= 0
            or frame.content_height <= 0
            or frame.content_width != client_w
            or frame.content_height != client_h
        ):
            try:
                self._native.recreate_pool()
            except Exception:
                pass
            return self._fail(
                GEOMETRY_MISMATCH,
                req.requested_at_ns,
                identity=req.identity,
                binding=binding,
                captured_at_ns=frame.captured_at_ns,
                capture_id=frame.capture_id,
                content_size=(frame.content_width, frame.content_height),
            )
        return CaptureNowResult(
            ok=True,
            reason=None,
            pixels=frame.pixels,
            capture_id=frame.capture_id,
            requested_at_ns=req.requested_at_ns,
            captured_at_ns=frame.captured_at_ns,
            freshness_ns=max(0, now_ns - frame.captured_at_ns),
            content_size=(frame.content_width, frame.content_height),
            client_rect=binding.client_rect,
            hwnd=binding.hwnd,
            pid=binding.pid,
            creation_time=binding.creation_time,
            identity=req.identity,
            viewport_transform_revision=req.identity.viewport_transform_revision,
            ui_pointer_available=False,
            image_to_pointer_available=False,
            occlusion=OCCLUSION_UNKNOWN,
            owner_thread_ident=threading.get_ident(),
            frame_pool_token=self._native.pool_token(),
        )

    def _fail(
        self,
        reason: str,
        requested_at_ns: int,
        *,
        identity: G2CaptureIdentity | None,
        binding: ArtiStardewWindowBinding | None,
        captured_at_ns: int | None = None,
        capture_id: str | None = None,
        freshness_ns: int | None = None,
        content_size: tuple[int, int] | None = None,
    ) -> CaptureNowResult:
        return CaptureNowResult(
            ok=False,
            reason=reason,
            pixels=None,
            capture_id=capture_id,
            requested_at_ns=requested_at_ns,
            captured_at_ns=captured_at_ns,
            freshness_ns=freshness_ns,
            content_size=content_size,
            client_rect=None if binding is None else binding.client_rect,
            hwnd=None if binding is None else binding.hwnd,
            pid=None if binding is None else binding.pid,
            creation_time=None if binding is None else binding.creation_time,
            identity=identity,
            viewport_transform_revision=(
                None if identity is None else identity.viewport_transform_revision
            ),
            ui_pointer_available=False,
            image_to_pointer_available=False,
            occlusion=OCCLUSION_UNKNOWN,
            owner_thread_ident=threading.get_ident(),
            frame_pool_token=self._native.pool_token(),
        )


def _client_size(rect: tuple[int, int, int, int]) -> tuple[int, int]:
    left, top, right, bottom = rect
    return (max(0, right - left), max(0, bottom - top))


def _pixels_blank(pixels: bytes) -> bool:
    if not pixels:
        return True
    return all(b == 0 for b in pixels)


def open_platform_owner(**kwargs) -> StardewWgcCaptureOwner:
    """Production constructor with mandatory per-capture OS identity revalidation."""
    if kwargs.get("enumerator") is None:
        try:
            kwargs["enumerator"] = platform_window_enumerator()
        except Exception:
            # A native owner with no revalidator is rejected by _fulfill.
            kwargs["enumerator"] = None
    return StardewWgcCaptureOwner(**kwargs)