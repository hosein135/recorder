#!/usr/bin/env python3
"""Enumerate visible top-level windows (Win32). No extra packages."""

from __future__ import annotations

import ctypes
import sys
from ctypes import wintypes
from dataclasses import dataclass

if sys.platform != "win32":
    raise RuntimeError("Window enumeration requires Windows")

user32 = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32
dwmapi = ctypes.windll.dwmapi

kernel32.QueryFullProcessImageNameW.argtypes = [
    wintypes.HANDLE,
    wintypes.DWORD,
    wintypes.LPWSTR,
    ctypes.POINTER(wintypes.DWORD),
]
kernel32.QueryFullProcessImageNameW.restype = wintypes.BOOL

PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
DWMWA_CLOAKED = 14
GWL_EXSTYLE = -20
WS_EX_TOOLWINDOW = 0x00000080
WS_EX_APPWINDOW = 0x00040000
GW_OWNER = 4

WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)


class RECT(ctypes.Structure):
    _fields_ = [
        ("left", wintypes.LONG),
        ("top", wintypes.LONG),
        ("right", wintypes.LONG),
        ("bottom", wintypes.LONG),
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
    is_desktop: bool = False

    @property
    def even_size(self) -> tuple[int, int]:
        w = max(2, self.width - (self.width % 2))
        h = max(2, self.height - (self.height % 2))
        return w, h

    def label(self) -> str:
        if self.is_desktop:
            return "Entire screen"
        proc = self.exe or f"pid {self.pid}"
        geo = f"{self.width}×{self.height}"
        title = self.title.replace("\n", " ").strip()
        if len(title) > 80:
            title = title[:77] + "…"
        return f"{title}  —  {proc}  ({geo})"


def _window_title(hwnd: int) -> str:
    n = user32.GetWindowTextLengthW(hwnd)
    if n <= 0:
        return ""
    buf = ctypes.create_unicode_buffer(n + 1)
    user32.GetWindowTextW(hwnd, buf, n + 1)
    return buf.value.strip()


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
            path = buf.value
            return path.rsplit("\\", 1)[-1]
        return ""
    finally:
        kernel32.CloseHandle(handle)


def _rect(hwnd: int) -> tuple[int, int, int, int]:
    rc = RECT()
    if not user32.GetWindowRect(hwnd, ctypes.byref(rc)):
        return 0, 0, 0, 0
    w = max(0, rc.right - rc.left)
    h = max(0, rc.bottom - rc.top)
    return rc.left, rc.top, w, h


def _virtual_screen() -> WindowInfo:
    x = user32.GetSystemMetrics(76)  # SM_XVIRTUALSCREEN
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
        is_desktop=True,
    )


def _is_candidate(hwnd: int) -> bool:
    if not user32.IsWindowVisible(hwnd):
        return False
    if user32.IsIconic(hwnd):
        return False
    if _is_cloaked(hwnd):
        return False
    if user32.GetWindow(hwnd, GW_OWNER):
        return False
    ex = user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
    if (ex & WS_EX_TOOLWINDOW) and not (ex & WS_EX_APPWINDOW):
        return False
    title = _window_title(hwnd)
    if not title:
        return False
    _, _, w, h = _rect(hwnd)
    if w < 32 or h < 32:
        return False
    return True


def list_windows() -> list[WindowInfo]:
    found: list[WindowInfo] = []

    @WNDENUMPROC
    def _cb(hwnd: int, _lparam: int) -> bool:
        if not _is_candidate(hwnd):
            return True
        pid = wintypes.DWORD(0)
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        x, y, w, h = _rect(hwnd)
        found.append(
            WindowInfo(
                hwnd=int(hwnd),
                title=_window_title(hwnd),
                pid=int(pid.value),
                exe=_process_image(int(pid.value)),
                x=x,
                y=y,
                width=w,
                height=h,
            )
        )
        return True

    user32.EnumWindows(_cb, 0)
    found.sort(key=lambda w: w.title.lower())
    return [_virtual_screen()] + found


def refresh_geometry(info: WindowInfo) -> WindowInfo:
    if info.is_desktop:
        return _virtual_screen()
    if not user32.IsWindow(info.hwnd):
        return info
    x, y, w, h = _rect(info.hwnd)
    return WindowInfo(
        hwnd=info.hwnd,
        title=_window_title(info.hwnd) or info.title,
        pid=info.pid,
        exe=info.exe,
        x=x,
        y=y,
        width=w,
        height=h,
        is_desktop=False,
    )
