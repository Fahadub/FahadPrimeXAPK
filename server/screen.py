"""Screen capture via GDI (no dependencies) with the mouse cursor drawn in.

Encodes to JPEG when Pillow is installed (small and fast), otherwise to PNG
using only the standard library.
"""

from __future__ import annotations

import base64
import ctypes
import io
import struct
import zlib

import winapi

if winapi.IS_WIN:
    from ctypes import wintypes

    gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)
    user32 = winapi.user32

    class BITMAPINFOHEADER(ctypes.Structure):
        _fields_ = [
            ("biSize", wintypes.DWORD),
            ("biWidth", wintypes.LONG),
            ("biHeight", wintypes.LONG),
            ("biPlanes", wintypes.WORD),
            ("biBitCount", wintypes.WORD),
            ("biCompression", wintypes.DWORD),
            ("biSizeImage", wintypes.DWORD),
            ("biXPelsPerMeter", wintypes.LONG),
            ("biYPelsPerMeter", wintypes.LONG),
            ("biClrUsed", wintypes.DWORD),
            ("biClrImportant", wintypes.DWORD),
        ]

    class BITMAPINFO(ctypes.Structure):
        _fields_ = [("bmiHeader", BITMAPINFOHEADER), ("bmiColors", wintypes.DWORD * 3)]

    class CURSORINFO(ctypes.Structure):
        _fields_ = [
            ("cbSize", wintypes.DWORD),
            ("flags", wintypes.DWORD),
            ("hCursor", wintypes.HANDLE),
            ("ptScreenPos", wintypes.POINT),
        ]

    class ICONINFO(ctypes.Structure):
        _fields_ = [
            ("fIcon", wintypes.BOOL),
            ("xHotspot", wintypes.DWORD),
            ("yHotspot", wintypes.DWORD),
            ("hbmMask", wintypes.HBITMAP),
            ("hbmColor", wintypes.HBITMAP),
        ]

    def _sig(dll, name, restype, *argtypes):
        fn = getattr(dll, name)
        fn.restype = restype
        fn.argtypes = list(argtypes)
        return fn

    _GetDC = _sig(user32, "GetDC", wintypes.HDC, wintypes.HWND)
    _ReleaseDC = _sig(user32, "ReleaseDC", ctypes.c_int, wintypes.HWND, wintypes.HDC)
    _CreateCompatibleDC = _sig(gdi32, "CreateCompatibleDC", wintypes.HDC, wintypes.HDC)
    _CreateCompatibleBitmap = _sig(gdi32, "CreateCompatibleBitmap", wintypes.HBITMAP, wintypes.HDC, ctypes.c_int, ctypes.c_int)
    _SelectObject = _sig(gdi32, "SelectObject", wintypes.HGDIOBJ, wintypes.HDC, wintypes.HGDIOBJ)
    _DeleteObject = _sig(gdi32, "DeleteObject", wintypes.BOOL, wintypes.HGDIOBJ)
    _DeleteDC = _sig(gdi32, "DeleteDC", wintypes.BOOL, wintypes.HDC)
    _SetStretchBltMode = _sig(gdi32, "SetStretchBltMode", ctypes.c_int, wintypes.HDC, ctypes.c_int)
    _SetBrushOrgEx = _sig(gdi32, "SetBrushOrgEx", wintypes.BOOL, wintypes.HDC, ctypes.c_int, ctypes.c_int, ctypes.c_void_p)
    _BitBlt = _sig(
        gdi32, "BitBlt", wintypes.BOOL,
        wintypes.HDC, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
        wintypes.HDC, ctypes.c_int, ctypes.c_int, wintypes.DWORD,
    )
    _StretchBlt = _sig(
        gdi32, "StretchBlt", wintypes.BOOL,
        wintypes.HDC, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
        wintypes.HDC, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, wintypes.DWORD,
    )
    _GetDIBits = _sig(
        gdi32, "GetDIBits", ctypes.c_int,
        wintypes.HDC, wintypes.HBITMAP, wintypes.UINT, wintypes.UINT, ctypes.c_void_p,
        ctypes.POINTER(BITMAPINFO), wintypes.UINT,
    )
    _GetCursorInfo = _sig(user32, "GetCursorInfo", wintypes.BOOL, ctypes.POINTER(CURSORINFO))
    _GetIconInfo = _sig(user32, "GetIconInfo", wintypes.BOOL, wintypes.HANDLE, ctypes.POINTER(ICONINFO))
    _DrawIconEx = _sig(
        user32, "DrawIconEx", wintypes.BOOL,
        wintypes.HDC, ctypes.c_int, ctypes.c_int, wintypes.HANDLE, ctypes.c_int, ctypes.c_int,
        wintypes.UINT, wintypes.HBRUSH, wintypes.UINT,
    )

SRCCOPY = 0x00CC0020
HALFTONE = 4


def _draw_cursor(hdc, scale: float) -> None:
    ci = CURSORINFO()
    ci.cbSize = ctypes.sizeof(CURSORINFO)
    if not _GetCursorInfo(ctypes.byref(ci)) or not (ci.flags & 1) or not ci.hCursor:
        return
    hx = hy = 0
    ii = ICONINFO()
    if _GetIconInfo(ci.hCursor, ctypes.byref(ii)):
        hx, hy = ii.xHotspot, ii.yHotspot
        if ii.hbmMask:
            _DeleteObject(ii.hbmMask)
        if ii.hbmColor:
            _DeleteObject(ii.hbmColor)
    x = int(ci.ptScreenPos.x * scale) - hx
    y = int(ci.ptScreenPos.y * scale) - hy
    _DrawIconEx(hdc, x, y, ci.hCursor, 0, 0, 0, None, 3)  # DI_NORMAL, native size (easy to see)


def capture_bgra(max_w: int = 1280, draw_cursor: bool = True) -> tuple[int, int, bytes]:
    """Grab the primary screen scaled to at most ``max_w`` wide. Returns (w, h, BGRA bytes)."""
    sw, sh = winapi.screen_size()
    if not winapi.IS_WIN:
        w, h = min(sw, max_w), int(sh * min(sw, max_w) / sw)
        return w, h, bytes([40, 30, 20, 255]) * (w * h)
    scale = min(1.0, max_w / sw) if max_w > 0 else 1.0
    tw, th = max(1, int(sw * scale)), max(1, int(sh * scale))
    hdc_screen = _GetDC(None)
    hdc_mem = _CreateCompatibleDC(hdc_screen)
    hbmp = _CreateCompatibleBitmap(hdc_screen, tw, th)
    old = _SelectObject(hdc_mem, hbmp)
    try:
        if scale < 1.0:
            _SetStretchBltMode(hdc_mem, HALFTONE)
            _SetBrushOrgEx(hdc_mem, 0, 0, None)
            _StretchBlt(hdc_mem, 0, 0, tw, th, hdc_screen, 0, 0, sw, sh, SRCCOPY)
        else:
            _BitBlt(hdc_mem, 0, 0, tw, th, hdc_screen, 0, 0, SRCCOPY)
        if draw_cursor:
            try:
                _draw_cursor(hdc_mem, scale)
            except Exception:
                pass
        _SelectObject(hdc_mem, old)
        bmi = BITMAPINFO()
        bmi.bmiHeader.biSize = ctypes.sizeof(BITMAPINFOHEADER)
        bmi.bmiHeader.biWidth = tw
        bmi.bmiHeader.biHeight = -th  # top-down rows
        bmi.bmiHeader.biPlanes = 1
        bmi.bmiHeader.biBitCount = 32
        bmi.bmiHeader.biCompression = 0  # BI_RGB
        buf = ctypes.create_string_buffer(tw * th * 4)
        if _GetDIBits(hdc_mem, hbmp, 0, th, buf, ctypes.byref(bmi), 0) == 0:
            raise OSError("GetDIBits failed")
        return tw, th, buf.raw
    finally:
        _DeleteObject(hbmp)
        _DeleteDC(hdc_mem)
        _ReleaseDC(None, hdc_screen)


def _bgra_to_rgb(bgra: bytes) -> bytes:
    rgb = bytearray(len(bgra) // 4 * 3)
    rgb[0::3] = bgra[2::4]
    rgb[1::3] = bgra[1::4]
    rgb[2::3] = bgra[0::4]
    return bytes(rgb)


def encode_png(w: int, h: int, rgb: bytes) -> bytes:
    stride = w * 3
    raw = b"".join(b"\x00" + rgb[y * stride:(y + 1) * stride] for y in range(h))

    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)

    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(raw, 3))
        + chunk(b"IEND", b"")
    )


def capture(max_w: int = 1280, quality: int = 60) -> tuple[bytes, str]:
    """Return (image bytes, mime type)."""
    w, h, bgra = capture_bgra(max_w)
    try:
        from PIL import Image  # optional, much smaller frames

        img = Image.frombuffer("RGB", (w, h), bgra, "raw", "BGRX", 0, 1)
        out = io.BytesIO()
        img.save(out, "JPEG", quality=max(20, min(int(quality), 95)))
        return out.getvalue(), "image/jpeg"
    except ImportError:
        return encode_png(w, h, _bgra_to_rgb(bgra)), "image/png"


GRID_W, GRID_H = 48, 27


def snapshot(max_w: int = 1280) -> dict:
    """One screenshot for the AI (base64 + size) and a small brightness grid to tell whether the screen changed."""
    w, h, bgra = capture_bgra(max_w)
    grid = []
    for gy in range(GRID_H):
        y = min(h - 1, int((gy + 0.5) * h / GRID_H))
        for gx in range(GRID_W):
            x = min(w - 1, int((gx + 0.5) * w / GRID_W))
            i = (y * w + x) * 4
            grid.append((bgra[i] + bgra[i + 1] + bgra[i + 2]) // 48)  # 0-15: ignores tiny colour noise
    try:
        from PIL import Image

        img = Image.frombuffer("RGB", (w, h), bgra, "raw", "BGRX", 0, 1)
        out = io.BytesIO()
        img.save(out, "JPEG", quality=70)
        data, mime = out.getvalue(), "image/jpeg"
    except ImportError:
        data, mime = encode_png(w, h, _bgra_to_rgb(bgra)), "image/png"
    return {"b64": base64.b64encode(data).decode("ascii"), "mime": mime, "w": w, "h": h, "grid": grid}


def changed(a: list[int] | None, b: list[int] | None, cells: int = 3) -> bool:
    """True when at least ``cells`` grid cells differ (a blinking text cursor alone does not count)."""
    if a is None or b is None or len(a) != len(b):
        return True
    return sum(1 for x, y in zip(a, b) if x != y) >= cells


def capture_b64(max_w: int = 1024) -> tuple[str, str]:
    data, mime = capture(max_w, 70)
    return base64.b64encode(data).decode("ascii"), mime
