"""Operations crew: keep the machine healthy, backed up, up to date and talking to you."""
from __future__ import annotations

import os
import shutil
import sqlite3
import subprocess
import sys
import time
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path

from ..config import ROOT
from .base import Agent, Ctx, utcnow


class Ops(Agent):
    crew, lane = "ops", "ops"


class Watchdog(Ops):
    name, title, lane, interval = "watchdog", "Watchdog", "watchdog", 20
    job = "Checks every lane's heartbeat every 20 seconds: restarts any lane whose thread has died, and flags an agent that has been running far longer than it should."
    backup = "The Watchdog's own lane is restarted by a guardian thread inside the orchestrator."

    def work(self, ctx: Ctx):
        if not self.orch:
            return None
        fixed = []
        for lane in self.orch.lane_status():
            if lane["lane"] == "watchdog":
                continue
            if not lane["alive"]:
                self.orch.restarts[lane["lane"]] = self.orch.restarts.get(lane["lane"], 0) + 1
                self.orch.start_lane(lane["lane"])
                err = ctx.get(f"lane_error:{lane['lane']}") or {}
                ctx.alert(f"lane_restart:{lane['lane']}", f"Restarted the {lane['lane']} lane",
                          (err.get("trace") or "the thread had stopped")[-400:], "warn")
                fixed.append(lane["lane"])
            elif lane["running"]:
                agent = self.orch.by_name.get(lane["running"])
                if agent and lane["running_for"] > agent.max_runtime:
                    ctx.conn.execute("UPDATE agents SET state='stuck' WHERE name=?", (agent.name,))
                    ctx.conn.commit()
                    ctx.alert(f"stuck:{agent.name}", f"{agent.title} has been running for {lane['running_for'] / 60:.0f} min",
                              "It may be waiting on a hung download or upload. The Supervisor re-queues stuck work; restart Clip Control if it doesn't recover.", "warn")
                else:
                    ctx.resolve(f"stuck:{lane['running']}")
        ctx.set("lanes", {"at": utcnow(), "lanes": self.orch.lane_status()})
        return f"Restarted lane(s): {', '.join(fixed)}" if fixed else None


class Supervisor(Ops):
    name, title, interval = "supervisor", "Supervisor", 120
    job = "Puts work that got stuck mid-step back in the queue (renders, downloads, uploads), retries failed downloads and renders once, and raises an alert when an agent keeps failing."
    backup = "After one automatic retry it stops and asks for a human, so a broken input can't loop forever."

    def work(self, ctx: Ctx):
        fixed = []
        n = ctx.conn.execute("UPDATE clips SET status='selected', error=NULL WHERE status='rendering' AND updated_at <= datetime('now','-30 minutes')").rowcount
        if n:
            fixed.append(f"{n} stuck render(s)")
        n = ctx.conn.execute("UPDATE vods SET status='new' WHERE status='downloading' AND updated_at <= datetime('now','-4 hours')").rowcount
        if n:
            fixed.append(f"{n} stuck download(s)")
        n = ctx.conn.execute("UPDATE posts SET status='queued', next_try_at=datetime('now','+10 minutes') WHERE status='uploading'"
                             " AND coalesce(upload_started_at, next_try_at, created_at) <= datetime('now','-60 minutes') AND posted_at IS NULL").rowcount
        if n:
            fixed.append(f"{n} stuck upload(s)")
        for table, back_to in (("vods", "new"), ("clips", "selected")):
            for row in ctx.conn.execute(f"SELECT id FROM {table} WHERE status='failed' AND updated_at <= datetime('now','-15 minutes')").fetchall():
                key = f"retry:{table}:{row['id']}"
                if not ctx.get(key):
                    ctx.set(key, 1)
                    ctx.conn.execute(f"UPDATE {table} SET status=?, updated_at=datetime('now') WHERE id=?", (back_to, row["id"]))
                    fixed.append(f"retrying {table[:-1]} {row['id']}")
        ctx.conn.commit()
        for a in ctx.conn.execute("SELECT name, streak, last_message FROM agents WHERE streak >= 3 AND enabled=1").fetchall():
            ctx.alert(f"failing:{a['name']}", f"{a['name']} has failed {a['streak']} times in a row", a["last_message"] or "", "error")
        for row in ctx.conn.execute("SELECT key FROM alerts WHERE key LIKE 'failing:%' AND resolved_at IS NULL").fetchall():
            agent = row["key"].split(":", 1)[1]
            r = ctx.conn.execute("SELECT streak FROM agents WHERE name=?", (agent,)).fetchone()
            if not r or r["streak"] == 0:
                ctx.resolve(row["key"])
        return "Recovered: " + ", ".join(fixed) if fixed else None


class Janitor(Ops):
    name, title, interval = "janitor", "Janitor", 1800
    job = "Deletes raw VOD files once every clip from them has been reviewed and the retention period has passed, and removes rejected clip files after a week."
    backup = "When the Disk Forecaster sees the drive filling fast, it halves the retention period until there's room again."

    def work(self, ctx: Ctx):
        days = ctx.cfg["download"]["raw_vod_retention_days"]
        if ctx.get("janitor_aggressive"):
            days = max(0.5, days / 2)
        freed, purged = 0, 0
        vods = ctx.conn.execute(
            "SELECT v.* FROM vods v WHERE v.status IN ('done','selected') AND v.video_path IS NOT NULL"
            " AND v.updated_at <= datetime('now', ?)"
            " AND NOT EXISTS (SELECT 1 FROM clips c WHERE c.vod_id=v.id AND c.status IN ('selected','rendering','pending_review'))"
            " AND NOT EXISTS (SELECT 1 FROM candidates k WHERE k.vod_id=v.id AND k.status='new')",
            (f"-{days * 24:.0f} hours",)).fetchall()
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
            folder = Path(c["file_path"]).parent
            if folder.exists():
                freed += sum(f.stat().st_size for f in folder.rglob("*") if f.is_file())
                shutil.rmtree(folder, ignore_errors=True)
            ctx.conn.execute("UPDATE clips SET file_path=NULL WHERE id=?", (c["id"],))
        ctx.conn.commit()
        return f"Freed {freed / 1e9:.1f} GB ({purged} raw VOD chunk(s) removed)" if freed else None


class Archivist(Ops):
    name, title, interval = "archivist", "Archivist", 6 * 3600
    job = "Backs up the database every 6 hours (a consistent snapshot, safe while everything runs) plus your config and sources files, keeping the last 2 weeks."
    backup = "If a backup fails it keeps the previous ones untouched and raises an alert; the Backup Verifier checks every new copy."

    def work(self, ctx: Ctx):
        folder = ctx.cfg.data_dir / "backups"
        folder.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d-%H%M")
        target = folder / f"clipper-{stamp}.db"
        dst = sqlite3.connect(target)
        with dst:
            ctx.conn.backup(dst)
        dst.close()
        for name in ("config.yaml", "sources.yaml"):
            if (ROOT / name).exists():
                shutil.copy2(ROOT / name, folder / f"{Path(name).stem}-{stamp}{Path(name).suffix}")
        keep = int(ctx.cfg.get("ops").get("keep_backups", 56))
        for pattern in ("clipper-*.db", "config-*.yaml", "sources-*.yaml"):
            files = sorted(folder.glob(pattern))
            for old in files[:-keep]:
                old.unlink(missing_ok=True)
        ctx.set("last_backup", {"file": target.name, "at": utcnow(), "mb": round(target.stat().st_size / 1e6, 1)})
        ctx.conn.execute("UPDATE agents SET run_requested=1 WHERE name='backup_verifier'")
        ctx.conn.commit()
        return f"Backed up the database ({target.stat().st_size / 1e6:.1f} MB) to backups/{target.name}"


class BackupVerifier(Ops):
    name, title, interval = "backup_verifier", "Backup Verifier", 12 * 3600
    job = "Opens the newest backup on its own, runs SQLite's integrity check and compares row counts with the live database, so a backup is known to restore before you ever need it."
    backup = "A failed check raises an error alert and asks the Archivist for a fresh copy."

    def work(self, ctx: Ctx):
        folder = ctx.cfg.data_dir / "backups"
        files = sorted(folder.glob("clipper-*.db")) if folder.exists() else []
        if not files:
            return None
        newest = files[-1]
        if ctx.get("verified_backup") == newest.name:
            return None
        b = sqlite3.connect(f"file:{newest}?mode=ro", uri=True)
        try:
            ok = b.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
            counts = {t: b.execute(f"SELECT count(*) FROM {t}").fetchone()[0] for t in ("clips", "posts", "sources")}
        finally:
            b.close()
        live = {t: ctx.conn.execute(f"SELECT count(*) FROM {t}").fetchone()[0] for t in counts}
        if not ok or any(counts[t] > live[t] for t in counts):
            ctx.alert("backup_bad", f"Backup {newest.name} failed its check", f"integrity ok={ok}, rows {counts} vs live {live}", "error")
            ctx.conn.execute("UPDATE agents SET run_requested=1 WHERE name='archivist'")
            ctx.conn.commit()
            return f"Backup {newest.name} FAILED verification"
        ctx.resolve("backup_bad")
        ctx.set("verified_backup", newest.name)
        return f"Verified {newest.name}: intact, {counts['clips']} clips, {counts['posts']} posts"


class HealthMonitor(Ops):
    name, title, interval = "health_monitor", "Health Monitor", 300
    job = "Measures the machine every 5 minutes: free disk, database size, memory and, with an NVIDIA card, GPU load, memory and temperature."
    backup = "Readings it can't take on this computer are left blank rather than guessed."

    def work(self, ctx: Ctx):
        h = {"at": utcnow()}
        du = shutil.disk_usage(ctx.cfg.data_dir)
        h.update(disk_free_gb=round(du.free / 1e9, 1), disk_total_gb=round(du.total / 1e9, 1))
        h["db_mb"] = round(ctx.cfg.db_path.stat().st_size / 1e6, 1) if ctx.cfg.db_path.exists() else None
        if hasattr(os, "getloadavg"):
            h["load"] = round(os.getloadavg()[0], 2)
        h["cpus"] = os.cpu_count()
        try:
            meminfo = Path("/proc/meminfo").read_text().split("\n")
            vals = {l.split(":")[0]: int(l.split()[1]) for l in meminfo if ":" in l and l.split()[1].isdigit()}
            h["mem_free_gb"] = round(vals.get("MemAvailable", 0) / 1e6, 1)
        except OSError:
            pass
        if shutil.which("nvidia-smi"):
            try:
                out = subprocess.run(["nvidia-smi", "--query-gpu=name,utilization.gpu,memory.used,memory.total,temperature.gpu",
                                      "--format=csv,noheader,nounits"], capture_output=True, text=True, timeout=15).stdout
                name, util, used, total, temp = [x.strip() for x in out.splitlines()[0].split(",")]
                h["gpu"] = {"name": name, "util": int(util), "mem_used_mb": int(used), "mem_total_mb": int(total), "temp_c": int(temp)}
                if int(temp) >= 88:
                    ctx.alert("gpu_hot", f"GPU is running hot ({temp}°C)", "Check the case fans; NVENC and Whisper will slow down when it throttles.", "warn")
                else:
                    ctx.resolve("gpu_hot")
            except Exception:
                pass
        ctx.set("health", h)
        return None


class NetworkProber(Ops):
    name, title, interval = "network_prober", "Network Prober", 600
    job = "Every 10 minutes checks it can reach Twitch, Kick, YouTube, Google's API, TikTok's API, Instagram's Graph API and Claude, so a network problem is named before uploads fail."
    backup = "One failed check is ignored; three in a row raise an alert naming the site."

    HOSTS = {"Twitch": "https://www.twitch.tv", "Kick": "https://kick.com", "YouTube": "https://www.youtube.com",
             "Google API": "https://www.googleapis.com", "TikTok API": "https://open.tiktokapis.com",
             "Instagram API": "https://graph.facebook.com", "Claude API": "https://api.anthropic.com"}

    def work(self, ctx: Ctx):
        import requests
        state = ctx.get("network") or {}
        bad = []
        for label, url in self.HOSTS.items():
            t0 = time.time()
            try:
                requests.head(url, timeout=10, allow_redirects=False)
                state[label] = {"ok": True, "ms": round((time.time() - t0) * 1000), "fails": 0}
            except requests.RequestException as e:
                prev = state.get(label, {}).get("fails", 0) + 1
                state[label] = {"ok": False, "ms": None, "fails": prev, "error": type(e).__name__}
                if prev >= 3:
                    bad.append(label)
        state["_at"] = utcnow()
        ctx.set("network", state)
        if bad:
            ctx.alert("network", f"Can't reach {', '.join(bad)}", "Check the internet connection, VPN or firewall.", "error")
            return f"Unreachable: {', '.join(bad)}"
        ctx.resolve("network")
        return None


class ClockChecker(Ops):
    name, title, interval = "clock_checker", "Clock Checker", 3600
    job = "Compares this computer's clock with Google's servers every hour; a clock that's minutes off breaks logins and makes scheduled posts go out at the wrong time."
    backup = "If Google can't be reached it tries Anthropic's servers instead."

    def work(self, ctx: Ctx):
        import requests
        for url in ("https://www.googleapis.com", "https://api.anthropic.com"):
            try:
                r = requests.head(url, timeout=10)
                server = parsedate_to_datetime(r.headers["Date"])
            except Exception:
                continue
            drift = (datetime.now(timezone.utc) - server).total_seconds()
            ctx.set("clock", {"drift_s": round(drift, 1), "at": utcnow()})
            if abs(drift) > 120:
                ctx.alert("clock", f"This computer's clock is {abs(drift) / 60:.0f} min {'fast' if drift > 0 else 'slow'}",
                          "Windows: Settings > Time & language > Date & time > Sync now.", "error")
                return f"Clock is off by {drift:.0f}s"
            ctx.resolve("clock")
            return None
        return None


class YtDlpUpdater(Ops):
    name, title, interval = "ytdlp_updater", "yt-dlp Updater", 6 * 3600
    job = "Checks for a new yt-dlp every 6 hours and installs it (sites change and break old versions, which is the most common reason downloads stop), then asks for a restart to load it."
    backup = "If the new version won't import, it reinstalls the previous one straight away."

    def pip(self, *args) -> list[str]:
        if shutil.which("uv") and not (Path(sys.prefix) / ("Scripts" if os.name == "nt" else "bin") / ("pip.exe" if os.name == "nt" else "pip")).exists():
            return ["uv", "pip", "install", "--python", sys.executable, *args]
        return [sys.executable, "-m", "pip", "install", "--disable-pip-version-check", *args]

    def work(self, ctx: Ctx):
        import requests
        from importlib.metadata import version
        if not ctx.cfg.get("ops").get("auto_update_ytdlp", True):
            return None
        current = version("yt-dlp")
        try:
            latest = requests.get("https://pypi.org/pypi/yt-dlp/json", timeout=20).json()["info"]["version"]
        except Exception:
            return None
        ctx.set("ytdlp", {"installed": current, "latest": latest, "at": utcnow()})
        if latest == current:
            return None
        out = subprocess.run(self.pip(f"yt-dlp[default,curl-cffi,deno]=={latest}"), capture_output=True, text=True, timeout=600)
        check = subprocess.run([sys.executable, "-c", "import yt_dlp; print(yt_dlp.version.__version__)"], capture_output=True, text=True, timeout=60)
        if out.returncode != 0 or check.returncode != 0:
            subprocess.run(self.pip(f"yt-dlp[default,curl-cffi,deno]=={current}"), capture_output=True, text=True, timeout=600)
            ctx.alert("ytdlp_update", f"yt-dlp {latest} failed to install; kept {current}", (out.stderr or check.stderr)[-400:], "warn")
            return f"yt-dlp {latest} failed to install; rolled back to {current}"
        ctx.resolve("ytdlp_update")
        ctx.set("restart_recommended", f"yt-dlp updated {current} -> {latest}")
        return f"Installed yt-dlp {latest} (was {current}); it loads at the next restart"


class ConfigWatcher(Ops):
    name, title, interval = "config_watcher", "Config Watcher", 30
    job = "Notices when you edit config.yaml, sources.yaml or .env, checks the new version loads without errors, and asks for a restart to apply it."
    backup = "If an edit breaks the file, it keeps running on the last good settings and shows you the exact error."

    FILES = ("config.yaml", "sources.yaml", ".env")

    def work(self, ctx: Ctx):
        from ..config import ConfigError, load_config
        seen = ctx.get("config_mtimes") or {}
        now = {f: (ROOT / f).stat().st_mtime for f in self.FILES if (ROOT / f).exists()}
        if not seen:
            ctx.set("config_mtimes", now)
            return None
        changed = [f for f in now if now[f] != seen.get(f)]
        if not changed:
            return None
        ctx.set("config_mtimes", now)
        try:
            load_config()
        except (ConfigError, KeyError, ValueError) as e:
            ctx.alert("config_error", f"Your edit to {', '.join(changed)} has an error", str(e)[:400], "error", "/sources")
            return f"{', '.join(changed)} changed but won't load: {e}"
        ctx.resolve("config_error")
        ctx.set("restart_recommended", f"{', '.join(changed)} changed")
        return f"{', '.join(changed)} changed and loads fine; restart to apply"


class LogKeeper(Ops):
    name, title, interval = "log_keeper", "Log Keeper", 6 * 3600
    job = "Keeps the database lean: trims the activity log to 14 days and AI usage records to 60, clears old resolved alerts, and compacts the file once a week."
    backup = "It never touches clips, posts or stats, only logs."

    def work(self, ctx: Ctx):
        n = ctx.conn.execute("DELETE FROM agent_events WHERE ts <= datetime('now','-14 days')").rowcount
        ctx.conn.execute("DELETE FROM alerts WHERE resolved_at IS NOT NULL AND resolved_at <= datetime('now','-7 days')")
        try:
            ctx.conn.execute("DELETE FROM llm_usage WHERE ts <= datetime('now','-60 days')")
        except sqlite3.OperationalError:
            pass
        ctx.conn.commit()
        if time.time() - (ctx.get("last_vacuum") or 0) > 7 * 86400:
            ctx.conn.execute("VACUUM")
            ctx.set("last_vacuum", time.time())
            return f"Trimmed {n} old log rows and compacted the database"
        return f"Trimmed {n} old log rows" if n else None


class AlertDesk(Ops):
    name, title, interval = "alert_desk", "Alert Desk", 120
    job = "Sorts the alerts: closes ones whose problem hasn't been seen for 6 hours, keeps urgent ones pinned, and counts what's open for the dashboard."
    backup = "Urgent alerts (campaign submissions) are never auto-closed; the Campaign Clerk closes them."

    def work(self, ctx: Ctx):
        n = ctx.conn.execute("UPDATE alerts SET resolved_at=datetime('now') WHERE resolved_at IS NULL AND level NOT IN ('urgent','info')"
                             " AND last_seen <= datetime('now','-6 hours')").rowcount
        ctx.conn.execute("UPDATE alerts SET resolved_at=datetime('now') WHERE resolved_at IS NULL AND level='info'"
                         " AND last_seen <= datetime('now','-1 day')")
        ctx.conn.commit()
        counts = {r[0]: r[1] for r in ctx.conn.execute("SELECT level, count(*) FROM alerts WHERE resolved_at IS NULL GROUP BY level")}
        ctx.set("alert_counts", counts)
        return f"Closed {n} stale alert(s)" if n else None


class Notifier(Ops):
    name, title, interval = "notifier", "Notifier", 30
    job = "Sends new alerts to your phone (ntfy app) and/or a Discord channel: urgent campaign submissions instantly, problems as they happen, and the daily digest. Quiet hours hold back anything that can wait."
    backup = "With no channel set up, everything still shows on the dashboard; failed sends are retried next round."

    def work(self, ctx: Ctx):
        from zoneinfo import ZoneInfo

        from ..notify import channels, send
        if not channels():
            return None
        n = ctx.cfg.get("notify")
        levels = set(n.get("levels", ["urgent", "error", "warn", "info"]))
        q_lo, q_hi = n.get("quiet_hours", [1, 8])
        hour = datetime.now(ZoneInfo(ctx.cfg["posting"].get("timezone", "America/New_York"))).hour
        quiet = False if q_lo == q_hi else (q_lo <= hour < q_hi) if q_lo < q_hi else (hour >= q_lo or hour < q_hi)
        sent = 0
        for a in ctx.conn.execute("SELECT * FROM alerts WHERE resolved_at IS NULL AND notified_at IS NULL ORDER BY"
                                  " CASE level WHEN 'urgent' THEN 0 WHEN 'error' THEN 1 WHEN 'warn' THEN 2 ELSE 3 END, id LIMIT 10").fetchall():
            if a["level"] not in levels:
                ctx.conn.execute("UPDATE alerts SET notified_at='skipped' WHERE id=?", (a["id"],))
                continue
            if quiet and a["level"] not in ("urgent", "error"):
                continue
            if send(a["title"], a["detail"] or "", a["level"], a["link"]):
                ctx.conn.execute("UPDATE alerts SET notified_at=datetime('now') WHERE id=?", (a["id"],))
                sent += 1
        ctx.conn.commit()
        return f"Sent {sent} alert(s) to {', '.join(channels())}" if sent else None


class RestartManager(Ops):
    name, title, interval = "restart_manager", "Restart Manager", 60
    job = "Restarts Clip Control cleanly when you press Restart, or after an update or settings change once nothing is downloading, rendering or uploading."
    backup = "If it's never idle, it waits; it never interrupts an upload. With auto-restart off it only reminds you."

    def busy(self, ctx: Ctx) -> bool:
        q = lambda sql: ctx.conn.execute(sql).fetchone()[0]
        return bool(q("SELECT count(*) FROM posts WHERE status='uploading'") or q("SELECT count(*) FROM clips WHERE status='rendering'")
                    or q("SELECT count(*) FROM vods WHERE status='downloading'"))

    def work(self, ctx: Ctx):
        asked = ctx.get("restart_requested")
        recommended = ctx.get("restart_recommended")
        if not asked and not recommended:
            ctx.resolve("restart")
            return None
        auto = ctx.cfg.get("ops").get("auto_restart", True)
        if recommended and not asked:
            ctx.alert("restart", "Restart recommended", f"{recommended}. Clip Control restarts itself when idle." if auto else
                      f"{recommended}. Press Restart on the dashboard.", "info")
            if not auto:
                return None
        if self.busy(ctx):
            return None
        ctx.set("restart_requested", None)
        ctx.set("restart_recommended", None)
        ctx.resolve("restart")
        ctx.log(f"Restarting Clip Control ({asked or recommended})", level="warn")
        if self.orch:
            self.orch.stop.set()
        time.sleep(1)
        if os.environ.get("CLIPPER_SUPERVISED"):
            os._exit(3)  # the launcher starts a fresh process
        os.execv(sys.executable, [sys.executable, *sys.argv])
        return None


class DiskForecaster(Ops):
    name, title, interval = "disk_forecaster", "Disk Forecaster", 1800
    job = "Tracks how fast the drive is filling. If it would hit the safety floor within 6 hours, it tells the Janitor to clean harder now instead of letting downloads stall mid-marathon."
    backup = "With fewer than 3 readings it only compares against the floor."

    def work(self, ctx: Ctx):
        free = shutil.disk_usage(ctx.cfg.data_dir).free / 1e9
        hist = (ctx.get("disk_history") or [])[-47:] + [[time.time(), round(free, 2)]]
        ctx.set("disk_history", hist)
        floor = ctx.cfg["agents"]["min_free_gb"]
        hours_left = None
        if len(hist) >= 3:
            dt = (hist[-1][0] - hist[0][0]) / 3600
            used = hist[0][1] - hist[-1][1]
            if dt > 0 and used > 0:
                hours_left = (free - floor) / (used / dt)
        ctx.set("disk_forecast", {"free_gb": round(free, 1), "hours_to_floor": round(hours_left, 1) if hours_left else None})
        if (hours_left is not None and hours_left < 6) or free < floor * 1.5:
            ctx.set("janitor_aggressive", True)
            ctx.conn.execute("UPDATE agents SET run_requested=1 WHERE name='janitor'")
            ctx.conn.commit()
            ctx.alert("disk_forecast", f"Disk will be full in about {hours_left:.0f}h" if hours_left else f"Disk nearly full ({free:.0f} GB)",
                      "The Janitor is cleaning more aggressively. Free up space or lower download.raw_vod_retention_days.", "warn")
            return f"Disk filling fast ({free:.0f} GB free); Janitor told to clean harder"
        if ctx.get("janitor_aggressive"):
            ctx.set("janitor_aggressive", False)
        ctx.resolve("disk_forecast")
        return None


class PerformanceMonitor(Ops):
    name, title, interval = "performance_monitor", "Performance Monitor", 600
    job = "Watches the production line for backlogs (chunks waiting to download, transcribe or render) and agents that suddenly run much slower than usual, and names the bottleneck."
    backup = "It only reports; the Watchdog and Supervisor do the fixing."

    def work(self, ctx: Ctx):
        q = lambda sql: ctx.conn.execute(sql).fetchone()[0]
        line = {"to_download": q("SELECT count(*) FROM vods WHERE status='new'"),
                "to_transcribe": q("SELECT count(*) FROM vods WHERE status='downloaded'"),
                "to_analyze": q("SELECT count(*) FROM vods WHERE status='transcribed'"),
                "to_pick": q("SELECT count(*) FROM vods WHERE status='analyzed'"),
                "to_render": q("SELECT count(*) FROM clips WHERE status='selected'"),
                "to_review": q("SELECT count(*) FROM clips WHERE status='pending_review'"),
                "to_post": q("SELECT count(*) FROM posts WHERE status='queued'")}
        ctx.set("pipeline", {**line, "at": utcnow()})
        worst = max(("to_download", "to_transcribe", "to_render"), key=lambda k: line[k])
        if line[worst] >= 6:
            ctx.alert("backlog", f"Backlog: {line[worst]} waiting at '{worst.replace('to_', '')}'",
                      "The machine can't keep up with the streams. Fewer sources, a GPU, or shorter retention will help.", "warn")
        else:
            ctx.resolve("backlog")
        slow = [r["name"] for r in ctx.conn.execute("SELECT name FROM agents WHERE last_duration > 60 AND avg_duration > 0"
                                                    " AND last_duration > 3 * avg_duration")]
        return f"Running slower than usual: {', '.join(slow)}" if slow else None


OPS = [Watchdog, Supervisor, Janitor, Archivist, BackupVerifier, HealthMonitor, NetworkProber, ClockChecker, YtDlpUpdater,
       ConfigWatcher, LogKeeper, AlertDesk, Notifier, RestartManager, DiskForecaster, PerformanceMonitor]
