"""Growth lab: learn what earns views on your accounts and feed it back to the other crews.

Each analyst looks at one dimension and only speaks once it has enough posts to be more than noise.
What they learn is stored in kv and read by the Moment Picker, the scorers and the Timing Planners.
"""
from __future__ import annotations

from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from ..stats import breakdown, latest, totals
from .base import Agent, Ctx

MIN_POSTS = 10


class Analyst(Agent):
    crew, lane, interval = "growth", "growth", 1800
    backup = "Until there are enough posts to learn from, it changes nothing and the launch-week defaults stay in charge."


class ArchetypeAnalyst(Analyst):
    name, title = "archetype_analyst", "Archetype Analyst"
    job = "Compares views by clip type (freakout, comedy, discovery, hot take, chaos/skill) and tells the Moment Picker and Archetype Scorer which types are winning."

    def work(self, ctx: Ctx):
        rows = breakdown(ctx.conn, "archetype")
        if sum(r["posts"] for r in rows) < MIN_POSTS:
            return None
        solid = [r for r in rows if r["posts"] >= 3 and r["avg_views"]]
        if not solid:
            return None
        best = max(r["avg_views"] for r in solid)
        learned = dict(ctx.get("learned") or {})
        learned.update(archetype_index={r["key"]: round(0.4 + 0.6 * r["avg_views"] / best, 2) for r in solid},
                       preferred_archetypes=[r["key"] for r in sorted(solid, key=lambda r: -r["avg_views"])[:2]],
                       posts_measured=sum(r["posts"] for r in rows))
        if learned == ctx.get("learned"):
            return None
        ctx.set("learned", learned)
        return f"Winning types: {', '.join(learned['preferred_archetypes'])} ({learned['posts_measured']} posts measured)"


class HourAnalyst(Analyst):
    name, title = "hour_analyst", "Hour Analyst"
    job = "Finds the posting hours that earn the most views on each platform (in your audience's time zone) and hands them to that platform's Timing Planner."

    def work(self, ctx: Ctx):
        tz = ZoneInfo(ctx.cfg["posting"].get("timezone", "America/New_York"))
        notes = []
        for platform in ("youtube", "tiktok", "instagram"):
            hours: dict[int, list[int]] = {}
            for r in latest(ctx.conn):
                if r["platform"] != platform or r["status"] != "posted" or not r["posted_at"] or r["views"] is None:
                    continue
                h = datetime.strptime(r["posted_at"], "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc).astimezone(tz).hour
                hours.setdefault(h, []).append(r["views"])
            if sum(len(v) for v in hours.values()) < MIN_POSTS:
                continue
            ranked = sorted(((sum(v) / len(v), h) for h, v in hours.items() if len(v) >= 2), reverse=True)
            best = [h for _, h in ranked[:4]]
            if best and best != (ctx.get(f"learned_hours:{platform}") or {}).get("best"):
                ctx.set(f"learned_hours:{platform}", {"best": best})
                notes.append(f"{platform}: " + ", ".join(f"{h:02d}:00" for h in best))
        return "Best hours: " + "; ".join(notes) if notes else None


class LengthAnalyst(Analyst):
    name, title = "length_analyst", "Length Analyst"
    job = "Finds which clip lengths (under 25s, 25-40s, 40-60s, over 60s) finish best on your accounts and adjusts the Length Scorer's sweet spot."

    def work(self, ctx: Ctx):
        buckets: dict[str, list[int]] = {}
        for r in latest(ctx.conn):
            if r["status"] == "posted" and r["views"] is not None and r["duration_s"]:
                d = r["duration_s"]
                key = "<25s" if d < 25 else "25-40s" if d < 40 else "40-60s" if d < 60 else ">60s"
                buckets.setdefault(key, []).append(r["views"])
        if sum(len(v) for v in buckets.values()) < MIN_POSTS:
            return None
        avg = {k: round(sum(v) / len(v)) for k, v in buckets.items() if len(v) >= 3}
        if not avg or avg == (ctx.get("learned_length") or {}).get("avg"):
            return None
        ctx.set("learned_length", {"avg": avg, "best": max(avg, key=avg.get)})
        return f"Best length so far: {max(avg, key=avg.get)} ({avg})"


class StyleAnalyst(Analyst):
    name, title = "style_analyst", "Caption Style Analyst"
    job = "Compares views across the three caption looks (bold yellow, clean white, pop green) so the rotation can lean toward what works without becoming one template."

    def work(self, ctx: Ctx):
        rows = [r for r in breakdown(ctx.conn, "caption_style") if r["posts"] >= 3]
        if sum(r["posts"] for r in rows) < MIN_POSTS:
            return None
        ranking = [r["key"] for r in sorted(rows, key=lambda r: -r["avg_views"])]
        if ranking == ctx.get("style_ranking"):
            return None
        ctx.set("style_ranking", ranking)
        return f"Caption styles ranked: {', '.join(ranking)}"


class HookABTester(Analyst):
    name, title = "hook_ab_tester", "Hook A/B Tester"
    job = "Every clip gets two hooks (curiosity and bold) and they alternate. This compares the two styles and reports a winner once each has 8+ posts and one leads by 25%."

    def work(self, ctx: Ctx):
        rows = {r["key"]: r for r in breakdown(ctx.conn, "hook_style")}
        a, b = rows.get("curiosity"), rows.get("bold")
        if not a or not b or a["posts"] < 8 or b["posts"] < 8:
            return None
        lead = (a["avg_views"] or 0) / max(1, b["avg_views"] or 0)
        winner = "curiosity" if lead >= 1.25 else "bold" if lead <= 0.8 else None
        result = {"winner": winner, "curiosity_avg": a["avg_views"], "bold_avg": b["avg_views"]}
        if result == ctx.get("hook_ab"):
            return None
        ctx.set("hook_ab", result)
        return f"Hook test: {winner or 'no clear winner'} (curiosity {a['avg_views']:,} vs bold {b['avg_views']:,} avg views)"


class SourceAnalyst(Analyst):
    name, title = "source_analyst", "Streamer Analyst"
    job = "Ranks streamers by average views per clip on your accounts, so you know whose streams are worth the most review time."

    def work(self, ctx: Ctx):
        rows = [r for r in breakdown(ctx.conn, "source") if r["posts"] >= 3]
        if not rows:
            return None
        ranking = [{"source": r["key"], "avg_views": r["avg_views"], "posts": r["posts"]} for r in sorted(rows, key=lambda r: -r["avg_views"])]
        if ranking == ctx.get("source_ranking"):
            return None
        ctx.set("source_ranking", ranking)
        return "Streamers by views per clip: " + ", ".join(f"{r['source']} {r['avg_views']:,}" for r in ranking[:4])


class PlatformAnalyst(Analyst):
    name, title = "platform_analyst", "Platform Analyst"
    job = "Compares the same clips across YouTube, TikTok and Instagram to show where each kind of moment travels furthest."

    def work(self, ctx: Ctx):
        rows = [r for r in breakdown(ctx.conn, "platform") if r["posts"] >= 3]
        if not rows:
            return None
        ranking = {r["key"]: r["avg_views"] for r in rows}
        if ranking == ctx.get("platform_ranking"):
            return None
        ctx.set("platform_ranking", ranking)
        return "Average views per post: " + ", ".join(f"{k} {v:,}" for k, v in sorted(ranking.items(), key=lambda x: -x[1]))


class DailyDigest(Analyst):
    name, title, interval = "daily_digest", "Daily Digest", 900
    job = "Writes one short morning summary (clips made, approved, posted, views, estimated earnings, anything that needs you) and sends it to your phone."
    backup = "If phone alerts aren't set up, the digest still appears at the top of the dashboard."

    def work(self, ctx: Ctx):
        tz = ZoneInfo(ctx.cfg["posting"].get("timezone", "America/New_York"))
        now = datetime.now(tz)
        hour = int(ctx.cfg.get("notify").get("digest_hour", 9))
        today = now.date().isoformat()
        if now.hour < hour or ctx.get("digest_day") == today:
            return None
        q = lambda sql: ctx.conn.execute(sql).fetchone()[0]
        made = q("SELECT count(*) FROM clips WHERE created_at >= datetime('now','-1 day')")
        approved = q("SELECT count(*) FROM clips WHERE status='approved' AND reviewed_at >= datetime('now','-1 day')")
        waiting = q("SELECT count(*) FROM clips WHERE status='pending_review'")
        posted = q("SELECT count(*) FROM posts WHERE status IN ('posted','draft_sent') AND posted_at >= datetime('now','-1 day')")
        t = totals(ctx.conn)
        money = (ctx.get("earnings") or {}).get("total_usd")
        problems = q("SELECT count(*) FROM alerts WHERE resolved_at IS NULL AND level IN ('error','urgent')")
        text = (f"Last 24h: {made} clips made, {approved} approved, {posted} posted. {waiting} waiting for your review. "
                f"All-time views: {t['views']:,}." + (f" Estimated campaign earnings: ${money:,.2f}." if money else "")
                + (f" {problems} problem(s) need you." if problems else " Nothing is broken."))
        ctx.set("digest_day", today)
        ctx.alert(f"digest:{today}", "Daily digest", text, "info", "/")
        return text


GROWTH = [ArchetypeAnalyst, HourAnalyst, LengthAnalyst, StyleAnalyst, HookABTester, SourceAnalyst, PlatformAnalyst, DailyDigest]
