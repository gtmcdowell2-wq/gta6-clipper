"""A local stand-in for the YouTube, TikTok and Instagram APIs that enforces the documented request rules.

It is not the real thing: it proves the connectors send what the docs describe and handle the documented
replies, including an interrupted YouTube upload and a multi-chunk TikTok upload.
"""
from __future__ import annotations

import http.server
import json
import re
import threading
from urllib.parse import parse_qs, urlparse


class State:
    def __init__(self):
        self.log: list[str] = []
        self.errors: list[str] = []
        self.yt = {"received": bytearray(), "size": 0, "interrupt_once": True}
        self.tt = {"size": 0, "chunk": 0, "count": 0, "next": 0, "posted": False}
        self.ig = {"polls": 0, "bytes": 0, "size": 0}
        self.thumbs: list[str] = []
        self.llm = {"overload_model": None, "calls": [], "images": 0}
        self.ntfy: list[dict] = []


def serve() -> tuple[http.server.HTTPServer, State]:
    st = State()

    class H(http.server.BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *a):
            pass

        def reply(self, code=200, body=None, headers=None):
            raw = json.dumps(body).encode() if body is not None else b""
            self.send_response(code)
            for k, v in (headers or {}).items():
                self.send_header(k, v)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def need(self, cond, msg):
            if not cond:
                st.errors.append(f"{self.command} {self.path.split('?')[0]}: {msg}")

        def handle_any(self):
            u = urlparse(self.path)
            q = {k: v[0] for k, v in parse_qs(u.query).items()}
            n = int(self.headers.get("Content-Length") or 0)
            body = self.rfile.read(n) if n else b""
            ctype = self.headers.get("Content-Type", "")
            js = json.loads(body) if body and "json" in ctype else {}
            form = {k: v[0] for k, v in parse_qs(body.decode()).items()} if "x-www-form-urlencoded" in ctype else {}
            auth = self.headers.get("Authorization", "")
            st.log.append(f"{self.command} {u.path}")
            p = u.path

            # ---------------- Google / YouTube
            if p == "/google/token":
                self.need(form.get("grant_type") in ("refresh_token", "authorization_code"), "bad grant_type")
                if form.get("grant_type") == "authorization_code":
                    self.need(form.get("code_verifier") and form.get("redirect_uri", "").startswith("http://127.0.0.1"), "needs code_verifier + loopback redirect")
                    return self.reply(200, {"access_token": "yt-access-1", "refresh_token": "yt-refresh", "expires_in": 3599})
                self.need(form.get("refresh_token") == "yt-refresh" and form.get("client_id"), "refresh fields")
                return self.reply(200, {"access_token": "yt-access-2", "expires_in": 3599})
            if p == "/youtube/v3/channels":
                return self.reply(200, {"items": [{"snippet": {"title": "Test Clips Channel"}}]})
            if p == "/upload/youtube/v3/videos" and self.command == "POST":
                self.need(q.get("uploadType") == "resumable" and q.get("part") == "snippet,status,paidProductPlacementDetails", f"query params {q.get('part')}")
                self.need(auth == "Bearer yt-access-2", f"auth header {auth!r}")
                self.need(self.headers.get("X-Upload-Content-Type") == "video/mp4", "X-Upload-Content-Type")
                st.yt["size"] = int(self.headers.get("X-Upload-Content-Length") or 0)
                self.need(st.yt["size"] > 0, "X-Upload-Content-Length")
                self.need(len(js.get("snippet", {}).get("title", "")) <= 100 and js["snippet"].get("categoryId") == "20", "snippet")
                self.need(js.get("status", {}).get("privacyStatus") in ("private", "public", "unlisted"), "privacyStatus")
                # Since 2026-11-16 an unspecified paid-placement flag no longer means "none": it must be explicit.
                self.need(isinstance(js.get("paidProductPlacementDetails", {}).get("hasPaidProductPlacement"), bool),
                          "paidProductPlacementDetails.hasPaidProductPlacement must be set explicitly")
                self.need(js.get("status", {}).get("containsSyntheticMedia") is False, "containsSyntheticMedia must be false")
                st.yt["meta"] = js
                host = self.headers["Host"]
                return self.reply(200, None, {"Location": f"http://{host}/upload/youtube/session?upload_id=abc"})
            if p == "/upload/youtube/session" and self.command == "PUT":
                rng = self.headers.get("Content-Range", "")
                if rng.startswith("bytes */"):
                    self.need(rng == f"bytes */{st.yt['size']}", "status query range")
                    got = len(st.yt["received"])
                    return self.reply(308, None, {"Range": f"bytes=0-{got - 1}"} if got else {})
                if st.yt["interrupt_once"]:  # keep the first half, then fail like a dropped connection
                    st.yt["interrupt_once"] = False
                    st.yt["received"] += body[: len(body) // 2]
                    return self.reply(503, {"error": "backend error"})
                m = re.match(r"bytes (\d+)-(\d+)/(\d+)", rng)
                self.need(m and int(m.group(1)) == len(st.yt["received"]), f"resume offset, got {rng!r}")
                st.yt["received"] += body
                self.need(len(st.yt["received"]) == st.yt["size"], "total bytes after resume")
                return self.reply(200, {"id": "ytVIDEO123"})
            if p == "/youtube/v3/videos" and q.get("chart") == "mostPopular":
                self.need(q.get("videoCategoryId") == "20" and q.get("part") == "snippet", "trending params")
                return self.reply(200, {"items": [{"snippet": {"title": f"GTA 6 police chase insane {i}", "tags": ["gta 6", "police chase"]}} for i in range(5)]})
            if p == "/youtube/v3/videos" and "processingDetails" in q.get("part", ""):
                return self.reply(200, {"items": [{"id": q["id"], "processingDetails": {"processingStatus": "succeeded"}, "status": {"uploadStatus": "processed"}}]})
            if p == "/upload/youtube/v3/thumbnails/set":
                self.need(q.get("videoId") and self.headers.get("Content-Type") == "image/jpeg" and body[:2] == b"\xff\xd8", "thumbnail upload")
                st.thumbs.append(q.get("videoId"))
                return self.reply(200, {"kind": "youtube#thumbnailSetResponse", "items": [{}]})
            if p == "/ntfy":
                self.need(js.get("topic") and js.get("title") and js.get("message") is not None and isinstance(js.get("priority"), int), "ntfy JSON publish")
                st.ntfy.append(js)
                return self.reply(200, {"id": "n1"})
            if p == "/youtube/v3/videos":
                self.need(q.get("part") == "statistics", "part")
                return self.reply(200, {"items": [{"id": i, "statistics": {"viewCount": "12345", "likeCount": "678", "commentCount": "9"}} for i in q["id"].split(",")]})

            # ---------------- Anthropic Messages API (what clipper/llm.py sends)
            if p == "/v1/messages":
                model = js.get("model")
                st.llm["calls"].append(model)
                self.need(self.headers.get("x-api-key") == "test-key" or auth == "Bearer test-key", "API key header")
                self.need("server-side-fallback-2026-07-01" in (self.headers.get("anthropic-beta") or ""), "fallback beta header")
                self.need(js.get("fallbacks") == "default", "fallbacks='default'")
                self.need(js.get("thinking") == {"type": "adaptive"}, f"thinking {js.get('thinking')}")
                self.need((js.get("output_config") or {}).get("effort") in ("low", "medium", "high"), "output_config.effort")
                self.need("temperature" not in js and "budget_tokens" not in json.dumps(js.get("thinking")), "no sampling params / budgets")
                system = js.get("system") or [{}]
                self.need(system[0].get("cache_control") == {"type": "ephemeral"}, "system prompt cached")
                content = js["messages"][0]["content"]
                if isinstance(content, list):
                    st.llm["images"] += sum(1 for b in content if b.get("type") == "image")
                if model == st.llm["overload_model"]:
                    return self.reply(529, {"type": "error", "error": {"type": "overloaded_error", "message": "Overloaded"}},
                                      {"x-should-retry": "false"})
                if isinstance(content, list):  # frames attached: the Visual Judge
                    answer = {"mostly_cutscene": False, "graphic_opening": False, "not_gta": False, "in_game_show": False, "issues": [], "best_cover_frame": 2}
                else:
                    answer = {"hook_score": 8, "title_score": 7, "context_independent": True, "misleading_title": False,
                              "violence_in_first_7s": False, "spoiler": False, "issues": [], "better_title": "Better title",
                              "better_hook": "Better hook"}
                return self.reply(200, {"id": "msg_1", "type": "message", "role": "assistant", "model": model,
                                        "content": [{"type": "text", "text": json.dumps(answer)}], "stop_reason": "end_turn",
                                        "stop_sequence": None, "usage": {"input_tokens": 1200, "output_tokens": 300,
                                        "cache_read_input_tokens": 0, "cache_creation_input_tokens": 800}})

            # ---------------- TikTok
            if p == "/v2/oauth/token/":
                self.need(form.get("client_key") and form.get("client_secret"), "client credentials")
                return self.reply(200, {"access_token": "tt-access-2", "refresh_token": "tt-refresh-ROTATED", "expires_in": 7200,
                                        "refresh_expires_in": 15552000, "open_id": "open-1", "scope": "video.upload,video.list"})
            if p == "/v2/user/info/":
                self.need(q.get("fields") == "display_name" and auth.startswith("Bearer "), "user info request")
                return self.reply(200, {"data": {"user": {"display_name": "Test TikTok"}}, "error": {"code": "ok"}})
            if p == "/v2/post/publish/inbox/video/init/":
                self.need(auth == "Bearer tt-access-2" and ctype == "application/json; charset=UTF-8", "headers")
                si = js.get("source_info", {})
                size, chunk, count = si.get("video_size"), si.get("chunk_size"), si.get("total_chunk_count")
                mb = 1024 * 1024
                self.need(si.get("source") == "FILE_UPLOAD", "source")
                self.need(count == size // chunk and 1 <= count <= 1000, f"total_chunk_count {count} != floor({size}/{chunk})")
                self.need(chunk == size if size < 5 * mb else 5 * mb <= chunk <= 64 * mb or count == 1 and size <= 64 * mb, "chunk_size rule")
                st.tt.update(size=size, chunk=chunk, count=count, next=0)
                return self.reply(200, {"data": {"publish_id": "v_inbox_file~v2.1", "upload_url": f"http://{self.headers['Host']}/tiktok/upload?upload_id=1"},
                                        "error": {"code": "ok", "message": "", "log_id": "x"}})
            if p == "/tiktok/upload":
                m = re.match(r"bytes (\d+)-(\d+)/(\d+)", self.headers.get("Content-Range", ""))
                self.need(m is not None, "Content-Range")
                first, last, total = map(int, m.groups())
                self.need(first == st.tt["next"] and total == st.tt["size"] and len(body) == last - first + 1, "chunk range/length")
                self.need(self.headers.get("Content-Type") == "video/mp4", "content type")
                st.tt["next"] = last + 1
                done = st.tt["next"] == st.tt["size"]
                if not done:
                    self.need(len(body) == st.tt["chunk"], "non-final chunk must equal chunk_size")
                else:
                    self.need(len(body) <= 128 * 1024 * 1024, "final chunk over 128MB")
                return self.reply(201 if done else 206, None)
            if p == "/v2/post/publish/status/fetch/":
                self.need(js.get("publish_id") == "v_inbox_file~v2.1", "publish_id")
                data = {"status": "PUBLISH_COMPLETE", "publicaly_available_post_id": [7300000000000000001]} if st.tt["posted"] else {"status": "SEND_TO_USER_INBOX", "publicaly_available_post_id": []}
                return self.reply(200, {"data": data, "error": {"code": "ok"}})
            if p == "/v2/video/query/":
                self.need("view_count" in q.get("fields", "") and len(js["filters"]["video_ids"]) <= 20, "fields/ids")
                return self.reply(200, {"data": {"videos": [{"id": i, "view_count": 54321, "like_count": 100, "comment_count": 5, "share_count": 7,
                                                             "share_url": "https://www.tiktok.com/@x/video/" + i} for i in js["filters"]["video_ids"]]},
                                        "error": {"code": "ok"}})

            # ---------------- Instagram
            if p == "/v27.0/IGUSER":
                return self.reply(200, {"username": "test_reels", "id": "IGUSER"})
            if p == "/v27.0/IGUSER/content_publishing_limit":
                return self.reply(200, {"data": [{"quota_usage": 2, "config": {"quota_total": 100, "quota_duration": 86400}}]})
            if p == "/v27.0/IGUSER/media":
                self.need(js.get("media_type") == "REELS" and js.get("upload_type") == "resumable", "container params")
                self.need(set(js) <= {"media_type", "upload_type", "caption", "audio_name", "collaborators", "cover_url", "thumb_offset"}, f"unsupported params {set(js)}")
                self.need(auth == "Bearer ig-token", "auth")
                return self.reply(200, {"id": "CONT1", "uri": f"http://{self.headers['Host']}/ig-api-upload/v27.0/CONT1"})
            if p == "/ig-api-upload/v27.0/CONT1":
                self.need(auth == "OAuth ig-token", f"rupload auth must be 'OAuth <token>', got {auth[:12]!r}")
                self.need(self.headers.get("offset") == "0" and self.headers.get("file_size") == str(len(body)), "offset/file_size headers")
                st.ig["bytes"] = len(body)
                return self.reply(200, {"success": True, "message": "Upload successful."})
            if p == "/v27.0/CONT1":
                st.ig["polls"] += 1
                return self.reply(200, {"status_code": "FINISHED" if st.ig["polls"] >= 2 else "IN_PROGRESS"})
            if p == "/v27.0/IGUSER/media_publish":
                self.need(js.get("creation_id") == "CONT1" and st.ig["polls"] >= 2, "publish before FINISHED")
                return self.reply(200, {"id": "IGMEDIA9"})
            if p == "/v27.0/IGMEDIA9":
                return self.reply(200, {"permalink": "https://www.instagram.com/reel/abc/"})
            if p == "/v27.0/IGMEDIA9/insights":
                self.need(q.get("metric", "").startswith("views"), "metrics")
                return self.reply(200, {"data": [{"name": k, "values": [{"value": v}]} for k, v in
                                                 {"views": 2222, "likes": 33, "comments": 4, "shares": 5, "saved": 6, "reach": 1800}.items()]})
            st.errors.append(f"unexpected {self.command} {p}")
            self.reply(404, {"error": "not found"})

        do_GET = do_POST = do_PUT = handle_any

    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, st
