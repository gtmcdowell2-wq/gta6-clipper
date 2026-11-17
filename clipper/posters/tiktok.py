"""TikTok via the Content Posting API in upload (inbox draft) mode.

The video lands in the account's TikTok inbox as a draft; you tap Post in the app. That is the only mode an
unaudited app can use for public posts. Checked against TikTok's docs on 2026-11-16:
- POST /v2/post/publish/inbox/video/init/ (scope video.upload), 6 requests/min, at most 5 pending drafts per 24h.
- Chunks: 5-64 MB each, final chunk may be larger; files up to 64 MB can go as one chunk;
  total_chunk_count = floor(video_size / chunk_size).
- Desktop login allows http://127.0.0.1:<port>/ redirects and requires PKCE where the code challenge is the
  HEX sha256 of the verifier (not the usual base64url).
- Access tokens last 2 hours, refresh tokens 180 days, and every refresh returns a NEW refresh token.
The token endpoint path (/v2/oauth/token/) is from TikTok's published API; its reference page was unavailable that day.
"""
from __future__ import annotations

import hashlib
import os
import secrets
import string
import time
from pathlib import Path
from urllib.parse import urlencode

from ..config import secret
from .base import NotConnected, PostError, Poster, PostResult, get_connection, http, save_connection, with_expiry

AUTH_URL = "https://www.tiktok.com/v2/auth/authorize/"
SCOPES = "user.info.basic,video.upload,video.list"
MB = 1024 * 1024
JSON_HEADERS = {"Content-Type": "application/json; charset=UTF-8"}


def _api() -> str:
    return os.environ.get("TIKTOK_API_BASE", "https://open.tiktokapis.com")


def chunk_plan(size: int) -> tuple[int, int]:
    """(chunk_size, total_chunk_count) obeying TikTok's chunk rules."""
    if size <= 64 * MB:
        return size, 1
    chunk = 10 * MB
    return chunk, size // chunk


def auth_url(redirect_uri: str) -> tuple[str, str, str]:
    alphabet = string.ascii_letters + string.digits + "-._~"
    verifier = "".join(secrets.choice(alphabet) for _ in range(64))
    state = secrets.token_urlsafe(24)
    params = {"client_key": secret("TIKTOK_CLIENT_KEY"), "response_type": "code", "scope": SCOPES,
              "redirect_uri": redirect_uri, "state": state,
              "code_challenge": hashlib.sha256(verifier.encode()).hexdigest(), "code_challenge_method": "S256"}
    return f"{AUTH_URL}?{urlencode(params)}", state, verifier


def _check(r, what: str) -> dict:
    data = r.json()
    err = data.get("error") or {}
    if err.get("code") not in (None, "ok"):
        raise PostError(f"{what}: {err.get('code')} {err.get('message', '')}", retry_after=None)
    return data.get("data") or {}


def _token_call(form: dict, what: str) -> dict:
    r = http("POST", f"{_api()}/v2/oauth/token/", what=what, data=form,
             headers={"Content-Type": "application/x-www-form-urlencoded"})
    tokens = r.json()
    if tokens.get("error"):  # this endpoint reports errors as flat fields with HTTP 200
        raise PostError(f"{what}: {tokens.get('error')} {tokens.get('error_description', '')}", retry_after=None)
    return with_expiry(tokens)


def exchange_code(conn, code: str, verifier: str, redirect_uri: str) -> str:
    tokens = _token_call({"client_key": secret("TIKTOK_CLIENT_KEY"), "client_secret": secret("TIKTOK_CLIENT_SECRET"),
                          "code": code, "grant_type": "authorization_code", "redirect_uri": redirect_uri,
                          "code_verifier": verifier}, "TikTok token exchange")
    name = ""
    try:
        r = http("GET", f"{_api()}/v2/user/info/", what="TikTok profile", params={"fields": "display_name"},
                 headers={"Authorization": f"Bearer {tokens['access_token']}"})
        name = (_check(r, "TikTok profile").get("user") or {}).get("display_name", "")
    except PostError:
        pass  # the name is cosmetic
    save_connection(conn, "tiktok", account=name or tokens.get("open_id", "connected"), tokens=tokens)
    return name


class TikTokPoster(Poster):
    platform, label = "tiktok", "TikTok (inbox drafts)"

    def configured(self):
        ok = bool(secret("TIKTOK_CLIENT_KEY") and secret("TIKTOK_CLIENT_SECRET"))
        return ok, "" if ok else "TIKTOK_CLIENT_KEY / TIKTOK_CLIENT_SECRET missing in .env"

    def _token(self) -> str:
        c = get_connection(self.conn, "tiktok")
        if not c or c["status"] != "connected":
            raise NotConnected("TikTok is not connected")
        t = c["tokens"]
        if t.get("expires_at", 0) - 120 > time.time():
            return t["access_token"]
        try:
            new = _token_call({"client_key": secret("TIKTOK_CLIENT_KEY"), "client_secret": secret("TIKTOK_CLIENT_SECRET"),
                               "grant_type": "refresh_token", "refresh_token": t["refresh_token"]}, "TikTok token refresh")
        except PostError as e:
            if e.retry_after is None:
                save_connection(self.conn, "tiktok", status="expired", detail="TikTok rejected the saved login. Reconnect.")
                raise NotConnected("TikTok login expired; reconnect in Connectors") from None
            raise
        t.update(new)  # includes the rotated refresh_token, which replaces the old one
        save_connection(self.conn, "tiktok", tokens=t)
        return t["access_token"]

    def can_post(self):
        if not self.connected():
            return False, "not connected"
        cap = min(self.cfg["posting"]["per_platform_daily"].get("tiktok", 5), 5)
        used = self.posts_last_24h()
        return used < cap, f"{used}/{cap} drafts in the last 24h (TikTok allows 5 pending)"

    def _auth(self) -> dict:
        return {"Authorization": f"Bearer {self._token()}", **JSON_HEADERS}

    def upload(self, clip, post) -> PostResult:
        path = Path(clip["file_path"])
        size = path.stat().st_size
        chunk, count = chunk_plan(size)
        try:
            r = http("POST", f"{_api()}/v2/post/publish/inbox/video/init/", what="TikTok upload init", headers=self._auth(),
                     json={"source_info": {"source": "FILE_UPLOAD", "video_size": size, "chunk_size": chunk, "total_chunk_count": count}})
        except PostError as e:
            if "spam_risk_too_many_pending_share" in str(e):
                raise PostError("TikTok: 5 drafts are already waiting in the inbox. Post or delete some in the app.", retry_after=3 * 3600) from None
            raise
        data = _check(r, "TikTok upload init")
        publish_id, upload_url = data["publish_id"], data["upload_url"]
        with path.open("rb") as f:
            for i in range(count):
                first = i * chunk
                last = size - 1 if i == count - 1 else first + chunk - 1  # the final chunk carries the remainder
                f.seek(first)
                blob = f.read(last - first + 1)
                for attempt in range(4):
                    try:
                        http("PUT", upload_url, what=f"TikTok chunk {i + 1}/{count}", ok=(201, 206), data=blob, timeout=(15, 600),
                             headers={"Content-Type": "video/mp4", "Content-Length": str(len(blob)),
                                      "Content-Range": f"bytes {first}-{last}/{size}"})
                        break
                    except PostError as e:
                        if e.retry_after is None or attempt == 3:
                            raise
                        time.sleep(2 ** attempt * 2)
        return PostResult("draft_sent", publish_id, None)

    def get_stats(self, posts):
        """Drafts have no views until you post them in the app; then TikTok reveals the public post id."""
        headers = self._auth()
        out, ids = {}, {}
        for p in posts:
            if p["status"] == "draft_sent":
                r = http("POST", f"{_api()}/v2/post/publish/status/fetch/", what="TikTok status", headers=headers,
                         json={"publish_id": p["platform_post_id"]})
                data = _check(r, "TikTok status")
                public = data.get("publicaly_available_post_id") or []  # TikTok's own spelling
                if data.get("status") == "PUBLISH_COMPLETE" and public:
                    out[p["id"]] = {"platform_post_id": str(public[0]), "status": "posted"}
                    ids[str(public[0])] = p["id"]
                elif data.get("status") == "FAILED":
                    out[p["id"]] = {"status": "failed", "error": f"TikTok: {data.get('fail_reason')}"}
                time.sleep(2.1)  # 30 requests/min
            elif p["platform_post_id"]:
                ids[p["platform_post_id"]] = p["id"]
        keys = list(ids)
        for i in range(0, len(keys), 20):
            r = http("POST", f"{_api()}/v2/video/query/", what="TikTok stats", headers=headers,
                     params={"fields": "id,share_url,view_count,like_count,comment_count,share_count"},
                     json={"filters": {"video_ids": keys[i:i + 20]}})
            for v in _check(r, "TikTok stats").get("videos", []):
                row = out.setdefault(ids[str(v["id"])], {})
                row.update(views=v.get("view_count"), likes=v.get("like_count"), comments=v.get("comment_count"),
                           shares=v.get("share_count"), url=v.get("share_url"))
        return out
