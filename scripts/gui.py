#!/usr/bin/env python3
"""Tkinter GUI: pick a window, FPS, resolution, quality, audio, record H.266/VVC."""

from __future__ import annotations

import math
import os
import shutil
import sys
import tempfile
import threading
import time
import traceback
from datetime import datetime
from pathlib import Path

_SCRIPTS = Path(__file__).resolve().parent
_ROOT = _SCRIPTS.parent
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

if sys.platform == "win32":
    try:
        import ctypes

        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass

import tkinter as tk
from tkinter import filedialog, font as tkfont, messagebox, ttk

from audio import AudioDevice, default_loopback, default_microphone, list_loopbacks, list_microphones
from capture import (
    AUDIO_KBPS_MAX,
    AUDIO_KBPS_MIN,
    CaptureSession,
    DEFAULT_AUDIO_KBPS,
    DEFAULT_FPS,
    DEFAULT_QUALITY,
    DEFAULT_SAMPLE_KHZ,
    DEFAULT_VIDEO_PRESET,
    MIN_USEFUL_AUDIO_KBPS,
    MIN_USEFUL_QUALITY,
    MIN_USEFUL_SAMPLE_KHZ,
    QUALITY_MAX,
    QUALITY_MIN,
    RecordConfig,
    RecorderError,
    SAMPLE_KHZ_MAX,
    SAMPLE_KHZ_MIN,
    VIDEO_PRESETS,
    clamp_audio_kbps,
    clamp_sample_rate,
    default_output_path,
    probe_summary,
    resolve_video_preset,
    sample_khz_to_hz,
    snap_opus_rate,
)
from hwnd_grab import grab_thumb_ppm
from hw_detect import HardwareProfile, detect
from player import find_mpc_hc, format_size, list_recordings, play_with_mpc
from windows import WindowInfo, cursor_pos, escape_pressed, left_button_down, list_windows, window_at_point

BG = "#f3f5f8"
PANEL = "#ffffff"
PANEL2 = "#f6f8fb"
FG = "#1c2434"
MUTED = "#5e6a80"
ACCENT = "#315efb"
ACCENT_HOVER = "#2348d6"
ACCENT_DIM = "#e7edff"
RECORD = "#e11d48"
RECORD_HOVER = "#be123c"
RECORD_DIM = "#fecdd6"
OK = "#0f9f6e"
OK_BG = "#e5f7ef"
BORDER = "#e1e6ef"
WARN = "#b45309"
CHIP_BG = "#eef2f7"
CHIP_WARN_BG = "#fff6db"
ON_COLOR = "#ffffff"
PAUSED_BG = "#fff4d4"
PAUSED_FG = "#8a5a00"

ICON_FONT = "Segoe UI"
UI_FONT = "Segoe UI"
UI_FONT_SEMI = "Segoe UI Semibold"
_MDL2 = {
    "record": "\uE7C8",
    "stop": "\uE71A",
    "play": "\uE768",
    "pause": "\uE769",
    "refresh": "\uE72C",
    "search": "\uE721",
    "settings": "\uE713",
    "folder": "\uE8B7",
    "folderopen": "\uE838",
    "mic": "\uE720",
    "video": "\uE714",
    "history": "\uE81C",
    "plus": "\uE710",
    "minus": "\uE738",
    "volume": "\uE767",
    "clock": "\uE823",
    "pointer": "\uE7C9",
    "window": "\uE8A7",
    "people": "\uE716",
    "cancel": "\uE711",
    "open": "\uE8E5",
}
_FALLBACK = {
    "record": "●",
    "stop": "■",
    "play": ">",
    "pause": "||",
    "refresh": "R",
    "search": "S",
    "settings": "*",
    "folder": "F",
    "folderopen": "F",
    "mic": "M",
    "video": "V",
    "history": "A",
    "plus": "+",
    "minus": "-",
    "volume": "L",
    "clock": "T",
    "pointer": "+",
    "window": "W",
    "people": "B",
    "cancel": "X",
    "open": "O",
}
ICONS = _FALLBACK


def install_icons(root: tk.Misc) -> None:
    global ICON_FONT, ICONS, UI_FONT, UI_FONT_SEMI
    families = set(tkfont.families(root))
    if "Bahnschrift" in families:
        UI_FONT = "Bahnschrift"
        UI_FONT_SEMI = "Bahnschrift SemiBold" if "Bahnschrift SemiBold" in families else "Bahnschrift"
    else:
        UI_FONT = "Segoe UI"
        UI_FONT_SEMI = "Segoe UI Semibold"
    if "Segoe MDL2 Assets" in families or "Segoe Fluent Icons" in families:
        ICON_FONT = "Segoe Fluent Icons" if "Segoe Fluent Icons" in families else "Segoe MDL2 Assets"
        ICONS = _MDL2
    else:
        ICON_FONT = UI_FONT
        ICONS = _FALLBACK


def ico(name: str) -> str:
    return ICONS.get(name, "")


def _hex_rgb(value: str) -> tuple[int, int, int]:
    value = value.lstrip("#")
    return int(value[0:2], 16), int(value[2:4], 16), int(value[4:6], 16)


def _round_ppm_file(path: Path, radius: int, color: tuple[int, int, int]) -> None:
    """Paint pixels outside a rounded rectangle so a thumbnail can sit in a round tile."""
    import numpy as np

    raw = path.read_bytes()
    if not raw.startswith(b"P6"):
        return
    parts = raw.split(b"\n", 3)
    if len(parts) < 4:
        return
    magic, size, depth, body = parts
    try:
        width, height = (int(n) for n in size.split())
    except ValueError:
        return
    arr = np.frombuffer(body, dtype=np.uint8)
    if arr.size < width * height * 3:
        return
    arr = arr[: width * height * 3].reshape(height, width, 3).copy()
    radius = max(1, min(radius, width // 2, height // 2))
    yy, xx = np.ogrid[:height, :width]
    centers = (
        (radius, radius),
        (width - radius, radius),
        (radius, height - radius),
        (width - radius, height - radius),
    )
    outside = np.zeros((height, width), dtype=bool)
    for cx, cy in centers:
        box_x = (xx >= cx - radius) & (xx < cx + radius)
        box_y = (yy >= cy - radius) & (yy < cy + radius)
        outside |= box_x & box_y & ((xx - cx) ** 2 + (yy - cy) ** 2 > radius ** 2)
    arr[outside] = color
    path.write_bytes(b"\n".join((magic, size, depth)) + b"\n" + arr.tobytes())


def _cover_corner(canvas: tk.Canvas, cx: float, cy: float, radius: int, corner: str, color: str) -> None:
    """Fill the square outside one rounded corner."""
    sweeps = {"tl": (90, 180), "tr": (0, 90), "bl": (180, 270), "br": (270, 360)}
    tips = {
        "tl": (cx - radius, cy - radius),
        "tr": (cx + radius, cy - radius),
        "bl": (cx - radius, cy + radius),
        "br": (cx + radius, cy + radius),
    }
    start, end = sweeps[corner]
    points = [tips[corner]]
    steps = 14
    for i in range(steps + 1):
        angle = math.radians(start + (end - start) * i / steps)
        points.append((cx + radius * math.cos(angle), cy - radius * math.sin(angle)))
    flat = [coord for point in points for coord in point]
    canvas.create_polygon(*flat, fill=color, outline=color)


def _widget_bg(widget: tk.Misc) -> str:
    for key in ("bg", "background"):
        try:
            value = str(widget.cget(key))
        except tk.TclError:
            continue
        if value:
            return value
    return BG


def _round_shape(canvas: tk.Canvas, x1: int, y1: int, x2: int, y2: int, radius: int, fill: str) -> None:
    r = max(1, min(radius, (x2 - x1) // 2, (y2 - y1) // 2))
    kw = {"fill": fill, "outline": fill}
    canvas.create_arc(x1, y1, x1 + 2 * r, y1 + 2 * r, start=90, extent=90, style="pieslice", **kw)
    canvas.create_arc(x2 - 2 * r, y1, x2, y1 + 2 * r, start=0, extent=90, style="pieslice", **kw)
    canvas.create_arc(x1, y2 - 2 * r, x1 + 2 * r, y2, start=180, extent=90, style="pieslice", **kw)
    canvas.create_arc(x2 - 2 * r, y2 - 2 * r, x2, y2, start=270, extent=90, style="pieslice", **kw)
    canvas.create_rectangle(x1 + r, y1, x2 - r, y2, **kw)
    canvas.create_rectangle(x1, y1 + r, x2, y2 - r, **kw)


class RoundButton(tk.Canvas):
    """Pill button. configure() accepts text, icon, state, and the old ttk style names."""

    _STYLES = {
        "Record.TButton": ("record", "record"),
        "Stop.TButton": ("stop", "stop"),
        "Accent.TButton": ("accent", None),
        "Ghost.TButton": ("ghost", None),
    }

    def __init__(
        self,
        master: tk.Misc,
        text: str = "",
        command: object | None = None,
        kind: str = "ghost",
        icon: str = "",
        height: int = 40,
        min_width: int = 96,
        canvas_bg: str | None = None,
    ) -> None:
        page = canvas_bg or _widget_bg(master)
        super().__init__(
            master,
            height=height,
            width=min_width,
            bg=page,
            highlightthickness=0,
            bd=0,
            cursor="hand2",
        )
        self._page = page
        self._text = text
        self._icon = icon
        self._kind = kind
        self._state = "normal"
        self._hover = False
        self._selected = False
        self._command = command
        self._height = height
        self._min_width = min_width
        self._drawing = False
        self._text_font = tkfont.Font(self, family=UI_FONT_SEMI, size=10)
        self._icon_font = tkfont.Font(self, family=ICON_FONT, size=12)
        self.bind("<Configure>", lambda _e: self._redraw())
        self.bind("<Enter>", self._on_enter)
        self.bind("<Leave>", self._on_leave)
        self.bind("<Button-1>", self._on_click)
        self._redraw()

    def set_selected(self, selected: bool) -> None:
        self._selected = selected
        self._redraw()

    def _on_enter(self, _event: object) -> None:
        if self._state == "disabled":
            return
        self._hover = True
        self._redraw()

    def _on_leave(self, _event: object) -> None:
        self._hover = False
        self._redraw()

    def _on_click(self, _event: object) -> None:
        if self._state == "disabled" or self._command is None:
            return
        self._command()

    def configure(self, cnf: object = None, **kw: object) -> object:  # type: ignore[override]
        if isinstance(cnf, dict):
            kw = {**cnf, **kw}
        changed = False
        if "text" in kw:
            self._text = str(kw.pop("text"))
            changed = True
        if "icon" in kw:
            self._icon = str(kw.pop("icon"))
            changed = True
        if "state" in kw:
            self._state = str(kw.pop("state"))
            tk.Canvas.configure(self, cursor="arrow" if self._state == "disabled" else "hand2")
            changed = True
        if "style" in kw:
            kind, icon = self._STYLES.get(str(kw.pop("style")), (self._kind, None))
            self._kind = kind
            if icon:
                self._icon = icon
            changed = True
        if changed:
            self._redraw()
        if kw:
            return tk.Canvas.configure(self, **kw)
        return None

    config = configure

    def _colors(self) -> tuple[str, str, str]:
        if self._state == "disabled":
            return PANEL2, MUTED, BORDER
        if self._selected or self._kind == "accent":
            fill = ACCENT_HOVER if self._hover else ACCENT
            return fill, ON_COLOR, fill
        if self._kind == "record":
            fill = RECORD_HOVER if self._hover else RECORD
            return fill, ON_COLOR, fill
        if self._kind == "stop":
            fill = RECORD if self._hover else RECORD_HOVER
            return fill, ON_COLOR, fill
        fill = ACCENT_DIM if self._hover else PANEL
        fg = ACCENT if self._hover else FG
        border = ACCENT if self._hover else BORDER
        return fill, fg, border

    def _content_width(self) -> int:
        side = 22
        glyph = ico(self._icon) if self._icon else ""
        gap = 10 if glyph and self._text else 0
        icon_w = (self._icon_font.measure(glyph) + 6) if glyph else 0
        text_w = self._text_font.measure(self._text) if self._text else 0
        return int(icon_w + gap + text_w + side * 2) + 8

    def _redraw(self) -> None:
        if self._drawing:
            return
        self._drawing = True
        try:
            self._paint()
        finally:
            self._drawing = False

    def _paint(self) -> None:
        self.delete("all")
        needed = max(self._min_width, self._content_width())
        current = self.winfo_width()
        if current < needed:
            tk.Canvas.configure(self, width=needed)
            width = needed
        else:
            width = current
        height = self.winfo_height()
        if height < 2:
            height = self._height
        fill, fg, border = self._colors()
        radius = height // 2
        _round_shape(self, 1, 1, width - 1, height - 1, radius, border)
        inset = 2 if border != fill else 1
        _round_shape(self, inset, inset, width - inset, height - inset, max(1, radius - 1), fill)
        glyph = ico(self._icon) if self._icon else ""
        cy = height / 2
        if glyph and self._text:
            gap = 10
            icon_w = self._icon_font.measure(glyph) + 6
            text_w = self._text_font.measure(self._text)
            x = (width - icon_w - gap - text_w) / 2
            self.create_text(x, cy, text=glyph, anchor="w", font=self._icon_font, fill=fg)
            self.create_text(x + icon_w + gap, cy, text=self._text, anchor="w", font=self._text_font, fill=fg)
        elif glyph:
            self.create_text(width / 2, cy, text=glyph, font=self._icon_font, fill=fg)
        else:
            self.create_text(width / 2, cy, text=self._text, font=self._text_font, fill=fg)


class SlimScroll(tk.Canvas):
    """Thin rounded scrollbar with no arrow buttons."""

    def __init__(
        self,
        master: tk.Misc,
        command: object,
        orient: str = "vertical",
        canvas_bg: str | None = None,
    ) -> None:
        self._orient = orient
        bg = canvas_bg or _widget_bg(master)
        if orient == "horizontal":
            super().__init__(master, height=10, bg=bg, highlightthickness=0, bd=0)
        else:
            super().__init__(master, width=10, bg=bg, highlightthickness=0, bd=0)
        self._command = command
        self._first = 0.0
        self._last = 1.0
        self._hover = False
        self._press: tuple[float, float] | None = None
        self.bind("<Configure>", lambda _e: self._redraw())
        self.bind("<Enter>", lambda _e: self._set_hover(True))
        self.bind("<Leave>", lambda _e: self._set_hover(False))
        self.bind("<Button-1>", self._on_press)
        self.bind("<B1-Motion>", self._on_drag)
        self.bind("<ButtonRelease-1>", lambda _e: setattr(self, "_press", None))

    def set(self, first: str, last: str) -> None:
        self._first = float(first)
        self._last = float(last)
        self._redraw()

    def _set_hover(self, on: bool) -> None:
        self._hover = on
        self._redraw()

    def _span(self) -> float:
        return max(0.0, self._last - self._first)

    def _on_press(self, event: tk.Event) -> None:
        span = self._span()
        if span >= 0.995:
            return
        if self._orient == "vertical":
            pos = event.y / max(1, self.winfo_height())
            self._press = (event.y, self._first)
        else:
            pos = event.x / max(1, self.winfo_width())
            self._press = (event.x, self._first)
        if pos < self._first or pos > self._last:
            first = min(max(0.0, pos - span / 2), 1.0 - span)
            self._command("moveto", str(first))
            self._press = ((event.y if self._orient == "vertical" else event.x), first)

    def _on_drag(self, event: tk.Event) -> None:
        if self._press is None:
            return
        origin, first0 = self._press
        span = self._span()
        if self._orient == "vertical":
            delta = (event.y - origin) / max(1, self.winfo_height())
        else:
            delta = (event.x - origin) / max(1, self.winfo_width())
        first = min(max(0.0, first0 + delta), max(0.0, 1.0 - span))
        self._command("moveto", str(first))

    def _redraw(self) -> None:
        self.delete("all")
        span = self._span()
        if span >= 0.995:
            return
        color = "#8b97ab" if self._hover else "#c5cedb"
        if self._orient == "vertical":
            length = max(1, self.winfo_height())
            thickness = max(8, self.winfo_width())
            thumb = max(36, int(span * length))
            top = int(self._first * (length - thumb))
            _round_shape(self, 1, top + 1, thickness - 1, top + thumb - 1, 4, color)
        else:
            length = max(1, self.winfo_width())
            thickness = max(8, self.winfo_height())
            thumb = max(36, int(span * length))
            left = int(self._first * (length - thumb))
            _round_shape(self, left + 1, 1, left + thumb - 1, thickness - 1, 4, color)


class RoundBadge(tk.Canvas):
    """Pill label. configure() accepts text, bg, fg, and the old badge style names."""

    _STYLES = {
        "Live.TLabel": (RECORD, ON_COLOR),
        "Paused.TLabel": (PAUSED_BG, PAUSED_FG),
        "Ready.TLabel": (OK_BG, OK),
        "Badge.TLabel": (ACCENT_DIM, ACCENT),
    }

    def __init__(
        self,
        master: tk.Misc,
        text: str,
        fill: str,
        fg: str,
        canvas_bg: str | None = None,
        height: int = 26,
    ) -> None:
        page = canvas_bg or _widget_bg(master)
        super().__init__(master, height=height, width=48, bg=page, highlightthickness=0, bd=0)
        self._text = text
        self._fill = fill
        self._fg = fg
        self._height = height
        self._font = tkfont.Font(self, family=UI_FONT_SEMI, size=9)
        self._drawing = False
        self._redraw()

    def configure(self, cnf: object = None, **kw: object) -> object:  # type: ignore[override]
        if isinstance(cnf, dict):
            kw = {**cnf, **kw}
        changed = False
        if "text" in kw:
            self._text = str(kw.pop("text"))
            changed = True
        if "bg" in kw:
            self._fill = str(kw.pop("bg"))
            changed = True
        if "fg" in kw:
            self._fg = str(kw.pop("fg"))
            changed = True
        if "style" in kw:
            fill, fg = self._STYLES.get(str(kw.pop("style")), (self._fill, self._fg))
            self._fill, self._fg = fill, fg
            changed = True
        if changed:
            self._redraw()
        if kw:
            return tk.Canvas.configure(self, **kw)
        return None

    config = configure

    def _redraw(self) -> None:
        if self._drawing:
            return
        self._drawing = True
        try:
            self.delete("all")
            width = self._font.measure(self._text) + 28
            height = self._height
            tk.Canvas.configure(self, width=width, height=height)
            radius = height // 2
            _round_shape(self, 1, 1, width - 1, height - 1, radius, self._fill)
            self.create_text(width / 2, height / 2, text=self._text, font=self._font, fill=self._fg)
        finally:
            self._drawing = False


class PillBar(tk.Frame):
    def __init__(
        self,
        master: tk.Misc,
        variable: tk.StringVar,
        options: list[tuple[str, str, str]],
        command: object | None = None,
        bg: str = PANEL2,
    ) -> None:
        super().__init__(master, bg=bg)
        self._var = variable
        self._command = command
        self._buttons: list[tuple[str, RoundButton]] = []
        for value, label, icon in options:
            btn = RoundButton(
                self,
                text=label,
                icon=icon,
                kind="ghost",
                height=34,
                min_width=72,
                canvas_bg=bg,
                command=lambda v=value: self._pick(v),
            )
            btn.pack(side="left", padx=(0, 6), pady=6)
            self._buttons.append((value, btn))
        variable.trace_add("write", lambda *_: self._sync())
        self._sync()

    def _pick(self, value: str) -> None:
        if self._var.get() != value:
            self._var.set(value)
        else:
            self._sync()
        if self._command is not None:
            self._command()

    def _sync(self) -> None:
        current = self._var.get()
        for value, btn in self._buttons:
            btn.set_selected(value == current)


class NumberStepper(tk.Frame):
    def __init__(
        self,
        master: tk.Misc,
        variable: tk.StringVar,
        lo: int,
        hi: int,
        step: int,
        bg: str = PANEL2,
    ) -> None:
        super().__init__(master, bg=bg)
        self.var = variable
        self.lo = lo
        self.hi = hi
        self.step = step
        RoundButton(
            self, icon="minus", kind="ghost", height=34, min_width=34, canvas_bg=bg, command=lambda: self._bump(-1)
        ).pack(side="left")
        well = tk.Frame(self, bg=bg, width=84, height=34)
        well.pack(side="left", padx=6)
        well.pack_propagate(False)
        self._canvas = tk.Canvas(well, width=84, height=34, bg=bg, highlightthickness=0, bd=0)
        self._canvas.pack(fill="both", expand=True)
        self._entry = tk.Entry(
            self._canvas,
            textvariable=variable,
            relief="flat",
            justify="center",
            bg=PANEL,
            fg=FG,
            insertbackground=FG,
            font=(UI_FONT_SEMI, 12),
            width=5,
            highlightthickness=0,
            bd=0,
        )
        self._canvas.bind("<Configure>", self._paint)
        self._entry.bind("<FocusOut>", lambda _e: self._clamp())
        self._entry.bind("<Return>", lambda _e: self._clamp())
        RoundButton(
            self, icon="plus", kind="ghost", height=34, min_width=34, canvas_bg=bg, command=lambda: self._bump(1)
        ).pack(side="left")
        self._paint()

    def get(self) -> str:
        return self._entry.get()

    def _paint(self, _event: object | None = None) -> None:
        width = max(84, self._canvas.winfo_width())
        height = max(34, self._canvas.winfo_height())
        self._canvas.delete("all")
        _round_shape(self._canvas, 1, 1, width - 1, height - 1, height // 2, BORDER)
        _round_shape(self._canvas, 2, 2, width - 2, height - 2, max(1, height // 2 - 1), PANEL)
        self._entry.place(x=8, y=4, width=max(20, width - 16), height=max(16, height - 8))

    def _clamp(self) -> None:
        try:
            n = int(str(self.var.get()).strip())
        except ValueError:
            n = self.lo
        self.var.set(str(max(self.lo, min(self.hi, n))))

    def _bump(self, direction: int) -> None:
        try:
            n = int(str(self.var.get()).strip())
        except ValueError:
            n = self.lo
        self.var.set(str(max(self.lo, min(self.hi, n + direction * self.step))))


class RecorderApp(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title("Window Recorder  ·  H.266 / VVC")
        self.geometry("1080x800")
        self.minsize(920, 680)
        self.configure(bg=BG)

        self.hw: HardwareProfile | None = None
        self.windows: list[WindowInfo] = []
        self._by_id: dict[str, WindowInfo] = {}
        self.mics: list[AudioDevice] = []
        self.loopbacks: list[AudioDevice] = []
        self.session: CaptureSession | None = None
        self._tick_job: str | None = None
        self._timer_hold = False
        self.output_dir = _ROOT / "recordings"
        self._rec_by_id: dict[str, Path] = {}
        self._min_labels: dict[str, RoundBadge] = {}
        self._min_floors: dict[str, int] = {}
        self._setting_spins: dict[str, tuple[NumberStepper, tk.StringVar]] = {}
        self._tab_icons: dict[str, tk.Label] = {}

        install_icons(self)
        self._style()
        self._build()
        self.after_idle(self._maximize)
        self.after(50, self._boot)

    def _maximize(self) -> None:
        self.update_idletasks()
        try:
            self.state("zoomed")
            return
        except tk.TclError:
            pass
        try:
            self.attributes("-zoomed", True)
            return
        except tk.TclError:
            pass
        if sys.platform == "win32":
            try:
                import ctypes

                user32 = ctypes.windll.user32
                hwnd = user32.GetParent(self.winfo_id())
                if not hwnd:
                    hwnd = int(self.winfo_id())
                user32.ShowWindow(int(hwnd), 3)
                return
            except Exception:
                pass
        w = self.winfo_screenwidth()
        h = self.winfo_screenheight()
        self.geometry(f"{w}x{h}+0+0")

    def _card(self, parent: tk.Misc, title: str, icon: str = "") -> tk.Frame:
        outer = tk.Frame(parent, bg=PANEL, highlightthickness=1, highlightbackground=BORDER)
        outer.pack(fill="x", pady=(0, 14))
        body = tk.Frame(outer, bg=PANEL)
        body.pack(fill="x", padx=16, pady=14)
        head = tk.Frame(body, bg=PANEL)
        head.pack(anchor="w", pady=(0, 12))
        if icon:
            tk.Label(head, text=ico(icon), bg=PANEL, fg=ACCENT, font=(ICON_FONT, 14)).pack(side="left", padx=(0, 8))
        ttk.Label(head, text=title, style="Section.TLabel").pack(side="left")
        return body

    def _add_setting_row(
        self,
        grid: tk.Misc,
        row: int,
        title: str,
        var: tk.StringVar,
        lo: int,
        hi: int,
        step: int,
        min_ok: int,
        key: str,
        unit: str,
        icon: str = "",
    ) -> None:
        rowf = tk.Frame(grid, bg=PANEL2, highlightthickness=0)
        rowf.grid(row=row, column=0, sticky="ew", pady=4)
        if icon:
            tk.Label(rowf, text=ico(icon), bg=PANEL2, fg=ACCENT, font=(ICON_FONT, 12)).pack(
                side="left", padx=(12, 0), pady=8
            )
        tk.Label(
            rowf,
            text=title,
            bg=PANEL2,
            fg=MUTED,
            font=(UI_FONT, 9),
            width=11,
            anchor="w",
        ).pack(side="left", padx=(8, 4), pady=8)
        spin = NumberStepper(rowf, var, lo, hi, step, bg=PANEL2)
        spin.pack(side="left", pady=6)
        self._setting_spins[key] = (spin, var)
        tk.Label(rowf, text=unit, bg=PANEL2, fg=MUTED, font=(UI_FONT, 8), anchor="w").pack(
            side="left", padx=(6, 8)
        )
        tk.Frame(rowf, bg=PANEL2).pack(side="left", fill="x", expand=True)
        chip = RoundBadge(rowf, f"min {min_ok} {unit}".strip(), CHIP_BG, MUTED, canvas_bg=PANEL2, height=24)
        chip.pack(side="right", padx=(0, 10), pady=8)
        range_chip = RoundBadge(rowf, f"range {lo}–{hi} {unit}".strip(), CHIP_BG, MUTED, canvas_bg=PANEL2, height=24)
        range_chip.pack(side="right", padx=(0, 8), pady=8)
        self._min_labels[key] = chip
        self._min_floors[key] = min_ok

    def _style(self) -> None:
        style = ttk.Style(self)
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass
        style.configure(".", background=BG, foreground=FG, fieldbackground=PANEL, bordercolor=BORDER)
        style.configure("TFrame", background=BG)
        style.configure("Panel.TFrame", background=PANEL)
        style.configure("TLabel", background=BG, foreground=FG, font=(UI_FONT, 10))
        style.configure("Muted.TLabel", background=BG, foreground=MUTED, font=(UI_FONT, 9))
        style.configure("Warn.TLabel", background=BG, foreground=WARN, font=(UI_FONT, 8))
        style.configure("Hint.TLabel", background=BG, foreground=MUTED, font=(UI_FONT, 8))
        style.configure("Panel.TLabel", background=PANEL, foreground=FG)
        style.configure("Side.TFrame", background=PANEL)
        style.configure("Section.TLabel", background=PANEL, foreground=FG, font=(UI_FONT_SEMI, 13))
        style.configure("Side.TLabel", background=PANEL, foreground=MUTED, font=(UI_FONT, 9))
        style.configure("Side.TRadiobutton", background=PANEL2, foreground=FG, font=(UI_FONT, 10), padding=(8, 4))
        style.map(
            "Side.TRadiobutton",
            background=[("active", PANEL2)],
            foreground=[("selected", ACCENT), ("active", ACCENT)],
        )
        style.configure("Title.TLabel", background=BG, foreground=FG, font=(UI_FONT_SEMI, 22))
        style.configure("Timer.TLabel", background=BG, foreground=FG, font=(UI_FONT_SEMI, 22))
        for font_name, family, size in (
            ("TkDefaultFont", UI_FONT, 10),
            ("TkTextFont", UI_FONT, 10),
            ("TkMenuFont", UI_FONT, 10),
            ("TkHeadingFont", UI_FONT_SEMI, 11),
        ):
            try:
                tkfont.nametofont(font_name).configure(family=family, size=size)
            except tk.TclError:
                pass
        style.configure("Badge.TLabel", background=ACCENT_DIM, foreground=ACCENT, font=(UI_FONT_SEMI, 9), padding=(10, 4))
        style.configure("Live.TLabel", background=RECORD, foreground=ON_COLOR, font=(UI_FONT_SEMI, 9), padding=(10, 4))
        style.configure("Ready.TLabel", background=OK_BG, foreground=OK, font=(UI_FONT_SEMI, 9), padding=(10, 4))
        style.configure("Paused.TLabel", background=PAUSED_BG, foreground=PAUSED_FG, font=(UI_FONT_SEMI, 9), padding=(10, 4))
        style.configure("Field.TLabel", background=BG, foreground=MUTED, font=(UI_FONT, 9), width=12)
        style.configure("TRadiobutton", background=BG, foreground=FG, font=(UI_FONT, 10), padding=(6, 4))
        style.map("TRadiobutton", background=[("active", BG)], foreground=[("selected", ACCENT)])
        style.configure(
            "TButton",
            font=(UI_FONT, 10),
            padding=(12, 8),
            background=PANEL,
            foreground=FG,
            bordercolor=BORDER,
            lightcolor=BORDER,
            darkcolor=BORDER,
            relief="flat",
            borderwidth=1,
        )
        style.map(
            "TButton",
            background=[("active", ACCENT_DIM), ("pressed", ACCENT_DIM), ("disabled", PANEL2)],
            foreground=[("disabled", MUTED)],
        )
        style.configure(
            "Ghost.TButton",
            font=(UI_FONT, 10),
            padding=(12, 8),
            background=PANEL,
            foreground=FG,
            bordercolor=BORDER,
            lightcolor=BORDER,
            darkcolor=BORDER,
            relief="flat",
        )
        style.map(
            "Ghost.TButton",
            background=[("active", ACCENT_DIM), ("pressed", ACCENT_DIM), ("disabled", PANEL2)],
            foreground=[("disabled", MUTED)],
        )
        style.configure(
            "Accent.TButton",
            font=(UI_FONT_SEMI, 10),
            padding=(16, 8),
            background=ACCENT,
            foreground=ON_COLOR,
            bordercolor=ACCENT,
            lightcolor=ACCENT,
            darkcolor=ACCENT,
            relief="flat",
        )
        style.map(
            "Accent.TButton",
            background=[("active", ACCENT_HOVER), ("pressed", ACCENT_HOVER), ("disabled", BORDER)],
            foreground=[("disabled", MUTED), ("!disabled", ON_COLOR)],
            bordercolor=[("active", ACCENT_HOVER), ("disabled", BORDER)],
        )
        style.configure(
            "Record.TButton",
            font=(UI_FONT_SEMI, 12),
            padding=(18, 12),
            background=RECORD,
            foreground=ON_COLOR,
            bordercolor=RECORD,
            lightcolor=RECORD,
            darkcolor=RECORD,
            relief="flat",
        )
        style.map(
            "Record.TButton",
            background=[("!disabled", RECORD), ("active", RECORD_HOVER), ("disabled", RECORD_DIM)],
            foreground=[("disabled", MUTED), ("!disabled", ON_COLOR)],
        )
        style.configure(
            "Stop.TButton",
            font=(UI_FONT_SEMI, 12),
            padding=(18, 12),
            background=RECORD_HOVER,
            foreground=ON_COLOR,
            bordercolor=RECORD_HOVER,
            lightcolor=RECORD_HOVER,
            darkcolor=RECORD_HOVER,
            relief="flat",
        )
        style.map("Stop.TButton", background=[("disabled", RECORD_DIM)], foreground=[("disabled", MUTED)])
        style.configure(
            "TEntry",
            fieldbackground=PANEL,
            foreground=FG,
            bordercolor=BORDER,
            lightcolor=BORDER,
            darkcolor=BORDER,
            padding=6,
        )
        style.map("TEntry", bordercolor=[("focus", ACCENT)], lightcolor=[("focus", ACCENT)], darkcolor=[("focus", ACCENT)])
        style.configure("TCombobox", fieldbackground=PANEL, background=PANEL, foreground=FG, arrowcolor=MUTED, padding=4)
        style.map(
            "TCombobox",
            fieldbackground=[("readonly", PANEL), ("disabled", PANEL2)],
            foreground=[("readonly", FG), ("disabled", MUTED)],
            selectbackground=[("readonly", ACCENT_DIM)],
            selectforeground=[("readonly", FG)],
        )
        style.configure("TSpinbox", fieldbackground=PANEL, background=PANEL, foreground=FG, arrowcolor=MUTED, padding=4)
        style.map("TSpinbox", fieldbackground=[("!disabled", PANEL)], foreground=[("!disabled", FG)])
        style.configure("TLabelframe", background=BG, foreground=FG, bordercolor=BORDER, relief="solid", borderwidth=1)
        style.configure("TLabelframe.Label", background=BG, foreground=FG, font=(UI_FONT_SEMI, 11))
        style.configure(
            "Treeview",
            background=PANEL,
            foreground=FG,
            fieldbackground=PANEL,
            rowheight=28,
            font=(UI_FONT, 10),
            borderwidth=0,
        )
        style.configure(
            "Treeview.Heading",
            background=PANEL2,
            foreground=MUTED,
            font=(UI_FONT_SEMI, 9),
            relief="flat",
            padding=(6, 8),
        )
        style.map("Treeview", background=[("selected", ACCENT_DIM)], foreground=[("selected", FG)])
        style.map("Treeview.Heading", background=[("active", ACCENT_DIM)], foreground=[("active", FG)])
        style.configure("TScrollbar", background=PANEL2, troughcolor=BG, bordercolor=BG, arrowcolor=MUTED)
        self.option_add("*TCombobox*Listbox.background", PANEL)
        self.option_add("*TCombobox*Listbox.foreground", FG)
        self.option_add("*TCombobox*Listbox.selectBackground", ACCENT_DIM)
        self.option_add("*TCombobox*Listbox.selectForeground", FG)
        self.option_add("*TCombobox*Listbox.font", f"{UI_FONT} 10")
        style.configure("Horizontal.TProgressbar", background=ACCENT, troughcolor=PANEL2)

    def _build(self) -> None:
        header = tk.Frame(self, bg=BG)
        header.pack(fill="x", padx=18, pady=(16, 8))
        tk.Label(header, text=ico("video"), bg=BG, fg=ACCENT, font=(ICON_FONT, 20)).pack(side="left")
        ttk.Label(header, text="Window Recorder", style="Title.TLabel").pack(side="left", padx=(10, 0))
        RoundBadge(header, "H.266  ·  VVC  ·  Opus", ACCENT_DIM, ACCENT, canvas_bg=BG).pack(side="left", padx=(12, 0))
        self.state_badge = RoundBadge(header, "READY", OK_BG, OK, canvas_bg=BG)
        self.state_badge.pack(side="right")
        self.time_label = ttk.Label(header, text="00:00:00", style="Timer.TLabel")
        self.time_label.pack(side="right", padx=(0, 10))

        nav = tk.Frame(self, bg=BG)
        nav.pack(fill="x", padx=18, pady=(4, 0))
        self._tab_bar = tk.Frame(nav, bg=BG)
        self._tab_bar.pack(side="left")
        self._tab_labels: dict[str, tk.Label] = {}
        self._tab_rules: dict[str, tk.Frame] = {}
        tabs = (
            ("record", "Record", "record"),
            ("recs", "Recordings", "folder"),
            ("activity", "Activity", "history"),
            ("settings", "Settings", "settings"),
        )
        for key, title, icon_name in tabs:
            cell = tk.Frame(self._tab_bar, bg=BG, cursor="hand2")
            cell.pack(side="left", padx=(0, 18))
            row = tk.Frame(cell, bg=BG, cursor="hand2")
            row.pack()
            icon_lbl = tk.Label(
                row,
                text=ico(icon_name),
                bg=BG,
                fg=MUTED,
                font=(ICON_FONT, 13),
                cursor="hand2",
            )
            icon_lbl.pack(side="left", padx=(2, 6))
            lbl = tk.Label(
                row,
                text=title,
                bg=BG,
                fg=MUTED,
                font=(UI_FONT_SEMI, 13),
                cursor="hand2",
                pady=4,
            )
            lbl.pack(side="left")
            rule = tk.Frame(cell, bg=BG, height=3)
            rule.pack(fill="x", pady=(4, 0))
            for widget in (cell, row, icon_lbl, lbl, rule):
                widget.bind("<Button-1>", lambda _e, k=key: self._show_page(k))
                widget.bind("<Enter>", lambda _e, k=key: self._tab_hover(k, True))
                widget.bind("<Leave>", lambda _e, k=key: self._tab_hover(k, False))
            self._tab_labels[key] = lbl
            self._tab_icons[key] = icon_lbl
            self._tab_rules[key] = rule
        tk.Frame(self, bg=BORDER, height=1).pack(fill="x", padx=18, pady=(0, 8))

        self._page_host = ttk.Frame(self)
        self._page_host.pack(fill="both", expand=True, padx=18, pady=(0, 6))
        self.record_tab = ttk.Frame(self._page_host)
        self.recs_tab = ttk.Frame(self._page_host)
        self.activity_tab = ttk.Frame(self._page_host)
        self.settings_tab = ttk.Frame(self._page_host)
        self._pages = {
            "record": self.record_tab,
            "recs": self.recs_tab,
            "activity": self.activity_tab,
            "settings": self.settings_tab,
        }
        self._current_page = "record"

        body = ttk.Frame(self.record_tab)
        body.pack(fill="both", expand=True, padx=12, pady=(10, 0))

        left = ttk.LabelFrame(body, text="  Source  ")
        left.pack(side="left", fill="both", expand=True)

        btns = tk.Frame(left, bg=BG)
        btns.pack(fill="x", padx=10, pady=(10, 6))
        RoundButton(btns, text="Refresh", icon="refresh", command=self.refresh_windows, canvas_bg=BG).pack(side="left")
        RoundButton(
            btns, text="Choose window", icon="window", kind="accent", command=self._open_share_picker, canvas_bg=BG
        ).pack(side="left", padx=(8, 0))
        RoundButton(
            btns, text="Click on screen", icon="pointer", command=self._start_click_pick, canvas_bg=BG
        ).pack(side="left", padx=(8, 0))
        self.filter_var = tk.StringVar()
        self.filter_var.trace_add("write", lambda *_: self._apply_filter())
        filt = ttk.Entry(btns, textvariable=self.filter_var, width=18)
        filt.pack(side="right")
        tk.Label(btns, text=ico("search"), bg=BG, fg=MUTED, font=(ICON_FONT, 12)).pack(side="right", padx=(0, 6))

        ttk.Label(
            left,
            text="You can see the selected window and minimize or maximize it — recording continues either way. F9 starts or stops.",
            style="Hint.TLabel",
        ).pack(anchor="w", padx=10, pady=(0, 6))

        list_wrap = ttk.Frame(left)
        list_wrap.pack(fill="both", expand=True, padx=10, pady=(0, 10))
        cols = ("title", "app", "size", "state")
        self.win_list = ttk.Treeview(list_wrap, columns=cols, show="headings", selectmode="browse")
        self.win_list.heading("title", text="Window")
        self.win_list.heading("app", text="App")
        self.win_list.heading("size", text="Size")
        self.win_list.heading("state", text="State")
        self.win_list.column("title", width=280, anchor="w", stretch=True)
        self.win_list.column("app", width=110, anchor="w", stretch=False)
        self.win_list.column("size", width=88, anchor="center", stretch=False)
        self.win_list.column("state", width=92, anchor="w", stretch=False)
        scroll = SlimScroll(list_wrap, command=self.win_list.yview, canvas_bg=BG)
        self.win_list.configure(yscrollcommand=scroll.set)
        self.win_list.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y", padx=(6, 0))
        self.win_list.bind("<<TreeviewSelect>>", self._on_win_select)
        self.win_list.bind("<Double-Button-1>", lambda _e: self._open_share_picker())
        self.win_list.tag_configure("odd", background=PANEL)
        self.win_list.tag_configure("even", background=PANEL2)
        self.win_list.tag_configure("screen", foreground=ACCENT)

        actions = tk.Frame(self.record_tab, bg=BG)
        actions.pack(fill="x", padx=12, pady=(8, 0))
        self.record_btn = RoundButton(
            actions,
            text="Record    F9",
            icon="record",
            kind="record",
            height=48,
            min_width=220,
            command=self.toggle_record,
            canvas_bg=BG,
        )
        self.record_btn.pack(side="left", fill="x", expand=True)
        self.pause_btn = RoundButton(
            actions, text="Pause", icon="pause", height=48, min_width=120, command=self.toggle_pause, canvas_bg=BG
        )
        self.pause_btn.configure(state="disabled")
        self.pause_btn.pack(side="left", padx=(8, 0))

        self._build_activity_page()
        self._build_settings_page()

        rec_bar = tk.Frame(self.recs_tab, bg=BG)
        rec_bar.pack(fill="x", padx=12, pady=(12, 8))
        RoundButton(
            rec_bar,
            text="Refresh",
            icon="refresh",
            command=lambda: self.refresh_recordings(log=True),
            canvas_bg=BG,
        ).pack(side="left")
        self.play_btn = RoundButton(
            rec_bar, text="Play", icon="play", kind="accent", command=self._play_selected, canvas_bg=BG
        )
        self.play_btn.pack(side="left", padx=(8, 0))
        RoundButton(
            rec_bar, text="Open folder", icon="folderopen", command=self._open_recordings_folder, canvas_bg=BG
        ).pack(side="left", padx=(8, 0))
        ttk.Label(rec_bar, text="Double-click or Enter to play in MPC-HC", style="Hint.TLabel").pack(
            side="left", padx=12
        )
        self.mpc_status = ttk.Label(rec_bar, text="", style="Muted.TLabel")
        self.mpc_status.pack(side="right")

        self.rec_empty = ttk.Label(
            self.recs_tab,
            text="No recordings yet. Finish a take on the Record tab and it will land here.",
            style="Muted.TLabel",
        )

        rec_wrap = ttk.Frame(self.recs_tab)
        rec_wrap.pack(fill="both", expand=True, padx=12, pady=(0, 4))
        rec_cols = ("name", "size", "modified")
        self.rec_list = ttk.Treeview(rec_wrap, columns=rec_cols, show="headings", selectmode="browse")
        self.rec_list.heading("name", text="File")
        self.rec_list.heading("size", text="Size")
        self.rec_list.heading("modified", text="Modified")
        self.rec_list.column("name", width=460, anchor="w", stretch=True)
        self.rec_list.column("size", width=90, anchor="e", stretch=False)
        self.rec_list.column("modified", width=170, anchor="w", stretch=False)
        rec_scroll = SlimScroll(rec_wrap, command=self.rec_list.yview, canvas_bg=BG)
        self.rec_list.configure(yscrollcommand=rec_scroll.set)
        self.rec_list.pack(side="left", fill="both", expand=True)
        rec_scroll.pack(side="right", fill="y", padx=(6, 0))
        self.rec_info = ttk.Label(self.recs_tab, text="", style="Muted.TLabel")
        self.rec_info.pack(fill="x", padx=12, pady=(0, 10))
        self.rec_list.bind("<Double-Button-1>", self._on_recording_click)
        self.rec_list.bind("<Return>", self._on_recording_enter)
        self.rec_list.bind("<<TreeviewSelect>>", lambda _e: self._sync_play_btn())
        self.rec_list.tag_configure("odd", background=PANEL)
        self.rec_list.tag_configure("even", background=PANEL2)
        self.rec_list.tag_configure("fresh", foreground=OK)

        status = ttk.Frame(self)
        status.pack(fill="x", padx=18, pady=(0, 10))
        self.status_var = tk.StringVar(value="Ready")
        ttk.Label(status, textvariable=self.status_var, style="Muted.TLabel").pack(side="left")

        self.bind("<F9>", lambda _e: self.toggle_record())
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self._show_page("record")

    def _build_activity_page(self) -> None:
        head = tk.Frame(self.activity_tab, bg=BG)
        head.pack(fill="x", padx=12, pady=(14, 6))
        tk.Label(head, text=ico("history"), bg=BG, fg=ACCENT, font=(ICON_FONT, 16)).pack(side="left")
        ttk.Label(head, text="Activity", style="Title.TLabel").pack(side="left", padx=(8, 0))
        ttk.Label(
            self.activity_tab,
            text="Hardware checks, capture progress, and save messages.",
            style="Muted.TLabel",
        ).pack(anchor="w", padx=14, pady=(0, 8))
        log_wrap = tk.Frame(self.activity_tab, bg=BG)
        log_wrap.pack(fill="both", expand=True, padx=12, pady=(0, 12))
        self.log = tk.Text(
            log_wrap,
            bg=PANEL,
            fg=FG,
            relief="flat",
            font=(UI_FONT, 10),
            highlightbackground=BORDER,
            highlightcolor=ACCENT,
            highlightthickness=1,
            wrap="word",
            insertbackground=FG,
            padx=12,
            pady=10,
        )
        scroll = SlimScroll(log_wrap, command=self.log.yview, canvas_bg=BG)
        self.log.configure(yscrollcommand=scroll.set)
        self.log.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")
        self.log.configure(state="disabled")

    def _build_settings_page(self) -> None:
        host = tk.Frame(self.settings_tab, bg=BG)
        host.pack(fill="both", expand=True, padx=12, pady=(10, 10))
        canvas = tk.Canvas(host, bg=BG, highlightthickness=0, bd=0)
        scroll = SlimScroll(host, command=canvas.yview, canvas_bg=BG)
        inner = ttk.Frame(canvas)
        inner.bind("<Configure>", lambda _e: canvas.configure(scrollregion=canvas.bbox("all")))
        win = canvas.create_window((0, 0), window=inner, anchor="nw")

        def _stretch(event: tk.Event) -> None:
            canvas.itemconfigure(win, width=event.width)

        canvas.bind("<Configure>", _stretch)
        canvas.configure(yscrollcommand=scroll.set)
        canvas.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")

        ttk.Label(
            inner,
            text="These apply the next time you press Record. F9 still starts and stops from any tab.",
            style="Muted.TLabel",
        ).pack(anchor="w", padx=2, pady=(0, 12))

        cols = tk.Frame(inner, bg=BG)
        cols.pack(fill="x")
        left = tk.Frame(cols, bg=BG)
        left.pack(side="left", fill="both", expand=True, padx=(0, 8))
        right = tk.Frame(cols, bg=BG)
        right.pack(side="left", fill="both", expand=True, padx=(8, 0))

        capture = self._card(left, "Capture", "video")
        grid = tk.Frame(capture, bg=PANEL)
        grid.pack(fill="x")
        grid.columnconfigure(0, weight=1)

        self.fps_var = tk.StringVar(value=str(DEFAULT_FPS))
        self.audio_kbps_var = tk.StringVar(value=str(DEFAULT_AUDIO_KBPS))
        self.sample_rate_var = tk.StringVar(value=str(DEFAULT_SAMPLE_KHZ))
        self.resolution_var = tk.StringVar(value=DEFAULT_VIDEO_PRESET)
        self.quality_var = tk.StringVar(value=str(DEFAULT_QUALITY))

        self._add_setting_row(grid, 0, "FPS", self.fps_var, 1, 240, 1, 1, "fps", "fps", "clock")

        res_row = tk.Frame(grid, bg=PANEL2)
        res_row.grid(row=1, column=0, sticky="ew", pady=4)
        tk.Label(res_row, text=ico("video"), bg=PANEL2, fg=ACCENT, font=(ICON_FONT, 12)).pack(
            side="left", padx=(12, 0), pady=8
        )
        tk.Label(
            res_row,
            text="Resolution",
            bg=PANEL2,
            fg=MUTED,
            font=(UI_FONT, 9),
            width=11,
            anchor="w",
        ).pack(side="left", padx=(8, 4), pady=8)
        PillBar(
            res_row,
            self.resolution_var,
            [(value, value, "") for value in VIDEO_PRESETS],
            bg=PANEL2,
        ).pack(side="left", padx=(0, 8))

        self._add_setting_row(
            grid, 2, "Quality", self.quality_var, QUALITY_MIN, QUALITY_MAX, 5, MIN_USEFUL_QUALITY, "quality", "", "record"
        )

        audio = self._card(right, "Audio", "volume")
        agrid = tk.Frame(audio, bg=PANEL)
        agrid.pack(fill="x")
        agrid.columnconfigure(0, weight=1)
        self._add_setting_row(
            agrid, 0, "Bit rate", self.audio_kbps_var, AUDIO_KBPS_MIN, AUDIO_KBPS_MAX, 8, MIN_USEFUL_AUDIO_KBPS, "audio", "kb/s", "volume"
        )
        self._add_setting_row(
            agrid,
            1,
            "Sample rate",
            self.sample_rate_var,
            SAMPLE_KHZ_MIN,
            SAMPLE_KHZ_MAX,
            1,
            MIN_USEFUL_SAMPLE_KHZ,
            "sample",
            "kHz",
            "clock",
        )

        source_row = tk.Frame(audio, bg=PANEL)
        source_row.pack(fill="x", pady=(12, 0))
        tk.Label(source_row, text=ico("volume"), bg=PANEL, fg=ACCENT, font=(ICON_FONT, 12)).pack(side="left", padx=(2, 6))
        ttk.Label(source_row, text="Sound source", style="Side.TLabel").pack(side="left")
        self.audio_var = tk.StringVar(value="internal")
        PillBar(
            audio,
            self.audio_var,
            (("internal", "Internal", "volume"), ("external", "Mic", "mic"), ("both", "Both", "people")),
            command=self._audio_changed,
            bg=PANEL,
        ).pack(anchor="w", pady=(6, 6))

        mic_row = tk.Frame(audio, bg=PANEL2)
        mic_row.pack(fill="x", pady=3)
        tk.Label(mic_row, text="Mic", bg=PANEL2, fg=MUTED, font=(UI_FONT, 9), width=12, anchor="w").pack(
            side="left", padx=(10, 4), pady=8
        )
        self.mic_var = tk.StringVar()
        self.mic_combo = ttk.Combobox(mic_row, textvariable=self.mic_var, state="readonly")
        self.mic_combo.pack(side="left", fill="x", expand=True, padx=(0, 10), pady=8)

        lb_row = tk.Frame(audio, bg=PANEL2)
        lb_row.pack(fill="x", pady=3)
        tk.Label(lb_row, text="Loopback", bg=PANEL2, fg=MUTED, font=(UI_FONT, 9), width=12, anchor="w").pack(
            side="left", padx=(10, 4), pady=8
        )
        self.loop_var = tk.StringVar()
        self.loop_combo = ttk.Combobox(lb_row, textvariable=self.loop_var, state="readonly")
        self.loop_combo.pack(side="left", fill="x", expand=True, padx=(0, 10), pady=8)
        ttk.Label(
            audio,
            text="Opus in MP4 is always listed as 48 kHz in Windows Properties. Encoding still uses the kHz you set.",
            style="Side.TLabel",
        ).pack(anchor="w", padx=2, pady=(8, 0))

        out = self._card(inner, "Save to", "folder")
        path_row = tk.Frame(out, bg=PANEL2)
        path_row.pack(fill="x")
        self.dir_var = tk.StringVar(value=str(self.output_dir))
        ttk.Entry(path_row, textvariable=self.dir_var).pack(side="left", fill="x", expand=True, padx=(10, 6), pady=8)
        RoundButton(
            path_row, text="Browse", icon="folderopen", command=self._browse, canvas_bg=PANEL2, height=36
        ).pack(side="left", padx=(0, 10), pady=8)

        self.audio_kbps_var.trace_add("write", lambda *_: self._refresh_quality_hints())
        self.sample_rate_var.trace_add("write", lambda *_: self._refresh_quality_hints())
        self.fps_var.trace_add("write", lambda *_: self._refresh_quality_hints())
        self.quality_var.trace_add("write", lambda *_: self._refresh_quality_hints())

        self._bind_sidebar_scroll(canvas, inner)

    def _paint_tab(self, key: str, active: bool, hover: bool = False) -> None:
        fg = FG if active or hover else MUTED
        self._tab_labels[key].configure(fg=fg, font=(UI_FONT_SEMI, 13))
        self._tab_icons[key].configure(fg=ACCENT if active or hover else MUTED)
        self._tab_rules[key].configure(bg=ACCENT if active else BG)

    def _tab_hover(self, key: str, on: bool) -> None:
        if key == self._current_page:
            return
        self._paint_tab(key, False, hover=on)

    def _show_page(self, key: str) -> None:
        if key not in self._pages:
            return
        for name, page in self._pages.items():
            page.pack_forget()
            self._paint_tab(name, name == key)
        if self._current_page == "settings":
            self._commit_settings()
        self._pages[key].pack(fill="both", expand=True)
        prev = self._current_page
        self._current_page = key
        if key == "recs" and prev != "recs":
            sel = self.rec_list.selection()
            keep = self._rec_by_id.get(sel[0]) if sel else None
            self.refresh_recordings(select_path=keep)

    def _bind_sidebar_scroll(self, canvas: tk.Canvas, inner: ttk.Frame) -> None:
        def on_wheel(event: tk.Event) -> str:
            canvas.yview_scroll(int(-1 * (event.delta / 120)), "units")
            return "break"

        def bind_tree(widget: tk.Misc) -> None:
            widget.bind("<MouseWheel>", on_wheel)
            for child in widget.winfo_children():
                bind_tree(child)

        bind_tree(inner)
        canvas.bind("<MouseWheel>", on_wheel)

    def _set_live_badge(self, mode: str) -> None:
        if mode == "rec":
            self.state_badge.configure(text="REC", style="Live.TLabel")
        elif mode == "paused":
            self.state_badge.configure(text="PAUSED", style="Paused.TLabel")
        else:
            self.state_badge.configure(text="READY", style="Ready.TLabel")

    def _boot(self) -> None:
        self._log("Detecting hardware and FFmpeg...")
        try:
            self.hw = detect()
        except Exception as exc:
            self._log(f"Hardware detect failed: {exc}")
            self.hw = None
        if self.hw:
            self._log(self.hw.summary())
            if not self.hw.has_libvvenc:
                self._log("ERROR: " + (self.hw.vvenc_skip_reason or "libvvenc missing"))
            if not getattr(self.hw, "has_libopus", False):
                self._log("ERROR: FFmpeg has no libopus. Install the Gyan.FFmpeg full build.")
        self.refresh_windows()
        self.refresh_audio()
        self.refresh_recordings(log=True)
        mpc = find_mpc_hc()
        if mpc:
            self._log(f"MPC-HC: {mpc}")
            self.mpc_status.configure(text=mpc.name)
        else:
            self._log("MPC-HC not found. Re-run run.cmd to install clsid2.mpc-hc 2.8.2.")
            self.mpc_status.configure(text="MPC-HC missing")
        self.status_var.set("Ready  ·  F9 to record")

    def _log(self, msg: str) -> None:
        line = msg.rstrip()

        def _append() -> None:
            self.log.configure(state="normal")
            self.log.insert("end", line + "\n")
            self.log.see("end")
            self.log.configure(state="disabled")
            short = line if len(line) < 110 else line[:107] + "..."
            self.status_var.set(short)

        if threading.current_thread() is threading.main_thread():
            _append()
        else:
            self.after(0, _append)

    def refresh_windows(self) -> None:
        keep_hwnd = None
        sel = self.win_list.selection()
        if sel and sel[0] in self._by_id:
            keep_hwnd = self._by_id[sel[0]].hwnd
        try:
            self.windows = list_windows()
        except Exception as exc:
            self._log(f"Window list failed: {exc}")
            return
        self._apply_filter(prefer_hwnd=keep_hwnd)
        chrome = [w for w in self.windows if w.exe.lower() in ("chrome.exe", "msedge.exe")]
        self._log(
            f"Found {len(self.windows) - 1} windows"
            + (f", including {len(chrome)} Chrome/Edge" if chrome else ", no Chrome/Edge yet")
        )

    def _apply_filter(self, prefer_hwnd: int | None = None) -> None:
        if prefer_hwnd is None:
            sel = self.win_list.selection()
            if sel and sel[0] in self._by_id:
                prefer_hwnd = self._by_id[sel[0]].hwnd
        needle = (self.filter_var.get() if hasattr(self, "filter_var") else "").strip().lower()
        children = self.win_list.get_children()
        if children:
            self.win_list.delete(*children)
        self._by_id = {}
        first_id = None
        prefer_id = None
        row_i = 0
        for w in self.windows:
            if needle and needle not in w.search_blob():
                continue
            iid = "screen" if w.is_desktop else f"hwnd-{w.hwnd}"
            title = w.title.replace("\n", " ").strip() or "(untitled)"
            if w.is_desktop:
                title = "Entire screen"
            if len(title) > 90:
                title = title[:87] + "..."
            tags = ["even" if row_i % 2 else "odd"]
            if w.is_desktop:
                tags.append("screen")
            self.win_list.insert(
                "",
                "end",
                iid=iid,
                values=(
                    title,
                    w.exe or ("display" if w.is_desktop else ""),
                    f"{w.width}x{w.height}",
                    w.state_text() or ("display" if w.is_desktop else ""),
                ),
                tags=tuple(tags),
            )
            self._by_id[iid] = w
            row_i += 1
            if first_id is None:
                first_id = iid
            if prefer_hwnd is not None and w.hwnd == prefer_hwnd:
                prefer_id = iid
        pick = prefer_id or first_id
        if pick:
            self.win_list.selection_set(pick)
            self.win_list.see(pick)

    def _selected_window(self) -> WindowInfo | None:
        sel = self.win_list.selection()
        if not sel:
            return None
        return self._by_id.get(sel[0])

    def _on_win_select(self, _event: object | None = None) -> None:
        return

    def _select_window_info(self, info: WindowInfo) -> None:
        iid = "screen" if info.is_desktop else f"hwnd-{info.hwnd}"
        if iid not in self._by_id:
            if info.is_desktop:
                self.windows = [info] + [w for w in self.windows if not w.is_desktop]
            else:
                rest = [w for w in self.windows if not w.is_desktop]
                screen = next((w for w in self.windows if w.is_desktop), None)
                self.windows = ([screen] if screen else []) + [info] + [w for w in rest if w.hwnd != info.hwnd]
            self._apply_filter(prefer_hwnd=info.hwnd)
        else:
            self.win_list.selection_set(iid)
            self.win_list.focus(iid)
            self.win_list.see(iid)
        self._log(f"Target: {info.label()}")

    def _parse_int(self, var: tk.StringVar, fallback: int) -> int:
        try:
            return int(str(var.get()).strip())
        except ValueError:
            return fallback

    def _refresh_quality_hints(self) -> None:
        checks: list[tuple[str, tk.StringVar, int]] = [
            ("fps", self.fps_var, 1),
            ("quality", self.quality_var, DEFAULT_QUALITY),
            ("audio", self.audio_kbps_var, DEFAULT_AUDIO_KBPS),
            ("sample", self.sample_rate_var, DEFAULT_SAMPLE_KHZ),
        ]
        for key, var, fallback in checks:
            lbl = self._min_labels.get(key)
            if lbl is None:
                continue
            floor = self._min_floors.get(key, 0)
            n = self._parse_int(var, fallback)
            if n < floor:
                lbl.configure(bg=CHIP_WARN_BG, fg=WARN)
            else:
                lbl.configure(bg=CHIP_BG, fg=MUTED)

    def _commit_settings(self) -> None:
        if not self._setting_spins:
            return
        try:
            self.focus_set()
        except tk.TclError:
            pass
        self.update_idletasks()
        for spin, var in self._setting_spins.values():
            try:
                var.set(str(spin.get()).strip())
            except tk.TclError:
                pass
        self._refresh_quality_hints()

    def _read_int_setting(
        self,
        var: tk.StringVar,
        name: str,
        lo: int,
        hi: int,
        example: str,
    ) -> int | None:
        try:
            n = int(str(var.get()).strip())
        except ValueError:
            messagebox.showwarning("Recorder", f"{name} must be a whole number you type, e.g. {example}.")
            return None
        if n < lo or n > hi:
            messagebox.showwarning(
                "Recorder",
                f"{name} must be a whole number between {lo} and {hi}.",
            )
            return None
        return n

    def _read_rate_settings(self) -> tuple[int, int] | None:
        self._commit_settings()
        audio_kbps = self._read_int_setting(
            self.audio_kbps_var, "Audio kb/s", AUDIO_KBPS_MIN, AUDIO_KBPS_MAX, str(DEFAULT_AUDIO_KBPS)
        )
        if audio_kbps is None:
            return None
        sample_khz = self._read_int_setting(
            self.sample_rate_var, "Sample rate", SAMPLE_KHZ_MIN, SAMPLE_KHZ_MAX, str(DEFAULT_SAMPLE_KHZ)
        )
        if sample_khz is None:
            return None
        sample_rate = sample_khz_to_hz(sample_khz)
        if audio_kbps < MIN_USEFUL_AUDIO_KBPS:
            if not messagebox.askyesno(
                "Audio may be too thin",
                f"{audio_kbps} kb/s is below {MIN_USEFUL_AUDIO_KBPS} kb/s. "
                "Speech and UI sounds often become hard to use. Record anyway?",
            ):
                return None
        if sample_khz < MIN_USEFUL_SAMPLE_KHZ:
            if not messagebox.askyesno(
                "Sample rate may be too low",
                f"{sample_khz} kHz is below {MIN_USEFUL_SAMPLE_KHZ} kHz. "
                "Speech often sounds muffled. Record anyway?",
            ):
                return None
        return audio_kbps, sample_rate

    def _tk_hwnds(self, *widgets: tk.Misc) -> set[int]:
        ids: set[int] = set()
        for w in widgets:
            if w is None:
                continue
            try:
                ids.add(int(w.winfo_id()))
            except Exception:
                pass
            try:
                frame = w.wm_frame()
                if frame:
                    ids.add(int(str(frame), 16))
            except Exception:
                pass
        return ids

    def _open_share_picker(self) -> None:
        self.refresh_windows()
        dlg = tk.Toplevel(self)
        dlg.title("Choose what to record")
        dlg.configure(bg=BG)
        dlg.transient(self)
        dlg.attributes("-topmost", True)
        dlg.geometry("780x580")
        head = ttk.Frame(dlg)
        head.pack(fill="x", padx=16, pady=(14, 6))
        ttk.Label(head, text="Choose what to record", style="Title.TLabel").pack(side="left")
        ttk.Label(head, text="Same idea as sharing a screen in a call", style="Muted.TLabel").pack(
            side="left", padx=(12, 0)
        )

        wrap = ttk.Frame(dlg)
        wrap.pack(fill="both", expand=True, padx=16, pady=4)
        canvas = tk.Canvas(wrap, bg=BG, highlightthickness=0)
        scroll = SlimScroll(wrap, command=canvas.yview, canvas_bg=BG)
        host = ttk.Frame(canvas)
        host.bind("<Configure>", lambda _e: canvas.configure(scrollregion=canvas.bbox("all")))
        win_id = canvas.create_window((0, 0), window=host, anchor="nw")
        canvas.configure(yscrollcommand=scroll.set)

        def _stretch(_event: tk.Event) -> None:
            canvas.itemconfigure(win_id, width=canvas.winfo_width())

        canvas.bind("<Configure>", _stretch)
        canvas.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")

        status = ttk.Label(dlg, text="Loading thumbnails...", style="Muted.TLabel")
        status.pack(anchor="w", padx=16)

        bar = ttk.Frame(dlg)
        bar.pack(fill="x", padx=16, pady=(8, 14))
        RoundButton(
            bar, text="Click on screen", icon="pointer", kind="accent", command=lambda: click_screen(), canvas_bg=BG
        ).pack(side="left")
        RoundButton(bar, text="Cancel", icon="cancel", command=lambda: close(), canvas_bg=BG).pack(side="right")

        dlg._photos = []
        thumb_dir = Path(tempfile.mkdtemp(prefix="recorder-share-"))
        thumb_w, thumb_h = 216, 122
        closed = {"done": False}

        def on_wheel(event: tk.Event) -> None:
            canvas.yview_scroll(int(-1 * (event.delta / 120)), "units")

        canvas.bind("<Enter>", lambda _e: canvas.bind_all("<MouseWheel>", on_wheel))
        canvas.bind("<Leave>", lambda _e: canvas.unbind_all("<MouseWheel>"))

        def close() -> None:
            if closed["done"]:
                return
            closed["done"] = True
            try:
                canvas.unbind_all("<MouseWheel>")
            except tk.TclError:
                pass
            try:
                dlg.destroy()
            except tk.TclError:
                pass
            shutil.rmtree(thumb_dir, ignore_errors=True)

        def click_screen() -> None:
            close()
            self._start_click_pick()

        def choose(info: WindowInfo) -> None:
            close()
            self._select_window_info(info)

        def add_card(parent: ttk.Frame, col: int, row: int, info: WindowInfo, photo: tk.PhotoImage) -> None:
            title = info.title.replace("\n", " ").strip() or "(untitled)"
            if info.is_desktop:
                title = "Entire screen"
            if len(title) > 34:
                title = title[:31] + "..."
            sub = "display" if info.is_desktop else (info.exe or "")
            radius = 18
            pad = 8
            tile_w = photo.width() + pad * 2
            tile_h = photo.height() + pad * 2 + 46
            card = tk.Canvas(
                parent,
                width=tile_w,
                height=tile_h,
                bg=BG,
                highlightthickness=0,
                bd=0,
                cursor="hand2",
            )
            card.grid(row=row, column=col, padx=8, pady=8, sticky="n")
            title_font = tkfont.Font(card, family=UI_FONT_SEMI, size=9)
            sub_font = tkfont.Font(card, family=UI_FONT, size=8)

            def paint(border: str) -> None:
                card.delete("all")
                _round_shape(card, 1, 1, tile_w - 1, tile_h - 1, radius, border)
                _round_shape(card, 2, 2, tile_w - 2, tile_h - 2, radius - 1, PANEL)
                card.create_image(pad, pad, image=photo, anchor="nw")
                image_r = 14
                ix, iy = pad, pad
                iw, ih = photo.width(), photo.height()
                _cover_corner(card, ix + image_r, iy + image_r, image_r, "tl", PANEL)
                _cover_corner(card, ix + iw - image_r, iy + image_r, image_r, "tr", PANEL)
                _cover_corner(card, ix + image_r, iy + ih - image_r, image_r, "bl", PANEL)
                _cover_corner(card, ix + iw - image_r, iy + ih - image_r, image_r, "br", PANEL)
                text_y = pad + ih + 10
                card.create_text(tile_w / 2, text_y, text=title, font=title_font, fill=FG, anchor="n")
                card.create_text(tile_w / 2, text_y + 18, text=sub, font=sub_font, fill=MUTED, anchor="n")
                card.image = photo

            paint(BORDER)
            card.bind("<Button-1>", lambda _e, target=info: choose(target))
            card.bind("<Enter>", lambda _e: paint(ACCENT))
            card.bind("<Leave>", lambda _e: paint(BORDER))

        def fill(rows: list[tuple[WindowInfo, Path]]) -> None:
            if closed["done"] or not dlg.winfo_exists():
                shutil.rmtree(thumb_dir, ignore_errors=True)
                return
            for child in host.winfo_children():
                child.destroy()
            dlg._photos.clear()
            for i, (info, path) in enumerate(rows):
                try:
                    _round_ppm_file(path, 14, _hex_rgb(PANEL))
                    photo = tk.PhotoImage(file=str(path))
                except (tk.TclError, OSError):
                    continue
                dlg._photos.append(photo)
                add_card(host, i % 3, i // 3, info, photo)
            status.configure(text=f"{len(dlg._photos)} sources  -  click a thumbnail")

        def worker() -> None:
            packed: list[tuple[WindowInfo, Path]] = []
            sources = list(self.windows[:37])
            for i, info in enumerate(sources):
                if closed["done"]:
                    return
                screen = (info.x, info.y, info.width, info.height) if info.is_desktop else None
                ppm = grab_thumb_ppm(info.hwnd, thumb_w, thumb_h, screen)
                path = thumb_dir / f"{i}.ppm"
                try:
                    path.write_bytes(ppm)
                except OSError:
                    continue
                packed.append((info, path))
            self.after(0, lambda: fill(packed))

        dlg.protocol("WM_DELETE_WINDOW", close)
        threading.Thread(target=worker, daemon=True, name="share-thumbs").start()
        dlg.grab_set()
        dlg.wait_window()

    def _start_click_pick(self) -> None:
        self._log("Click the window to record. Esc cancels.")
        banner = tk.Toplevel(self)
        banner.overrideredirect(True)
        banner.attributes("-topmost", True)
        banner.configure(bg=ACCENT)
        ttk.Label(
            banner,
            text="  Click the window to record    Esc cancels  ",
            background=ACCENT,
            foreground=ON_COLOR,
            font=(UI_FONT_SEMI, 12),
        ).pack(padx=10, pady=10)
        banner.update_idletasks()
        bw = banner.winfo_width()
        sw = banner.winfo_screenwidth()
        banner.geometry(f"+{(sw - bw) // 2}+16")
        self._pick_banner = banner
        self.iconify()
        threading.Thread(target=self._click_pick_worker, daemon=True, name="click-pick").start()

    def _click_pick_worker(self) -> None:
        time.sleep(0.3)
        while left_button_down():
            if escape_pressed():
                self.after(0, self._cancel_click_pick)
                return
            time.sleep(0.02)
        while True:
            if escape_pressed():
                self.after(0, self._cancel_click_pick)
                return
            if left_button_down():
                x, y = cursor_pos()
                extra = getattr(self, "_pick_banner", None)
                exclude = self._tk_hwnds(self, extra) if extra else self._tk_hwnds(self)
                info = window_at_point(x, y, exclude)
                self.after(0, lambda i=info: self._finish_click_pick(i))
                return
            time.sleep(0.02)

    def _cancel_click_pick(self) -> None:
        banner = getattr(self, "_pick_banner", None)
        if banner is not None:
            try:
                banner.destroy()
            except tk.TclError:
                pass
            self._pick_banner = None
        self.deiconify()
        self.lift()
        self._log("Window pick cancelled")

    def _finish_click_pick(self, info: WindowInfo | None) -> None:
        banner = getattr(self, "_pick_banner", None)
        if banner is not None:
            try:
                banner.destroy()
            except tk.TclError:
                pass
            self._pick_banner = None
        self.deiconify()
        self.lift()
        if info is None:
            messagebox.showwarning("Pick window", "No window under the cursor.")
            return
        self._select_window_info(info)

    def toggle_pause(self) -> None:
        if not self.session:
            return
        self.session.set_paused(not self.session.paused)
        paused = self.session.paused
        self.pause_btn.configure(text="Resume" if paused else "Pause", icon="play" if paused else "pause")
        self._set_live_badge("paused" if paused else "rec")

    def refresh_audio(self) -> None:
        try:
            self.mics = list_microphones()
            self.loopbacks = list_loopbacks()
        except Exception as exc:
            self._log(f"Audio devices: {exc}")
            self.mics, self.loopbacks = [], []
            return
        self.mic_combo["values"] = [m.label() for m in self.mics]
        self.loop_combo["values"] = [d.label() for d in self.loopbacks]
        dm = default_microphone()
        dl = default_loopback()
        if dm:
            self.mic_var.set(dm.label())
        elif self.mics:
            self.mic_var.set(self.mics[0].label())
        if dl:
            self.loop_var.set(dl.label())
        elif self.loopbacks:
            self.loop_var.set(self.loopbacks[0].label())
        self._audio_changed()

    def _audio_changed(self) -> None:
        mode = self.audio_var.get()
        mic_state = "readonly" if mode in ("external", "both") else "disabled"
        loop_state = "readonly" if mode in ("internal", "both") else "disabled"
        self.mic_combo.configure(state=mic_state)
        self.loop_combo.configure(state=loop_state)

    def _browse(self) -> None:
        chosen = filedialog.askdirectory(initialdir=self.dir_var.get() or str(self.output_dir))
        if chosen:
            self.dir_var.set(chosen)
            self.refresh_recordings()

    def _open_recordings_folder(self) -> None:
        folder = Path(self.dir_var.get().strip() or self.output_dir)
        folder.mkdir(parents=True, exist_ok=True)
        try:
            os.startfile(folder)  # type: ignore[attr-defined]
        except AttributeError:
            messagebox.showinfo("Folder", str(folder))

    def _device_by_label(self, items: list[AudioDevice], label: str) -> AudioDevice | None:
        for item in items:
            if item.label() == label:
                return item
        return items[0] if items else None

    def _stop_timer(self) -> None:
        self._timer_hold = True
        if self._tick_job:
            try:
                self.after_cancel(self._tick_job)
            except tk.TclError:
                pass
            self._tick_job = None

    def toggle_record(self) -> None:
        if self.session:
            self._stop_timer()
            self.record_btn.configure(state="disabled", text="Saving")
            self.pause_btn.configure(state="disabled")
            threading.Thread(target=self._stop_worker, daemon=True).start()
            return
        self._start_record()

    def _start_record(self) -> None:
        if self.hw is None:
            messagebox.showerror("Recorder", "Hardware detection has not finished.")
            return
        if not self.hw.has_libvvenc:
            messagebox.showerror(
                "H.266 unavailable",
                self.hw.vvenc_skip_reason
                or "FFmpeg has no libvvenc. Install the Gyan.FFmpeg *full* build via run.cmd.",
            )
            return
        if not getattr(self.hw, "has_libopus", False):
            messagebox.showerror(
                "Opus unavailable",
                "FFmpeg has no libopus. Install the Gyan.FFmpeg *full* build via run.cmd.",
            )
            return
        window = self._selected_window()
        if window is None:
            messagebox.showwarning("Recorder", "Select a window (or Entire screen).")
            return
        try:
            self._commit_settings()
            fps = int(self.fps_var.get().strip())
        except ValueError:
            messagebox.showwarning("Recorder", "FPS must be a whole number you type, e.g. 5 or 30.")
            return
        if fps < 1 or fps > 240:
            messagebox.showwarning("Recorder", "FPS must be a whole number between 1 and 240.")
            return

        parsed = self._read_rate_settings()
        if parsed is None:
            return
        audio_kbps, sample_rate = parsed
        quality = self._read_int_setting(
            self.quality_var, "Quality", QUALITY_MIN, QUALITY_MAX, str(DEFAULT_QUALITY)
        )
        if quality is None:
            return
        if quality < MIN_USEFUL_QUALITY:
            if not messagebox.askyesno(
                "Quality may be too low",
                f"Quality {quality} is below {MIN_USEFUL_QUALITY}. "
                "On-screen text and UI often become hard to read. Record anyway?",
            ):
                return
        preset, _height = resolve_video_preset(self.resolution_var.get().strip())

        mode = self.audio_var.get()
        mic = self._device_by_label(self.mics, self.mic_var.get()) if mode in ("external", "both") else None
        loop = self._device_by_label(self.loopbacks, self.loop_var.get()) if mode in ("internal", "both") else None
        if mode in ("external", "both") and mic is None:
            messagebox.showerror("Recorder", "No microphone selected.")
            return
        if mode in ("internal", "both") and loop is None:
            messagebox.showerror(
                "Recorder",
                "No WASAPI loopback device. Internal (speaker) audio cannot be captured on this machine.",
            )
            return

        out_dir = Path(self.dir_var.get().strip() or self.output_dir)
        cfg = RecordConfig(
            window=window,
            fps=fps,
            audio_mode=mode,  # type: ignore[arg-type]
            output=default_output_path(out_dir, window),
            microphone=mic,
            loopback=loop,
            audio_kbps=clamp_audio_kbps(audio_kbps),
            sample_rate=clamp_sample_rate(sample_rate),
            video_preset=preset,
            video_quality=quality,
        )
        self.session = CaptureSession(cfg, self.hw, on_log=self._log)
        try:
            self.session.start()
        except Exception as exc:
            self.session = None
            messagebox.showerror("Record failed", str(exc))
            return
        self.record_btn.configure(text="Stop    F9", style="Stop.TButton", state="normal")
        self.pause_btn.configure(state="normal", text="Pause")
        self._set_live_badge("rec")
        try:
            self.win_list.configure(selectmode="none")
        except tk.TclError:
            pass
        self._timer_hold = False
        self.time_label.configure(text="00:00:00")
        self._tick()
        self._log(
            f"Recording {window.label()} @ {fps} fps, {preset}, quality {quality}, "
            f"Opus {audio_kbps} kb/s {snap_opus_rate(sample_rate) // 1000} kHz -> {cfg.output.name}"
        )

    def _tick(self) -> None:
        if self._timer_hold or not self.session:
            return
        sec = int(self.session.elapsed())
        h, rem = divmod(sec, 3600)
        m, s = divmod(rem, 60)
        self.time_label.configure(text=f"{h:02d}:{m:02d}:{s:02d}")
        if self.session.paused:
            self._set_live_badge("paused")
        else:
            self._set_live_badge("rec")
        self._tick_job = self.after(250, self._tick)

    def _stop_worker(self) -> None:
        session = self.session
        path = None
        err = None
        if session:
            try:
                path = session.stop()
            except RecorderError as exc:
                err = str(exc)
            except Exception:
                err = traceback.format_exc()
        self.after(0, lambda: self._after_stop(path, err))

    def _after_stop(self, path: Path | None, err: str | None) -> None:
        if self._tick_job:
            self.after_cancel(self._tick_job)
            self._tick_job = None
        self.session = None
        self.record_btn.configure(text="Record    F9", style="Record.TButton", state="normal")
        self.pause_btn.configure(state="disabled", text="Pause")
        self._set_live_badge("ready")
        try:
            self.win_list.configure(selectmode="browse")
        except tk.TclError:
            pass
        if err:
            messagebox.showerror("Recording finished with errors", err)
            self.refresh_recordings(log=True)
            return
        self.refresh_recordings(log=True, select_path=path)
        self._show_page("recs")
        if path:
            self._log(f"Saved {path.name}")
            self.status_var.set(f"Saved  ·  {path.name}")

    def refresh_recordings(self, log: bool = False, select_path: Path | None = None) -> None:
        folder = Path(self.dir_var.get().strip() or self.output_dir)
        files = list_recordings(folder)
        keep = select_path
        if keep is None:
            sel = self.rec_list.selection()
            if sel:
                keep = self._rec_by_id.get(sel[0])
        children = self.rec_list.get_children()
        if children:
            self.rec_list.delete(*children)
        self._rec_by_id = {}
        pick = None
        keep_res = None
        if keep is not None:
            try:
                keep_res = keep.resolve()
            except OSError:
                keep_res = keep
        for i, path in enumerate(files):
            iid = f"rec-{i}"
            try:
                st = path.stat()
                size = format_size(st.st_size)
                modified = datetime.fromtimestamp(st.st_mtime).strftime("%Y-%m-%d %H:%M:%S")
            except OSError:
                size, modified = "?", ""
            tags = ["even" if i % 2 else "odd"]
            try:
                resolved = path.resolve()
            except OSError:
                resolved = path
            if keep_res is not None and resolved == keep_res:
                tags.append("fresh")
                pick = iid
            self.rec_list.insert("", "end", iid=iid, values=(path.name, size, modified), tags=tuple(tags))
            self._rec_by_id[iid] = path
        if pick:
            self.rec_list.selection_set(pick)
            self.rec_list.focus(pick)
            self.rec_list.see(pick)
        elif files:
            first = self.rec_list.get_children()
            if first:
                self.rec_list.selection_set(first[0])
        if files:
            try:
                self.rec_empty.pack_forget()
            except tk.TclError:
                pass
        else:
            self.rec_empty.pack(anchor="w", padx=16, pady=(0, 8))
        self._sync_play_btn()
        if log:
            self._log(f"Recordings: {len(files)} file(s) in {folder}")

    def _sync_play_btn(self) -> None:
        if getattr(self, "play_btn", None) is None:
            return
        state = "normal" if self.rec_list.selection() else "disabled"
        self.play_btn.configure(state=state)
        self._update_rec_info()

    def _update_rec_info(self) -> None:
        if getattr(self, "rec_info", None) is None:
            return
        sel = self.rec_list.selection()
        path = self._rec_by_id.get(sel[0]) if sel else None
        if path is None or self.hw is None or not path.is_file():
            self.rec_info.configure(text="")
            return
        summary = probe_summary(self.hw, path)
        self.rec_info.configure(text=summary or str(path))

    def _play_selected(self) -> None:
        sel = self.rec_list.selection()
        if sel:
            self._play_recording(sel[0])

    def _on_recording_click(self, event: tk.Event) -> None:
        row = self.rec_list.identify_row(event.y)
        if not row:
            return
        self.rec_list.selection_set(row)
        self._play_recording(row)

    def _on_recording_enter(self, _event: tk.Event) -> None:
        sel = self.rec_list.selection()
        if sel:
            self._play_recording(sel[0])

    def _play_recording(self, iid: str) -> None:
        path = self._rec_by_id.get(iid)
        if path is None:
            return
        try:
            play_with_mpc(path)
            self._log(f"Playing in MPC-HC: {path.name}")
        except Exception as exc:
            messagebox.showerror("Playback", str(exc))

    def _on_close(self) -> None:
        if self.session:
            if not messagebox.askyesno("Recording", "Stop recording and exit?"):
                return
            try:
                self.session.stop()
            except Exception:
                pass
        self.destroy()


def main() -> int:
    app = RecorderApp()
    app.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
