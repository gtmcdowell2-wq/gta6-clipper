"""Operations crew: keep the disk clean and the other agents unstuck."""
from __future__ import annotations

import shutil
from pathlib import Path

from .base import Agent, Ctx


class Janitor(Agent):
    name, title, crew, lane, interval = "janitor", "Janitor", "ops", "light", 1800
    job = "Deletes raw VOD files once every clip from them has been reviewed and the retention period has passed, removes rejected clip files after a week, and warns when the disk is filling up."

    def work(self, ctx: Ctx):
        days = ctx.cfg["download"]["raw_vod_retention_days"]
        freed, purged = 0, 0
        vods = ctx.conn.execute(
            "SELECT v.* FROM vods v WHERE v.status IN ('done','selected') AND v.video_path IS NOT NULL"
            " AND v.updated_at <= datetime('now', ?)"
            " AND NOT EXISTS (SELECT 1 FROM clips c WHERE c.vod_id=v.id AND c.status IN ('selected','rendering','pending_review'))"
            " AND NOT EXISTS (SELECT 1 FROM candidates k WHERE k.vod_id=v.id AND k.status='new')",
            (f"-{days} days",)).fetchall()
        for v in vods:
            folder = Path(v["video_path"]).parent
            for name in ("video.mp4", "video.mkv", "video.webm", "audio.wav"):
                f = folder / name
                if f.exists():
                    freed += f.stat().st_size
                    f.unlink()
            ctx.conn.execute("UPDATE vods SET status='purged', video_path=NULL, updated_at=datetime('now') WHERE id=?", (v["id"],))
            purged += 1
        for c in ctx.conn.execute("SELECT id, file_path FROM clips WHERE status='rejected' AND file_path IS NOT NULL"
                                  " AND updated_at <= datetime('now','-7 days')").fetchall():
            f = Path(c["file_path"])
            if f.exists():
                freed += f.stat().st_size
                f.unlink()
            ctx.conn.execute("UPDATE clips SET file_path=NULL WHERE id=?", (c["id"],))
        ctx.conn.execute("DELETE FROM agent_events WHERE ts <= datetime('now','-14 days')")
        ctx.conn.commit()
        free = shutil.disk_usage(ctx.cfg.data_dir).free / 1e9
        if free < ctx.cfg["agents"]["min_free_gb"] * 2:
            ctx.log(f"Disk is getting full: {free:.0f} GB free", level="warn")
        return f"Freed {freed / 1e9:.1f} GB ({purged} raw VOD chunk(s) removed)" if freed else None


class Supervisor(Agent):
    name, title, crew, lane, interval = "supervisor", "Supervisor", "ops", "light", 120
    job = "Watches the other agents: puts work that got stuck mid-step back in the queue, retries failed downloads, and raises an alert when an agent keeps failing."

    def work(self, ctx: Ctx):
        fixed = []
        n = ctx.conn.execute("UPDATE clips SET status='selected', error=NULL WHERE status='rendering' AND updated_at <= datetime('now','-20 minutes')").rowcount
        if n:
            fixed.append(f"{n} stuck render(s)")
        n = ctx.conn.execute("UPDATE vods SET status='new' WHERE status='downloading' AND updated_at <= datetime('now','-4 hours')").rowcount
        if n:
            fixed.append(f"{n} stuck download(s)")
        n = ctx.conn.execute("UPDATE posts SET status='queued', next_try_at=datetime('now','+10 minutes') WHERE status='uploading'"
                             " AND coalesce(next_try_at, created_at) <= datetime('now','-45 minutes') AND posted_at IS NULL").rowcount
        if n:
            fixed.append(f"{n} stuck upload(s)")
        # One automatic retry for failed downloads and renders; after that a person should look.
        for table, back_to in (("vods", "new"), ("clips", "selected")):
            for row in ctx.conn.execute(f"SELECT id FROM {table} WHERE status='failed' AND updated_at <= datetime('now','-15 minutes')").fetchall():
                key = f"retry:{table}:{row['id']}"
                if not ctx.get(key):
                    ctx.set(key, 1)
                    ctx.conn.execute(f"UPDATE {table} SET status=?, updated_at=datetime('now') WHERE id=?", (back_to, row["id"]))
                    fixed.append(f"retrying {table[:-1]} {row['id']}")
        ctx.conn.commit()
        for a in ctx.conn.execute("SELECT name, streak, last_message FROM agents WHERE streak IN (3, 10)").fetchall():
            key = f"alerted:{a['name']}:{a['streak']}"
            if not ctx.get(key):
                ctx.set(key, 1)
                ctx.log(f"{a['name']} has failed {a['streak']} times in a row: {a['last_message']}", level="error")
        ctx.conn.execute("DELETE FROM kv WHERE key LIKE 'alerted:%' AND updated_at <= datetime('now','-1 day')")
        ctx.conn.commit()
        return "Recovered: " + ", ".join(fixed) if fixed else None
