"""Judges panel: AI critics, reach scorers, the Reach Forecaster and the Gatekeeper.

The inspectors (inspectors.py) check facts. The judges here form opinions: would a stranger stop scrolling,
how far could this travel, and is it ready for you to approve. None of them can post anything.
"""
from __future__ import annotations

import json
import re
import time
from pathlib import Path

from ..config import ROOT
from ..llm import LLMDeclined, LLMUnavailable, ask
from .base import Ctx
from .inspectors import FAIL, PASS, WARN, Inspector


def _clip_words(ctx: Ctx, clip) -> list[dict]:
    from ..captions import remap_words
    vod = ctx.conn.execute("SELECT transcript_path FROM vods WHERE id=?", (clip["vod_id"],)).fetchone()
    if not vod or not vod["transcript_path"] or not Path(vod["transcript_path"]).exists():
        return []
    transcript = json.loads(Path(vod["transcript_path"]).read_text(encoding="utf-8"))
    return remap_words([w for s in transcript["segments"] for w in s["words"]], json.loads(clip["segments"]))


def _extract(text: str) -> dict:
    from ..selector import Rejected, extract_json
    try:
        return extract_json(text)
    except Rejected as e:
        raise LLMDeclined(f"unreadable answer ({e})") from None


class Judge(Inspector):
    crew, lane, interval = "judges", "judges", 10


# ------------------------------------------------------------------ AI critics

class HookCritic(Judge):
    name, title, lane, interval = "hook_critic", "Hook Critic", "llm", 15
    need_facts = True
    job = "A second, independent Claude read of each finished clip as a cold viewer: does the opening stop the scroll, does it make sense without context, is the title honest? Suggests a better title and hook."
    backup = "Without Claude (no key, outage or budget spent) it scores the hook and title with fixed rules instead and says so on the card."

    def rules(self, clip, f) -> tuple:
        hook, title = (clip["hook_used"] or ""), (clip["post_title"] or "")
        h = 6.0 + (1 if len(hook.split()) <= 6 else -1) + (1 if (f["speech"]["first_word"] or 9) < 1.5 else -1)
        t = 6.0 + (1 if len(title) <= 70 else -1) + (1 if re.search(r"\d|!|\?", title) else 0)
        score = round((h + t) / 2, 1)
        return (PASS if score >= 6 else WARN, f"Rule-based check (Claude unavailable): hook {h:g}, title {t:g}", score,
                {"method": "rules", "hook_score": h, "title_score": t})

    def work(self, ctx: Ctx):
        clip = ctx.unreviewed(need_facts=True)
        if not clip:
            return None
        f = ctx.facts(clip["id"])
        words = _clip_words(ctx, clip)
        opening = " ".join(w["word"] for w in words if w["start"] < 7)
        user = (f"ARCHETYPE: {clip['archetype']}\nLENGTH: {clip['duration_s']:.0f}s\nON-SCREEN HOOK: {clip['hook_used']}\n"
                f"TITLE: {clip['post_title']}\nCAPTION: {clip['caption']}\n\nSPOKEN IN THE FIRST 7 SECONDS:\n{opening or '(nothing)'}\n\n"
                f"FULL CLIP TRANSCRIPT:\n{' '.join(w['word'] for w in words) or '(no speech)'}")
        try:
            answer = ask(ctx.cfg, ctx.conn, purpose=self.name, effort="low", max_tokens=6000,
                         system=(ROOT / "prompts" / "hook_critic.md").read_text(encoding="utf-8"), user=user)
            data = _extract(answer.text)
            hook, title = float(data["hook_score"]), float(data["title_score"])
        except LLMUnavailable as e:
            verdict, summary, score, details = self.rules(clip, f)
            ctx.review(clip["id"], verdict, summary + f" [{e}]", score=score, details=details)
            return f"Clip {clip['id']}: rule-based critique ({e})"
        except (LLMDeclined, KeyError, TypeError, ValueError) as e:
            verdict, summary, score, details = self.rules(clip, f)
            ctx.review(clip["id"], WARN, f"Claude couldn't judge this clip ({str(e)[:80]}); rule-based score shown", score=score, details=details)
            return f"Clip {clip['id']}: Claude declined, used rules"
        issues = [str(i) for i in (data.get("issues") or [])][:4]
        flags = [label for key, label in (("violence_in_first_7s", "graphic violence in the first 7 seconds"),
                                          ("spoiler", "reads like a story spoiler"), ("misleading_title", "title overpromises"))
                 if data.get(key) is True]
        if data.get("context_independent") is False:
            flags.append("needs context a new viewer won't have")
        if flags and "spoiler" in " ".join(flags):
            ctx.conn.execute("UPDATE clips SET spoiler_risk=1 WHERE id=?", (clip["id"],))
        verdict = WARN if flags or hook < 6 or title < 6 else PASS
        ctx.review(clip["id"], verdict, "; ".join(flags + issues) or "Strong open and honest title", score=round((hook + title) / 2, 1),
                   details={"hook_score": hook, "title_score": title, "better_title": str(data.get("better_title") or "")[:100],
                            "better_hook": " ".join(str(data.get("better_hook") or "").split()[:7]), "model": answer.model,
                            "backup_model": answer.used_backup})
        return f"Clip {clip['id']}: hook {hook:g}, title {title:g}" + (" (backup model)" if answer.used_backup else "")


class VisualJudge(Judge):
    name, title, lane, interval = "visual_judge", "Visual Judge", "llm", 15
    need_facts = True
    job = "Shows Claude four frames of the finished clip: is it gameplay or a story cutscene, is the opening graphic, is the facecam framed well, is any text unreadable, and which frame makes the best cover."
    backup = "Without Claude it records 'skipped' and leaves the picture checks to the Cutscene Detector, Facecam and Thumbnail inspectors."

    def work(self, ctx: Ctx):
        clip = ctx.unreviewed(need_facts=True)
        if not clip:
            return None
        if not ctx.cfg.get("judges").get("visual_judge", True):
            ctx.review(clip["id"], "skipped", "turned off in config.yaml (judges.visual_judge)")
            return None
        f = ctx.facts(clip["id"])
        folder = Path(clip["file_path"]).parent / "frames"
        frames = f.get("frames", [])
        picks = [frames[0]] + [frames[int(len(frames) * q)] for q in (0.3, 0.55, 0.8)] if len(frames) >= 4 else frames
        paths = [folder / p["file"] for p in picks if (folder / p["file"]).exists()]
        if not paths:
            ctx.review(clip["id"], "skipped", "no frames to look at")
            return None
        times = ", ".join("%.0fs" % p["t"] for p in picks[1:])
        streamer = ctx.source_for_clip(clip)["name"]
        user = (f"These are {len(paths)} frames from a finished vertical clip, in order: frame 1 is the opening, then {times}. "
                f"Stream: {streamer} playing GTA. Title: {clip['post_title']}.")
        try:
            answer = ask(ctx.cfg, ctx.conn, purpose=self.name, effort="low", max_tokens=4000, images=paths,
                         system=(ROOT / "prompts" / "visual_judge.md").read_text(encoding="utf-8"), user=user)
            data = _extract(answer.text)
        except LLMUnavailable as e:
            ctx.review(clip["id"], "skipped", f"Claude unavailable ({e}); picture checks left to the inspectors")
            return None
        except LLMDeclined as e:
            ctx.review(clip["id"], WARN, f"Claude couldn't judge the frames ({str(e)[:80]}): look at it yourself")
            return f"Clip {clip['id']}: visual check declined"
        flags = []
        if data.get("mostly_cutscene"):
            flags.append("looks like a story cutscene")
        if data.get("graphic_opening"):
            flags.append("graphic content in the opening frame")
        if data.get("not_gta"):
            flags.append("doesn't look like GTA gameplay")
        if data.get("in_game_show"):
            flags.append("shows an in-game TV show or performance, which Rockstar removes when posted on its own")
        flags += [str(x) for x in (data.get("issues") or [])][:3]
        best = data.get("best_cover_frame")
        details = {"best_cover": picks[int(best) - 1]["t"] if isinstance(best, int) and 1 <= best <= len(picks) else None,
                   "model": answer.model, **{k: data.get(k) for k in ("mostly_cutscene", "graphic_opening", "not_gta", "in_game_show")}}
        ctx.review(clip["id"], WARN if flags else PASS, "; ".join(flags) or "Frames look like clean GTA gameplay with a good cam", details=details)
        return f"Clip {clip['id']}: {'; '.join(flags) or 'frames look good'}"


# ------------------------------------------------------------------ reach scorers

class Scorer(Judge):
    """Writes one 0-1 score the Reach Forecaster weighs. Verdicts are always pass: scores rank, they don't block."""
    need_facts = False
    part = ""

    def score(self, ctx: Ctx, clip) -> tuple[float, str]:
        raise NotImplementedError

    def work(self, ctx: Ctx):
        clip = ctx.unreviewed()
        if not clip:
            return None
        value, why = self.score(ctx, clip)
        value = round(min(1.0, max(0.0, value)), 3)
        ctx.review(clip["id"], PASS, why, score=value, details={"part": self.part})
        return f"Clip {clip['id']}: {self.part} {value:.2f}"


class SignalScorer(Scorer):
    name, title, part = "signal_scorer", "Signal Scorer", "signal"
    job = "Scores how strong the original moment was: where its audio and chat spike ranks among every candidate from the same stream."

    def score(self, ctx, clip):
        cand = ctx.conn.execute("SELECT score, vod_id FROM candidates WHERE id=?", (clip["candidate_id"],)).fetchone()
        if not cand:
            return 0.5, "no signal data (clip was cut by hand)"
        scores = sorted(r[0] for r in ctx.conn.execute("SELECT score FROM candidates WHERE vod_id=?", (cand["vod_id"],)))
        rank = (scores.index(cand["score"]) + 1) / len(scores)
        return rank, f"moment ranks in the top {100 - rank * 100:.0f}% of this stream's spikes" if rank < 1 else "strongest moment of the stream"


class ArchetypeScorer(Scorer):
    name, title, part = "archetype_scorer", "Archetype Scorer", "archetype"
    job = "Scores the clip's type (freakout, comedy, discovery, hot take, chaos/skill) by what has been earning views on your accounts, starting from launch-week priors."
    PRIOR = {"DISCOVERY": 1.0, "FREAKOUT": 0.9, "COMEDY": 0.75, "CHAOS_SKILL": 0.7, "HOT_TAKE": 0.6}

    def score(self, ctx, clip):
        learned = (ctx.get("learned") or {}).get("archetype_index") or {}
        v = learned.get(clip["archetype"])
        if v is not None:
            return v, f"{clip['archetype']} is performing at {v:.0%} of your best type"
        return self.PRIOR.get(clip["archetype"], 0.6), f"{clip['archetype']}: launch-week prior (no data yet)"


class FreshnessScorer(Scorer):
    name, title, part = "freshness_scorer", "Freshness Scorer", "fresh"
    job = "Scores how recent the moment is. In launch week a clip loses most of its value within a day, so fresh moments go to the front."

    def score(self, ctx, clip):
        vod = ctx.conn.execute("SELECT published_at, chunk_start_s FROM vods WHERE id=?", (clip["vod_id"],)).fetchone()
        if not vod or not vod["published_at"]:
            return 0.5, "stream time unknown"
        happened = ctx.conn.execute("SELECT strftime('%s', ?)", (vod["published_at"],)).fetchone()[0]
        age_h = max(0.0, (time.time() - (int(happened) + (vod["chunk_start_s"] or 0) + clip["start_s"])) / 3600) if happened else 0
        v = 1.0 if age_h < 6 else 0.7 if age_h < 24 else 0.4 if age_h < 72 else 0.15
        return v, f"moment happened {age_h:.0f}h ago"


class LengthScorer(Scorer):
    name, title, part = "length_scorer", "Length Scorer", "length"
    job = "Scores length against what finishes best: 18-45 seconds is the sweet spot across TikTok, Reels and Shorts."

    def score(self, ctx, clip):
        d = clip["duration_s"]
        v = 1.0 if 18 <= d <= 45 else 0.8 if d <= 60 else 0.6 if d <= 90 else 0.4
        return v, f"{d:.0f}s"


class TrendScorer(Scorer):
    name, title, part = "trend_scorer", "Trend Scorer", "trend"
    job = "Scores how well the title and caption match what's trending in gaming right now (terms collected by the Trend Watcher)."

    def score(self, ctx, clip):
        trends = ctx.get("trends") or {}
        terms = trends.get("terms") or {}
        if not terms:
            return 0.5, "no trend data yet (the Trend Watcher needs YouTube connected)"
        text = f"{clip['post_title']} {clip['caption']} {clip['hook_used']}".lower()
        hits = [t for t in terms if t in text]
        v = min(1.0, 0.45 + 0.15 * len(hits))
        return v, f"matches trending terms: {', '.join(hits[:5])}" if hits else "no trending terms in the title or caption"


class StreamerScorer(Scorer):
    name, title, part = "streamer_scorer", "Streamer Pull Scorer", "streamer"
    job = "Scores the streamer's pull on your accounts: average views of their past clips compared with your overall average."

    def score(self, ctx, clip):
        from ..stats import latest
        source = ctx.source_for_clip(clip)
        rows = latest(ctx.conn)
        mine = [r["views"] for r in rows if r.get("source") == source["name"] and r.get("views") is not None]
        allv = [r["views"] for r in rows if r.get("views") is not None]
        if len(mine) < 3 or not allv:
            return 0.6, f"not enough posts of {source['name']} yet"
        ratio = (sum(mine) / len(mine)) / max(1.0, sum(allv) / len(allv))
        return min(1.0, 0.5 * ratio), f"{source['name']} clips average {ratio:.1f}x your overall views"


SCORERS = [SignalScorer, ArchetypeScorer, FreshnessScorer, LengthScorer, TrendScorer, StreamerScorer]


class ReachForecaster(Judge):
    name, title, interval = "reach_forecaster", "Reach Forecaster", 10
    need_facts = False
    job = "Combines the six scorers, the Moment Picker's own scores and the Hook Critic into one 0-100 reach score. The Timing Planners post the highest scores first."
    backup = "If a scorer or the Hook Critic hasn't reported within 20 minutes it uses a neutral value for that part instead of waiting forever."

    def work(self, ctx: Ctx):
        names = [s.name for s in SCORERS]
        clip = ctx.unreviewed()
        if not clip:
            return None
        rows = {r["agent"]: r for r in ctx.conn.execute("SELECT agent, score, details, verdict FROM reviews WHERE clip_id=?", (clip["id"],))}
        waited = ctx.conn.execute("SELECT (julianday('now') - julianday(updated_at)) * 1440 FROM clips WHERE id=?", (clip["id"],)).fetchone()[0] or 0
        if (any(n not in rows for n in names) or "hook_critic" not in rows) and waited < 20:
            return None  # the panel hasn't finished
        w = dict(ctx.cfg["agents"]["reach_weights"])
        parts = {"selector": (clip["score_overall"] or 0) / 10, "share": (clip["score_shareability"] or 0) / 10}
        critic = rows.get("hook_critic")
        parts["hook_critic"] = critic["score"] / 10 if critic and critic["score"] is not None else (clip["score_hook"] or 0) / 10
        for s in SCORERS:
            r = rows.get(s.name)
            parts[s.part] = r["score"] if r and r["score"] is not None else 0.5
        score = round(sum(w.get(k, 0) * v for k, v in parts.items()) / max(1, sum(w.get(k, 0) for k in parts)) * 100, 1)
        tier = "A" if score >= 75 else "B" if score >= 60 else "C"
        ctx.conn.execute("UPDATE clips SET reach_score=? WHERE id=?", (score, clip["id"]))
        ctx.review(clip["id"], PASS if tier != "C" else WARN, f"Reach {score:.0f}/100, tier {tier}", score=score,
                   details={k: round(v, 2) for k, v in parts.items()})
        return f"Clip {clip['id']}: reach {score:.0f} (tier {tier})"


class Gatekeeper(Judge):
    name, title, interval = "gatekeeper", "Gatekeeper", 10
    need_facts = False
    reviews_clips = False
    job = "Waits for every inspector and judge, then writes one recommendation on the review card: ready, needs a look, reject, or blocked (source not permitted). It only approves on its own if you turn on auto_approve_threshold."
    backup = "If some checker is paused or stuck for 30 minutes it decides with the verdicts it has and lists what was missing."

    def work(self, ctx: Ctx):
        reviewers = [a.name for a in self.orch.agents if getattr(a, "reviews_clips", False)] if self.orch else []
        enabled = {r["name"] for r in ctx.conn.execute("SELECT name FROM agents WHERE enabled=1")}
        needed = [n for n in reviewers if n in enabled]
        for clip in ctx.conn.execute("SELECT c.* FROM clips c WHERE c.status='pending_review' AND c.recommendation IS NULL ORDER BY c.id").fetchall():
            verdicts = {r["agent"]: (r["verdict"], r["summary"]) for r in ctx.conn.execute("SELECT agent, verdict, summary FROM reviews WHERE clip_id=?", (clip["id"],))}
            missing = [n for n in needed if n not in verdicts]
            waited = ctx.conn.execute("SELECT (julianday('now') - julianday(updated_at)) * 1440 FROM clips WHERE id=?", (clip["id"],)).fetchone()[0] or 0
            if missing and waited < 30:
                continue
            return self.decide(ctx, clip, verdicts, missing)
        return None

    def decide(self, ctx: Ctx, clip, verdicts: dict, missing: list[str]):
        source = ctx.source_for_clip(clip)
        title = {a.name: a.title for a in (self.orch.agents if self.orch else [])}
        fails = [f"{title.get(a, a)}: {s}" for a, (v, s) in verdicts.items() if v == FAIL]
        warns = [f"{title.get(a, a)}: {s}" for a, (v, s) in verdicts.items() if v == WARN and a != "permission_guard"]
        postable = ctx.cfg.can_post(source["permission"])
        if fails:
            rec, why = "reject", fails[:3]
        elif not postable:
            rec, why = "blocked", [f"{source['name']} is '{source['permission']}': confirm permission in sources.yaml before this can post"] + warns[:2]
        elif warns or clip["spoiler_risk"] or clip["picked_by"] == "backup" or missing:
            rec = "needs_look"
            why = warns[:3] + (["picked by the backup picker (Claude was unavailable)"] if clip["picked_by"] == "backup" else []) \
                + ([f"not checked by: {', '.join(title.get(m, m) for m in missing[:4])}"] if missing else [])
        else:
            rec, why = "ready", ["every check passed"]
        ctx.conn.execute("UPDATE clips SET recommendation=?, rec_reason=? WHERE id=?", (rec, " | ".join(why)[:900], clip["id"]))
        ctx.conn.commit()
        threshold = ctx.cfg["safety"].get("auto_approve_threshold")
        critic = verdicts.get("hook_critic", ("", ""))[0]
        if (rec == "ready" and threshold is not None and postable and not clip["spoiler_risk"] and critic == PASS
                and (clip["reach_score"] or 0) >= threshold * 10):
            ctx.conn.execute("UPDATE clips SET status='approved', reviewed_at=datetime('now'), updated_at=datetime('now') WHERE id=?", (clip["id"],))
            ctx.conn.commit()
            ctx.log(f"Auto-approved clip {clip['id']} (reach {clip['reach_score']:.0f})", level="warn", clip_id=clip["id"])
        return f"Clip {clip['id']}: {rec.replace('_', ' ')}" + (f" ({why[0][:100]})" if rec != "ready" else "")


JUDGES = [HookCritic, VisualJudge, *SCORERS, ReachForecaster, Gatekeeper]
