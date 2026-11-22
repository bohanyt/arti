"""printwindow.py: native PrintWindow capture for ui_op.py `shot` (and the DPI-awareness call ui_op makes at start).

Vendored verbatim from the S3 R6 harness (artifacts/s3-audio-closure-r6-20261006/tools/r6_cli.py, functions dpi_aware,
geometry, _grab, _printwindow, uniform, luma_std, luma_gate, crop, client_gate, encode_png) so the operator tools no
longer load a file from another worktree. Windows only (ctypes WinDLL); stdlib only, Pillow optional for PNG encoding.
Read-only: captures pixels, sends no input.
"""
import ctypes, io, statistics, struct, zlib
from ctypes import wintypes as W

PNG_MAGIC = b'\x89PNG\r\n\x1a\n'
# Near-uniform capture gate: luminance standard deviation over a 64 x 36 grid of the CLIENT-area pixels (R6 value).
# Coarse sanity check only, not a readiness proof.
LUMA_STD_MIN = 6.0
LUMA_GRID = (64, 36)

# ---------------------------------------------------------------- native (ctypes only)
k32 = ctypes.WinDLL('kernel32', use_last_error=True)
u32 = ctypes.WinDLL('user32', use_last_error=True)
g32 = ctypes.WinDLL('gdi32', use_last_error=True)
k32.OpenProcess.argtypes = [W.DWORD, W.BOOL, W.DWORD]; k32.OpenProcess.restype = W.HANDLE
k32.GetExitCodeProcess.argtypes = [W.HANDLE, ctypes.POINTER(W.DWORD)]; k32.GetExitCodeProcess.restype = W.BOOL
k32.CloseHandle.argtypes = [W.HANDLE]; k32.CloseHandle.restype = W.BOOL
for _n in ('IsWindow', 'IsWindowVisible', 'IsIconic', 'IsHungAppWindow'):
    getattr(u32, _n).argtypes = [W.HWND]; getattr(u32, _n).restype = W.BOOL
u32.GetWindowThreadProcessId.argtypes = [W.HWND, ctypes.POINTER(W.DWORD)]; u32.GetWindowThreadProcessId.restype = W.DWORD
u32.GetForegroundWindow.restype = W.HWND
u32.GetWindowTextW.argtypes = [W.HWND, W.LPWSTR, ctypes.c_int]
u32.SendMessageTimeoutW.argtypes = [W.HWND, W.UINT, W.WPARAM, W.LPARAM, W.UINT, W.UINT, ctypes.POINTER(ctypes.c_size_t)]
u32.SendMessageTimeoutW.restype = ctypes.c_size_t
u32.GetWindowRect.argtypes = [W.HWND, ctypes.POINTER(W.RECT)]; u32.GetWindowRect.restype = W.BOOL
u32.GetDC.argtypes = [W.HWND]; u32.GetDC.restype = W.HDC
u32.ReleaseDC.argtypes = [W.HWND, W.HDC]; u32.ReleaseDC.restype = ctypes.c_int
u32.PrintWindow.argtypes = [W.HWND, W.HDC, W.UINT]; u32.PrintWindow.restype = W.BOOL
g32.CreateCompatibleDC.argtypes = [W.HDC]; g32.CreateCompatibleDC.restype = W.HDC
g32.CreateCompatibleBitmap.argtypes = [W.HDC, ctypes.c_int, ctypes.c_int]; g32.CreateCompatibleBitmap.restype = W.HBITMAP
g32.SelectObject.argtypes = [W.HDC, W.HGDIOBJ]; g32.SelectObject.restype = W.HGDIOBJ
g32.BitBlt.argtypes = [W.HDC, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, W.HDC, ctypes.c_int, ctypes.c_int, W.DWORD]
g32.BitBlt.restype = W.BOOL
g32.DeleteObject.argtypes = [W.HGDIOBJ]; g32.DeleteObject.restype = W.BOOL
g32.DeleteDC.argtypes = [W.HDC]; g32.DeleteDC.restype = W.BOOL
u32.GetClientRect.argtypes = [W.HWND, ctypes.POINTER(W.RECT)]; u32.GetClientRect.restype = W.BOOL
u32.ClientToScreen.argtypes = [W.HWND, ctypes.POINTER(W.POINT)]; u32.ClientToScreen.restype = W.BOOL
u32.WindowFromPoint.argtypes = [W.POINT]; u32.WindowFromPoint.restype = W.HWND
u32.IsChild.argtypes = [W.HWND, W.HWND]; u32.IsChild.restype = W.BOOL
u32.GetAncestor.argtypes = [W.HWND, W.UINT]; u32.GetAncestor.restype = W.HWND
GA_ROOT = 2


class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [('biSize', W.DWORD), ('biWidth', W.LONG), ('biHeight', W.LONG), ('biPlanes', W.WORD),
                ('biBitCount', W.WORD), ('biCompression', W.DWORD), ('biSizeImage', W.DWORD),
                ('biXPelsPerMeter', W.LONG), ('biYPelsPerMeter', W.LONG), ('biClrUsed', W.DWORD), ('biClrImportant', W.DWORD)]


class BITMAPINFO(ctypes.Structure):
    _fields_ = [('bmiHeader', BITMAPINFOHEADER), ('bmiColors', W.DWORD * 3)]


g32.GetDIBits.argtypes = [W.HDC, W.HBITMAP, W.UINT, W.UINT, ctypes.c_void_p, ctypes.POINTER(BITMAPINFO), W.UINT]
g32.GetDIBits.restype = ctypes.c_int
PW_RENDERFULLCONTENT, SRCCOPY, CAPTUREBLT, WM_NULL, SMTO_ABORTIFHUNG = 0x2, 0x00CC0020, 0x40000000, 0x0, 0x2


def dpi_aware():
    try:  # physical pixels for GetWindowRect/BitBlt; failure (already set / old OS) is harmless
        u32.SetProcessDpiAwarenessContext.argtypes = [ctypes.c_void_p]
        return bool(u32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4)))
    except (AttributeError, OSError):
        return False


def _dib(memdc, bmp, w, h):
    """Call only after the bitmap has been deselected from memdc (review m9; GetDIBits requirement)."""
    bi = BITMAPINFO()
    bi.bmiHeader.biSize = ctypes.sizeof(BITMAPINFOHEADER)
    bi.bmiHeader.biWidth, bi.bmiHeader.biHeight = w, -h  # top-down rows
    bi.bmiHeader.biPlanes, bi.bmiHeader.biBitCount, bi.bmiHeader.biCompression = 1, 32, 0
    buf = ctypes.create_string_buffer(w * h * 4)
    if g32.GetDIBits(memdc, bmp, 0, h, buf, ctypes.byref(bi), 0) != h:
        raise OSError(ctypes.get_last_error(), 'GetDIBits failed')
    return buf.raw  # BGRA/BGRX, top-down


def geometry(hwnd):
    """(window rect, client box inside the window image, client rect on screen) in the calling thread's DPI space."""
    wr, cr, pt = W.RECT(), W.RECT(), W.POINT(0, 0)
    if not (u32.GetWindowRect(hwnd, ctypes.byref(wr)) and u32.GetClientRect(hwnd, ctypes.byref(cr))
            and u32.ClientToScreen(hwnd, ctypes.byref(pt))):
        raise OSError(ctypes.get_last_error(), 'window geometry unavailable')
    ox, oy = pt.x - wr.left, pt.y - wr.top
    return ([wr.left, wr.top, wr.right, wr.bottom], [ox, oy, ox + cr.right, oy + cr.bottom],
            [pt.x, pt.y, pt.x + cr.right, pt.y + cr.bottom])


def _grab(hwnd, method):
    rect, client, _ = geometry(hwnd)
    w, h = rect[2] - rect[0], rect[3] - rect[1]
    if w <= 0 or h <= 0:
        raise OSError(0, f'empty window rect {rect}')
    screen = u32.GetDC(None)
    memdc = g32.CreateCompatibleDC(screen)
    bmp = g32.CreateCompatibleBitmap(screen, w, h)
    old = g32.SelectObject(memdc, bmp)
    selected = True
    try:
        if method == 'PrintWindow':
            ok = u32.PrintWindow(hwnd, memdc, PW_RENDERFULLCONTENT)
        else:
            ok = g32.BitBlt(memdc, 0, 0, w, h, screen, rect[0], rect[1], SRCCOPY | CAPTUREBLT)
        g32.SelectObject(memdc, old); selected = False  # deselect BEFORE GetDIBits (review m9)
        if not ok:
            raise OSError(ctypes.get_last_error(), method + ' failed')
        client = [max(0, client[0]), max(0, client[1]), min(w, client[2]), min(h, client[3])]
        return dict(bgra=_dib(memdc, bmp, w, h), w=w, h=h, rect=rect, client=client, method=method)
    finally:
        if selected:
            g32.SelectObject(memdc, old)
        g32.DeleteObject(bmp); g32.DeleteDC(memdc); u32.ReleaseDC(None, screen)


def _printwindow(hwnd):
    """PrintWindow in the target window's own DPI awareness (thread-local), so the bitmap matches what it renders."""
    prev = None
    try:
        u32.GetWindowDpiAwarenessContext.argtypes = [W.HWND]; u32.GetWindowDpiAwarenessContext.restype = ctypes.c_void_p
        u32.SetThreadDpiAwarenessContext.argtypes = [ctypes.c_void_p]; u32.SetThreadDpiAwarenessContext.restype = ctypes.c_void_p
        prev = u32.SetThreadDpiAwarenessContext(u32.GetWindowDpiAwarenessContext(hwnd))
    except AttributeError:
        pass
    try:
        return _grab(hwnd, 'PrintWindow')
    finally:
        if prev:
            u32.SetThreadDpiAwarenessContext(prev)


def uniform(bgra):
    px = bgra[:3]
    return all(bgra[i:i + 3] == px for i in range(0, len(bgra), 4 * 97))  # sampled; a world frame is never uniform


def luma_std(bgra, w, h, grid=LUMA_GRID):
    """Population standard deviation of Rec.709 luminance (0..255) over a grid[0] x grid[1] sample of pixel centres."""
    gx, gy = min(grid[0], w), min(grid[1], h)
    vals = []
    for j in range(gy):
        y = int((j + 0.5) * h / gy)
        for i in range(gx):
            o = (y * w + int((i + 0.5) * w / gx)) * 4
            vals.append(0.0722 * bgra[o] + 0.7152 * bgra[o + 1] + 0.2126 * bgra[o + 2])
    return statistics.pstdev(vals) if len(vals) > 1 else 0.0


def luma_gate(bgra, w, h):
    """(ok, std): ok only if the luminance spread is at least LUMA_STD_MIN. Coarse sanity check, NOT a readiness proof."""
    s = luma_std(bgra, w, h)
    return s >= LUMA_STD_MIN, s


def crop(bgra, w, h, box):
    x0, y0, x1, y1 = max(0, box[0]), max(0, box[1]), min(w, box[2]), min(h, box[3])
    if x1 <= x0 or y1 <= y0:
        return b'', 0, 0
    return b''.join(bytes(bgra[(y * w + x0) * 4:(y * w + x1) * 4]) for y in range(y0, y1)), x1 - x0, y1 - y0


def client_gate(bgra, w, h, client):
    """(ok, std, uniform) computed on the CLIENT area only (review M2: a light title bar must not rescue a black client)."""
    c, cw, ch = crop(bgra, w, h, client)
    if not c:
        return False, 0.0, True
    u = uniform(c)
    ok, s = luma_gate(c, cw, ch)
    return ok and not u, s, u


def encode_png(bgra, w, h):
    try:
        from PIL import Image  # Pillow 12.2.0 is importable in the project venv (checked 2026-10-06)
        img = Image.frombuffer('RGB', (w, h), bgra, 'raw', 'BGRX', 0, 1)
        b = io.BytesIO(); img.save(b, format='PNG'); return b.getvalue(), 'Pillow'
    except ImportError:
        rows = bytearray()
        for y in range(h):
            row = bgra[y * w * 4:(y + 1) * w * 4]
            rgb = bytearray(w * 3); rgb[0::3] = row[2::4]; rgb[1::3] = row[1::4]; rgb[2::3] = row[0::4]
            rows += b'\x00' + rgb
        chunk = lambda t, d: struct.pack('>I', len(d)) + t + d + struct.pack('>I', zlib.crc32(t + d) & 0xffffffff)
        return (PNG_MAGIC + chunk(b'IHDR', struct.pack('>IIBBBBB', w, h, 8, 2, 0, 0, 0)) +
                chunk(b'IDAT', zlib.compress(bytes(rows), 6)) + chunk(b'IEND', b'')), 'zlib'


