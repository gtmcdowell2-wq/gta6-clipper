"""Intake crew: find new GTA 6 footage and turn it into rendered, measured clips.

One Scout per streamer, a Stream Radar that knows who is live, then the production line: Downloader ->
Transcriber -> Signal Analyst -> Moment Picker (or the Backup Picker) -> Editor -> Caption Finisher ->
Clip Profiler. The inspectors start once a clip has been profiled.
"""
from __future__ import annotations

import json
import re
import shutil
import time
from collections import Counter
from pathlib import Path

from ..config import secret
from .base import Agent, Ctx


class _Quiet:
    """yt-dlp prints 'not currently live' as an ERROR for every offline channel; we only want the result."""
    def debug(self, msg): pass
    def info(self, msg): pass
    def warning(self, msg): pass
    def error(self, msg): pass


QUIET = {"quiet": True, "no_warnings": True, "logger": _Quiet()}


def _handle(source) -> str:
    return source["key"].split(":", 1)[1]


class Scout(Agent):
    """One per streamer, so each channel has its own schedule, its own log and its own pause switch."""
    crew, lane = "intake", "scout"
    backup = "If the channel listing fails it logs a warning and tries again next round; the Stream Radar wakes it early when the streamer goes live."

    def __init__(self, cfg, source):
        super().__init__(cfg)
        self.key = source.key
        self.name = f"scout.{source.handle}"
        self.title = f"{source.name} Scout"
        self.job = (f"Watches {source.name}'s {source.platform} channel for new VODs and live marathons, keeps only the hours "
                    f"where the game is GTA 6, and queues them in chunks (30 minutes while live, 2 hours after).")
        self.interval = cfg["watcher"]["poll_minutes"] * 60

    @staticmethod
    def listing_url(source) -> str:
        handle = _handle(source)
        if source["platform"] == "twitch":
            return f"https://www.twitch.tv/{handle}/videos?filter=archives&sort=time"
        if source["platform"] == "kick":
            return f"https://kick.com/{handle}/videos"
        return source["url"].rstrip("/") + "/streams"

    def work(self, ctx: Ctx):
        import yt_dlp

        from ..downloader import game_ranges, probe
        a = ctx.cfg["agents"]
        source = ctx.conn.execute("SELECT * FROM sources WHERE key=?", (self.key,)).fetchone()
        if not source or not source["active"]:
            return None
        if source["permission"] == "unknown" and not a["include_unknown_sources"]:
            return None  # can't be posted anyway; the Brief Keeper explains this on the dashboard
        live = bool(source["live_now"])
        self.interval = 300 if live else ctx.cfg["watcher"]["poll_minutes"] * 60
        dl = ctx.cfg["download"]
        chunk = int(dl.get("live_chunk_minutes", 30) * 60) if live else int(dl["chunk_hours"] * 3600)
        patterns = ctx.cfg["watcher"]["game_patterns"]
        queued, notes = 0, []
        try:
            with yt_dlp.YoutubeDL({**QUIET, "extract_flat": True, "playlistend": a["max_vods_per_source"]}) as ydl:
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
            # A VOD still being recorded keeps growing; only cut chunks that are completely on disk at the site.
            ended_by_clock = bool(meta["timestamp"]) and meta["timestamp"] + duration < time.time() - 1800
            finished = not meta["is_live"] and (ended_by_clock or seen.get("duration") == duration)
            ctx.set(f"scout:{url}", {"duration": duration})
            for lo, hi in game_ranges(meta, patterns):
                hi = min(hi, duration) if duration else hi
                # Continue from the last chunk already queued, so live (short) and finished (long) chunks never overlap.
                done = ctx.conn.execute("SELECT max(chunk_end_s) FROM vods WHERE source_id=? AND platform_id=? AND chunk_end_s > ?"
                                        " AND chunk_start_s < ?", (source["id"], meta["platform_id"], lo, hi)).fetchone()[0]
                start = max(lo, done or lo)
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
                        notes.append(f"{int(start // 3600)}h{int(start % 3600 // 60):02d}")
                    start = end
            ctx.conn.commit()
        ctx.conn.execute("UPDATE sources SET last_checked=datetime('now') WHERE id=?", (source["id"],))
        ctx.conn.commit()
        if queued:
            ctx.conn.execute("UPDATE agents SET run_requested=1 WHERE name='downloader'")
            ctx.conn.commit()
        return f"Queued {queued} new chunk(s) from {source['name']}: {', '.join(notes[:6])}" if queued else None


class StreamRadar(Agent):
    name, title, crew, lane, interval = "stream_radar", "Stream Radar", "intake", "radar", 180
    job = "Checks every 3 minutes which streamers are live and what they're playing, shows LIVE badges on the dashboard, and wakes that streamer's Scout the moment a stream starts or ends."
    backup = "If a live check fails it keeps the last known state and the Scouts fall back to their normal 20-minute rounds."

    @staticmethod
    def live_url(source) -> str:
        handle = _handle(source)
        return {"twitch": f"https://www.twitch.tv/{handle}", "kick": f"https://kick.com/{handle}"}.get(
            source["platform"], source["url"].rstrip("/") + "/live")

    def work(self, ctx: Ctx):
        import yt_dlp
        changed = []
        for s in ctx.conn.execute("SELECT * FROM sources WHERE active=1").fetchall():
            live, title = False, None
            try:
                with yt_dlp.YoutubeDL({**QUIET, "skip_download": True}) as ydl:
                    info = ydl.extract_info(self.live_url(s), download=False)
                live = bool(info.get("is_live")) or info.get("live_status") == "is_live"
                title = info.get("title") or info.get("description")
            except Exception as e:
                msg = str(e).lower()
                if not any(x in msg for x in ("not currently live", "is offline", "not live", "will begin", "no video formats",
                                              "does not exist", "unable to find", "offline")):
                    ctx.conn.execute("UPDATE sources SET live_checked=datetime('now') WHERE id=?", (s["id"],))
                    continue  # unknown failure: keep the last known state
            if live != bool(s["live_now"]):
                changed.append(f"{s['name']} {'went live' if live else 'went offline'}" + (f": {title[:80]}" if live and title else ""))
                ctx.conn.execute("UPDATE agents SET run_requested=1 WHERE name=?", (f"scout.{_handle(s)}",))
            ctx.conn.execute("UPDATE sources SET live_now=?, live_title=?, live_checked=datetime('now') WHERE id=?",
                             (int(live), title if live else None, s["id"]))
        ctx.conn.commit()
        return "; ".join(changed) or None


class Downloader(Agent):
    name, title, crew, lane, interval = "downloader", "Downloader", "intake", "download", 30
    job = "Downloads each queued chunk at up to 1080p with its chat log, newest footage first, and stops when disk space runs low."
    backup = "A failed download is retried once automatically by the Supervisor; chat that can't be fetched is skipped and the clip is picked from audio and speech alone."
    max_runtime = 3 * 3600

    def work(self, ctx: Ctx):
        from ..downloader import Skipped, download
        vod = ctx.conn.execute("SELECT * FROM vods WHERE status='new' ORDER BY published_at DESC, chunk_start_s LIMIT 1").fetchone()
        if not vod:
            return None
        ctx.cfg.data_dir.mkdir(parents=True, exist_ok=True)
        free = shutil.disk_usage(ctx.cfg.data_dir).free / 1e9
        if free < ctx.cfg["agents"]["min_free_gb"]:
            ctx.alert("disk_low", f"Downloads paused: only {free:.0f} GB free", "The Janitor clears old raw VODs; you can also free space on the drive.", "error")
            raise RuntimeError(f"only {free:.0f} GB free; downloads paused until space is freed")
        ctx.resolve("disk_low")
        try:
            download(ctx.cfg, ctx.conn, vod["url"], vod_id=vod["id"])
        except Skipped as e:
            return f"Skipped vod {vod['id']}: {e}"
        return f"Downloaded {vod['title']!r} from {vod['chunk_start_s'] / 3600:.1f}h"


class Transcriber(Agent):
    name, title, crew, lane, interval = "transcriber", "Transcriber", "intake", "gpu", 20
    job = "Turns the audio into a word-by-word transcript with Whisper (GPU when available), which drives captions and moment picking."
    backup = "If the GPU model can't load it falls back to the CPU model, so transcripts keep coming (slower)."
    max_runtime = 2 * 3600

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
        try:
            transcribe_vod(ctx.cfg, ctx.conn, vod["id"], self._model)
        except RuntimeError as e:
            if "cuda" not in str(e).lower() and "cudnn" not in str(e).lower() and "cublas" not in str(e).lower():
                raise
            # Backup: the GPU failed mid-run (driver, out of memory). Reload on the CPU and carry on.
            from ..transcribe import load_cpu_model
            self._model, label = load_cpu_model(ctx.cfg)
            ctx.log(f"GPU transcription failed ({str(e)[:120]}); switched to {label}", level="warn")
            transcribe_vod(ctx.cfg, ctx.conn, vod["id"], self._model)
        return f"Transcribed vod {vod['id']}"


class SignalAnalyst(Agent):
    name, title, crew, lane, interval = "signal_analyst", "Signal Analyst", "intake", "cpu", 20
    job = "Scores every second for excitement from audio spikes, chat speed and excited speech, and proposes the strongest windows as candidates."
    backup = "With no chat log it scores from audio and speech alone, so the stream still gets candidates."

    def work(self, ctx: Ctx):
        from ..signals import analyze_vod
        vod = ctx.conn.execute("SELECT id FROM vods WHERE status='transcribed' ORDER BY published_at DESC, chunk_start_s LIMIT 1").fetchone()
        if not vod:
            return None
        return f"Found {analyze_vod(ctx.cfg, ctx.conn, vod['id'])} candidate moments in vod {vod['id']}"


class MomentPicker(Agent):
    name, title, crew, lane, interval = "moment_picker", "Moment Picker", "intake", "llm", 30
    job = "Sends each candidate window to Claude to choose exact start and end points, a hook, title and caption; rejects anything weak, overlapping, leaked or too long."
    backup = "If Claude is unreachable or the daily budget is spent, the Backup Picker takes over after a grace period so launch-week moments aren't lost."

    def work(self, ctx: Ctx):
        import os

        from ..llm import LLMUnavailable
        from ..selector import select_vod
        vod = ctx.conn.execute("SELECT id FROM vods WHERE status='analyzed' ORDER BY published_at DESC, chunk_start_s LIMIT 1").fetchone()
        if not vod:
            return None
        provider = os.environ.get("CLIPPER_SELECTOR_PROVIDER") or ctx.cfg["selector"]["provider"]
        if provider == "anthropic" and not secret("ANTHROPIC_API_KEY"):
            ctx.alert("no_api_key", "Moment Picker is waiting for your Anthropic API key",
                      "Put ANTHROPIC_API_KEY=... in the .env file next to config.yaml, then restart Clip Control.", "error")
            ctx.set("llm_down_since", ctx.get("llm_down_since") or time.time())
            return None
        ctx.resolve("no_api_key")
        try:
            made = select_vod(ctx.cfg, ctx.conn, vod["id"])
        except LLMUnavailable as e:
            ctx.set("llm_down_since", ctx.get("llm_down_since") or time.time())
            ctx.alert("llm_down", "Claude is unavailable", f"{e}. The Backup Picker takes over if this lasts.", "warn")
            raise
        ctx.set("llm_down_since", None)
        ctx.resolve("llm_down")
        status = ctx.conn.execute("SELECT status FROM vods WHERE id=?", (vod["id"],)).fetchone()[0]
        if status == "analyzed":  # file provider with answers still missing
            return None
        return f"Picked {made} clip(s) from vod {vod['id']}"


class BackupPicker(Agent):
    name, title, crew, lane, interval = "backup_picker", "Backup Picker", "intake", "cpu", 60
    job = "Stands in for the Moment Picker when Claude has been unavailable for a while: cuts the strongest audio/chat spikes on sentence boundaries with a plain title, and marks every clip 'picked by backup' so you look closely."
    backup = "This agent is the backup. It only cuts the top few spikes per chunk, so an outage doesn't flood your review queue."

    def work(self, ctx: Ctx):
        from ..selector import backup_select_vod
        mode = ctx.cfg["selector"].get("backup_picker", "outage")
        if mode == "off":
            return None
        down = ctx.get("llm_down_since")
        grace = float(ctx.cfg["selector"].get("backup_after_minutes", 45)) * 60
        no_key = not secret("ANTHROPIC_API_KEY")
        if not ((down and time.time() - down > grace and (mode == "always" or not no_key)) or (mode == "always" and no_key)):
            return None
        vod = ctx.conn.execute("SELECT id FROM vods WHERE status='analyzed' ORDER BY published_at DESC, chunk_start_s LIMIT 1").fetchone()
        if not vod:
            return None
        made = backup_select_vod(ctx.cfg, ctx.conn, vod["id"])
        return f"Backup-picked {made} clip(s) from vod {vod['id']} (Claude unavailable)"


class Editor(Agent):
    name, title, crew, lane, interval = "editor", "Editor", "intake", "render", 10
    job = "Renders each picked moment as a 1080x1920 short: finds the facecam, stacks it over gameplay, burns in captions, hook, credit and disclosure, and levels the audio."
    backup = "If the GPU encoder fails it re-renders on the CPU; a failed render is retried once by the Supervisor."
    max_runtime = 3600

    def work(self, ctx: Ctx):
        from ..editor import render_clip
        clip = ctx.conn.execute("SELECT id, post_title FROM clips WHERE status='selected' ORDER BY score_overall DESC, id LIMIT 1").fetchone()
        if not clip:
            return None
        # New pixels need fresh checks.
        ctx.conn.execute("DELETE FROM reviews WHERE clip_id=?", (clip["id"],))
        ctx.conn.execute("DELETE FROM clip_facts WHERE clip_id=?", (clip["id"],))
        ctx.conn.execute("UPDATE clips SET recommendation=NULL, rec_reason=NULL, reach_score=NULL, caption_final=0 WHERE id=?", (clip["id"],))
        ctx.conn.commit()
        path = render_clip(ctx.cfg, ctx.conn, clip["id"])
        vod_id = ctx.conn.execute("SELECT vod_id FROM clips WHERE id=?", (clip["id"],)).fetchone()[0]
        if not ctx.conn.execute("SELECT 1 FROM clips WHERE vod_id=? AND status='selected'", (vod_id,)).fetchone():
            ctx.conn.execute("UPDATE vods SET status='done', updated_at=datetime('now') WHERE id=? AND status='selected'", (vod_id,))
            ctx.conn.commit()
        return f"Rendered clip {clip['id']}: {clip['post_title']!r} ({Path(path).stat().st_size / 1e6:.0f} MB)"


class CaptionFinisher(Agent):
    name, title, crew, lane, interval = "caption_finisher", "Caption Finisher", "intake", "light", 8
    job = "Finishes every post caption before the inspectors see it: adds the campaign's required credit, tags and paid disclosure, and the #GTA6 tag, so nothing gets rejected for a missing detail."
    backup = "Anything it can't add automatically is left for the Campaign Rules Guard to flag."

    def work(self, ctx: Ctx):
        clip = ctx.conn.execute("SELECT * FROM clips WHERE status='pending_review' AND caption_final=0 ORDER BY id LIMIT 1").fetchone()
        if not clip:
            return None
        source = ctx.source_for_clip(clip)
        brief = ctx.brief(source) if source["permission"] == "campaign" else {}
        caption, added = (clip["caption"] or "").strip(), []

        def add(text: str):
            nonlocal caption
            if text and text.lower() not in caption.lower():
                caption = f"{caption} {text}".strip()
                added.append(text)
        add((source["caption_suffix"] or "").strip())
        for item in brief.get("required_text", []) + brief.get("required_tags", []):
            add(str(item))
        if brief:
            add(brief.get("disclosure") or "#ad")
        if not re.search(r"#gta(6|vi)\b", caption, re.I):
            add("#GTA6")
        streamer_tag = "#" + re.sub(r"\W", "", source["name"])
        if streamer_tag.lower() not in caption.lower() and f"#{_handle(source)}".lower() not in caption.lower():
            add(streamer_tag)
        ctx.conn.execute("UPDATE clips SET caption=?, caption_final=1 WHERE id=?", (caption, clip["id"]))
        ctx.conn.commit()
        return f"Clip {clip['id']}: caption finished" + (f" (added {', '.join(added)})" if added else "")


class ClipProfiler(Agent):
    name, title, crew, lane, interval = "clip_profiler", "Clip Profiler", "intake", "render", 8
    job = "Measures each finished clip once (format, loudness, peaks, black/frozen/silent stretches, faces, sharpness, letterbox bars, music beat, caption timing, fingerprints) so every inspector can check it in milliseconds."
    backup = "If a measurement fails the rest are still saved; inspectors that needed the missing number mark themselves 'skipped'."
    max_runtime = 1800

    def work(self, ctx: Ctx):
        from ..profile import profile_clip, save_facts
        clip = ctx.conn.execute("SELECT * FROM clips c WHERE status='pending_review' AND caption_final=1 AND file_path IS NOT NULL"
                                " AND NOT EXISTS (SELECT 1 FROM clip_facts f WHERE f.clip_id=c.id) ORDER BY id LIMIT 1").fetchone()
        if not clip:
            return None
        if not Path(clip["file_path"]).exists():
            ctx.conn.execute("UPDATE clips SET status='failed', error='rendered file is missing' WHERE id=?", (clip["id"],))
            ctx.conn.commit()
            return f"Clip {clip['id']}: file missing, sent back to the Editor"
        t0 = time.time()
        facts = profile_clip(ctx.cfg, ctx.conn, clip)
        save_facts(ctx.conn, clip["id"], facts)
        return f"Profiled clip {clip['id']} in {time.time() - t0:.0f}s"


class ThumbnailMaker(Agent):
    name, title, crew, lane, interval = "thumbnail_maker", "Thumbnail Maker", "intake", "render", 20
    job = "Makes a full-size 1080x1920 cover for each clip from its best frame (the Visual Judge's pick, else the sharpest frame with a face) for YouTube's Shorts thumbnail."
    backup = "Without the Visual Judge's pick it chooses by face, sharpness and brightness; without any frame data it uses the 1.5-second frame."

    def work(self, ctx: Ctx):
        from ..util import run
        clip = ctx.conn.execute("SELECT c.* FROM clips c WHERE c.status IN ('pending_review','approved') AND c.file_path IS NOT NULL"
                                " AND EXISTS (SELECT 1 FROM clip_facts f WHERE f.clip_id=c.id) ORDER BY c.id DESC LIMIT 40").fetchall()
        for c in clip:
            work = Path(c["file_path"]).parent
            if (work / "cover.jpg").exists() or not Path(c["file_path"]).exists():
                continue
            judge = ctx.conn.execute("SELECT details, created_at FROM reviews WHERE clip_id=? AND agent='visual_judge'", (c["id"],)).fetchone()
            waited = ctx.conn.execute("SELECT (julianday('now') - julianday(profiled_at)) * 1440 FROM clip_facts WHERE clip_id=?", (c["id"],)).fetchone()[0] or 0
            if not judge and waited < 10:
                continue  # give the Visual Judge a chance to pick
            t = (json.loads(judge["details"] or "{}").get("best_cover") if judge else None)
            if t is None:
                frames = ctx.facts(c["id"]).get("frames", [])[1:]
                good = sorted(frames, key=lambda s: ((s.get("faces") or 0) > 0, min(s.get("sharpness") or 0, 800), -abs((s.get("brightness") or 0) - 110)), reverse=True)
                t = good[0]["t"] if good else 1.5
            run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-ss", f"{max(0.0, float(t)):.2f}", "-i", c["file_path"],
                 "-frames:v", "1", "-q:v", "2", work / "cover.jpg"], timeout=60)
            return f"Clip {c['id']}: cover made from the {float(t):.1f}s frame"
        return None


class TrendWatcher(Agent):
    name, title, crew, lane, interval = "trend_watcher", "Trend Watcher", "intake", "radar", 6 * 3600
    job = "Every 6 hours reads YouTube's most popular gaming videos (1 quota unit) and keeps a list of trending terms, which the Trend Scorer and Hashtag Strategist use."
    backup = "Until YouTube is connected it uses a launch-week seed list (GTA 6, Vice City, Leonida, police chase, first time, glitch...)."

    SEED = ["gta 6", "gta vi", "vice city", "leonida", "lucia", "jason", "police chase", "wanted", "first time", "glitch",
            "physics", "heist", "npc", "funny", "insane", "reaction"]
    STOP = set("the a an and or of to in on for with at by from is it this that you your my me we our are was be new vs - | "
               "how what why when who official video live stream full part episode ep 2026 shorts short".split())

    def work(self, ctx: Ctx):
        from ..posters import get_poster
        from ..posters.base import PostError
        yt = get_poster("youtube", ctx.cfg, ctx.conn)
        terms: Counter = Counter()
        source = "seed"
        if yt.connected():
            try:
                for item in yt.most_popular_gaming():
                    words = [w for w in re.findall(r"[a-z0-9']+", (item.get("title") or "").lower()) if w not in self.STOP and len(w) > 2]
                    terms.update(words)
                    terms.update(" ".join(p) for p in zip(words, words[1:]))
                    terms.update(t.lower() for t in item.get("tags", [])[:10] if len(t) < 30)
                source = "youtube"
            except PostError as e:
                ctx.log(f"YouTube trending unavailable ({str(e)[:120]}); using the seed list", level="warn")
        top = {t: n for t, n in terms.most_common(40) if n >= 2}
        for t in self.SEED:
            top.setdefault(t, 1)
        ctx.set("trends", {"terms": top, "source": source, "at": time.time()})
        return f"Trending terms updated from {source}: {', '.join(list(top)[:6])}"


INTAKE = [StreamRadar, Downloader, Transcriber, SignalAnalyst, MomentPicker, BackupPicker, Editor, CaptionFinisher,
          ClipProfiler, ThumbnailMaker, TrendWatcher]
