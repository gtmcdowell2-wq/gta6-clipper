"""Campaign desk: paid clipping campaigns, from brief to payout.

Content Rewards (Whop) checks every submission at the door: posted from a linked account, never submitted before,
and posted within the last 30 minutes. A clip that misses the window earns nothing however many views it gets.
There is no public submission API, so the Clerk's job is to get the link in front of you while the window is open.
"""
from __future__ import annotations

import json
from datetime import date, datetime, timezone

from .base import Agent, Ctx, utcnow

FMT = "%Y-%m-%d %H:%M:%S"


def _minutes_since(ts: str) -> float:
    return (datetime.now(timezone.utc) - datetime.strptime(ts, FMT).replace(tzinfo=timezone.utc)).total_seconds() / 60


def _campaign_posts(ctx: Ctx, where: str, args=()):
    return ctx.conn.execute(
        "SELECT p.*, c.post_title, s.name AS source_name, s.campaign, s.campaign_json, s.id AS source_id FROM posts p"
        " JOIN clips c ON c.id=p.clip_id JOIN vods v ON v.id=c.vod_id JOIN sources s ON s.id=v.source_id"
        f" WHERE s.permission='campaign' AND {where}", args).fetchall()


class CampaignClerk(Agent):
    name, title, crew, lane, interval = "campaign_clerk", "Campaign Clerk", "campaign", "campaign", 30
    job = "The moment a campaign clip goes live, puts its link in front of you with a countdown: Content Rewards only accepts posts submitted within 30 minutes of posting. Marks it missed if the window closes."
    backup = "Every due submission is also pinned at the top of the dashboard, so it works even without phone alerts."

    def work(self, ctx: Ctx):
        news = []
        for p in _campaign_posts(ctx, "p.status='posted' AND p.live_at IS NOT NULL AND p.submit_status IS NULL"):
            brief = json.loads(p["campaign_json"] or "{}")
            if p["platform"] not in brief.get("platforms", ["youtube", "tiktok", "instagram"]):
                ctx.conn.execute("UPDATE posts SET submit_status='not_needed' WHERE id=?", (p["id"],))
                continue
            ctx.conn.execute("UPDATE posts SET submit_status='due' WHERE id=?", (p["id"],))
            news.append(p)
        ctx.conn.commit()
        for p in _campaign_posts(ctx, "p.submit_status='due'"):
            brief = json.loads(p["campaign_json"] or "{}")
            window = float(brief.get("submit_within_minutes", 30))
            left = window - _minutes_since(p["live_at"])
            key = f"submit:{p['id']}"
            if left <= 0:
                ctx.conn.execute("UPDATE posts SET submit_status='missed' WHERE id=?", (p["id"],))
                ctx.conn.commit()
                ctx.resolve(key)
                ctx.log(f"Submission window closed for {p['platform']} post {p['id']} ({p['source_name']})", level="warn")
                continue
            ctx.alert(key, f"Submit now to {p['campaign'] or 'the campaign'}: {int(left)} min left",
                      f"{p['post_title']}\n{p['url'] or ''}\nOpen Content Rewards > the campaign > Submit, paste the link, then press 'Submitted' on the dashboard.",
                      "urgent", p["url"])
        return f"{len(news)} campaign post(s) went live: submit them now" if news else None


class SubmissionTracker(Agent):
    name, title, crew, lane, interval = "submission_tracker", "Submission Tracker", "campaign", "campaign", 600
    job = "Keeps the scoreboard for each campaign: submitted, pending review, approved, rejected and missed, and reminds you about clips that sit in 'pending' for more than two days."
    backup = "Statuses you set on the dashboard are the source of truth; nothing is guessed."

    def work(self, ctx: Ctx):
        board: dict[str, dict] = {}
        for p in _campaign_posts(ctx, "p.submit_status IS NOT NULL AND p.submit_status != 'not_needed'"):
            b = board.setdefault(p["campaign"] or p["source_name"], {})
            b[p["submit_status"]] = b.get(p["submit_status"], 0) + 1
        stale = _campaign_posts(ctx, "p.submit_status='submitted' AND p.submitted_at <= datetime('now','-2 days')")
        if stale:
            ctx.alert("stale_submissions", f"{len(stale)} submission(s) pending for 2+ days",
                      "Pending isn't a rejection, but check the brief for a review condition (minimum views, analytics proof).", "info")
        else:
            ctx.resolve("stale_submissions")
        if board != ctx.get("submission_board"):
            ctx.set("submission_board", board)
            return "Scoreboard: " + "; ".join(f"{k}: " + ", ".join(f"{n} {s}" for s, n in v.items()) for k, v in board.items())
        return None


class EarningsAccountant(Agent):
    name, title, crew, lane, interval = "earnings_accountant", "Earnings Accountant", "campaign", "campaign", 1800
    job = "Estimates what each campaign owes you: views on submitted and approved posts x the campaign's rate per 1,000 views, capped per clip, minus the platform fee."
    backup = "Without a rate in the brief it shows views only; estimates are labelled as estimates, not payouts."

    def work(self, ctx: Ctx):
        totals: dict[str, dict] = {}
        for p in _campaign_posts(ctx, "p.submit_status IN ('submitted','approved')"):
            brief = json.loads(p["campaign_json"] or "{}")
            views = ctx.conn.execute("SELECT views FROM post_stats WHERE post_id=? ORDER BY fetched_at DESC LIMIT 1", (p["id"],)).fetchone()
            views = (views["views"] if views else 0) or 0
            cpm = brief.get("cpm")
            est = None
            if cpm:
                est = views / 1000 * float(cpm)
                if brief.get("cap_per_clip"):
                    est = min(est, float(brief["cap_per_clip"]))
                est *= 1 - float(brief.get("fee", 0.0))
            t = totals.setdefault(p["campaign"] or p["source_name"], {"views": 0, "est_usd": 0.0, "posts": 0, "approved": 0})
            t["views"] += views
            t["posts"] += 1
            t["approved"] += p["submit_status"] == "approved"
            t["est_usd"] = round(t["est_usd"] + (est or 0), 2)
        ctx.set("earnings", {"campaigns": totals, "at": utcnow(),
                             "total_usd": round(sum(t["est_usd"] for t in totals.values()), 2)})
        return None


class BudgetWatcher(Agent):
    name, title, crew, lane, interval = "budget_watcher", "Budget Watcher", "campaign", "campaign", 1800
    job = "Pauses new posts for a campaign that has ended, or whose estimated payouts are about to use up its budget, so you don't post clips that can't earn."
    backup = "Without an end date or budget in the brief it never pauses anything."

    def work(self, ctx: Ctx):
        earnings = (ctx.get("earnings") or {}).get("campaigns", {})
        changed = []
        for s in ctx.conn.execute("SELECT * FROM sources WHERE permission='campaign' AND active=1").fetchall():
            brief = ctx.brief(s)
            key = f"campaign_paused:{s['id']}"
            reason = None
            ends = brief.get("ends")
            if ends and str(ends) < date.today().isoformat():
                reason = f"campaign ended {ends}"
            spent = (earnings.get(s["campaign"] or s["name"]) or {}).get("est_usd", 0)
            if brief.get("budget") and spent >= 0.95 * float(brief["budget"]):
                reason = f"about {spent:.0f} of the ${float(brief['budget']):.0f} budget is earned"
            if reason and not ctx.get(key):
                ctx.set(key, reason)
                ctx.alert(key, f"Paused new posts for {s['name']}'s campaign", reason, "warn")
                changed.append(f"paused {s['name']} ({reason})")
            elif not reason and ctx.get(key):
                ctx.set(key, None)
                ctx.resolve(key)
                changed.append(f"resumed {s['name']}")
        return "; ".join(changed) or None


class BriefKeeper(Agent):
    name, title, crew, lane, interval = "brief_keeper", "Brief Keeper", "campaign", "campaign", 900
    job = "Reads sources.yaml like a campaign manager: warns when every streamer is still 'unknown' (so nothing can post), when a campaign source has no rate, disclosure or end date, and when a brief asks for something the bot can't check."
    backup = "It only reports; it never changes permissions or briefs on its own."

    def work(self, ctx: Ctx):
        sources = ctx.conn.execute("SELECT * FROM sources WHERE active=1").fetchall()
        if sources and all(s["permission"] == "unknown" for s in sources):
            ctx.alert("no_postable_sources", "No streamer is cleared for posting yet",
                      "Every source in sources.yaml is 'unknown', so the Scouts are idle and nothing can be posted. "
                      "When you join a campaign, set that streamer's permission to campaign and paste the brief.", "warn", "/sources")
        else:
            ctx.resolve("no_postable_sources")
        for s in sources:
            key = f"brief:{s['id']}"
            if s["permission"] != "campaign":
                ctx.resolve(key)
                continue
            brief = ctx.brief(s)
            missing = [k for k in ("cpm", "ends") if not brief.get(k)]
            if missing:
                ctx.alert(key, f"{s['name']}'s campaign brief is missing {', '.join(missing)}",
                          "Add them under campaign: in sources.yaml so earnings and end dates can be tracked.", "info", "/sources")
            else:
                ctx.resolve(key)
        return None


CAMPAIGN = [CampaignClerk, SubmissionTracker, EarningsAccountant, BudgetWatcher, BriefKeeper]
