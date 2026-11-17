"""The agent framework: every agent has one job, a schedule, a pause switch and an activity log."""
from __future__ import annotations

import sqlite3
import threading
import time
import traceback
from datetime import datetime, timezone

from ..config import Config
from ..db import connect, kv_get, kv_set

CREWS = {"intake": "Intake crew", "quality": "Quality crew", "publishing": "Publishing crew", "ops": "Operations crew"}


class Agent:
    name = ""        # stable id
    title = ""       # shown in the control center
    crew = "ops"
    job = ""         # one sentence: what this agent is responsible for
    interval = 60    # seconds between runs when there is nothing to do
    lane = "light"   # agents in the same lane never run at the same time: heavy | llm | light | publish

    def __init__(self, cfg: Config):
        self.cfg = cfg

    def work(self, ctx: "Ctx") -> str | None:
        """Do one unit of work. Return a short summary, or None if there was nothing to do."""
        raise NotImplementedError


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

    def review(self, clip_id: int, verdict: str, summary: str, score: float | None = None, details: dict | None = None) -> None:
        import json
        self.conn.execute(
            "INSERT INTO reviews (clip_id, agent, verdict, score, summary, details) VALUES (?,?,?,?,?,?)"
            " ON CONFLICT(clip_id, agent) DO UPDATE SET verdict=excluded.verdict, score=excluded.score,"
            " summary=excluded.summary, details=excluded.details, created_at=datetime('now')",
            (clip_id, self.agent.name, verdict, score, summary[:500], json.dumps(details or {}, ensure_ascii=False)))
        self.conn.commit()

    def unreviewed(self, extra_where: str = "") -> sqlite3.Row | None:
        """The next clip in the review queue this agent hasn't looked at yet."""
        return self.conn.execute(
            f"SELECT c.* FROM clips c WHERE c.status='pending_review' {extra_where}"
            " AND NOT EXISTS (SELECT 1 FROM reviews r WHERE r.clip_id=c.id AND r.agent=?) ORDER BY c.id LIMIT 1",
            (self.agent.name,)).fetchone()


def utcnow() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


class Orchestrator:
    """Runs every agent on its schedule, one lane per thread."""

    def __init__(self, cfg: Config, agents: list[Agent]):
        self.cfg, self.agents = cfg, agents
        self.stop = threading.Event()
        self._next: dict[str, float] = {a.name: 0.0 for a in agents}
        conn = connect(cfg.db_path)
        for a in agents:
            conn.execute("INSERT OR IGNORE INTO agents (name) VALUES (?)", (a.name,))
        conn.execute("UPDATE agents SET state='idle'")  # nothing is running at startup
        conn.commit()
        conn.close()

    def run_agent(self, agent: Agent, conn: sqlite3.Connection) -> str | None:
        """One run with bookkeeping. Returns the summary if the agent did work."""
        conn.execute("UPDATE agents SET state='running', run_requested=0, last_run_at=? WHERE name=?", (utcnow(), agent.name))
        conn.commit()
        ctx = Ctx(self.cfg, conn, agent)
        try:
            summary = agent.work(ctx)
        except Exception as e:
            conn.rollback()
            message = f"{type(e).__name__}: {str(e)[:400]}"
            ctx.log(message + "\n" + traceback.format_exc()[-600:], level="error")
            conn.execute("UPDATE agents SET state='error', failures=failures+1, streak=streak+1, runs=runs+1, last_message=? WHERE name=?",
                         (message, agent.name))
            conn.commit()
            return None
        if summary:
            ctx.log(summary)
            conn.execute("UPDATE agents SET state='idle', runs=runs+1, streak=0, last_work_at=?, last_message=? WHERE name=?",
                         (utcnow(), summary[:500], agent.name))
        else:
            conn.execute("UPDATE agents SET state='idle', runs=runs+1, streak=0 WHERE name=?", (agent.name,))
        conn.commit()
        return summary

    def _lane(self, lane: str) -> None:
        mine = [a for a in self.agents if a.lane == lane]
        while not self.stop.is_set():
            busy = False
            for agent in mine:
                if self.stop.is_set():
                    break
                conn = connect(self.cfg.db_path)
                try:
                    row = conn.execute("SELECT enabled, run_requested, streak FROM agents WHERE name=?", (agent.name,)).fetchone()
                    due = time.time() >= self._next[agent.name]
                    if not (row["run_requested"] or (row["enabled"] and due)):
                        continue
                    did = self.run_agent(agent, conn)
                    streak = conn.execute("SELECT streak FROM agents WHERE name=?", (agent.name,)).fetchone()[0]
                    if streak:  # back off after errors: 1, 2, 4... minutes, capped at 30
                        self._next[agent.name] = time.time() + min(60 * 2 ** (streak - 1), 1800)
                    else:
                        self._next[agent.name] = time.time() + (1 if did else agent.interval)
                    busy = busy or bool(did)
                except sqlite3.Error:
                    pass  # a locked database is retried on the next pass
                finally:
                    conn.close()
            self.stop.wait(0.5 if busy else 3)

    def start(self) -> None:
        for lane in sorted({a.lane for a in self.agents}):
            threading.Thread(target=self._lane, args=(lane,), name=f"lane-{lane}", daemon=True).start()
