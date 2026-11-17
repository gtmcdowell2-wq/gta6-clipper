"""End-to-end check of the agent network on a copy of the real database, with stand-in platforms.

    python -m tests.test_network

Uses a copy of data/clipper.db when it holds 3+ rendered clips; otherwise (a fresh clone) it renders
3 synthetic clips with ffmpeg. Touches nothing in data/.
"""
from __future__ import annotations

import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _rendered_clips(db: Path) -> int:
    import sqlite3
    c = sqlite3.connect(db)
    try:
        paths = [r[0] for r in c.execute("SELECT file_path FROM clips WHERE file_path IS NOT NULL")]
    except sqlite3.Error:
        return 0
    finally:
        c.close()
    return sum(Path(p).exists() for p in paths)


def _seed_synthetic(conn, tmp: Path) -> None:
    """Three 20s 1080x1920 clips with a tone, a transcript, and the rows the agents expect."""
    import json
    import subprocess
    src = conn.execute("SELECT id FROM sources WHERE key LIKE '%:kaicenat'").fetchone()[0]
    vdir = tmp / "vod"
    vdir.mkdir()
    line = "no way he actually pulled that off chat is going crazy right now".split()
    segs = [{"start": 100 * c + 0.5, "end": 100 * c + 10, "text": " ".join(line),
             "words": [{"word": w, "start": 100 * c + 0.5 + k * 0.6, "end": 100 * c + 0.9 + k * 0.6} for k, w in enumerate(line)]}
            for c in (1, 2, 3)]
    (vdir / "transcript.json").write_text(json.dumps({"segments": segs}))
    conn.execute("INSERT INTO vods (id, source_id, platform_id, url, title, status, transcript_path) VALUES (1,?,?,?,?,?,?)",
                 (src, "test", "https://www.twitch.tv/videos/0", "test stream", "done", str(vdir / "transcript.json")))
    titles = ["He pulled off the impossible heist escape", "Chat could not believe this getaway drive",
              "The worst parking job in Vice City history"]
    for i, (title, style) in enumerate(zip(titles, ("bold_yellow", "comic", "clean")), start=1):
        out = tmp / f"clip{i}.mp4"
        subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
                        "-f", "lavfi", "-i", f"testsrc2=size=1080x1920:rate=30:duration=20",
                        "-f", "lavfi", "-i", f"sine=frequency={300 + 100 * i}:duration=20",
                        "-af", "volume=6dB", "-c:v", "libx264", "-preset", "ultrafast", "-c:a", "aac",
                        "-shortest", str(out)], check=True)
        conn.execute(
            "INSERT INTO clips (id, vod_id, segments, start_s, end_s, duration_s, archetype, working_title, hook_a, hook_b, hook_used,"
            " post_title, caption, score_hook, score_retention, score_shareability, score_overall, why, caption_style, layout_used,"
            " file_path, status) VALUES (?,1,?,?,?,20,?,?,?,?,?,?,?,8,7,7,8,?,?,?,?,'pending_review')",
            (i, json.dumps([{"start": 100 * i, "end": 100 * i + 20, "verbatim_text": "no way"}]), 100 * i, 100 * i + 20,
             ("HYPE", "COMEDY", "FAIL")[i - 1], title, f"Hook {i} A", f"Hook {i} B", f"Hook {i} A", title,
             f"Caption {i} #GTA6", "synthetic", style, "gameplay", str(out)))
    conn.commit()


def main() -> int:
    from tests.fake_platforms import serve
    srv, st = serve()
    base = f"http://127.0.0.1:{srv.server_port}"
    tmp = Path(tempfile.mkdtemp(prefix="clipper-test-"))
    os.environ.update(YT_API_BASE=base, YT_TOKEN_URL=f"{base}/google/token", TIKTOK_API_BASE=base, IG_GRAPH_BASE=base,
                      IG_POLL_SECONDS="0.2", TIKTOK_CLIENT_KEY="k", TIKTOK_CLIENT_SECRET="s", IG_USER_ID="IGUSER",
                      IG_ACCESS_TOKEN="ig-token", YOUTUBE_CLIENT_SECRETS_FILE=str(tmp / "client_secret.json"),
                      ANTHROPIC_API_KEY="", CLIPPER_SELECTOR_PROVIDER="file")
    (tmp / "client_secret.json").write_text('{"installed": {"client_id": "cid", "client_secret": "csec"}}')

    import yaml

    from clipper.agents import build_agents
    from clipper.agents.base import Orchestrator
    from clipper.config import load_config
    from clipper.db import connect, init_db
    from clipper.posters import get_poster, save_connection
    from clipper.posters import tiktok, youtube

    real = yaml.safe_load((ROOT / "config.yaml").read_text(encoding="utf-8"))
    src_db = ROOT / real["paths"]["db"]
    real["paths"]["db"] = str(tmp / "test.db")
    real["posting"]["active_hours"] = [0, 24]
    (tmp / "config.yaml").write_text(yaml.safe_dump(real), encoding="utf-8")
    src = yaml.safe_load((ROOT / "sources.yaml").read_text(encoding="utf-8"))
    for s in src["sources"]:
        if s["handle"] == "kaicenat":
            s.update(permission="campaign", campaign="test-campaign", caption_suffix="Clipped from @kaicenat #ad")
    (tmp / "sources.yaml").write_text(yaml.safe_dump(src), encoding="utf-8")
    use_real = src_db.exists() and _rendered_clips(src_db) >= 3
    if use_real:
        seed = connect(src_db)
        seed.execute("VACUUM INTO ?", (str(tmp / "test.db"),))
        seed.close()

    cfg = load_config(tmp / "config.yaml", tmp / "sources.yaml")
    conn = init_db(cfg)
    if not use_real:
        print("No real clips found: rendering 3 synthetic clips")
        _seed_synthetic(conn, tmp)
    conn.executescript("DELETE FROM posts; DELETE FROM post_stats; DELETE FROM reviews; DELETE FROM agent_events; DELETE FROM agents;"
                       "UPDATE clips SET status='pending_review', recommendation=NULL, reach_score=NULL WHERE file_path IS NOT NULL;")
    conn.commit()
    agents = {a.name: a for a in build_agents(cfg)}
    orch = Orchestrator(cfg, list(agents.values()))
    failures: list[str] = []

    def check(cond, label):
        print(("  ok   " if cond else "  FAIL ") + label)
        if not cond:
            failures.append(label)

    def drain(*names, limit=40):
        for _ in range(limit):
            if not any(orch.run_agent(agents[n], conn) for n in names):
                break

    print("Quality crew")
    drain("tech_qc", "policy_guard", "originality", "hook_critic", "reach_forecaster", "gatekeeper")
    clips = conn.execute("SELECT id, recommendation, reach_score, caption FROM clips WHERE status='pending_review'").fetchall()
    check(len(clips) >= 1, f"{len(clips)} clips in review")
    for c in clips:
        got = {r["agent"]: (r["verdict"], r["summary"]) for r in conn.execute("SELECT agent, verdict, summary FROM reviews WHERE clip_id=?", (c["id"],))}
        check(len(got) == 5 and c["recommendation"] and c["reach_score"] is not None, f"clip {c['id']}: 5 verdicts, reach {c['reach_score']}, {c['recommendation']}")
        for agent, (verdict, summary) in sorted(got.items()):
            print(f"         {agent:17} {verdict:8} {summary[:110]}")
    check(all("#ad" in c["caption"] for c in clips), "Policy Guard added the campaign's required text to every caption")
    errs = conn.execute("SELECT agent, message FROM agent_events WHERE level='error'").fetchall()
    check(not errs, "no agent errors" + "".join(f"\n         {e['agent']}: {e['message'][:300]}" for e in errs))

    print("Connectors")
    yt_url, state, verifier = youtube.auth_url("http://127.0.0.1:8787/connect/youtube/callback")
    check("code_challenge_method=S256" in yt_url and "access_type=offline" in yt_url, "YouTube auth URL has PKCE + offline access")
    check(youtube.exchange_code(conn, "code", verifier, "http://127.0.0.1:8787/connect/youtube/callback") == "Test Clips Channel", "YouTube code exchange + channel name")
    t = get_poster("youtube", cfg, conn)
    tokens = __import__("clipper.posters", fromlist=["get_connection"]).get_connection(conn, "youtube")["tokens"]
    tokens["expires_at"] = 0  # force a refresh on first use
    save_connection(conn, "youtube", tokens=tokens)
    tt_url, _, tt_verifier = tiktok.auth_url("http://127.0.0.1:8787/connect/tiktok/callback/")
    import hashlib
    check(f"code_challenge={hashlib.sha256(tt_verifier.encode()).hexdigest()}" in tt_url, "TikTok code challenge is hex SHA-256")
    tiktok.exchange_code(conn, "code", tt_verifier, "http://127.0.0.1:8787/connect/tiktok/callback/")
    check(get_poster("instagram", cfg, conn).verify() == "test_reels", "Instagram token check")

    print("Publishing crew")
    ids = [c["id"] for c in clips]
    conn.execute(f"UPDATE clips SET status='approved' WHERE id IN ({','.join('?' * len(ids[:2]))})", ids[:2])
    conn.commit()
    drain("timing_planner")
    posts = conn.execute("SELECT * FROM posts ORDER BY platform, scheduled_for").fetchall()
    check(len(posts) == 3 * len(ids[:2]), f"{len(posts)} posts scheduled for {len(ids[:2])} approved clips on 3 platforms")
    for platform in ("youtube", "tiktok", "instagram"):
        times = [p["scheduled_for"] for p in posts if p["platform"] == platform]
        if len(times) == 2:
            gap = (time.mktime(time.strptime(times[1], "%Y-%m-%d %H:%M:%S")) - time.mktime(time.strptime(times[0], "%Y-%m-%d %H:%M:%S"))) / 60
            check(59 <= gap <= 91, f"{platform}: posts {gap:.0f} min apart")
    # an unpermitted or unapproved clip must never be uploaded, even with a post row forced in
    conn.execute("UPDATE posts SET scheduled_for=datetime('now','-1 minute')")
    conn.execute("UPDATE clips SET status='pending_review' WHERE id=?", (ids[1],))
    conn.commit()
    before = len(st.log)
    drain("publisher")
    rows = {(p["clip_id"], p["platform"]): p for p in conn.execute("SELECT * FROM posts")}
    check(all(rows[(ids[1], pf)]["status"] == "cancelled" for pf in ("youtube", "tiktok", "instagram")), "posts for an un-approved clip are cancelled, not uploaded")
    yt, tk, ig = rows[(ids[0], "youtube")], rows[(ids[0], "tiktok")], rows[(ids[0], "instagram")]
    check(yt["status"] == "posted" and yt["platform_post_id"] == "ytVIDEO123" and "shorts/ytVIDEO123" in (yt["url"] or ""), f"YouTube upload ({yt['status']}: {yt['error'] or yt['url']})")
    size = Path(conn.execute("SELECT file_path FROM clips WHERE id=?", (ids[0],)).fetchone()[0]).stat().st_size
    check(len(st.yt["received"]) == size, "YouTube received every byte after an interrupted upload was resumed")
    check(st.yt.get("meta", {}).get("status", {}).get("privacyStatus") == cfg["youtube"]["privacy_status"], f"YouTube privacy = {cfg['youtube']['privacy_status']}")
    check(tk["status"] == "draft_sent" and st.tt["next"] == size, f"TikTok draft upload ({tk['status']}: {tk['error'] or 'all bytes sent'})")
    check(ig["status"] == "posted" and ig["platform_post_id"] == "IGMEDIA9" and st.ig["bytes"] == size, f"Instagram reel ({ig['status']}: {ig['error'] or ig['url']})")
    check(any("oauth/token" in l or "google/token" in l for l in st.log[before:]), "expired tokens were refreshed before uploading")
    rot = __import__("clipper.posters", fromlist=["get_connection"]).get_connection(conn, "tiktok")["tokens"]["refresh_token"]
    check(rot == "tt-refresh-ROTATED", "TikTok's rotated refresh token was saved")

    # multi-chunk TikTok upload (documented example sizes) without a real 100 MB file
    big = tmp / "big.mp4"
    with big.open("wb") as f:
        f.truncate(70 * 1024 * 1024 + 123)
    fake_clip = dict(conn.execute("SELECT * FROM clips WHERE id=?", (ids[0],)).fetchone(), file_path=str(big))
    res = get_poster("tiktok", cfg, conn).upload(fake_clip, tk)
    check(res.status == "draft_sent" and st.tt["count"] == 7 and st.tt["next"] == big.stat().st_size, f"TikTok 70 MB file sent as {st.tt['count']} chunks")

    print("Stats")
    drain("stats_tracker")
    n = conn.execute("SELECT count(*) FROM post_stats").fetchone()[0]
    check(n == 2, f"{n} stat rows after first pull (YouTube + Instagram; the TikTok draft has none yet)")
    st.tt["posted"] = True  # the user taps Post in the TikTok app
    conn.execute("UPDATE posts SET stats_at=datetime('now','-2 hours') WHERE platform='tiktok'")
    conn.commit()
    drain("stats_tracker")
    tk = conn.execute("SELECT * FROM posts WHERE id=?", (tk["id"],)).fetchone()
    check(tk["status"] == "posted" and tk["platform_post_id"] == "7300000000000000001" and "tiktok.com" in (tk["url"] or ""), "TikTok draft detected as posted, public id and link saved")
    from clipper.stats import export_csv, totals
    tot = totals(conn)
    check(tot["views"] == 12345 + 2222 + 54321, f"total views {tot['views']}")
    csv_text = export_csv(conn)
    check(csv_text.count("\n") == 4 and "youtube.com/shorts/ytVIDEO123" in csv_text, "campaign CSV has 3 posts with links and views")

    print("Operations")
    conn.execute("UPDATE clips SET status='rendering', updated_at=datetime('now','-1 hour') WHERE id=?", (ids[-1],))
    conn.commit()
    drain("supervisor")
    check(conn.execute("SELECT status FROM clips WHERE id=?", (ids[-1],)).fetchone()[0] == "selected", "Supervisor re-queues a stuck render")
    drain("janitor")
    check(not st.errors, "platform stand-in saw only well-formed requests" + "".join(f"\n         {e}" for e in st.errors))
    errs = conn.execute("SELECT agent, message FROM agent_events WHERE level='error'").fetchall()
    check(not errs, "no agent errors" + "".join(f"\n         {e['agent']}: {e['message'][:400]}" for e in errs))

    conn.close()
    srv.shutdown()
    shutil.rmtree(tmp, ignore_errors=True)
    print(f"\n{'ALL PASSED' if not failures else str(len(failures)) + ' FAILED'}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
