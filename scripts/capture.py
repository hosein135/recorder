#!/usr/bin/env python3
"""Build and run the FFmpeg capture/encode pipeline (H.266 / libvvenc)."""

from __future__ import annotations

import shutil
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from audio import AudioDevice, AudioMode, AudioRecorder
from hw_detect import HardwareProfile
from windows import WindowInfo, refresh_geometry

CREATE_NO_WINDOW = 0x08000000


@dataclass
class RecordConfig:
    window: WindowInfo
    fps: int
    audio_mode: AudioMode
    output: Path
    microphone: AudioDevice | None = None
    loopback: AudioDevice | None = None
    qp: int = 32


class RecorderError(RuntimeError):
    pass


def _ffmpeg(hw: HardwareProfile) -> str:
    path = hw.ffmpeg_path or shutil.which("ffmpeg") or "ffmpeg"
    return path


def _sanitize_stem(title: str) -> str:
    keep = []
    for ch in title.strip() or "window":
        if ch.isalnum() or ch in ("-", "_", "."):
            keep.append(ch)
        elif ch.isspace():
            keep.append("_")
    stem = "".join(keep).strip("._") or "window"
    return stem[:60]


def default_output_path(root: Path, window: WindowInfo) -> Path:
    stamp = time.strftime("%Y%m%d_%H%M%S")
    name = "desktop" if window.is_desktop else _sanitize_stem(window.title)
    return root / f"{name}_{stamp}.mp4"


def build_video_cmd(
    cfg: RecordConfig,
    hw: HardwareProfile,
    video_path: Path,
) -> list[str]:
    ffmpeg = _ffmpeg(hw)
    win = refresh_geometry(cfg.window)
    fps = max(1, min(120, int(cfg.fps)))
    preset = hw.recommended_vvenc_preset(fps)
    threads = str(hw.recommended_vvenc_threads())
    w, h = win.even_size

    cmd: list[str] = [
        ffmpeg,
        "-y",
        "-hide_banner",
        "-loglevel",
        "info",
        "-stats",
        "-thread_queue_size",
        "64",
    ]

    if win.is_desktop and hw.has_ddagrab:
        # DXGI Desktop Duplication — GPU copies the composed desktop.
        cmd += [
            "-f",
            "lavfi",
            "-i",
            f"ddagrab=0:framerate={fps}:draw_mouse=1,hwdownload,format=bgra",
        ]
    elif win.is_desktop:
        cmd += [
            "-f",
            "gdigrab",
            "-framerate",
            str(fps),
            "-draw_mouse",
            "1",
            "-i",
            "desktop",
        ]
    else:
        # Crop the desktop to the window rect so GPU-composited content
        # (D3D/UWP) is visible. title= follows a moving window but often
        # records a black frame for hardware-accelerated clients.
        cmd += [
            "-f",
            "gdigrab",
            "-framerate",
            str(fps),
            "-offset_x",
            str(win.x),
            "-offset_y",
            str(win.y),
            "-video_size",
            f"{w}x{h}",
            "-show_region",
            "1",
            "-draw_mouse",
            "1",
            "-i",
            "desktop",
        ]

    vf = "scale=trunc(iw/2)*2:trunc(ih/2)*2,format=yuv420p10le"
    cmd += [
        "-vf",
        vf,
        "-r",
        str(fps),
        "-c:v",
        "libvvenc",
        "-preset",
        preset,
        "-qp",
        str(cfg.qp),
        "-qpa",
        "1",
        "-period",
        "1",
        "-pix_fmt",
        "yuv420p10le",
        "-tag:v",
        "vvc1",
        "-threads",
        threads,
        "-an",
        str(video_path),
    ]
    return cmd


def build_mux_cmd(
    hw: HardwareProfile,
    video_path: Path,
    audio_path: Path,
    output: Path,
) -> list[str]:
    ffmpeg = _ffmpeg(hw)
    return [
        ffmpeg,
        "-y",
        "-hide_banner",
        "-loglevel",
        "error",
        "-i",
        str(video_path),
        "-i",
        str(audio_path),
        "-c:v",
        "copy",
        "-c:a",
        "aac",
        "-b:a",
        "192k",
        "-shortest",
        "-movflags",
        "+faststart",
        str(output),
    ]


class CaptureSession:
    def __init__(
        self,
        cfg: RecordConfig,
        hw: HardwareProfile,
        on_log: Callable[[str], None] | None = None,
    ) -> None:
        self.cfg = cfg
        self.hw = hw
        self.on_log = on_log or (lambda _m: None)
        self.proc: subprocess.Popen[str] | None = None
        self.audio: AudioRecorder | None = None
        self.video_tmp = cfg.output.with_suffix(".video.tmp.mp4")
        self.audio_tmp = cfg.output.with_suffix(".audio.tmp.wav")
        self._stderr: list[str] = []
        self._err_thread: threading.Thread | None = None
        self.started_at: float | None = None

    def start(self) -> None:
        if not self.hw.has_libvvenc:
            raise RecorderError(self.hw.vvenc_skip_reason or "libvvenc is not available")
        desktop_ok = self.cfg.window.is_desktop and self.hw.has_ddagrab
        if not self.hw.has_gdigrab and not desktop_ok:
            raise RecorderError(self.hw.capture_skip_reason or "gdigrab is not available")

        self.cfg.output.parent.mkdir(parents=True, exist_ok=True)
        for p in (self.video_tmp, self.audio_tmp, self.cfg.output):
            if p.exists():
                p.unlink()

        cmd = build_video_cmd(self.cfg, self.hw, self.video_tmp)
        self.on_log("FFmpeg: " + " ".join(cmd))

        # WASAPI first so the wav timeline covers the FFmpeg startup gap.
        self.audio = AudioRecorder(
            mode=self.cfg.audio_mode,
            wav_path=self.audio_tmp,
            microphone=self.cfg.microphone,
            loopback=self.cfg.loopback,
        )
        self.audio.start()
        self.on_log(f"Audio: {self.cfg.audio_mode} (WASAPI)")

        flags = CREATE_NO_WINDOW if sys.platform == "win32" else 0
        self.proc = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            creationflags=flags,
        )
        self.started_at = time.time()
        self._err_thread = threading.Thread(target=self._drain_stderr, daemon=True)
        self._err_thread.start()

    def _drain_stderr(self) -> None:
        assert self.proc and self.proc.stderr
        for line in self.proc.stderr:
            text = line.rstrip()
            if not text:
                continue
            self._stderr.append(text)
            if len(self._stderr) > 200:
                self._stderr = self._stderr[-100:]
            # Keep the GUI noise down — stats lines only.
            if "time=" in text or "error" in text.lower() or "failed" in text.lower():
                self.on_log(text)

    def elapsed(self) -> float:
        if not self.started_at:
            return 0.0
        return time.time() - self.started_at

    def stop(self) -> Path:
        if self.proc and self.proc.poll() is None:
            try:
                if self.proc.stdin:
                    self.proc.stdin.write("q")
                    self.proc.stdin.flush()
            except (BrokenPipeError, OSError):
                pass
            try:
                self.proc.wait(timeout=20)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait(timeout=5)

        audio_err: str | None = None
        if self.audio:
            try:
                self.audio.stop()
            except Exception as exc:
                audio_err = str(exc)

        code = self.proc.returncode if self.proc else 1
        # 255 is ffmpeg's usual code after 'q'; 0 is clean EOF.
        if code not in (0, 255, None) and not (self.video_tmp.is_file() and self.video_tmp.stat().st_size > 0):
            tail = "\n".join(self._stderr[-20:])
            raise RecorderError(f"FFmpeg exited {code}\n{tail}")

        if not self.video_tmp.is_file() or self.video_tmp.stat().st_size == 0:
            tail = "\n".join(self._stderr[-20:])
            raise RecorderError(f"No video was written.\n{tail}")

        if audio_err:
            self.on_log(f"Audio warning: {audio_err} — saving video only")
            self.video_tmp.replace(self.cfg.output)
            return self.cfg.output

        if not self.audio_tmp.is_file() or self.audio_tmp.stat().st_size < 128:
            self.on_log("Audio file empty — saving video only")
            self.video_tmp.replace(self.cfg.output)
            return self.cfg.output

        mux = build_mux_cmd(self.hw, self.video_tmp, self.audio_tmp, self.cfg.output)
        self.on_log("Mux: " + " ".join(mux))
        flags = CREATE_NO_WINDOW if sys.platform == "win32" else 0
        mux_proc = subprocess.run(
            mux,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            creationflags=flags,
        )
        if mux_proc.returncode != 0:
            self.on_log(mux_proc.stderr.strip() or "mux failed")
            # Keep the VVC video even if AAC mux fails.
            fallback = self.cfg.output.with_suffix(".video-only.mp4")
            self.video_tmp.replace(fallback)
            raise RecorderError(
                f"Video encoded but audio mux failed. Video-only file: {fallback}\n{mux_proc.stderr}"
            )

        self.video_tmp.unlink(missing_ok=True)
        self.audio_tmp.unlink(missing_ok=True)
        self.on_log(f"Wrote {self.cfg.output}")
        return self.cfg.output
