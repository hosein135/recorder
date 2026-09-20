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
    DEFAULT_SAMPLE_RATE,
    DEFAULT_TOTAL_KBPS,
    DEFAULT_VIDEO_KBPS,
    MIN_USEFUL_AUDIO_KBPS,
    MIN_USEFUL_SAMPLE_RATE,
    MIN_USEFUL_TOTAL_KBPS,
    MIN_USEFUL_VIDEO_KBPS,
    RecordConfig,
    RecorderError,
    SAMPLE_RATE_MAX,
    SAMPLE_RATE_MIN,
    TOTAL_KBPS_MAX,
    TOTAL_KBPS_MIN,
    VIDEO_KBPS_MAX,
    VIDEO_KBPS_MIN,
    clamp_audio_kbps,
    clamp_sample_rate,
    clamp_video_kbps,
    default_output_path,
    snap_opus_rate,
)
from hwnd_grab import grab_thumb_ppm
from hw_detect import HardwareProfile, detect, format_involvement_report
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
SIDE_W = 340


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

        self._style()
        self._build()
        self.after(50, self._boot)

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
        style.configure("Title.TLabel", background=BG, foreground=FG, font=("Segoe UI Semibold", 18))
        style.configure("Timer.TLabel", background=BG, foreground=FG, font=("Cascadia Mono", 20, "bold"))
        style.configure("Badge.TLabel", background=PANEL2, foreground=MUTED, font=("Segoe UI", 8), padding=(8, 3))
        style.configure("Live.TLabel", background=RECORD, foreground="#fff", font=("Segoe UI Semibold", 8), padding=(8, 3))
        style.configure("Ready.TLabel", background=PANEL2, foreground=OK, font=("Segoe UI Semibold", 8), padding=(8, 3))
        style.configure("Paused.TLabel", background="#6b5420", foreground="#fff", font=("Segoe UI Semibold", 8), padding=(8, 3))
        style.configure("Field.TLabel", background=BG, foreground=MUTED, font=("Segoe UI", 9), width=14)
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
        style.configure("TNotebook", background=BG, borderwidth=0, tabmargins=(0, 0, 0, 0))
        style.configure(
            "TNotebook.Tab",
            background=PANEL2,
            foreground=MUTED,
            padding=(22, 9),
            font=("Segoe UI Semibold", 10),
            borderwidth=0,
        )
        style.map(
            "TNotebook.Tab",
            background=[("selected", ACCENT), ("active", ACCENT_DIM)],
            foreground=[("selected", "#fff"), ("active", "#fff")],
        )
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

        self.nb = ttk.Notebook(self)
        self.nb.pack(fill="both", expand=True, padx=18, pady=(0, 6))
        self.record_tab = ttk.Frame(self.nb)
        self.recs_tab = ttk.Frame(self.nb)
        self.sys_tab = ttk.Frame(self.nb)
        self.nb.add(self.record_tab, text="  Record  ")
        self.nb.add(self.recs_tab, text="  Recordings  ")
        self.nb.add(self.sys_tab, text="  System  ")
        self.nb.bind("<<NotebookTabChanged>>", self._on_tab)

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
            text="Window stays on screen. Minimize or maximize anytime. F9 starts or stops.",
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

        right = ttk.Frame(body, width=SIDE_W)
        right.pack(side="right", fill="y", padx=(12, 0))
        right.pack_propagate(False)

        rec = ttk.LabelFrame(right, text="  Capture  ")
        rec.pack(fill="x")

        grid = ttk.Frame(rec)
        grid.pack(fill="x", padx=12, pady=(12, 4))
        for col, weight in ((0, 0), (1, 1), (2, 0)):
            grid.columnconfigure(col, weight=weight)

        ttk.Label(grid, text="FPS", style="Field.TLabel").grid(row=0, column=0, sticky="w", pady=4)
        self.fps_var = tk.StringVar(value="30")
        ttk.Spinbox(grid, textvariable=self.fps_var, from_=1, to=240, increment=1, width=7).grid(
            row=0, column=1, sticky="w", padx=(0, 8), pady=4
        )
        ttk.Label(grid, text="1-240", style="Hint.TLabel").grid(row=0, column=2, sticky="w")

        ttk.Label(grid, text="Audio kb/s", style="Field.TLabel").grid(row=1, column=0, sticky="w", pady=4)
        self.audio_kbps_var = tk.StringVar(value=str(DEFAULT_AUDIO_KBPS))
        ttk.Spinbox(
            grid,
            textvariable=self.audio_kbps_var,
            from_=AUDIO_KBPS_MIN,
            to=AUDIO_KBPS_MAX,
            increment=8,
            width=7,
        ).grid(row=1, column=1, sticky="w", padx=(0, 8), pady=4)
        ttk.Label(grid, text="Opus", style="Hint.TLabel").grid(row=1, column=2, sticky="w")

        ttk.Label(grid, text="Sample rate", style="Field.TLabel").grid(row=2, column=0, sticky="w", pady=4)
        self.sample_rate_var = tk.StringVar(value=str(DEFAULT_SAMPLE_RATE))
        ttk.Spinbox(
            grid,
            textvariable=self.sample_rate_var,
            from_=SAMPLE_RATE_MIN,
            to=SAMPLE_RATE_MAX,
            increment=1000,
            width=7,
        ).grid(row=2, column=1, sticky="w", padx=(0, 8), pady=4)
        ttk.Label(grid, text="Hz", style="Hint.TLabel").grid(row=2, column=2, sticky="w")

        ttk.Label(grid, text="Data rate", style="Field.TLabel").grid(row=3, column=0, sticky="w", pady=4)
        self.video_kbps_var = tk.StringVar(value=str(DEFAULT_VIDEO_KBPS))
        ttk.Spinbox(
            grid,
            textvariable=self.video_kbps_var,
            from_=VIDEO_KBPS_MIN,
            to=VIDEO_KBPS_MAX,
            increment=100,
            width=7,
        ).grid(row=3, column=1, sticky="w", padx=(0, 8), pady=4)
        ttk.Label(grid, text="video kb/s", style="Hint.TLabel").grid(row=3, column=2, sticky="w")

        ttk.Label(grid, text="Total kb/s", style="Field.TLabel").grid(row=4, column=0, sticky="w", pady=4)
        self.total_kbps_var = tk.StringVar(value=str(DEFAULT_TOTAL_KBPS))
        ttk.Spinbox(
            grid,
            textvariable=self.total_kbps_var,
            from_=TOTAL_KBPS_MIN,
            to=TOTAL_KBPS_MAX,
            increment=100,
            width=7,
        ).grid(row=4, column=1, sticky="w", padx=(0, 8), pady=4)
        ttk.Label(grid, text="A+V", style="Hint.TLabel").grid(row=4, column=2, sticky="w")

        self.audio_hint = ttk.Label(
            rec,
            text=f"Stay at {DEFAULT_AUDIO_KBPS} kb/s or above {MIN_USEFUL_AUDIO_KBPS} so speech stays usable.",
            style="Hint.TLabel",
            wraplength=SIDE_W - 36,
        )
        self.audio_hint.pack(anchor="w", padx=12, pady=(2, 0))
        self.sample_hint = ttk.Label(
            rec,
            text=f"{DEFAULT_SAMPLE_RATE} Hz is a good default. Avoid going below {MIN_USEFUL_SAMPLE_RATE} Hz - speech gets muffled.",
            style="Hint.TLabel",
            wraplength=SIDE_W - 36,
        )
        self.sample_hint.pack(anchor="w", padx=12, pady=(0, 0))
        self.video_hint = ttk.Label(
            rec,
            text=f"{DEFAULT_VIDEO_KBPS} kb/s is a smaller-file default. Avoid going below {MIN_USEFUL_VIDEO_KBPS} kb/s - on-screen text gets hard to read.",
            style="Hint.TLabel",
            wraplength=SIDE_W - 36,
        )
        self.video_hint.pack(anchor="w", padx=12, pady=(0, 0))
        self.total_hint = ttk.Label(
            rec,
            text=f"Total is video + audio. Avoid going below {MIN_USEFUL_TOTAL_KBPS} kb/s.",
            style="Hint.TLabel",
            wraplength=SIDE_W - 36,
        )
        self.total_hint.pack(anchor="w", padx=12, pady=(0, 6))
        self._syncing_rates = False
        self.audio_kbps_var.trace_add("write", lambda *_: self._on_audio_or_video_rate())
        self.sample_rate_var.trace_add("write", lambda *_: self._refresh_quality_hints())
        self.video_kbps_var.trace_add("write", lambda *_: self._on_audio_or_video_rate())
        self.total_kbps_var.trace_add("write", lambda *_: self._on_total_rate())

        ttk.Label(rec, text="Sound source", style="Muted.TLabel").pack(anchor="w", padx=12, pady=(6, 2))
        self.audio_var = tk.StringVar(value="both")
        audio_row = ttk.Frame(rec)
        audio_row.pack(fill="x", padx=10, pady=(0, 6))
        for value, label in (("internal", "Internal"), ("external", "Mic"), ("both", "Both")):
            ttk.Radiobutton(
                audio_row,
                text=label,
                value=value,
                variable=self.audio_var,
                command=self._audio_changed,
            ).pack(side="left", padx=6)

        mic_row = ttk.Frame(rec)
        mic_row.pack(fill="x", padx=12, pady=(4, 4))
        ttk.Label(mic_row, text="Mic", style="Field.TLabel").pack(side="left")
        self.mic_var = tk.StringVar()
        self.mic_combo = ttk.Combobox(mic_row, textvariable=self.mic_var, state="readonly", width=22)
        self.mic_combo.pack(side="left", fill="x", expand=True)

        lb_row = ttk.Frame(rec)
        lb_row.pack(fill="x", padx=12, pady=(0, 12))
        ttk.Label(lb_row, text="Loopback", style="Field.TLabel").pack(side="left")
        self.loop_var = tk.StringVar()
        self.loop_combo = ttk.Combobox(lb_row, textvariable=self.loop_var, state="readonly", width=22)
        self.loop_combo.pack(side="left", fill="x", expand=True)

        out = ttk.LabelFrame(right, text="  Save to  ")
        out.pack(fill="x", pady=(10, 0))
        path_row = ttk.Frame(out)
        path_row.pack(fill="x", padx=12, pady=10)
        self.dir_var = tk.StringVar(value=str(self.output_dir))
        ttk.Entry(path_row, textvariable=self.dir_var).pack(side="left", fill="x", expand=True)
        ttk.Button(path_row, text="Browse", style="Ghost.TButton", command=self._browse).pack(
            side="left", padx=(6, 0)
        )

        actions = ttk.Frame(right)
        actions.pack(fill="x", pady=(14, 0))
        self.record_btn = ttk.Button(
            actions, text="●   Record    F9", style="Record.TButton", command=self.toggle_record
        )
        self.record_btn.pack(fill="x")
        pause_row = ttk.Frame(actions)
        pause_row.pack(fill="x", pady=(8, 0))
        self.pause_btn = ttk.Button(pause_row, text="Pause", command=self.toggle_pause, state="disabled")
        self.pause_btn.pack(side="left", fill="x", expand=True)
        ttk.Label(pause_row, text="Window is not hidden or moved", style="Hint.TLabel").pack(
            side="left", padx=(8, 0)
        )

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
        rec_wrap.pack(fill="both", expand=True, padx=12, pady=(0, 12))
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
        self.rec_list.bind("<Double-Button-1>", self._on_recording_click)
        self.rec_list.bind("<Return>", self._on_recording_enter)
        self.rec_list.bind("<<TreeviewSelect>>", lambda _e: self._sync_play_btn())
        self.rec_list.tag_configure("odd", background=PANEL)
        self.rec_list.tag_configure("even", background=PANEL2)
        self.rec_list.tag_configure("fresh", foreground=OK)

        self.hw_box = tk.Text(
            self.sys_tab,
            bg=PANEL,
            fg=MUTED,
            relief="flat",
            highlightthickness=0,
            font=("Cascadia Mono", 9),
            wrap="word",
            padx=12,
            pady=12,
        )
        self.hw_box.pack(fill="both", expand=True, padx=12, pady=12)
        self.hw_box.configure(state="disabled")

        status = ttk.Frame(self)
        status.pack(fill="x", padx=18, pady=(0, 10))
        self.status_var = tk.StringVar(value="Ready")
        ttk.Label(status, textvariable=self.status_var, style="Muted.TLabel").pack(side="left")

        self.bind("<F9>", lambda _e: self.toggle_record())
        self.protocol("WM_DELETE_WINDOW", self._on_close)

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
            report = format_involvement_report(self.hw, int(self.fps_var.get() or 30))
            self._set_hw(report)
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

    def _set_hw(self, text: str) -> None:
        self.hw_box.configure(state="normal")
        self.hw_box.delete("1.0", "end")
        self.hw_box.insert("1.0", text)
        self.hw_box.configure(state="disabled")

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
        self._syncing_rates = True
        try:
            audio = self._parse_int(self.audio_kbps_var, DEFAULT_AUDIO_KBPS)
            total = self._parse_int(self.total_kbps_var, DEFAULT_TOTAL_KBPS)
            video = max(VIDEO_KBPS_MIN, total - audio)
            self.video_kbps_var.set(str(video))
        finally:
            self._syncing_rates = False
        self._refresh_quality_hints()

    def _set_hint(self, label: ttk.Label, warn: bool, warn_text: str, ok_text: str) -> None:
        label.configure(style="Warn.TLabel" if warn else "Hint.TLabel", text=warn_text if warn else ok_text)

    def _refresh_quality_hints(self) -> None:
        if getattr(self, "audio_hint", None) is None:
            return
        kbps = self._parse_int(self.audio_kbps_var, DEFAULT_AUDIO_KBPS)
        self._set_hint(
            self.audio_hint,
            kbps < MIN_USEFUL_AUDIO_KBPS,
            (
                f"Below {MIN_USEFUL_AUDIO_KBPS} kb/s, audio is usually too thin to use. "
                f"Stay at {DEFAULT_AUDIO_KBPS} unless you need a smaller file."
            ),
            f"Stay at {DEFAULT_AUDIO_KBPS} kb/s or above {MIN_USEFUL_AUDIO_KBPS} so speech stays usable.",
        )
        rate = self._parse_int(self.sample_rate_var, DEFAULT_SAMPLE_RATE)
        self._set_hint(
            self.sample_hint,
            rate < MIN_USEFUL_SAMPLE_RATE,
            (
                f"Below {MIN_USEFUL_SAMPLE_RATE} Hz, speech gets muffled. "
                f"Stay at {DEFAULT_SAMPLE_RATE} unless you need a smaller file."
            ),
            f"{DEFAULT_SAMPLE_RATE} Hz is a good default. Avoid going below {MIN_USEFUL_SAMPLE_RATE} Hz - speech gets muffled.",
        )
        video = self._parse_int(self.video_kbps_var, DEFAULT_VIDEO_KBPS)
        self._set_hint(
            self.video_hint,
            video < MIN_USEFUL_VIDEO_KBPS,
            (
                f"Below {MIN_USEFUL_VIDEO_KBPS} kb/s, video is usually too blocky to use. "
                "On-screen text and UI get hard to read."
            ),
            f"{DEFAULT_VIDEO_KBPS} kb/s is a smaller-file default. Avoid going below {MIN_USEFUL_VIDEO_KBPS} kb/s - on-screen text gets hard to read.",
        )
        total = self._parse_int(self.total_kbps_var, DEFAULT_TOTAL_KBPS)
        self._set_hint(
            self.total_hint,
            total < MIN_USEFUL_TOTAL_KBPS,
            (
                f"Below {MIN_USEFUL_TOTAL_KBPS} kb/s total, the file is usually too thin to use. "
                f"Keep video at {MIN_USEFUL_VIDEO_KBPS}+ and audio at {MIN_USEFUL_AUDIO_KBPS}+."
            ),
            f"Total is video + audio. Avoid going below {MIN_USEFUL_TOTAL_KBPS} kb/s.",
        )

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
        audio_kbps = self._read_int_setting(
            self.audio_kbps_var, "Audio kb/s", AUDIO_KBPS_MIN, AUDIO_KBPS_MAX, "48"
        )
        if audio_kbps is None:
            return None
        sample_rate = self._read_int_setting(
            self.sample_rate_var, "Sample rate", SAMPLE_RATE_MIN, SAMPLE_RATE_MAX, "48000"
        )
        if sample_rate is None:
            return None
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
        if audio_kbps < MIN_USEFUL_AUDIO_KBPS:
            if not messagebox.askyesno(
                "Audio may be too thin",
                f"{audio_kbps} kb/s is below {MIN_USEFUL_AUDIO_KBPS} kb/s. "
                "Speech and UI sounds often become hard to use. Record anyway?",
            ):
                return None
        if sample_rate < MIN_USEFUL_SAMPLE_RATE:
            if not messagebox.askyesno(
                "Sample rate may be too low",
                f"{sample_rate} Hz is below {MIN_USEFUL_SAMPLE_RATE} Hz. "
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
            f"Opus {audio_kbps} kb/s {snap_opus_rate(sample_rate)} Hz -> {cfg.output.name}"
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
        try:
            self.nb.select(self.recs_tab)
        except tk.TclError:
            pass
        if path:
            self._log(f"Saved {path.name}")
            self.status_var.set(f"Saved  ·  {path.name}")

    def _on_tab(self, _event: object | None = None) -> None:
        try:
            current = self.nb.index(self.nb.select())
        except tk.TclError:
            return
        if current == 1:
            sel = self.rec_list.selection()
            keep = self._rec_by_id.get(sel[0]) if sel else None
            self.refresh_recordings(select_path=keep)

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
