#!/usr/bin/env python3
"""Build and run the FFmpeg capture/encode pipeline (H.266 / libvvenc)."""

from __future__ import annotations

import json
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
from hwnd_grab import WindowGrabber, grab_screen_bgra
from windows import WindowInfo, WindowRestore, prepare_for_capture, refresh_geometry, window_is_maximized, window_is_minimized

CREATE_NO_WINDOW = 0x08000000

DEFAULT_FPS = 5
MIN_USEFUL_FPS = 1
FPS_MIN = 1
FPS_MAX = 240

DEFAULT_AUDIO_KBPS = 24
MIN_USEFUL_AUDIO_KBPS = 24
AUDIO_KBPS_MIN = 8
AUDIO_KBPS_MAX = 256

DEFAULT_SAMPLE_RATE = 16000
MIN_USEFUL_SAMPLE_RATE = 16000
SAMPLE_RATE_MIN = 8000
SAMPLE_RATE_MAX = 48000
DEFAULT_SAMPLE_KHZ = DEFAULT_SAMPLE_RATE // 1000
MIN_USEFUL_SAMPLE_KHZ = MIN_USEFUL_SAMPLE_RATE // 1000
SAMPLE_KHZ_MIN = SAMPLE_RATE_MIN // 1000
SAMPLE_KHZ_MAX = SAMPLE_RATE_MAX // 1000
OPUS_SAMPLE_RATES = (8000, 12000, 16000, 24000, 48000)

DEFAULT_VIDEO_KBPS = 600
MIN_USEFUL_VIDEO_KBPS = 600
VIDEO_KBPS_MIN = 100
VIDEO_KBPS_MAX = 50000

DEFAULT_TOTAL_KBPS = DEFAULT_VIDEO_KBPS + DEFAULT_AUDIO_KBPS
MIN_USEFUL_TOTAL_KBPS = MIN_USEFUL_VIDEO_KBPS + MIN_USEFUL_AUDIO_KBPS
TOTAL_KBPS_MIN = VIDEO_KBPS_MIN + AUDIO_KBPS_MIN
TOTAL_KBPS_MAX = VIDEO_KBPS_MAX + AUDIO_KBPS_MAX


@dataclass
class RecordConfig:
    window: WindowInfo
    fps: int
    audio_mode: AudioMode
    output: Path
    microphone: AudioDevice | None = None
    loopback: AudioDevice | None = None
    audio_kbps: int = DEFAULT_AUDIO_KBPS
    sample_rate: int = DEFAULT_SAMPLE_RATE
    video_kbps: int = DEFAULT_VIDEO_KBPS


def clamp_audio_kbps(n: int) -> int:
    return max(AUDIO_KBPS_MIN, min(AUDIO_KBPS_MAX, int(n)))


def clamp_sample_rate(n: int) -> int:
    return max(SAMPLE_RATE_MIN, min(SAMPLE_RATE_MAX, int(n)))


def sample_khz_to_hz(khz: int) -> int:
    return clamp_sample_rate(int(khz) * 1000)


def snap_opus_rate(n: int) -> int:
    n = clamp_sample_rate(n)
    return min(OPUS_SAMPLE_RATES, key=lambda rate: abs(rate - n))


def clamp_video_kbps(n: int) -> int:
    return max(VIDEO_KBPS_MIN, min(VIDEO_KBPS_MAX, int(n)))


def clamp_total_kbps(n: int) -> int:
    return max(TOTAL_KBPS_MIN, min(TOTAL_KBPS_MAX, int(n)))


def _ffprobe(hw: HardwareProfile) -> str:
    ffmpeg = Path(_ffmpeg(hw))
    name = "ffprobe.exe" if ffmpeg.suffix.lower() == ".exe" else "ffprobe"
    sibling = ffmpeg.with_name(name)
    if sibling.is_file():
        return str(sibling)
    return shutil.which("ffprobe") or "ffprobe"


def probe_summary(hw: HardwareProfile, path: Path) -> str:
    """Human line from ffprobe so Activity/Recordings show real stream numbers."""
    try:
        proc = subprocess.run(
            [
                _ffprobe(hw),
                "-v",
                "error",
                "-show_entries",
                "format=duration,bit_rate,size:stream=codec_type,codec_name,width,height,r_frame_rate,avg_frame_rate,bit_rate,sample_rate,channels",
                "-of",
                "json",
                str(path),
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            creationflags=CREATE_NO_WINDOW if sys.platform == "win32" else 0,
        )
        if proc.returncode != 0 or not proc.stdout.strip():
            return ""
        data = json.loads(proc.stdout)
    except Exception:
        return ""
    fmt = data.get("format") or {}
    video = next((s for s in data.get("streams") or [] if s.get("codec_type") == "video"), {})
    audio = next((s for s in data.get("streams") or [] if s.get("codec_type") == "audio"), {})

    def _kbps(raw: object) -> str:
        try:
            n = int(str(raw))
        except (TypeError, ValueError):
            return "?"
        if n <= 0:
            return "?"
        return str(max(1, round(n / 1000)))

    w, h = video.get("width"), video.get("height")
    fps_raw = str(video.get("avg_frame_rate") or video.get("r_frame_rate") or "")
    fps = fps_raw
    if "/" in fps_raw:
        num, den = fps_raw.split("/", 1)
        try:
            fps = f"{int(num) / int(den):.2f}".rstrip("0").rstrip(".")
        except (TypeError, ValueError, ZeroDivisionError):
            fps = fps_raw
    size = fmt.get("size")
    try:
        size_txt = f"{int(size) / 1024:.0f} KB"
    except (TypeError, ValueError):
        size_txt = ""
    ar = audio.get("sample_rate")
    ar_txt = f"container {int(ar) / 1000:g} kHz" if str(ar).isdigit() else ""
    bits = [
        f"{w}x{h}" if w and h else "",
        f"{fps} fps" if fps else "",
        f"file {_kbps(fmt.get('bit_rate'))} kb/s" if fmt.get("bit_rate") else "",
        f"video {_kbps(video.get('bit_rate'))} kb/s" if video.get("bit_rate") else "",
        f"audio {_kbps(audio.get('bit_rate'))} kb/s" if audio.get("bit_rate") else "",
        ar_txt,
        size_txt,
    ]
    return "  |  ".join(p for p in bits if p)


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


def _vvenc_tail(hw: HardwareProfile, fps: int, video_kbps: int, video_path: Path) -> list[str]:
    kbps = clamp_video_kbps(video_kbps)
    # libvvenc defaults to -qp 32. Older FFmpeg wrappers then pass bitrate=0
    # (fixed QP) unless qp is -1. Without this, Data rate / Total never change the file.
    maxrate = kbps * 2
    return [
        "-vf",
        "scale=trunc(iw/2)*2:trunc(ih/2)*2,format=yuv420p10le",
        "-r",
        str(fps),
        "-c:v",
        "libvvenc",
        "-preset",
        hw.recommended_vvenc_preset(fps),
        "-qp",
        "-1",
        "-b:v",
        f"{kbps}k",
        "-maxrate",
        f"{maxrate}k",
        "-bufsize",
        f"{kbps * 2}k",
        "-qpa",
        "1",
        "-period",
        "1",
        "-pix_fmt",
        "yuv420p10le",
        "-tag:v",
        "vvc1",
        "-threads",
        str(hw.recommended_vvenc_threads()),
        "-an",
        str(video_path),
    ]


def build_hwnd_cmd(
    hw: HardwareProfile,
    width: int,
    height: int,
    fps: int,
    video_kbps: int,
    video_path: Path,
) -> list[str]:
    ffmpeg = _ffmpeg(hw)
    return [
        ffmpeg,
        "-y",
        "-hide_banner",
        "-loglevel",
        "info",
        "-stats",
        "-f",
        "rawvideo",
        "-pix_fmt",
        "bgra",
        "-s",
        f"{width}x{height}",
        "-framerate",
        str(fps),
        "-i",
        "-",
        *_vvenc_tail(hw, fps, video_kbps, video_path),
    ]


def build_video_cmd(
    cfg: RecordConfig,
    hw: HardwareProfile,
    video_path: Path,
) -> list[str]:
    ffmpeg = _ffmpeg(hw)
    win = refresh_geometry(cfg.window)
    fps = max(1, min(240, int(cfg.fps)))

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
        cmd += [
            "-f",
            "lavfi",
            "-i",
            f"ddagrab=0:framerate={fps}:draw_mouse=1,hwdownload,format=bgra",
        ]
    else:
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

    cmd += _vvenc_tail(hw, fps, cfg.video_kbps, video_path)
    return cmd


def build_mux_cmd(
    hw: HardwareProfile,
    video_path: Path,
    audio_path: Path,
    output: Path,
    audio_kbps: int = DEFAULT_AUDIO_KBPS,
    sample_rate: int = DEFAULT_SAMPLE_RATE,
    comment: str = "",
) -> list[str]:
    ffmpeg = _ffmpeg(hw)
    kbps = clamp_audio_kbps(audio_kbps)
    rate = snap_opus_rate(sample_rate)
    cmd = [
        ffmpeg,
        "-y",
        "-hide_banner",
        "-loglevel",
        "error",
        "-i",
        str(video_path),
        "-i",
        str(audio_path),
        "-map",
        "0:v:0",
        "-map",
        "1:a:0",
        "-c:v",
        "copy",
        "-filter:a",
        f"aresample={rate}",
        "-c:a",
        "libopus",
        "-application",
        "audio",
        "-vbr",
        "off",
        "-b:a",
        f"{kbps}k",
        "-ar",
        str(rate),
        "-shortest",
        "-movflags",
        "+faststart",
    ]
    if comment:
        cmd += ["-metadata", f"comment={comment}"]
    cmd.append(str(output))
    return cmd


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
        self.proc: subprocess.Popen[bytes] | None = None
        self.audio: AudioRecorder | None = None
        self.video_tmp = cfg.output.with_suffix(".video.tmp.mp4")
        self.audio_tmp = cfg.output.with_suffix(".audio.tmp.wav")
        self._stderr: list[str] = []
        self._err_thread: threading.Thread | None = None
        self.started_at: float | None = None
        self._paused = threading.Event()
        self._pause_started: float | None = None
        self._paused_total = 0.0
        self._hwnd_mode = not cfg.window.is_desktop
        self._restore: WindowRestore | None = None
        self._grabber: WindowGrabber | None = None
        self._grab_stop = threading.Event()
        self._grab_thread: threading.Thread | None = None
        self._frame_w = 0
        self._frame_h = 0
        self._screen_x = 0
        self._screen_y = 0
        self._win_state: str | None = None

    def start(self) -> None:
        if not self.hw.has_libvvenc:
            raise RecorderError(self.hw.vvenc_skip_reason or "libvvenc is not available")
        if not getattr(self.hw, "has_libopus", False):
            raise RecorderError(
                "FFmpeg has no libopus. Install the Gyan.FFmpeg *full* build via run.cmd."
            )

        self.cfg.output.parent.mkdir(parents=True, exist_ok=True)
        for p in (self.video_tmp, self.audio_tmp, self.cfg.output):
            if p.exists():
                p.unlink()

        fps = max(1, min(240, int(self.cfg.fps)))
        flags = CREATE_NO_WINDOW if sys.platform == "win32" else 0

        if self._hwnd_mode:
            if not self.cfg.window.hwnd:
                raise RecorderError("That window no longer exists.")
            self._restore = prepare_for_capture(self.cfg.window)
            geo = refresh_geometry(self.cfg.window)
            self._frame_w, self._frame_h = geo.even_size
            self._grabber = WindowGrabber(self.cfg.window.hwnd, self._frame_w, self._frame_h)
            if self._restore.was_minimized:
                self.on_log(
                    f"Window is minimized — capture continues at "
                    f"{self._frame_w}x{self._frame_h}. Restore it from the taskbar to see it."
                )
        else:
            geo = refresh_geometry(self.cfg.window)
            self._screen_x, self._screen_y = geo.x, geo.y
            self._frame_w, self._frame_h = geo.even_size

        cmd = build_hwnd_cmd(
            self.hw,
            self._frame_w,
            self._frame_h,
            fps,
            self.cfg.video_kbps,
            self.video_tmp,
        )
        self.on_log("FFmpeg: " + " ".join(cmd))

        self.audio = AudioRecorder(
            mode=self.cfg.audio_mode,
            wav_path=self.audio_tmp,
            microphone=self.cfg.microphone,
            loopback=self.cfg.loopback,
            sample_rate=snap_opus_rate(self.cfg.sample_rate),
        )
        self.audio.start()
        total = clamp_video_kbps(self.cfg.video_kbps) + clamp_audio_kbps(self.cfg.audio_kbps)
        self.on_log(
            f"Audio: {self.cfg.audio_mode} (WASAPI) -> Opus {self.cfg.audio_kbps} kb/s "
            f"{snap_opus_rate(self.cfg.sample_rate)} Hz | "
            f"video {self.cfg.video_kbps} kb/s | total {total} kb/s"
        )

        self.proc = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            bufsize=0,
            creationflags=flags,
        )
        self.started_at = time.time()
        self._paused_total = 0.0
        self._pause_started = None
        self._paused.clear()
        self._err_thread = threading.Thread(target=self._drain_stderr, daemon=True)
        self._err_thread.start()
        self._grab_thread = threading.Thread(
            target=self._grab_loop, args=(fps,), daemon=True, name="frame-grab"
        )
        self._grab_thread.start()
        if self._hwnd_mode:
            self.on_log(
                f"Window capture {self.cfg.window.exe or self.cfg.window.title} "
                f"hwnd={self.cfg.window.hwnd} {self._frame_w}x{self._frame_h} @ {fps} fps "
                f"(you can see it and min/max it)"
            )
        else:
            self.on_log(f"Entire screen {self._frame_w}x{self._frame_h} @ {fps} fps")

    def set_paused(self, paused: bool) -> None:
        if paused and not self._paused.is_set():
            self._paused.set()
            self._pause_started = time.time()
            if self.audio:
                self.audio.set_paused(True)
            self.on_log("Paused")
        elif not paused and self._paused.is_set():
            if self._pause_started is not None:
                self._paused_total += time.time() - self._pause_started
            self._pause_started = None
            self._paused.clear()
            if self.audio:
                self.audio.set_paused(False)
            self.on_log("Resumed")

    @property
    def paused(self) -> bool:
        return self._paused.is_set()

    def _grab_loop(self, fps: int) -> None:
        period = 1.0 / max(1, fps)
        assert self.proc and self.proc.stdin
        stdin = self.proc.stdin
        next_t = time.perf_counter()
        try:
            while not self._grab_stop.is_set():
                if self.proc.poll() is not None:
                    break
                if self._paused.is_set():
                    next_t = time.perf_counter() + period
                    self._grab_stop.wait(0.05)
                    continue
                if self._hwnd_mode:
                    assert self._grabber is not None
                    if self._restore is not None:
                        self._restore.ensure_composing()
                    frame, state = self._grabber.grab()
                    if self._restore is not None and self._restore.capture_hidden:
                        state = "minimized"
                    elif window_is_maximized(self.cfg.window.hwnd):
                        state = "maximized"
                    elif not window_is_minimized(self.cfg.window.hwnd):
                        state = "open"
                    if state != self._win_state:
                        prev = self._win_state
                        self._win_state = state
                        if prev is None:
                            pass
                        elif state == "minimized":
                            self.on_log(
                                "Window minimized — still recording. Click the taskbar to show it again."
                            )
                        elif state == "maximized":
                            self.on_log(
                                "Window maximized - capture continues (letterboxed to recording size)."
                            )
                        elif state == "open":
                            self.on_log("Window restored — you can see it; capture is live.")
                else:
                    frame = grab_screen_bgra(
                        self._screen_x,
                        self._screen_y,
                        self._frame_w,
                        self._frame_h,
                        self._frame_w,
                        self._frame_h,
                    )
                stdin.write(frame)
                stdin.flush()
                next_t += period
                delay = next_t - time.perf_counter()
                if delay > 0:
                    self._grab_stop.wait(delay)
                else:
                    next_t = time.perf_counter()
        except (BrokenPipeError, OSError) as exc:
            self.on_log(f"Grab stopped: {exc}")
        except Exception as exc:
            self.on_log(f"Grab error: {exc}")
        finally:
            try:
                stdin.close()
            except OSError:
                pass

    def _drain_stderr(self) -> None:
        assert self.proc and self.proc.stderr
        for raw in self.proc.stderr:
            text = raw.decode("utf-8", errors="replace").rstrip()
            if not text:
                continue
            self._stderr.append(text)
            if len(self._stderr) > 200:
                self._stderr = self._stderr[-100:]
            if "time=" in text or "error" in text.lower() or "failed" in text.lower():
                self.on_log(text)

    def elapsed(self) -> float:
        if not self.started_at:
            return 0.0
        extra = self._paused_total
        if self._paused.is_set() and self._pause_started is not None:
            extra += time.time() - self._pause_started
        return max(0.0, time.time() - self.started_at - extra)

    def stop(self) -> Path:
        if self._paused.is_set():
            self.set_paused(False)
        self._grab_stop.set()
        if self._grab_thread:
            self._grab_thread.join(timeout=5)
        if self._grabber is not None:
            try:
                self._grabber.close()
            except Exception:
                pass
            self._grabber = None

        if self._restore is not None:
            try:
                self._restore.revert()
            except Exception as exc:
                self.on_log(f"Could not restore window state: {exc}")
            self._restore = None

        if self.proc and self.proc.poll() is None:
            try:
                if self.proc.stdin and not self.proc.stdin.closed:
                    self.proc.stdin.close()
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

        mux = build_mux_cmd(
            self.hw,
            self.video_tmp,
            self.audio_tmp,
            self.cfg.output,
            self.cfg.audio_kbps,
            self.cfg.sample_rate,
            comment=(
                f"{self.cfg.fps} fps, video {self.cfg.video_kbps} kb/s, "
                f"audio {self.cfg.audio_kbps} kb/s, "
                f"{snap_opus_rate(self.cfg.sample_rate) // 1000} kHz Opus, "
                f"total {clamp_video_kbps(self.cfg.video_kbps) + clamp_audio_kbps(self.cfg.audio_kbps)} kb/s"
            ),
        )
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
            # Keep the VVC video even if Opus mux fails.
            fallback = self.cfg.output.with_suffix(".video-only.mp4")
            self.video_tmp.replace(fallback)
            raise RecorderError(
                f"Video encoded but audio mux failed. Video-only file: {fallback}\n{mux_proc.stderr}"
            )

        self.video_tmp.unlink(missing_ok=True)
        self.audio_tmp.unlink(missing_ok=True)
        self.on_log(f"Wrote {self.cfg.output}")
        summary = probe_summary(self.hw, self.cfg.output)
        if summary:
            self.on_log("File: " + summary)
        return self.cfg.output
