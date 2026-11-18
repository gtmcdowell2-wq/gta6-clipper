"""End-to-end check of the whole agent network against stand-in platforms, with synthetic footage.

    python -m tests.test_network

What it does, in a temp folder (nothing in data/ is touched):
- renders a synthetic 100 s "stream" (three visually different parts, a tone, a transcript) and three picked clips,
- runs every crew in order: Editor -> Caption Finisher -> Clip Profiler -> 39 inspectors -> judges -> Gatekeeper,
- talks to stand-ins for YouTube, TikTok, Instagram, Claude and ntfy that enforce the documented request rules,
- simulates an Opus outage (the backup model must answer) and a dead lane (the Watchdog must restart it).
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _synthetic_stream(folder: Path) -> tuple[Path, Path]:
    """100 s of 1920x1080 video in three looks (so the clips don't fingerprint alike), a tone with loud bursts, a transcript."""
    folder.mkdir(parents=True, exist_ok=True)
    video = folder / "video.mp4"
    parts = [("testsrc2=size=1920x1080:rate=30", 35), ("life=s=1920x1080:mold=10:r=30:ratio=0.1:death_color=#C83232:life_color=#00ff00", 30),
             ("cellauto=s=1920x1080:rule=110:r=30", 35)]
    inputs, graph = [], []
    for i, (src, d) in enumerate(parts):
        inputs += ["-f", "lavfi", "-t", str(d), "-i", src]
        graph.append(f"[{i}:v]scale=1920:1080,setsar=1,format=yuv420p[v{i}]")
    graph.append("".join(f"[v{i}]" for i in range(len(parts))) + f"concat=n={len(parts)}:v=1:a=0[v]")
    # Speech-like tone with loud bursts at 25 s, 52 s and 81 s (the "reactions" the punch-ins should find).
    vol = "volume='if(between(t,25,25.8)+between(t,52,52.8)+between(t,81,81.8),1.0,0.12)':eval=frame"
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", *inputs, "-f", "lavfi", "-t", "100",
                    "-i", "sine=frequency=220:sample_rate=48000", "-filter_complex", ";".join(graph) + f";[3:a]{vol}[a]",
                    "-map", "[v]", "-map", "[a]", "-c:v", "libx264", "-preset", "ultrafast", "-crf", "28", "-c:a", "aac", str(video)], check=True)
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", str(video), "-ac", "1", "-ar", "16000",
                    str(folder / "audio.wav")], check=True)
    line = "no way he actually pulled that off chat is going crazy right now look at this".split()
    segments = []
    for start in range(0, 100, 6):
        words = [{"word": w, "start": start + 0.4 + k * 0.33, "end": start + 0.65 + k * 0.33} for k, w in enumerate(line)]
        segments.append({"start": words[0]["start"], "end": words[-1]["end"], "text": " ".join(line), "words": words})
    transcript = folder / "transcript.json"
    transcript.write_text(json.dumps({"duration": 100.0, "segments": segments}))
    (folder / "meta.json").write_text(json.dumps({"title": "GTA 6 launch stream", "chapters": [{"start": 0, "end": 100000, "title": "Grand Theft Auto VI"}]}))
    return video, transcript


def main() -> int:
    from tests.fake_platforms import serve
    srv, st = serve()
    base = f"http://127.0.0.1:{srv.server_port}"
    tmp = Path(tempfile.mkdtemp(prefix="clipper-test-"))
    os.environ.update(YT_API_BASE=base, YT_TOKEN_URL=f"{base}/google/token", TIKTOK_API_BASE=base, IG_GRAPH_BASE=base,
                      IG_POLL_SECONDS="0.2", TIKTOK_CLIENT_KEY="k", TIKTOK_CLIENT_SECRET="s", IG_USER_ID="IGUSER",
                      IG_ACCESS_TOKEN="ig-token", YOUTUBE_CLIENT_SECRETS_FILE=str(tmp / "client_secret.json"),
                      ANTHROPIC_API_KEY="", ANTHROPIC_BASE_URL=base, CLIPPER_SELECTOR_PROVIDER="file",
                      NTFY_TOPIC="clipper-test", NTFY_SERVER=f"{base}/ntfy")
    os.environ.pop("DISCORD_WEBHOOK_URL", None)
    (tmp / "client_secret.json").write_text('{"installed": {"client_id": "cid", "client_secret": "csec"}}')

    import yaml

    from clipper.agents import build_agents
    from clipper.agents.base import Orchestrator
    from clipper.agents.ops import NetworkProber
    from clipper.config import load_config
    from clipper.db import init_db, kv_get
    from clipper.posters import get_connection, get_poster, save_connection
    from clipper.posters import tiktok, youtube

    real = yaml.safe_load((ROOT / "config.yaml").read_text(encoding="utf-8"))
    real["paths"] = {"data_dir": str(tmp / "data"), "db": str(tmp / "data" / "test.db")}
    real["posting"].update(active_hours=[0, 24], attended_hours=[0, 24])
    real["notify"] = {"levels": ["urgent", "error", "warn", "info"], "quiet_hours": [0, 0], "digest_hour": 0}
    real["ops"] = {"auto_update_ytdlp": False, "auto_restart": False, "keep_backups": 5}
    real["agents"]["min_free_gb"] = 1
    (tmp / "config.yaml").write_text(yaml.safe_dump(real), encoding="utf-8")
    src = yaml.safe_load((ROOT / "sources.yaml").read_text(encoding="utf-8"))
    for s in src["sources"]:
        if s["handle"] == "kaicenat":
            s.update(permission="campaign", caption_suffix="Clipped from @kaicenat",
                     campaign={"name": "Kai GTA 6 (test)", "cpm": 1.5, "cap_per_clip": 150, "budget": 10000, "ends": "2099-01-01",
                               "disclosure": "#ad", "required_tags": ["#KaiCenat"], "submit_within_minutes": 30})
    (tmp / "sources.yaml").write_text(yaml.safe_dump(src), encoding="utf-8")

    cfg = load_config(tmp / "config.yaml", tmp / "sources.yaml")
    conn = init_db(cfg)
    failures: list[str] = []

    def check(cond, label):
        print(("  ok   " if cond else "  FAIL ") + label, flush=True)
        if not cond:
            failures.append(label)

    print("Synthetic stream")
    vod_dir = cfg.data_dir / "vods" / "kaicenat" / "test_0-100"
    video, transcript = _synthetic_stream(vod_dir)
    source_id = conn.execute("SELECT id FROM sources WHERE key='twitch:kaicenat'").fetchone()[0]
    conn.execute("INSERT INTO vods (id, source_id, platform_id, url, title, duration_s, published_at, chunk_start_s, chunk_end_s, status,"
                 " video_path, transcript_path) VALUES (1,?,?,?,?,100,datetime('now','-1 hour'),0,100,'selected',?,?)",
                 (source_id, "test", "https://www.twitch.tv/videos/1", "GTA 6 launch stream", str(video), str(transcript)))
    titles = ["He pulled off the impossible heist escape", "Chat could not believe this getaway drive",
              "The worst parking job in Vice City history"]
    for i, (a, b) in enumerate(((8, 31), (40, 63), (70, 93)), start=1):
        conn.execute("INSERT INTO candidates (id, vod_id, start_s, end_s, peak_s, score, signals) VALUES (?,1,?,?,?,?,?)",
                     (i, a, b, (a + b) / 2, 0.5 + i / 10, json.dumps({"audio_spikes": [], "chat_spikes": []})))
        conn.execute(
            "INSERT INTO clips (id, vod_id, candidate_id, segments, start_s, end_s, duration_s, archetype, working_title, hook_a, hook_b,"
            " hook_used, post_title, caption, score_hook, score_retention, score_shareability, score_overall, why, caption_style, status)"
            " VALUES (?,1,?,?,?,?,?,?,?,?,?,?,?,?,8,7,7,8,'synthetic',?,'selected')",
            (i, i, json.dumps([{"start": a, "end": b, "verbatim_text": "no way"}]), a, b, b - a, ("FREAKOUT", "COMEDY", "CHAOS_SKILL")[i - 1],
             titles[i - 1], f"Hook {i} A", f"Hook {i} B", f"Hook {i} A", titles[i - 1], f"Kai Cenat moment {i}",
             ("bold_yellow", "clean_white", "pop_green")[i - 1]))
    conn.commit()

    agents_list = build_agents(cfg)
    agents = {a.name: a for a in agents_list}
    orch = Orchestrator(cfg, agents_list)
    check(len(agents_list) >= 100, f"{len(agents_list)} agents in the network, every one with a job and a backup plan"
          if all(a.job and a.backup for a in agents_list) else "every agent has a job and a backup plan")

    def drain(*names, limit=60):
        for _ in range(limit):
            if not any(orch.run_agent(agents[n], conn) for n in names):
                break

    print("Intake crew")
    t0 = time.time()
    drain("editor")
    rendered = conn.execute("SELECT count(*) FROM clips WHERE status='pending_review' AND file_path IS NOT NULL").fetchone()[0]
    check(rendered == 3, f"Editor rendered {rendered}/3 clips ({time.time() - t0:.0f}s)")
    drain("caption_finisher")
    caps = [r[0] for r in conn.execute("SELECT caption FROM clips ORDER BY id")]
    check(all("#ad" in c and "#KaiCenat" in c and "Clipped from @kaicenat" in c and "#GTA6" in c for c in caps),
          "Caption Finisher added credit, required tag, #ad disclosure and #GTA6 to every caption")
    t0 = time.time()
    drain("clip_profiler")
    facts = {r[0]: json.loads(r[1]) for r in conn.execute("SELECT clip_id, facts FROM clip_facts")}
    check(len(facts) == 3, f"Clip Profiler measured {len(facts)}/3 clips ({time.time() - t0:.0f}s)")
    check(all(f["captions"]["has_disclosure"] for f in facts.values()), "paid disclosure is burned in at the start of every campaign clip")
    geo = json.loads((Path(conn.execute("SELECT file_path FROM clips WHERE id=1").fetchone()[0]).parent / "render.json").read_text())
    check(geo["pieces"] and "src_w" in geo["pieces"][0], "Editor saved the render geometry for the profiler")

    print("Inspection crew + judges (Claude stand-in, with an Opus outage)")
    os.environ["ANTHROPIC_API_KEY"] = "test-key"
    st.llm["overload_model"] = cfg["llm"]["model"]  # every call to the main model is 'overloaded'
    inspectors = [a.name for a in agents_list if a.crew == "inspect"]
    judges = [a.name for a in agents_list if a.crew == "judges" and a.name not in ("reach_forecaster", "gatekeeper")]
    drain(*inspectors, limit=400)
    drain(*judges, limit=200)
    drain("reach_forecaster", "gatekeeper", limit=20)
    reviewers = [a.name for a in agents_list if getattr(a, "reviews_clips", False)]
    for cid in (1, 2, 3):
        got = {r["agent"]: r for r in conn.execute("SELECT * FROM reviews WHERE clip_id=?", (cid,))}
        missing = [n for n in reviewers if n not in got]
        clip = conn.execute("SELECT recommendation, rec_reason, reach_score FROM clips WHERE id=?", (cid,)).fetchone()
        tech_fail = [n for n, r in got.items() if r["verdict"] == "fail"]
        check(not missing and clip["recommendation"] in ("ready", "needs_look") and not tech_fail,
              f"clip {cid}: {len(got)}/{len(reviewers)} verdicts, reach {clip['reach_score']}, {clip['recommendation']}"
              + (f" missing {missing}" if missing else "") + (f" FAILS {tech_fail}" if tech_fail else ""))
        warns = [(n, r["summary"]) for n, r in got.items() if r["verdict"] in ("warn", "skipped")]
        for n, s in warns:
            print(f"         {n:28} {s[:100]}")
    critic = json.loads(conn.execute("SELECT details FROM reviews WHERE clip_id=1 AND agent='hook_critic'").fetchone()[0])
    check(critic.get("backup_model") is True and critic.get("model") == cfg["llm"]["backup_model"],
          f"Opus 'overloaded': the Hook Critic was answered by the backup model ({critic.get('model')})")
    check(st.llm["images"] >= 4, f"Visual Judge sent {st.llm['images']} frames to Claude")
    usage = {r[0]: r[1] for r in conn.execute("SELECT outcome, count(*) FROM llm_usage GROUP BY outcome")}
    check(usage.get("backup", 0) >= 6 and usage.get("unavailable", 0) >= 6, f"AI usage log: {usage}")
    cost = conn.execute("SELECT sum(cost_usd) FROM llm_usage").fetchone()[0] or 0
    check(cost > 0, f"AI cost tracked: ${cost:.4f}")
    errs = conn.execute("SELECT agent, message FROM agent_events WHERE level='error'").fetchall()
    check(not errs, "no agent errors" + "".join(f"\n         {e['agent']}: {e['message'][:300]}" for e in errs))

    print("Connectors")
    yt_url, state, verifier = youtube.auth_url("http://127.0.0.1:8787")
    check("code_challenge_method=S256" in yt_url and "access_type=offline" in yt_url, "YouTube auth URL has PKCE + offline access")
    check(youtube.exchange_code(conn, "code", verifier, "http://127.0.0.1:8787") == "Test Clips Channel", "YouTube code exchange + channel name")
    tokens = get_connection(conn, "youtube")["tokens"]
    tokens["expires_at"] = 0  # force a refresh on first use
    save_connection(conn, "youtube", tokens=tokens)
    tt_url, _, tt_verifier = tiktok.auth_url("http://127.0.0.1:8787/connect/tiktok/callback/")
    import hashlib
    check(f"code_challenge={hashlib.sha256(tt_verifier.encode()).hexdigest()}" in tt_url, "TikTok code challenge is hex SHA-256")
    tiktok.exchange_code(conn, "code", tt_verifier, "http://127.0.0.1:8787/connect/tiktok/callback/")
    check(get_poster("instagram", cfg, conn).verify() == "test_reels", "Instagram token check")

    print("Publishing crew")
    conn.execute("UPDATE clips SET status='approved', reviewed_at=datetime('now') WHERE id IN (1, 2)")
    conn.commit()
    drain("thumbnail_maker", limit=10)
    check(all((Path(r[0]).parent / "cover.jpg").exists() for r in conn.execute("SELECT file_path FROM clips WHERE id IN (1,2)")),
          "Thumbnail Maker made a full-size cover for each approved clip")
    drain("timing.youtube", "timing.tiktok", "timing.instagram")
    posts = conn.execute("SELECT * FROM posts ORDER BY platform, scheduled_for").fetchall()
    check(len(posts) == 6, f"{len(posts)} posts scheduled for 2 approved clips on 3 platforms")
    for platform in ("youtube", "tiktok", "instagram"):
        times = [p["scheduled_for"] for p in posts if p["platform"] == platform]
        if len(times) == 2:
            gap = (time.mktime(time.strptime(times[1], "%Y-%m-%d %H:%M:%S")) - time.mktime(time.strptime(times[0], "%Y-%m-%d %H:%M:%S"))) / 60
            check(59 <= gap <= 91, f"{platform}: posts {gap:.0f} min apart")
    drain("metadata.youtube", "metadata.tiktok", "metadata.instagram")
    meta = {p["platform"]: p for p in conn.execute("SELECT platform, title, caption FROM posts WHERE clip_id=1")}
    check(meta["youtube"]["title"] and "Kai Cenat" in meta["youtube"]["title"] and "twitch.tv/kaicenat" in meta["youtube"]["caption"],
          "YouTube Metadata Writer: title names the streamer, description has the credit line")
    ig_tags = [t for t in meta["instagram"]["caption"].split() if t.startswith("#")]
    check(len(ig_tags) <= 5 and "#ad" in ig_tags, f"Instagram caption keeps the #ad disclosure within 5 hashtags ({ig_tags})")
    # an unapproved clip must never be uploaded, even with a post row forced in
    conn.execute("UPDATE posts SET scheduled_for=datetime('now','-1 minute')")
    conn.execute("UPDATE clips SET status='pending_review' WHERE id=2")
    conn.commit()
    before = len(st.log)
    drain("publisher.youtube", "publisher.tiktok", "publisher.instagram")
    rows = {(p["clip_id"], p["platform"]): p for p in conn.execute("SELECT * FROM posts")}
    check(all(rows[(2, pf)]["status"] == "cancelled" for pf in ("youtube", "tiktok", "instagram")), "posts for an un-approved clip are cancelled, not uploaded")
    yt, tk, ig = rows[(1, "youtube")], rows[(1, "tiktok")], rows[(1, "instagram")]
    check(yt["status"] == "posted" and yt["platform_post_id"] == "ytVIDEO123" and "shorts/ytVIDEO123" in (yt["url"] or ""), f"YouTube upload ({yt['status']}: {yt['error'] or yt['url']})")
    size = Path(conn.execute("SELECT file_path FROM clips WHERE id=1").fetchone()[0]).stat().st_size
    check(len(st.yt["received"]) == size, "YouTube received every byte after an interrupted upload was resumed")
    m = st.yt.get("meta", {})
    check(m.get("paidProductPlacementDetails", {}).get("hasPaidProductPlacement") is True,
          "YouTube: paid campaign clip declared as paid product placement (required explicitly since Nov 16)")
    check(m.get("status", {}).get("privacyStatus") == cfg["youtube"]["privacy_status"], f"YouTube privacy = {cfg['youtube']['privacy_status']}")
    check(tk["status"] == "draft_sent" and st.tt["next"] == size, f"TikTok draft upload ({tk['status']}: {tk['error'] or 'all bytes sent'})")
    check(ig["status"] == "posted" and ig["platform_post_id"] == "IGMEDIA9" and st.ig["bytes"] == size, f"Instagram reel ({ig['status']}: {ig['error'] or ig['url']})")
    check(any("oauth/token" in l or "google/token" in l for l in st.log[before:]), "expired tokens were refreshed before uploading")
    check(get_connection(conn, "tiktok")["tokens"]["refresh_token"] == "tt-refresh-ROTATED", "TikTok's rotated refresh token was saved")

    big = tmp / "big.mp4"
    with big.open("wb") as f:
        f.truncate(70 * 1024 * 1024 + 123)
    fake_clip = dict(conn.execute("SELECT * FROM clips WHERE id=1").fetchone(), file_path=str(big))
    res = get_poster("tiktok", cfg, conn).upload(fake_clip, tk)
    check(res.status == "draft_sent" and st.tt["count"] == 7 and st.tt["next"] == big.stat().st_size, f"TikTok 70 MB file sent as {st.tt['count']} chunks")

    drain("thumbnail_setter")
    check(st.thumbs == ["ytVIDEO123"], "Thumbnail Setter set the custom Shorts thumbnail after processing finished")

    print("Campaign desk + phone alerts")
    drain("campaign_clerk")
    due = conn.execute("SELECT platform FROM posts WHERE submit_status='due'").fetchall()
    check({r[0] for r in due} == {"youtube", "instagram"}, f"Campaign Clerk: {len(due)} live campaign post(s) due for submission")
    urgent = conn.execute("SELECT title FROM alerts WHERE key LIKE 'submit:%' AND resolved_at IS NULL").fetchall()
    check(len(urgent) == 2 and "min left" in urgent[0][0], f"urgent submit alerts with a countdown: {urgent[0][0] if urgent else None}")
    drain("notifier")
    pushed = [n for n in st.ntfy if n["priority"] == 5]
    check(len(pushed) >= 2, f"Notifier pushed {len(st.ntfy)} alert(s) to the phone, {len(pushed)} urgent")
    conn.execute("UPDATE posts SET submit_status='submitted', submitted_at=datetime('now') WHERE submit_status='due'")
    conn.commit()

    print("Stats + growth")
    drain("stats.youtube", "stats.tiktok", "stats.instagram")
    n = conn.execute("SELECT count(*) FROM post_stats").fetchone()[0]
    check(n == 2, f"{n} stat rows after first pull (YouTube + Instagram; the TikTok draft has none yet)")
    st.tt["posted"] = True  # you tap Post in the TikTok app
    conn.execute("UPDATE posts SET stats_at=datetime('now','-2 hours') WHERE platform='tiktok'")
    conn.commit()
    drain("stats.tiktok")
    tk = conn.execute("SELECT * FROM posts WHERE id=?", (tk["id"],)).fetchone()
    check(tk["status"] == "posted" and tk["platform_post_id"] == "7300000000000000001" and tk["live_at"], "TikTok draft detected as posted; its submission window opens")
    drain("campaign_clerk")
    check(conn.execute("SELECT submit_status FROM posts WHERE id=?", (tk["id"],)).fetchone()[0] == "due", "the TikTok post is now due for submission too")
    conn.execute("UPDATE posts SET submit_status='submitted' WHERE id=?", (tk["id"],))
    conn.commit()
    drain("earnings_accountant")
    earn = kv_get(conn, "earnings")
    expected = round(min(150, 12345 / 1000 * 1.5) + min(150, 2222 / 1000 * 1.5) + min(150, 54321 / 1000 * 1.5), 2)
    check(abs(earn["total_usd"] - expected) < 0.05, f"Earnings Accountant: ${earn['total_usd']} (expected ${expected})")
    drain("momentum.youtube", "momentum.tiktok", "momentum.instagram")
    check(bool(kv_get(conn, "breakout:1")), "Momentum Watcher flagged the 12k-view Short as breaking out")
    drain("trend_watcher")
    trends = kv_get(conn, "trends")
    check(trends["source"] == "youtube" and "police chase" in trends["terms"], "Trend Watcher read YouTube's trending gaming videos")
    drain("token.youtube", "token.tiktok", "token.instagram", "limits.youtube", "limits.tiktok", "limits.instagram", "daily_digest",
          "submission_tracker", "budget_watcher", "brief_keeper")
    check(bool(kv_get(conn, "limits:instagram")), "Limit Watchers recorded each platform's usage")
    check(conn.execute("SELECT count(*) FROM alerts WHERE key LIKE 'digest:%'").fetchone()[0] == 1, "Daily Digest written")
    from clipper.stats import export_csv, totals
    check(totals(conn)["views"] == 12345 + 2222 + 54321, f"total views {totals(conn)['views']}")
    check(export_csv(conn).count("\n") == 4, "campaign CSV has 3 posts with links and views")

    print("Operations crew")
    conn.execute("UPDATE clips SET status='rendering', updated_at=datetime('now','-1 hour') WHERE id=3")
    conn.commit()
    drain("supervisor")
    check(conn.execute("SELECT status FROM clips WHERE id=3").fetchone()[0] == "selected", "Supervisor re-queues a stuck render")
    orch.run_agent(agents["archivist"], conn)
    orch.run_agent(agents["backup_verifier"], conn)
    backups = list((cfg.data_dir / "backups").glob("clipper-*.db"))
    check(len(backups) == 1 and kv_get(conn, "verified_backup") == backups[0].name, "Archivist backed up the database and the Backup Verifier checked it")
    NetworkProber.HOSTS = {"Stand-in": base}
    drain("health_monitor", "network_prober", "alert_desk", "log_keeper", "disk_forecaster", "performance_monitor", "config_watcher",
          "restart_manager", "janitor")
    check(kv_get(conn, "network")["Stand-in"]["ok"] and kv_get(conn, "health")["disk_free_gb"] > 0, "Health Monitor and Network Prober report")
    check(not st.errors, "platform stand-ins saw only well-formed requests" + "".join(f"\n         {e}" for e in st.errors))
    errs = conn.execute("SELECT agent, message FROM agent_events WHERE level='error'").fetchall()
    check(not errs, "no agent errors" + "".join(f"\n         {e['agent']}: {e['message'][:400]}" for e in errs))

    print("Watchdog")
    from clipper.agents.base import Agent

    class Tick(Agent):
        name, title, crew, lane, interval, job, backup = "tick", "Tick", "ops", "tick", 1, "test", "test"

        def work(self, ctx):
            return None
    wd = agents["watchdog"]
    mini = Orchestrator(cfg, [Tick(cfg), wd])
    mini.start()
    dead = threading.Thread(target=lambda: None)
    dead.start()
    dead.join()
    mini.threads["tick"] = dead  # the tick lane's thread has "died"
    mini.run_agent(wd, conn)
    check(mini.threads["tick"].is_alive() and mini.restarts.get("tick") == 1, "Watchdog restarted a dead lane")
    mini.shutdown()
    orch.stop.set()

    conn.close()
    srv.shutdown()
    shutil.rmtree(tmp, ignore_errors=True)
    print(f"\n{'ALL PASSED' if not failures else str(len(failures)) + ' FAILED'}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
