#!/usr/bin/env python3
"""Locate MPC-HC (Media Player Classic) and open recordings in it."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

MPC_NAMES = ("mpc-hc64.exe", "mpc-hc.exe")


def find_mpc_hc() -> Path | None:
    for name in MPC_NAMES:
        found = shutil.which(name)
        if found:
            return Path(found)

    roots: list[Path] = []
    pf = os.environ.get("ProgramFiles")
    pf86 = os.environ.get("ProgramFiles(x86)")
    local = os.environ.get("LOCALAPPDATA")
    if pf:
        roots.append(Path(pf) / "MPC-HC")
    if pf86:
        roots.append(Path(pf86) / "MPC-HC")
    if local:
        roots.append(Path(local) / "Programs" / "MPC-HC")
        roots.append(Path(local) / "Microsoft" / "WinGet" / "Packages")

    for root in roots:
        if not root.is_dir():
            continue
        for name in MPC_NAMES:
            direct = root / name
            if direct.is_file():
                return direct
        try:
            for name in MPC_NAMES:
                hit = next(root.rglob(name), None)
                if hit is not None:
                    return hit
        except OSError:
            continue
    return None


def play_with_mpc(media: Path) -> None:
    exe = find_mpc_hc()
    if exe is None:
        raise FileNotFoundError(
            "MPC-HC was not found. Re-run run.cmd so winget can install clsid2.mpc-hc 2.8.2."
        )
    if not media.is_file():
        raise FileNotFoundError(f"Recording is gone: {media}")
    flags = 0
    if sys.platform == "win32":
        flags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
    subprocess.Popen(
        [str(exe), str(media)],
        close_fds=True,
        creationflags=flags,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def list_recordings(folder: Path) -> list[Path]:
    if not folder.is_dir():
        return []
    skip_bits = (".tmp.", ".gitkeep")
    exts = {".mp4", ".mkv", ".webm", ".avi"}
    out: list[Path] = []
    for path in folder.iterdir():
        if not path.is_file():
            continue
        name = path.name.lower()
        if any(bit in name for bit in skip_bits):
            continue
        if path.suffix.lower() not in exts:
            continue
        out.append(path)
    out.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    return out


def format_size(n: int) -> str:
    if n < 1024:
        return f"{n} B"
    if n < 1024 * 1024:
        return f" {n / 1024:.1f} KB".strip()
    return f"{n / (1024 * 1024):.1f} MB"
