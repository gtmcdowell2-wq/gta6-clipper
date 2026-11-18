"""Environment check: what's installed, what the GPU can do, and whether config loads."""
from __future__ import annotations

import importlib.util
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from .config import ROOT, ConfigError, load_config

OK, WARN, FAIL = "ok", "warn", "FAIL"


def _run(cmd: list[str], timeout: int = 30) -> tuple[int, str]:
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout,
                           encoding="utf-8", errors="replace")
        return p.returncode, (p.stdout or "") + (p.stderr or "")
    except (OSError, subprocess.TimeoutExpired) as e:
        return 1, str(e)


def nvenc_works() -> bool:
    """ffmpeg listing h264_nvenc proves nothing; only a real encode does."""
    out = Path(tempfile.gettempdir()) / "clipper_nvenc_test.mp4"
    code, _ = _run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi",
                    "-i", "testsrc=size=1280x720:rate=30:duration=1",
                    "-c:v", "h264_nvenc", str(out)])
    out.unlink(missing_ok=True)
    return code == 0


def gpu_info() -> dict:
    info = {"name": None, "vram_mb": None, "whisper_cuda": False, "nvenc": False}
    if shutil.which("nvidia-smi"):
        code, out = _run(["nvidia-smi", "--query-gpu=name,memory.total",
                          "--format=csv,noheader,nounits"])
        if code == 0 and out.strip():
            name, _, vram = out.strip().splitlines()[0].rpartition(",")
            info["name"], info["vram_mb"] = name.strip(), int(vram.strip())
    if importlib.util.find_spec("ctranslate2"):
        try:
            import ctranslate2
            info["whisper_cuda"] = ctranslate2.get_cuda_device_count() > 0
        except Exception:
            pass
    if shutil.which("ffmpeg") and info["name"]:
        info["nvenc"] = nvenc_works()
    return info


def run_checks() -> list[tuple[str, str, str]]:
    rows: list[tuple[str, str, str]] = []
    add = lambda name, status, detail: rows.append((name, status, detail))

    v = sys.version_info
    add("Python", OK if v >= (3, 11) else FAIL, f"{v.major}.{v.minor}.{v.micro} (need 3.11+)")
    add("venv", OK if sys.prefix != sys.base_prefix else WARN,
        sys.prefix if sys.prefix != sys.base_prefix else "not running inside a virtualenv")
    add("git", OK if shutil.which("git") else WARN, shutil.which("git") or "not on PATH")

    for tool in ("ffmpeg", "ffprobe"):
        path = shutil.which(tool)
        if not path:
            add(tool, FAIL, "not on PATH")
            continue
        _, out = _run([tool, "-version"])
        add(tool, OK, out.splitlines()[0][:70] if out else path)

    if shutil.which("ffmpeg"):
        _, enc = _run(["ffmpeg", "-hide_banner", "-encoders"])
        _, flt = _run(["ffmpeg", "-hide_banner", "-filters"])
        add("ffmpeg libx264", OK if "libx264" in enc else FAIL, "CPU H.264 encoder")
        add("ffmpeg AAC", OK if " aac " in enc else FAIL, "audio encoder")
        has = lambda f: any(len(l.split()) > 1 and l.split()[1] == f for l in flt.splitlines())
        add("ffmpeg subtitles (libass)", OK if has("ass") and has("subtitles") else FAIL,
            "needed to burn captions; install a *full* ffmpeg build if missing")
        add("ffmpeg loudnorm", OK if has("loudnorm") else FAIL, "-14 LUFS normalization")

    gpu = gpu_info()
    if gpu["name"]:
        add("NVIDIA GPU", OK, f"{gpu['name']}, {gpu['vram_mb']} MB VRAM")
        add("NVENC encode", OK if gpu["nvenc"] else WARN,
            "test encode passed" if gpu["nvenc"] else "test encode failed; will use libx264")
        if importlib.util.find_spec("ctranslate2"):
            add("Whisper on CUDA", OK if gpu["whisper_cuda"] else WARN,
                "ctranslate2 sees the GPU" if gpu["whisper_cuda"]
                else "ctranslate2 can't see CUDA (cuDNN/cuBLAS DLLs missing?); will use CPU")
        else:
            add("Whisper on CUDA", WARN, "faster-whisper not installed yet (milestone 2)")
    else:
        add("NVIDIA GPU", WARN, "none detected: Whisper falls back to 'small' on CPU, encoding to libx264")

    for mod, label in (("yaml", "PyYAML"), ("dotenv", "python-dotenv"), ("yt_dlp", "yt-dlp"),
                       ("faster_whisper", "faster-whisper"), ("cv2", "opencv"), ("anthropic", "anthropic"), ("flask", "Flask"),
                       ("requests", "requests"), ("tzdata", "tzdata"), ("waitress", "waitress")):
        add(label, OK if importlib.util.find_spec(mod) else FAIL, "python package")
    from .util import find_tool
    tdl = find_tool("TwitchDownloaderCLI")
    add("TwitchDownloaderCLI", OK if tdl else WARN, tdl or "not found in ./tools or PATH: Twitch chat signal will be missing")
    missing = [p for p in ("assets/models/face_detection_yunet_2023mar.onnx", "assets/fonts/Anton-Regular.ttf",
                           "assets/fonts/Bangers-Regular.ttf", "assets/fonts/Poppins-ExtraBold.ttf", "prompts/selector_system.md",
                           "prompts/hook_critic.md")
               if not (ROOT / p).exists()]
    add("bundled assets", FAIL if missing else OK, "missing: " + ", ".join(missing) if missing else "fonts, face model, prompt")

    try:
        cfg = load_config()
    except (ConfigError, KeyError) as e:
        add("config", FAIL, str(e))
        return rows
    counts = {p: sum(s.permission == p for s in cfg.sources) for p in ("campaign", "public_ok", "unknown")}
    add("config.yaml", OK, "loaded and validated")
    add("sources.yaml", OK if cfg.sources else WARN,
        f"{len(cfg.sources)} sources: " + ", ".join(f"{n} {p}" for p, n in counts.items()))
    if cfg.sources and not counts["campaign"] + counts["public_ok"]:
        add("postable sources", WARN, "every source is 'unknown', so nothing can be posted yet")
    add("auto-approve", OK if cfg["safety"]["auto_approve_threshold"] is None else WARN,
        "off (human review required)" if cfg["safety"]["auto_approve_threshold"] is None
        else f"ON at {cfg['safety']['auto_approve_threshold']}")

    add(".env", OK if (ROOT / ".env").exists() else WARN,
        "present" if (ROOT / ".env").exists() else "missing; copy .env.example when a milestone needs keys")
    add("Claude API key", OK if os.environ.get("ANTHROPIC_API_KEY") else WARN,
        "set" if os.environ.get("ANTHROPIC_API_KEY") else "ANTHROPIC_API_KEY not set: the Moment Picker and critics wait for it")
    try:
        from importlib.metadata import version
        v = version("anthropic")
        add("anthropic SDK", OK if int(v.split(".")[0]) >= 2 else FAIL, f"{v} (2.x needed for the refusal fallback)")
    except Exception:
        pass
    from .notify import channels
    ch = channels()
    add("phone alerts", OK if ch else WARN, ", ".join(ch) if ch else "none: set NTFY_TOPIC or DISCORD_WEBHOOK_URL in .env to get alerts on your phone")
    try:
        from .agents import build_agents
        add("agent network", OK, f"{len(build_agents(cfg))} agents build")
    except Exception as e:
        add("agent network", FAIL, f"{type(e).__name__}: {e}")

    cfg.data_dir.mkdir(parents=True, exist_ok=True)
    free_gb = shutil.disk_usage(cfg.data_dir).free / 1e9
    add("free disk", OK if free_gb >= 200 else WARN, f"{free_gb:.0f} GB free at {cfg.data_dir} (200+ recommended)")

    try:
        from .db import init_db
        conn = init_db(cfg)
        tables = [r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
        n = conn.execute("SELECT count(*) FROM sources WHERE active=1").fetchone()[0]
        conn.close()
        add("database", OK, f"{cfg.db_path.name}: {len(tables)} tables, {n} active sources")
    except Exception as e:  # report whatever broke rather than crash the check
        add("database", FAIL, str(e))
    return rows


def main() -> int:
    rows = run_checks()
    width = max(len(r[0]) for r in rows)
    for name, status, detail in rows:
        print(f"[{status:^4}] {name:<{width}}  {detail}")
    fails = sum(r[1] == FAIL for r in rows)
    warns = sum(r[1] == WARN for r in rows)
    print(f"\n{len(rows) - fails - warns} ok, {warns} warnings, {fails} failures")
    return 1 if fails else 0
