#!/usr/bin/env python3
"""Grab BGRA frames from a specific HWND (PrintWindow / BitBlt).

This is window-accurate: overlapping windows are not in the shot, and after
prepare_for_capture() a previously minimized window still has a bitmap.
PW_RENDERFULLCONTENT is required for Chrome's GPU-composited surface.
"""

from __future__ import annotations

import ctypes
from ctypes import wintypes

from windows import current_frame_rect

user32 = ctypes.windll.user32
gdi32 = ctypes.windll.gdi32

SRCCOPY = 0x00CC0020
DIB_RGB_COLORS = 0
BI_RGB = 0
PW_RENDERFULLCONTENT = 0x00000002

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


def grab_hwnd_bgra(hwnd: int, out_w: int, out_h: int) -> bytes:
    """Return one BGRA frame of out_w x out_h from hwnd (stretched if resized)."""
    if not user32.IsWindow(hwnd):
        raise RuntimeError("Window closed during capture")
    _x, _y, src_w, src_h = current_frame_rect(hwnd)
    src_w = _even(src_w)
    src_h = _even(src_h)
    if src_w < 2 or src_h < 2:
        # Still iconic or not composed - emit black so encode keeps going.
        return b"\x00" * (out_w * out_h * 4)

    hdc_win = user32.GetWindowDC(hwnd)
    if not hdc_win:
        raise RuntimeError("GetWindowDC failed")
    hdc_src = gdi32.CreateCompatibleDC(hdc_win)
    hdc_dst = gdi32.CreateCompatibleDC(hdc_win)
    try:
        src = _Dib(hdc_src, src_w, src_h)
        printed = user32.PrintWindow(hwnd, hdc_src, PW_RENDERFULLCONTENT)
        if not printed:
            printed = user32.PrintWindow(hwnd, hdc_src, 0)
        if not printed:
            gdi32.BitBlt(hdc_src, 0, 0, src_w, src_h, hdc_win, 0, 0, SRCCOPY)
        if src_w == out_w and src_h == out_h:
            data = src.to_bytes()
        else:
            dst = _Dib(hdc_dst, out_w, out_h)
            gdi32.SetStretchBltMode(hdc_dst, 4)  # HALFTONE
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
