"""Quality crew: five independent checks on every rendered clip, then one agent that sums them up.

None of these agents can post anything. They write verdicts that show up on the clip's review card.
"""
from __future__ import annotations

import json
import re
import time
from pathlib import Path

from ..captions import has_slur
from ..config import ROOT, secret
from ..util import ffprobe, run
from .base import Agent, Ctx

QUALITY_AGENTS = ("tech_qc", "policy_guard", "originality", "hook_critic", "reach_forecaster")


def _clip_words(ctx: Ctx, clip) -> list[dict]:
    from ..captions import remap_words
    vod = ctx.conn.execute("SELECT transcript_path FROM vods WHERE id=?", (clip["vod_id"],)).fetchone()
    if not vod or not vod["transcript_path"] or not Path(vod["transcript_path"]).exists():
        return []
    transcript = json.loads(Path(vod["transcript_path"]).read_text(encoding="utf-8"))
    return remap_words([w for s in transcript["segments"] for w in s["words"]], json.loads(clip["segments"]))


class TechQC(Agent):
    name, title, crew, lane, interval = "tech_qc", "Technical QC", "quality", "heavy", 15
    job = "Opens every rendered file and measures it: 1080x1920, frame rate, length, loudness near -14 LUFS, no black or silent opening, no long frozen picture, captions present."

    def work(self, ctx: Ctx):
        clip = ctx.unreviewed()
        if not clip:
            return None
        ed = ctx.cfg["editor"]
        path = Path(clip["file_path"] or "")
        if not path.exists():
            ctx.review(clip["id"], "fail", "Rendered file is missing")
            return f"Clip {clip['id']}: file missing"
        fails, warns, facts = [], [], {}
        info = ffprobe(path)
        facts.update(width=info["width"], height=info["height"], fps=round(info["fps"]), duration=round(info["duration"], 1),
                     size_mb=round(path.stat().st_size / 1e6, 1))
        if (info["width"], info["height"]) != (ed["width"], ed["height"]):
            fails.append(f"resolution is {info['width']}x{info['height']}")
        if not info["has_audio"]:
            fails.append("no audio track")
        if abs(info["duration"] - clip["duration_s"]) > 1.0:
            fails.append(f"length {info['duration']:.1f}s does not match the planned {clip['duration_s']:.1f}s")
        if info["duration"] > 180:
            fails.append("longer than 3 minutes: YouTube will not treat it as a Short")
        elif info["duration"] > 90:
            warns.append("over 90s: too long for some Reels placements")
        if round(info["fps"]) not in (30, 60):
            warns.append(f"unusual frame rate {info['fps']:.1f}")

        out = run(["ffmpeg", "-hide_banner", "-nostats", "-i", path, "-filter_complex",
                   "[0:v]blackdetect=d=0.4:pix_th=0.10,freezedetect=n=-50dB:d=6[v];[0:a]ebur128,silencedetect=n=-45dB:d=1.2[a]",
                   "-map", "[v]", "-map", "[a]", "-f", "null", "-"], timeout=600)
        m = re.search(r"Integrated loudness:\s+I:\s+(-?[\d.]+) LUFS", out)
        if m:
            facts["lufs"] = float(m.group(1))
            if abs(facts["lufs"] - ed["target_lufs"]) > 2:
                warns.append(f"loudness {facts['lufs']} LUFS is off target {ed['target_lufs']}")
        for b in re.finditer(r"black_start:([\d.]+) black_end:([\d.]+)", out):
            if float(b.group(1)) < 1.0:
                fails.append("opens on a black screen")
            elif float(b.group(2)) - float(b.group(1)) > 1.5:
                warns.append(f"black screen at {float(b.group(1)):.0f}s")
        for s in re.finditer(r"silence_start: ([\d.]+)", out):
            if float(s.group(1)) < 0.5:
                warns.append("first second is silent (dead air in the hook)")
        if "freeze_start" in out:
            warns.append("picture is frozen for 6+ seconds at some point")
        words = _clip_words(ctx, clip)
        facts["caption_words"] = len(words)
        if not words:
            warns.append("no speech, so no captions")
        elif words[0]["start"] > 4:
            warns.append(f"first words come {words[0]['start']:.0f}s in")

        verdict = "fail" if fails else "warn" if warns else "pass"
        ctx.review(clip["id"], verdict, "; ".join(fails + warns) or "File is clean", details=facts)
        return f"Clip {clip['id']}: {verdict}" + (f" ({'; '.join(fails + warns)})" if fails or warns else "")


class PolicyGuard(Agent):
    name, title, crew, lane, interval = "policy_guard", "Policy Guard", "quality", "light", 15
    job = "Checks each clip against the account-safety rules: source permission, leak wording, spoiler flags, slurs in the audio, campaign credit and disclosure, title and caption limits."

    def work(self, ctx: Ctx):
        clip = ctx.unreviewed()
        if not clip:
            return None
        source = ctx.conn.execute("SELECT s.* FROM sources s JOIN vods v ON v.source_id=s.id WHERE v.id=?", (clip["vod_id"],)).fetchone()
        fails, warns = [], []
        if not ctx.cfg.can_post(source["permission"]):
            fails.append(f"source permission is '{source['permission']}': cannot be posted")
        text = f"{clip['post_title']} {clip['caption']} {clip['hook_used']}"
        if ctx.cfg.is_leak(text):
            fails.append("title, caption or hook uses leak wording")
        if clip["spoiler_risk"]:
            warns.append("possible story spoiler: Rockstar's policy targets endings and major reveals")
        spoken = " ".join(w["word"] for w in _clip_words(ctx, clip))
        if has_slur(spoken):
            warns.append("slur audible in the audio (captions are masked)")
        suffix = (source["caption_suffix"] or "").strip()
        fixed = ""
        if suffix and suffix.lower() not in (clip["caption"] or "").lower():
            # Required campaign text is not a judgement call, so add it rather than fail the clip.
            caption = f"{(clip['caption'] or '').strip()} {suffix}".strip()
            ctx.conn.execute("UPDATE clips SET caption=? WHERE id=?", (caption, clip["id"]))
            ctx.conn.commit()
            clip = dict(clip, caption=caption)
            fixed = "Added the campaign's required text to the caption. "
        if source["permission"] == "campaign" and not suffix:
            warns.append("campaign source has no caption_suffix: check the brief for required credit or paid disclosure")
        if len(clip["post_title"] or "") > 100:
            fails.append("title is over YouTube's 100 characters")
        if len(clip["caption"] or "") > 2200:
            fails.append("caption is over Instagram's 2,200 characters")
        tags = re.findall(r"#\w+", clip["caption"] or "")
        if len(tags) > 30:
            fails.append("more than 30 hashtags (Instagram rejects these)")
        if not any(t.lower() in ("#gta6", "#gtavi") for t in tags):
            warns.append("caption has no #GTA6 tag")
        handle = source["key"].split(":", 1)[1].lower()
        if source["name"].lower() not in text.lower() and handle not in text.lower():
            warns.append("streamer is not credited in the title or caption")
        verdict = "fail" if fails else "warn" if warns else "pass"
        ctx.review(clip["id"], verdict, fixed + ("; ".join(fails + warns) or "No rule problems"), details={"permission": source["permission"]})
        return f"Clip {clip['id']}: {verdict}"


class Originality(Agent):
    name, title, crew, lane, interval = "originality", "Originality Checker", "quality", "light", 15
    job = "Guards against YouTube's 'inauthentic content' rule: flags near-duplicate titles and hooks, the same caption style several uploads in a row, and too many clips from one stretch of stream."

    @staticmethod
    def _tokens(text: str) -> set[str]:
        return {w for w in re.findall(r"[a-z0-9']+", (text or "").lower()) if len(w) > 2}

    def work(self, ctx: Ctx):
        clip = ctx.unreviewed()
        if not clip:
            return None
        warns = []
        others = ctx.conn.execute(
            "SELECT id, post_title, hook_used, caption_style, vod_id, start_s FROM clips WHERE id != ? AND status != 'rejected'"
            " AND created_at >= datetime('now','-7 day')", (clip["id"],)).fetchall()
        mine = self._tokens(clip["post_title"])
        for o in others:
            theirs = self._tokens(o["post_title"])
            if mine and theirs and len(mine & theirs) / len(mine | theirs) > 0.7:
                warns.append(f"title is very close to clip {o['id']}")
                break
        if any((o["hook_used"] or "").strip().lower() == (clip["hook_used"] or "").strip().lower() for o in others):
            warns.append("another recent clip uses the same hook text")
        previous = ctx.conn.execute("SELECT caption_style FROM clips WHERE id < ? AND status != 'rejected' ORDER BY id DESC LIMIT 2", (clip["id"],)).fetchall()
        if len(previous) == 2 and all(p["caption_style"] == clip["caption_style"] for p in previous):
            warns.append("third clip in a row with the same caption style")
        nearby = sum(1 for o in others if o["vod_id"] == clip["vod_id"] and abs(o["start_s"] - clip["start_s"]) < 600)
        if nearby >= 3:
            warns.append(f"{nearby + 1} clips from the same 10 minutes of stream")
        ctx.review(clip["id"], "warn" if warns else "pass", "; ".join(warns) or "Distinct from recent clips")
        return f"Clip {clip['id']}: {'warn' if warns else 'pass'}"


class HookCritic(Agent):
    name, title, crew, lane, interval = "hook_critic", "Hook Critic", "quality", "llm", 20
    job = "A second, independent Claude read of each finished clip as a cold viewer: does the first three seconds stop the scroll, does it make sense without context, is the title honest? Suggests a better title and hook."

    def work(self, ctx: Ctx):
        clip = ctx.unreviewed()
        if not clip:
            return None
        if not secret("ANTHROPIC_API_KEY"):
            ctx.review(clip["id"], "skipped", "No ANTHROPIC_API_KEY, so this check did not run")
            return f"Clip {clip['id']}: skipped (no API key)"
        import anthropic

        words = _clip_words(ctx, clip)
        opening = " ".join(w["word"] for w in words if w["start"] < 7)
        user = (f"ARCHETYPE: {clip['archetype']}\nLENGTH: {clip['duration_s']:.0f}s\nON-SCREEN HOOK: {clip['hook_used']}\n"
                f"TITLE: {clip['post_title']}\nCAPTION: {clip['caption']}\n\nSPOKEN IN THE FIRST 7 SECONDS:\n{opening or '(nothing)'}\n\n"
                f"FULL CLIP TRANSCRIPT:\n{' '.join(w['word'] for w in words) or '(no speech)'}")
        client = anthropic.Anthropic(api_key=secret("ANTHROPIC_API_KEY"))
        response = client.messages.create(
            model=ctx.cfg["agents"]["llm_model"], max_tokens=4000, thinking={"type": "adaptive"},
            output_config={"effort": "low"},
            system=[{"type": "text", "text": (ROOT / "prompts" / "hook_critic.md").read_text(encoding="utf-8"),
                     "cache_control": {"type": "ephemeral"}}],
            messages=[{"role": "user", "content": user}])
        if response.stop_reason == "refusal":
            ctx.review(clip["id"], "warn", "The model declined to assess this clip; look at it yourself")
            return f"Clip {clip['id']}: model declined"
        from ..selector import Rejected, extract_json
        try:
            data = extract_json("".join(b.text for b in response.content if b.type == "text"))
            hook, title = float(data["hook_score"]), float(data["title_score"])
        except (Rejected, KeyError, TypeError, ValueError):
            ctx.review(clip["id"], "skipped", "The model's answer could not be read")
            return f"Clip {clip['id']}: unreadable answer"
        issues = [str(i) for i in (data.get("issues") or [])][:4]
        flags = [label for key, label in (("violence_in_first_7s", "graphic violence in the first 7 seconds"),
                                          ("spoiler", "reads like a story spoiler"), ("misleading_title", "title overpromises"))
                 if data.get(key) is True]
        if data.get("context_independent") is False:
            flags.append("needs context a new viewer won't have")
        verdict = "warn" if flags or hook < 6 or title < 6 else "pass"
        ctx.review(clip["id"], verdict, "; ".join(flags + issues) or "Strong open and honest title", score=round((hook + title) / 2, 1),
                   details={"hook_score": hook, "title_score": title, "better_title": str(data.get("better_title") or "")[:100],
                            "better_hook": " ".join(str(data.get("better_hook") or "").split()[:7])})
        return f"Clip {clip['id']}: hook {hook:g}, title {title:g}"


class ReachForecaster(Agent):
    name, title, crew, lane, interval = "reach_forecaster", "Reach Forecaster", "quality", "light", 15
    job = "Gives every clip a 0-100 reach score from the picker's scores, the Hook Critic, how strong the original moment was, which archetypes are winning on your accounts, length and freshness. The Planner posts high scores first."

    PRIOR = {"DISCOVERY": 1.0, "FREAKOUT": 0.9, "COMEDY": 0.75, "CHAOS_SKILL": 0.7, "HOT_TAKE": 0.6}

    def work(self, ctx: Ctx):
        # Wait for the Hook Critic so its opinion is included.
        clip = ctx.unreviewed("AND EXISTS (SELECT 1 FROM reviews r2 WHERE r2.clip_id=c.id AND r2.agent='hook_critic')")
        if not clip:
            return None
        w = ctx.cfg["agents"]["reach_weights"]
        parts = {"selector": (clip["score_overall"] or 0) / 10, "share": (clip["score_shareability"] or 0) / 10}
        critic = ctx.conn.execute("SELECT score, verdict FROM reviews WHERE clip_id=? AND agent='hook_critic'", (clip["id"],)).fetchone()
        parts["hook_critic"] = critic["score"] / 10 if critic and critic["score"] is not None else (clip["score_hook"] or 0) / 10
        cand = ctx.conn.execute("SELECT score, vod_id FROM candidates WHERE id=?", (clip["candidate_id"],)).fetchone()
        if cand:
            scores = sorted(r[0] for r in ctx.conn.execute("SELECT score FROM candidates WHERE vod_id=?", (cand["vod_id"],)))
            parts["signal"] = (scores.index(cand["score"]) + 1) / len(scores)
        else:
            parts["signal"] = 0.5
        learned = (ctx.get("learned") or {}).get("archetype_index") or {}
        parts["archetype"] = min(1.0, learned.get(clip["archetype"], self.PRIOR.get(clip["archetype"], 0.6)))
        d = clip["duration_s"]
        parts["duration"] = 1.0 if 18 <= d <= 45 else 0.8 if d <= 60 else 0.55
        vod = ctx.conn.execute("SELECT published_at, chunk_start_s FROM vods WHERE id=?", (clip["vod_id"],)).fetchone()
        age_h = 0.0
        if vod and vod["published_at"]:
            happened = ctx.conn.execute("SELECT strftime('%s', ?)", (vod["published_at"],)).fetchone()[0]
            if happened:
                age_h = max(0.0, (time.time() - (int(happened) + vod["chunk_start_s"] + clip["start_s"])) / 3600)
        parts["fresh"] = 1.0 if age_h < 6 else 0.7 if age_h < 24 else 0.4 if age_h < 72 else 0.15
        score = round(sum(w[k] * parts[k] for k in w) / sum(w.values()) * 100, 1)
        tier = "A" if score >= 75 else "B" if score >= 60 else "C"
        ctx.conn.execute("UPDATE clips SET reach_score=? WHERE id=?", (score, clip["id"]))
        ctx.review(clip["id"], "pass" if tier != "C" else "warn", f"Reach {score:.0f}/100, tier {tier}; moment happened {age_h:.0f}h ago",
                   score=score, details={k: round(v, 2) for k, v in parts.items()})
        return f"Clip {clip['id']}: reach {score:.0f} (tier {tier})"


class Gatekeeper(Agent):
    name, title, crew, lane, interval = "gatekeeper", "Gatekeeper", "quality", "light", 15
    job = "Reads all five verdicts and writes one recommendation on the review card: ready to approve, needs a look, or reject. It only approves on its own if you turn on auto_approve_threshold."

    def work(self, ctx: Ctx):
        clip = ctx.conn.execute(
            "SELECT c.* FROM clips c WHERE c.status='pending_review' AND c.recommendation IS NULL AND"
            " (SELECT count(*) FROM reviews r WHERE r.clip_id=c.id AND r.agent IN (%s)) = ? ORDER BY c.id LIMIT 1"
            % ",".join("?" * len(QUALITY_AGENTS)), (*QUALITY_AGENTS, len(QUALITY_AGENTS))).fetchone()
        if not clip:
            return None
        verdicts = {r["agent"]: r["verdict"] for r in ctx.conn.execute("SELECT agent, verdict FROM reviews WHERE clip_id=?", (clip["id"],))}
        failed = [a for a, v in verdicts.items() if v == "fail"]
        warned = [a for a, v in verdicts.items() if v == "warn"]
        rec = "reject" if failed else "needs_look" if warned else "ready"
        ctx.conn.execute("UPDATE clips SET recommendation=? WHERE id=?", (rec, clip["id"]))
        ctx.conn.commit()
        threshold = ctx.cfg["safety"].get("auto_approve_threshold")
        if rec == "ready" and threshold is not None and (clip["reach_score"] or 0) >= threshold * 10 and verdicts.get("hook_critic") == "pass":
            ctx.conn.execute("UPDATE clips SET status='approved', reviewed_at=datetime('now'), updated_at=datetime('now') WHERE id=?", (clip["id"],))
            ctx.conn.commit()
            ctx.log(f"Auto-approved clip {clip['id']} (reach {clip['reach_score']:.0f})", level="warn", clip_id=clip["id"])
        return f"Clip {clip['id']}: {rec.replace('_', ' ')}" + (f" ({', '.join(failed or warned)})" if failed or warned else "")
