"""One entry point for the whole pipeline.

    python run_pipeline.py start                        control center + the whole agent network (the normal way to run)
    python run_pipeline.py doctor                       check environment, config, database
    python run_pipeline.py process URL [--start T --end T]   download -> transcribe -> signals -> select -> edit
    python run_pipeline.py review                       control center only, agents off

Single stages (each takes a vod id printed by the previous one):
    download URL [--start T --end T] | transcribe ID | analyze ID | select ID | edit ID
Times are seconds or HH:MM:SS.
"""
from __future__ import annotations

import argparse
import sys


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
    args = parser.parse_args()

    if args.cmd == "doctor":
        from clipper.doctor import main as doctor
        return doctor()

    from clipper.config import load_config
    from clipper.db import init_db
    from clipper.util import log, parse_ts
    cfg = load_config()
    conn = init_db(cfg)

    if args.cmd == "init-db":
        print(f"database ready at {cfg.db_path}")
        return 0
    if args.cmd in ("review", "start"):
        from review_app.app import serve
        conn.close()
        return serve(cfg, start_agents=args.cmd == "start")

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
