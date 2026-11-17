"""YouTube Shorts via the Data API v3 (plain HTTP, resumable upload).

Checked against Google's docs on 2026-11-16:
- videos.insert has its own quota bucket: 100 calls per day by default, 1 unit each.
- Since 2026-11-02, uploads from unaudited API projects are no longer forced to private.
- A vertical video up to 3 minutes is treated as a Short; no #Shorts tag is needed.
- Desktop OAuth uses a loopback redirect (http://127.0.0.1:port) with PKCE.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import secrets
import time
from pathlib import Path
from urllib.parse import urlencode

from ..config import ROOT, secret
from .base import NotConnected, PostError, Poster, PostResult, get_connection, http, save_connection, with_expiry

AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
SCOPES = "https://www.googleapis.com/auth/youtube.upload https://www.googleapis.com/auth/youtube.readonly"


def _api() -> str:
    return os.environ.get("YT_API_BASE", "https://www.googleapis.com")


def _token_url() -> str:
    return os.environ.get("YT_TOKEN_URL", "https://oauth2.googleapis.com/token")


def client_credentials() -> tuple[str, str] | None:
    path = Path(secret("YOUTUBE_CLIENT_SECRETS_FILE") or "client_secret.json")
    path = path if path.is_absolute() else ROOT / path
    if not path.exists():
        return None
    data = json.loads(path.read_text(encoding="utf-8"))
    block = data.get("installed") or data.get("web") or {}
    if not block.get("client_id"):
        return None
    return block["client_id"], block.get("client_secret", "")


def auth_url(redirect_uri: str) -> tuple[str, str, str]:
    """Returns (url, state, code_verifier)."""
    client_id, _ = client_credentials()
    state, verifier = secrets.token_urlsafe(24), secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    params = {"client_id": client_id, "redirect_uri": redirect_uri, "response_type": "code", "scope": SCOPES,
              "access_type": "offline", "prompt": "consent", "state": state,
              "code_challenge": challenge, "code_challenge_method": "S256"}
    return f"{AUTH_URL}?{urlencode(params)}", state, verifier


def exchange_code(conn, code: str, verifier: str, redirect_uri: str) -> str:
    client_id, client_secret = client_credentials()
    r = http("POST", _token_url(), what="Google token exchange", data={
        "client_id": client_id, "client_secret": client_secret, "code": code, "code_verifier": verifier,
        "grant_type": "authorization_code", "redirect_uri": redirect_uri})
    tokens = with_expiry(r.json())
    if not tokens.get("refresh_token"):
        raise PostError("Google returned no refresh token. Remove the app's access at myaccount.google.com/permissions and connect again.", None)
    save_connection(conn, "youtube", tokens=tokens, account="")
    name = YouTubePoster.channel_name(tokens["access_token"])
    save_connection(conn, "youtube", account=name, tokens=tokens)
    return name


class YouTubePoster(Poster):
    platform, label = "youtube", "YouTube Shorts"

    def configured(self):
        return (True, "") if client_credentials() else (False, "client_secret.json not found (Google Cloud > Credentials > OAuth client, type Desktop app)")

    def _token(self) -> str:
        c = get_connection(self.conn, "youtube")
        if not c or c["status"] != "connected":
            raise NotConnected("YouTube is not connected")
        t = c["tokens"]
        if t.get("expires_at", 0) - 120 > time.time():
            return t["access_token"]
        creds = client_credentials()
        if not creds:
            raise NotConnected("client_secret.json is missing")
        try:
            r = http("POST", _token_url(), what="Google token refresh", data={
                "client_id": creds[0], "client_secret": creds[1], "refresh_token": t["refresh_token"], "grant_type": "refresh_token"})
        except PostError as e:
            if e.retry_after is None:  # invalid_grant: the refresh token was revoked or expired
                save_connection(self.conn, "youtube", status="expired",
                                detail="Google rejected the saved login. Reconnect. (Apps left in 'Testing' on the OAuth consent screen lose their login every 7 days; publish the app to stop that.)")
                raise NotConnected("YouTube login expired; reconnect in Connectors") from None
            raise
        t.update(with_expiry(r.json()))
        save_connection(self.conn, "youtube", tokens=t)
        return t["access_token"]

    @staticmethod
    def channel_name(access_token: str) -> str:
        r = http("GET", f"{_api()}/youtube/v3/channels", what="YouTube channel lookup",
                 params={"part": "snippet", "mine": "true"}, headers={"Authorization": f"Bearer {access_token}"})
        items = r.json().get("items") or []
        if not items:
            raise PostError("This Google account has no YouTube channel", None)
        return items[0]["snippet"]["title"]

    def can_post(self):
        if not self.connected():
            return False, "not connected"
        cap = min(self.cfg["posting"]["per_platform_daily"].get("youtube", 8), 100)
        used = self.posts_last_24h()
        return (used < cap, f"{used}/{cap} uploads in the last 24h")

    def upload(self, clip, post) -> PostResult:
        token = self._token()
        yt = self.cfg["youtube"]
        path = Path(clip["file_path"])
        size = path.stat().st_size
        clean = lambda s: (s or "").replace("<", "").replace(">", "")
        body = {"snippet": {"title": clean(clip["post_title"])[:100], "description": clean(clip["caption"])[:4900],
                            "tags": yt.get("tags", []), "categoryId": str(yt.get("category_id", "20"))},
                "status": {"privacyStatus": post["privacy"] or yt.get("privacy_status", "private"),
                           "selfDeclaredMadeForKids": False}}
        try:
            start = http("POST", f"{_api()}/upload/youtube/v3/videos", what="YouTube upload start",
                         params={"uploadType": "resumable", "part": "snippet,status"},
                         headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json; charset=UTF-8",
                                  "X-Upload-Content-Length": str(size), "X-Upload-Content-Type": "video/mp4"},
                         data=json.dumps(body).encode("utf-8"))
        except PostError as e:
            if "quotaExceeded" in str(e) or "uploadLimitExceeded" in str(e):
                raise PostError("YouTube daily upload quota reached", retry_after=6 * 3600) from None
            raise
        session = start.headers.get("Location")
        if not session:
            raise PostError("YouTube did not return an upload session URL")

        offset, last_error = 0, None
        for attempt in range(6):
            try:
                with path.open("rb") as f:
                    f.seek(offset)
                    headers = {"Authorization": f"Bearer {token}", "Content-Type": "video/mp4", "Content-Length": str(size - offset)}
                    if offset:
                        headers["Content-Range"] = f"bytes {offset}-{size - 1}/{size}"
                    r = http("PUT", session, what="YouTube upload", ok=(200, 201), headers=headers, data=f, timeout=(15, 900))
                video_id = r.json()["id"]
                return PostResult("posted", video_id, f"https://www.youtube.com/shorts/{video_id}")
            except PostError as e:
                last_error = e
                if e.retry_after is None:
                    raise
            time.sleep(min(2 ** attempt, 30))
            # Ask how much arrived, then resume from there.
            try:
                q = http("PUT", session, what="YouTube upload status", ok=(200, 201, 308),
                         headers={"Authorization": f"Bearer {token}", "Content-Length": "0", "Content-Range": f"bytes */{size}"})
            except PostError:
                continue
            if q.status_code in (200, 201):
                video_id = q.json()["id"]
                return PostResult("posted", video_id, f"https://www.youtube.com/shorts/{video_id}")
            rng = q.headers.get("Range")
            offset = int(rng.rsplit("-", 1)[1]) + 1 if rng else 0
        raise PostError(f"YouTube upload kept failing: {last_error}")

    def get_stats(self, posts):
        token = self._token()
        out = {}
        for i in range(0, len(posts), 50):
            batch = {p["platform_post_id"]: p["id"] for p in posts[i:i + 50] if p["platform_post_id"]}
            if not batch:
                continue
            r = http("GET", f"{_api()}/youtube/v3/videos", what="YouTube stats",
                     params={"part": "statistics", "id": ",".join(batch)}, headers={"Authorization": f"Bearer {token}"})
            for item in r.json().get("items", []):
                s = item.get("statistics", {})
                out[batch[item["id"]]] = {"views": int(s.get("viewCount", 0)), "likes": int(s.get("likeCount", 0)),
                                          "comments": int(s.get("commentCount", 0)), "shares": None}
        return out
