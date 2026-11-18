"""Stage 2: download a VOD (or a time range of one) plus its chat log.

Times stored for a VOD are relative to the downloaded file: 0 is `chunk_start_s` in the original VOD.
"""
from __future__ import annotations

import json
import os
import sqlite3
import subprocess
from pathlib import Path

import yt_dlp
from yt_dlp.utils import download_range_func

from .config import Config
from .util import ffprobe, find_tool, log, safe_name


class Skipped(Exception):
    """The VOD must not be processed (leak keywords, wrong game...)."""


def probe(url: str) -> dict:
    with yt_dlp.YoutubeDL({"quiet": True, "no_warnings": True, "skip_download": True}) as ydl:
        info = ydl.extract_info(url, download=False)
    return {
        "platform_id": str(info["id"]).lstrip("v") if "twitch" in info.get("extractor", "") else str(info["id"]),
        "title": info.get("title"),
        "description": info.get("description"),
        "uploader": info.get("uploader"),
        "uploader_id": info.get("uploader_id"),
        "duration": info.get("duration"),
        "timestamp": info.get("timestamp"),
        "is_live": bool(info.get("is_live")),
        "chapters": [{"start": c["start_time"], "end": c["end_time"], "title": c.get("title", "")}
                     for c in (info.get("chapters") or [])],
    }


def game_ranges(meta: dict, patterns: list[str]) -> list[tuple[float, float]]:
    """Time ranges whose chapter (Twitch category) matches a game pattern.

    Marathon VODs switch games, and the chapter list is the only reliable per-range signal.
    """
    hit = lambda text: any(p.lower() in (text or "").lower() for p in patterns)
    duration = float(meta.get("duration") or 0)
    chapters = sorted(meta["chapters"], key=lambda c: c["start"])
    ranges = []
    for i, c in enumerate(chapters):
        start, end = float(c["start"]), float(c["end"] or 0)
        # While a stream is live, Twitch reports the current chapter with end == start (it hasn't ended yet).
        # A chapter runs until the next one begins; the last one runs to the end of the recording so far.
        until = float(chapters[i + 1]["start"]) if i + 1 < len(chapters) else duration
        if end <= start or i == len(chapters) - 1:
            end = max(end, until)
        if hit(c["title"]) and end > start:
            ranges.append((start, end))
    if not ranges and not chapters and hit(meta["title"]):
        ranges = [(0.0, duration)]
    return ranges


def _platform(url: str) -> str:
    for p in ("twitch", "kick", "youtube", "youtu.be"):
        if p in url:
            return "youtube" if p == "youtu.be" else p
    return "other"


def ensure_source(conn: sqlite3.Connection, platform: str, uploader_id: str | None, uploader: str | None, url: str) -> sqlite3.Row:
    """Match the VOD to a sources.yaml entry; anything unmatched becomes an 'unknown' source."""
    handle = (uploader_id or uploader or "manual").lower()
    row = conn.execute("SELECT * FROM sources WHERE key = ?", (f"{platform}:{handle}",)).fetchone()
    if row:
        return row
    conn.execute(
        "INSERT INTO sources (key, name, platform, url, permission, active, notes) VALUES (?,?,?,?, 'unknown', 0, ?)",
        (f"{platform}:{handle}", uploader or handle, platform, url, "added automatically from a manual URL"))
    conn.commit()
    return conn.execute("SELECT * FROM sources WHERE key = ?", (f"{platform}:{handle}",)).fetchone()


def download_video(url: str, out_dir: Path, max_height: int, start: float | None, end: float | None) -> Path:
    opts = {
        "format": f"bv*[height<={max_height}]+ba/b[height<={max_height}]/b",
        "outtmpl": str(out_dir / "video.%(ext)s"),
        "merge_output_format": "mp4",
        "concurrent_fragment_downloads": 8,
        "retries": 10, "fragment_retries": 10,
        "quiet": True, "no_warnings": True, "noprogress": True,
        "overwrites": True,
    }
    proxy = os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy")
    if proxy:  # range downloads go through ffmpeg, which ignores HTTPS_PROXY unless told
        opts["proxy"] = proxy
    if start is not None or end is not None:
        opts["download_ranges"] = download_range_func(None, [(start or 0, end if end is not None else float("inf"))])
    with yt_dlp.YoutubeDL(opts) as ydl:
        ydl.download([url])
    files = [p for p in out_dir.glob("video.*") if p.suffix in (".mp4", ".mkv", ".webm")]
    if not files:
        raise RuntimeError("yt-dlp finished but produced no video file")
    return files[0]


def download_chat(url: str, platform: str, platform_id: str, out_dir: Path,
                  start: float | None, end: float | None) -> Path | None:
    """Writes chat.json as [{"t": seconds_from_chunk_start, "user": ..., "text": ...}]. None if unavailable."""
    offset = start or 0.0
    messages: list[dict] = []
    try:
        if platform == "twitch":
            tool = find_tool("TwitchDownloaderCLI")
            if not tool:
                log("  chat: TwitchDownloaderCLI not found in ./tools or PATH; continuing without chat")
                return None
            raw = out_dir / "chat_raw.json"
            cmd = [tool, "chatdownload", "--id", platform_id, "-o", str(raw), "--banner", "false", "--collision", "overwrite"]
            if start is not None:
                cmd += ["-b", str(int(start))]
            if end is not None:
                cmd += ["-e", str(int(end))]
            subprocess.run(cmd, check=True, capture_output=True, timeout=3600)
            for c in json.loads(raw.read_text(encoding="utf-8"))["comments"]:
                messages.append({"t": c["content_offset_seconds"] - offset,
                                 "user": (c.get("commenter") or {}).get("display_name") or "",
                                 "text": (c.get("message") or {}).get("body") or ""})
            raw.unlink(missing_ok=True)
        elif platform == "youtube":
            opts = {"skip_download": True, "writesubtitles": True, "subtitleslangs": ["live_chat"],
                    "outtmpl": str(out_dir / "ytchat"), "quiet": True, "no_warnings": True}
            with yt_dlp.YoutubeDL(opts) as ydl:
                ydl.download([url])
            raw = out_dir / "ytchat.live_chat.json"
            if not raw.exists():
                return None
            for line in raw.read_text(encoding="utf-8").splitlines():
                try:
                    replay = json.loads(line)["replayChatItemAction"]
                    t = int(replay["videoOffsetTimeMsec"]) / 1000
                    item = replay["actions"][0]["addChatItemAction"]["item"]["liveChatTextMessageRenderer"]
                    text = "".join(r.get("text") or r.get("emoji", {}).get("shortcuts", [""])[0]
                                   for r in item["message"]["runs"])
                except (KeyError, IndexError, ValueError):
                    continue
                if t >= offset and (end is None or t <= end):
                    messages.append({"t": t - offset, "user": item.get("authorName", {}).get("simpleText", ""), "text": text})
            raw.unlink(missing_ok=True)
        else:
            log(f"  chat: no chat replay support for {platform}; signals will use audio + transcript only")
            return None
    except Exception as e:  # chat is a bonus signal, never a reason to fail the VOD
        log(f"  chat: download failed ({type(e).__name__}: {str(e)[:200]}); continuing without chat")
        return None
    if not messages:
        return None
    messages.sort(key=lambda m: m["t"])
    path = out_dir / "chat.json"
    path.write_text(json.dumps(messages, ensure_ascii=False), encoding="utf-8")
    return path


def download(cfg: Config, conn: sqlite3.Connection, url: str,
             start: float | None = None, end: float | None = None, vod_id: int | None = None) -> int:
    """Download a VOD or a [start, end] range of it. Returns the vod id. Raises Skipped for leaks.

    With vod_id, downloads the range already recorded on that row (the Scout agent creates those).
    """
    meta = probe(url)
    platform = _platform(url)
    if vod_id is not None:
        row = conn.execute("SELECT * FROM vods WHERE id=?", (vod_id,)).fetchone()
        source = conn.execute("SELECT * FROM sources WHERE id=?", (row["source_id"],)).fetchone()
        start, end = row["chunk_start_s"], row["chunk_end_s"]
        chunk_start = float(start or 0)
        if start == 0 and end is None:
            start = None
    else:
        source = ensure_source(conn, platform, meta["uploader_id"], meta["uploader"], url)
        chunk_start = float(start or 0)
        row = conn.execute("SELECT * FROM vods WHERE source_id=? AND platform_id=? AND chunk_start_s=?",
                           (source["id"], meta["platform_id"], chunk_start)).fetchone()
    if row and row["status"] not in ("new", "failed", "downloading", "purged") and row["video_path"] \
            and Path(row["video_path"]).exists():
        log(f"  already downloaded (vod {row['id']}, status {row['status']})")
        return row["id"]
    if row:
        vod_id = row["id"]
    else:
        vod_id = conn.execute(
            "INSERT INTO vods (source_id, platform_id, url, title, description, duration_s, published_at, chunk_start_s, chunk_end_s)"
            " VALUES (?,?,?,?,?,?, datetime(?, 'unixepoch'), ?, ?)",
            (source["id"], meta["platform_id"], url, meta["title"], meta["description"], meta["duration"],
             meta["timestamp"], chunk_start, end)).lastrowid
        conn.commit()

    def set_status(status: str, **extra):
        cols = "".join(f", {k}=?" for k in extra)
        conn.execute(f"UPDATE vods SET status=?, updated_at=datetime('now'){cols} WHERE id=?",
                     (status, *extra.values(), vod_id))
        conn.commit()

    if cfg.is_leak(meta["title"], meta["description"]):
        set_status("skipped", skip_reason="leak keyword in title/description")
        raise Skipped(f"leak keyword in title/description: {meta['title']!r}")

    suffix = f"_{int(chunk_start)}-{int(end)}" if start is not None or end is not None else ""
    out_dir = cfg.data_dir / "vods" / safe_name(source["key"].split(":", 1)[1]) / (safe_name(meta["platform_id"]) + suffix)
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "meta.json").write_text(json.dumps(meta, indent=2, ensure_ascii=False), encoding="utf-8")

    set_status("downloading")
    try:
        log(f"  downloading video: {meta['title']!r} [{chunk_start:.0f}s - {end if end is not None else 'end'}]")
        video = download_video(url, out_dir, cfg["download"]["max_height"], start, end)
        info = ffprobe(video)
        if not info["has_audio"]:
            raise RuntimeError("downloaded file has no audio track")
        log(f"  video: {info['width']}x{info['height']} {info['fps']:.0f}fps, {info['duration']:.0f}s, {video.stat().st_size / 1e6:.0f} MB")
        log("  downloading chat")
        chat = download_chat(url, platform, meta["platform_id"], out_dir, start, end)
    except Exception as e:
        set_status("failed", error=str(e)[:500])
        raise
    set_status("downloaded", video_path=str(video), chat_path=str(chat) if chat else None, error=None)
    return vod_id
