#!/usr/bin/env python3
"""Enumerate top-level windows, including minimized / cloaked browsers.

Chrome is often missed because it is minimized (GetWindowRect is off-screen),
cloaked on another virtual desktop, or uses class Chrome_WidgetWin_1.
Geometry always comes from WINDOWPLACEMENT.rcNormalPosition so a minimized
window still has a real size. Capture restores the HWND long enough to grab
that window's pixels (not a desktop crop).
"""

from __future__ import annotations

import os
import sys
import time
from ctypes import wintypes
from dataclasses import dataclass, field

import ctypes

if sys.platform != "win32":
    raise RuntimeError("Window enumeration requires Windows")

user32 = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32
dwmapi = ctypes.windll.dwmapi

PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
DWMWA_CLOAKED = 14
DWMWA_EXTENDED_FRAME_BOUNDS = 9
GWL_EXSTYLE = -20
WS_EX_TOOLWINDOW = 0x00000080
WS_EX_APPWINDOW = 0x00040000
WS_EX_NOACTIVATE = 0x08000000
GW_OWNER = 4
GA_ROOT = 2
SW_SHOWNORMAL = 1
SW_SHOWMINIMIZED = 2
SW_SHOWNOACTIVATE = 4
SW_RESTORE = 9
HWND_NOTOPMOST = -2
SWP_NOMOVE = 0x0002
SWP_NOSIZE = 0x0001
SWP_NOACTIVATE = 0x0010
SWP_SHOWWINDOW = 0x0040

WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

BROWSERS = {
    "chrome.exe",
    "msedge.exe",
    "firefox.exe",
    "brave.exe",
    "opera.exe",
    "vivaldi.exe",
    "chromium.exe",
}

SKIP_CLASSES = {
    "Progman",
    "WorkerW",
    "Shell_TrayWnd",
    "Shell_SecondaryTrayWnd",
    "NotifyIconOverflowWindow",
    "ForegroundStaging",
    "MultitaskingViewFrame",
    "EdgeUiInputTopWndClass",
    "DummyDWMListenerWindow",
    "tooltips_class32",
    "IME",
    "MSCTFIME UI",
    "OleMainThreadWndClass",
    "PseudoConsoleWindow",
}

kernel32.QueryFullProcessImageNameW.argtypes = [
    wintypes.HANDLE,
    wintypes.DWORD,
    wintypes.LPWSTR,
    ctypes.POINTER(wintypes.DWORD),
]
kernel32.QueryFullProcessImageNameW.restype = wintypes.BOOL

if ctypes.sizeof(ctypes.c_void_p) == 8:
    _get_long_ptr = user32.GetWindowLongPtrW
    _get_long_ptr.restype = ctypes.c_ssize_t
else:
    _get_long_ptr = user32.GetWindowLongW
    _get_long_ptr.restype = ctypes.c_long


class RECT(ctypes.Structure):
    _fields_ = [
        ("left", wintypes.LONG),
        ("top", wintypes.LONG),
        ("right", wintypes.LONG),
        ("bottom", wintypes.LONG),
    ]


class POINT(ctypes.Structure):
    _fields_ = [("x", wintypes.LONG), ("y", wintypes.LONG)]


class WINDOWPLACEMENT(ctypes.Structure):
    _fields_ = [
        ("length", wintypes.UINT),
        ("flags", wintypes.UINT),
        ("showCmd", wintypes.UINT),
        ("ptMinPosition", POINT),
        ("ptMaxPosition", POINT),
        ("rcNormalPosition", RECT),
    ]


@dataclass
class WindowInfo:
    hwnd: int
    title: str
    pid: int
    exe: str
    x: int
    y: int
    width: int
    height: int
    class_name: str = ""
    minimized: bool = False
    cloaked: bool = False
    is_desktop: bool = False

    @property
    def even_size(self) -> tuple[int, int]:
        w = max(2, self.width - (self.width % 2))
        h = max(2, self.height - (self.height % 2))
        return w, h

    def state_text(self) -> str:
        if self.is_desktop:
            return ""
        parts = []
        if self.minimized:
            parts.append("minimized")
        if self.cloaked:
            parts.append("cloaked")
        return ", ".join(parts) or "open"

    def search_blob(self) -> str:
        return " ".join(
            [
                self.title,
                self.exe,
                self.class_name,
                self.state_text(),
                f"hwnd:{self.hwnd}",
            ]
        ).lower()

    def label(self) -> str:
        if self.is_desktop:
            return "Entire screen"
        proc = self.exe or f"pid {self.pid}"
        geo = f"{self.width}x{self.height}"
        title = self.title.replace("\n", " ").strip() or f"({proc})"
        if len(title) > 80:
            title = title[:77] + "..."
        extra = self.state_text()
        if extra and extra != "open":
            return f"{title}  [{extra}]  -  {proc}  ({geo})"
        return f"{title}  -  {proc}  ({geo})"


@dataclass
class WindowRestore:
    hwnd: int
    was_minimized: bool
    _placement: WINDOWPLACEMENT | None = field(default=None, repr=False)

    def revert(self) -> None:
        if not self.was_minimized or self._placement is None:
            return
        if not user32.IsWindow(self.hwnd):
            return
        user32.SetWindowPlacement(self.hwnd, ctypes.byref(self._placement))


def _window_title(hwnd: int) -> str:
    n = user32.GetWindowTextLengthW(hwnd)
    if n <= 0:
        return ""
    buf = ctypes.create_unicode_buffer(n + 1)
    user32.GetWindowTextW(hwnd, buf, n + 1)
    return buf.value.strip()


def _class_name(hwnd: int) -> str:
    buf = ctypes.create_unicode_buffer(256)
    n = user32.GetClassNameW(hwnd, buf, 256)
    return buf.value if n else ""


def _is_cloaked(hwnd: int) -> bool:
    cloaked = wintypes.DWORD(0)
    try:
        hr = dwmapi.DwmGetWindowAttribute(
            wintypes.HWND(hwnd),
            DWMWA_CLOAKED,
            ctypes.byref(cloaked),
            ctypes.sizeof(cloaked),
        )
        return hr == 0 and cloaked.value != 0
    except OSError:
        return False


def _process_image(pid: int) -> str:
    handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return ""
    try:
        size = wintypes.DWORD(32768)
        buf = ctypes.create_unicode_buffer(size.value)
        if kernel32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size)):
            return buf.value.rsplit("\\", 1)[-1]
        return ""
    finally:
        kernel32.CloseHandle(handle)


def _placement(hwnd: int) -> WINDOWPLACEMENT | None:
    wp = WINDOWPLACEMENT()
    wp.length = ctypes.sizeof(WINDOWPLACEMENT)
    if not user32.GetWindowPlacement(hwnd, ctypes.byref(wp)):
        return None
    return wp


def _normal_rect(hwnd: int) -> tuple[int, int, int, int]:
    """Visible restore rect. GetWindowRect is useless while iconic (-32000, -32000)."""
    wp = _placement(hwnd)
    if wp is not None:
        rc = wp.rcNormalPosition
        w = max(0, rc.right - rc.left)
        h = max(0, rc.bottom - rc.top)
        if w >= 16 and h >= 16:
            return rc.left, rc.top, w, h
    rc = RECT()
    if user32.GetWindowRect(hwnd, ctypes.byref(rc)):
        w = max(0, rc.right - rc.left)
        h = max(0, rc.bottom - rc.top)
        if w >= 16 and h >= 16 and rc.left > -10000:
            return rc.left, rc.top, w, h
    ext = RECT()
    hr = dwmapi.DwmGetWindowAttribute(
        wintypes.HWND(hwnd),
        DWMWA_EXTENDED_FRAME_BOUNDS,
        ctypes.byref(ext),
        ctypes.sizeof(ext),
    )
    if hr == 0:
        w = max(0, ext.right - ext.left)
        h = max(0, ext.bottom - ext.top)
        return ext.left, ext.top, w, h
    return 0, 0, 0, 0


def current_frame_rect(hwnd: int) -> tuple[int, int, int, int]:
    """GetWindowRect size - this is what PrintWindow paints into."""
    rc = RECT()
    if user32.GetWindowRect(hwnd, ctypes.byref(rc)):
        w = max(0, rc.right - rc.left)
        h = max(0, rc.bottom - rc.top)
        if w >= 2 and h >= 2 and rc.left > -10000:
            return rc.left, rc.top, w, h
    ext = RECT()
    hr = dwmapi.DwmGetWindowAttribute(
        wintypes.HWND(hwnd),
        DWMWA_EXTENDED_FRAME_BOUNDS,
        ctypes.byref(ext),
        ctypes.sizeof(ext),
    )
    if hr == 0:
        w = max(0, ext.right - ext.left)
        h = max(0, ext.bottom - ext.top)
        if w >= 2 and h >= 2:
            return ext.left, ext.top, w, h
    return 0, 0, 0, 0


def _virtual_screen() -> WindowInfo:
    x = user32.GetSystemMetrics(76)
    y = user32.GetSystemMetrics(77)
    w = user32.GetSystemMetrics(78)
    h = user32.GetSystemMetrics(79)
    if w <= 0 or h <= 0:
        w = user32.GetSystemMetrics(0)
        h = user32.GetSystemMetrics(1)
        x, y = 0, 0
    return WindowInfo(
        hwnd=0,
        title="desktop",
        pid=0,
        exe="",
        x=x,
        y=y,
        width=w,
        height=h,
        class_name="",
        is_desktop=True,
    )


def _exstyle(hwnd: int) -> int:
    try:
        return int(_get_long_ptr(hwnd, GWL_EXSTYLE)) & 0xFFFFFFFF
    except OSError:
        return 0


def _is_self(hwnd: int, pid: int, title: str, class_name: str) -> bool:
    if pid == os.getpid() and class_name.startswith("Tk"):
        return True
    if title.startswith("Window Recorder"):
        return True
    return False


def _is_candidate(hwnd: int) -> bool:
    if not user32.IsWindow(hwnd):
        return False
    class_name = _class_name(hwnd)
    if class_name in SKIP_CLASSES:
        return False

    pid_dw = wintypes.DWORD(0)
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid_dw))
    pid = int(pid_dw.value)
    exe = _process_image(pid).lower()
    title = _window_title(hwnd)
    if _is_self(hwnd, pid, title, class_name):
        return False

    is_browser = exe in BROWSERS or class_name.startswith("Chrome_WidgetWin")
    minimized = bool(user32.IsIconic(hwnd))
    visible = bool(user32.IsWindowVisible(hwnd))
    # Minimized windows keep WS_VISIBLE; still include them if iconic.
    if not visible and not minimized and not is_browser:
        return False

    owner = user32.GetWindow(hwnd, GW_OWNER)
    ex = _exstyle(hwnd)
    tool = bool(ex & WS_EX_TOOLWINDOW) and not bool(ex & WS_EX_APPWINDOW)
    # Skip tiny owner-only tool popups, but never skip browser frames.
    if tool and not is_browser:
        return False
    if owner and not is_browser and not (ex & WS_EX_APPWINDOW):
        if not title:
            return False

    x, y, w, h = _normal_rect(hwnd)
    if is_browser:
        if w < 16 or h < 16:
            return False
        return True

    if not title:
        return False
    if w < 32 or h < 32:
        return False
    return True


def list_windows() -> list[WindowInfo]:
    found: list[WindowInfo] = []
    seen: set[int] = set()

    def _add(hwnd: int) -> None:
        hwnd = int(hwnd)
        if hwnd in seen or not _is_candidate(hwnd):
            return
        seen.add(hwnd)
        pid_dw = wintypes.DWORD(0)
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid_dw))
        pid = int(pid_dw.value)
        exe = _process_image(pid)
        title = _window_title(hwnd)
        if not title:
            title = f"{exe or 'window'} hwnd={hwnd}"
        x, y, w, h = _normal_rect(hwnd)
        found.append(
            WindowInfo(
                hwnd=hwnd,
                title=title,
                pid=pid,
                exe=exe,
                x=x,
                y=y,
                width=w,
                height=h,
                class_name=_class_name(hwnd),
                minimized=bool(user32.IsIconic(hwnd)),
                cloaked=_is_cloaked(hwnd),
            )
        )

    @WNDENUMPROC
    def _cb(hwnd: int, _lparam: int) -> bool:
        _add(hwnd)
        return True

    user32.EnumWindows(_cb, 0)

    # Alt-Tab style walk (GetWindow GW_HWNDNEXT) sometimes sees windows
    # EnumWindows skips when a shell hook is late.
    try:
        hwnd = user32.GetTopWindow(0)
        while hwnd:
            _add(hwnd)
            hwnd = user32.GetWindow(hwnd, 2)  # GW_HWNDNEXT
    except OSError:
        pass

    def _sort_key(w: WindowInfo) -> tuple:
        browser = 0 if w.exe.lower() in BROWSERS else 1
        min_key = 1 if w.minimized else 0
        return (min_key, browser, w.exe.lower(), w.title.lower())

    found.sort(key=_sort_key)
    return [_virtual_screen()] + found


def refresh_geometry(info: WindowInfo) -> WindowInfo:
    if info.is_desktop:
        return _virtual_screen()
    if not user32.IsWindow(info.hwnd):
        return info
    x, y, w, h = _normal_rect(info.hwnd)
    if not user32.IsIconic(info.hwnd):
        x, y, w, h = current_frame_rect(info.hwnd)
    return WindowInfo(
        hwnd=info.hwnd,
        title=_window_title(info.hwnd) or info.title,
        pid=info.pid,
        exe=info.exe,
        x=x,
        y=y,
        width=w,
        height=h,
        class_name=_class_name(info.hwnd) or info.class_name,
        minimized=bool(user32.IsIconic(info.hwnd)),
        cloaked=_is_cloaked(info.hwnd),
        is_desktop=False,
    )


def prepare_for_capture(info: WindowInfo) -> WindowRestore:
    """Restore a minimized/hidden HWND so DWM has pixels for that window.

    The window is shown without forcing it topmost. Caller must revert()
    after recording to put a previously minimized window back.
    """
    token = WindowRestore(hwnd=info.hwnd, was_minimized=False)
    if info.is_desktop or not info.hwnd:
        return token
    hwnd = info.hwnd
    if not user32.IsWindow(hwnd):
        raise RuntimeError("That window no longer exists.")
    wp = _placement(hwnd)
    token._placement = wp
    token.was_minimized = bool(user32.IsIconic(hwnd)) or (
        wp is not None and wp.showCmd == SW_SHOWMINIMIZED
    )
    if token.was_minimized:
        user32.ShowWindow(hwnd, SW_RESTORE)
        user32.SetWindowPos(
            hwnd,
            HWND_NOTOPMOST,
            0,
            0,
            0,
            0,
            SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE | SWP_SHOWWINDOW,
        )
        # DWM needs a beat after restore before PrintWindow has content.
        time.sleep(0.25)
        user32.RedrawWindow(hwnd, None, None, 0x0103)  # RDW_INVALIDATE|ERASE|UPDATENOW
        time.sleep(0.05)
    return token
