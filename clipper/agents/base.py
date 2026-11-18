"""The agent framework: every agent has one job, a schedule, a pause switch, an activity log and a backup plan.

Agents are grouped into lanes. Agents in the same lane take turns on one thread, so two heavy jobs never
fight over the GPU, while independent lanes (each platform's publisher, the inspectors, the scouts) run in
parallel. A lane thread can't die from an agent's mistake: every run is wrapped, every lane beats a heart,
and the Watchdog agent restarts any lane whose thread has stopped.
"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
import traceback
from datetime import datetime, timezone

from ..config import Config
from ..db import connect, kv_get, kv_set

CREWS = {
    "intake": ("Intake crew", "Finds GTA 6 footage and turns it into rendered clips"),
    "inspect": ("Inspection crew", "Each inspector checks one thing on every finished clip"),
    "judges": ("Judges panel", "Critiques, scores reach and decides what is ready for you"),
    "publishing": ("Publishing crew", "Writes per-platform posts, schedules, uploads and tracks them"),
    "campaign": ("Campaign desk", "Keeps paid campaigns compliant, submitted on time and paid"),
    "growth": ("Growth lab", "Learns what works and feeds it back to the other crews"),
    "ops": ("Operations crew", "Keeps the machine healthy, backed up, updated and talking to you"),
}

FMT = "%Y-%m-%d %H:%M:%S"
NETWORK_VERSION = 2  # bump when the set of reviewers changes, so pending clips are judged again


def utcnow() -> str:
    return datetime.now(timezone.utc).strftime(FMT)


class Agent:
    name = ""        # stable id, e.g. "publisher.youtube"
    title = ""       # shown in the control center
    crew = "ops"
    job = ""         # one sentence: what this agent is responsible for
    backup = ""      # one sentence: what happens when its normal method fails
    interval = 60    # seconds between runs when there is nothing to do
    lane = "light"   # agents in the same lane never run at the same time
    max_runtime = 1200  # seconds one run may take before the Watchdog calls it stuck

    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.orch: Orchestrator | None = None

    def work(self, ctx: "Ctx") -> str | None:
        """Do one unit of work. Return a short summary, or None if there was nothing to do."""
        raise NotImplementedError

    def meta(self) -> dict:
        return {"name": self.name, "title": self.title, "crew": self.crew, "job": self.job, "backup": self.backup,
                "lane": self.lane, "interval": self.interval}


class Ctx:
    def __init__(self, cfg: Config, conn: sqlite3.Connection, agent: Agent):
        self.cfg, self.conn, self.agent = cfg, conn, agent

    def log(self, message: str, level: str = "info", clip_id: int | None = None, vod_id: int | None = None) -> None:
        self.conn.execute("INSERT INTO agent_events (agent, level, message, clip_id, vod_id) VALUES (?,?,?,?,?)",
                          (self.agent.name, level, message[:1000], clip_id, vod_id))
        self.conn.commit()

    def get(self, key: str, default=None):
        return kv_get(self.conn, key, default)

    def set(self, key: str, value) -> None:
        kv_set(self.conn, key, value)
        self.conn.commit()

    # -- alerts: one row per problem, deduplicated by key, resolved when the problem goes away
    def alert(self, key: str, title: str, detail: str = "", level: str = "warn", link: str | None = None) -> None:
        self.conn.execute(
            "INSERT INTO alerts (key, level, title, detail, agent, link) VALUES (?,?,?,?,?,?)"
            " ON CONFLICT(key) DO UPDATE SET level=excluded.level, title=excluded.title, detail=excluded.detail,"
            " link=excluded.link, last_seen=datetime('now'), count=count+1,"
            " resolved_at=NULL, notified_at=CASE WHEN resolved_at IS NOT NULL THEN NULL ELSE notified_at END",
            (key, level, title[:200], detail[:1000], self.agent.name, link))
        self.conn.commit()

    def resolve(self, key: str) -> None:
        self.conn.execute("UPDATE alerts SET resolved_at=datetime('now') WHERE key=? AND resolved_at IS NULL", (key,))
        self.conn.commit()

    def review(self, clip_id: int, verdict: str, summary: str, score: float | None = None, details: dict | None = None) -> None:
        self.conn.execute(
            "INSERT INTO reviews (clip_id, agent, verdict, score, summary, details) VALUES (?,?,?,?,?,?)"
            " ON CONFLICT(clip_id, agent) DO UPDATE SET verdict=excluded.verdict, score=excluded.score,"
            " summary=excluded.summary, details=excluded.details, created_at=datetime('now')",
            (clip_id, self.agent.name, verdict, score, summary[:500], json.dumps(details or {}, ensure_ascii=False, default=str)))
        self.conn.commit()

    def unreviewed(self, extra_where: str = "", need_facts: bool = False) -> sqlite3.Row | None:
        """The next clip in the review queue this agent hasn't looked at yet."""
        facts = " AND EXISTS (SELECT 1 FROM clip_facts f WHERE f.clip_id=c.id)" if need_facts else ""
        return self.conn.execute(
            f"SELECT c.* FROM clips c WHERE c.status='pending_review' {extra_where}{facts}"
            " AND NOT EXISTS (SELECT 1 FROM reviews r WHERE r.clip_id=c.id AND r.agent=?) ORDER BY c.id LIMIT 1",
            (self.agent.name,)).fetchone()

    def facts(self, clip_id: int) -> dict:
        row = self.conn.execute("SELECT facts FROM clip_facts WHERE clip_id=?", (clip_id,)).fetchone()
        return json.loads(row[0]) if row else {}

    def source_for_clip(self, clip) -> sqlite3.Row:
        return self.conn.execute("SELECT s.* FROM sources s JOIN vods v ON v.source_id=s.id WHERE v.id=?",
                                 (clip["vod_id"],)).fetchone()

    def brief(self, source) -> dict:
        """Campaign brief for a source row ({} when it has none)."""
        try:
            return json.loads(source["campaign_json"] or "{}") if source is not None else {}
        except (TypeError, ValueError):
            return {}


class Orchestrator:
    """Runs every agent on its schedule, one thread per lane, with heartbeats the Watchdog reads."""

    def __init__(self, cfg: Config, agents: list[Agent]):
        self.cfg, self.agents = cfg, agents
        self.by_name = {a.name: a for a in agents}
        self.stop = threading.Event()
        self._next: dict[str, float] = {a.name: 0.0 for a in agents}
        self.threads: dict[str, threading.Thread] = {}
        self.beats: dict[str, float] = {}               # lane -> last loop time
        self.current: dict[str, tuple[str, float]] = {}  # lane -> (agent running, since)
        self.restarts: dict[str, int] = {}
        self.started_at = time.time()
        for a in agents:
            a.orch = self
        conn = connect(cfg.db_path)
        for a in agents:
            conn.execute("INSERT OR IGNORE INTO agents (name) VALUES (?)", (a.name,))
        conn.execute("UPDATE agents SET state='idle' WHERE state IN ('running','stuck')")  # nothing is running at startup
        names = [a.name for a in agents]
        marks = ",".join("?" * len(names))
        # Verdicts from agents that no longer exist would confuse the Gatekeeper; drop them.
        stale = conn.execute(f"DELETE FROM reviews WHERE agent NOT IN ({marks})", names).rowcount
        if kv_get(conn, "network_version") != NETWORK_VERSION:
            # A new set of checkers: judge everything in the review queue again from scratch.
            conn.execute("DELETE FROM reviews WHERE clip_id IN (SELECT id FROM clips WHERE status='pending_review')")
            conn.execute("UPDATE clips SET recommendation=NULL, rec_reason=NULL, caption_final=0 WHERE status='pending_review'")
            kv_set(conn, "network_version", NETWORK_VERSION)
        elif stale:
            conn.execute("UPDATE clips SET recommendation=NULL WHERE status='pending_review'")
        conn.execute(f"DELETE FROM agents WHERE name NOT IN ({marks})", names)
        conn.commit()
        conn.close()

    @property
    def lanes(self) -> list[str]:
        return sorted({a.lane for a in self.agents})

    def run_agent(self, agent: Agent, conn: sqlite3.Connection) -> str | None:
        """One run with bookkeeping. Returns the summary if the agent did work."""
        conn.execute("UPDATE agents SET state='running', run_requested=0, last_run_at=? WHERE name=?", (utcnow(), agent.name))
        conn.commit()
        ctx = Ctx(self.cfg, conn, agent)
        t0 = time.time()
        try:
            summary = agent.work(ctx)
        except Exception as e:
            try:
                conn.rollback()
            except sqlite3.Error:
                pass
            message = f"{type(e).__name__}: {str(e)[:400]}"
            ctx.log(message + "\n" + traceback.format_exc()[-800:], level="error")
            conn.execute("UPDATE agents SET state='error', failures=failures+1, streak=streak+1, runs=runs+1, last_message=?,"
                         " last_duration=? WHERE name=?", (message, round(time.time() - t0, 2), agent.name))
            conn.commit()
            return None
        took = round(time.time() - t0, 2)
        if summary:
            ctx.log(summary)
            conn.execute("UPDATE agents SET state='idle', runs=runs+1, streak=0, last_work_at=?, last_message=?, last_duration=?,"
                         " avg_duration=coalesce(avg_duration*0.8 + ?*0.2, ?) WHERE name=?",
                         (utcnow(), summary[:500], took, took, took, agent.name))
        else:
            conn.execute("UPDATE agents SET state='idle', runs=runs+1, streak=0, last_duration=? WHERE name=?", (took, agent.name))
        conn.commit()
        return summary

    def _lane(self, lane: str) -> None:
        mine = [a for a in self.agents if a.lane == lane]
        while not self.stop.is_set():
            self.beats[lane] = time.time()
            busy = False
            try:
                conn = connect(self.cfg.db_path)
            except sqlite3.Error:
                self.stop.wait(5)
                continue
            try:
                paused = bool(kv_get(conn, "network_paused", False))
                rows = {r["name"]: r for r in conn.execute("SELECT name, enabled, run_requested FROM agents")}
                for agent in mine:
                    if self.stop.is_set():
                        break
                    self.beats[lane] = time.time()
                    row = rows.get(agent.name)
                    if row is None:
                        conn.execute("INSERT OR IGNORE INTO agents (name) VALUES (?)", (agent.name,))
                        conn.commit()
                        continue
                    due = time.time() >= self._next[agent.name]
                    if not (row["run_requested"] or (row["enabled"] and due and not paused)):
                        continue
                    self.current[lane] = (agent.name, time.time())
                    try:
                        did = self.run_agent(agent, conn)
                    finally:
                        self.current.pop(lane, None)
                    streak = conn.execute("SELECT streak FROM agents WHERE name=?", (agent.name,)).fetchone()[0]
                    if streak:  # back off after errors: 1, 2, 4... minutes, capped at 30
                        self._next[agent.name] = time.time() + min(60 * 2 ** (streak - 1), 1800)
                    else:
                        self._next[agent.name] = time.time() + (1 if did else agent.interval)
                    busy = busy or bool(did)
            except sqlite3.Error:
                pass  # a locked database is retried on the next pass
            except Exception:  # never let a lane die; the error is recorded for the Watchdog
                try:
                    kv_set(conn, f"lane_error:{lane}", {"at": utcnow(), "trace": traceback.format_exc()[-1500:]})
                    conn.commit()
                except sqlite3.Error:
                    pass
                self.stop.wait(5)
            finally:
                conn.close()
            self.stop.wait(0.5 if busy else 2)

    def start_lane(self, lane: str) -> None:
        t = threading.Thread(target=self._lane, args=(lane,), name=f"lane-{lane}", daemon=True)
        self.threads[lane] = t
        self.beats[lane] = time.time()
        t.start()

    def start(self) -> None:
        for lane in self.lanes:
            self.start_lane(lane)
        threading.Thread(target=self._guardian, name="guardian", daemon=True).start()

    def _guardian(self) -> None:
        """Keeps the Watchdog's own lane alive (the Watchdog restarts everything else)."""
        while not self.stop.wait(30):
            t = self.threads.get("watchdog")
            if t is not None and not t.is_alive():
                self.restarts["watchdog"] = self.restarts.get("watchdog", 0) + 1
                self.start_lane("watchdog")

    def lane_status(self) -> list[dict]:
        now = time.time()
        out = []
        for lane in self.lanes:
            t = self.threads.get(lane)
            cur = self.current.get(lane)
            out.append({"lane": lane, "alive": bool(t and t.is_alive()), "beat_age": round(now - self.beats.get(lane, 0), 1),
                        "running": cur[0] if cur else None, "running_for": round(now - cur[1], 1) if cur else 0,
                        "restarts": self.restarts.get(lane, 0),
                        "agents": sum(1 for a in self.agents if a.lane == lane)})
        return out

    def shutdown(self, timeout: float = 10) -> None:
        self.stop.set()
        for t in self.threads.values():
            t.join(timeout=timeout / max(1, len(self.threads)))
