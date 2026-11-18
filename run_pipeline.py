"""One entry point for the whole pipeline.

    python run_pipeline.py app                          open Clip Control in its own window (agents + dashboard)
    python run_pipeline.py start                        same, in the console: dashboard + the whole agent network
    python run_pipeline.py doctor                       check environment, config, database
    python run_pipeline.py process URL [--start T --end T]   download -> transcribe -> signals -> select -> edit
    python run_pipeline.py review                       control center only, agents off

Single stages (each takes a vod id printed by the previous one):
    download URL [--start T --end T] | transcribe ID | analyze ID | select ID | edit ID
Times are seconds or HH:MM:SS.
"""
from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent


def _browser() -> str | None:
    """A Chromium browser that can open a chromeless app window (Edge ships with Windows 10/11)."""
    candidates = [shutil.which(n) for n in ("msedge", "chrome", "google-chrome", "chromium", "chromium-browser", "brave")]
    if os.name == "nt":
        for base in (os.environ.get("PROGRAMFILES(X86)", r"C:\Program Files (x86)"), os.environ.get("PROGRAMFILES", r"C:\Program Files"),
                     os.environ.get("LOCALAPPDATA", "")):
            candidates += [os.path.join(base, r"Microsoft\Edge\Application\msedge.exe"), os.path.join(base, r"Google\Chrome\Application\chrome.exe")]
    elif sys.platform == "darwin":
        candidates += ["/Applications/Google Chrome.app/Contents/MacOS/Google Chrome", "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge"]
    return next((c for c in candidates if c and os.path.exists(c)), None)


def _wait_for_port(port: int, timeout: float = 90) -> bool:
    import socket
    end = time.time() + timeout
    while time.time() < end:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=1):
                return True
        except OSError:
            time.sleep(0.5)
    return False


def open_window(port: int) -> None:
    url = f"http://127.0.0.1:{port}/"
    browser = _browser()
    if browser:
        profile = ROOT / "data" / ".window"
        profile.mkdir(parents=True, exist_ok=True)
        subprocess.Popen([browser, f"--app={url}", "--window-size=1480,940", f"--user-data-dir={profile}", "--no-first-run"],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    else:
        import webbrowser
        webbrowser.open(url)


def supervise(window: bool) -> int:
    """Runs the dashboard + agents in a child process and restarts it if it crashes or asks to restart (exit 3)."""
    from clipper.config import load_config
    port = load_config()["review_app"]["port"]
    if _wait_for_port(port, timeout=0.5):  # already running: just show it
        print(f"Clip Control is already running at http://127.0.0.1:{port}/", flush=True)
        if window:
            open_window(port)
        return 0
    logs = ROOT / "data" / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    quiet = sys.stdout is None or window and os.name == "nt" and sys.executable.lower().endswith("pythonw.exe")
    env = dict(os.environ, CLIPPER_SUPERVISED="1", PYTHONUNBUFFERED="1")
    backoff, opened = 5, False
    while True:
        log = open(logs / "clipcontrol.log", "a", encoding="utf-8") if quiet else None
        started = time.time()
        child = subprocess.Popen([sys.executable, str(ROOT / "run_pipeline.py"), "serve"], cwd=ROOT, env=env,
                                 stdout=log or None, stderr=subprocess.STDOUT if log else None)
        if window and not opened and _wait_for_port(port):
            open_window(port)
            opened = True
        try:
            code = child.wait()
        except KeyboardInterrupt:
            child.terminate()
            try:
                child.wait(timeout=15)
            except subprocess.TimeoutExpired:
                child.kill()
            return 0
        finally:
            if log:
                log.close()
        if code == 0:
            return 0
        if code == 3:
            print("Clip Control restarting...", flush=True)
            end = time.time() + 30
            while time.time() < end and _wait_for_port(port, timeout=0.5):
                time.sleep(1)  # let the old server let go of the port
            backoff = 5
            continue
        # Crashed: wait, then bring it back. Quick repeated crashes back off up to a minute.
        backoff = 5 if time.time() - started > 300 else min(60, backoff * 2)
        print(f"Clip Control stopped unexpectedly (exit {code}); restarting in {backoff}s. Log: data/logs/clipcontrol.log", flush=True)
        time.sleep(backoff)


def main() -> int:
    for stream in (sys.stdout, sys.stderr):  # Windows consoles default to cp1252
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(description="GTA 6 clipping pipeline",
                                     epilog=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("doctor")
    sub.add_parser("init-db")
    for name in ("download", "process"):
        p = sub.add_parser(name)
        p.add_argument("url")
        p.add_argument("--start")
        p.add_argument("--end")
    for name in ("transcribe", "analyze", "select", "edit"):
        sub.add_parser(name).add_argument("vod_id", type=int)
    sub.add_parser("review")
    sub.add_parser("start")
    sub.add_parser("app")
    sub.add_parser("serve")  # internal: what start/app run under their supervisor
    sub.add_parser("agents")  # print the roster
    args = parser.parse_args()

    if args.cmd in ("start", "app"):
        return supervise(window=args.cmd == "app")

    if args.cmd == "doctor":
        from clipper.doctor import main as doctor
        return doctor()
    if args.cmd == "agents":
        from clipper.agents import CREWS, build_agents
        from clipper.config import load_config
        roster = build_agents(load_config())
        print(f"{len(roster)} agents")
        for cid, (cname, cdesc) in CREWS.items():
            mine = [a for a in roster if a.crew == cid]
            print(f"\n{cname} ({len(mine)}): {cdesc}")
            for a in mine:
                print(f"  - {a.title}: {a.job}")
        return 0

    from clipper.config import load_config
    from clipper.db import init_db
    from clipper.util import log, parse_ts
    cfg = load_config()
    conn = init_db(cfg)

    if args.cmd == "init-db":
        print(f"database ready at {cfg.db_path}")
        return 0
    if args.cmd in ("review", "serve"):
        from review_app.app import serve
        conn.close()
        return serve(cfg, start_agents=args.cmd == "serve")

    stages = {"download": ["download"], "transcribe": ["transcribe"], "analyze": ["analyze"],
              "select": ["select"], "edit": ["edit"],
              "process": ["download", "transcribe", "analyze", "select", "edit"]}[args.cmd]
    vod_id = getattr(args, "vod_id", None)
    for stage in stages:
        log(f"[{stage}]")
        if stage == "download":
            from clipper.downloader import Skipped, download
            try:
                vod_id = download(cfg, conn, args.url,
                                  parse_ts(args.start) if args.start else None,
                                  parse_ts(args.end) if args.end else None)
            except Skipped as e:
                log(f"  SKIPPED: {e}")
                return 0
            print(f"vod_id={vod_id}")
        elif stage == "transcribe":
            from clipper.transcribe import transcribe_vod
            print(f"transcript={transcribe_vod(cfg, conn, vod_id)}")
        elif stage == "analyze":
            from clipper.signals import analyze_vod
            print(f"candidates={analyze_vod(cfg, conn, vod_id)}")
        elif stage == "select":
            from clipper.selector import select_vod
            print(f"clips_selected={select_vod(cfg, conn, vod_id)}")
        elif stage == "edit":
            from clipper.editor import render_vod
            print(f"clips_rendered={render_vod(cfg, conn, vod_id)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
