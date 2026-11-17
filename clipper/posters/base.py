"""Shared pieces for the platform connectors: token storage, HTTP helper, the Poster interface."""
from __future__ import annotations

import json
import sqlite3
import time
from dataclasses import dataclass

import requests

from ..config import Config

TIMEOUT = (15, 120)  # connect, read


class PostError(Exception):
    """Upload failed. retry_after: seconds until trying again makes sense; None means don't retry."""

    def __init__(self, message: str, retry_after: float | None = 600):
        super().__init__(message)
        self.retry_after = retry_after


class NotConnected(PostError):
    def __init__(self, message: str):
        super().__init__(message, retry_after=None)


@dataclass
class PostResult:
    status: str                 # 'posted' or 'draft_sent'
    platform_post_id: str
    url: str | None = None


def get_connection(conn: sqlite3.Connection, platform: str) -> dict | None:
    row = conn.execute("SELECT * FROM connections WHERE platform=?", (platform,)).fetchone()
    if not row:
        return None
    return {"platform": platform, "account": row["account"], "status": row["status"], "detail": row["detail"],
            "tokens": json.loads(row["tokens"] or "{}"), "updated_at": row["updated_at"]}


def save_connection(conn: sqlite3.Connection, platform: str, *, account: str | None = None, tokens: dict | None = None,
                    status: str = "connected", detail: str | None = None) -> None:
    current = get_connection(conn, platform) or {}
    conn.execute(
        "INSERT INTO connections (platform, account, tokens, status, detail, updated_at) VALUES (?,?,?,?,?,datetime('now'))"
        " ON CONFLICT(platform) DO UPDATE SET account=excluded.account, tokens=excluded.tokens, status=excluded.status,"
        " detail=excluded.detail, updated_at=excluded.updated_at",
        (platform, account if account is not None else current.get("account"),
         json.dumps(tokens if tokens is not None else current.get("tokens", {})), status, detail))
    conn.commit()


def with_expiry(tokens: dict) -> dict:
    """Stamp absolute expiry times onto a token response."""
    now = time.time()
    if "expires_in" in tokens:
        tokens["expires_at"] = now + float(tokens["expires_in"])
    if "refresh_expires_in" in tokens:
        tokens["refresh_expires_at"] = now + float(tokens["refresh_expires_in"])
    return tokens


def http(method: str, url: str, *, ok=(200, 201), what: str = "request", **kwargs) -> requests.Response:
    """One HTTP call. Network errors and 5xx/429 raise a retryable PostError; other bad statuses a short one."""
    kwargs.setdefault("timeout", TIMEOUT)
    try:
        r = requests.request(method, url, **kwargs)
    except requests.RequestException as e:
        raise PostError(f"{what}: network error ({type(e).__name__})", retry_after=300) from None
    if r.status_code in ok:
        return r
    body = r.text[:400].replace("\n", " ")
    if r.status_code == 429 or r.status_code >= 500:
        raise PostError(f"{what}: HTTP {r.status_code} {body}", retry_after=900)
    raise PostError(f"{what}: HTTP {r.status_code} {body}", retry_after=None if r.status_code in (400, 401, 403, 404) else 600)


class Poster:
    platform = ""
    label = ""

    def __init__(self, cfg: Config, conn: sqlite3.Connection):
        self.cfg, self.conn = cfg, conn

    def configured(self) -> tuple[bool, str]:
        """Are the app credentials (client id/secret, etc.) present in .env?"""
        raise NotImplementedError

    def connected(self) -> bool:
        c = get_connection(self.conn, self.platform)
        return bool(c and c["status"] == "connected")

    def can_post(self) -> tuple[bool, str]:
        """Is the account connected and under its platform limits right now?"""
        raise NotImplementedError

    def upload(self, clip: sqlite3.Row, post: sqlite3.Row) -> PostResult:
        raise NotImplementedError

    def get_stats(self, posts: list[sqlite3.Row]) -> dict[int, dict]:
        """{post row id: {"views":..,"likes":..,"comments":..,"shares":.., optional "platform_post_id"/"url"/"status"}}"""
        raise NotImplementedError

    def posts_last_24h(self) -> int:
        return self.conn.execute(
            "SELECT count(*) FROM posts WHERE platform=? AND status IN ('posted','draft_sent','uploading')"
            " AND posted_at >= datetime('now','-1 day')", (self.platform,)).fetchone()[0]
