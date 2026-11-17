"""Stage 8: view tracking queries, breakdowns and the campaign CSV export."""
from __future__ import annotations

import csv
import io
import sqlite3
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

LATEST = """
SELECT p.id AS post_id, p.platform, p.url, p.status, p.posted_at, p.platform_post_id,
       c.id AS clip_id, c.post_title, c.archetype, c.caption_style, c.duration_s, c.reach_score,
       CASE WHEN c.hook_used = c.hook_a THEN 'curiosity' WHEN c.hook_used = c.hook_b THEN 'bold' ELSE 'custom' END AS hook_style,
       s.name AS source, s.campaign,
       st.views, st.likes, st.comments, st.shares, st.fetched_at
FROM posts p
JOIN clips c ON c.id = p.clip_id JOIN vods v ON v.id = c.vod_id JOIN sources s ON s.id = v.source_id
LEFT JOIN post_stats st ON st.id = (SELECT id FROM post_stats WHERE post_id = p.id ORDER BY fetched_at DESC, id DESC LIMIT 1)
WHERE p.status IN ('posted', 'draft_sent')
"""


def latest(conn: sqlite3.Connection) -> list[dict]:
    """One row per post with its most recent numbers."""
    return [dict(r) for r in conn.execute(LATEST + " ORDER BY coalesce(st.views, -1) DESC, p.posted_at DESC")]


def breakdown(conn: sqlite3.Connection, by: str, tz: str = "UTC") -> list[dict]:
    """Posts, total and average views grouped by: platform, source, archetype, caption_style, hook_style, hour."""
    groups: dict[str, list[int]] = {}
    for r in latest(conn):
        if r["status"] != "posted":
            continue
        if by == "hour":
            if not r["posted_at"]:
                continue
            local = datetime.strptime(r["posted_at"], "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc).astimezone(ZoneInfo(tz))
            key = f"{local.hour:02d}:00"
        else:
            key = r[by] or "unknown"
        groups.setdefault(key, []).append(r["views"])
    out = []
    for key, views in groups.items():
        known = [v for v in views if v is not None]
        out.append({"key": key, "posts": len(views), "views": sum(known), "avg_views": round(sum(known) / len(known)) if known else 0})
    return sorted(out, key=lambda r: -r["views"])


def totals(conn: sqlite3.Connection) -> dict:
    rows = latest(conn)
    return {"posts": sum(r["status"] == "posted" for r in rows), "drafts": sum(r["status"] == "draft_sent" for r in rows),
            "views": sum(r["views"] or 0 for r in rows), "likes": sum(r["likes"] or 0 for r in rows),
            "comments": sum(r["comments"] or 0 for r in rows), "shares": sum(r["shares"] or 0 for r in rows)}


def export_csv(conn: sqlite3.Connection) -> str:
    """Post links and views, in the shape campaign dashboards ask for."""
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["platform", "post_url", "posted_at_utc", "views", "likes", "comments", "shares", "stats_checked_utc",
                "source", "campaign", "title", "archetype", "clip_id"])
    for r in latest(conn):
        if r["status"] == "posted":
            w.writerow([r["platform"], r["url"] or "", r["posted_at"], r["views"] if r["views"] is not None else "", r["likes"] or "",
                        r["comments"] or "", r["shares"] or "", r["fetched_at"] or "", r["source"], r["campaign"] or "",
                        r["post_title"], r["archetype"], r["clip_id"]])
    return buf.getvalue()
