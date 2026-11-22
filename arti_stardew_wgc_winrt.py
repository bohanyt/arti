"""Windows-only WinRT/D3D Graphics Capture interop for the G4 HWND adapter.

Isolates COM / WinRT / Direct3D ownership from StardewWgcCaptureOwner.
This module is not a second WGC stack, geometry authority, or visual
framework. The capture owner remains the single live session owner.

CreateForWindow(HWND) only. The interop vtable may contain an unused
adjacent slot; this module never invokes a display/desktop constructor.

Cloud/Linux: import-safe, available() is False. Loading this file is not
native WGC proof. Real RoGetActivationFactory / D3D / frame-pool /
StartCapture is exercised only on Windows with those APIs present.
"""

from __future__ import annotations

import sys
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Protocol

from arti_stardew_window_binding import PLATFORM_UNAVAILABLE

HWND_INVALID = "HWND_INVALID"
DEVICE_LOSS = "DEVICE_LOSS"
CAPTURE_UNAVAILABLE = "CAPTURE_UNAVAILABLE"
SHUTTING_DOWN = "SHUTTING_DOWN"
COM_APARTMENT_REQUIRED = "COM_APARTMENT_REQUIRED"

# DirectXPixelFormat.B8G8R8A8UIntNormalized
_PIXEL_BGRA = 87
_POOL_BUFFERS = 2

# DXGI_ERROR_DEVICE_REMOVED / RESET
_DXGI_DEVICE_REMOVED = 0x887A0005
_DXGI_DEVICE_RESET = 0x887A0007


@dataclass(frozen=True)
class WinrtRawFrame:
    """CPU-side frame envelope. Fake interop never holds private user pixels."""

    capture_id: str
    captured_at_ns: int
    content_width: int
    content_height: int
    pixels: bytes
    blank: bool = False
    protected: bool = False
    device_lost: bool = False
    hwnd_invalid: bool = False


class WgcWinrtSession(Protocol):
    hwnd: int
    closed: bool

    def pool_token(self) -> str: ...

    def try_take_frame(self) -> WinrtRawFrame | None: ...

    def release_frame(self, frame: WinrtRawFrame) -> None: ...

    def recreate_pool(self) -> None: ...

    def close(self) -> None: ...

    def resource_flags(self) -> dict[str, bool]: ...


class WgcWinrtInterop(Protocol):
    def available(self) -> bool: ...

    def ensure_owner_apartment(self) -> int: ...

    def create_session_for_window(self, hwnd: int) -> WgcWinrtSession: ...

    def release_apartment_if_owner_thread(self) -> None: ...


@dataclass
class FakeWgcWinrtSession:
    """Synthetic CreateForWindow session. No WinRT, no pixels from a user window."""

    hwnd: int
    token: str
    closed: bool = False
    released: list[str] = field(default_factory=list)
    frames: list[WinrtRawFrame] = field(default_factory=list)
    recreated: int = 0
    released_pools: int = 0
    resources: dict[str, bool] = field(
        default_factory=lambda: {
            "item": True,
            "d3d_device": True,
            "frame_pool": True,
            "capture_session": True,
        }
    )

    def pool_token(self) -> str:
        return self.token

    def try_take_frame(self) -> WinrtRawFrame | None:
        if self.closed or not self.frames:
            return None
        return self.frames.pop(0)

    def release_frame(self, frame: WinrtRawFrame) -> None:
        self.released.append(frame.capture_id)

    def recreate_pool(self) -> None:
        if self.closed:
            return
        if self.resources.get("frame_pool"):
            self.released_pools += 1
        self.recreated += 1
        self.token = f"{self.token}-r{self.recreated}"
        self.resources["frame_pool"] = True

    def close(self) -> None:
        self.closed = True
        if self.resources.get("frame_pool"):
            self.released_pools += 1
        for key in list(self.resources):
            self.resources[key] = False

    def resource_flags(self) -> dict[str, bool]:
        return dict(self.resources)

    def push(self, frame: WinrtRawFrame) -> None:
        self.frames.append(frame)


class FakeWgcWinrtInterop:
    """Injected double for constructor success/failure seams."""

    def __init__(
        self,
        *,
        available: bool = True,
        create_error: str | None = None,
        auto_frame: WinrtRawFrame | None = None,
    ) -> None:
        self._available = available
        self.create_error = create_error
        self.auto_frame = auto_frame
        self.created_hwnds: list[int] = []
        self.sessions: list[FakeWgcWinrtSession] = []
        self.apartment_threads: list[int] = []
        self.apartment_releases: list[int] = []
        self._apartment_ident: int | None = None

    def available(self) -> bool:
        return self._available

    def ensure_owner_apartment(self) -> int:
        ident = threading.get_ident()
        if self._apartment_ident is None:
            self._apartment_ident = ident
        elif ident != self._apartment_ident:
            raise RuntimeError(COM_APARTMENT_REQUIRED)
        self.apartment_threads.append(ident)
        return ident

    def create_session_for_window(self, hwnd: int) -> FakeWgcWinrtSession:
        if not self._available:
            raise RuntimeError(PLATFORM_UNAVAILABLE)
        if int(hwnd) <= 0:
            raise RuntimeError(HWND_INVALID)
        if self.create_error:
            raise RuntimeError(self.create_error)
        self.ensure_owner_apartment()
        self.created_hwnds.append(int(hwnd))
        session = FakeWgcWinrtSession(
            hwnd=int(hwnd),
            token=f"winrt-pool-{int(hwnd):x}-{uuid.uuid4().hex[:8]}",
        )
        if self.auto_frame is not None:
            session.push(self.auto_frame)
        self.sessions.append(session)
        return session

    def release_apartment_if_owner_thread(self) -> None:
        ident = threading.get_ident()
        if self._apartment_ident is not None and ident != self._apartment_ident:
            return
        self.apartment_releases.append(ident)
        self._apartment_ident = None


def platform_winrt_interop() -> WgcWinrtInterop:
    return CtypesWgcWinrtInterop()


class CtypesWgcWinrtInterop:
    """Real Windows Graphics Capture / WinRT / D3D11 seam via ctypes.

    available() probes factories and GraphicsCaptureSession.IsSupported.
    It does not CreateForWindow and does not copy pixels.
    """

    def __init__(self) -> None:
        self._api: _WinrtApi | None = None
        self._apartment_ident: int | None = None
        self._uninit = False
        self._probe_reason: str | None = None
        self._probed = False

    def available(self) -> bool:
        return self._ensure_api() is not None

    def _ensure_api(self) -> _WinrtApi | None:
        if self._probed:
            return self._api
        self._probed = True
        if sys.platform != "win32":
            self._probe_reason = PLATFORM_UNAVAILABLE
            return None
        try:
            api = _WinrtApi.load()
        except Exception:
            self._probe_reason = PLATFORM_UNAVAILABLE
            return None
        if api is None or not api.capture_supported():
            self._probe_reason = PLATFORM_UNAVAILABLE
            return None
        self._api = api
        return api

    def ensure_owner_apartment(self) -> int:
        api = self._ensure_api()
        if api is None:
            raise RuntimeError(self._probe_reason or PLATFORM_UNAVAILABLE)
        ident = threading.get_ident()
        if self._apartment_ident is None:
            self._uninit = api.initialize_mta()
            self._apartment_ident = ident
        elif ident != self._apartment_ident:
            raise RuntimeError(COM_APARTMENT_REQUIRED)
        return ident

    def create_session_for_window(self, hwnd: int) -> WgcWinrtSession:
        api = self._ensure_api()
        if api is None:
            raise RuntimeError(self._probe_reason or PLATFORM_UNAVAILABLE)
        self.ensure_owner_apartment()
        if int(hwnd) <= 0:
            raise RuntimeError(HWND_INVALID)
        return api.create_for_window(int(hwnd))

    def release_apartment_if_owner_thread(self) -> None:
        if self._apartment_ident is None:
            return
        if threading.get_ident() != self._apartment_ident:
            return
        if self._uninit and self._api is not None:
            try:
                self._api.uninitialize()
            except Exception:
                pass
        self._uninit = False
        self._apartment_ident = None


class _ComPtr:
    __slots__ = ("ptr",)

    def __init__(self, ptr: int | None = None) -> None:
        self.ptr = int(ptr) if ptr else 0

    def __bool__(self) -> bool:
        return self.ptr != 0

    def attach(self, ptr: int) -> None:
        self.release()
        self.ptr = int(ptr) if ptr else 0

    def detach(self) -> int:
        ptr = self.ptr
        self.ptr = 0
        return ptr

    def release(self) -> None:
        if not self.ptr:
            return
        try:
            _vt_call(self.ptr, 2, _ULONG, ())
        except Exception:
            pass
        self.ptr = 0

    def __del__(self) -> None:
        try:
            self.release()
        except Exception:
            pass


# ctypes prototypes filled on Windows only.
_HRESULT = None  # type: ignore[assignment]
_ULONG = None  # type: ignore[assignment]
_FUNCTYPE = None  # type: ignore[assignment]


def _vt_call(ptr: int, index: int, restype, argtypes, *args):
    import ctypes

    vptr = ctypes.cast(ptr, ctypes.POINTER(ctypes.c_void_p))
    vtbl = ctypes.cast(vptr[0], ctypes.POINTER(ctypes.c_void_p))
    proto = _FUNCTYPE(restype, ctypes.c_void_p, *argtypes)
    return proto(vtbl[index])(ptr, *args)


class _WinrtApi:
    def __init__(self, ctypes_mod, combase, d3d11) -> None:
        self.ct = ctypes_mod
        self.combase = combase
        self.d3d11 = d3d11

    @classmethod
    def load(cls) -> _WinrtApi | None:
        if sys.platform != "win32":
            return None
        import ctypes
        from ctypes import wintypes

        global _HRESULT, _ULONG, _FUNCTYPE
        _HRESULT = ctypes.HRESULT
        _ULONG = wintypes.ULONG
        _FUNCTYPE = ctypes.WINFUNCTYPE
        try:
            combase = ctypes.WinDLL("combase")
            d3d11 = ctypes.WinDLL("d3d11")
        except OSError:
            return None
        needed = (
            "RoInitialize",
            "RoUninitialize",
            "RoGetActivationFactory",
            "WindowsCreateString",
            "WindowsDeleteString",
        )
        for name in needed:
            if not hasattr(combase, name):
                return None
        if not hasattr(d3d11, "D3D11CreateDevice"):
            return None
        if not hasattr(d3d11, "CreateDirect3D11DeviceFromDXGIDevice"):
            return None
        combase.RoInitialize.argtypes = [ctypes.c_uint]
        combase.RoInitialize.restype = ctypes.HRESULT
        combase.RoUninitialize.argtypes = []
        combase.WindowsCreateString.argtypes = [
            ctypes.c_wchar_p,
            ctypes.c_uint,
            ctypes.POINTER(ctypes.c_void_p),
        ]
        combase.WindowsCreateString.restype = ctypes.HRESULT
        combase.WindowsDeleteString.argtypes = [ctypes.c_void_p]
        combase.WindowsDeleteString.restype = ctypes.HRESULT
        combase.RoGetActivationFactory.argtypes = [
            ctypes.c_void_p,
            ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_void_p),
        ]
        combase.RoGetActivationFactory.restype = ctypes.HRESULT
        d3d11.D3D11CreateDevice.restype = ctypes.HRESULT
        d3d11.CreateDirect3D11DeviceFromDXGIDevice.restype = ctypes.HRESULT
        d3d11.CreateDirect3D11DeviceFromDXGIDevice.argtypes = [
            ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_void_p),
        ]
        return cls(ctypes, combase, d3d11)

    def initialize_mta(self) -> bool:
        # RO_INIT_MULTITHREADED = 1. S_FALSE = already initialized.
        hr = int(self.combase.RoInitialize(1))
        if hr in (0, 1):
            return hr == 0
        # Fallback CoInitializeEx MTA if RoInitialize rejects the mode.
        ole32 = self.ct.WinDLL("ole32")
        hr2 = int(ole32.CoInitializeEx(None, 0x0))  # COINIT_MULTITHREADED
        if hr2 in (0, 1):
            return hr2 == 0
        raise RuntimeError(PLATFORM_UNAVAILABLE)

    def uninitialize(self) -> None:
        try:
            self.combase.RoUninitialize()
        except Exception:
            pass

    def capture_supported(self) -> bool:
        factory = _ComPtr()
        try:
            hr = self._activation_factory(
                "Windows.Graphics.Capture.GraphicsCaptureSession",
                "2224A540-5974-49AA-B232-0882536F4CB5",
                factory,
            )
            if hr != 0 or not factory:
                return False
            supported = self.ct.c_ubyte(0)
            # IInspectable: 0-5, IsSupported at 6
            hr = int(
                _vt_call(
                    factory.ptr,
                    6,
                    _HRESULT,
                    (self.ct.POINTER(self.ct.c_ubyte),),
                    self.ct.byref(supported),
                )
            )
            return hr == 0 and bool(supported.value)
        except Exception:
            return False
        finally:
            factory.release()

    def create_for_window(self, hwnd: int) -> CtypesWgcSession:
        item = self._create_item_for_window(hwnd)
        try:
            width, height = self._item_size(item.ptr)
            d3d, winrt_device = self._create_d3d_device()
            try:
                pool = self._create_free_threaded_pool(
                    winrt_device.ptr, width, height
                )
                try:
                    session = self._create_capture_session(pool.ptr, item.ptr)
                    try:
                        self._maybe_disable_cursor(session.ptr)
                        hr = int(_vt_call(session.ptr, 6, _HRESULT, ()))
                        if hr != 0:
                            raise OSError(hr)
                    except Exception:
                        session.release()
                        raise
                except Exception:
                    _close_inspectable(pool.ptr)
                    pool.release()
                    raise
            except Exception:
                winrt_device.release()
                d3d.release()
                raise
        except OSError as exc:
            item.release()
            raise RuntimeError(_reason_from_hresult(int(exc.args[0]))) from exc
        except Exception:
            item.release()
            raise
        return CtypesWgcSession(
            api=self,
            hwnd=hwnd,
            item=item,
            d3d=d3d,
            winrt_device=winrt_device,
            pool=pool,
            session=session,
            width=width,
            height=height,
            token=f"wgc-native-{uuid.uuid4().hex[:12]}",
        )

    def _create_item_for_window(self, hwnd: int) -> _ComPtr:
        factory = _ComPtr()
        item = _ComPtr()
        try:
            hr = self._activation_factory(
                "Windows.Graphics.Capture.GraphicsCaptureItem",
                "3628E81B-3CAC-4C60-B7F4-23CE0E0C3356",
                factory,
            )
            if hr != 0 or not factory:
                raise RuntimeError(PLATFORM_UNAVAILABLE)
            iid_item = _guid(self.ct, "79C3F95B-31F7-4EC2-A464-632EF5D30760")
            out = self.ct.c_void_p()
            # IUnknown 0-2, CreateForWindow at 3. Adjacent vtable slot unused.
            hr = int(
                _vt_call(
                    factory.ptr,
                    3,
                    _HRESULT,
                    (self.ct.c_void_p, self.ct.c_void_p, self.ct.POINTER(self.ct.c_void_p)),
                    self.ct.c_void_p(int(hwnd)),
                    self.ct.byref(iid_item),
                    self.ct.byref(out),
                )
            )
            if hr != 0 or not out.value:
                raise OSError(hr)
            item.attach(int(out.value))
            return item
        finally:
            factory.release()

    def _item_size(self, item_ptr: int) -> tuple[int, int]:
        class SizeInt32(self.ct.Structure):
            _fields_ = [("Width", self.ct.c_int32), ("Height", self.ct.c_int32)]

        size = SizeInt32()
        # IInspectable 0-5, DisplayName 6, Size 7
        hr = int(
            _vt_call(
                item_ptr,
                7,
                _HRESULT,
                (self.ct.POINTER(SizeInt32),),
                self.ct.byref(size),
            )
        )
        if hr != 0 or size.Width <= 0 or size.Height <= 0:
            raise OSError(hr or 1)
        return int(size.Width), int(size.Height)

    def _create_d3d_device(self) -> tuple[_ComPtr, _ComPtr]:
        device = self.ct.c_void_p()
        context = self.ct.c_void_p()
        feature = self.ct.c_uint(0)
        hr = int(
            self.d3d11.D3D11CreateDevice(
                None,
                1,  # HARDWARE
                None,
                0x20,  # BGRA_SUPPORT
                None,
                0,
                7,  # D3D11_SDK_VERSION
                self.ct.byref(device),
                self.ct.byref(feature),
                self.ct.byref(context),
            )
        )
        if context.value:
            _ComPtr(int(context.value)).release()
        if hr != 0 or not device.value:
            raise OSError(hr or 1)
        d3d = _ComPtr(int(device.value))
        dxgi = _ComPtr()
        winrt_device = _ComPtr()
        try:
            iid_dxgi = _guid(self.ct, "54EC77FA-1377-44E6-8C32-88FD5F44C84C")
            dxgi_ptr = self.ct.c_void_p()
            hr = int(
                _vt_call(
                    d3d.ptr,
                    0,
                    _HRESULT,
                    (self.ct.c_void_p, self.ct.POINTER(self.ct.c_void_p)),
                    self.ct.byref(iid_dxgi),
                    self.ct.byref(dxgi_ptr),
                )
            )
            if hr != 0 or not dxgi_ptr.value:
                raise OSError(hr or 1)
            dxgi.attach(int(dxgi_ptr.value))
            inspectable = self.ct.c_void_p()
            hr = int(
                self.d3d11.CreateDirect3D11DeviceFromDXGIDevice(
                    self.ct.c_void_p(dxgi.ptr),
                    self.ct.byref(inspectable),
                )
            )
            if hr != 0 or not inspectable.value:
                raise OSError(hr or 1)
            iid_d3d = _guid(self.ct, "A37624AB-8D5F-4650-9D3E-9EAE3D9BC670")
            out = self.ct.c_void_p()
            hr = int(
                _vt_call(
                    int(inspectable.value),
                    0,
                    _HRESULT,
                    (self.ct.c_void_p, self.ct.POINTER(self.ct.c_void_p)),
                    self.ct.byref(iid_d3d),
                    self.ct.byref(out),
                )
            )
            _ComPtr(int(inspectable.value)).release()
            if hr != 0 or not out.value:
                raise OSError(hr or 1)
            winrt_device.attach(int(out.value))
        except Exception:
            dxgi.release()
            d3d.release()
            raise
        dxgi.release()
        return d3d, winrt_device

    def _create_free_threaded_pool(
        self, winrt_device: int, width: int, height: int
    ) -> _ComPtr:
        factory = _ComPtr()
        pool = _ComPtr()
        try:
            hr = self._activation_factory(
                "Windows.Graphics.Capture.Direct3D11CaptureFramePool",
                "589B103F-6BBC-5DF5-A991-02E28B3B66D5",
                factory,
            )
            if hr != 0 or not factory:
                raise RuntimeError(PLATFORM_UNAVAILABLE)

            class SizeInt32(self.ct.Structure):
                _fields_ = [("Width", self.ct.c_int32), ("Height", self.ct.c_int32)]

            size = SizeInt32(width, height)
            out = self.ct.c_void_p()
            # IInspectable 0-5, CreateFreeThreaded at 6
            hr = int(
                _vt_call(
                    factory.ptr,
                    6,
                    _HRESULT,
                    (
                        self.ct.c_void_p,
                        self.ct.c_int,
                        self.ct.c_int,
                        SizeInt32,
                        self.ct.POINTER(self.ct.c_void_p),
                    ),
                    self.ct.c_void_p(winrt_device),
                    _PIXEL_BGRA,
                    _POOL_BUFFERS,
                    size,
                    self.ct.byref(out),
                )
            )
            if hr != 0 or not out.value:
                raise OSError(hr or 1)
            pool.attach(int(out.value))
            return pool
        finally:
            factory.release()

    def _create_capture_session(self, pool_ptr: int, item_ptr: int) -> _ComPtr:
        out = self.ct.c_void_p()
        # IInspectable 0-5, Recreate 6, TryGetNextFrame 7, FrameArrived add 8,
        # FrameArrived remove 9, CreateCaptureSession 10
        hr = int(
            _vt_call(
                pool_ptr,
                10,
                _HRESULT,
                (self.ct.c_void_p, self.ct.POINTER(self.ct.c_void_p)),
                self.ct.c_void_p(item_ptr),
                self.ct.byref(out),
            )
        )
        if hr != 0 or not out.value:
            raise OSError(hr or 1)
        return _ComPtr(int(out.value))

    def _maybe_disable_cursor(self, session_ptr: int) -> None:
        iid = _guid(self.ct, "2C39AE40-7D2E-5044-804E-8B6799D4CF9E")
        extra = self.ct.c_void_p()
        hr = int(
            _vt_call(
                session_ptr,
                0,
                _HRESULT,
                (self.ct.c_void_p, self.ct.POINTER(self.ct.c_void_p)),
                self.ct.byref(iid),
                self.ct.byref(extra),
            )
        )
        if hr != 0 or not extra.value:
            return
        try:
            # IInspectable 0-5, get 6, put IsCursorCaptureEnabled 7
            _vt_call(
                int(extra.value),
                7,
                _HRESULT,
                (self.ct.c_ubyte,),
                self.ct.c_ubyte(0),
            )
        finally:
            _ComPtr(int(extra.value)).release()

    def _activation_factory(self, class_name: str, iid_text: str, out: _ComPtr) -> int:
        hstr = self.ct.c_void_p()
        hr = int(
            self.combase.WindowsCreateString(
                class_name,
                len(class_name),
                self.ct.byref(hstr),
            )
        )
        if hr != 0:
            return hr
        try:
            iid = _guid(self.ct, iid_text)
            ptr = self.ct.c_void_p()
            hr = int(
                self.combase.RoGetActivationFactory(
                    hstr,
                    self.ct.byref(iid),
                    self.ct.byref(ptr),
                )
            )
            if hr == 0 and ptr.value:
                out.attach(int(ptr.value))
            return hr
        finally:
            self.combase.WindowsDeleteString(hstr)


class CtypesWgcSession:
    """Live CreateForWindow session. Closed/released by the capture owner."""

    def __init__(
        self,
        *,
        api: _WinrtApi,
        hwnd: int,
        item: _ComPtr,
        d3d: _ComPtr,
        winrt_device: _ComPtr,
        pool: _ComPtr,
        session: _ComPtr,
        width: int,
        height: int,
        token: str,
    ) -> None:
        self.api = api
        self.hwnd = hwnd
        self._item = item
        self._d3d = d3d
        self._winrt_device = winrt_device
        self._pool = pool
        self._session = session
        self._width = width
        self._height = height
        self._token = token
        self.closed = False
        self.recreated = 0
        self.released: list[str] = []

    def pool_token(self) -> str:
        return self._token

    def resource_flags(self) -> dict[str, bool]:
        return {
            "item": bool(self._item),
            "d3d_device": bool(self._d3d),
            "frame_pool": bool(self._pool),
            "capture_session": bool(self._session),
        }

    def try_take_frame(self) -> WinrtRawFrame | None:
        if self.closed or not self._pool:
            return None
        ct = self.api.ct
        frame_ptr = ct.c_void_p()
        hr = int(
            _vt_call(
                self._pool.ptr,
                7,
                _HRESULT,
                (ct.POINTER(ct.c_void_p),),
                ct.byref(frame_ptr),
            )
        )
        if hr in (_DXGI_DEVICE_REMOVED, _DXGI_DEVICE_RESET):
            return WinrtRawFrame(
                capture_id=uuid.uuid4().hex,
                captured_at_ns=time.perf_counter_ns(),
                content_width=0,
                content_height=0,
                pixels=b"",
                device_lost=True,
            )
        if hr != 0:
            if _is_hwnd_hr(hr):
                return WinrtRawFrame(
                    capture_id=uuid.uuid4().hex,
                    captured_at_ns=time.perf_counter_ns(),
                    content_width=0,
                    content_height=0,
                    pixels=b"",
                    hwnd_invalid=True,
                )
            return None
        if not frame_ptr.value:
            return None
        raw = _ComPtr(int(frame_ptr.value))
        try:
            return self._read_frame(raw)
        finally:
            _close_inspectable(raw.ptr)
            raw.release()

    def _read_frame(self, raw: _ComPtr) -> WinrtRawFrame:
        ct = self.api.ct

        class SizeInt32(ct.Structure):
            _fields_ = [("Width", ct.c_int32), ("Height", ct.c_int32)]

        size = SizeInt32()
        # Surface 6, SystemRelativeTime 7, ContentSize 8
        hr = int(
            _vt_call(
                raw.ptr,
                8,
                _HRESULT,
                (ct.POINTER(SizeInt32),),
                ct.byref(size),
            )
        )
        if hr != 0:
            raise OSError(hr)
        width, height = int(size.Width), int(size.Height)
        surface = ct.c_void_p()
        hr = int(
            _vt_call(
                raw.ptr,
                6,
                _HRESULT,
                (ct.POINTER(ct.c_void_p),),
                ct.byref(surface),
            )
        )
        pixels = b""
        blank = True
        device_lost = False
        if hr == 0 and surface.value:
            try:
                pixels = _copy_bgra_from_surface(self.api, self._d3d.ptr, int(surface.value))
                blank = not pixels or all(b == 0 for b in pixels)
            except OSError as exc:
                if int(exc.args[0]) in (_DXGI_DEVICE_REMOVED, _DXGI_DEVICE_RESET):
                    device_lost = True
                pixels = b""
                blank = True
            finally:
                _close_inspectable(int(surface.value))
                _ComPtr(int(surface.value)).release()
        return WinrtRawFrame(
            capture_id=uuid.uuid4().hex,
            captured_at_ns=time.perf_counter_ns(),
            content_width=width,
            content_height=height,
            pixels=pixels,
            blank=blank,
            device_lost=device_lost,
        )

    def release_frame(self, frame: WinrtRawFrame) -> None:
        self.released.append(frame.capture_id)

    def recreate_pool(self) -> None:
        if self.closed or not self._pool or not self._winrt_device:
            return
        ct = self.api.ct

        class SizeInt32(ct.Structure):
            _fields_ = [("Width", ct.c_int32), ("Height", ct.c_int32)]

        try:
            self._width, self._height = self.api._item_size(self._item.ptr)
        except Exception:
            pass
        size = SizeInt32(self._width, self._height)
        hr = int(
            _vt_call(
                self._pool.ptr,
                6,
                _HRESULT,
                (ct.c_void_p, ct.c_int, ct.c_int, SizeInt32),
                ct.c_void_p(self._winrt_device.ptr),
                _PIXEL_BGRA,
                _POOL_BUFFERS,
                size,
            )
        )
        if hr != 0:
            raise RuntimeError(_reason_from_hresult(hr))
        self.recreated += 1
        self._token = f"{self._token}-r{self.recreated}"

    def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        if self._session:
            _close_inspectable(self._session.ptr)
            self._session.release()
        if self._pool:
            _close_inspectable(self._pool.ptr)
            self._pool.release()
        if self._item:
            self._item.release()
        if self._winrt_device:
            _close_inspectable(self._winrt_device.ptr)
            self._winrt_device.release()
        if self._d3d:
            self._d3d.release()


def _close_inspectable(ptr: int) -> None:
    if not ptr or _HRESULT is None:
        return
    try:
        import ctypes

        iid = _guid(ctypes, "30D5A829-7FA4-4026-83BB-D75BAE4EA99E")
        closable = ctypes.c_void_p()
        hr = int(
            _vt_call(
                ptr,
                0,
                _HRESULT,
                (ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p)),
                ctypes.byref(iid),
                ctypes.byref(closable),
            )
        )
        if hr == 0 and closable.value:
            try:
                _vt_call(int(closable.value), 6, _HRESULT, ())
            finally:
                _ComPtr(int(closable.value)).release()
    except Exception:
        return


def _copy_bgra_from_surface(api: _WinrtApi, d3d_ptr: int, surface_ptr: int) -> bytes:
    """Map IDirect3DSurface to a CPU BGRA buffer. Unexercised in Cloud."""
    ct = api.ct
    iid_access = _guid(ct, "A9B3D012-3DF2-4EE3-B8D1-8695F457D3C1")
    access = ct.c_void_p()
    hr = int(
        _vt_call(
            surface_ptr,
            0,
            _HRESULT,
            (ct.c_void_p, ct.POINTER(ct.c_void_p)),
            ct.byref(iid_access),
            ct.byref(access),
        )
    )
    if hr != 0 or not access.value:
        raise OSError(hr or 1)
    tex = ct.c_void_p()
    try:
        iid_tex = _guid(ct, "6F15AAF2-D208-4E89-9AB4-489535D34F9C")
        hr = int(
            _vt_call(
                int(access.value),
                3,
                _HRESULT,
                (ct.c_void_p, ct.POINTER(ct.c_void_p)),
                ct.byref(iid_tex),
                ct.byref(tex),
            )
        )
        if hr != 0 or not tex.value:
            raise OSError(hr or 1)
    finally:
        _ComPtr(int(access.value)).release()

    class TexDesc(ct.Structure):
        _fields_ = [
            ("Width", ct.c_uint),
            ("Height", ct.c_uint),
            ("MipLevels", ct.c_uint),
            ("ArraySize", ct.c_uint),
            ("Format", ct.c_uint),
            ("SampleCount", ct.c_uint),
            ("SampleQuality", ct.c_uint),
            ("Usage", ct.c_uint),
            ("BindFlags", ct.c_uint),
            ("CPUAccessFlags", ct.c_uint),
            ("MiscFlags", ct.c_uint),
        ]

    desc = TexDesc()
    _vt_call(int(tex.value), 10, None, (ct.POINTER(TexDesc),), ct.byref(desc))
    staging_desc = TexDesc()
    ct.memmove(ct.byref(staging_desc), ct.byref(desc), ct.sizeof(TexDesc))
    staging_desc.Usage = 3  # STAGING
    staging_desc.BindFlags = 0
    staging_desc.CPUAccessFlags = 0x20000  # READ
    staging_desc.MiscFlags = 0
    staging = ct.c_void_p()
    hr = int(
        _vt_call(
            d3d_ptr,
            5,
            _HRESULT,
            (ct.c_void_p, ct.c_void_p, ct.POINTER(ct.c_void_p)),
            ct.byref(staging_desc),
            None,
            ct.byref(staging),
        )
    )
    if hr != 0 or not staging.value:
        _ComPtr(int(tex.value)).release()
        raise OSError(hr or 1)
    ctx = ct.c_void_p()
    _vt_call(d3d_ptr, 40, None, (ct.POINTER(ct.c_void_p),), ct.byref(ctx))
    if not ctx.value:
        _ComPtr(int(staging.value)).release()
        _ComPtr(int(tex.value)).release()
        raise OSError(1)
    try:
        _vt_call(
            int(ctx.value),
            47,
            None,
            (ct.c_void_p, ct.c_void_p),
            staging.value,
            tex.value,
        )

        class Mapped(ct.Structure):
            _fields_ = [
                ("pData", ct.c_void_p),
                ("RowPitch", ct.c_uint),
                ("DepthPitch", ct.c_uint),
            ]

        mapped = Mapped()
        hr = int(
            _vt_call(
                int(ctx.value),
                14,
                _HRESULT,
                (ct.c_void_p, ct.c_uint, ct.c_uint, ct.c_uint, ct.POINTER(Mapped)),
                staging.value,
                0,
                1,  # D3D11_MAP_READ
                0,
                ct.byref(mapped),
            )
        )
        if hr != 0 or not mapped.pData:
            raise OSError(hr or 1)
        try:
            height = int(desc.Height)
            width = int(desc.Width)
            pitch = int(mapped.RowPitch)
            row_bytes = width * 4
            base = int(mapped.pData)
            rows = [
                ct.string_at(base + (y * pitch), row_bytes) for y in range(height)
            ]
            return b"".join(rows)
        finally:
            _vt_call(
                int(ctx.value),
                15,
                None,
                (ct.c_void_p, ct.c_uint),
                staging.value,
                0,
            )
    finally:
        _ComPtr(int(ctx.value)).release()
        _ComPtr(int(staging.value)).release()
        _ComPtr(int(tex.value)).release()


def _guid(ct, text: str):
    class GUID(ct.Structure):
        _fields_ = [
            ("Data1", ct.c_uint32),
            ("Data2", ct.c_uint16),
            ("Data3", ct.c_uint16),
            ("Data4", ct.c_ubyte * 8),
        ]

    parts = text.split("-")
    data4 = bytes.fromhex(parts[3] + parts[4])
    g = GUID()
    g.Data1 = int(parts[0], 16)
    g.Data2 = int(parts[1], 16)
    g.Data3 = int(parts[2], 16)
    for i, b in enumerate(data4):
        g.Data4[i] = b
    return g


def _is_hwnd_hr(hr: int) -> bool:
    hr = hr & 0xFFFFFFFF
    return hr in {
        0x80070006,  # E_HANDLE
        0x80070057,  # E_INVALIDARG
        0x80010108,  # RPC_E_DISCONNECTED
        0x80004003,  # E_POINTER (treat as invalid target, not desktop fallback)
    }


def _reason_from_hresult(hr: int) -> str:
    hr = hr & 0xFFFFFFFF
    if hr in (_DXGI_DEVICE_REMOVED, _DXGI_DEVICE_RESET):
        return DEVICE_LOSS
    if _is_hwnd_hr(hr):
        return HWND_INVALID
    if hr == 0x80070005:  # E_ACCESSDENIED
        return CAPTURE_UNAVAILABLE
    return CAPTURE_UNAVAILABLE
