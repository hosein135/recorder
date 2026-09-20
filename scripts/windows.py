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
dwmapi.DwmSetWindowAttribute.argtypes = [
    wintypes.HWND,
    ctypes.c_uint,
    ctypes.c_void_p,
    wintypes.DWORD,
]
dwmapi.DwmSetWindowAttribute.restype = ctypes.c_long

PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
DWMWA_CLOAK = 13
DWMWA_CLOAKED = 14
DWMWA_EXTENDED_FRAME_BOUNDS = 9
HWND_BOTTOM = 1
GWL_EXSTYLE = -20
WS_EX_TOOLWINDOW = 0x00000080
WS_EX_APPWINDOW = 0x00040000
WS_EX_NOACTIVATE = 0x08000000
GW_OWNER = 4
GA_ROOT = 2
GA_ROOTOWNER = 3
GW_HWNDNEXT = 2
GW_CHILD = 5
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


user32.WindowFromPoint.argtypes = [POINT]
user32.WindowFromPoint.restype = wintypes.HWND
user32.GetAncestor.argtypes = [wintypes.HWND, ctypes.c_uint]
user32.GetAncestor.restype = wintypes.HWND
user32.GetLastActivePopup.argtypes = [wintypes.HWND]
user32.GetLastActivePopup.restype = wintypes.HWND
user32.GetDesktopWindow.restype = wintypes.HWND
user32.GetTopWindow.argtypes = [wintypes.HWND]
user32.GetTopWindow.restype = wintypes.HWND


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
    _cloaked_by_us: bool = field(default=False, repr=False)
    _user_minimized: bool = field(default=False, repr=False)
    _kick_done: bool = field(default=False, repr=False)
    _saved_pos: tuple[int, int] | None = field(default=None, repr=False)
    _last_kick: float = field(default=0.0, repr=False)

    def ensure_composing(self) -> None:
        """Keep DWM pixels flowing even if the user minimizes the target."""
        hwnd = self.hwnd
        if not hwnd or not user32.IsWindow(hwnd):
            return
        iconic = bool(user32.IsIconic(hwnd))
        cloaked = _is_cloaked(hwnd)
        if iconic:
            self._user_minimized = True
            now = time.monotonic()
            if self._last_kick and now - self._last_kick < 0.4:
                return
            self._last_kick = now
            self._restore_hidden()
            return
        if self._cloaked_by_us:
            if not cloaked and self._saved_pos is None:
                # User brought the window back (taskbar / restore).
                self._cloaked_by_us = False
                self._user_minimized = False
            return
        self._user_minimized = False

    def _restore_hidden(self) -> None:
        hwnd = self.hwnd
        wp = _placement(hwnd)
        if wp is not None:
            wp.showCmd = SW_SHOWNOACTIVATE
            user32.SetWindowPlacement(hwnd, ctypes.byref(wp))
        else:
            user32.ShowWindow(hwnd, SW_RESTORE)
        user32.SetWindowPos(
            hwnd,
            HWND_BOTTOM,
            0,
            0,
            0,
            0,
            SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE,
        )
        if not _set_cloak(hwnd, True):
            # Cloak unavailable: park the restored window off the virtual screen.
            vx = user32.GetSystemMetrics(76)
            x, y, w, _h = _normal_rect(hwnd)
            if self._saved_pos is None:
                self._saved_pos = (x, y)
            user32.SetWindowPos(
                hwnd,
                HWND_BOTTOM,
                int(vx - max(w, 64) - 80),
                y,
                0,
                0,
                SWP_NOSIZE | SWP_NOACTIVATE | SWP_SHOWWINDOW,
            )
        self._cloaked_by_us = True
        if not self._kick_done:
            time.sleep(0.25)
            user32.RedrawWindow(hwnd, None, None, 0x0103)
            time.sleep(0.05)
            self._kick_done = True
        else:
            time.sleep(0.05)
            user32.RedrawWindow(hwnd, None, None, 0x0103)

    def revert(self) -> None:
        if not user32.IsWindow(self.hwnd):
            return
        hwnd = self.hwnd
        if self._cloaked_by_us:
            _set_cloak(hwnd, False)
            self._cloaked_by_us = False
        if self._saved_pos is not None:
            x, y = self._saved_pos
            user32.SetWindowPos(
                hwnd,
                HWND_NOTOPMOST,
                x,
                y,
                0,
                0,
                SWP_NOSIZE | SWP_NOACTIVATE,
            )
            self._saved_pos = None
        if self._user_minimized or self.was_minimized:
            if self._placement is not None:
                user32.SetWindowPlacement(hwnd, ctypes.byref(self._placement))
            else:
                user32.ShowWindow(hwnd, SW_SHOWMINIMIZED)
            return
        if self._placement is not None and self.was_minimized:
            user32.SetWindowPlacement(hwnd, ctypes.byref(self._placement))


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


def _set_cloak(hwnd: int, cloak: bool) -> bool:
    """Hide the HWND from the user while DWM still composes it (PrintWindow works)."""
    val = wintypes.BOOL(1 if cloak else 0)
    try:
        hr = dwmapi.DwmSetWindowAttribute(
            wintypes.HWND(hwnd),
            DWMWA_CLOAK,
            ctypes.byref(val),
            ctypes.sizeof(val),
        )
        return hr == 0
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


def grab_source_rect(hwnd: int) -> tuple[int, int, int, int]:
    """Size PrintWindow should use; falls back to the restore rect if iconic."""
    x, y, w, h = current_frame_rect(hwnd)
    if w >= 2 and h >= 2:
        return x, y, w, h
    return _normal_rect(hwnd)


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
        title="Entire screen",
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


def _is_share_target(hwnd: int) -> bool:
    """Windows a screen-share picker would offer (plus browsers)."""
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
    if not visible and not minimized:
        return False
    if _is_cloaked(hwnd) and not minimized and not is_browser:
        return False

    ex = _exstyle(hwnd)
    if (ex & WS_EX_TOOLWINDOW) and not (ex & WS_EX_APPWINDOW) and not is_browser:
        return False
    owner = user32.GetWindow(hwnd, GW_OWNER)
    if owner and not (ex & WS_EX_APPWINDOW) and not is_browser:
        return False

    root_owner = user32.GetAncestor(hwnd, GA_ROOTOWNER) or hwnd
    walk = int(root_owner)
    for _ in range(16):
        popup = int(user32.GetLastActivePopup(walk) or walk)
        if popup == walk or user32.IsWindowVisible(popup):
            break
        walk = popup
    if int(hwnd) != walk and not (ex & WS_EX_APPWINDOW) and not is_browser:
        return False

    _x, _y, w, h = _normal_rect(hwnd)
    if is_browser:
        return w >= 16 and h >= 16
    if not title:
        return False
    return w >= 32 and h >= 32


def _info_from_hwnd(hwnd: int) -> WindowInfo | None:
    hwnd = int(hwnd)
    if not _is_share_target(hwnd):
        return None
    pid_dw = wintypes.DWORD(0)
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid_dw))
    pid = int(pid_dw.value)
    exe = _process_image(pid)
    title = _window_title(hwnd) or f"{exe or 'window'} hwnd={hwnd}"
    x, y, w, h = _normal_rect(hwnd)
    return WindowInfo(
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


def list_windows() -> list[WindowInfo]:
    """Screen-share order: Z-order from front to back, Entire screen first."""
    found: list[WindowInfo] = []
    seen: set[int] = set()

    def _add(hwnd: int) -> None:
        hwnd = int(hwnd)
        if hwnd in seen:
            return
        info = _info_from_hwnd(hwnd)
        if info is None:
            return
        seen.add(hwnd)
        found.append(info)

    desktop = user32.GetDesktopWindow()
    hwnd = user32.GetTopWindow(desktop)
    while hwnd:
        _add(hwnd)
        hwnd = user32.GetWindow(hwnd, GW_HWNDNEXT)

    @WNDENUMPROC
    def _cb(h: int, _lparam: int) -> bool:
        _add(h)
        return True

    user32.EnumWindows(_cb, 0)
    return [_virtual_screen()] + found


def window_at_point(x: int, y: int, exclude_hwnds: set[int] | None = None) -> WindowInfo | None:
    """Top-level window under a screen point (share-picker target)."""
    exclude_hwnds = exclude_hwnds or set()
    pt = POINT(int(x), int(y))
    hwnd = int(user32.WindowFromPoint(pt) or 0)
    if not hwnd:
        return None
    root = int(user32.GetAncestor(hwnd, GA_ROOT) or hwnd)
    if root in exclude_hwnds:
        return None
    info = _info_from_hwnd(root)
    if info is not None:
        return info
    if root and root not in exclude_hwnds and user32.IsWindow(root):
        pid_dw = wintypes.DWORD(0)
        user32.GetWindowThreadProcessId(root, ctypes.byref(pid_dw))
        pid = int(pid_dw.value)
        exe = _process_image(pid)
        title = _window_title(root) or exe or f"hwnd={root}"
        x0, y0, w, h = _normal_rect(root)
        return WindowInfo(
            hwnd=root,
            title=title,
            pid=pid,
            exe=exe,
            x=x0,
            y=y0,
            width=w,
            height=h,
            class_name=_class_name(root),
            minimized=bool(user32.IsIconic(root)),
            cloaked=_is_cloaked(root),
        )
    return None


def refresh_geometry(info: WindowInfo) -> WindowInfo:
    if info.is_desktop:
        return _virtual_screen()
    if not user32.IsWindow(info.hwnd):
        return info
    x, y, w, h = grab_source_rect(info.hwnd)
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
    """Make a (possibly minimized) HWND composable without leaving it in the user's face.

    Minimized targets are restored then DWM-cloaked so PrintWindow still
    gets pixels. Caller must revert() after recording.
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
    token._user_minimized = token.was_minimized
    if token.was_minimized:
        token.ensure_composing()
    return token


user32.GetCursorPos.argtypes = [ctypes.POINTER(POINT)]
user32.GetCursorPos.restype = wintypes.BOOL
user32.GetAsyncKeyState.argtypes = [ctypes.c_int]
user32.GetAsyncKeyState.restype = ctypes.c_short


def cursor_pos() -> tuple[int, int]:
    pt = POINT()
    if not user32.GetCursorPos(ctypes.byref(pt)):
        return 0, 0
    return int(pt.x), int(pt.y)


def left_button_down() -> bool:
    return bool(user32.GetAsyncKeyState(0x01) & 0x8000)


def escape_pressed() -> bool:
    return bool(user32.GetAsyncKeyState(0x1B) & 0x0001)
