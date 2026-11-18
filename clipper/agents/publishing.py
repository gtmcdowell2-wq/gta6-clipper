"""Publishing crew: for every platform, its own Metadata Writer, Timing Planner, Publisher, Stats Tracker,
Token Keeper, Limit Watcher and Momentum Watcher, plus the shared Thumbnail Setter, Draft Reminder and
Breakout Booster.

Per-platform agents run in per-platform lanes, so a TikTok outage or a slow YouTube upload never holds up
the other two.
"""
from __future__ import annotations

import random
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from statistics import median
from zoneinfo import ZoneInfo

from ..posters import POSTERS, NotConnected, PostError, get_poster
from .base import Agent, Ctx, utcnow

FMT = "%Y-%m-%d %H:%M:%S"
LABEL = {"youtube": "YouTube", "tiktok": "TikTok", "instagram": "Instagram"}


def _parse(ts: str) -> datetime:
    return datetime.strptime(ts, FMT).replace(tzinfo=timezone.utc)


class PlatformAgent(Agent):
    crew = "publishing"

    def __init__(self, cfg, platform: str):
        super().__init__(cfg)
        self.platform = platform
        self.name = f"{self.kind}.{platform}"
        self.title = f"{LABEL[platform]} {self.kind_title}"
        self.lane = self.lane_for(platform)

    kind, kind_title = "", ""

    def lane_for(self, platform: str) -> str:
        return f"publish.{platform}"

    def poster(self, ctx: Ctx):
        return get_poster(self.platform, ctx.cfg, ctx.conn)


# ------------------------------------------------------------------ metadata

class MetadataWriter(PlatformAgent):
    kind, kind_title, interval = "metadata", "Metadata Writer", 20

    def __init__(self, cfg, platform):
        super().__init__(cfg, platform)
        self.job = {
            "youtube": "Writes the YouTube version of each post: a searchable title that names the streamer and the game, and a description with the credit line and the first three hashtags YouTube shows above the title.",
            "tiktok": "Writes the TikTok caption for each draft (TikTok's inbox upload has no caption field, so this is the text you paste when you tap Post): hook first, trend-aware tags.",
            "instagram": "Writes the Instagram caption: a clean hook line first, the credit, and at most 5 hashtags (Instagram's limit since December 2025).",
        }[platform]
        self.backup = "If it's paused, the Publisher uses the clip's own title and caption after 10 minutes."

    def lane_for(self, platform):
        return "metadata"

    @staticmethod
    def split_tags(caption: str) -> tuple[str, list[str]]:
        tags = re.findall(r"#\w+", caption or "")
        body = re.sub(r"\s*#\w+", "", caption or "").strip()
        seen, out = set(), []
        for t in tags:
            if t.lower() not in seen:
                seen.add(t.lower())
                out.append(t)
        return body, out

    def write(self, ctx: Ctx, clip, source) -> tuple[str | None, str]:
        name, handle = source["name"], source["key"].split(":", 1)[1]
        where = {"twitch": f"twitch.tv/{handle}", "kick": f"kick.com/{handle}"}.get(source["platform"], source["url"])
        body, tags = self.split_tags(clip["caption"])
        title = (clip["post_title"] or "").strip()
        if self.platform == "youtube":
            if name.lower() not in title.lower() and handle.lower() not in title.lower() and len(title) + len(name) + 3 <= 100:
                title = f"{title} | {name}"
            front = [t for t in tags if t.lower() in ("#gta6", "#gtavi")] + [t for t in tags if t.lower() not in ("#gta6", "#gtavi")]
            desc = f"{body}\n\n🎮 {name} live on {where}\n\n{' '.join(front)}".strip()
            return title[:100], desc[:4900]
        if self.platform == "instagram":
            keep = [t for t in tags if t.lower() in ("#gta6", "#gtavi", "#ad")] + [t for t in tags if t.lower() not in ("#gta6", "#gtavi", "#ad")]
            hook = (clip["hook_used"] or "").strip()
            first = hook if hook and hook.lower() not in body.lower() else ""
            cap = "\n".join(x for x in (first, body, f"🎮 @{handle} · {where}", " ".join(keep[:5])) if x)
            return None, cap[:2200]
        cap = f"{body} {' '.join(tags)}".strip()
        return None, cap[:2200]

    def work(self, ctx: Ctx):
        post = ctx.conn.execute("SELECT * FROM posts WHERE platform=? AND status='queued' AND caption IS NULL ORDER BY scheduled_for LIMIT 1",
                                (self.platform,)).fetchone()
        if not post:
            return None
        clip = ctx.conn.execute("SELECT * FROM clips WHERE id=?", (post["clip_id"],)).fetchone()
        title, caption = self.write(ctx, clip, ctx.source_for_clip(clip))
        ctx.conn.execute("UPDATE posts SET title=?, caption=? WHERE id=?", (title, caption, post["id"]))
        ctx.conn.commit()
        return f"Clip {clip['id']}: {LABEL[self.platform]} text written ({len(caption)} chars)"


# ------------------------------------------------------------------ timing

class TimingPlanner(PlatformAgent):
    kind, kind_title, interval = "timing", "Timing Planner", 30

    def __init__(self, cfg, platform):
        super().__init__(cfg, platform)
        self.job = (f"Gives every approved clip a {LABEL[platform]} post time: highest reach score first, 60-90 minutes apart, "
                    f"inside your audience's waking hours and the hours the Hour Analyst found best, never over the daily cap.")
        self.backup = "With no learned hours yet it spreads posts evenly across the active hours."

    def lane_for(self, platform):
        return "plan"

    def next_slot(self, ctx: Ctx, attended: bool = False) -> datetime:
        p = ctx.cfg["posting"]
        tz = ZoneInfo(p.get("timezone", "America/New_York"))
        lo, hi = p.get("attended_hours", p.get("active_hours", [8, 24])) if attended else p.get("active_hours", [8, 24])
        cap = p["per_platform_daily"].get(self.platform, 6)
        best = set((ctx.get(f"learned_hours:{self.platform}") or {}).get("best", []))
        last = ctx.conn.execute("SELECT max(scheduled_for) FROM posts WHERE platform=? AND status NOT IN ('cancelled','failed')",
                                (self.platform,)).fetchone()[0]
        slot = datetime.now(timezone.utc) + timedelta(minutes=3)
        if last:
            slot = max(slot, _parse(last) + timedelta(minutes=random.uniform(*p["stagger_minutes"])))
        for _ in range(80):
            local = slot.astimezone(tz)
            if local.hour < lo:
                local = local.replace(hour=lo, minute=0, second=0) + timedelta(minutes=random.uniform(0, 25))
            elif local.hour >= hi:
                local = (local + timedelta(days=1)).replace(hour=lo, minute=0, second=0) + timedelta(minutes=random.uniform(0, 25))
            # Prefer a learned best hour if one comes up within the next 3 hours.
            if best and local.hour not in best:
                for k in range(1, 4):
                    cand = local + timedelta(hours=k)
                    if cand.hour in best and lo <= cand.hour < hi:
                        local = cand.replace(minute=random.randint(0, 20), second=0)
                        break
            day_start = local.replace(hour=0, minute=0, second=0, microsecond=0).astimezone(timezone.utc)
            used = ctx.conn.execute(
                "SELECT count(*) FROM posts WHERE platform=? AND status NOT IN ('cancelled','failed') AND scheduled_for >= ? AND scheduled_for < ?",
                (self.platform, day_start.strftime(FMT), (day_start + timedelta(days=1)).strftime(FMT))).fetchone()[0]
            slot = local.astimezone(timezone.utc)
            if used < cap:
                return slot
            slot = (local + timedelta(days=1)).replace(hour=lo, minute=0, second=0).astimezone(timezone.utc) + timedelta(minutes=random.uniform(0, 25))
        return slot

    def work(self, ctx: Ctx):
        if not self.poster(ctx).connected():
            return None
        clips = ctx.conn.execute(
            "SELECT c.id, c.file_path, s.permission, s.id AS source_id, s.campaign_json FROM clips c JOIN vods v ON v.id=c.vod_id"
            " JOIN sources s ON s.id=v.source_id WHERE c.status='approved' AND NOT EXISTS"
            " (SELECT 1 FROM posts p WHERE p.clip_id=c.id AND p.platform=?) ORDER BY c.reach_score IS NULL, c.reach_score DESC, c.id",
            (self.platform,)).fetchall()
        planned = 0
        for clip in clips:
            if not ctx.cfg.can_post(clip["permission"]) or not clip["file_path"] or not Path(clip["file_path"]).exists():
                continue
            if ctx.get(f"campaign_paused:{clip['source_id']}"):
                continue  # the Budget Watcher paused this campaign
            # Campaign posts must be submitted within minutes of going live, so they only go out when you're around.
            attended = clip["permission"] == "campaign" and self.platform != "tiktok"
            slot = self.next_slot(ctx, attended=attended)
            privacy = (ctx.get("youtube_privacy") or ctx.cfg["youtube"].get("privacy_status", "private")) if self.platform == "youtube" else None
            ctx.conn.execute("INSERT OR IGNORE INTO posts (clip_id, platform, scheduled_for, privacy) VALUES (?,?,?,?)",
                             (clip["id"], self.platform, slot.strftime(FMT), privacy))
            ctx.conn.commit()
            planned += 1
        return f"Scheduled {planned} {LABEL[self.platform]} post(s)" if planned else None


# ------------------------------------------------------------------ upload

class Publisher(PlatformAgent):
    kind, kind_title, interval = "publisher", "Publisher", 20
    max_runtime = 3600

    def __init__(self, cfg, platform):
        super().__init__(cfg, platform)
        self.job = {
            "youtube": "Uploads each YouTube Short when its time comes (resumable, so a dropped connection picks up where it stopped), after re-checking the clip is still approved and its source permitted.",
            "tiktok": "Sends each clip to your TikTok inbox as a draft when its time comes; you tap Post in the app. Re-checks approval and permission first.",
            "instagram": "Publishes each Reel when its time comes (resumable upload, then waits for Instagram to finish processing), after re-checking approval and permission.",
        }[platform]
        self.backup = "Network errors retry with growing waits (up to 4 tries); platform limits pause it until the limit resets; a stuck upload is reset by the Supervisor."

    def work(self, ctx: Ctx):
        now = utcnow()
        post = ctx.conn.execute(
            "SELECT * FROM posts WHERE platform=? AND status='queued' AND scheduled_for <= ? AND (next_try_at IS NULL OR next_try_at <= ?)"
            " ORDER BY scheduled_for LIMIT 1", (self.platform, now, now)).fetchone()
        if not post:
            return None
        if post["caption"] is None:
            age = ctx.conn.execute("SELECT (julianday('now') - julianday(?)) * 1440", (post["scheduled_for"],)).fetchone()[0] or 0
            writer = ctx.conn.execute("SELECT enabled FROM agents WHERE name=?", (f"metadata.{self.platform}",)).fetchone()
            if writer and writer["enabled"] and age < 10:
                return None  # give the Metadata Writer a moment
        clip = ctx.conn.execute(
            "SELECT c.*, s.permission, s.name AS source_name FROM clips c JOIN vods v ON v.id=c.vod_id JOIN sources s ON s.id=v.source_id WHERE c.id=?",
            (post["clip_id"],)).fetchone()

        def finish(status: str, **cols):
            sets = "".join(f", {k}=?" for k in cols)
            ctx.conn.execute(f"UPDATE posts SET status=?{sets} WHERE id=?", (status, *cols.values(), post["id"]))
            ctx.conn.commit()

        # The last line of defence: nothing unapproved or unpermitted leaves this machine.
        if clip["status"] != "approved":
            finish("cancelled", error="clip is no longer approved")
            return f"Cancelled {self.platform} post for clip {clip['id']}: not approved"
        if not ctx.cfg.can_post(clip["permission"]):
            finish("cancelled", error=f"source permission is {clip['permission']}")
            return f"Cancelled {self.platform} post for clip {clip['id']}: permission {clip['permission']}"
        if not clip["file_path"] or not Path(clip["file_path"]).exists():
            finish("failed", error="rendered file is missing")
            return f"Clip {clip['id']} file is missing"

        poster = self.poster(ctx)
        later = lambda seconds: (datetime.now(timezone.utc) + timedelta(seconds=seconds)).strftime(FMT)
        try:
            ok, why = poster.can_post()
            if not ok:
                finish("queued", next_try_at=later(1800), error=f"waiting: {why}")
                return None
            finish("uploading", upload_started_at=utcnow())
            result = poster.upload(clip, post)
        except NotConnected as e:
            finish("queued", next_try_at=later(1800), error=str(e))
            ctx.alert(f"not_connected:{self.platform}", f"{LABEL[self.platform]} is disconnected", str(e), "error", "/connectors")
            raise
        except PostError as e:
            attempts = post["attempts"] + 1
            if e.retry_after is None or attempts >= 4:
                finish("failed", attempts=attempts, error=str(e)[:500])
                ctx.alert(f"post_failed:{post['id']}", f"{LABEL[self.platform]} upload failed for clip {clip['id']}", str(e)[:300], "error", "/schedule")
            else:
                finish("queued", attempts=attempts, next_try_at=later(e.retry_after), error=str(e)[:500])
            raise
        except Exception as e:
            finish("queued", attempts=post["attempts"] + 1, next_try_at=later(900), error=f"{type(e).__name__}: {str(e)[:300]}")
            raise
        ctx.resolve(f"not_connected:{self.platform}")
        live = utcnow() if result.status == "posted" else None
        finish(result.status, platform_post_id=result.platform_post_id, url=result.url, posted_at=utcnow(), error=None, live_at=live)
        what = "Draft sent to the TikTok inbox" if result.status == "draft_sent" else f"Posted to {poster.label}"
        return f"{what}: clip {clip['id']} {clip['post_title']!r}" + (f" {result.url}" if result.url else "")


# ------------------------------------------------------------------ after posting

class StatsTracker(PlatformAgent):
    kind, kind_title, interval = "stats", "Stats Tracker", 600

    def __init__(self, cfg, platform):
        super().__init__(cfg, platform)
        self.job = (f"Pulls views, likes, comments and shares for every {LABEL[platform]} post: hourly for the first day, then every 6 hours"
                    + (", and notices when a TikTok draft has been posted from your phone." if platform == "tiktok" else "."))
        self.backup = "If the stats call fails it keeps the last numbers and tries again next round."

    def lane_for(self, platform):
        return f"track.{platform}"

    def work(self, ctx: Ctx):
        rows = ctx.conn.execute(
            "SELECT * FROM posts WHERE platform=? AND ((status='posted' AND (stats_at IS NULL OR stats_at <= datetime('now',"
            " CASE WHEN posted_at >= datetime('now','-1 day') THEN '-1 hours' ELSE '-6 hours' END)))"
            " OR (status='draft_sent' AND (stats_at IS NULL OR stats_at <= datetime('now','-30 minutes')) AND posted_at >= datetime('now','-7 days')))",
            (self.platform,)).fetchall()
        if not rows:
            return None
        try:
            stats = self.poster(ctx).get_stats(rows)
        except NotConnected:
            return None
        except PostError as e:
            ctx.log(f"{self.platform}: stats unavailable ({str(e)[:200]})", level="warn")
            return None
        updated = 0
        for r in rows:
            s = stats.get(r["id"]) or {}
            if s.get("status") == "failed":
                ctx.conn.execute("UPDATE posts SET status='failed', error=? WHERE id=?", (s.get("error"), r["id"]))
            elif s.get("status") == "posted":
                ctx.conn.execute("UPDATE posts SET status='posted', platform_post_id=?, live_at=coalesce(live_at, datetime('now')) WHERE id=?",
                                 (s["platform_post_id"], r["id"]))
            if s.get("url"):
                ctx.conn.execute("UPDATE posts SET url=? WHERE id=?", (s["url"], r["id"]))
            if s.get("views") is not None:
                ctx.conn.execute("INSERT INTO post_stats (post_id, views, likes, comments, shares) VALUES (?,?,?,?,?)",
                                 (r["id"], s["views"], s.get("likes"), s.get("comments"), s.get("shares")))
                updated += 1
            ctx.conn.execute("UPDATE posts SET stats_at=datetime('now') WHERE id=?", (r["id"],))
        ctx.conn.commit()
        return f"Updated {LABEL[self.platform]} stats for {updated} post(s)" if updated else None


class TokenKeeper(PlatformAgent):
    kind, kind_title, interval = "token", "Token Keeper", 1800

    def __init__(self, cfg, platform):
        super().__init__(cfg, platform)
        self.job = (f"Keeps the {LABEL[platform]} login alive: refreshes it before it expires, checks it still works every half hour, "
                    f"and tells you the moment it needs reconnecting instead of letting an upload fail.")
        self.backup = "When a login can't be refreshed it marks the platform disconnected and raises an alert with the fix."

    def lane_for(self, platform):
        return f"track.{platform}"

    def work(self, ctx: Ctx):
        poster = self.poster(ctx)
        key = f"login:{self.platform}"
        if not poster.connected():
            return None
        try:
            if self.platform == "instagram":
                poster.verify()
            else:
                poster._token()  # refreshes when close to expiry (TikTok rotates its refresh token each time)
        except NotConnected as e:
            ctx.alert(key, f"{LABEL[self.platform]} needs reconnecting", str(e), "error", "/connectors")
            return f"{LABEL[self.platform]} login lost: {e}"
        except PostError as e:
            if e.retry_after is None:
                ctx.alert(key, f"{LABEL[self.platform]} login problem", str(e)[:300], "error", "/connectors")
            return None
        ctx.resolve(key)
        return None


class LimitWatcher(PlatformAgent):
    kind, kind_title, interval = "limits", "Limit Watcher", 900

    def __init__(self, cfg, platform):
        super().__init__(cfg, platform)
        self.job = {
            "youtube": "Tracks today's YouTube uploads against your cap and the API's 100-uploads-per-day bucket, plus the general quota used by thumbnails and stats.",
            "tiktok": "Tracks drafts waiting in your TikTok inbox against TikTok's limit of 5 pending drafts per 24 hours.",
            "instagram": "Asks Instagram how much of its 24-hour publishing limit is used, so posts wait instead of failing.",
        }[platform]
        self.backup = "If the platform won't say, it counts posts from the local log."

    def lane_for(self, platform):
        return f"track.{platform}"

    def work(self, ctx: Ctx):
        poster = self.poster(ctx)
        if not poster.connected():
            return None
        cap = ctx.cfg["posting"]["per_platform_daily"].get(self.platform, 6)
        used = poster.posts_last_24h()
        info = {"used": used, "cap": cap, "at": utcnow()}
        if self.platform == "tiktok":
            pending = ctx.conn.execute("SELECT count(*) FROM posts WHERE platform='tiktok' AND status='draft_sent'"
                                       " AND posted_at >= datetime('now','-1 day')").fetchone()[0]
            info["pending_drafts"] = pending
            if pending >= 4:
                ctx.alert("tiktok_drafts", f"{pending} TikTok drafts are waiting in your inbox",
                          "Open TikTok > Inbox and post (or delete) them; TikTok refuses new drafts at 5.", "warn")
            else:
                ctx.resolve("tiktok_drafts")
        if self.platform == "youtube":
            thumbs = ctx.conn.execute("SELECT count(*) FROM posts WHERE platform='youtube' AND thumb_status='set'"
                                      " AND posted_at >= datetime('now','-1 day')").fetchone()[0]
            info["quota_units_est"] = thumbs * 50 + 200  # thumbnails (~50 each) + stats/trends lookups
        ok, why = poster.can_post()
        info["ok"], info["why"] = ok, why
        ctx.set(f"limits:{self.platform}", info)
        return None


class MomentumWatcher(PlatformAgent):
    kind, kind_title, interval = "momentum", "Momentum Watcher", 900

    def __init__(self, cfg, platform):
        super().__init__(cfg, platform)
        self.job = (f"Watches every {LABEL[platform]} post's first 48 hours: views per hour against your typical post at the same age. "
                    f"A clip running at 3x or more is flagged as breaking out, so the Breakout Booster can move it up on the other platforms.")
        self.backup = "Until there are 5 posts to compare against, it uses an absolute threshold (2,000 views in the first 3 hours)."

    def lane_for(self, platform):
        return f"track.{platform}"

    def work(self, ctx: Ctx):
        rows = ctx.conn.execute(
            "SELECT p.id, p.clip_id, p.posted_at, (SELECT views FROM post_stats s WHERE s.post_id=p.id ORDER BY fetched_at DESC LIMIT 1) AS views"
            " FROM posts p WHERE p.platform=? AND p.status='posted' AND p.posted_at >= datetime('now','-2 days')", (self.platform,)).fetchall()
        if not rows:
            return None
        now = datetime.now(timezone.utc)
        rates = []
        for r in rows:
            hours = max(0.5, (now - _parse(r["posted_at"])).total_seconds() / 3600)
            rates.append((r, (r["views"] or 0) / hours, hours))
        typical = median(v for _, v, _ in rates) if len(rates) >= 5 else None
        flagged = []
        for r, rate, hours in rates:
            breaking = (rate >= 3 * typical and (r["views"] or 0) >= 500) if typical else ((r["views"] or 0) >= 2000 and hours <= 3)
            key = f"breakout:{r['clip_id']}"
            if breaking and not ctx.get(key):
                ctx.set(key, {"platform": self.platform, "views": r["views"], "rate": round(rate), "at": utcnow()})
                flagged.append(f"clip {r['clip_id']} ({r['views']:,} views, {rate:,.0f}/h)")
        return f"🔥 Breaking out on {LABEL[self.platform]}: {', '.join(flagged)}" if flagged else None


class ThumbnailSetter(Agent):
    name, title, crew, lane, interval = "thumbnail_setter", "Thumbnail Setter", "publishing", "publish.youtube", 300
    job = "Sets the Thumbnail Maker's cover as each YouTube Short's custom thumbnail once YouTube has finished processing the upload (supported for Shorts since Nov 3, 2026)."
    backup = "If the channel isn't allowed custom thumbnails yet (it needs phone verification) it stops trying and tells you how to enable it."

    def work(self, ctx: Ctx):
        if not ctx.cfg["youtube"].get("custom_thumbnails", True) or ctx.get("yt_thumbs_blocked"):
            return None
        poster = get_poster("youtube", ctx.cfg, ctx.conn)
        if not poster.connected():
            return None
        post = ctx.conn.execute("SELECT p.*, c.file_path FROM posts p JOIN clips c ON c.id=p.clip_id WHERE p.platform='youtube' AND p.status='posted'"
                                " AND p.thumb_status IS NULL AND p.posted_at >= datetime('now','-2 days') ORDER BY p.posted_at LIMIT 1").fetchone()
        if not post:
            return None
        cover = Path(post["file_path"]).parent / "cover.jpg" if post["file_path"] else None
        if not cover or not cover.exists():
            ctx.conn.execute("UPDATE posts SET thumb_status='no_cover' WHERE id=?", (post["id"],))
            ctx.conn.commit()
            return None
        try:
            status = poster.processing_status(post["platform_post_id"])
            if status != "succeeded":
                if status in ("failed", "terminated"):
                    ctx.conn.execute("UPDATE posts SET thumb_status='video_failed' WHERE id=?", (post["id"],))
                    ctx.conn.commit()
                return None  # still processing; try again next round
            poster.set_thumbnail(post["platform_post_id"], cover)
        except PostError as e:
            if "403" in str(e) or "forbidden" in str(e).lower():
                ctx.set("yt_thumbs_blocked", True)
                ctx.alert("yt_thumbs", "YouTube won't accept custom thumbnails yet",
                          "Verify the channel with a phone number at youtube.com/verify, then clear this alert.", "warn")
                return "Custom thumbnails not allowed on this channel yet"
            raise
        ctx.conn.execute("UPDATE posts SET thumb_status='set' WHERE id=?", (post["id"],))
        ctx.conn.commit()
        return f"Custom thumbnail set on {post['url']}"


class DraftReminder(Agent):
    name, title, crew, lane, interval = "draft_reminder", "Draft Reminder", "publishing", "publish.tiktok", 600
    job = "TikTok drafts only go live when you tap Post. This reminds you about drafts waiting more than 2 hours and gives you the caption to paste."
    backup = "Reminders also appear on the dashboard if phone alerts aren't set up."

    def work(self, ctx: Ctx):
        rows = ctx.conn.execute("SELECT p.id, p.clip_id, p.caption, c.post_title FROM posts p JOIN clips c ON c.id=p.clip_id"
                                " WHERE p.platform='tiktok' AND p.status='draft_sent' AND p.posted_at <= datetime('now','-2 hours')"
                                " AND p.posted_at >= datetime('now','-3 days')").fetchall()
        for r in rows:
            ctx.alert(f"draft:{r['id']}", f"TikTok draft waiting: {r['post_title'][:60]}",
                      f"Open TikTok > Inbox, paste this caption and tap Post:\n{r['caption'] or ''}", "warn", "/schedule")
        open_ids = {r["id"] for r in rows}
        for a in ctx.conn.execute("SELECT key FROM alerts WHERE key LIKE 'draft:%' AND resolved_at IS NULL").fetchall():
            if int(a["key"].split(":")[1]) not in open_ids:
                ctx.resolve(a["key"])
        return None


class BreakoutBooster(Agent):
    name, title, crew, lane, interval = "breakout_booster", "Breakout Booster", "publishing", "plan", 300
    job = "When a clip breaks out on one platform, moves its queued posts on the other platforms to the next open slot, while the moment is hot."
    backup = "It never moves a post outside your active hours or over a daily cap; if there's no room it leaves the schedule alone."

    def work(self, ctx: Ctx):
        moved = []
        for row in ctx.conn.execute("SELECT key FROM kv WHERE key LIKE 'breakout:%'").fetchall():
            clip_id = int(row["key"].split(":")[1])
            for post in ctx.conn.execute("SELECT * FROM posts WHERE clip_id=? AND status='queued' AND boosted=0 AND scheduled_for > ?",
                                         (clip_id, (datetime.now(timezone.utc) + timedelta(minutes=20)).strftime(FMT))).fetchall():
                tz = ZoneInfo(ctx.cfg["posting"].get("timezone", "America/New_York"))
                lo, hi = ctx.cfg["posting"].get("active_hours", [8, 24])
                soon = datetime.now(timezone.utc) + timedelta(minutes=10)
                if not lo <= soon.astimezone(tz).hour < hi:
                    continue
                ctx.conn.execute("UPDATE posts SET scheduled_for=?, boosted=1 WHERE id=?", (soon.strftime(FMT), post["id"]))
                moved.append(f"clip {clip_id} on {LABEL.get(post['platform'], post['platform'])}")
        ctx.conn.commit()
        return f"Moved up: {', '.join(moved)}" if moved else None


PER_PLATFORM = [MetadataWriter, TimingPlanner, Publisher, StatsTracker, TokenKeeper, LimitWatcher, MomentumWatcher]
SHARED = [ThumbnailSetter, DraftReminder, BreakoutBooster]


def build_publishing(cfg) -> list[Agent]:
    return [cls(cfg, p) for p in POSTERS for cls in PER_PLATFORM] + [cls(cfg) for cls in SHARED]
