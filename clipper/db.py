"""SQLite state: sources -> vods -> candidates -> clips -> posts -> post_stats."""
from __future__ import annotations

import sqlite3
from pathlib import Path

from .config import Config

SCHEMA_VERSION = 1

SCHEMA = """
CREATE TABLE sources (
    id            INTEGER PRIMARY KEY,
    key           TEXT NOT NULL UNIQUE,          -- platform:handle
    name          TEXT NOT NULL,
    platform      TEXT NOT NULL,
    url           TEXT NOT NULL,
    permission    TEXT NOT NULL CHECK (permission IN ('campaign','public_ok','unknown')),
    layout        TEXT NOT NULL DEFAULT 'auto',
    cam           TEXT,                          -- JSON [x,y,w,h]
    campaign      TEXT,
    caption_suffix TEXT,
    notes         TEXT,
    live_marathon INTEGER NOT NULL DEFAULT 0,
    active        INTEGER NOT NULL DEFAULT 1,
    last_checked  TEXT
);

CREATE TABLE vods (
    id            INTEGER PRIMARY KEY,
    source_id     INTEGER REFERENCES sources(id),
    platform_id   TEXT NOT NULL,                 -- the platform's own video id
    url           TEXT NOT NULL,
    title         TEXT,
    description   TEXT,
    duration_s    REAL,
    published_at  TEXT,
    chunk_start_s REAL NOT NULL DEFAULT 0,       -- rolling chunks of a live marathon
    chunk_end_s   REAL,
    status        TEXT NOT NULL DEFAULT 'new' CHECK (status IN
                  ('new','skipped','downloading','downloaded','transcribed','analyzed','selected','done','failed','purged')),
    skip_reason   TEXT,
    video_path    TEXT,
    chat_path     TEXT,
    transcript_path TEXT,
    error         TEXT,
    created_at    TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at    TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (source_id, platform_id, chunk_start_s)
);

CREATE TABLE candidates (
    id          INTEGER PRIMARY KEY,
    vod_id      INTEGER NOT NULL REFERENCES vods(id) ON DELETE CASCADE,
    start_s     REAL NOT NULL,
    end_s       REAL NOT NULL,
    peak_s      REAL,
    score       REAL NOT NULL,
    audio_score REAL,
    chat_score  REAL,
    text_score  REAL,
    signals     TEXT,                            -- JSON: spikes sent to the LLM
    status      TEXT NOT NULL DEFAULT 'new' CHECK (status IN ('new','selected','rejected','failed')),
    llm_response TEXT,
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE clips (
    id            INTEGER PRIMARY KEY,
    vod_id        INTEGER NOT NULL REFERENCES vods(id),
    candidate_id  INTEGER REFERENCES candidates(id),
    segments      TEXT NOT NULL,                 -- JSON [{start,end,verbatim_text}]
    start_s       REAL NOT NULL,
    end_s         REAL NOT NULL,
    duration_s    REAL NOT NULL,
    archetype     TEXT,
    working_title TEXT,
    hook_a        TEXT,
    hook_b        TEXT,
    hook_used     TEXT,
    post_title    TEXT,
    caption       TEXT,
    score_hook    REAL,
    score_retention REAL,
    score_shareability REAL,
    score_overall REAL,
    spoiler_risk  INTEGER NOT NULL DEFAULT 0,
    why           TEXT,
    caption_style TEXT,
    layout_used   TEXT,
    file_path     TEXT,
    status        TEXT NOT NULL DEFAULT 'selected' CHECK (status IN
                  ('selected','rendering','pending_review','approved','rejected','failed')),
    reviewed_at   TEXT,
    error         TEXT,
    created_at    TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at    TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE posts (
    id            INTEGER PRIMARY KEY,
    clip_id       INTEGER NOT NULL REFERENCES clips(id),
    platform      TEXT NOT NULL,
    scheduled_for TEXT,
    status        TEXT NOT NULL DEFAULT 'queued' CHECK (status IN
                  ('queued','uploading','posted','draft_sent','failed','cancelled')),
    platform_post_id TEXT,
    url           TEXT,
    posted_at     TEXT,
    error         TEXT,
    created_at    TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (clip_id, platform)
);

CREATE TABLE post_stats (
    id         INTEGER PRIMARY KEY,
    post_id    INTEGER NOT NULL REFERENCES posts(id) ON DELETE CASCADE,
    fetched_at TEXT NOT NULL DEFAULT (datetime('now')),
    views      INTEGER,
    likes      INTEGER,
    comments   INTEGER,
    shares     INTEGER
);

CREATE INDEX idx_vods_status       ON vods(status);
CREATE INDEX idx_candidates_vod    ON candidates(vod_id, status);
CREATE INDEX idx_clips_status      ON clips(status);
CREATE INDEX idx_posts_due         ON posts(status, scheduled_for);
CREATE INDEX idx_stats_post        ON post_stats(post_id, fetched_at);
"""


def connect(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")  # review app and pipeline share the file
    return conn


def init_db(cfg: Config) -> sqlite3.Connection:
    """Create the schema if needed and sync sources.yaml into the sources table."""
    conn = connect(cfg.db_path)
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    if version == 0:
        conn.executescript(SCHEMA)
        conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
    elif version != SCHEMA_VERSION:
        raise RuntimeError(f"database is schema v{version}, code expects v{SCHEMA_VERSION}")
    if "caption_suffix" not in [r[1] for r in conn.execute("PRAGMA table_info(sources)")]:
        conn.execute("ALTER TABLE sources ADD COLUMN caption_suffix TEXT")  # databases created by milestone 1
    ensure_extras(conn)
    sync_sources(conn, cfg)
    conn.commit()
    return conn


def sync_sources(conn: sqlite3.Connection, cfg: Config) -> None:
    import json

    keys = []
    for s in cfg.sources:
        keys.append(s.key)
        conn.execute(
            """INSERT INTO sources (key, name, platform, url, permission, layout, cam, campaign, campaign_json, caption_suffix, notes, live_marathon, active)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,1)
               ON CONFLICT(key) DO UPDATE SET name=excluded.name, url=excluded.url,
                   permission=excluded.permission, layout=excluded.layout, cam=excluded.cam,
                   campaign=excluded.campaign, campaign_json=excluded.campaign_json, caption_suffix=excluded.caption_suffix,
                   notes=excluded.notes, live_marathon=excluded.live_marathon, active=1""",
            (s.key, s.name, s.platform, s.url, s.permission, s.layout,
             json.dumps(s.cam) if s.cam else None, s.campaign_name, json.dumps(s.brief, default=str) if s.brief else None,
             s.caption_suffix, s.notes, int(s.live_marathon)),
        )
    # Sources removed from the yaml are kept (their clips reference them) but deactivated.
    marks = ",".join("?" * len(keys)) or "''"
    conn.execute(f"UPDATE sources SET active=0 WHERE key NOT IN ({marks})", keys)


EXTRAS = """
CREATE TABLE IF NOT EXISTS agents (
    name TEXT PRIMARY KEY, enabled INTEGER NOT NULL DEFAULT 1, run_requested INTEGER NOT NULL DEFAULT 0,
    state TEXT NOT NULL DEFAULT 'idle', last_run_at TEXT, last_work_at TEXT, last_message TEXT,
    runs INTEGER NOT NULL DEFAULT 0, failures INTEGER NOT NULL DEFAULT 0, streak INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS agent_events (
    id INTEGER PRIMARY KEY, ts TEXT NOT NULL DEFAULT (datetime('now')), agent TEXT NOT NULL,
    level TEXT NOT NULL DEFAULT 'info', message TEXT NOT NULL, clip_id INTEGER, vod_id INTEGER
);
CREATE INDEX IF NOT EXISTS idx_events_agent ON agent_events(agent, id);
CREATE TABLE IF NOT EXISTS reviews (
    id INTEGER PRIMARY KEY, clip_id INTEGER NOT NULL REFERENCES clips(id) ON DELETE CASCADE, agent TEXT NOT NULL,
    verdict TEXT NOT NULL CHECK (verdict IN ('pass','warn','fail','skipped')), score REAL, summary TEXT,
    details TEXT, created_at TEXT NOT NULL DEFAULT (datetime('now')), UNIQUE (clip_id, agent)
);
CREATE TABLE IF NOT EXISTS connections (
    platform TEXT PRIMARY KEY, account TEXT, tokens TEXT, status TEXT NOT NULL DEFAULT 'disconnected',
    detail TEXT, updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT, updated_at TEXT NOT NULL DEFAULT (datetime('now')));
CREATE TABLE IF NOT EXISTS clip_facts (
    clip_id INTEGER PRIMARY KEY REFERENCES clips(id) ON DELETE CASCADE, facts TEXT NOT NULL,
    profiled_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS alerts (
    id INTEGER PRIMARY KEY, key TEXT NOT NULL UNIQUE, level TEXT NOT NULL DEFAULT 'warn', title TEXT NOT NULL,
    detail TEXT, agent TEXT, link TEXT, first_seen TEXT NOT NULL DEFAULT (datetime('now')),
    last_seen TEXT NOT NULL DEFAULT (datetime('now')), count INTEGER NOT NULL DEFAULT 1,
    resolved_at TEXT, notified_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_alerts_open ON alerts(resolved_at, level);
CREATE INDEX IF NOT EXISTS idx_events_ts ON agent_events(ts);
CREATE INDEX IF NOT EXISTS idx_reviews_clip ON reviews(clip_id);
"""

EXTRA_COLUMNS = {
    "agents": {"last_duration": "REAL", "avg_duration": "REAL"},
    "sources": {"campaign_json": "TEXT", "live_now": "INTEGER NOT NULL DEFAULT 0", "live_title": "TEXT", "live_checked": "TEXT"},
    "clips": {"reach_score": "REAL", "recommendation": "TEXT", "picked_by": "TEXT", "rec_reason": "TEXT",
              "caption_final": "INTEGER NOT NULL DEFAULT 0"},
    "posts": {"attempts": "INTEGER NOT NULL DEFAULT 0", "next_try_at": "TEXT", "privacy": "TEXT", "stats_at": "TEXT",
              "title": "TEXT", "caption": "TEXT", "thumb_status": "TEXT", "submit_status": "TEXT", "submitted_at": "TEXT",
              "live_at": "TEXT", "upload_started_at": "TEXT", "boosted": "INTEGER NOT NULL DEFAULT 0"},
}


def ensure_extras(conn: sqlite3.Connection) -> None:
    """Tables and columns added after milestone 5. Safe to run on every start."""
    conn.executescript(EXTRAS)
    for table, cols in EXTRA_COLUMNS.items():
        have = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
        for col, decl in cols.items():
            if col not in have:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {decl}")


def kv_get(conn: sqlite3.Connection, key: str, default=None):
    import json
    row = conn.execute("SELECT value FROM kv WHERE key=?", (key,)).fetchone()
    return json.loads(row[0]) if row and row[0] is not None else default


def kv_set(conn: sqlite3.Connection, key: str, value) -> None:
    import json
    conn.execute("INSERT INTO kv (key, value, updated_at) VALUES (?,?,datetime('now'))"
                 " ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
                 (key, json.dumps(value, ensure_ascii=False)))
