#!/usr/bin/env python3
"""Tkinter GUI: pick a window, FPS, internal/mic/both audio, record H.266/VVC."""

from __future__ import annotations

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
    DEFAULT_VIDEO_QUALITY,
    MIN_USEFUL_AUDIO_KBPS,
    MIN_USEFUL_VIDEO_QUALITY,
    RecordConfig,
    RecorderError,
    VIDEO_QUALITY_MAX,
    VIDEO_QUALITY_MIN,
    clamp_audio_kbps,
    clamp_video_quality,
    default_output_path,
    quality_to_qp,
)
from hwnd_grab import grab_thumb_ppm
from hw_detect import HardwareProfile, detect, format_involvement_report
from player import find_mpc_hc, format_size, list_recordings, play_with_mpc
from windows import WindowInfo, cursor_pos, escape_pressed, left_button_down, list_windows, window_at_point

BG = "#1b1d23"
PANEL = "#252830"
FG = "#e8eaed"
MUTED = "#9aa3b2"
ACCENT = "#3d8bfd"
RECORD = "#e23d3d"
BORDER = "#3a3f4b"


class RecorderApp(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title("Window Recorder  ·  H.266 / VVC")
        self.geometry("960x720")
        self.minsize(820, 620)
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

        self._style()
        self._build()
        self.after(50, self._boot)

    def _style(self) -> None:
        style = ttk.Style(self)
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass
        style.configure(".", background=BG, foreground=FG, fieldbackground=PANEL)
        style.configure("TFrame", background=BG)
        style.configure("Panel.TFrame", background=PANEL)
        style.configure("TLabel", background=BG, foreground=FG, font=("Segoe UI", 10))
        style.configure("Muted.TLabel", background=BG, foreground=MUTED, font=("Segoe UI", 9))
        style.configure("Warn.TLabel", background=BG, foreground="#e8b849", font=("Segoe UI", 9))
        style.configure("Panel.TLabel", background=PANEL, foreground=FG)
        style.configure("Title.TLabel", background=BG, foreground=FG, font=("Segoe UI Semibold", 16))
        style.configure("TRadiobutton", background=BG, foreground=FG, font=("Segoe UI", 10))
        style.configure("TButton", font=("Segoe UI", 10), padding=6)
        style.configure("Accent.TButton", font=("Segoe UI Semibold", 11), padding=(18, 8))
        style.configure("Record.TButton", font=("Segoe UI Semibold", 11), padding=(18, 8), foreground="#fff")
        style.map("Record.TButton", background=[("!disabled", RECORD), ("disabled", BORDER)])
        style.configure("TCombobox", fieldbackground=PANEL, background=PANEL, foreground=FG)
        style.configure("TSpinbox", fieldbackground=PANEL, background=PANEL, foreground=FG)
        style.configure("TLabelframe", background=BG, foreground=FG)
        style.configure("TLabelframe.Label", background=BG, foreground=MUTED)
        style.configure("TNotebook", background=BG, borderwidth=0)
        style.configure("TNotebook.Tab", background=PANEL, foreground=MUTED, padding=(16, 6), font=("Segoe UI", 10))
        style.map("TNotebook.Tab", background=[("selected", ACCENT)], foreground=[("selected", "#fff")])

    def _build(self) -> None:
        pad = {"padx": 14, "pady": 8}

        header = ttk.Frame(self)
        header.pack(fill="x", **pad)
        ttk.Label(header, text="Window Recorder", style="Title.TLabel").pack(side="left")
        ttk.Label(header, text="FFmpeg libvvenc + libopus  ·  H.266 / VVC", style="Muted.TLabel").pack(
            side="left", padx=(12, 0)
        )

        self.nb = ttk.Notebook(self)
        self.nb.pack(fill="both", expand=True, padx=14, pady=(0, 4))
        record_tab = ttk.Frame(self.nb)
        recs_tab = ttk.Frame(self.nb)
        self.nb.add(record_tab, text="  Record  ")
        self.nb.add(recs_tab, text="  Recordings  ")
        self.nb.bind("<<NotebookTabChanged>>", self._on_tab)

        body = ttk.Frame(record_tab)
        body.pack(fill="both", expand=True, padx=14)

        left = ttk.LabelFrame(body, text="Open windows")
        left.pack(side="left", fill="both", expand=True, padx=(0, 8))

        btns = ttk.Frame(left)
        btns.pack(fill="x", padx=8, pady=(8, 4))
        ttk.Button(btns, text="Refresh", command=self.refresh_windows).pack(side="left")
        ttk.Button(btns, text="Choose window...", command=self._open_share_picker).pack(
            side="left", padx=(8, 0)
        )
        ttk.Button(btns, text="Click on screen", command=self._start_click_pick).pack(
            side="left", padx=(8, 0)
        )
        ttk.Label(btns, text="Filter").pack(side="left", padx=(12, 4))
        self.filter_var = tk.StringVar()
        self.filter_var.trace_add("write", lambda *_: self._apply_filter())
        filt = ttk.Entry(btns, textvariable=self.filter_var, width=22)
        filt.pack(side="left", fill="x", expand=True)
        ttk.Label(
            left,
            text="Pick a window like a screen share. It stays on screen - you can minimize or maximize while recording.",
            style="Muted.TLabel",
        ).pack(anchor="w", padx=8)

        cols = ("title", "app", "size", "state")
        self.win_list = ttk.Treeview(left, columns=cols, show="headings", selectmode="browse", height=14)
        self.win_list.heading("title", text="Window")
        self.win_list.heading("app", text="App")
        self.win_list.heading("size", text="Size")
        self.win_list.heading("state", text="State")
        self.win_list.column("title", width=280, anchor="w")
        self.win_list.column("app", width=110, anchor="w")
        self.win_list.column("size", width=90, anchor="center")
        self.win_list.column("state", width=90, anchor="w")
        scroll = ttk.Scrollbar(left, command=self.win_list.yview)
        self.win_list.configure(yscrollcommand=scroll.set)
        self.win_list.pack(side="left", fill="both", expand=True, padx=(8, 0), pady=(0, 8))
        scroll.pack(side="right", fill="y", pady=(0, 8), padx=(0, 8))
        self.win_list.bind("<<TreeviewSelect>>", self._on_win_select)
        self.selected_lbl = ttk.Label(left, text="Selected: Entire screen", style="Muted.TLabel")
        self.selected_lbl.pack(fill="x", padx=8, pady=(0, 8))
        style = ttk.Style(self)
        style.configure(
            "Treeview",
            background=PANEL,
            foreground=FG,
            fieldbackground=PANEL,
            rowheight=22,
        )
        style.configure("Treeview.Heading", background=BG, foreground=MUTED)
        style.map("Treeview", background=[("selected", ACCENT)], foreground=[("selected", "#fff")])

        right = ttk.Frame(body)
        right.pack(side="right", fill="y")

        rec = ttk.LabelFrame(right, text="Capture")
        rec.pack(fill="x")

        row = ttk.Frame(rec)
        row.pack(fill="x", padx=10, pady=(10, 4))
        ttk.Label(row, text="FPS").pack(side="left")
        self.fps_var = tk.StringVar(value="30")
        fps = ttk.Spinbox(
            row,
            textvariable=self.fps_var,
            from_=1,
            to=240,
            increment=1,
            width=8,
        )
        fps.pack(side="left", padx=(8, 0))
        ttk.Label(row, text="type any integer 1-240", style="Muted.TLabel").pack(side="left", padx=(8, 0))

        row = ttk.Frame(rec)
        row.pack(fill="x", padx=10, pady=(6, 0))
        ttk.Label(row, text="Audio kb/s").pack(side="left")
        self.audio_kbps_var = tk.StringVar(value=str(DEFAULT_AUDIO_KBPS))
        ttk.Spinbox(
            row,
            textvariable=self.audio_kbps_var,
            from_=AUDIO_KBPS_MIN,
            to=AUDIO_KBPS_MAX,
            increment=8,
            width=8,
        ).pack(side="left", padx=(8, 0))
        ttk.Label(
            row,
            text=f"Opus  ·  type {AUDIO_KBPS_MIN}-{AUDIO_KBPS_MAX}",
            style="Muted.TLabel",
        ).pack(side="left", padx=(8, 0))
        self.audio_hint = ttk.Label(
            rec,
            text=(
                f"{DEFAULT_AUDIO_KBPS} kb/s is a good default. Avoid going below "
                f"{MIN_USEFUL_AUDIO_KBPS} kb/s - audio gets too thin to use."
            ),
            style="Muted.TLabel",
            wraplength=340,
        )
        self.audio_hint.pack(anchor="w", padx=10, pady=(2, 0))

        row = ttk.Frame(rec)
        row.pack(fill="x", padx=10, pady=(8, 0))
        ttk.Label(row, text="Video quality").pack(side="left")
        self.quality_var = tk.StringVar(value=str(DEFAULT_VIDEO_QUALITY))
        ttk.Spinbox(
            row,
            textvariable=self.quality_var,
            from_=VIDEO_QUALITY_MIN,
            to=VIDEO_QUALITY_MAX,
            increment=1,
            width=8,
        ).pack(side="left", padx=(8, 0))
        ttk.Label(
            row,
            text=f"1-{VIDEO_QUALITY_MAX}, higher = larger file",
            style="Muted.TLabel",
        ).pack(side="left", padx=(8, 0))
        self.video_hint = ttk.Label(
            rec,
            text=(
                f"{DEFAULT_VIDEO_QUALITY} is a smaller-file default. Avoid going below "
                f"{MIN_USEFUL_VIDEO_QUALITY} - on-screen text gets hard to read."
            ),
            style="Muted.TLabel",
            wraplength=340,
        )
        self.video_hint.pack(anchor="w", padx=10, pady=(2, 4))
        self.audio_kbps_var.trace_add("write", lambda *_: self._refresh_quality_hints())
        self.quality_var.trace_add("write", lambda *_: self._refresh_quality_hints())

        ttk.Label(rec, text="Audio", style="Muted.TLabel").pack(anchor="w", padx=10, pady=(8, 0))
        self.audio_var = tk.StringVar(value="both")
        for value, label in (
            ("internal", "Internal  (speakers / apps)"),
            ("external", "External  (microphone)"),
            ("both", "Both"),
        ):
            ttk.Radiobutton(
                rec,
                text=label,
                value=value,
                variable=self.audio_var,
                command=self._audio_changed,
            ).pack(anchor="w", padx=18, pady=1)

        mic_row = ttk.Frame(rec)
        mic_row.pack(fill="x", padx=10, pady=(8, 4))
        ttk.Label(mic_row, text="Mic").pack(side="left")
        self.mic_var = tk.StringVar()
        self.mic_combo = ttk.Combobox(mic_row, textvariable=self.mic_var, state="readonly", width=28)
        self.mic_combo.pack(side="left", padx=(8, 0), fill="x", expand=True)

        lb_row = ttk.Frame(rec)
        lb_row.pack(fill="x", padx=10, pady=(0, 10))
        ttk.Label(lb_row, text="Loopback").pack(side="left")
        self.loop_var = tk.StringVar()
        self.loop_combo = ttk.Combobox(lb_row, textvariable=self.loop_var, state="readonly", width=24)
        self.loop_combo.pack(side="left", padx=(8, 0), fill="x", expand=True)

        out = ttk.LabelFrame(right, text="Save to")
        out.pack(fill="x", pady=(10, 0))
        path_row = ttk.Frame(out)
        path_row.pack(fill="x", padx=10, pady=8)
        self.dir_var = tk.StringVar(value=str(self.output_dir))
        ttk.Entry(path_row, textvariable=self.dir_var, width=28).pack(side="left", fill="x", expand=True)
        ttk.Button(path_row, text="Browse", command=self._browse).pack(side="left", padx=(6, 0))

        actions = ttk.Frame(right)
        actions.pack(fill="x", pady=12)
        self.record_btn = ttk.Button(actions, text="●  Record", style="Record.TButton", command=self.toggle_record)
        self.record_btn.pack(side="left")
        self.pause_btn = ttk.Button(actions, text="Pause", command=self.toggle_pause, state="disabled")
        self.pause_btn.pack(side="left", padx=(8, 0))
        self.time_label = ttk.Label(actions, text="00:00:00", style="Title.TLabel")
        self.time_label.pack(side="left", padx=16)

        rec_bar = ttk.Frame(recs_tab)
        rec_bar.pack(fill="x", padx=8, pady=8)
        ttk.Button(rec_bar, text="Refresh", command=lambda: self.refresh_recordings(log=True)).pack(side="left")
        ttk.Label(
            rec_bar,
            text="Double-click a recording to play it in Media Player Classic (MPC-HC).",
            style="Muted.TLabel",
        ).pack(side="left", padx=12)
        self.mpc_status = ttk.Label(rec_bar, text="", style="Muted.TLabel")
        self.mpc_status.pack(side="right")

        rec_cols = ("name", "size", "modified")
        rec_wrap = ttk.Frame(recs_tab)
        rec_wrap.pack(fill="both", expand=True, padx=8, pady=(0, 8))
        self.rec_list = ttk.Treeview(
            rec_wrap, columns=rec_cols, show="headings", selectmode="browse", height=16
        )
        self.rec_list.heading("name", text="File")
        self.rec_list.heading("size", text="Size")
        self.rec_list.heading("modified", text="Modified")
        self.rec_list.column("name", width=420, anchor="w")
        self.rec_list.column("size", width=90, anchor="e")
        self.rec_list.column("modified", width=160, anchor="w")
        rec_scroll = ttk.Scrollbar(rec_wrap, command=self.rec_list.yview)
        self.rec_list.configure(yscrollcommand=rec_scroll.set)
        self.rec_list.pack(side="left", fill="both", expand=True)
        rec_scroll.pack(side="right", fill="y")
        self.rec_list.bind("<Double-Button-1>", self._on_recording_click)
        self.rec_list.bind("<Return>", self._on_recording_enter)

        self.hw_box = tk.Text(
            self,
            height=7,
            bg=PANEL,
            fg=MUTED,
            relief="flat",
            highlightthickness=1,
            highlightbackground=BORDER,
            font=("Consolas", 9),
            wrap="word",
        )
        self.hw_box.pack(fill="x", padx=14, pady=(8, 0))
        self.hw_box.configure(state="disabled")

        self.log = tk.Text(
            self,
            height=6,
            bg=PANEL,
            fg=FG,
            relief="flat",
            highlightthickness=1,
            highlightbackground=BORDER,
            font=("Consolas", 9),
            wrap="word",
        )
        self.log.pack(fill="both", expand=True, padx=14, pady=10)
        self.log.configure(state="disabled")

        self.protocol("WM_DELETE_WINDOW", self._on_close)

    def _boot(self) -> None:
        self._log("Detecting hardware and FFmpeg…")
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

    def _set_hw(self, text: str) -> None:
        self.hw_box.configure(state="normal")
        self.hw_box.delete("1.0", "end")
        self.hw_box.insert("1.0", text)
        self.hw_box.configure(state="disabled")

    def _log(self, msg: str) -> None:
        def _append() -> None:
            self.log.configure(state="normal")
            self.log.insert("end", msg.rstrip() + "\n")
            self.log.see("end")
            self.log.configure(state="disabled")

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
        for w in self.windows:
            if needle and needle not in w.search_blob():
                continue
            iid = "screen" if w.is_desktop else f"hwnd-{w.hwnd}"
            title = w.title.replace("\n", " ").strip() or "(untitled)"
            if w.is_desktop:
                title = "Entire screen"
            if len(title) > 90:
                title = title[:87] + "..."
            self.win_list.insert(
                "",
                "end",
                iid=iid,
                values=(title, w.exe or ("display" if w.is_desktop else ""), f"{w.width}x{w.height}", w.state_text() or ("display" if w.is_desktop else "")),
            )
            self._by_id[iid] = w
            if first_id is None:
                first_id = iid
            if prefer_hwnd is not None and w.hwnd == prefer_hwnd:
                prefer_id = iid
        pick = prefer_id or first_id
        if pick:
            self.win_list.selection_set(pick)
            self.win_list.see(pick)
            self._on_win_select()

    def _selected_window(self) -> WindowInfo | None:
        sel = self.win_list.selection()
        if not sel:
            return None
        return self._by_id.get(sel[0])

    def _on_win_select(self, _event: object | None = None) -> None:
        if getattr(self, "selected_lbl", None) is None:
            return
        w = self._selected_window()
        if w is None:
            self.selected_lbl.configure(text="Selected: (none)")
            return
        self.selected_lbl.configure(text=f"Selected: {w.label()}")

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
            self.win_list.see(iid)
            self._on_win_select()
        self._log(f"Target: {info.label()}")

    def _refresh_quality_hints(self) -> None:
        if getattr(self, "audio_hint", None) is None or getattr(self, "video_hint", None) is None:
            return
        try:
            kbps = int(str(self.audio_kbps_var.get()).strip())
        except ValueError:
            kbps = DEFAULT_AUDIO_KBPS
        if kbps < MIN_USEFUL_AUDIO_KBPS:
            self.audio_hint.configure(
                style="Warn.TLabel",
                text=(
                    f"Below {MIN_USEFUL_AUDIO_KBPS} kb/s, audio is usually too thin to use. "
                    f"Stay at {DEFAULT_AUDIO_KBPS} unless you need a smaller file."
                ),
            )
        else:
            self.audio_hint.configure(
                style="Muted.TLabel",
                text=(
                    f"{DEFAULT_AUDIO_KBPS} kb/s is a good default. Avoid going below "
                    f"{MIN_USEFUL_AUDIO_KBPS} kb/s - audio gets too thin to use."
                ),
            )
        try:
            quality = int(str(self.quality_var.get()).strip())
        except ValueError:
            quality = DEFAULT_VIDEO_QUALITY
        if quality < MIN_USEFUL_VIDEO_QUALITY:
            self.video_hint.configure(
                style="Warn.TLabel",
                text=(
                    f"Below {MIN_USEFUL_VIDEO_QUALITY}, video is usually too blocky to use. "
                    "On-screen text and UI get hard to read."
                ),
            )
        else:
            self.video_hint.configure(
                style="Muted.TLabel",
                text=(
                    f"{DEFAULT_VIDEO_QUALITY} is a smaller-file default. Avoid going below "
                    f"{MIN_USEFUL_VIDEO_QUALITY} - on-screen text gets hard to read."
                ),
            )

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
        dlg.geometry("760x560")
        ttk.Label(
            dlg,
            text="Click a screen or window, the same way you share in a call.",
            style="Muted.TLabel",
        ).pack(anchor="w", padx=12, pady=(10, 4))

        wrap = ttk.Frame(dlg)
        wrap.pack(fill="both", expand=True, padx=12, pady=4)
        canvas = tk.Canvas(wrap, bg=BG, highlightthickness=0)
        scroll = ttk.Scrollbar(wrap, orient="vertical", command=canvas.yview)
        host = ttk.Frame(canvas)
        host.bind("<Configure>", lambda _e: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.create_window((0, 0), window=host, anchor="nw")
        canvas.configure(yscrollcommand=scroll.set)
        canvas.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")

        status = ttk.Label(dlg, text="Loading thumbnails...", style="Muted.TLabel")
        status.pack(anchor="w", padx=12)

        bar = ttk.Frame(dlg)
        bar.pack(fill="x", padx=12, pady=(4, 12))
        ttk.Button(bar, text="Click on screen...", command=lambda: click_screen()).pack(side="left")
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

            for w in (card, img, cap):
                w.bind("<Button-1>", on_click)

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
            status.configure(text=f"{len(dlg._photos)} sources  -  click one to record it")

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
            text="  Click the window to record   (Esc to cancel)  ",
            background=ACCENT,
            foreground="#fff",
            font=("Segoe UI Semibold", 12),
        ).pack(padx=8, pady=8)
        banner.update_idletasks()
        bw = banner.winfo_width()
        bh = banner.winfo_height()
        sw = banner.winfo_screenwidth()
        banner.geometry(f"+{(sw - bw) // 2}+12")
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
        self.pause_btn.configure(text="Resume" if self.session.paused else "Pause")

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

        try:
            audio_kbps = int(self.audio_kbps_var.get().strip())
        except ValueError:
            messagebox.showwarning("Recorder", "Audio kb/s must be a whole number you type, e.g. 48.")
            return
        if audio_kbps < AUDIO_KBPS_MIN or audio_kbps > AUDIO_KBPS_MAX:
            messagebox.showwarning(
                "Recorder",
                f"Audio kb/s must be a whole number between {AUDIO_KBPS_MIN} and {AUDIO_KBPS_MAX}.",
            )
            return
        if audio_kbps < MIN_USEFUL_AUDIO_KBPS:
            if not messagebox.askyesno(
                "Audio may be too thin",
                f"{audio_kbps} kb/s is below {MIN_USEFUL_AUDIO_KBPS} kb/s. "
                "Speech and UI sounds often become hard to use. Record anyway?",
            ):
                return

        try:
            quality = int(self.quality_var.get().strip())
        except ValueError:
            messagebox.showwarning(
                "Recorder", "Video quality must be a whole number you type, e.g. 45."
            )
            return
        if quality < VIDEO_QUALITY_MIN or quality > VIDEO_QUALITY_MAX:
            messagebox.showwarning(
                "Recorder",
                f"Video quality must be a whole number between {VIDEO_QUALITY_MIN} and {VIDEO_QUALITY_MAX}.",
            )
            return
        if quality < MIN_USEFUL_VIDEO_QUALITY:
            if not messagebox.askyesno(
                "Video may be too rough",
                f"Quality {quality} is below {MIN_USEFUL_VIDEO_QUALITY}. "
                "On-screen text and UI often become hard to read. Record anyway?",
            ):
                return
        qp = quality_to_qp(quality)

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
            qp=qp,
            audio_kbps=clamp_audio_kbps(audio_kbps),
            video_quality=clamp_video_quality(quality),
        )
        self.session = CaptureSession(cfg, self.hw, on_log=self._log)
        try:
            self.session.start()
        except Exception as exc:
            self.session = None
            messagebox.showerror("Record failed", str(exc))
            return
        self.record_btn.configure(text="■  Stop")
        self.pause_btn.configure(state="normal", text="Pause")
        try:
            self.win_list.configure(selectmode="none")
        except tk.TclError:
            pass
        self._tick()
        self._log(
            f"Recording {window.label()} @ {fps} fps, quality {quality} (qp {qp}), "
            f"Opus {audio_kbps} kb/s → {cfg.output.name}"
        )

    def _tick(self) -> None:
        if not self.session:
            self.time_label.configure(text="00:00:00")
            return
        sec = int(self.session.elapsed())
        h, rem = divmod(sec, 3600)
        m, s = divmod(rem, 60)
        suffix = "  paused" if self.session.paused else ""
        self.time_label.configure(text=f"{h:02d}:{m:02d}:{s:02d}{suffix}")
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
        self.record_btn.configure(text="●  Record", state="normal")
        self.pause_btn.configure(state="disabled", text="Pause")
        self.time_label.configure(text="00:00:00")
        try:
            self.win_list.configure(selectmode="browse")
        except tk.TclError:
            pass
        if err:
            messagebox.showerror("Recording finished with errors", err)
        elif path:
            messagebox.showinfo("Saved", f"Wrote:\n{path}")
        self.refresh_recordings(log=True)

    def _on_tab(self, _event: object | None = None) -> None:
        try:
            current = self.nb.index(self.nb.select())
        except tk.TclError:
            return
        if current == 1:
            self.refresh_recordings()

    def refresh_recordings(self, log: bool = False) -> None:
        folder = Path(self.dir_var.get().strip() or self.output_dir)
        files = list_recordings(folder)
        children = self.rec_list.get_children()
        if children:
            self.rec_list.delete(*children)
        self._rec_by_id = {}
        for i, path in enumerate(files):
            iid = f"rec-{i}"
            try:
                st = path.stat()
                size = format_size(st.st_size)
                modified = datetime.fromtimestamp(st.st_mtime).strftime("%Y-%m-%d %H:%M:%S")
            except OSError:
                size, modified = "?", ""
            self.rec_list.insert("", "end", iid=iid, values=(path.name, size, modified))
            self._rec_by_id[iid] = path
        if log:
            self._log(f"Recordings: {len(files)} file(s) in {folder}")

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
