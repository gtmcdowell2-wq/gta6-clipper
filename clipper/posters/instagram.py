"""Instagram Reels via the Graph API with a resumable upload of the local file.

Checked against Meta's docs on 2026-11-16:
- Uploading a LOCAL file (no public URL) only works for apps using Facebook Login for Business, on
  graph.facebook.com + rupload.facebook.com. That is the path used here, so no S3 bucket or tunnel is needed.
- Flow: POST /<ig-user>/media {media_type: REELS, upload_type: resumable, caption} -> POST the bytes to the
  returned rupload URI with headers Authorization: OAuth <token>, offset, file_size -> poll status_code until
  FINISHED -> POST /<ig-user>/media_publish.
- 100 API-published posts per 24h (one section of the docs says 50; the live number is read from
  /content_publishing_limit).
- Reel insights: views, likes, comments, shares, saved, reach.
Needs in .env: IG_USER_ID (the Instagram professional account id) and IG_ACCESS_TOKEN (long-lived token with
instagram_basic, instagram_content_publish, instagram_manage_insights, pages_show_list, pages_read_engagement).
"""
from __future__ import annotations

import os
import time
from pathlib import Path

from ..config import secret
from .base import NotConnected, PostError, Poster, PostResult, get_connection, http, save_connection


def _graph() -> str:
    return os.environ.get("IG_GRAPH_BASE", "https://graph.facebook.com")


def _post_caption(post):
    try:
        return post["caption"]
    except (KeyError, IndexError, TypeError):
        return None


class InstagramPoster(Poster):
    platform, label = "instagram", "Instagram Reels"

    def configured(self):
        ok = bool(secret("IG_USER_ID") and secret("IG_ACCESS_TOKEN"))
        return ok, "" if ok else "IG_USER_ID / IG_ACCESS_TOKEN missing in .env"

    def _base(self) -> str:
        return f"{_graph()}/{self.cfg['instagram'].get('api_version', 'v27.0')}"

    def _auth(self) -> dict:
        if not self.configured()[0]:
            raise NotConnected("Instagram token is not set")
        return {"Authorization": f"Bearer {secret('IG_ACCESS_TOKEN')}"}

    def verify(self) -> str:
        """Check the token against the account and record the connection. Returns the username."""
        try:
            r = http("GET", f"{self._base()}/{secret('IG_USER_ID')}", what="Instagram account check",
                     params={"fields": "username"}, headers=self._auth())
        except PostError as e:
            save_connection(self.conn, "instagram", status="error", detail=str(e)[:300])
            raise
        name = r.json().get("username", "")
        save_connection(self.conn, "instagram", account=name, tokens={}, status="connected")
        return name

    def connected(self):
        c = get_connection(self.conn, "instagram")
        return bool(self.configured()[0] and c and c["status"] == "connected")

    def can_post(self):
        if not self.connected():
            return False, "not connected"
        cap = self.cfg["posting"]["per_platform_daily"].get("instagram", 8)
        used = self.posts_last_24h()
        if used >= cap:
            return False, f"{used}/{cap} reels in the last 24h"
        try:
            r = http("GET", f"{self._base()}/{secret('IG_USER_ID')}/content_publishing_limit", what="Instagram limit",
                     params={"fields": "quota_usage,config"}, headers=self._auth())
            row = (r.json().get("data") or [{}])[0]
            total = (row.get("config") or {}).get("quota_total")
            if total is not None and row.get("quota_usage", 0) >= total:
                return False, f"Instagram's own limit reached ({row['quota_usage']}/{total} in 24h)"
        except PostError:
            pass  # the limit check is advisory; publish will report a real limit error
        return True, f"{used}/{cap} reels in the last 24h"

    def upload(self, clip, post) -> PostResult:
        auth, ig = self._auth(), secret("IG_USER_ID")
        path = Path(clip["file_path"])
        size = path.stat().st_size
        r = http("POST", f"{self._base()}/{ig}/media", what="Instagram container", headers=auth,
                 json={"media_type": "REELS", "upload_type": "resumable", "caption": (_post_caption(post) or clip["caption"] or clip["post_title"] or "")[:2200]})
        container = r.json()
        container_id, uri = container["id"], container["uri"]
        with path.open("rb") as f:
            up = http("POST", uri, what="Instagram video upload", data=f, timeout=(15, 900),
                      headers={"Authorization": f"OAuth {secret('IG_ACCESS_TOKEN')}", "offset": "0", "file_size": str(size)})
        if not up.json().get("success"):
            raise PostError(f"Instagram upload was not accepted: {up.text[:300]}")
        deadline = time.time() + 600
        while True:
            s = http("GET", f"{self._base()}/{container_id}", what="Instagram processing status",
                     params={"fields": "status_code,status"}, headers=auth).json()
            code = s.get("status_code")
            if code == "FINISHED":
                break
            if code in ("ERROR", "EXPIRED"):
                raise PostError(f"Instagram could not process the video: {s.get('status') or code}", retry_after=None)
            if time.time() > deadline:
                raise PostError("Instagram was still processing the video after 10 minutes", retry_after=1800)
            time.sleep(float(os.environ.get("IG_POLL_SECONDS", "8")))
        pub = http("POST", f"{self._base()}/{ig}/media_publish", what="Instagram publish", headers=auth,
                   json={"creation_id": container_id})
        media_id = pub.json()["id"]
        link = None
        try:
            link = http("GET", f"{self._base()}/{media_id}", what="Instagram permalink", params={"fields": "permalink"},
                        headers=auth).json().get("permalink")
        except PostError:
            pass
        return PostResult("posted", media_id, link)

    def get_stats(self, posts):
        auth, out = self._auth(), {}
        for p in posts:
            if not p["platform_post_id"]:
                continue
            try:
                r = http("GET", f"{self._base()}/{p['platform_post_id']}/insights", what="Instagram insights",
                         params={"metric": "views,likes,comments,shares,saved,reach"}, headers=auth)
            except PostError:
                continue  # one deleted reel must not stop the rest
            m = {d["name"]: (d.get("values") or [{}])[0].get("value") for d in r.json().get("data", [])}
            out[p["id"]] = {"views": m.get("views"), "likes": m.get("likes"), "comments": m.get("comments"), "shares": m.get("shares")}
        return out
