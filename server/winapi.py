"""Thin ctypes layer over the Win32 APIs the remote needs.

Keyboard / mouse injection (SendInput), clipboard, window enumeration and focus,
screen metrics. On non-Windows systems every call is recorded in ``SIM_LOG``
instead of touching the OS, so the rest of the server can be exercised in tests.
"""

from __future__ import annotations

import ctypes
import sys
import threading
import time
from typing import Iterable

IS_WIN = sys.platform == "win32"
SIM_LOG: list[tuple] = []  # records calls when not on Windows

if IS_WIN:
    from ctypes import wintypes

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

    ULONG_PTR = ctypes.c_size_t

    class MOUSEINPUT(ctypes.Structure):
        _fields_ = [
            ("dx", wintypes.LONG),
            ("dy", wintypes.LONG),
            ("mouseData", wintypes.DWORD),
            ("dwFlags", wintypes.DWORD),
            ("time", wintypes.DWORD),
            ("dwExtraInfo", ULONG_PTR),
        ]

    class KEYBDINPUT(ctypes.Structure):
        _fields_ = [
            ("wVk", wintypes.WORD),
            ("wScan", wintypes.WORD),
            ("dwFlags", wintypes.DWORD),
            ("time", wintypes.DWORD),
            ("dwExtraInfo", ULONG_PTR),
        ]

    class HARDWAREINPUT(ctypes.Structure):
        _fields_ = [("uMsg", wintypes.DWORD), ("wParamL", wintypes.WORD), ("wParamH", wintypes.WORD)]

    class _INPUTUNION(ctypes.Union):
        _fields_ = [("mi", MOUSEINPUT), ("ki", KEYBDINPUT), ("hi", HARDWAREINPUT)]

    class INPUT(ctypes.Structure):
        _fields_ = [("type", wintypes.DWORD), ("u", _INPUTUNION)]

    WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    def _sig(dll, name, restype, *argtypes):
        fn = getattr(dll, name)
        fn.restype = restype
        fn.argtypes = list(argtypes)
        return fn

    _SendInput = _sig(user32, "SendInput", wintypes.UINT, wintypes.UINT, ctypes.POINTER(INPUT), ctypes.c_int)
    _MapVirtualKeyW = _sig(user32, "MapVirtualKeyW", wintypes.UINT, wintypes.UINT, wintypes.UINT)
    _SetCursorPos = _sig(user32, "SetCursorPos", wintypes.BOOL, ctypes.c_int, ctypes.c_int)
    _GetCursorPos = _sig(user32, "GetCursorPos", wintypes.BOOL, ctypes.POINTER(wintypes.POINT))
    _GetSystemMetrics = _sig(user32, "GetSystemMetrics", ctypes.c_int, ctypes.c_int)
    _GetForegroundWindow = _sig(user32, "GetForegroundWindow", wintypes.HWND)
    _SetForegroundWindow = _sig(user32, "SetForegroundWindow", wintypes.BOOL, wintypes.HWND)
    _BringWindowToTop = _sig(user32, "BringWindowToTop", wintypes.BOOL, wintypes.HWND)
    _ShowWindow = _sig(user32, "ShowWindow", wintypes.BOOL, wintypes.HWND, ctypes.c_int)
    _IsIconic = _sig(user32, "IsIconic", wintypes.BOOL, wintypes.HWND)
    _IsWindowVisible = _sig(user32, "IsWindowVisible", wintypes.BOOL, wintypes.HWND)
    _GetWindowTextLengthW = _sig(user32, "GetWindowTextLengthW", ctypes.c_int, wintypes.HWND)
    _GetWindowTextW = _sig(user32, "GetWindowTextW", ctypes.c_int, wintypes.HWND, wintypes.LPWSTR, ctypes.c_int)
    _EnumWindows = _sig(user32, "EnumWindows", wintypes.BOOL, WNDENUMPROC, wintypes.LPARAM)
    _GetWindowThreadProcessId = _sig(
        user32, "GetWindowThreadProcessId", wintypes.DWORD, wintypes.HWND, ctypes.POINTER(wintypes.DWORD)
    )
    _AttachThreadInput = _sig(user32, "AttachThreadInput", wintypes.BOOL, wintypes.DWORD, wintypes.DWORD, wintypes.BOOL)
    _GetCurrentThreadId = _sig(kernel32, "GetCurrentThreadId", wintypes.DWORD)
    _SwitchToThisWindow = _sig(user32, "SwitchToThisWindow", None, wintypes.HWND, wintypes.BOOL)
    _WindowFromPoint = _sig(user32, "WindowFromPoint", wintypes.HWND, wintypes.POINT)
    _GetAncestor = _sig(user32, "GetAncestor", wintypes.HWND, wintypes.HWND, wintypes.UINT)

    class GUITHREADINFO(ctypes.Structure):
        _fields_ = [
            ("cbSize", wintypes.DWORD),
            ("flags", wintypes.DWORD),
            ("hwndActive", wintypes.HWND),
            ("hwndFocus", wintypes.HWND),
            ("hwndCapture", wintypes.HWND),
            ("hwndMenuOwner", wintypes.HWND),
            ("hwndMoveSize", wintypes.HWND),
            ("hwndCaret", wintypes.HWND),
            ("rcCaret", wintypes.RECT),
        ]

    class _CURSORINFO(ctypes.Structure):
        _fields_ = [("cbSize", wintypes.DWORD), ("flags", wintypes.DWORD),
                    ("hCursor", wintypes.HANDLE), ("ptScreenPos", wintypes.POINT)]

    _GetGUIThreadInfo = _sig(user32, "GetGUIThreadInfo", wintypes.BOOL, wintypes.DWORD, ctypes.POINTER(GUITHREADINFO))
    _GetClassNameW = _sig(user32, "GetClassNameW", ctypes.c_int, wintypes.HWND, wintypes.LPWSTR, ctypes.c_int)
    _GetCursorInfo = _sig(user32, "GetCursorInfo", wintypes.BOOL, ctypes.POINTER(_CURSORINFO))
    _LoadCursorW = _sig(user32, "LoadCursorW", wintypes.HANDLE, wintypes.HINSTANCE, ctypes.c_void_p)

    _OpenClipboard = _sig(user32, "OpenClipboard", wintypes.BOOL, wintypes.HWND)
    _CloseClipboard = _sig(user32, "CloseClipboard", wintypes.BOOL)
    _EmptyClipboard = _sig(user32, "EmptyClipboard", wintypes.BOOL)
    _GetClipboardData = _sig(user32, "GetClipboardData", wintypes.HANDLE, wintypes.UINT)
    _SetClipboardData = _sig(user32, "SetClipboardData", wintypes.HANDLE, wintypes.UINT, wintypes.HANDLE)
    _GlobalAlloc = _sig(kernel32, "GlobalAlloc", wintypes.HGLOBAL, wintypes.UINT, ctypes.c_size_t)
    _GlobalLock = _sig(kernel32, "GlobalLock", wintypes.LPVOID, wintypes.HGLOBAL)
    _GlobalUnlock = _sig(kernel32, "GlobalUnlock", wintypes.BOOL, wintypes.HGLOBAL)
    _GlobalFree = _sig(kernel32, "GlobalFree", wintypes.HGLOBAL, wintypes.HGLOBAL)

INPUT_MOUSE, INPUT_KEYBOARD = 0, 1
KEYEVENTF_EXTENDEDKEY = 0x0001
KEYEVENTF_KEYUP = 0x0002
KEYEVENTF_UNICODE = 0x0004
KEYEVENTF_SCANCODE = 0x0008
MOUSE_FLAGS = {
    "left_down": 0x0002,
    "left_up": 0x0004,
    "right_down": 0x0008,
    "right_up": 0x0010,
    "middle_down": 0x0020,
    "middle_up": 0x0040,
}
MOUSEEVENTF_WHEEL = 0x0800
MOUSEEVENTF_HWHEEL = 0x1000
CF_UNICODETEXT = 13
GMEM_MOVEABLE = 0x0002

# ---------------------------------------------------------------------------
# Key names
# ---------------------------------------------------------------------------

VK: dict[str, int] = {
    "backspace": 0x08, "tab": 0x09, "enter": 0x0D, "shift": 0x10, "ctrl": 0x11, "alt": 0x12,
    "pause": 0x13, "capslock": 0x14, "esc": 0x1B, "space": 0x20, "pageup": 0x21, "pagedown": 0x22,
    "end": 0x23, "home": 0x24, "left": 0x25, "up": 0x26, "right": 0x27, "down": 0x28,
    "printscreen": 0x2C, "insert": 0x2D, "delete": 0x2E, "win": 0x5B, "apps": 0x5D,
    "volume_mute": 0xAD, "volume_down": 0xAE, "volume_up": 0xAF,
    "media_next": 0xB0, "media_previous": 0xB1, "media_stop": 0xB2, "media_play_pause": 0xB3,
    ";": 0xBA, "=": 0xBB, ",": 0xBC, "-": 0xBD, ".": 0xBE, "/": 0xBF, "`": 0xC0,
    "[": 0xDB, "\\": 0xDC, "]": 0xDD, "'": 0xDE,
}
VK.update({f"f{i}": 0x6F + i for i in range(1, 25)})
VK.update({c: ord(c.upper()) for c in "abcdefghijklmnopqrstuvwxyz0123456789"})

ALIASES = {
    "return": "enter", "escape": "esc", "control": "ctrl", "ctl": "ctrl", "menu": "alt",
    "windows": "win", "super": "win", "meta": "win", "cmd": "win", "del": "delete",
    "bksp": "backspace", "back": "backspace", "pgup": "pageup", "pgdn": "pagedown",
    "page_up": "pageup", "page_down": "pagedown", "arrowup": "up", "arrowdown": "down",
    "arrowleft": "left", "arrowright": "right", "spacebar": "space", "ins": "insert",
    "caps": "capslock", "prtsc": "printscreen", "mute": "volume_mute",
    "play_pause": "media_play_pause", "next": "media_next", "previous": "media_previous",
    "prev": "media_previous", "stop": "media_stop",
}

EXTENDED = {"insert", "delete", "home", "end", "pageup", "pagedown", "left", "up", "right", "down", "win", "apps"}
MODIFIERS = {"ctrl", "alt", "shift", "win"}


def key_name(name: str) -> str:
    n = str(name).strip().lower().replace(" ", "")
    return ALIASES.get(n, n)


def vk_of(name: str) -> int:
    n = key_name(name)
    if n in VK:
        return VK[n]
    raise ValueError(f"unknown key: {name}")


# ---------------------------------------------------------------------------
# Low level injection
# ---------------------------------------------------------------------------

_input_lock = threading.Lock()


def _send(inputs: list) -> int:
    if not inputs:
        return 0
    arr = (INPUT * len(inputs))(*inputs)
    with _input_lock:
        return int(_SendInput(len(inputs), arr, ctypes.sizeof(INPUT)))


def _kbd(vk: int = 0, scan: int = 0, flags: int = 0):
    return INPUT(type=INPUT_KEYBOARD, u=_INPUTUNION(ki=KEYBDINPUT(wVk=vk, wScan=scan, dwFlags=flags, time=0, dwExtraInfo=0)))


def _key_input(name: str, up: bool, scancode: bool = False):
    n = key_name(name)
    vk = vk_of(n)
    flags = KEYEVENTF_KEYUP if up else 0
    if n in EXTENDED:
        flags |= KEYEVENTF_EXTENDEDKEY
    scan = _MapVirtualKeyW(vk, 0) & 0xFF
    if scancode and scan:
        return _kbd(0, scan, flags | KEYEVENTF_SCANCODE)
    return _kbd(vk, scan, flags)


def key_down(name: str, scancode: bool = False) -> None:
    if not IS_WIN:
        SIM_LOG.append(("key_down", key_name(name)))
        vk_of(name)
        return
    _send([_key_input(name, False, scancode)])


def key_up(name: str, scancode: bool = False) -> None:
    if not IS_WIN:
        SIM_LOG.append(("key_up", key_name(name)))
        vk_of(name)
        return
    _send([_key_input(name, True, scancode)])


def tap(name: str, scancode: bool = False, hold: float = 0.015) -> None:
    key_down(name, scancode)
    time.sleep(hold)
    key_up(name, scancode)


def hotkey(keys: Iterable[str]) -> None:
    """Press a chord like ["ctrl", "shift", "n"]: hold all but the last, tap the last."""
    names = [key_name(k) for k in keys if str(k).strip()]
    if not names:
        raise ValueError("no keys")
    for n in names:
        vk_of(n)  # validate before pressing anything
    held = names[:-1]
    for n in held:
        key_down(n)
    try:
        tap(names[-1])
    finally:
        for n in reversed(held):
            key_up(n)


def type_text(text: str) -> None:
    """Type arbitrary Unicode (Arabic included) into the focused window."""
    if not IS_WIN:
        SIM_LOG.append(("type", text))
        return
    batch: list = []
    for ch in text:
        if ch == "\n":
            batch.append(_kbd(VK["enter"], 0, 0))
            batch.append(_kbd(VK["enter"], 0, KEYEVENTF_KEYUP))
            continue
        if ch == "\r":
            continue
        data = ch.encode("utf-16-le")
        for i in range(0, len(data), 2):
            unit = int.from_bytes(data[i:i + 2], "little")
            batch.append(_kbd(0, unit, KEYEVENTF_UNICODE))
            batch.append(_kbd(0, unit, KEYEVENTF_UNICODE | KEYEVENTF_KEYUP))
        if len(batch) >= 200:
            _send(batch)
            batch = []
            time.sleep(0.005)
    _send(batch)


# ---------------------------------------------------------------------------
# Mouse
# ---------------------------------------------------------------------------


def set_dpi_aware() -> None:
    """Make coordinates physical pixels so screenshots, clicks and metrics agree."""
    if not IS_WIN:
        return
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
        return
    except Exception:
        pass
    try:
        user32.SetProcessDPIAware()
    except Exception:
        pass


def screen_size() -> tuple[int, int]:
    if not IS_WIN:
        return 1920, 1080
    return int(_GetSystemMetrics(0)), int(_GetSystemMetrics(1))


def get_cursor() -> tuple[int, int]:
    if not IS_WIN:
        return 960, 540
    pt = wintypes.POINT()
    _GetCursorPos(ctypes.byref(pt))
    return int(pt.x), int(pt.y)


def set_cursor(x: int, y: int) -> None:
    if not IS_WIN:
        SIM_LOG.append(("cursor", int(x), int(y)))
        return
    _SetCursorPos(int(x), int(y))


def move_rel(dx: int, dy: int) -> tuple[int, int]:
    x, y = get_cursor()
    w, h = screen_size()
    nx = min(max(x + int(dx), 0), w - 1)
    ny = min(max(y + int(dy), 0), h - 1)
    set_cursor(nx, ny)
    return nx, ny


def mouse_button(kind: str) -> None:
    flags = MOUSE_FLAGS[kind]
    if not IS_WIN:
        SIM_LOG.append(("mouse", kind))
        return
    inp = INPUT(type=INPUT_MOUSE, u=_INPUTUNION(mi=MOUSEINPUT(0, 0, 0, flags, 0, 0)))
    if _send([inp]) == 0:
        user32.mouse_event(flags, 0, 0, 0, 0)


def click(button: str = "left", count: int = 1) -> None:
    b = "right" if button == "right" else "middle" if button == "middle" else "left"
    for i in range(max(1, count)):
        mouse_button(f"{b}_down")
        time.sleep(0.03)
        mouse_button(f"{b}_up")
        if i + 1 < count:
            time.sleep(0.06)


def scroll(delta: int, horizontal: bool = False) -> None:
    if not IS_WIN:
        SIM_LOG.append(("scroll", int(delta), horizontal))
        return
    flag = MOUSEEVENTF_HWHEEL if horizontal else MOUSEEVENTF_WHEEL
    inp = INPUT(type=INPUT_MOUSE, u=_INPUTUNION(mi=MOUSEINPUT(0, 0, int(delta) & 0xFFFFFFFF, flag, 0, 0)))
    _send([inp])


# ---------------------------------------------------------------------------
# Clipboard
# ---------------------------------------------------------------------------

_sim_clipboard = [""]


def _open_clipboard() -> None:
    for _ in range(20):
        if _OpenClipboard(None):
            return
        time.sleep(0.03)
    raise OSError("clipboard is busy")


def set_clipboard(text: str) -> None:
    if not IS_WIN:
        _sim_clipboard[0] = text
        SIM_LOG.append(("clipboard_set", text))
        return
    data = text.encode("utf-16-le") + b"\x00\x00"
    _open_clipboard()
    try:
        _EmptyClipboard()
        h = _GlobalAlloc(GMEM_MOVEABLE, len(data))
        if not h:
            raise OSError("GlobalAlloc failed")
        p = _GlobalLock(h)
        ctypes.memmove(p, data, len(data))
        _GlobalUnlock(h)
        if not _SetClipboardData(CF_UNICODETEXT, h):
            _GlobalFree(h)
            raise OSError("SetClipboardData failed")
    finally:
        _CloseClipboard()


def get_clipboard() -> str:
    if not IS_WIN:
        return _sim_clipboard[0]
    _open_clipboard()
    try:
        h = _GetClipboardData(CF_UNICODETEXT)
        if not h:
            return ""
        p = _GlobalLock(h)
        if not p:
            return ""
        try:
            return ctypes.wstring_at(p)
        finally:
            _GlobalUnlock(h)
    finally:
        _CloseClipboard()


# ---------------------------------------------------------------------------
# Windows
# ---------------------------------------------------------------------------


def list_windows() -> list[tuple[int, str]]:
    if not IS_WIN:
        return []
    out: list[tuple[int, str]] = []

    def cb(hwnd, _lparam):
        if _IsWindowVisible(hwnd):
            n = _GetWindowTextLengthW(hwnd)
            if n > 0:
                buf = ctypes.create_unicode_buffer(n + 1)
                _GetWindowTextW(hwnd, buf, n + 1)
                if buf.value.strip():
                    out.append((int(hwnd), buf.value))
        return True

    _EnumWindows(WNDENUMPROC(cb), 0)
    return out


def foreground_title() -> str:
    if not IS_WIN:
        return ""
    hwnd = _GetForegroundWindow()
    if not hwnd:
        return ""
    n = _GetWindowTextLengthW(hwnd)
    buf = ctypes.create_unicode_buffer(n + 1)
    _GetWindowTextW(hwnd, buf, n + 1)
    return buf.value


def find_window(match: str) -> int | None:
    """Return the first visible window whose title contains ``match`` (case-insensitive)."""
    m = match.lower()
    for hwnd, title in list_windows():
        if m in title.lower():
            return hwnd
    return None


def focus_window(hwnd: int) -> bool:
    if not IS_WIN:
        SIM_LOG.append(("focus", hwnd))
        return True
    if _IsIconic(hwnd):
        _ShowWindow(hwnd, 9)  # SW_RESTORE
    fg = _GetForegroundWindow()
    if fg == hwnd:
        return True
    cur = _GetCurrentThreadId()
    other = _GetWindowThreadProcessId(fg, None) if fg else 0
    attached = bool(other and other != cur and _AttachThreadInput(cur, other, True))
    try:
        _BringWindowToTop(hwnd)
        _SetForegroundWindow(hwnd)
    finally:
        if attached:
            _AttachThreadInput(cur, other, False)
    time.sleep(0.05)
    if _GetForegroundWindow() == hwnd:
        return True
    _SwitchToThisWindow(hwnd, True)
    time.sleep(0.1)
    return _GetForegroundWindow() == hwnd


def activate_at(x: int, y: int) -> None:
    """Bring the top-level window under a screen point to the foreground."""
    if not IS_WIN:
        return
    try:
        hwnd = _WindowFromPoint(wintypes.POINT(int(x), int(y)))
        if hwnd:
            root = _GetAncestor(hwnd, 2)  # GA_ROOT
            if root and root != _GetForegroundWindow():
                _SetForegroundWindow(root)
    except Exception:
        pass


EDIT_CLASSES = ("edit", "richedit", "scintilla", "textbox", "combobox")


def text_input_focused() -> bool:
    """Best guess whether the click landed in something you can type into.

    A blinking system caret (Win32 edits, Chrome/Electron/VS Code via their accessibility caret),
    an edit-like focused control, or the I-beam mouse cursor all count.
    """
    if not IS_WIN:
        return False
    try:
        info = GUITHREADINFO()
        info.cbSize = ctypes.sizeof(GUITHREADINFO)
        if _GetGUIThreadInfo(0, ctypes.byref(info)):
            if info.hwndCaret:
                return True
            if info.hwndFocus:
                buf = ctypes.create_unicode_buffer(128)
                _GetClassNameW(info.hwndFocus, buf, 128)
                if any(c in buf.value.lower() for c in EDIT_CLASSES):
                    return True
        ci = _CURSORINFO()
        ci.cbSize = ctypes.sizeof(_CURSORINFO)
        if _GetCursorInfo(ctypes.byref(ci)) and ci.hCursor:
            return ci.hCursor == _LoadCursorW(None, ctypes.c_void_p(32513))  # IDC_IBEAM
    except Exception:
        pass
    return False
