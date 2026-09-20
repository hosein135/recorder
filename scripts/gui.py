#!/usr/bin/env python3
"""Tkinter GUI: pick a window, FPS, internal/mic/both audio, record H.266/VVC."""

from __future__ import annotations

import sys
import threading
import traceback
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
from capture import CaptureSession, RecordConfig, RecorderError, default_output_path
from hw_detect import HardwareProfile, detect, format_involvement_report
from windows import WindowInfo, list_windows

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
        self.geometry("920x640")
        self.minsize(780, 560)
        self.configure(bg=BG)

        self.hw: HardwareProfile | None = None
        self.windows: list[WindowInfo] = []
        self.mics: list[AudioDevice] = []
        self.loopbacks: list[AudioDevice] = []
        self.session: CaptureSession | None = None
        self._tick_job: str | None = None
        self.output_dir = _ROOT / "recordings"

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

    def _build(self) -> None:
        pad = {"padx": 14, "pady": 8}

        header = ttk.Frame(self)
        header.pack(fill="x", **pad)
        ttk.Label(header, text="Window Recorder", style="Title.TLabel").pack(side="left")
        ttk.Label(header, text="FFmpeg libvvenc  ·  H.266 / VVC", style="Muted.TLabel").pack(
            side="left", padx=(12, 0)
        )

        body = ttk.Frame(self)
        body.pack(fill="both", expand=True, padx=14)

        left = ttk.LabelFrame(body, text="Open windows")
        left.pack(side="left", fill="both", expand=True, padx=(0, 8))

        btns = ttk.Frame(left)
        btns.pack(fill="x", padx=8, pady=(8, 4))
        ttk.Button(btns, text="Refresh", command=self.refresh_windows).pack(side="left")

        self.win_list = tk.Listbox(
            left,
            bg=PANEL,
            fg=FG,
            selectbackground=ACCENT,
            selectforeground="#fff",
            relief="flat",
            highlightthickness=1,
            highlightbackground=BORDER,
            font=("Segoe UI", 10),
            activestyle="none",
        )
        scroll = ttk.Scrollbar(left, command=self.win_list.yview)
        self.win_list.configure(yscrollcommand=scroll.set)
        self.win_list.pack(side="left", fill="both", expand=True, padx=(8, 0), pady=(0, 8))
        scroll.pack(side="right", fill="y", pady=(0, 8), padx=(0, 8))

        right = ttk.Frame(body)
        right.pack(side="right", fill="y")

        rec = ttk.LabelFrame(right, text="Capture")
        rec.pack(fill="x")

        row = ttk.Frame(rec)
        row.pack(fill="x", padx=10, pady=(10, 4))
        ttk.Label(row, text="FPS").pack(side="left")
        self.fps_var = tk.StringVar(value="30")
        fps = ttk.Combobox(
            row,
            textvariable=self.fps_var,
            values=("15", "24", "25", "30", "60"),
            width=8,
            state="normal",
        )
        fps.pack(side="left", padx=(8, 0))

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
        self.time_label = ttk.Label(actions, text="00:00:00", style="Title.TLabel")
        self.time_label.pack(side="left", padx=16)

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
        self.refresh_windows()
        self.refresh_audio()

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
        try:
            self.windows = list_windows()
        except Exception as exc:
            self._log(f"Window list failed: {exc}")
            return
        self.win_list.delete(0, "end")
        for w in self.windows:
            self.win_list.insert("end", w.label())
        if self.windows:
            self.win_list.selection_set(0)

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

    def _selected_window(self) -> WindowInfo | None:
        sel = self.win_list.curselection()
        if not sel:
            return None
        idx = int(sel[0])
        if 0 <= idx < len(self.windows):
            return self.windows[idx]
        return None

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
        window = self._selected_window()
        if window is None:
            messagebox.showwarning("Recorder", "Select a window (or Entire screen).")
            return
        try:
            fps = int(self.fps_var.get())
        except ValueError:
            messagebox.showwarning("Recorder", "FPS must be a whole number.")
            return
        if fps < 1 or fps > 120:
            messagebox.showwarning("Recorder", "FPS must be between 1 and 120.")
            return

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
        )
        self.session = CaptureSession(cfg, self.hw, on_log=self._log)
        try:
            self.session.start()
        except Exception as exc:
            self.session = None
            messagebox.showerror("Record failed", str(exc))
            return
        self.record_btn.configure(text="■  Stop")
        self.win_list.configure(state="disabled")
        self._tick()
        self._log(f"Recording {window.label()} @ {fps} fps → {cfg.output.name}")

    def _tick(self) -> None:
        if not self.session:
            self.time_label.configure(text="00:00:00")
            return
        sec = int(self.session.elapsed())
        h, rem = divmod(sec, 3600)
        m, s = divmod(rem, 60)
        self.time_label.configure(text=f"{h:02d}:{m:02d}:{s:02d}")
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
        self.win_list.configure(state="normal")
        if err:
            messagebox.showerror("Recording finished with errors", err)
        elif path:
            messagebox.showinfo("Saved", f"Wrote:\n{path}")

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
