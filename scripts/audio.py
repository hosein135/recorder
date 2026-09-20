#!/usr/bin/env python3
"""WASAPI loopback (internal) + microphone capture via SoundCard.

FFmpeg on Windows has no first-class WASAPI loopback demuxer, so system
audio is captured here and muxed after the video encode stops.
"""

from __future__ import annotations

import queue
import threading
import wave
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import numpy as np

try:
    import soundcard as sc
except ImportError:  # pragma: no cover - bootstrap installs this
    sc = None  # type: ignore[assignment]


AudioMode = Literal["internal", "external", "both"]
SAMPLE_RATE = 48000
CHUNK = 1024


@dataclass
class AudioDevice:
    id: str
    name: str
    is_loopback: bool

    def label(self) -> str:
        kind = "loopback" if self.is_loopback else "microphone"
        return f"{self.name}  ({kind})"


def _require_soundcard() -> None:
    if sc is None:
        raise RuntimeError(
            "Python package 'soundcard' is missing. Re-run run.cmd so pip can install requirements.txt."
        )


def list_microphones() -> list[AudioDevice]:
    _require_soundcard()
    out: list[AudioDevice] = []
    for mic in sc.all_microphones(include_loopback=False):
        out.append(AudioDevice(id=str(mic.id), name=str(mic.name), is_loopback=False))
    return out


def list_loopbacks() -> list[AudioDevice]:
    _require_soundcard()
    out: list[AudioDevice] = []
    for mic in sc.all_microphones(include_loopback=True):
        if getattr(mic, "isloopback", False):
            out.append(AudioDevice(id=str(mic.id), name=str(mic.name), is_loopback=True))
    return out


def default_microphone() -> AudioDevice | None:
    _require_soundcard()
    mic = sc.default_microphone()
    if mic is None:
        return None
    return AudioDevice(id=str(mic.id), name=str(mic.name), is_loopback=False)


def default_loopback() -> AudioDevice | None:
    _require_soundcard()
    speaker = sc.default_speaker()
    loopbacks = list_loopbacks()
    if speaker is not None:
        spk_id = str(speaker.id)
        spk_name = str(speaker.name)
        for lb in loopbacks:
            if spk_id and spk_id in lb.id:
                return lb
            if spk_name and spk_name in lb.name:
                return lb
    return loopbacks[0] if loopbacks else None


def _open_mic(device: AudioDevice):
    _require_soundcard()
    for mic in sc.all_microphones(include_loopback=device.is_loopback):
        if str(mic.id) == device.id:
            return mic
        if str(mic.name) == device.name:
            return mic
    raise RuntimeError(f"Audio device disappeared: {device.name}")


def _to_stereo(frames: np.ndarray) -> np.ndarray:
    if frames.ndim == 1:
        frames = frames.reshape(-1, 1)
    if frames.shape[1] == 1:
        return np.repeat(frames, 2, axis=1)
    if frames.shape[1] > 2:
        return frames[:, :2]
    return frames


def _pcm16(frames: np.ndarray) -> bytes:
    clipped = np.clip(_to_stereo(frames), -1.0, 1.0)
    return (clipped * 32767.0).astype(np.int16).tobytes()


class AudioRecorder:
    def __init__(
        self,
        mode: AudioMode,
        wav_path: Path,
        microphone: AudioDevice | None = None,
        loopback: AudioDevice | None = None,
    ) -> None:
        self.mode = mode
        self.wav_path = wav_path
        self.microphone = microphone
        self.loopback = loopback
        self._stop = threading.Event()
        self._pause = threading.Event()
        self._thread: threading.Thread | None = None
        self._error: str | None = None

    def start(self) -> None:
        self.wav_path.parent.mkdir(parents=True, exist_ok=True)
        self._thread = threading.Thread(target=self._run, name="wasapi-capture", daemon=True)
        self._thread.start()

    def set_paused(self, paused: bool) -> None:
        if paused:
            self._pause.set()
        else:
            self._pause.clear()

    def stop(self, timeout: float = 8.0) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=timeout)
        if self._error:
            raise RuntimeError(self._error)

    def _run(self) -> None:
        try:
            use_internal = self.mode in ("internal", "both")
            use_mic = self.mode in ("external", "both")
            lb = self.loopback or (default_loopback() if use_internal else None)
            mic = self.microphone or (default_microphone() if use_mic else None)
            if use_internal and lb is None:
                raise RuntimeError("No WASAPI loopback device (internal / speaker audio).")
            if use_mic and mic is None:
                raise RuntimeError("No microphone found.")

            sources: list[AudioDevice] = []
            if use_internal and lb is not None:
                sources.append(lb)
            if use_mic and mic is not None:
                sources.append(mic)

            queues = [queue.Queue(maxsize=32) for _ in sources]
            workers: list[threading.Thread] = []

            def _pump(device: AudioDevice, q: queue.Queue) -> None:
                try:
                    with _open_mic(device).recorder(
                        samplerate=SAMPLE_RATE, channels=2, blocksize=CHUNK
                    ) as rec:
                        while not self._stop.is_set():
                            frames = _to_stereo(
                                np.asarray(rec.record(numframes=CHUNK), dtype=np.float32)
                            )
                            try:
                                q.put(frames, timeout=0.5)
                            except queue.Full:
                                continue
                except Exception as exc:
                    self._error = str(exc)
                    self._stop.set()

            for device, q in zip(sources, queues):
                t = threading.Thread(target=_pump, args=(device, q), daemon=True)
                workers.append(t)
                t.start()

            with wave.open(str(self.wav_path), "wb") as wf:
                wf.setnchannels(2)
                wf.setsampwidth(2)
                wf.setframerate(SAMPLE_RATE)
                while not self._stop.is_set() or any(not q.empty() for q in queues):
                    chunks: list[np.ndarray] = []
                    timed_out = False
                    for q in queues:
                        try:
                            chunks.append(q.get(timeout=0.4))
                        except queue.Empty:
                            timed_out = True
                            break
                    if timed_out:
                        if self._stop.is_set():
                            break
                        continue
                    if self._pause.is_set():
                        continue
                    n = min(c.shape[0] for c in chunks)
                    mixed = chunks[0][:n].copy()
                    for extra in chunks[1:]:
                        mixed += extra[:n]
                    if len(chunks) > 1:
                        mixed *= 0.7
                    wf.writeframes(_pcm16(mixed))

            for t in workers:
                t.join(timeout=2.0)
        except Exception as exc:
            self._error = str(exc)
