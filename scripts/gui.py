#!/usr/bin/env python3
"""Tkinter GUI: pick a window, FPS, internal/mic/both audio, record H.266/VVC."""

from __future__ import annotations

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
from tkinter import filedialog, messagebox, ttk

from audio import AudioDevice, default_loopback, default_microphone, list_loopbacks, list_microphones
from capture import (
    AUDIO_KBPS_MAX,
    AUDIO_KBPS_MIN,
    CaptureSession,
    DEFAULT_AUDIO_KBPS,
    DEFAULT_SAMPLE_KHZ,
    DEFAULT_TOTAL_KBPS,
    DEFAULT_VIDEO_KBPS,
    MIN_USEFUL_AUDIO_KBPS,
    MIN_USEFUL_SAMPLE_KHZ,
    MIN_USEFUL_TOTAL_KBPS,
    MIN_USEFUL_VIDEO_KBPS,
    RecordConfig,
    RecorderError,
    SAMPLE_KHZ_MAX,
    SAMPLE_KHZ_MIN,
    TOTAL_KBPS_MAX,
    TOTAL_KBPS_MIN,
    VIDEO_KBPS_MAX,
    VIDEO_KBPS_MIN,
    clamp_audio_kbps,
    clamp_sample_rate,
    clamp_video_kbps,
    default_output_path,
    probe_summary,
    sample_khz_to_hz,
    snap_opus_rate,
)
from hwnd_grab import grab_thumb_ppm
from hw_detect import HardwareProfile, detect
from player import find_mpc_hc, format_size, list_recordings, play_with_mpc
from windows import WindowInfo, cursor_pos, escape_pressed, left_button_down, list_windows, window_at_point

BG = "#12141a"
PANEL = "#1c2029"
PANEL2 = "#252a36"
FG = "#f1f3f7"
MUTED = "#8b93a7"
ACCENT = "#4c8dff"
ACCENT_DIM = "#2a4f8a"
RECORD = "#e5484d"
RECORD_DIM = "#8f2d32"
OK = "#3dd68c"
BORDER = "#343b4a"
WARN = "#f0c14b"
CHIP_BG = "#171b24"
CHIP_WARN_BG = "#3a3218"


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
        self.output_dir = _ROOT / "recordings"
        self._rec_by_id: dict[str, Path] = {}
        self._syncing_rates = False
        self._min_labels: dict[str, tk.Label] = {}
        self._min_floors: dict[str, int] = {}
        self._setting_spins: dict[str, tuple[ttk.Spinbox, tk.StringVar]] = {}
        self._rate_source = "av"

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

    def _card(self, parent: tk.Misc, title: str) -> tk.Frame:
        outer = tk.Frame(parent, bg=PANEL, highlightthickness=1, highlightbackground=BORDER)
        outer.pack(fill="x", pady=(0, 14))
        body = tk.Frame(outer, bg=PANEL)
        body.pack(fill="x", padx=16, pady=14)
        ttk.Label(body, text=title.upper(), style="Section.TLabel").pack(anchor="w", pady=(0, 10))
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
    ) -> None:
        rowf = tk.Frame(grid, bg=PANEL2, highlightthickness=0)
        rowf.grid(row=row, column=0, sticky="ew", pady=3)
        tk.Label(
            rowf,
            text=title,
            bg=PANEL2,
            fg=MUTED,
            font=("Segoe UI", 9),
            width=12,
            anchor="w",
        ).pack(side="left", padx=(10, 4), pady=8)
        spin = ttk.Spinbox(rowf, textvariable=var, from_=lo, to=hi, increment=step, width=8)
        spin.pack(side="left")
        self._setting_spins[key] = (spin, var)
        spin.bind("<FocusOut>", lambda _e, s=spin, v=var: v.set(str(s.get()).strip()), add="+")
        tk.Label(rowf, text=unit, bg=PANEL2, fg=MUTED, font=("Segoe UI", 8), anchor="w").pack(
            side="left", padx=(6, 8)
        )
        tk.Frame(rowf, bg=PANEL2).pack(side="left", fill="x", expand=True)
        chip = tk.Label(
            rowf,
            text=f"min {min_ok} {unit}",
            bg=CHIP_BG,
            fg=MUTED,
            font=("Segoe UI", 8),
            padx=8,
            pady=2,
        )
        chip.pack(side="right", padx=(0, 10), pady=8)
        range_chip = tk.Label(
            rowf,
            text=f"range {lo}–{hi} {unit}",
            bg=CHIP_BG,
            fg=MUTED,
            font=("Segoe UI", 8),
            padx=8,
            pady=2,
        )
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
        style.configure("TLabel", background=BG, foreground=FG, font=("Segoe UI", 10))
        style.configure("Muted.TLabel", background=BG, foreground=MUTED, font=("Segoe UI", 9))
        style.configure("Warn.TLabel", background=BG, foreground=WARN, font=("Segoe UI", 8))
        style.configure("Hint.TLabel", background=BG, foreground=MUTED, font=("Segoe UI", 8))
        style.configure("Panel.TLabel", background=PANEL, foreground=FG)
        style.configure("Side.TFrame", background=PANEL)
        style.configure("Section.TLabel", background=PANEL, foreground=MUTED, font=("Segoe UI Semibold", 8))
        style.configure("Side.TLabel", background=PANEL, foreground=MUTED, font=("Segoe UI", 9))
        style.configure("Side.TRadiobutton", background=PANEL2, foreground=FG, font=("Segoe UI", 10), padding=2)
        style.map("Side.TRadiobutton", background=[("active", PANEL2)], foreground=[("selected", ACCENT)])
        style.configure("Title.TLabel", background=BG, foreground=FG, font=("Segoe UI Semibold", 18))
        style.configure("Timer.TLabel", background=BG, foreground=FG, font=("Cascadia Mono", 20, "bold"))
        style.configure("Badge.TLabel", background=PANEL2, foreground=MUTED, font=("Segoe UI", 8), padding=(8, 3))
        style.configure("Live.TLabel", background=RECORD, foreground="#fff", font=("Segoe UI Semibold", 8), padding=(8, 3))
        style.configure("Ready.TLabel", background=PANEL2, foreground=OK, font=("Segoe UI Semibold", 8), padding=(8, 3))
        style.configure("Paused.TLabel", background="#6b5420", foreground="#fff", font=("Segoe UI Semibold", 8), padding=(8, 3))
        style.configure("Field.TLabel", background=BG, foreground=MUTED, font=("Segoe UI", 9), width=12)
        style.configure("TRadiobutton", background=BG, foreground=FG, font=("Segoe UI", 10), padding=2)
        style.map("TRadiobutton", background=[("active", BG)], foreground=[("selected", ACCENT)])
        style.configure("TButton", font=("Segoe UI", 10), padding=(10, 6), background=PANEL2, foreground=FG)
        style.map("TButton", background=[("active", ACCENT_DIM), ("pressed", ACCENT)], foreground=[("active", "#fff")])
        style.configure("Ghost.TButton", font=("Segoe UI", 9), padding=(8, 5), background=PANEL2, foreground=FG)
        style.configure("Accent.TButton", font=("Segoe UI Semibold", 10), padding=(14, 7), background=ACCENT, foreground="#fff")
        style.map("Accent.TButton", background=[("active", "#6aa1ff"), ("disabled", BORDER)])
        style.configure("Record.TButton", font=("Segoe UI Semibold", 12), padding=(18, 10), background=RECORD, foreground="#fff")
        style.map("Record.TButton", background=[("!disabled", RECORD), ("active", "#ff6b6f"), ("disabled", RECORD_DIM)])
        style.configure("Stop.TButton", font=("Segoe UI Semibold", 12), padding=(18, 10), background="#c43c40", foreground="#fff")
        style.configure("TCombobox", fieldbackground=PANEL, background=PANEL, foreground=FG, arrowcolor=FG)
        style.map(
            "TCombobox",
            fieldbackground=[("readonly", PANEL), ("disabled", PANEL2)],
            foreground=[("readonly", FG), ("disabled", MUTED)],
            selectbackground=[("readonly", ACCENT)],
            selectforeground=[("readonly", "#fff")],
        )
        style.configure("TSpinbox", fieldbackground=PANEL, background=PANEL, foreground=FG, arrowcolor=FG)
        style.map("TSpinbox", fieldbackground=[("!disabled", PANEL)], foreground=[("!disabled", FG)])
        style.configure("TLabelframe", background=BG, foreground=FG, bordercolor=BORDER, relief="flat")
        style.configure("TLabelframe.Label", background=BG, foreground=MUTED, font=("Segoe UI Semibold", 9))
        style.configure(
            "Treeview",
            background=PANEL,
            foreground=FG,
            fieldbackground=PANEL,
            rowheight=28,
            font=("Segoe UI", 10),
            borderwidth=0,
        )
        style.configure(
            "Treeview.Heading",
            background=PANEL2,
            foreground=MUTED,
            font=("Segoe UI Semibold", 9),
            relief="flat",
            padding=(6, 8),
        )
        style.map("Treeview", background=[("selected", ACCENT)], foreground=[("selected", "#fff")])
        style.map("Treeview.Heading", background=[("active", ACCENT_DIM)])
        style.configure("TScrollbar", background=PANEL2, troughcolor=BG, bordercolor=BG, arrowcolor=MUTED)
        style.configure("Horizontal.TProgressbar", background=ACCENT, troughcolor=PANEL2)

    def _build(self) -> None:
        header = ttk.Frame(self)
        header.pack(fill="x", padx=18, pady=(14, 8))
        ttk.Label(header, text="Window Recorder", style="Title.TLabel").pack(side="left")
        ttk.Label(header, text="H.266 / VVC  ·  Opus", style="Badge.TLabel").pack(side="left", padx=(12, 0))
        self.state_badge = ttk.Label(header, text="READY", style="Ready.TLabel")
        self.state_badge.pack(side="right")
        self.time_label = ttk.Label(header, text="00:00:00", style="Timer.TLabel")
        self.time_label.pack(side="right", padx=(0, 10))

        nav = tk.Frame(self, bg=BG)
        nav.pack(fill="x", padx=18, pady=(4, 0))
        self._tab_bar = tk.Frame(nav, bg=BG)
        self._tab_bar.pack(side="left")
        self._tab_labels: dict[str, tk.Label] = {}
        self._tab_rules: dict[str, tk.Frame] = {}
        for key, title in (("record", "Record"), ("recs", "Recordings"), ("settings", "Settings")):
            cell = tk.Frame(self._tab_bar, bg=BG)
            cell.pack(side="left", padx=(0, 22))
            lbl = tk.Label(
                cell,
                text=title,
                bg=BG,
                fg=MUTED,
                font=("Segoe UI Semibold", 13),
                cursor="hand2",
                padx=2,
                pady=4,
            )
            lbl.pack()
            rule = tk.Frame(cell, bg=BG, height=2)
            rule.pack(fill="x")
            lbl.bind("<Button-1>", lambda _e, k=key: self._show_page(k))
            rule.bind("<Button-1>", lambda _e, k=key: self._show_page(k))
            lbl.bind("<Enter>", lambda _e, k=key: self._tab_hover(k, True))
            lbl.bind("<Leave>", lambda _e, k=key: self._tab_hover(k, False))
            self._tab_labels[key] = lbl
            self._tab_rules[key] = rule
        tk.Frame(self, bg=BORDER, height=1).pack(fill="x", padx=18, pady=(0, 8))

        self._page_host = ttk.Frame(self)
        self._page_host.pack(fill="both", expand=True, padx=18, pady=(0, 6))
        self.record_tab = ttk.Frame(self._page_host)
        self.recs_tab = ttk.Frame(self._page_host)
        self.settings_tab = ttk.Frame(self._page_host)
        self._pages = {"record": self.record_tab, "recs": self.recs_tab, "settings": self.settings_tab}
        self._current_page = "record"

        body = ttk.Frame(self.record_tab)
        body.pack(fill="both", expand=True, padx=12, pady=(10, 0))

        left = ttk.LabelFrame(body, text="  Source  ")
        left.pack(side="left", fill="both", expand=True)

        btns = ttk.Frame(left)
        btns.pack(fill="x", padx=10, pady=(10, 6))
        ttk.Button(btns, text="Refresh", style="Ghost.TButton", command=self.refresh_windows).pack(side="left")
        ttk.Button(
            btns, text="Choose window", style="Accent.TButton", command=self._open_share_picker
        ).pack(side="left", padx=(8, 0))
        ttk.Button(
            btns, text="Click on screen", style="Ghost.TButton", command=self._start_click_pick
        ).pack(side="left", padx=(8, 0))
        self.filter_var = tk.StringVar()
        self.filter_var.trace_add("write", lambda *_: self._apply_filter())
        filt = ttk.Entry(btns, textvariable=self.filter_var, width=18)
        filt.pack(side="right")
        ttk.Label(btns, text="Search", style="Muted.TLabel").pack(side="right", padx=(0, 6))

        ttk.Label(
            left,
            text="The selected window stays on screen. You can minimize or maximize it anytime — recording continues. F9 starts or stops.",
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
        scroll = ttk.Scrollbar(list_wrap, command=self.win_list.yview)
        self.win_list.configure(yscrollcommand=scroll.set)
        self.win_list.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")
        self.win_list.bind("<<TreeviewSelect>>", self._on_win_select)
        self.win_list.bind("<Double-Button-1>", lambda _e: self._open_share_picker())
        self.win_list.tag_configure("odd", background=PANEL)
        self.win_list.tag_configure("even", background=PANEL2)
        self.win_list.tag_configure("screen", foreground=ACCENT)

        actions = tk.Frame(self.record_tab, bg=BG)
        actions.pack(fill="x", padx=12, pady=(8, 0))
        self.record_btn = ttk.Button(
            actions, text="●   Record    F9", style="Record.TButton", command=self.toggle_record
        )
        self.record_btn.pack(side="left", fill="x", expand=True)
        self.pause_btn = ttk.Button(actions, text="Pause", command=self.toggle_pause, state="disabled")
        self.pause_btn.pack(side="left", padx=(8, 0), ipadx=18)

        self._build_settings_page()

        log_frame = ttk.LabelFrame(self.record_tab, text="  Activity  ")
        log_frame.pack(fill="x", padx=12, pady=(8, 10))
        self.log = tk.Text(
            log_frame,
            height=5,
            bg=PANEL,
            fg=FG,
            relief="flat",
            highlightthickness=0,
            font=("Cascadia Mono", 9),
            wrap="word",
            insertbackground=FG,
            padx=8,
            pady=6,
        )
        self.log.pack(fill="x", padx=4, pady=4)
        self.log.configure(state="disabled")

        rec_bar = ttk.Frame(self.recs_tab)
        rec_bar.pack(fill="x", padx=12, pady=(12, 8))
        ttk.Button(rec_bar, text="Refresh", style="Ghost.TButton", command=lambda: self.refresh_recordings(log=True)).pack(
            side="left"
        )
        self.play_btn = ttk.Button(rec_bar, text="Play", style="Accent.TButton", command=self._play_selected)
        self.play_btn.pack(side="left", padx=(8, 0))
        ttk.Button(rec_bar, text="Open folder", style="Ghost.TButton", command=self._open_recordings_folder).pack(
            side="left", padx=(8, 0)
        )
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
        rec_scroll = ttk.Scrollbar(rec_wrap, command=self.rec_list.yview)
        self.rec_list.configure(yscrollcommand=rec_scroll.set)
        self.rec_list.pack(side="left", fill="both", expand=True)
        rec_scroll.pack(side="right", fill="y")
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

    def _build_settings_page(self) -> None:
        host = tk.Frame(self.settings_tab, bg=BG)
        host.pack(fill="both", expand=True, padx=12, pady=(10, 10))
        canvas = tk.Canvas(host, bg=BG, highlightthickness=0, bd=0)
        scroll = ttk.Scrollbar(host, orient="vertical", command=canvas.yview)
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

        capture = self._card(left, "Capture")
        grid = tk.Frame(capture, bg=PANEL)
        grid.pack(fill="x")
        grid.columnconfigure(0, weight=1)

        self.fps_var = tk.StringVar(value="30")
        self.audio_kbps_var = tk.StringVar(value=str(DEFAULT_AUDIO_KBPS))
        self.sample_rate_var = tk.StringVar(value=str(DEFAULT_SAMPLE_KHZ))
        self.video_kbps_var = tk.StringVar(value=str(DEFAULT_VIDEO_KBPS))
        self.total_kbps_var = tk.StringVar(value=str(DEFAULT_TOTAL_KBPS))

        self._add_setting_row(grid, 0, "FPS", self.fps_var, 1, 240, 1, 1, "fps", "fps")
        self._add_setting_row(
            grid, 1, "Data rate", self.video_kbps_var, VIDEO_KBPS_MIN, VIDEO_KBPS_MAX, 100, MIN_USEFUL_VIDEO_KBPS, "video", "kb/s"
        )
        self._add_setting_row(
            grid, 2, "Total", self.total_kbps_var, TOTAL_KBPS_MIN, TOTAL_KBPS_MAX, 100, MIN_USEFUL_TOTAL_KBPS, "total", "kb/s"
        )

        audio = self._card(right, "Audio")
        agrid = tk.Frame(audio, bg=PANEL)
        agrid.pack(fill="x")
        agrid.columnconfigure(0, weight=1)
        self._add_setting_row(
            agrid, 0, "Bit rate", self.audio_kbps_var, AUDIO_KBPS_MIN, AUDIO_KBPS_MAX, 8, MIN_USEFUL_AUDIO_KBPS, "audio", "kb/s"
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
        )

        ttk.Label(audio, text="Sound source", style="Side.TLabel").pack(anchor="w", padx=2, pady=(12, 6))
        self.audio_var = tk.StringVar(value="both")
        audio_row = tk.Frame(audio, bg=PANEL2)
        audio_row.pack(fill="x", pady=(0, 6))
        for value, label in (("internal", "Internal"), ("external", "Mic"), ("both", "Both")):
            ttk.Radiobutton(
                audio_row,
                text=label,
                value=value,
                variable=self.audio_var,
                command=self._audio_changed,
                style="Side.TRadiobutton",
            ).pack(side="left", padx=8, pady=8)

        mic_row = tk.Frame(audio, bg=PANEL2)
        mic_row.pack(fill="x", pady=3)
        tk.Label(mic_row, text="Mic", bg=PANEL2, fg=MUTED, font=("Segoe UI", 9), width=12, anchor="w").pack(
            side="left", padx=(10, 4), pady=8
        )
        self.mic_var = tk.StringVar()
        self.mic_combo = ttk.Combobox(mic_row, textvariable=self.mic_var, state="readonly")
        self.mic_combo.pack(side="left", fill="x", expand=True, padx=(0, 10), pady=8)

        lb_row = tk.Frame(audio, bg=PANEL2)
        lb_row.pack(fill="x", pady=3)
        tk.Label(lb_row, text="Loopback", bg=PANEL2, fg=MUTED, font=("Segoe UI", 9), width=12, anchor="w").pack(
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

        out = self._card(inner, "Save to")
        path_row = tk.Frame(out, bg=PANEL2)
        path_row.pack(fill="x")
        self.dir_var = tk.StringVar(value=str(self.output_dir))
        ttk.Entry(path_row, textvariable=self.dir_var).pack(side="left", fill="x", expand=True, padx=(10, 6), pady=8)
        ttk.Button(path_row, text="Browse", style="Ghost.TButton", command=self._browse).pack(
            side="left", padx=(0, 10), pady=8
        )

        self._syncing_rates = False
        self.audio_kbps_var.trace_add("write", lambda *_: self._on_audio_or_video_rate())
        self.sample_rate_var.trace_add("write", lambda *_: self._refresh_quality_hints())
        self.video_kbps_var.trace_add("write", lambda *_: self._on_audio_or_video_rate())
        self.total_kbps_var.trace_add("write", lambda *_: self._on_total_rate())
        self.fps_var.trace_add("write", lambda *_: self._refresh_quality_hints())

        self._bind_sidebar_scroll(canvas, inner)

    def _tab_hover(self, key: str, on: bool) -> None:
        if key == self._current_page:
            return
        self._tab_labels[key].configure(fg=FG if on else MUTED)

    def _show_page(self, key: str) -> None:
        if key not in self._pages:
            return
        for name, page in self._pages.items():
            page.pack_forget()
            active = name == key
            self._tab_labels[name].configure(
                fg=FG if active else MUTED,
                font=("Segoe UI Semibold", 13),
            )
            self._tab_rules[name].configure(bg=ACCENT if active else BG)
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

    def _on_audio_or_video_rate(self) -> None:
        if self._syncing_rates:
            return
        self._rate_source = "av"
        self._syncing_rates = True
        try:
            audio = self._parse_int(self.audio_kbps_var, DEFAULT_AUDIO_KBPS)
            video = self._parse_int(self.video_kbps_var, DEFAULT_VIDEO_KBPS)
            self.total_kbps_var.set(str(audio + video))
        finally:
            self._syncing_rates = False
        self._refresh_quality_hints()

    def _on_total_rate(self) -> None:
        if self._syncing_rates:
            return
        self._rate_source = "total"
        self._syncing_rates = True
        try:
            audio = self._parse_int(self.audio_kbps_var, DEFAULT_AUDIO_KBPS)
            total = self._parse_int(self.total_kbps_var, DEFAULT_TOTAL_KBPS)
            video = max(VIDEO_KBPS_MIN, total - audio)
            self.video_kbps_var.set(str(video))
        finally:
            self._syncing_rates = False
        self._refresh_quality_hints()

    def _refresh_quality_hints(self) -> None:
        checks: list[tuple[str, tk.StringVar, int]] = [
            ("fps", self.fps_var, 1),
            ("audio", self.audio_kbps_var, DEFAULT_AUDIO_KBPS),
            ("sample", self.sample_rate_var, DEFAULT_SAMPLE_KHZ),
            ("video", self.video_kbps_var, DEFAULT_VIDEO_KBPS),
            ("total", self.total_kbps_var, DEFAULT_TOTAL_KBPS),
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
        self._syncing_rates = True
        try:
            for spin, var in self._setting_spins.values():
                try:
                    var.set(str(spin.get()).strip())
                except tk.TclError:
                    pass
        finally:
            self._syncing_rates = False
        if self._rate_source == "total":
            self._on_total_rate()
        else:
            self._on_audio_or_video_rate()

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

    def _read_rate_settings(self) -> tuple[int, int, int, int] | None:
        self._commit_settings()
        audio_kbps = self._read_int_setting(
            self.audio_kbps_var, "Audio kb/s", AUDIO_KBPS_MIN, AUDIO_KBPS_MAX, "48"
        )
        if audio_kbps is None:
            return None
        sample_khz = self._read_int_setting(
            self.sample_rate_var, "Sample rate", SAMPLE_KHZ_MIN, SAMPLE_KHZ_MAX, "48"
        )
        if sample_khz is None:
            return None
        sample_rate = sample_khz_to_hz(sample_khz)
        video_kbps = self._read_int_setting(
            self.video_kbps_var, "Data rate", VIDEO_KBPS_MIN, VIDEO_KBPS_MAX, "1500"
        )
        if video_kbps is None:
            return None
        total_kbps = self._read_int_setting(
            self.total_kbps_var, "Total kb/s", TOTAL_KBPS_MIN, TOTAL_KBPS_MAX, str(DEFAULT_TOTAL_KBPS)
        )
        if total_kbps is None:
            return None
        if self._rate_source == "total":
            video_kbps = max(VIDEO_KBPS_MIN, min(VIDEO_KBPS_MAX, total_kbps - audio_kbps))
        else:
            total_kbps = audio_kbps + video_kbps
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
        if video_kbps < MIN_USEFUL_VIDEO_KBPS:
            if not messagebox.askyesno(
                "Data rate may be too low",
                f"{video_kbps} kb/s is below {MIN_USEFUL_VIDEO_KBPS} kb/s. "
                "On-screen text and UI often become hard to read. Record anyway?",
            ):
                return None
        if total_kbps < MIN_USEFUL_TOTAL_KBPS:
            if not messagebox.askyesno(
                "Total bit rate may be too low",
                f"{total_kbps} kb/s is below {MIN_USEFUL_TOTAL_KBPS} kb/s. "
                "The recording is often too thin to use. Record anyway?",
            ):
                return None
        return audio_kbps, sample_rate, video_kbps, total_kbps

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
        scroll = ttk.Scrollbar(wrap, orient="vertical", command=canvas.yview)
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
        ttk.Button(bar, text="Click on screen...", style="Accent.TButton", command=lambda: click_screen()).pack(
            side="left"
        )
        ttk.Button(bar, text="Cancel", command=lambda: close()).pack(side="right")

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
            card = tk.Frame(
                parent,
                bg=PANEL,
                highlightbackground=BORDER,
                highlightthickness=1,
                cursor="hand2",
                bd=0,
            )
            card.grid(row=row, column=col, padx=8, pady=8, sticky="n")
            img = tk.Label(card, image=photo, bg=PANEL, cursor="hand2")
            img.pack(padx=6, pady=(6, 2))
            title = info.title.replace("\n", " ").strip() or "(untitled)"
            if info.is_desktop:
                title = "Entire screen"
            if len(title) > 34:
                title = title[:31] + "..."
            sub = "display" if info.is_desktop else (info.exe or "")
            cap = tk.Label(
                card,
                text=f"{title}\n{sub}",
                bg=PANEL,
                fg=FG,
                font=("Segoe UI", 9),
                justify="center",
                cursor="hand2",
            )
            cap.pack(padx=6, pady=(0, 8))

            def on_click(_event: object | None = None, target: WindowInfo = info) -> None:
                choose(target)

            def enter(_e: object, c: tk.Frame = card) -> None:
                c.configure(highlightbackground=ACCENT, highlightthickness=2)

            def leave(_e: object, c: tk.Frame = card) -> None:
                c.configure(highlightbackground=BORDER, highlightthickness=1)

            for w in (card, img, cap):
                w.bind("<Button-1>", on_click)
                w.bind("<Enter>", enter)
                w.bind("<Leave>", leave)

        def fill(rows: list[tuple[WindowInfo, Path]]) -> None:
            if closed["done"] or not dlg.winfo_exists():
                shutil.rmtree(thumb_dir, ignore_errors=True)
                return
            for child in host.winfo_children():
                child.destroy()
            dlg._photos.clear()
            for i, (info, path) in enumerate(rows):
                try:
                    photo = tk.PhotoImage(file=str(path))
                except tk.TclError:
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
            foreground="#fff",
            font=("Segoe UI Semibold", 12),
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
        self.pause_btn.configure(text="Resume" if paused else "Pause")
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

    def toggle_record(self) -> None:
        if self.session:
            self.record_btn.configure(state="disabled")
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
            messagebox.showwarning("Recorder", "FPS must be a whole number you type, e.g. 30 or 60.")
            return
        if fps < 1 or fps > 240:
            messagebox.showwarning("Recorder", "FPS must be a whole number between 1 and 240.")
            return

        parsed = self._read_rate_settings()
        if parsed is None:
            return
        audio_kbps, sample_rate, video_kbps, total_kbps = parsed

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
            video_kbps=clamp_video_kbps(video_kbps),
        )
        self.session = CaptureSession(cfg, self.hw, on_log=self._log)
        try:
            self.session.start()
        except Exception as exc:
            self.session = None
            messagebox.showerror("Record failed", str(exc))
            return
        self.record_btn.configure(text="■   Stop    F9", style="Stop.TButton")
        self.pause_btn.configure(state="normal", text="Pause")
        self._set_live_badge("rec")
        try:
            self.win_list.configure(selectmode="none")
        except tk.TclError:
            pass
        self._tick()
        self._log(
            f"Recording {window.label()} @ {fps} fps, "
            f"video {video_kbps} kb/s, total {total_kbps} kb/s, "
            f"Opus {audio_kbps} kb/s {snap_opus_rate(sample_rate) // 1000} kHz -> {cfg.output.name}"
        )

    def _tick(self) -> None:
        if not self.session:
            self.time_label.configure(text="00:00:00")
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
        self.record_btn.configure(text="●   Record    F9", style="Record.TButton", state="normal")
        self.pause_btn.configure(state="disabled", text="Pause")
        self.time_label.configure(text="00:00:00")
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
