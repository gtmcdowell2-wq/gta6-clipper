"""Publishing crew: plan post times, upload, track views, learn what works."""
from __future__ import annotations

import random
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from ..posters import POSTERS, NotConnected, PostError, get_poster
from .base import Agent, Ctx, utcnow

FMT = "%Y-%m-%d %H:%M:%S"


def _parse(ts: str) -> datetime:
    return datetime.strptime(ts, FMT).replace(tzinfo=timezone.utc)


class TimingPlanner(Agent):
    name, title, crew, lane, interval = "timing_planner", "Timing Planner", "publishing", "light", 30
    job = "Gives every approved clip a post time on each connected platform: highest reach score first, 60-90 minutes apart, inside your audience's waking hours, never over a platform's daily cap."

    def next_slot(self, ctx: Ctx, platform: str) -> datetime:
        p = ctx.cfg["posting"]
        tz = ZoneInfo(p.get("timezone", "America/New_York"))
        lo, hi = p.get("active_hours", [8, 24])
        cap = p["per_platform_daily"].get(platform, 6)
        last = ctx.conn.execute("SELECT max(scheduled_for) FROM posts WHERE platform=? AND status NOT IN ('cancelled','failed')", (platform,)).fetchone()[0]
        slot = datetime.now(timezone.utc) + timedelta(minutes=3)
        if last:
            slot = max(slot, _parse(last) + timedelta(minutes=random.uniform(*p["stagger_minutes"])))
        for _ in range(60):
            local = slot.astimezone(tz)
            if local.hour < lo:
                local = local.replace(hour=lo, minute=0, second=0) + timedelta(minutes=random.uniform(0, 25))
            elif local.hour >= hi:
                local = (local + timedelta(days=1)).replace(hour=lo, minute=0, second=0) + timedelta(minutes=random.uniform(0, 25))
            day_start = local.replace(hour=0, minute=0, second=0, microsecond=0).astimezone(timezone.utc)
            used = ctx.conn.execute(
                "SELECT count(*) FROM posts WHERE platform=? AND status NOT IN ('cancelled','failed') AND scheduled_for >= ? AND scheduled_for < ?",
                (platform, day_start.strftime(FMT), (day_start + timedelta(days=1)).strftime(FMT))).fetchone()[0]
            slot = local.astimezone(timezone.utc)
            if used < cap:
                return slot
            slot = (local + timedelta(days=1)).replace(hour=lo, minute=0, second=0).astimezone(timezone.utc) + timedelta(minutes=random.uniform(0, 25))
        return slot

    def work(self, ctx: Ctx):
        platforms = [name for name in POSTERS if get_poster(name, ctx.cfg, ctx.conn).connected()]
        if not platforms:
            return None
        clips = ctx.conn.execute(
            "SELECT c.id, c.file_path, s.permission FROM clips c JOIN vods v ON v.id=c.vod_id JOIN sources s ON s.id=v.source_id"
            " WHERE c.status='approved' ORDER BY c.reach_score IS NULL, c.reach_score DESC, c.id").fetchall()
        planned = 0
        for clip in clips:
            if not ctx.cfg.can_post(clip["permission"]) or not clip["file_path"] or not Path(clip["file_path"]).exists():
                continue
            for platform in platforms:
                if ctx.conn.execute("SELECT 1 FROM posts WHERE clip_id=? AND platform=?", (clip["id"], platform)).fetchone():
                    continue
                slot = self.next_slot(ctx, platform)
                privacy = (ctx.get("youtube_privacy") or ctx.cfg["youtube"].get("privacy_status", "private")) if platform == "youtube" else None
                ctx.conn.execute("INSERT INTO posts (clip_id, platform, scheduled_for, privacy) VALUES (?,?,?,?)",
                                 (clip["id"], platform, slot.strftime(FMT), privacy))
                ctx.conn.commit()
                planned += 1
        return f"Scheduled {planned} post(s) across {', '.join(platforms)}" if planned else None


class Publisher(Agent):
    name, title, crew, lane, interval = "publisher", "Publisher", "publishing", "publish", 20
    job = "Uploads each post when its time comes: YouTube Shorts directly, Instagram Reels directly, TikTok as a draft in your inbox. Re-checks approval and permission first, retries on network errors, and waits when a platform limit is hit."

    def work(self, ctx: Ctx):
        now = utcnow()
        post = ctx.conn.execute(
            "SELECT * FROM posts WHERE status='queued' AND scheduled_for <= ? AND (next_try_at IS NULL OR next_try_at <= ?)"
            " ORDER BY scheduled_for LIMIT 1", (now, now)).fetchone()
        if not post:
            return None
        clip = ctx.conn.execute(
            "SELECT c.*, s.permission FROM clips c JOIN vods v ON v.id=c.vod_id JOIN sources s ON s.id=v.source_id WHERE c.id=?",
            (post["clip_id"],)).fetchone()

        def finish(status: str, **cols):
            sets = "".join(f", {k}=?" for k in cols)
            ctx.conn.execute(f"UPDATE posts SET status=?{sets} WHERE id=?", (status, *cols.values(), post["id"]))
            ctx.conn.commit()

        # The last line of defence: nothing unapproved or unpermitted leaves this machine.
        if clip["status"] != "approved":
            finish("cancelled", error="clip is no longer approved")
            return f"Cancelled {post['platform']} post for clip {clip['id']}: not approved"
        if not ctx.cfg.can_post(clip["permission"]):
            finish("cancelled", error=f"source permission is {clip['permission']}")
            return f"Cancelled {post['platform']} post for clip {clip['id']}: permission {clip['permission']}"
        if not clip["file_path"] or not Path(clip["file_path"]).exists():
            finish("failed", error="rendered file is missing")
            return f"Clip {clip['id']} file is missing"

        poster = get_poster(post["platform"], ctx.cfg, ctx.conn)
        later = lambda seconds: (datetime.now(timezone.utc) + timedelta(seconds=seconds)).strftime(FMT)
        try:
            ok, why = poster.can_post()
            if not ok:
                finish("queued", next_try_at=later(1800), error=f"waiting: {why}")
                return None
            finish("uploading")
            result = poster.upload(clip, post)
        except NotConnected as e:
            finish("queued", next_try_at=later(1800), error=str(e))
            raise
        except PostError as e:
            attempts = post["attempts"] + 1
            if e.retry_after is None or attempts >= 4:
                finish("failed", attempts=attempts, error=str(e)[:500])
            else:
                finish("queued", attempts=attempts, next_try_at=later(e.retry_after), error=str(e)[:500])
            raise
        except Exception as e:
            finish("queued", attempts=post["attempts"] + 1, next_try_at=later(900), error=f"{type(e).__name__}: {str(e)[:300]}")
            raise
        finish(result.status, platform_post_id=result.platform_post_id, url=result.url, posted_at=utcnow(), error=None)
        what = "Draft sent to the TikTok inbox" if result.status == "draft_sent" else f"Posted to {poster.label}"
        return f"{what}: clip {clip['id']} {clip['post_title']!r}" + (f" {result.url}" if result.url else "")


class StatsTracker(Agent):
    name, title, crew, lane, interval = "stats_tracker", "Stats Tracker", "publishing", "publish", 600
    job = "Every 6 hours pulls views, likes, comments and shares for each post, and notices when a TikTok draft has been posted from your phone."

    def work(self, ctx: Ctx):
        rows = ctx.conn.execute(
            "SELECT * FROM posts WHERE (status='posted' AND (stats_at IS NULL OR stats_at <= datetime('now','-6 hours')))"
            " OR (status='draft_sent' AND (stats_at IS NULL OR stats_at <= datetime('now','-1 hours')) AND posted_at >= datetime('now','-7 days'))").fetchall()
        if not rows:
            return None
        updated = 0
        for platform in POSTERS:
            mine = [r for r in rows if r["platform"] == platform]
            if not mine:
                continue
            try:
                stats = get_poster(platform, ctx.cfg, ctx.conn).get_stats(mine)
            except PostError as e:
                ctx.log(f"{platform}: stats unavailable ({str(e)[:200]})", level="warn")
                continue
            for r in mine:
                s = stats.get(r["id"]) or {}
                if s.get("status") == "failed":
                    ctx.conn.execute("UPDATE posts SET status='failed', error=? WHERE id=?", (s.get("error"), r["id"]))
                elif s.get("status") == "posted":
                    ctx.conn.execute("UPDATE posts SET status='posted', platform_post_id=? WHERE id=?", (s["platform_post_id"], r["id"]))
                if s.get("url"):
                    ctx.conn.execute("UPDATE posts SET url=? WHERE id=?", (s["url"], r["id"]))
                if s.get("views") is not None:
                    ctx.conn.execute("INSERT INTO post_stats (post_id, views, likes, comments, shares) VALUES (?,?,?,?,?)",
                                     (r["id"], s["views"], s.get("likes"), s.get("comments"), s.get("shares")))
                    updated += 1
                ctx.conn.execute("UPDATE posts SET stats_at=datetime('now') WHERE id=?", (r["id"],))
            ctx.conn.commit()
        return f"Updated stats for {updated} post(s)" if updated else None


class GrowthAnalyst(Agent):
    name, title, crew, lane, interval = "growth_analyst", "Growth Analyst", "publishing", "light", 1800
    job = "Compares views by archetype, caption style, hook style, source and posting hour, then feeds the winners back: the Moment Picker is told which archetypes are working and the Reach Forecaster re-weights them."

    def work(self, ctx: Ctx):
        from ..stats import breakdown
        rows = breakdown(ctx.conn, "archetype")
        total_posts = sum(r["posts"] for r in rows)
        if total_posts < 10:
            return None  # too little data to learn from
        solid = [r for r in rows if r["posts"] >= 3 and r["avg_views"]]
        if not solid:
            return None
        best = max(r["avg_views"] for r in solid)
        tz = ctx.cfg["posting"].get("timezone", "America/New_York")
        learned = {
            "archetype_index": {r["key"]: round(0.4 + 0.6 * r["avg_views"] / best, 2) for r in solid},
            "preferred_archetypes": [r["key"] for r in sorted(solid, key=lambda r: -r["avg_views"])[:2]],
            "best_hours": [r["key"] for r in sorted((r for r in breakdown(ctx.conn, "hour", tz) if r["posts"] >= 3),
                                                    key=lambda r: -r["avg_views"])[:4]],
            "posts_measured": total_posts,
        }
        if learned == ctx.get("learned"):
            return None
        ctx.set("learned", learned)
        return f"Winners so far: {', '.join(learned['preferred_archetypes'])} ({total_posts} posts measured)"
