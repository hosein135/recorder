#!/usr/bin/env python3
"""Grab BGRA frames from a specific HWND (PrintWindow / BitBlt / DWM).

Same idea as Mirillis Action! Window / Selected application mode: capture
that window in place. The HWND is never restored, cloaked, moved, or
forced on top — the user can still see it, minimize it, and maximize it.
Minimized windows keep recording at restore size (not the 146x20 ghost rect).
"""

from __future__ import annotations

import ctypes
from ctypes import wintypes

import numpy as np

from windows import RECT, grab_source_rect, window_is_maximized, window_is_minimized

user32 = ctypes.windll.user32
gdi32 = ctypes.windll.gdi32
dwmapi = ctypes.windll.dwmapi
kernel32 = ctypes.windll.kernel32

SRCCOPY = 0x00CC0020
DIB_RGB_COLORS = 0
BI_RGB = 0
PW_CLIENTONLY = 0x00000001
PW_RENDERFULLCONTENT = 0x00000002
WM_PRINT = 0x0317
PRF_NONCLIENT = 0x00000002
PRF_CLIENT = 0x00000004
PRF_ERASEBKGND = 0x00000008
PRF_CHILDREN = 0x00000010
PRF_OWNED = 0x00000020
WM_PRINT_FLAGS = PRF_CLIENT | PRF_NONCLIENT | PRF_CHILDREN | PRF_ERASEBKGND | PRF_OWNED

WS_POPUP = 0x80000000
WS_EX_TOOLWINDOW = 0x00000080
WS_EX_NOACTIVATE = 0x08000000
WS_EX_LAYERED = 0x00080000
SW_SHOWNOACTIVATE = 4
HWND_BOTTOM = 1
SWP_NOACTIVATE = 0x0010
SWP_SHOWWINDOW = 0x0040
SWP_NOZORDER = 0x0004
LWA_ALPHA = 0x00000002
DWMWA_CLOAK = 13
DWM_TNP_RECTDESTINATION = 0x00000001
DWM_TNP_VISIBLE = 0x00000008
DWM_TNP_OPACITY = 0x00000004
PM_REMOVE = 0x0001

gdi32.CreateCompatibleDC.restype = wintypes.HDC
gdi32.CreateCompatibleDC.argtypes = [wintypes.HDC]
gdi32.DeleteDC.argtypes = [wintypes.HDC]
gdi32.SelectObject.restype = wintypes.HGDIOBJ
gdi32.SelectObject.argtypes = [wintypes.HDC, wintypes.HGDIOBJ]
gdi32.DeleteObject.argtypes = [wintypes.HGDIOBJ]
gdi32.BitBlt.argtypes = [
    wintypes.HDC,
    ctypes.c_int,
    ctypes.c_int,
    ctypes.c_int,
    ctypes.c_int,
    wintypes.HDC,
    ctypes.c_int,
    ctypes.c_int,
    wintypes.DWORD,
]
gdi32.SetStretchBltMode.argtypes = [wintypes.HDC, ctypes.c_int]
gdi32.StretchBlt.argtypes = [
    wintypes.HDC,
    ctypes.c_int,
    ctypes.c_int,
    ctypes.c_int,
    ctypes.c_int,
    wintypes.HDC,
    ctypes.c_int,
    ctypes.c_int,
    ctypes.c_int,
    ctypes.c_int,
    wintypes.DWORD,
]
user32.GetDC.restype = wintypes.HDC
user32.GetDC.argtypes = [wintypes.HWND]
user32.GetWindowDC.restype = wintypes.HDC
user32.GetWindowDC.argtypes = [wintypes.HWND]
user32.ReleaseDC.argtypes = [wintypes.HWND, wintypes.HDC]
user32.PrintWindow.argtypes = [wintypes.HWND, wintypes.HDC, wintypes.UINT]
user32.PrintWindow.restype = wintypes.BOOL
user32.SendMessageW.restype = wintypes.LPARAM
user32.CreateWindowExW.restype = wintypes.HWND
user32.CreateWindowExW.argtypes = [
    wintypes.DWORD,
    wintypes.LPCWSTR,
    wintypes.LPCWSTR,
    wintypes.DWORD,
    ctypes.c_int,
    ctypes.c_int,
    ctypes.c_int,
    ctypes.c_int,
    wintypes.HWND,
    wintypes.HMENU,
    wintypes.HINSTANCE,
    wintypes.LPVOID,
]


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


class SIZE(ctypes.Structure):
    _fields_ = [("cx", wintypes.LONG), ("cy", wintypes.LONG)]


class DWM_THUMBNAIL_PROPERTIES(ctypes.Structure):
    _fields_ = [
        ("dwFlags", wintypes.DWORD),
        ("rcDestination", RECT),
        ("rcSource", RECT),
        ("opacity", ctypes.c_ubyte),
        ("fVisible", wintypes.BOOL),
        ("fSourceClientAreaOnly", wintypes.BOOL),
    ]


class MSG(ctypes.Structure):
    _fields_ = [
        ("hwnd", wintypes.HWND),
        ("message", wintypes.UINT),
        ("wParam", wintypes.WPARAM),
        ("lParam", wintypes.LPARAM),
        ("time", wintypes.DWORD),
        ("pt", wintypes.POINT),
    ]


gdi32.CreateDIBSection.restype = wintypes.HBITMAP
gdi32.CreateDIBSection.argtypes = [
    wintypes.HDC,
    ctypes.POINTER(BITMAPINFO),
    wintypes.UINT,
    ctypes.POINTER(ctypes.c_void_p),
    wintypes.HANDLE,
    wintypes.DWORD,
]


def _even(n: int) -> int:
    return max(2, n - (n % 2))


def _fit_rect(src_w: int, src_h: int, dst_w: int, dst_h: int) -> tuple[int, int, int, int]:
    """Letterbox dest rect so maximize/restore keep aspect ratio."""
    if src_w < 2 or src_h < 2:
        return 0, 0, dst_w, dst_h
    scale = min(dst_w / src_w, dst_h / src_h)
    nw = _even(max(2, int(src_w * scale)))
    nh = _even(max(2, int(src_h * scale)))
    if nw > dst_w:
        nw = _even(dst_w)
    if nh > dst_h:
        nh = _even(dst_h)
    x = max(0, (dst_w - nw) // 2)
    y = max(0, (dst_h - nh) // 2)
    return x, y, nw, nh


class _Dib:
    def __init__(self, hdc: int, width: int, height: int) -> None:
        self.width = width
        self.height = height
        self.bits = ctypes.c_void_p()
        bmi = BITMAPINFO()
        bmi.bmiHeader.biSize = ctypes.sizeof(BITMAPINFOHEADER)
        bmi.bmiHeader.biWidth = width
        bmi.bmiHeader.biHeight = -height  # top-down BGRA
        bmi.bmiHeader.biPlanes = 1
        bmi.bmiHeader.biBitCount = 32
        bmi.bmiHeader.biCompression = BI_RGB
        self.hbmp = gdi32.CreateDIBSection(
            hdc, ctypes.byref(bmi), DIB_RGB_COLORS, ctypes.byref(self.bits), None, 0
        )
        if not self.hbmp or not self.bits.value:
            raise RuntimeError("CreateDIBSection failed")
        self._old = gdi32.SelectObject(hdc, self.hbmp)

    def nbytes(self) -> int:
        return self.width * self.height * 4

    def to_bytes(self) -> bytes:
        return ctypes.string_at(self.bits.value, self.nbytes())


def _paint_hwnd(hwnd: int, hdc: int, width: int, height: int) -> None:
    """Paint hwnd into hdc. Tries PrintWindow / WM_PRINT even if minimized."""
    if user32.PrintWindow(hwnd, hdc, PW_RENDERFULLCONTENT):
        return
    if user32.PrintWindow(hwnd, hdc, 0):
        return
    if user32.PrintWindow(hwnd, hdc, PW_CLIENTONLY):
        return
    user32.SendMessageW(hwnd, WM_PRINT, hdc, WM_PRINT_FLAGS)
    hdc_win = user32.GetWindowDC(hwnd)
    if hdc_win:
        try:
            gdi32.BitBlt(hdc, 0, 0, width, height, hdc_win, 0, 0, SRCCOPY)
        finally:
            user32.ReleaseDC(hwnd, hdc_win)


def grab_hwnd_bgra(hwnd: int, out_w: int, out_h: int, *, letterbox: bool = False) -> bytes:
    """Return one BGRA frame of out_w x out_h from hwnd.

    letterbox=True keeps aspect ratio when the user maximizes or resizes
    (bars instead of stretching). Thumbs keep letterbox=False.
    """
    if not user32.IsWindow(hwnd):
        raise RuntimeError("Window closed during capture")
    _x, _y, src_w, src_h = grab_source_rect(hwnd)
    src_w = _even(src_w)
    src_h = _even(src_h)
    if src_w < 2 or src_h < 2:
        return b"\x00" * (out_w * out_h * 4)

    hdc_win = user32.GetWindowDC(hwnd)
    if not hdc_win:
        raise RuntimeError("GetWindowDC failed")
    hdc_src = gdi32.CreateCompatibleDC(hdc_win)
    hdc_dst = gdi32.CreateCompatibleDC(hdc_win)
    try:
        src = _Dib(hdc_src, src_w, src_h)
        _paint_hwnd(hwnd, hdc_src, src_w, src_h)
        if src_w == out_w and src_h == out_h:
            data = src.to_bytes()
        else:
            dst = _Dib(hdc_dst, out_w, out_h)
            if dst.bits.value:
                ctypes.memset(dst.bits.value, 0, dst.nbytes())
            gdi32.SetStretchBltMode(hdc_dst, 4)
            if letterbox:
                x, y, nw, nh = _fit_rect(src_w, src_h, out_w, out_h)
                gdi32.StretchBlt(
                    hdc_dst, x, y, nw, nh, hdc_src, 0, 0, src_w, src_h, SRCCOPY
                )
            else:
                gdi32.StretchBlt(
                    hdc_dst, 0, 0, out_w, out_h, hdc_src, 0, 0, src_w, src_h, SRCCOPY
                )
            data = dst.to_bytes()
            gdi32.SelectObject(hdc_dst, dst._old)
            gdi32.DeleteObject(dst.hbmp)
        gdi32.SelectObject(hdc_src, src._old)
        gdi32.DeleteObject(src.hbmp)
        return data
    finally:
        gdi32.DeleteDC(hdc_src)
        gdi32.DeleteDC(hdc_dst)
        user32.ReleaseDC(hwnd, hdc_win)


def _nearly_black(bgra: bytes) -> bool:
    if not bgra:
        return True
    arr = np.frombuffer(bgra, dtype=np.uint8)
    if arr.size < 16:
        return True
    sample = arr.reshape(-1, 4)[::16, :3]
    return int(sample.max()) < 12


class WindowGrabber:
    """Independent HWND capture. Never changes the target's min/max/z-order."""

    def __init__(self, hwnd: int, out_w: int, out_h: int) -> None:
        self.hwnd = hwnd
        self.out_w = out_w
        self.out_h = out_h
        self._last = b"\x00" * (out_w * out_h * 4)
        self._host = 0
        self._thumb = wintypes.HANDLE()

    def grab(self) -> tuple[bytes, str | None]:
        if not user32.IsWindow(self.hwnd):
            raise RuntimeError("Window closed during capture")
        iconic = window_is_minimized(self.hwnd)
        frame = grab_hwnd_bgra(self.hwnd, self.out_w, self.out_h, letterbox=True)
        if iconic and _nearly_black(frame):
            thumb = self._grab_dwm_thumb()
            if thumb and not _nearly_black(thumb):
                frame = thumb
        if not _nearly_black(frame):
            self._last = frame
        elif not _nearly_black(self._last):
            frame = self._last
        if iconic:
            return frame, "minimized"
        if window_is_maximized(self.hwnd):
            return frame, "maximized"
        return frame, "open"

    def close(self) -> None:
        self._release_thumb()

    def _pump(self) -> None:
        msg = MSG()
        while user32.PeekMessageW(ctypes.byref(msg), None, 0, 0, PM_REMOVE):
            user32.TranslateMessage(ctypes.byref(msg))
            user32.DispatchMessageW(ctypes.byref(msg))

    def _ensure_host(self) -> int:
        if self._host and user32.IsWindow(self._host):
            return int(self._host)
        inst = kernel32.GetModuleHandleW(None)
        host = user32.CreateWindowExW(
            WS_EX_TOOLWINDOW | WS_EX_NOACTIVATE | WS_EX_LAYERED,
            "Static",
            "recorder-thumb-host",
            WS_POPUP,
            0,
            0,
            self.out_w,
            self.out_h,
            None,
            None,
            inst,
            None,
        )
        if not host:
            return 0
        cloak = wintypes.BOOL(True)
        try:
            dwmapi.DwmSetWindowAttribute(
                host, DWMWA_CLOAK, ctypes.byref(cloak), ctypes.sizeof(cloak)
            )
        except Exception:
            pass
        try:
            user32.SetLayeredWindowAttributes(host, 0, 1, LWA_ALPHA)
        except Exception:
            pass
        user32.SetWindowPos(
            host,
            HWND_BOTTOM,
            0,
            0,
            self.out_w,
            self.out_h,
            SWP_NOACTIVATE | SWP_SHOWWINDOW | SWP_NOZORDER,
        )
        user32.ShowWindow(host, SW_SHOWNOACTIVATE)
        self._host = int(host)
        return self._host

    def _release_thumb(self) -> None:
        if self._thumb:
            try:
                dwmapi.DwmUnregisterThumbnail(self._thumb)
            except Exception:
                pass
            self._thumb = wintypes.HANDLE()
        if self._host:
            try:
                user32.DestroyWindow(self._host)
            except Exception:
                pass
            self._host = 0

    def _grab_dwm_thumb(self) -> bytes | None:
        """DWM still has a thumbnail of minimized windows (taskbar / Alt+Tab)."""
        host = self._ensure_host()
        if not host or not user32.IsWindow(self.hwnd):
            return None
        if not self._thumb:
            thumb = wintypes.HANDLE()
            hr = dwmapi.DwmRegisterThumbnail(host, self.hwnd, ctypes.byref(thumb))
            if hr != 0 or not thumb:
                return None
            self._thumb = thumb
        props = DWM_THUMBNAIL_PROPERTIES()
        props.dwFlags = DWM_TNP_RECTDESTINATION | DWM_TNP_VISIBLE | DWM_TNP_OPACITY
        props.rcDestination = RECT(0, 0, self.out_w, self.out_h)
        props.opacity = 255
        props.fVisible = True
        props.fSourceClientAreaOnly = False
        dwmapi.DwmUpdateThumbnailProperties(self._thumb, ctypes.byref(props))
        self._pump()
        hdc_host = user32.GetWindowDC(host) or user32.GetDC(host)
        if not hdc_host:
            return None
        hdc = gdi32.CreateCompatibleDC(hdc_host)
        try:
            dib = _Dib(hdc, self.out_w, self.out_h)
            painted = user32.PrintWindow(host, hdc, PW_RENDERFULLCONTENT)
            if not painted:
                gdi32.BitBlt(hdc, 0, 0, self.out_w, self.out_h, hdc_host, 0, 0, SRCCOPY)
            data = dib.to_bytes()
            gdi32.SelectObject(hdc, dib._old)
            gdi32.DeleteObject(dib.hbmp)
            return data
        finally:
            gdi32.DeleteDC(hdc)
            user32.ReleaseDC(host, hdc_host)


def grab_screen_bgra(x: int, y: int, src_w: int, src_h: int, out_w: int, out_h: int) -> bytes:
    """BitBlt the virtual screen (Entire screen). Coords may be negative."""
    src_w = _even(src_w)
    src_h = _even(src_h)
    if src_w < 2 or src_h < 2:
        return b"\x00" * (out_w * out_h * 4)
    hdc_screen = user32.GetDC(0)
    if not hdc_screen:
        raise RuntimeError("GetDC(desktop) failed")
    hdc_src = gdi32.CreateCompatibleDC(hdc_screen)
    hdc_dst = gdi32.CreateCompatibleDC(hdc_screen)
    try:
        src = _Dib(hdc_src, src_w, src_h)
        gdi32.BitBlt(hdc_src, 0, 0, src_w, src_h, hdc_screen, int(x), int(y), SRCCOPY)
        if src_w == out_w and src_h == out_h:
            data = src.to_bytes()
        else:
            dst = _Dib(hdc_dst, out_w, out_h)
            gdi32.SetStretchBltMode(hdc_dst, 4)
            gdi32.StretchBlt(
                hdc_dst, 0, 0, out_w, out_h, hdc_src, 0, 0, src_w, src_h, SRCCOPY
            )
            data = dst.to_bytes()
            gdi32.SelectObject(hdc_dst, dst._old)
            gdi32.DeleteObject(dst.hbmp)
        gdi32.SelectObject(hdc_src, src._old)
        gdi32.DeleteObject(src.hbmp)
        return data
    finally:
        gdi32.DeleteDC(hdc_src)
        gdi32.DeleteDC(hdc_dst)
        user32.ReleaseDC(0, hdc_screen)


def bgra_to_ppm(bgra: bytes, width: int, height: int) -> bytes:
    expected = width * height * 4
    arr = np.frombuffer(bgra, dtype=np.uint8)
    if arr.size < expected:
        arr = np.pad(arr, (0, expected - int(arr.size)))
    rgb = arr[:expected].reshape(height, width, 4)[:, :, [2, 1, 0]]
    header = f"P6\n{width} {height}\n255\n".encode("ascii")
    return header + rgb.tobytes()


def placeholder_ppm(width: int, height: int) -> bytes:
    rgb = bytes([45, 48, 56]) * (width * height)
    return f"P6\n{width} {height}\n255\n".encode("ascii") + rgb


def grab_thumb_ppm(
    hwnd: int,
    out_w: int,
    out_h: int,
    screen: tuple[int, int, int, int] | None = None,
) -> bytes:
    """Small PPM for a screen-share style picker. Never raises."""
    try:
        if hwnd == 0 and screen is not None:
            x, y, w, h = screen
            data = grab_screen_bgra(x, y, w, h, out_w, out_h)
        else:
            data = grab_hwnd_bgra(hwnd, out_w, out_h)
        if _nearly_black(data):
            return placeholder_ppm(out_w, out_h)
        return bgra_to_ppm(data, out_w, out_h)
    except Exception:
        return placeholder_ppm(out_w, out_h)
