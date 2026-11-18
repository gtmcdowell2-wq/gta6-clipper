"""Stage 4: ask the LLM for the best clip in each candidate window, then validate what comes back.

The LLM's answer is never trusted as-is: timestamps are checked against the window and snapped to real
word boundaries, lengths and overlaps are enforced here, and anything malformed is rejected.
"""
from __future__ import annotations

import bisect
import json
import os
import re
import sqlite3
from pathlib import Path

from .config import ROOT, Config, secret
from .util import hms, log, parse_ts

ARCHETYPES = {"FREAKOUT", "COMEDY", "DISCOVERY", "HOT_TAKE", "CHAOS_SKILL"}
MAX_SPLICE_GAP_S = 60
START_PAD_S, END_PAD_S = 0.15, 0.30


class Rejected(Exception):
    pass


# ---------------------------------------------------------------- prompt

def build_request(cfg: Config, vod: sqlite3.Row, source: sqlite3.Row, cand: sqlite3.Row,
                  transcript: dict, taken: list[tuple[float, float]]) -> str:
    ctx = cfg["signals"]["context_seconds"]
    lo, hi = max(0.0, cand["peak_s"] - ctx), cand["peak_s"] + ctx
    lines = [f"[{hms(s['start'], ms=True)}] {s['text']}" for s in transcript["segments"]
             if s["end"] >= lo and s["start"] <= hi]
    signals = json.loads(cand["signals"] or "{}")
    audio = [f"{hms(a['t'])} intensity {a['intensity']}" for a in signals.get("audio_spikes", [])]
    chat = [f"{hms(c['t'])} {c['msgs_per_sec']} msgs/sec" + (f" | top: {'; '.join(c['top'])}" if c["top"] else "")
            for c in signals.get("chat_spikes", [])]
    parts = [
        f"SOURCE: streamer {source['name']} | platform {source['platform']} | permission {source['permission']}",
        f"STREAM TITLE: {vod['title']}",
        f"WINDOW: {hms(lo)} to {hms(hi)} (timestamps are positions in this recording). "
        f"Combined excitement peaks near {hms(cand['peak_s'])}.",
        "AUDIO LOUDNESS SPIKES:\n" + ("\n".join(audio) or "none above baseline"),
        "CHAT VELOCITY SPIKES:\n" + ("\n".join(chat) or ("none above baseline" if vod["chat_path"] else "no chat log for this source")),
    ]
    if taken:
        parts.append("ALREADY CLIPPED (do not overlap): " + ", ".join(f"{hms(a)}-{hms(b)}" for a, b in taken))
    pref = cfg["selector"].get("preferred_archetypes") or []
    if pref:
        parts.append("Archetypes performing best on this channel right now: " + ", ".join(pref))
    parts.append("TRANSCRIPT:\n" + "\n".join(lines))
    return "\n\n".join(parts)


# ---------------------------------------------------------------- providers

def call_anthropic(cfg: Config, conn: sqlite3.Connection, system: str, user: str) -> str:
    """Claude through the shared door (refusal fallback, outage backup model, spend cap, usage log)."""
    from .llm import LLMDeclined, ask
    try:
        answer = ask(cfg, conn, purpose="moment_picker", system=system, user=user,
                     effort=cfg["selector"].get("effort", "medium"), model=cfg["selector"].get("model"))
    except LLMDeclined as e:
        raise Rejected(str(e)) from None
    return answer.text


def call_file(llm_dir: Path, cand_id: int, system: str, user: str) -> str | None:
    """Offline provider: writes the exact request to disk and reads a response file if one is there.

    Lets you inspect prompts, or answer them with any model by hand, without an API key.
    """
    llm_dir.mkdir(parents=True, exist_ok=True)
    (llm_dir / f"cand_{cand_id}.request.md").write_text(f"<!-- SYSTEM -->\n{system}\n\n<!-- USER -->\n{user}\n", encoding="utf-8")
    response = llm_dir / f"cand_{cand_id}.response.json"
    return response.read_text(encoding="utf-8") if response.exists() else None


# ---------------------------------------------------------------- validation

def extract_json(text: str) -> dict:
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip())
    start = text.find("{")
    if start < 0:
        raise Rejected("no JSON object in response")
    try:
        obj, _ = json.JSONDecoder().raw_decode(text[start:])
    except json.JSONDecodeError as e:
        raise Rejected(f"invalid JSON: {e}") from None
    if not isinstance(obj, dict):
        raise Rejected("response JSON is not an object")
    return obj


def _trim_words(text: str, n: int) -> str:
    return " ".join(str(text or "").split()[:n])


def validate(cfg: Config, data: dict, transcript: dict, window: tuple[float, float],
             taken: list[tuple[float, float]]) -> dict:
    """Returns clean clip fields, or raises Rejected with the reason."""
    sel, safety = cfg["selector"], cfg["safety"]
    clip = data.get("clip")
    if clip is None:
        raise Rejected("model found nothing strong enough")
    if not isinstance(clip, dict):
        raise Rejected("'clip' is not an object")
    if clip.get("skip"):
        raise Rejected("model flagged leaked/pre-release/AI footage")

    words = [w for s in transcript["segments"] for w in s["words"]]
    starts, ends = [w["start"] for w in words], [w["end"] for w in words]

    def snap(t: float, table: list[float]) -> float:
        i = bisect.bisect_left(table, t)
        near = min(table[max(0, i - 1): i + 1], key=lambda x: abs(x - t), default=t)
        return near if abs(near - t) <= 1.5 else t

    raw = clip.get("segments")
    if not isinstance(raw, list) or not 1 <= len(raw) <= 3:
        raise Rejected("needs 1-3 segments")
    segments, prev_end = [], None
    for seg in raw:
        try:
            a, b = parse_ts(seg["start"]), parse_ts(seg["end"])
        except (KeyError, TypeError, ValueError):
            raise Rejected("segment has a missing or malformed timestamp") from None
        if a < window[0] - 2 or b > window[1] + 2:
            raise Rejected(f"timestamps {hms(a)}-{hms(b)} fall outside the window it was given")
        a = max(0.0, snap(a, starts) - START_PAD_S)
        b = min(float(transcript["duration"]), snap(b, ends) + END_PAD_S)
        if b - a < 1:
            raise Rejected("segment is empty or reversed")
        if prev_end is not None:
            if a < prev_end:
                raise Rejected("segments overlap or are out of order")
            if a - prev_end > MAX_SPLICE_GAP_S:
                raise Rejected("spliced parts are more than 60s apart")
            if a - prev_end < 1.0:  # a 'splice' with no real gap is one segment
                segments[-1]["end"] = round(b, 3)
                segments[-1]["verbatim_text"] += " " + str(seg.get("verbatim_text", ""))
                prev_end = b
                continue
        segments.append({"start": round(a, 3), "end": round(b, 3), "verbatim_text": str(seg.get("verbatim_text", ""))})
        prev_end = b

    duration = sum(s["end"] - s["start"] for s in segments)
    if not sel["min_seconds"] <= duration <= sel["max_seconds"]:
        raise Rejected(f"duration {duration:.0f}s is outside {sel['min_seconds']}-{sel['max_seconds']}s")
    span = (segments[0]["start"], segments[-1]["end"])
    for a, b in taken:
        overlap = min(span[1], b) - max(span[0], a)
        if overlap > 0.3 * min(span[1] - span[0], b - a):
            raise Rejected(f"overlaps an existing clip at {hms(a)}-{hms(b)}")

    archetype = str(clip.get("archetype", "")).upper().replace(" ", "_").replace("/", "_")
    if archetype not in ARCHETYPES:
        raise Rejected(f"unknown archetype {clip.get('archetype')!r}")
    scores = clip.get("scores") or {}
    try:
        scores = {k: min(10.0, max(0.0, float(scores[k]))) for k in ("hook", "retention", "shareability", "overall")}
    except (KeyError, TypeError, ValueError):
        raise Rejected("scores are missing or not numbers") from None
    if scores["overall"] < sel["min_overall_score"]:
        raise Rejected(f"overall score {scores['overall']:g} is below the {sel['min_overall_score']} cutoff")

    hook_a, hook_b = _trim_words(clip.get("on_screen_hook_a"), 7), _trim_words(clip.get("on_screen_hook_b"), 7)
    title = str(clip.get("post_title") or "").strip()
    if not (hook_a or hook_b) or not title:
        raise Rejected("missing hook or post title")
    if len(title) > 70:
        title = title[:70].rsplit(" ", 1)[0]

    spoken = " ".join(w["word"] for w in words if any(s["start"] <= w["start"] < s["end"] for s in segments)).lower()
    keyword_spoiler = next((k for k in safety["spoiler_keywords"] if k.lower() in spoken), None)
    return {
        "segments": segments, "start_s": span[0], "end_s": span[1], "duration_s": round(duration, 2),
        "archetype": archetype, "working_title": str(clip.get("title") or "")[:200],
        "hook_a": hook_a or hook_b, "hook_b": hook_b or hook_a, "post_title": title,
        "caption": str(clip.get("caption") or "").strip(), "scores": scores,
        "spoiler_risk": bool(clip.get("spoiler_risk")) or keyword_spoiler is not None,
        "why": str(clip.get("why") or "") + (f" [spoiler keyword: {keyword_spoiler}]" if keyword_spoiler else ""),
    }


# ---------------------------------------------------------------- driver

def select_vod(cfg: Config, conn: sqlite3.Connection, vod_id: int) -> int:
    vod = conn.execute("SELECT * FROM vods WHERE id=?", (vod_id,)).fetchone()
    source = conn.execute("SELECT * FROM sources WHERE id=?", (vod["source_id"],)).fetchone()
    transcript = json.loads(Path(vod["transcript_path"]).read_text(encoding="utf-8"))
    system = (ROOT / "prompts" / "selector_system.md").read_text(encoding="utf-8")
    provider = os.environ.get("CLIPPER_SELECTOR_PROVIDER") or cfg["selector"]["provider"]
    if provider == "anthropic" and not secret("ANTHROPIC_API_KEY"):
        raise RuntimeError("ANTHROPIC_API_KEY is not set. Add it to .env (or set selector.provider: file to work offline)")
    if not cfg["selector"].get("preferred_archetypes"):  # fall back to what the Growth Analyst has learned
        from .db import kv_get
        cfg["selector"]["preferred_archetypes"] = (kv_get(conn, "learned") or {}).get("preferred_archetypes", [])
    styles = cfg["editor"]["caption_styles"]
    ctx = cfg["signals"]["context_seconds"]
    llm_dir = Path(vod["video_path"]).parent / "llm"

    made = waiting = 0
    candidates = conn.execute("SELECT * FROM candidates WHERE vod_id=? AND status='new' ORDER BY score DESC", (vod_id,)).fetchall()
    for cand in candidates:
        taken = [(r["start_s"], r["end_s"]) for r in conn.execute(
            "SELECT start_s, end_s FROM clips WHERE vod_id=? AND status != 'rejected'", (vod_id,))]
        user = build_request(cfg, vod, source, cand, transcript, taken)
        text = None
        try:
            if provider == "anthropic":
                text = call_anthropic(cfg, conn, system, user)
            elif provider == "file":
                text = call_file(llm_dir, cand["id"], system, user)
                if text is None:
                    waiting += 1
                    continue
            else:
                raise RuntimeError(f"unknown selector.provider {provider!r} (use anthropic or file)")
            window = (max(0.0, cand["peak_s"] - ctx), cand["peak_s"] + ctx)
            clip = validate(cfg, extract_json(text), transcript, window, taken)
        except Rejected as e:
            conn.execute("UPDATE candidates SET status='rejected', llm_response=? WHERE id=?",
                         (json.dumps({"reason": str(e), "raw": text}, ensure_ascii=False), cand["id"]))
            conn.commit()
            log(f"  candidate {hms(cand['peak_s'])}: no clip ({e})")
            continue

        n = conn.execute("SELECT count(*) FROM clips").fetchone()[0]
        conn.execute(
            """INSERT INTO clips (vod_id, candidate_id, segments, start_s, end_s, duration_s, archetype, working_title,
                   hook_a, hook_b, hook_used, post_title, caption, score_hook, score_retention, score_shareability,
                   score_overall, spoiler_risk, why, caption_style)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (vod_id, cand["id"], json.dumps(clip["segments"], ensure_ascii=False), clip["start_s"], clip["end_s"],
             clip["duration_s"], clip["archetype"], clip["working_title"], clip["hook_a"], clip["hook_b"],
             clip["hook_a"] if n % 2 == 0 else clip["hook_b"],  # alternate hook styles across uploads
             clip["post_title"], (clip["caption"] + " " + (source["caption_suffix"] or "")).strip(), clip["scores"]["hook"], clip["scores"]["retention"],
             clip["scores"]["shareability"], clip["scores"]["overall"], int(clip["spoiler_risk"]), clip["why"],
             styles[n % len(styles)]))
        conn.execute("UPDATE candidates SET status='selected', llm_response=? WHERE id=?", (text, cand["id"]))
        conn.commit()
        made += 1
        log(f"  clip: {hms(clip['start_s'])}-{hms(clip['end_s'])} {clip['duration_s']:.0f}s {clip['archetype']} "
            f"overall {clip['scores']['overall']:g}  {clip['post_title']!r}" + ("  [SPOILER FLAG]" if clip["spoiler_risk"] else ""))
    if waiting:
        log(f"  {waiting} candidates are waiting for response files in {llm_dir}")
    else:
        conn.execute("UPDATE vods SET status='selected', updated_at=datetime('now') WHERE id=?", (vod_id,))
        conn.commit()
    return made


# ---------------------------------------------------------------- backup picker

def backup_pick(cfg: Config, cand: sqlite3.Row, transcript: dict, source: sqlite3.Row) -> dict:
    """A rule-based answer in the same shape Claude returns, so it passes through the same validation."""
    segs = transcript["segments"]
    peak = cand["peak_s"]
    lo_t, hi_t = peak - 12, peak + 16
    inside = [s for s in segs if s["end"] > lo_t and s["start"] < hi_t]
    if not inside:
        raise Rejected("no speech around the spike")
    start, end = min(s["start"] for s in inside), max(s["end"] for s in inside)
    sel = cfg["selector"]
    if end - start < sel["min_seconds"]:
        end = min(float(transcript["duration"]), start + sel["min_seconds"] + 2)
    if end - start > 45:
        end = start + 45
    quote = max(inside, key=lambda s: (s["text"].count("!") + s["text"].isupper(), len(s["text"])))["text"].strip()
    quote = " ".join(quote.split()[:9]).strip(' ".,')
    name = source["name"]
    return {"clip": {
        "segments": [{"start": round(start, 2), "end": round(end, 2), "verbatim_text": " ".join(s["text"] for s in inside)[:400]}],
        "archetype": "FREAKOUT", "title": f"{name}: {quote}",
        "scores": {"hook": sel["min_overall_score"], "retention": sel["min_overall_score"], "shareability": sel["min_overall_score"],
                   "overall": sel["min_overall_score"]},
        "on_screen_hook_a": " ".join(f'"{quote}"'.split()[:7]), "on_screen_hook_b": "Wait for it...",
        "post_title": f'{name} in GTA 6: "{quote}"'[:70], "caption": f"{name} on GTA 6 #GTA6",
        "spoiler_risk": False, "why": "Picked by the backup picker from the strongest audio/chat spike (Claude was unavailable)."}}


def backup_select_vod(cfg: Config, conn: sqlite3.Connection, vod_id: int, per_chunk: int = 4) -> int:
    vod = conn.execute("SELECT * FROM vods WHERE id=?", (vod_id,)).fetchone()
    source = conn.execute("SELECT * FROM sources WHERE id=?", (vod["source_id"],)).fetchone()
    transcript = json.loads(Path(vod["transcript_path"]).read_text(encoding="utf-8"))
    ctx_s = cfg["signals"]["context_seconds"]
    styles = cfg["editor"]["caption_styles"]
    made = 0
    for cand in conn.execute("SELECT * FROM candidates WHERE vod_id=? AND status='new' ORDER BY score DESC", (vod_id,)).fetchall():
        if made >= per_chunk:
            conn.execute("UPDATE candidates SET status='rejected', llm_response=? WHERE id=?",
                         (json.dumps({"reason": "backup picker only cuts the top spikes"}), cand["id"]))
            continue
        taken = [(r["start_s"], r["end_s"]) for r in conn.execute("SELECT start_s, end_s FROM clips WHERE vod_id=? AND status != 'rejected'", (vod_id,))]
        window = (max(0.0, cand["peak_s"] - ctx_s), cand["peak_s"] + ctx_s)
        try:
            clip = validate(cfg, backup_pick(cfg, cand, transcript, source), transcript, window, taken)
        except Rejected as e:
            conn.execute("UPDATE candidates SET status='rejected', llm_response=? WHERE id=?", (json.dumps({"reason": f"backup: {e}"}), cand["id"]))
            continue
        n = conn.execute("SELECT count(*) FROM clips").fetchone()[0]
        conn.execute(
            """INSERT INTO clips (vod_id, candidate_id, segments, start_s, end_s, duration_s, archetype, working_title, hook_a, hook_b,
                   hook_used, post_title, caption, score_hook, score_retention, score_shareability, score_overall, spoiler_risk, why,
                   caption_style, picked_by) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?, 'backup')""",
            (vod_id, cand["id"], json.dumps(clip["segments"], ensure_ascii=False), clip["start_s"], clip["end_s"], clip["duration_s"],
             clip["archetype"], clip["working_title"], clip["hook_a"], clip["hook_b"], clip["hook_a"], clip["post_title"],
             (clip["caption"] + " " + (source["caption_suffix"] or "")).strip(), clip["scores"]["hook"], clip["scores"]["retention"],
             clip["scores"]["shareability"], clip["scores"]["overall"], int(clip["spoiler_risk"]), clip["why"], styles[n % len(styles)]))
        conn.execute("UPDATE candidates SET status='selected', llm_response=? WHERE id=?", (json.dumps({"backup": True}), cand["id"]))
        made += 1
    conn.execute("UPDATE vods SET status='selected', updated_at=datetime('now') WHERE id=?", (vod_id,))
    conn.commit()
    return made
