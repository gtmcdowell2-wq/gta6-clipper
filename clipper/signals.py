"""Stage 3b: per-second excitement timeline from audio loudness, chat velocity and transcript, then candidate windows."""
from __future__ import annotations

import json
import re
import sqlite3
import wave
from collections import Counter
from pathlib import Path

import numpy as np

from .config import Config
from .transcribe import extract_audio
from .util import hms, log

CHAT_LAG_S = 6          # chat reacts a few seconds after the moment (stream delay + typing)
MIN_PEAK_GAP_S = 45
PRE_S, POST_S = 40, 25  # candidate window around a peak: the setup comes before the reaction

EXCITED = re.compile(
    r"\b(oh my (god|gosh)|no way|what the|holy|wtf|bro+|yo+|let'?s go+|oh no+|oh sh\w*|ha(ha)+|"
    r"are you (kidding|serious)|what just happened|did you see|look at (this|that)|i'?m dead|crazy|insane|"
    r"run|go go|watch out|chat)\b", re.I)


def _rolling(x: np.ndarray, window: int, fn) -> np.ndarray:
    """Centered rolling statistic with edge padding."""
    if len(x) == 0:
        return x
    half = window // 2
    padded = np.pad(x, (half, half), mode="edge")
    return fn(np.lib.stride_tricks.sliding_window_view(padded, 2 * half + 1), axis=1)[: len(x)]


def _zscore(x: np.ndarray, window: int, min_floor: float = 1e-6) -> np.ndarray:
    """How far each second sits above its local baseline, in robust standard deviations."""
    base = _rolling(x, window, np.median)
    mad = _rolling(np.abs(x - base), window, np.median) * 1.4826
    floor = max(float(np.median(mad)), min_floor)  # sparse signals would otherwise saturate
    return np.clip((x - base) / np.maximum(mad, floor), 0, 8)


def audio_loudness(wav_path: Path) -> np.ndarray:
    """RMS level in dB for each second of a 16 kHz mono WAV."""
    with wave.open(str(wav_path), "rb") as w:
        rate, n = w.getframerate(), w.getnframes()
        seconds = n // rate
        out = np.empty(seconds, dtype=np.float32)
        for i in range(seconds):  # one second at a time keeps a 12h VOD out of RAM
            chunk = np.frombuffer(w.readframes(rate), dtype=np.int16).astype(np.float32) / 32768
            out[i] = 20 * np.log10(np.sqrt(np.mean(chunk ** 2)) + 1e-6)
    return np.maximum(out, -60)


def chat_series(chat: list[dict], seconds: int, hype_terms: list[str]) -> tuple[np.ndarray, np.ndarray]:
    rate, hype = np.zeros(seconds, np.float32), np.zeros(seconds, np.float32)
    terms = [t.lower() for t in hype_terms]
    for m in chat:
        t = int(m["t"] - CHAT_LAG_S)
        if 0 <= t < seconds:
            rate[t] += 1
            text = m["text"].lower()
            tokens = set(text.split())
            if any((term in tokens) if len(term) <= 2 else (term in text) for term in terms):
                hype[t] += 1
    return rate, hype


def text_series(transcript: dict, seconds: int) -> np.ndarray:
    out = np.zeros(seconds, np.float32)
    for seg in transcript["segments"]:
        t = int(seg["start"])
        if 0 <= t < seconds:
            out[t] += 2 * len(EXCITED.findall(seg["text"])) + seg["text"].count("!") + 0.5 * seg["text"].count("?")
    return out


def pick_peaks(score: np.ndarray, n: int, min_gap: int) -> list[int]:
    order, picked = np.argsort(score)[::-1], []
    for i in order:
        if score[i] <= 0 or len(picked) >= n:
            break
        if all(abs(int(i) - p) >= min_gap for p in picked):
            picked.append(int(i))
    return sorted(picked)


def _top_messages(chat: list[dict], start: float, end: float, k: int = 5) -> list[str]:
    counts = Counter(m["text"].strip().lower()[:40] for m in chat
                     if start <= m["t"] - CHAT_LAG_S <= end and m["text"].strip())
    return [f"{text} x{c}" for text, c in counts.most_common(k) if c > 1]


def analyze(cfg: Config, video: Path, transcript: dict, chat: list[dict] | None) -> tuple[list[dict], dict]:
    sig = cfg["signals"]
    loud = audio_loudness(extract_audio(video, video.parent / "audio.wav"))
    seconds = len(loud)
    smooth = lambda x, w: _rolling(x, w, np.mean)

    audio_z = _zscore(smooth(loud, 3), 121, 1.0)
    text_z = _zscore(smooth(text_series(transcript, seconds), 9), 301, min_floor=0.4)
    if chat:
        rate, hype = chat_series(chat, seconds, sig["chat_hype_terms"])
        chat_z = _zscore(smooth(rate, 5), 301, 0.3) + 0.5 * _zscore(smooth(hype, 5), 301, 0.3)
        combined = 0.40 * audio_z + 0.40 * chat_z + 0.20 * text_z
    else:
        rate, chat_z = np.zeros(seconds, np.float32), np.zeros(seconds, np.float32)
        combined = 0.65 * audio_z + 0.35 * text_z
    # A moment is a stretch of excitement, not one loud second.
    score = smooth(combined, 15)

    want = max(8, round(sig["candidates_per_2h"] * seconds / 7200))
    want = min(want, max(1, seconds // 90))
    candidates = []
    for peak in pick_peaks(score, want, MIN_PEAK_GAP_S):
        start, end = max(0, peak - PRE_S), min(seconds, peak + POST_S)
        ctx0, ctx1 = max(0, peak - sig["context_seconds"]), min(seconds, peak + sig["context_seconds"])
        a_idx = [int(i) for i in np.argsort(audio_z[ctx0:ctx1])[::-1][:6] + ctx0 if audio_z[i] >= 2]
        c_idx = [int(i) for i in np.argsort(chat_z[ctx0:ctx1])[::-1][:20] + ctx0 if chat_z[i] >= 2]
        chat_spikes, last = [], -99
        for i in sorted(c_idx):
            if i - last >= 10:  # one entry per burst
                chat_spikes.append({"t": i, "msgs_per_sec": round(float(smooth(rate, 5)[i]), 1),
                                    "top": _top_messages(chat or [], i - 5, i + 10)})
                last = i
        candidates.append({
            "start_s": float(start), "end_s": float(end), "peak_s": float(peak),
            "score": round(float(score[peak]), 3),
            "audio_score": round(float(audio_z[start:end].max()), 2),
            "chat_score": round(float(chat_z[start:end].max()), 2) if chat else None,
            "text_score": round(float(text_z[start:end].max()), 2),
            "signals": {
                "audio_spikes": [{"t": i, "intensity": round(float(audio_z[i]), 1)} for i in sorted(a_idx)],
                "chat_spikes": chat_spikes[:6],
            },
        })
    timeline = {"seconds": seconds, "has_chat": bool(chat),
                "score": np.round(score, 3).tolist(), "audio": np.round(audio_z, 2).tolist(),
                "chat": np.round(chat_z, 2).tolist(), "text": np.round(text_z, 2).tolist()}
    return candidates, timeline


def analyze_vod(cfg: Config, conn: sqlite3.Connection, vod_id: int) -> int:
    vod = conn.execute("SELECT * FROM vods WHERE id=?", (vod_id,)).fetchone()
    video = Path(vod["video_path"])
    transcript = json.loads(Path(vod["transcript_path"]).read_text(encoding="utf-8"))
    chat = json.loads(Path(vod["chat_path"]).read_text(encoding="utf-8")) if vod["chat_path"] else None
    candidates, timeline = analyze(cfg, video, transcript, chat)
    (video.parent / "signals.json").write_text(json.dumps(timeline), encoding="utf-8")

    # Re-analysis replaces untouched candidates; ones already sent to the LLM stay.
    conn.execute("DELETE FROM candidates WHERE vod_id=? AND status='new'", (vod_id,))
    done = [(r["start_s"], r["end_s"]) for r in conn.execute("SELECT start_s, end_s FROM candidates WHERE vod_id=?", (vod_id,))]
    added = 0
    for c in candidates:
        if any(c["peak_s"] >= s and c["peak_s"] <= e for s, e in done):
            continue
        conn.execute(
            "INSERT INTO candidates (vod_id, start_s, end_s, peak_s, score, audio_score, chat_score, text_score, signals)"
            " VALUES (?,?,?,?,?,?,?,?,?)",
            (vod_id, c["start_s"], c["end_s"], c["peak_s"], c["score"], c["audio_score"], c["chat_score"],
             c["text_score"], json.dumps(c["signals"], ensure_ascii=False)))
        added += 1
        log(f"  candidate {hms(c['peak_s'])}  score {c['score']:.2f}  (audio {c['audio_score']}, chat {c['chat_score']}, text {c['text_score']})")
    conn.execute("UPDATE vods SET status='analyzed', updated_at=datetime('now') WHERE id=?", (vod_id,))
    conn.commit()
    return added
