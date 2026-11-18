"""The control center: review queue, agent management, schedule, connectors, sources and stats.

One Flask app, usable from a phone on the same Wi-Fi. Started by `run_pipeline.py start`, which also
starts the agent network in the same process.
"""
from __future__ import annotations

import json
import os
import secrets
import shutil
import socket
import sqlite3
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

from flask import Flask, Response, abort, g, jsonify, redirect, render_template, request, send_file, session, url_for

from clipper import stats as stats_mod
from clipper.agents import CREWS, Orchestrator, build_agents
from clipper.config import Config, load_config, secret
from clipper.db import connect, init_db, kv_get, kv_set
from clipper.posters import POSTERS, PostError, get_connection, get_poster, save_connection
from clipper.posters import tiktok as tiktok_api
from clipper.posters import youtube as youtube_api
from clipper.util import find_tool, hms

NUDGE_S = 2.0
STATUSES = ("pending_review", "approved", "rejected", "failed", "all")
FMT = "%Y-%m-%d %H:%M:%S"


def ago(ts: str | None) -> str:
    if not ts:
        return "never"
    seconds = (datetime.now(timezone.utc) - datetime.strptime(ts, FMT).replace(tzinfo=timezone.utc)).total_seconds()
    if seconds < 0:
        seconds, suffix = -seconds, ""
        prefix = "in "
    else:
        prefix, suffix = "", " ago"
    for size, unit in ((86400, "d"), (3600, "h"), (60, "m")):
        if seconds >= size:
            return f"{prefix}{int(seconds // size)}{unit}{suffix}"
    return "now" if not prefix else "in <1m"


def create_app(cfg: Config | None = None, start_agents: bool = True) -> Flask:
    cfg = cfg or load_config()
    init_db(cfg).close()
    app = Flask(__name__)
    app.jinja_env.filters["ago"] = ago
    key_file = cfg.data_dir / ".review_secret"
    if not key_file.exists():
        cfg.data_dir.mkdir(parents=True, exist_ok=True)
        key_file.write_text(secrets.token_hex(32))
    app.secret_key = key_file.read_text().strip()
    app.permanent_session_lifetime = timedelta(days=30)
    pin = os.environ.get("REVIEW_PIN") or None
    port = cfg["review_app"]["port"]
    tz = ZoneInfo(cfg["posting"].get("timezone", "America/New_York"))

    agents_list = build_agents(cfg)
    TITLES = {a.name: a.title for a in agents_list}
    ORDER = {a.name: i for i, a in enumerate(agents_list)}
    orchestrator = None
    if start_agents:
        orchestrator = Orchestrator(cfg, agents_list)
        orchestrator.start()
    app.config["orchestrator"] = orchestrator
    app.config["agents"] = agents_list

    def db() -> sqlite3.Connection:
        if "db" not in g:
            g.db = connect(cfg.db_path)
        return g.db

    @app.teardown_appcontext
    def close_db(_exc):
        conn = g.pop("db", None)
        if conn is not None:
            conn.close()

    @app.before_request
    def guard():
        if request.method == "POST":  # reject cross-site form posts
            origin = request.headers.get("Origin") or request.headers.get("Referer")
            if origin and urlparse(origin).netloc != request.host:
                abort(403)
        # OAuth callbacks arrive on 127.0.0.1 without the session cookie; they are protected by their state value.
        returning = request.endpoint == "overview" and request.args.get("state")  # Google's login lands on the root URL
        if pin and not session.get("ok") and not returning and request.endpoint not in ("login", "static", "connect_callback"):
            return render_template("login.html", error=None)

    @app.post("/login")
    def login():
        if pin and secrets.compare_digest(request.form.get("pin", ""), pin):
            session["ok"] = True
            session.permanent = True
            return redirect(url_for("overview"))
        return render_template("login.html", error="Wrong PIN"), 403

    @app.context_processor
    def nav_counts():
        if request.endpoint in ("static", "media"):
            return {}
        c = db()
        return {"nav": {
            "pending": c.execute("SELECT count(*) FROM clips WHERE status='pending_review'").fetchone()[0],
            "errors": c.execute("SELECT count(*) FROM agents WHERE state='error' AND enabled=1").fetchone()[0]},
            "network_on": orchestrator is not None}

    def agent_rows() -> list[dict]:
        rows = {r["name"]: r for r in db().execute("SELECT * FROM agents")}
        out = []
        for a in agents_list:
            r = rows.get(a.name)
            state = "idle" if not r else "paused" if not r["enabled"] else r["state"]
            out.append({"name": a.name, "title": a.title, "crew": a.crew, "job": a.job, "backup": a.backup, "lane": a.lane, "state": state,
                        "cls": {"running": "run", "error": "bad", "idle": "ok", "stuck": "warn"}.get(state, ""),
                        "enabled": bool(r["enabled"]) if r else True, "runs": r["runs"] if r else 0,
                        "failures": r["failures"] if r else 0, "last_message": r["last_message"] if r else None,
                        "last_run_at": r["last_run_at"] if r else None, "last_work_at": r["last_work_at"] if r else None})
        return out

    def events(limit: int, agent: str | None = None) -> list[dict]:
        q = "SELECT * FROM agent_events" + (" WHERE agent=?" if agent else "") + " ORDER BY id DESC LIMIT ?"
        return [dict(r, title=TITLES.get(r["agent"], r["agent"])) for r in db().execute(q, (agent, limit) if agent else (limit,))]

    def local(ts: str | None) -> str:
        if not ts:
            return ""
        return datetime.strptime(ts, FMT).replace(tzinfo=timezone.utc).astimezone(tz).strftime("%a %b %d, %I:%M %p").replace(" 0", " ")

    # ------------------------------------------------------------------ overview

    def youtube_privacy() -> str:
        return kv_get(db(), "youtube_privacy") or cfg["youtube"].get("privacy_status", "private")

    @app.get("/")
    def overview():
        if request.args.get("state") and (request.args.get("code") or request.args.get("error")):
            return finish_connect("youtube")
        c = db()
        one = lambda q, *a: c.execute(q, a).fetchone()[0]
        alerts = []
        if not secret("ANTHROPIC_API_KEY") and cfg["selector"]["provider"] == "anthropic":
            alerts.append({"level": "bad", "title": "No Claude API key.", "text": "The Moment Picker and Hook Critic can't run. Add <code>ANTHROPIC_API_KEY</code> to <code>.env</code> and restart."})
        if not orchestrator and not one("SELECT count(*) FROM sources WHERE active=1 AND permission != 'unknown'"):
            alerts.append({"level": "", "title": "No postable sources.", "text": "Every streamer is still <code>unknown</code>, so the Scout is idle and nothing can be posted. Set a permission in <code>sources.yaml</code> once you have joined a campaign."})
        if not one("SELECT count(*) FROM connections WHERE status='connected'"):
            alerts.append({"level": "", "title": "No platform connected.", "text": f"Approved clips have nowhere to go yet. <a href='{url_for('connectors')}'>Open Connectors</a>."})
        elif youtube_privacy() == "private" and one("SELECT count(*) FROM connections WHERE platform='youtube' AND status='connected'"):
            alerts.append({"level": "info", "title": "YouTube uploads are set to private.", "text": f"Good for the first test. Switch to public on the <a href='{url_for('connectors')}'>Connectors</a> page when you are ready."})
        if cfg["safety"].get("auto_approve_threshold") is not None:
            alerts.append({"level": "", "title": "Auto-approve is on.", "text": "Clips above the threshold are posted without you seeing them."})
        free = shutil.disk_usage(cfg.data_dir).free / 1e9
        if free < cfg["agents"]["min_free_gb"]:
            alerts.append({"level": "bad", "title": "Disk almost full.", "text": f"{free:.0f} GB free. Downloads are paused."})
        for r in c.execute("SELECT name, last_message FROM agents WHERE state='error' AND enabled=1"):
            alerts.append({"level": "bad", "title": f"{TITLES.get(r['name'], r['name'])} is failing:", "text": (r["last_message"] or "")[:200].replace("<", "&lt;")})
        steps = [
            ("Add your Claude API key to .env", bool(secret("ANTHROPIC_API_KEY")), None),
            ("Mark at least one streamer as campaign or public_ok in sources.yaml", bool(one("SELECT count(*) FROM sources WHERE active=1 AND permission != 'unknown'")), url_for("sources_page")),
            ("Connect YouTube", bool(one("SELECT count(*) FROM connections WHERE platform='youtube' AND status='connected'")), url_for("connectors")),
            ("Get a first clip rendered", bool(one("SELECT count(*) FROM clips WHERE file_path IS NOT NULL")), url_for("agents_page")),
            ("Approve a clip", bool(one("SELECT count(*) FROM clips WHERE status='approved'")), url_for("review")),
            ("First post goes out", bool(one("SELECT count(*) FROM posts WHERE status IN ('posted','draft_sent')")), url_for("schedule")),
        ]
        return render_template("mission.html", page="overview", title="Mission Control", alerts=alerts, steps=steps,
                               state=build_state())

    # ------------------------------------------------------------------ mission control (live JSON)

    LAUNCH = datetime(2026, 11, 19, 0, 0, tzinfo=ZoneInfo("America/New_York"))

    def build_state() -> dict:
        from clipper.llm import settings as llm_settings
        from clipper.llm import usage_summary
        c = db()
        q = lambda sql, *a: c.execute(sql, a).fetchone()[0]
        rows = agent_rows()
        status = {r["name"]: r for r in rows}
        crews = []
        for cid, (cname, cdesc) in CREWS.items():
            mine = [r for r in rows if r["crew"] == cid]
            crews.append({"id": cid, "name": cname, "desc": cdesc, "count": len(mine),
                          "working": sum(r["state"] == "running" for r in mine), "errors": sum(r["state"] in ("error", "stuck") for r in mine)})
        stages = [
            ("queued", "Chunks queued", q("SELECT count(*) FROM vods WHERE status='new'"), "downloader"),
            ("download", "Downloading", q("SELECT count(*) FROM vods WHERE status='downloading'"), "downloader"),
            ("transcribe", "Transcribing", q("SELECT count(*) FROM vods WHERE status='downloaded'"), "transcriber"),
            ("analyze", "Finding moments", q("SELECT count(*) FROM vods WHERE status='transcribed'"), "signal_analyst"),
            ("pick", "Picking clips", q("SELECT count(*) FROM vods WHERE status='analyzed'"), "moment_picker"),
            ("render", "Rendering", q("SELECT count(*) FROM clips WHERE status IN ('selected','rendering')"), "editor"),
            ("check", "Agents checking", q("SELECT count(*) FROM clips WHERE status='pending_review' AND recommendation IS NULL"), "gatekeeper"),
            ("review", "Your review", q("SELECT count(*) FROM clips WHERE status='pending_review' AND recommendation IS NOT NULL"), None),
            ("scheduled", "Scheduled", q("SELECT count(*) FROM posts WHERE status IN ('queued','uploading')"), "timing.youtube"),
            ("live", "Live (24h)", q("SELECT count(*) FROM posts WHERE status IN ('posted','draft_sent') AND posted_at >= datetime('now','-1 day')"), None),
        ]
        pipeline = [{"key": k, "label": label, "n": n, "active": bool(agent and status.get(agent, {}).get("state") == "running")}
                    for k, label, n, agent in stages]
        views_total = stats_mod.totals(c)["views"]
        views_24h = q("SELECT coalesce(sum(st.views),0) FROM posts p JOIN post_stats st ON st.id=(SELECT id FROM post_stats WHERE post_id=p.id"
                      " ORDER BY fetched_at DESC LIMIT 1) WHERE p.posted_at >= datetime('now','-1 day')")
        llm = usage_summary(c)
        budget = llm_settings(cfg).get("daily_budget_usd")
        earnings = kv_get(c, "earnings") or {}
        submit = []
        for r in c.execute("SELECT p.id, p.platform, p.url, p.live_at, c.post_title, s.campaign, s.campaign_json FROM posts p JOIN clips c ON c.id=p.clip_id"
                           " JOIN vods v ON v.id=c.vod_id JOIN sources s ON s.id=v.source_id WHERE p.submit_status='due' ORDER BY p.live_at"):
            window = float(json.loads(r["campaign_json"] or "{}").get("submit_within_minutes", 30))
            deadline = datetime.strptime(r["live_at"], FMT).replace(tzinfo=timezone.utc) + timedelta(minutes=window)
            submit.append({"id": r["id"], "platform": r["platform"], "url": r["url"], "title": r["post_title"], "campaign": r["campaign"],
                           "deadline": deadline.timestamp()})
        alerts = [dict(r, ago=ago(r["last_seen"])) for r in c.execute(
            "SELECT id, key, level, title, detail, link, last_seen, count FROM alerts WHERE resolved_at IS NULL ORDER BY"
            " CASE level WHEN 'urgent' THEN 0 WHEN 'error' THEN 1 WHEN 'warn' THEN 2 ELSE 3 END, last_seen DESC LIMIT 30")]
        feed = [{"ago": ago(e["ts"]), "agent": e["title"], "level": e["level"], "msg": e["message"].split("\n")[0][:220], "id": e["id"]}
                for e in events(40)]
        upcoming = [{"platform": r["platform"], "title": r["post_title"], "when": local(r["scheduled_for"]), "rel": ago(r["scheduled_for"]),
                     "status": r["status"]} for r in c.execute(
            "SELECT p.platform, p.scheduled_for, p.status, c.post_title FROM posts p JOIN clips c ON c.id=p.clip_id"
            " WHERE p.status IN ('queued','uploading') ORDER BY p.scheduled_for LIMIT 8")]
        connectors = []
        for name, cls in POSTERS.items():
            conn_row = get_connection(c, name) or {}
            limits = kv_get(c, f"limits:{name}") or {}
            connectors.append({"platform": name, "label": cls.label, "connected": get_poster(name, cfg, c).connected(),
                               "account": conn_row.get("account"), "status": conn_row.get("status", "disconnected"),
                               "used": limits.get("used"), "cap": limits.get("cap"), "pending": limits.get("pending_drafts")})
        sources = [dict(r) for r in c.execute("SELECT name, platform, permission, campaign, live_now, live_title, last_checked FROM sources WHERE active=1 ORDER BY live_now DESC, name")]
        recent = [{"id": r["id"], "title": r["post_title"], "rec": r["recommendation"], "status": r["status"], "reach": r["reach_score"],
                   "thumb": url_for("media", clip_id=r["id"], name="thumb.jpg", v=r["updated_at"]) if r["file_path"] else None}
                  for r in c.execute("SELECT id, post_title, recommendation, status, reach_score, file_path, updated_at FROM clips"
                                     " WHERE status != 'rejected' ORDER BY id DESC LIMIT 6")]
        health = kv_get(c, "health") or {}
        free = shutil.disk_usage(cfg.data_dir)
        health.setdefault("disk_free_gb", round(free.free / 1e9, 1))
        health.setdefault("disk_total_gb", round(free.total / 1e9, 1))
        lanes = orchestrator.lane_status() if orchestrator else []
        launch_in = (LAUNCH - datetime.now(LAUNCH.tzinfo)).total_seconds()
        return {
            "now": datetime.now(timezone.utc).timestamp(), "launch_ts": LAUNCH.timestamp(), "launch_in": launch_in,
            "network": {"running": orchestrator is not None, "paused": bool(kv_get(c, "network_paused", False)), "agents": len(rows),
                        "lanes": len(lanes), "lanes_alive": sum(l["alive"] for l in lanes),
                        "working": sum(r["state"] == "running" for r in rows), "errors": sum(r["state"] in ("error", "stuck") for r in rows),
                        "paused_agents": sum(not r["enabled"] for r in rows), "uptime_s": round(time.time() - orchestrator.started_at) if orchestrator else 0,
                        "restart": kv_get(c, "restart_recommended")},
            "kpis": {"pending": q("SELECT count(*) FROM clips WHERE status='pending_review'"),
                     "ready": q("SELECT count(*) FROM clips WHERE status='pending_review' AND recommendation='ready'"),
                     "approved": q("SELECT count(*) FROM clips WHERE status='approved'"),
                     "scheduled": q("SELECT count(*) FROM posts WHERE status IN ('queued','uploading')"),
                     "live_24h": q("SELECT count(*) FROM posts WHERE status IN ('posted','draft_sent') AND posted_at >= datetime('now','-1 day')"),
                     "views_total": views_total, "views_24h": views_24h, "earnings": earnings.get("total_usd") or 0,
                     "ai_today": llm["today_usd"], "ai_budget": budget, "ai_calls": llm["calls_today"],
                     "clips_today": q("SELECT count(*) FROM clips WHERE created_at >= datetime('now','-1 day')"),
                     "live_streamers": sum(1 for s in sources if s["live_now"]), "open_problems": sum(1 for a in alerts if a["level"] in ("urgent", "error"))},
            "crews": crews, "agents": rows, "pipeline": pipeline, "submit": submit, "alerts": alerts, "feed": feed,
            "upcoming": upcoming, "connectors": connectors, "sources": sources, "recent": recent, "lanes": lanes,
            "health": health, "network_checks": {k: v for k, v in (kv_get(c, "network") or {}).items() if not k.startswith("_")},
            "clock": kv_get(c, "clock"), "backup": kv_get(c, "last_backup"), "verified_backup": kv_get(c, "verified_backup"),
            "ytdlp": kv_get(c, "ytdlp"), "disk_forecast": kv_get(c, "disk_forecast"), "llm": llm,
            "trends": list(((kv_get(c, "trends") or {}).get("terms") or {}).keys())[:12],
        }

    @app.get("/api/state")
    def api_state():
        return jsonify(build_state())

    @app.post("/api/agent/<name>/<action>")
    def api_agent(name: str, action: str):
        if name not in TITLES or action not in ("run", "toggle"):
            abort(404)
        c = db()
        c.execute("INSERT OR IGNORE INTO agents (name) VALUES (?)", (name,))
        if action == "run":
            c.execute("UPDATE agents SET run_requested=1 WHERE name=?", (name,))
        else:
            c.execute("UPDATE agents SET enabled = 1 - enabled, state='idle', streak=0 WHERE name=?", (name,))
        c.commit()
        return jsonify(ok=True)

    @app.post("/api/network/<action>")
    def api_network(action: str):
        if action not in ("pause", "resume"):
            abort(404)
        kv_set(db(), "network_paused", action == "pause")
        db().commit()
        return jsonify(ok=True)

    @app.post("/api/restart")
    def api_restart():
        kv_set(db(), "restart_requested", "you pressed Restart")
        db().commit()
        return jsonify(ok=True)

    @app.post("/api/shutdown")
    def api_shutdown():
        c = db()
        busy = c.execute("SELECT count(*) FROM posts WHERE status='uploading'").fetchone()[0]
        if busy and request.args.get("force") != "1":
            return jsonify(ok=False, reason=f"{busy} upload(s) in progress; try again in a minute")
        if orchestrator:
            orchestrator.stop.set()
        import threading
        threading.Timer(0.8, lambda: os._exit(0)).start()  # exit code 0 tells the launcher not to restart
        return jsonify(ok=True)

    @app.post("/api/alert/<int:alert_id>/dismiss")
    def api_dismiss(alert_id: int):
        db().execute("UPDATE alerts SET resolved_at=datetime('now') WHERE id=?", (alert_id,))
        db().commit()
        return jsonify(ok=True)

    @app.post("/api/post/<int:post_id>/submission/<state>")
    def api_submission(post_id: int, state: str):
        if state not in ("submitted", "approved", "rejected", "missed"):
            abort(404)
        c = db()
        c.execute("UPDATE posts SET submit_status=?, submitted_at=coalesce(submitted_at, datetime('now')) WHERE id=?", (state, post_id))
        c.execute("UPDATE alerts SET resolved_at=datetime('now') WHERE key=? AND resolved_at IS NULL", (f"submit:{post_id}",))
        c.commit()
        return jsonify(ok=True)

    # ------------------------------------------------------------------ agents

    @app.get("/agents")
    def agents_page():
        only = request.args.get("agent") if request.args.get("agent") in TITLES else None
        return render_template("agents.html", page="agents", title="Agents", agents=agent_rows(), crews=CREWS,
                               events=events(80, only), only=TITLES.get(only), live_ms=3000)

    @app.post("/agents/<name>/<action>")
    def agent_action(name: str, action: str):
        if name not in TITLES or action not in ("run", "toggle"):
            abort(404)
        db().execute("INSERT OR IGNORE INTO agents (name) VALUES (?)", (name,))
        if action == "run":
            db().execute("UPDATE agents SET run_requested=1 WHERE name=?", (name,))
        else:
            db().execute("UPDATE agents SET enabled = 1 - enabled, state='idle', streak=0 WHERE name=?", (name,))
        db().commit()
        return redirect(url_for("agents_page"))

    @app.post("/agents-all/<action>")
    def agents_all(action: str):
        if action not in ("pause", "resume"):
            abort(404)
        for name in TITLES:
            db().execute("INSERT OR IGNORE INTO agents (name) VALUES (?)", (name,))
        db().execute("UPDATE agents SET enabled=?, streak=0", (int(action == "resume"),))
        db().commit()
        return redirect(url_for("agents_page"))

    # ------------------------------------------------------------------ review

    def back():
        return redirect(url_for("review", status=request.form.get("status", "pending_review")))

    def get_clip(clip_id: int) -> sqlite3.Row:
        row = db().execute("SELECT * FROM clips WHERE id=?", (clip_id,)).fetchone()
        if row is None:
            abort(404)
        return row

    def rerender(clip_id: int) -> None:
        """Send a clip back to the Editor agent; the quality crew re-checks it afterwards."""
        db().execute("UPDATE clips SET status='selected', error=NULL, updated_at=datetime('now') WHERE id=?", (clip_id,))
        db().execute("UPDATE posts SET status='cancelled', error='clip was re-edited' WHERE clip_id=? AND status='queued'", (clip_id,))
        db().execute("UPDATE agents SET run_requested=1 WHERE name='editor'")

    @app.get("/review")
    def review():
        status = request.args.get("status", "pending_review")
        if status not in STATUSES:
            status = "pending_review"
        where = {"pending_review": "c.status IN ('pending_review','rendering','selected')", "all": "1=1"}.get(status, "c.status = ?")
        args = () if status in ("pending_review", "all") else (status,)
        rows = db().execute(
            f"""SELECT c.*, s.name AS source_name, s.permission FROM clips c
                JOIN vods v ON v.id = c.vod_id JOIN sources s ON s.id = v.source_id
                WHERE {where} ORDER BY c.reach_score IS NULL, c.reach_score DESC, c.id DESC LIMIT 200""", args).fetchall()
        reviews: dict[int, list] = {}
        order = ORDER
        for r in db().execute("SELECT * FROM reviews ORDER BY clip_id"):
            reviews.setdefault(r["clip_id"], []).append(dict(r, title=TITLES.get(r["agent"], r["agent"])))
        clips = []
        for r in rows:
            mine = sorted(reviews.get(r["id"], []), key=lambda x: order.get(x["agent"], 99))
            critic = next((json.loads(x["details"] or "{}") for x in mine if x["agent"] == "hook_critic"), {})
            postable = cfg.can_post(r["permission"])
            confirm = ("This clip is flagged as a possible story spoiler. Approve anyway?" if r["spoiler_risk"] else
                       "The agents recommend rejecting this clip. Approve anyway?" if r["recommendation"] == "reject" and postable else "")
            problems = [x for x in mine if x["verdict"] in ("fail", "warn", "skipped")]
            problems.sort(key=lambda x: {"fail": 0, "warn": 1, "skipped": 2}[x["verdict"]])
            passed = [x for x in mine if x["verdict"] == "pass"]
            clips.append(dict(r, postable=postable, span=f"{hms(r['start_s'])}–{hms(r['end_s'])}", reviews=mine, confirm=confirm,
                              problems=problems, passed=passed,
                              better_title=critic.get("better_title"), better_hook=critic.get("better_hook")))
        counts = {r[0]: r[1] for r in db().execute("SELECT status, count(*) FROM clips GROUP BY status")}
        counts["pending_review"] = sum(counts.get(k, 0) for k in ("pending_review", "rendering", "selected"))
        checkers = sum(1 for x in agents_list if getattr(x, "reviews_clips", False))
        return render_template("review.html", page="review", title="Review", clips=clips, status=status, statuses=STATUSES, counts=counts,
                               checkers=checkers, live_ms=5000)

    @app.get("/media/<int:clip_id>/<name>")
    def media(clip_id: int, name: str):
        if name not in ("clip.mp4", "thumb.jpg"):
            abort(404)
        clip = get_clip(clip_id)
        if not clip["file_path"]:
            abort(404)
        path = Path(clip["file_path"]).parent / name
        if not path.exists():
            abort(404)
        return send_file(path, conditional=True)  # byte ranges, so phones can seek

    @app.post("/clip/<int:clip_id>/save")
    def save(clip_id: int):
        clip = get_clip(clip_id)
        title = request.form.get("post_title", "").strip()[:100] or clip["post_title"]
        caption = request.form.get("caption", "").strip()
        choice = request.form.get("hook_choice", "a")
        hook = {"a": clip["hook_a"], "b": clip["hook_b"]}.get(choice) or " ".join(request.form.get("hook_custom", "").split())[:60] or clip["hook_used"]
        db().execute("UPDATE clips SET post_title=?, caption=?, hook_used=?, updated_at=datetime('now') WHERE id=?", (title, caption, hook, clip_id))
        if hook != clip["hook_used"] and clip["status"] not in ("rendering", "selected"):
            rerender(clip_id)
        elif (title, caption) != (clip["post_title"], clip["caption"]):
            # Text changed: the rule and originality checks no longer describe this clip.
            db().execute("DELETE FROM reviews WHERE clip_id=? AND agent IN ('policy_guard','originality')", (clip_id,))
            db().execute("UPDATE clips SET recommendation=NULL WHERE id=?", (clip_id,))
        db().commit()
        return back()

    @app.post("/clip/<int:clip_id>/apply")
    def apply(clip_id: int):
        clip = get_clip(clip_id)
        row = db().execute("SELECT details FROM reviews WHERE clip_id=? AND agent='hook_critic'", (clip_id,)).fetchone()
        details = json.loads(row["details"] or "{}") if row else {}
        field = request.form.get("field")
        if field == "title" and details.get("better_title"):
            db().execute("UPDATE clips SET post_title=?, updated_at=datetime('now') WHERE id=?", (details["better_title"][:100], clip_id))
            db().execute("DELETE FROM reviews WHERE clip_id=? AND agent IN ('policy_guard','originality')", (clip_id,))
            db().execute("UPDATE clips SET recommendation=NULL WHERE id=?", (clip_id,))
        elif field == "hook" and details.get("better_hook") and clip["status"] not in ("rendering", "selected"):
            db().execute("UPDATE clips SET hook_used=? WHERE id=?", (details["better_hook"][:60], clip_id))
            rerender(clip_id)
        db().commit()
        return back()

    @app.post("/clip/<int:clip_id>/recut")
    def recut(clip_id: int):
        clip = get_clip(clip_id)
        if clip["status"] in ("rendering", "selected"):
            return back()
        edge, _, amount = request.form.get("nudge", "").partition(":")
        if edge not in ("start", "end") or amount not in ("-2", "2"):
            abort(400)
        delta = NUDGE_S if amount == "2" else -NUDGE_S
        segments = json.loads(clip["segments"])
        vod = db().execute("SELECT * FROM vods WHERE id=?", (clip["vod_id"],)).fetchone()
        if not vod["video_path"] or not Path(vod["video_path"]).exists():
            db().execute("UPDATE clips SET error='Re-cut not possible: the raw footage has been cleaned up.' WHERE id=?", (clip_id,))
            db().commit()
            return back()
        transcript = json.loads(Path(vod["transcript_path"]).read_text(encoding="utf-8"))
        if edge == "start":
            segments[0]["start"] = round(max(0.0, segments[0]["start"] + delta), 3)
        else:
            segments[-1]["end"] = round(min(float(transcript["duration"]), segments[-1]["end"] + delta), 3)
        total = sum(s["end"] - s["start"] for s in segments)
        sel = cfg["selector"]
        if any(s["end"] - s["start"] < 1 for s in segments) or not sel["min_seconds"] <= total <= sel["max_seconds"]:
            db().execute("UPDATE clips SET error=? WHERE id=?",
                         (f"Re-cut not applied: the clip must stay between {sel['min_seconds']} and {sel['max_seconds']} seconds.", clip_id))
            db().commit()
            return back()
        db().execute("UPDATE clips SET segments=?, start_s=?, end_s=?, duration_s=? WHERE id=?",
                     (json.dumps(segments, ensure_ascii=False), segments[0]["start"], segments[-1]["end"], round(total, 2), clip_id))
        rerender(clip_id)
        db().commit()
        return back()

    @app.post("/clip/<int:clip_id>/decide")
    def decide(clip_id: int):
        clip = get_clip(clip_id)
        decision = request.form.get("decision")
        if decision not in ("approved", "rejected") or clip["status"] in ("rendering", "selected"):
            return back()
        if decision == "approved" and not clip["file_path"]:
            return back()
        db().execute("UPDATE clips SET status=?, reviewed_at=datetime('now'), updated_at=datetime('now') WHERE id=?", (decision, clip_id))
        if decision == "rejected":
            db().execute("UPDATE posts SET status='cancelled', error='clip was rejected' WHERE clip_id=? AND status='queued'", (clip_id,))
        else:
            db().execute("UPDATE agents SET run_requested=1 WHERE name='timing_planner'")
        db().commit()
        return back()

    # ------------------------------------------------------------------ schedule

    POST_Q = """SELECT p.*, c.post_title, c.reach_score, s.name AS source,
                (SELECT views FROM post_stats WHERE post_id=p.id ORDER BY id DESC LIMIT 1) AS views
                FROM posts p JOIN clips c ON c.id=p.clip_id JOIN vods v ON v.id=c.vod_id JOIN sources s ON s.id=v.source_id"""

    @app.get("/schedule")
    def schedule():
        c = db()
        day_start = datetime.now(tz).replace(hour=0, minute=0, second=0, microsecond=0).astimezone(timezone.utc)
        platforms = []
        for name, cls in POSTERS.items():
            today = c.execute("SELECT count(*) FROM posts WHERE platform=? AND status NOT IN ('cancelled','failed') AND scheduled_for >= ? AND scheduled_for < ?",
                              (name, day_start.strftime(FMT), (day_start + timedelta(days=1)).strftime(FMT))).fetchone()[0]
            platforms.append({"label": cls.label, "connected": get_poster(name, cfg, c).connected(), "today": today,
                              "cap": max(1, cfg["posting"]["per_platform_daily"].get(name, 6))})
        decorate = lambda rows, key: [dict(r, local=local(r[key] or r["scheduled_for"]), rel=ago(r["scheduled_for"])) for r in rows]
        upcoming = decorate(c.execute(POST_Q + " WHERE p.status IN ('queued','uploading') ORDER BY p.scheduled_for LIMIT 200").fetchall(), "scheduled_for")
        history = decorate(c.execute(POST_Q + " WHERE p.status NOT IN ('queued','uploading') ORDER BY coalesce(p.posted_at, p.scheduled_for) DESC LIMIT 100").fetchall(), "posted_at")
        return render_template("schedule.html", page="schedule", title="Schedule", platforms=platforms, upcoming=upcoming, history=history,
                               tz=str(tz), stagger=cfg["posting"]["stagger_minutes"], hours=cfg["posting"].get("active_hours", [8, 24]), live_ms=6000)

    @app.post("/post/<int:post_id>")
    def post_action(post_id: int):
        post = db().execute("SELECT * FROM posts WHERE id=?", (post_id,)).fetchone()
        action = request.form.get("action")
        if not post or action not in ("now", "earlier", "later", "cancel", "retry"):
            abort(404)
        now = datetime.now(timezone.utc)
        when = datetime.strptime(post["scheduled_for"], FMT).replace(tzinfo=timezone.utc) if post["scheduled_for"] else now
        if post["status"] == "queued":
            if action == "cancel":
                db().execute("UPDATE posts SET status='cancelled', error='cancelled by you' WHERE id=?", (post_id,))
            elif action in ("now", "earlier", "later"):
                new = now if action == "now" else max(now, when + timedelta(hours=-1 if action == "earlier" else 1))
                db().execute("UPDATE posts SET scheduled_for=?, next_try_at=NULL WHERE id=?", (new.strftime(FMT), post_id))
                if action == "now":
                    db().execute("UPDATE agents SET run_requested=1 WHERE name=?", (f"publisher.{post['platform']}",))
        elif post["status"] in ("failed", "cancelled") and action == "retry":
            db().execute("UPDATE posts SET status='queued', attempts=0, error=NULL, next_try_at=NULL, scheduled_for=? WHERE id=?", (now.strftime(FMT), post_id))
        db().commit()
        return redirect(url_for("schedule"))

    # ------------------------------------------------------------------ connectors

    def redirect_uri(platform: str) -> str:
        if platform == "youtube":  # Google documents the loopback redirect as exactly http://127.0.0.1:port
            return f"http://127.0.0.1:{port}"
        return f"http://127.0.0.1:{port}/connect/{platform}/callback/"

    @app.get("/connectors")
    def connectors():
        c = db()
        flash = session.pop("flash", None)
        steps = {
            "youtube": ["console.cloud.google.com → new project → APIs &amp; Services → Library → enable <b>YouTube Data API v3</b>.",
                        "Google Auth Platform → Get started: app name, your email, audience <b>External</b>.",
                        "Audience → <b>Publish app</b> so the status reads In production (in Testing the login expires every 7 days).",
                        "Clients → Create client → <b>Desktop app</b> → Download JSON → save it as <code>client_secret.json</code> in the project folder.",
                        "Press Connect. Google shows an 'unverified app' screen: Advanced → Go to (your app). Tick both YouTube permissions."],
            "tiktok": ["developers.tiktok.com → create an app → add <b>Login Kit</b> and <b>Content Posting API</b>.",
                       f"Login Kit → Desktop → redirect URI: <code>{redirect_uri('tiktok')}</code>",
                       "Scopes: <code>user.info.basic</code>, <code>video.upload</code>, <code>video.list</code>.",
                       "Put the client key and secret in <code>.env</code>, restart, press Connect. Each clip arrives as a draft in your TikTok inbox; you tap Post."],
            "instagram": ["Switch the Instagram account to <b>Professional</b> and link it to a Facebook Page.",
                          "developers.facebook.com → create a Business app → add Instagram (API setup with <b>Facebook Login</b>).",
                          "Graph API Explorer → generate a token with <code>instagram_basic</code>, <code>instagram_content_publish</code>, <code>instagram_manage_insights</code>, <code>pages_show_list</code>, <code>pages_read_engagement</code>, then extend it to a long-lived token.",
                          "Put <code>IG_USER_ID</code> and <code>IG_ACCESS_TOKEN</code> in <code>.env</code>, restart, press Check token."],
        }
        notes = {"youtube": "Uploads directly as a Short. 100 uploads/day API limit; your own cap is in config.yaml.",
                 "tiktok": "Sends drafts to your inbox (max 5 waiting). Public auto-posting needs TikTok's app audit.",
                 "instagram": "Publishes Reels directly from the file on this PC. Tokens last about 60 days."}
        socials = []
        for name, cls in POSTERS.items():
            poster = get_poster(name, cfg, c)
            configured, missing = poster.configured()
            conn_row = get_connection(c, name) or {}
            connected = poster.connected()
            state = "connected" if connected else conn_row.get("status") if conn_row.get("status") in ("expired", "error") else "ready to connect" if configured else "needs setup"
            socials.append({"platform": name, "label": cls.label, "configured": configured, "connected": connected, "state": state,
                            "cls": "ok" if connected else "bad" if state in ("expired", "error") else "info" if configured else "",
                            "account": conn_row.get("account") if connected else None, "note": notes[name] if configured else missing,
                            "detail": conn_row.get("detail") if not connected else None, "steps": steps[name]})
        privacy = youtube_privacy()
        tool = lambda name, ok, good, bad, warn=False: {"name": name, "cls": "ok" if ok else "warn" if warn else "bad", "state": "ok" if ok else "missing", "detail": good if ok else bad}
        import importlib.util
        gpu = bool(shutil.which("nvidia-smi"))
        import yt_dlp
        tools = [
            tool("Claude API key", bool(secret("ANTHROPIC_API_KEY")), f"Moment Picker uses {cfg['selector']['model']}; Hook Critic uses {cfg['agents']['llm_model']}", "Add ANTHROPIC_API_KEY to .env"),
            tool("ffmpeg", bool(shutil.which("ffmpeg")), shutil.which("ffmpeg") or "", "Not on PATH: run setup.ps1"),
            tool("NVIDIA GPU", gpu, "Whisper and video encoding use the GPU", "Not found: transcription and rendering run on the CPU (slower, still works)", warn=True),
            tool("TwitchDownloaderCLI", bool(find_tool("TwitchDownloaderCLI")), "Twitch chat replays available", "Not found: Twitch clips lose the chat signal", warn=True),
            tool("yt-dlp", True, f"version {yt_dlp.version.__version__}. Update weekly: pip install -U yt-dlp", ""),
            tool("Whisper", bool(importlib.util.find_spec("faster_whisper")), f"GPU model {cfg['transcribe']['model_gpu']}, CPU model {cfg['transcribe']['model_cpu']}", "faster-whisper is not installed"),
            tool("Review PIN", bool(pin), "Phone access is PIN-protected", "Not set: anyone on your Wi-Fi can open this page. Add REVIEW_PIN to .env", warn=True),
        ]
        return render_template("connectors.html", page="connectors", title="Connectors", socials=socials, tools=tools, port=port, flash=flash, privacy=privacy)

    @app.get("/connect/<platform>")
    def connect_start(platform: str):
        if platform not in ("youtube", "tiktok"):
            abort(404)
        if request.host.split(":")[0] != "127.0.0.1":
            session["flash"] = {"level": "", "text": f"Open http://127.0.0.1:{port}/connectors on the PC running the bot and connect from there. Logins only return to that address."}
            return redirect(url_for("connectors"))
        configured, missing = get_poster(platform, cfg, db()).configured()
        if not configured:
            session["flash"] = {"level": "", "text": f"{platform.title()} is not set up yet: {missing}"}
            return redirect(url_for("connectors"))
        api = youtube_api if platform == "youtube" else tiktok_api
        url, state, verifier = api.auth_url(redirect_uri(platform))
        kv_set(db(), f"oauth:{state}", {"platform": platform, "verifier": verifier, "at": datetime.now(timezone.utc).strftime(FMT)})
        db().commit()
        return redirect(url)

    @app.get("/connect/<platform>/callback")
    @app.get("/connect/<platform>/callback/")
    def connect_callback(platform: str):
        return finish_connect(platform)

    def finish_connect(platform: str):
        state = request.args.get("state", "")
        saved = kv_get(db(), f"oauth:{state}") if state else None
        db().execute("DELETE FROM kv WHERE key=?", (f"oauth:{state}",))
        db().commit()
        fresh = saved and datetime.now(timezone.utc) - datetime.strptime(saved["at"], FMT).replace(tzinfo=timezone.utc) < timedelta(minutes=15)
        if not saved or saved["platform"] != platform or not fresh:
            session["flash"] = {"level": "bad", "text": "That login link was not started here or has expired. Press Connect again."}
        elif request.args.get("error") or not request.args.get("code"):
            session["flash"] = {"level": "bad", "text": f"{platform.title()} did not grant access: {request.args.get('error_description') or request.args.get('error') or 'no code returned'}"}
        else:
            api = youtube_api if platform == "youtube" else tiktok_api
            try:
                name = api.exchange_code(db(), request.args["code"], saved["verifier"], redirect_uri(platform))
                session["flash"] = {"level": "info", "text": f"{platform.title()} connected" + (f" as {name}." if name else ".")}
            except PostError as e:
                session["flash"] = {"level": "bad", "text": f"Could not finish connecting {platform.title()}: {e}"}
        return redirect(url_for("connectors"))

    @app.post("/connect/instagram/verify")
    def ig_verify():
        try:
            name = get_poster("instagram", cfg, db()).verify()
            session["flash"] = {"level": "info", "text": f"Instagram connected as {name}."}
        except PostError as e:
            session["flash"] = {"level": "bad", "text": f"Instagram rejected the token: {e}"}
        return redirect(url_for("connectors"))

    @app.post("/settings/youtube-privacy")
    def set_youtube_privacy():
        value = request.form.get("privacy")
        if value not in ("private", "unlisted", "public"):
            abort(400)
        kv_set(db(), "youtube_privacy", value)
        db().execute("UPDATE posts SET privacy=? WHERE platform='youtube' AND status='queued'", (value,))  # applies to what is already scheduled
        db().commit()
        session["flash"] = {"level": "info", "text": f"New YouTube uploads will be {value}."}
        return redirect(url_for("connectors"))

    @app.post("/disconnect/<platform>")
    def disconnect(platform: str):
        if platform not in POSTERS:
            abort(404)
        save_connection(db(), platform, account="", tokens={}, status="disconnected")
        return redirect(url_for("connectors"))

    # ------------------------------------------------------------------ sources + stats

    @app.get("/sources")
    def sources_page():
        rows = db().execute(
            """SELECT s.*, (SELECT count(*) FROM vods v WHERE v.source_id=s.id) AS vods,
                      (SELECT count(*) FROM clips c JOIN vods v ON v.id=c.vod_id WHERE v.source_id=s.id) AS clips,
                      (SELECT count(*) FROM posts p JOIN clips c ON c.id=p.clip_id JOIN vods v ON v.id=c.vod_id
                        WHERE v.source_id=s.id AND p.status IN ('posted','draft_sent')) AS posted
               FROM sources s ORDER BY s.active DESC, s.permission, s.name""").fetchall()
        return render_template("sources.html", page="sources", title="Sources", sources=[dict(r, postable=cfg.can_post(r["permission"])) for r in rows])

    @app.get("/stats")
    def stats_page():
        c = db()
        tables = []
        for label, by in (("By platform", "platform"), ("By source", "source"), ("By archetype", "archetype"),
                          ("By caption style", "caption_style"), ("By hook style", "hook_style"), ("By posting hour", "hour")):
            rows = stats_mod.breakdown(c, by, str(tz))
            top = max((r["avg_views"] for r in rows), default=0) or 1
            tables.append((label, [dict(r, pct=round(100 * r["avg_views"] / top)) for r in rows]))
        return render_template("stats.html", page="stats", title="Stats", t=stats_mod.totals(c), tables=tables,
                               rows=stats_mod.latest(c)[:200], learned=kv_get(c, "learned"))

    @app.get("/stats/export.csv")
    def stats_csv():
        name = f"clip-views-{datetime.now(tz).strftime('%Y-%m-%d')}.csv"
        return Response(stats_mod.export_csv(db()), mimetype="text/csv", headers={"Content-Disposition": f"attachment; filename={name}"})

    return app


def lan_ip() -> str:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("10.255.255.255", 1))
            return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"


def port_in_use(port: int) -> bool:
    """True when a server is answering on the port (a socket left over from a restart doesn't count)."""
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=1):
            return True
    except OSError:
        return False


def serve(cfg: Config, start_agents: bool = True) -> int:
    host, port = cfg["review_app"]["host"], cfg["review_app"]["port"]
    # Claim the port before any agent starts: two copies must never run agents on one database.
    if port_in_use(port):
        print(f"Port {port} is already in use: Clip Control is probably already running (open http://127.0.0.1:{port}).\n"
              f"If something else uses that port, change review_app.port in config.yaml.", flush=True)
        return 0
    app = create_app(cfg, start_agents)
    print(f"Clip Control:  http://127.0.0.1:{port}   (this PC; use this address to connect accounts)")
    if host == "0.0.0.0":
        print(f"On your phone (same Wi-Fi):  http://{lan_ip()}:{port}")
        if not os.environ.get("REVIEW_PIN"):
            print("Tip: set REVIEW_PIN in .env so others on your network can't approve clips.")
    print(f"Agent network: {'running, ' + str(len(app.config['agents'])) + ' agents' if start_agents else 'off (review only)'}", flush=True)
    try:
        from waitress import serve as waitress_serve  # a production server; steadier than Flask's on Windows
    except ImportError:
        waitress_serve = None
    if waitress_serve:
        waitress_serve(app, host=host, port=port, threads=16, channel_timeout=120, ident="Clip Control")
    else:
        import logging
        logging.getLogger("werkzeug").setLevel(logging.WARNING)  # the live refresh would flood the console
        app.run(host=host, port=port, threaded=True, use_reloader=False)
    return 0
