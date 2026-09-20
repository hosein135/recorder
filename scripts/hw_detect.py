#!/usr/bin/env python3
"""Runtime hardware + FFmpeg capability detection — never hardcodes host topology.

Adapted from ../animation1/scripts/hw_detect.py:
  live Win32 adapters, nvidia-smi, encoder *probe* (not merely listed),
  DXGI vs GDI capture, CPU thread budget for libvvenc.

H.266 / VVC: consumer NVIDIA NVENC and Intel QSV do not encode VVC.
The recorder always encodes with libvvenc (CPU). A GPU may still be used
for DXGI Desktop Duplication capture when FFmpeg's ddagrab filter exists.
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class HardwareProfile:
    cpu_count: int
    cpu_name: str = "Unknown CPU"
    nvidia_gpus: list[str] = field(default_factory=list)
    intel_gpus: list[str] = field(default_factory=list)
    other_gpus: list[str] = field(default_factory=list)
    has_cuda: bool = False
    has_nvenc: bool = False
    has_qsv: bool = False
    has_libvvenc: bool = False
    has_gdigrab: bool = False
    has_ddagrab: bool = False
    has_dshow: bool = False
    ffmpeg_path: str | None = None
    ffmpeg_version: str = ""
    nvidia_smi: str | None = None
    nvenc_skip_reason: str | None = None
    qsv_skip_reason: str | None = None
    vvenc_skip_reason: str | None = None
    capture_skip_reason: str | None = None

    def recommended_vvenc_preset(self, fps: int) -> str:
        """Pick a live-encode VVenC preset from CPU + target fps."""
        threads = max(1, self.cpu_count)
        if fps >= 50 and threads < 12:
            return "faster"
        if fps >= 30 and threads < 8:
            return "faster"
        if threads >= 16:
            return "fast"
        return "faster"

    def recommended_vvenc_threads(self) -> int:
        return max(1, self.cpu_count - 1)

    def summary(self) -> str:
        nvidia = ", ".join(self.nvidia_gpus) if self.nvidia_gpus else "none"
        intel = ", ".join(self.intel_gpus) if self.intel_gpus else "none"
        return (
            f"CPU={self.cpu_name} ({self.cpu_count} threads) | "
            f"NVIDIA=[{nvidia}] IntelGPU=[{intel}] "
            f"libvvenc={self.has_libvvenc} ddagrab={self.has_ddagrab} "
            f"gdigrab={self.has_gdigrab}"
        )


def _run(cmd: list[str], timeout: float = 12.0) -> subprocess.CompletedProcess[str] | None:
    try:
        return subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
            encoding="utf-8",
            errors="replace",
        )
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return None


def _ffmpeg_encoders(ffmpeg: str) -> set[str]:
    proc = _run([ffmpeg, "-hide_banner", "-encoders"])
    if not proc or proc.returncode != 0:
        return set()
    names: set[str] = set()
    for line in proc.stdout.splitlines():
        m = re.match(r"^\s*\S+\s+(\S+)\s+", line)
        if m:
            names.add(m.group(1))
    return names


def _ffmpeg_devices(ffmpeg: str) -> set[str]:
    proc = _run([ffmpeg, "-hide_banner", "-devices"])
    if not proc or proc.returncode != 0:
        return set()
    names: set[str] = set()
    blob = (proc.stdout or "") + "\n" + (proc.stderr or "")
    for line in blob.splitlines():
        m = re.match(r"^\s*[DEd]+\s+(\S+)\s+", line)
        if m:
            names.add(m.group(1))
    return names


def _ffmpeg_filters(ffmpeg: str) -> set[str]:
    proc = _run([ffmpeg, "-hide_banner", "-filters"])
    if not proc or proc.returncode != 0:
        return set()
    names: set[str] = set()
    for line in proc.stdout.splitlines():
        m = re.match(r"^\s*\S+\s+(\S+)\s+", line)
        if m:
            names.add(m.group(1))
    return names


def _ffmpeg_version_line(ffmpeg: str) -> str:
    proc = _run([ffmpeg, "-version"])
    if not proc or not proc.stdout:
        return ""
    return proc.stdout.splitlines()[0].strip()


def _nvidia_gpus(nvidia_smi: str) -> list[str]:
    proc = _run([nvidia_smi, "-L"])
    if not proc or proc.returncode != 0:
        return []
    return [line.strip() for line in proc.stdout.splitlines() if line.strip().startswith("GPU ")]


def _probe_encoder(ffmpeg: str, codec: str, extra: list[str], pix_fmt: str | None = None) -> bool:
    """True only if FFmpeg can open the encoder on this machine (not merely list it)."""
    with tempfile.TemporaryDirectory(prefix="rec_enc_probe_") as tmp:
        out = Path(tmp) / "probe.mkv"
        cmd = [
            ffmpeg,
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            "color=c=black:s=128x128:d=0.4:r=10",
            "-frames:v",
            "4",
        ]
        if pix_fmt:
            cmd += ["-pix_fmt", pix_fmt]
        cmd += ["-c:v", codec, *extra, "-f", "matroska", "-y", str(out)]
        proc = _run(cmd, timeout=40.0)
        if not proc or proc.returncode != 0:
            return False
        return out.is_file() and out.stat().st_size > 0


def _cpu_name() -> str:
    if sys.platform == "win32":
        proc = _run(
            [
                "powershell",
                "-NoProfile",
                "-Command",
                "(Get-CimInstance Win32_Processor | Select-Object -First 1 -ExpandProperty Name)",
            ]
        )
        if proc and proc.returncode == 0 and proc.stdout.strip():
            return re.sub(r"\s+", " ", proc.stdout.strip())
    return platform.processor() or platform.machine() or "Unknown CPU"


def _windows_video_controllers() -> list[str]:
    if sys.platform != "win32":
        return []
    proc = _run(
        [
            "powershell",
            "-NoProfile",
            "-Command",
            "Get-CimInstance Win32_VideoController | ForEach-Object { $_.Name }",
        ]
    )
    if not proc or proc.returncode != 0:
        return []
    return [line.strip() for line in proc.stdout.splitlines() if line.strip()]


def _classify_adapters(
    names: list[str], nvidia_from_smi: list[str]
) -> tuple[list[str], list[str], list[str]]:
    nvidia: list[str] = list(nvidia_from_smi)
    intel: list[str] = []
    other: list[str] = []
    seen_nvidia = {n.lower() for n in nvidia}

    for name in names:
        low = name.lower()
        if re.search(r"(?i)nvidia|geforce|quadro|rtx |gtx ", name):
            if name.lower() not in seen_nvidia and not any(
                name.lower() in s.lower() for s in seen_nvidia
            ):
                nvidia.append(name)
                seen_nvidia.add(name.lower())
            continue
        if "intel" in low:
            intel.append(name)
            continue
        other.append(name)
    return nvidia, intel, other


def detect() -> HardwareProfile:
    cpu = os.cpu_count() or 1
    profile = HardwareProfile(
        cpu_count=cpu,
        cpu_name=_cpu_name(),
        ffmpeg_path=shutil.which("ffmpeg"),
        nvidia_smi=shutil.which("nvidia-smi"),
    )

    smi_gpus: list[str] = []
    if profile.nvidia_smi:
        smi_gpus = _nvidia_gpus(profile.nvidia_smi)
        profile.has_cuda = bool(smi_gpus)
    else:
        profile.nvenc_skip_reason = "nvidia-smi not found (NVIDIA driver not on PATH)"

    adapters = _windows_video_controllers()
    nvidia, intel, other = _classify_adapters(adapters, smi_gpus)
    if not adapters and smi_gpus:
        nvidia = smi_gpus
    profile.nvidia_gpus = nvidia
    profile.intel_gpus = intel
    profile.other_gpus = other
    if profile.nvidia_gpus:
        profile.has_cuda = True

    if not profile.ffmpeg_path:
        profile.vvenc_skip_reason = "ffmpeg not found on PATH"
        profile.capture_skip_reason = "ffmpeg not found on PATH"
        profile.qsv_skip_reason = "ffmpeg not found on PATH"
        return profile

    profile.ffmpeg_version = _ffmpeg_version_line(profile.ffmpeg_path)
    enc = _ffmpeg_encoders(profile.ffmpeg_path)
    devices = _ffmpeg_devices(profile.ffmpeg_path)
    filters = _ffmpeg_filters(profile.ffmpeg_path)

    profile.has_gdigrab = "gdigrab" in devices
    profile.has_dshow = "dshow" in devices
    profile.has_ddagrab = "ddagrab" in filters
    if not profile.has_gdigrab and not profile.has_ddagrab:
        profile.capture_skip_reason = "FFmpeg build has neither gdigrab nor ddagrab"

    if "libvvenc" in enc:
        profile.has_libvvenc = _probe_encoder(
            profile.ffmpeg_path,
            "libvvenc",
            ["-preset", "faster", "-qp", "40"],
            pix_fmt="yuv420p10le",
        )
        if not profile.has_libvvenc:
            profile.vvenc_skip_reason = (
                "FFmpeg lists libvvenc but the VVC encode probe failed "
                "(need Gyan *full* build, not essentials)"
            )
    else:
        profile.vvenc_skip_reason = (
            "FFmpeg build has no libvvenc — install Gyan.FFmpeg full "
            "(essentials builds omit H.266)"
        )

    if "h264_nvenc" in enc and profile.nvidia_gpus:
        profile.has_nvenc = _probe_encoder(
            profile.ffmpeg_path,
            "h264_nvenc",
            ["-preset", "p4", "-cq", "28", "-b:v", "0"],
        )
        if not profile.has_nvenc:
            profile.nvenc_skip_reason = "h264_nvenc listed but probe failed"
    elif profile.nvidia_gpus and "h264_nvenc" not in enc:
        profile.nvenc_skip_reason = "FFmpeg build has no h264_nvenc"
    elif not profile.nvidia_gpus:
        profile.nvenc_skip_reason = profile.nvenc_skip_reason or "No NVIDIA GPU detected"

    if "h264_qsv" in enc:
        profile.has_qsv = _probe_encoder(
            profile.ffmpeg_path,
            "h264_qsv",
            ["-vf", "format=nv12", "-global_quality", "28", "-look_ahead", "0"],
        )
        if not profile.has_qsv:
            profile.qsv_skip_reason = "h264_qsv listed but probe failed"
    elif profile.intel_gpus:
        profile.qsv_skip_reason = "FFmpeg build has no h264_qsv"
    else:
        profile.qsv_skip_reason = "No Intel GPU detected"

    return profile


def involvement_rows(hw: HardwareProfile, fps: int = 30) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    preset = hw.recommended_vvenc_preset(fps)

    nvidia_label = "External / discrete NVIDIA GPU"
    if not hw.nvidia_gpus:
        rows.append(
            {
                "device": nvidia_label,
                "status": "NOT involved",
                "detail": hw.nvenc_skip_reason or "No NVIDIA GPU detected",
            }
        )
    else:
        names = "; ".join(hw.nvidia_gpus)
        if hw.has_ddagrab:
            detail = (
                f"{names} | DXGI Desktop Duplication (ddagrab) may copy frames on this GPU. "
                f"NVENC cannot encode H.266/VVC — encode stays on libvvenc."
            )
            status = "INVOLVED (capture only)"
        else:
            detail = (
                f"{names} | not used. NVENC has no H.266 encoder; "
                f"this FFmpeg build also has no ddagrab capture filter."
            )
            status = "NOT involved"
        rows.append({"device": nvidia_label, "status": status, "detail": detail})

    intel_label = "Internal Intel GPU (iGPU / Arc)"
    if not hw.intel_gpus:
        rows.append(
            {
                "device": intel_label,
                "status": "NOT involved",
                "detail": hw.qsv_skip_reason or "No Intel GPU adapter detected",
            }
        )
    else:
        names = "; ".join(hw.intel_gpus)
        detail = (
            f"{names} | Quick Sync can decode VVC on some Arc/Lunar Lake chips "
            f"but does not encode VVC. Encode is libvvenc on the CPU."
        )
        if hw.has_ddagrab:
            detail += " DXGI capture may run on this adapter."
            status = "INVOLVED (capture only)"
        else:
            status = "NOT involved"
        rows.append({"device": intel_label, "status": status, "detail": detail})

    is_intel_cpu = "intel" in hw.cpu_name.lower()
    cpu_label = "Intel CPU" if is_intel_cpu else f"CPU ({hw.cpu_name.split()[0] if hw.cpu_name else 'host'})"
    vvenc = (
        f"H.266 encode via libvvenc preset={preset} threads≈{hw.recommended_vvenc_threads()}"
        if hw.has_libvvenc
        else f"libvvenc UNAVAILABLE — {hw.vvenc_skip_reason}"
    )
    grab = "gdigrab (GDI window grab)" if hw.has_gdigrab else "no gdigrab"
    if hw.has_ddagrab:
        grab += " + ddagrab (DXGI) fallback"
    cpu_roles = [
        f"{hw.cpu_name} — {hw.cpu_count} logical threads",
        "GUI orchestration",
        grab,
        "WASAPI loopback / microphone mix",
        vvenc,
    ]
    rows.append(
        {
            "device": cpu_label,
            "status": "INVOLVED",
            "detail": " | ".join(cpu_roles),
        }
    )

    if hw.other_gpus:
        rows.append(
            {
                "device": "Other GPU adapter(s)",
                "status": "Detected only",
                "detail": "; ".join(hw.other_gpus),
            }
        )
    return rows


def format_involvement_report(hw: HardwareProfile, fps: int = 30) -> str:
    lines = [
        "=== Hardware inventory ===",
        f"  CPU:    {hw.cpu_name} ({hw.cpu_count} threads)",
        f"  NVIDIA: {', '.join(hw.nvidia_gpus) if hw.nvidia_gpus else '(none)'}",
        f"  Intel:  {', '.join(hw.intel_gpus) if hw.intel_gpus else '(none)'}",
    ]
    if hw.other_gpus:
        lines.append(f"  Other:  {', '.join(hw.other_gpus)}")
    lines += [
        f"  FFmpeg: {hw.ffmpeg_version or hw.ffmpeg_path or '(missing)'}",
        f"  Caps:   libvvenc={hw.has_libvvenc} gdigrab={hw.has_gdigrab} "
        f"ddagrab={hw.has_ddagrab} NVENC={hw.has_nvenc} QSV={hw.has_qsv}",
        f"  Plan:   capture window via GDI; encode H.266 with libvvenc "
        f"({hw.recommended_vvenc_preset(fps)})",
        "=== Involvement (this recorder) ===",
    ]
    for row in involvement_rows(hw, fps):
        lines.append(f"  [{row['status']}] {row['device']}")
        lines.append(f"           {row['detail']}")
    if hw.vvenc_skip_reason and not hw.has_libvvenc:
        lines.append(f"  WARNING: {hw.vvenc_skip_reason}")
    return "\n".join(lines)


def main() -> int:
    p = argparse.ArgumentParser(description="Detect GPUs/CPU/FFmpeg for the VVC recorder")
    p.add_argument("--json", action="store_true")
    p.add_argument("--fps", type=int, default=30)
    args = p.parse_args()
    hw = detect()
    if args.json:
        print(
            json.dumps(
                {
                    "summary": hw.summary(),
                    "cpu_name": hw.cpu_name,
                    "cpu_count": hw.cpu_count,
                    "nvidia_gpus": hw.nvidia_gpus,
                    "intel_gpus": hw.intel_gpus,
                    "other_gpus": hw.other_gpus,
                    "has_libvvenc": hw.has_libvvenc,
                    "has_gdigrab": hw.has_gdigrab,
                    "has_ddagrab": hw.has_ddagrab,
                    "has_nvenc": hw.has_nvenc,
                    "has_qsv": hw.has_qsv,
                    "vvenc_skip_reason": hw.vvenc_skip_reason,
                    "ffmpeg_path": hw.ffmpeg_path,
                    "ffmpeg_version": hw.ffmpeg_version,
                    "involvement": involvement_rows(hw, args.fps),
                    "report": format_involvement_report(hw, args.fps),
                },
                indent=2,
            )
        )
    else:
        print(format_involvement_report(hw, args.fps))
    return 0 if hw.has_libvvenc and (hw.has_gdigrab or hw.has_ddagrab) else 2


if __name__ == "__main__":
    raise SystemExit(main())
