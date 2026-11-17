"""Small shared helpers: timestamps, subprocess, tool lookup."""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

from .config import ROOT


def hms(seconds: float, ms: bool = False) -> str:
    seconds = max(0.0, float(seconds))
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{int(h):02d}:{int(m):02d}:{s:06.3f}" if ms else f"{int(h):02d}:{int(m):02d}:{int(s):02d}"


def parse_ts(value) -> float:
    """'HH:MM:SS.mmm', 'MM:SS', or plain seconds -> seconds."""
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip().strip("[]")
    if not re.fullmatch(r"\d+(:\d{1,2}){0,2}([.,]\d+)?", text):
        raise ValueError(f"bad timestamp: {value!r}")
    total = 0.0
    for part in text.replace(",", ".").split(":"):
        total = total * 60 + float(part)
    return total


def run(cmd: list[str], cwd: Path | None = None, timeout: int | None = None) -> str:
    """Run a command, return combined output, raise with the tail of the output on failure."""
    p = subprocess.run([str(c) for c in cmd], cwd=cwd, capture_output=True, text=True,
                       encoding="utf-8", errors="replace", timeout=timeout)
    out = (p.stdout or "") + (p.stderr or "")
    if p.returncode != 0:
        raise RuntimeError(f"{Path(str(cmd[0])).name} failed ({p.returncode}):\n{out[-1500:]}")
    return out


def find_tool(name: str) -> str | None:
    """Look in ./tools first (where setup puts TwitchDownloaderCLI), then PATH."""
    exe = name + (".exe" if os.name == "nt" else "")
    local = ROOT / "tools" / exe
    if local.exists():
        return str(local)
    return shutil.which(name)


def ffprobe(path: Path) -> dict:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-print_format", "json", "-show_format", "-show_streams", str(path)],
        capture_output=True, text=True, encoding="utf-8", errors="replace", check=True).stdout
    data = json.loads(out)
    info = {"duration": float(data["format"].get("duration", 0)),
            "start_time": float(data["format"].get("start_time", 0) or 0),
            "has_audio": False, "width": 0, "height": 0, "fps": 0.0}
    for s in data["streams"]:
        if s["codec_type"] == "video" and not info["width"]:
            num, _, den = s.get("avg_frame_rate", "0/1").partition("/")
            info.update(width=int(s["width"]), height=int(s["height"]),
                        fps=float(num) / float(den or 1) if float(den or 1) else 0.0)
        elif s["codec_type"] == "audio":
            info["has_audio"] = True
    return info


def log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


def safe_name(text: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", text).strip("_")[:80] or "x"
