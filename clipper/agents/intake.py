"""Intake crew: find new GTA 6 footage and turn it into rendered clips."""
from __future__ import annotations

import shutil
import time
from pathlib import Path

from ..config import secret
from .base import Agent, Ctx


class Scout(Agent):
    name, title, crew, lane = "scout", "Scout", "intake", "light"
    job = "Watches every permitted streamer for new VODs and live marathons, keeps only the hours where the game is GTA 6, and queues them in 2-hour chunks."

    def __init__(self, cfg):
        super().__init__(cfg)
        self.interval = cfg["watcher"]["poll_minutes"] * 60

    @staticmethod
    def listing_url(source) -> str:
        handle = source["key"].split(":", 1)[1]
        if source["platform"] == "twitch":
            return f"https://www.twitch.tv/{handle}/videos?filter=archives&sort=time"
        if source["platform"] == "kick":
            return f"https://kick.com/{handle}/videos"
        return source["url"].rstrip("/") + "/streams"

    def work(self, ctx: Ctx):
        import yt_dlp

        from ..downloader import game_ranges, probe
        a = ctx.cfg["agents"]
        where = "active=1" if a["include_unknown_sources"] else "active=1 AND permission != 'unknown'"
        sources = ctx.conn.execute(f"SELECT * FROM sources WHERE {where} ORDER BY last_checked IS NOT NULL, last_checked").fetchall()
        if not sources:
            return None
        chunk = ctx.cfg["download"]["chunk_hours"] * 3600
        patterns = ctx.cfg["watcher"]["game_patterns"]
        queued, notes = 0, []
        for source in sources:
            try:
                with yt_dlp.YoutubeDL({"quiet": True, "no_warnings": True, "extract_flat": True,
                                       "playlistend": a["max_vods_per_source"]}) as ydl:
                    listing = ydl.extract_info(self.listing_url(source), download=False)
                entries = [e for e in (listing.get("entries") or []) if e and e.get("url")][: a["max_vods_per_source"]]
            except Exception as e:
                ctx.log(f"{source['name']}: could not list videos ({str(e)[:160]})", level="warn")
                entries = []
            for entry in entries:
                url = entry["url"].split("#__youtubedl_smuggle")[0]
                seen = ctx.get(f"scout:{url}") or {}
                if seen.get("skip"):
                    continue
                try:
                    meta = probe(url)
                except Exception as e:
                    if "will begin in" not in str(e):  # a scheduled stream that hasn't started is not a problem
                        ctx.log(f"{source['name']}: could not read {url} ({str(e)[:160]})", level="warn")
                    continue
                duration = float(meta["duration"] or 0)
                age_days = (time.time() - meta["timestamp"]) / 86400 if meta["timestamp"] else 0
                if ctx.cfg.is_leak(meta["title"], meta["description"]):
                    ctx.set(f"scout:{url}", {"skip": "leak keyword"})
                    ctx.log(f"Skipped (leak keyword in title): {meta['title']!r}", level="warn")
                    continue
                if age_days > a["max_vod_age_days"]:
                    ctx.set(f"scout:{url}", {"skip": "too old"})
                    continue
                # A VOD still being recorded keeps growing; only cut chunks that are completely on disk at Twitch.
                ended_by_clock = bool(meta["timestamp"]) and meta["timestamp"] + duration < time.time() - 1800
                finished = not meta["is_live"] and (ended_by_clock or seen.get("duration") == duration)
                ctx.set(f"scout:{url}", {"duration": duration})
                for lo, hi in game_ranges(meta, patterns):
                    hi = min(hi, duration) if duration else hi
                    start = lo
                    while start < hi - 120:
                        end = min(start + chunk, hi)
                        if not finished and (end - start < chunk or end > duration - 300):
                            break  # wait for this chunk to finish recording
                        cur = ctx.conn.execute(
                            "INSERT OR IGNORE INTO vods (source_id, platform_id, url, title, description, duration_s, published_at,"
                            " chunk_start_s, chunk_end_s) VALUES (?,?,?,?,?,?, datetime(?, 'unixepoch'), ?, ?)",
                            (source["id"], meta["platform_id"], url, meta["title"], meta["description"], duration,
                             meta["timestamp"], float(start), float(end)))
                        if cur.rowcount:
                            queued += 1
                            notes.append(f"{source['name']} {int(start // 3600)}h{int(start % 3600 // 60):02d}")
                        start = end
                ctx.conn.commit()
            ctx.conn.execute("UPDATE sources SET last_checked=datetime('now') WHERE id=?", (source["id"],))
            ctx.conn.commit()
        return f"Queued {queued} new chunk(s): {', '.join(notes[:6])}" if queued else None


class Downloader(Agent):
    name, title, crew, lane, interval = "downloader", "Downloader", "intake", "heavy", 30
    job = "Downloads each queued chunk at up to 1080p with its chat log, newest footage first, and stops when disk space runs low."

    def work(self, ctx: Ctx):
        from ..downloader import Skipped, download
        vod = ctx.conn.execute("SELECT * FROM vods WHERE status='new' ORDER BY published_at DESC, chunk_start_s LIMIT 1").fetchone()
        if not vod:
            return None
        ctx.cfg.data_dir.mkdir(parents=True, exist_ok=True)
        free = shutil.disk_usage(ctx.cfg.data_dir).free / 1e9
        if free < ctx.cfg["agents"]["min_free_gb"]:
            raise RuntimeError(f"only {free:.0f} GB free; downloads paused until space is freed")
        try:
            download(ctx.cfg, ctx.conn, vod["url"], vod_id=vod["id"])
        except Skipped as e:
            return f"Skipped vod {vod['id']}: {e}"
        return f"Downloaded {vod['title']!r} from {vod['chunk_start_s'] / 3600:.1f}h"


class Transcriber(Agent):
    name, title, crew, lane, interval = "transcriber", "Transcriber", "intake", "heavy", 20
    job = "Turns the audio into a word-by-word transcript with Whisper (GPU when available), which drives captions and moment picking."

    def __init__(self, cfg):
        super().__init__(cfg)
        self._model = None

    def work(self, ctx: Ctx):
        from ..transcribe import load_model, transcribe_vod
        vod = ctx.conn.execute("SELECT id, title FROM vods WHERE status='downloaded' ORDER BY published_at DESC, chunk_start_s LIMIT 1").fetchone()
        if not vod:
            return None
        if self._model is None:  # loaded once, reused for every chunk
            self._model, label = load_model(ctx.cfg)
            ctx.log(f"Whisper ready: {label}")
        transcribe_vod(ctx.cfg, ctx.conn, vod["id"], self._model)
        return f"Transcribed vod {vod['id']}"


class SignalAnalyst(Agent):
    name, title, crew, lane, interval = "signal_analyst", "Signal Analyst", "intake", "heavy", 20
    job = "Scores every second for excitement from audio spikes, chat speed and excited speech, and proposes the strongest windows as candidates."

    def work(self, ctx: Ctx):
        from ..signals import analyze_vod
        vod = ctx.conn.execute("SELECT id FROM vods WHERE status='transcribed' ORDER BY published_at DESC, chunk_start_s LIMIT 1").fetchone()
        if not vod:
            return None
        return f"Found {analyze_vod(ctx.cfg, ctx.conn, vod['id'])} candidate moments in vod {vod['id']}"


class MomentPicker(Agent):
    name, title, crew, lane, interval = "moment_picker", "Moment Picker", "intake", "llm", 30
    job = "Sends each candidate window to Claude to choose exact start and end points, a hook, title and caption; rejects anything weak, overlapping, leaked or too long."

    def work(self, ctx: Ctx):
        import os

        from ..selector import select_vod
        vod = ctx.conn.execute("SELECT id FROM vods WHERE status='analyzed' ORDER BY published_at DESC, chunk_start_s LIMIT 1").fetchone()
        if not vod:
            return None
        provider = os.environ.get("CLIPPER_SELECTOR_PROVIDER") or ctx.cfg["selector"]["provider"]
        if provider == "anthropic" and not secret("ANTHROPIC_API_KEY"):
            raise RuntimeError("waiting for ANTHROPIC_API_KEY in .env")
        made = select_vod(ctx.cfg, ctx.conn, vod["id"])
        status = ctx.conn.execute("SELECT status FROM vods WHERE id=?", (vod["id"],)).fetchone()[0]
        if status == "analyzed":  # file provider with answers still missing
            return None
        return f"Picked {made} clip(s) from vod {vod['id']}"


class Editor(Agent):
    name, title, crew, lane, interval = "editor", "Editor", "intake", "heavy", 10
    job = "Renders each picked moment as a 1080x1920 short: finds the facecam, stacks it over gameplay, burns in captions, hook and credit, and levels the audio."

    def work(self, ctx: Ctx):
        from ..editor import render_clip
        clip = ctx.conn.execute("SELECT id, post_title FROM clips WHERE status='selected' ORDER BY score_overall DESC, id LIMIT 1").fetchone()
        if not clip:
            return None
        ctx.conn.execute("DELETE FROM reviews WHERE clip_id=?", (clip["id"],))  # new pixels need fresh checks
        ctx.conn.execute("UPDATE clips SET recommendation=NULL, reach_score=NULL WHERE id=?", (clip["id"],))
        ctx.conn.commit()
        path = render_clip(ctx.cfg, ctx.conn, clip["id"])
        vod_id = ctx.conn.execute("SELECT vod_id FROM clips WHERE id=?", (clip["id"],)).fetchone()[0]
        if not ctx.conn.execute("SELECT 1 FROM clips WHERE vod_id=? AND status='selected'", (vod_id,)).fetchone():
            ctx.conn.execute("UPDATE vods SET status='done', updated_at=datetime('now') WHERE id=? AND status='selected'", (vod_id,))
            ctx.conn.commit()
        return f"Rendered clip {clip['id']}: {clip['post_title']!r} ({Path(path).stat().st_size / 1e6:.0f} MB)"
