"""Measures a rendered clip once, so dozens of inspector agents can check it without re-running ffmpeg.

Everything here is a measurement, not a judgement. The inspectors decide what the numbers mean.
"""
from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

import numpy as np

from .config import ROOT, Config
from .util import ffprobe, run

FRAMES = 10  # evenly spaced samples of the finished clip (plus the opening frame)


def _streams(path: Path) -> dict:
    out = subprocess.run(["ffprobe", "-v", "error", "-print_format", "json", "-show_format", "-show_streams", str(path)],
                         capture_output=True, text=True, encoding="utf-8", errors="replace", check=True).stdout
    data = json.loads(out)
    v = next((s for s in data["streams"] if s["codec_type"] == "video"), {})
    a = next((s for s in data["streams"] if s["codec_type"] == "audio"), {})
    fmt = data.get("format", {})
    return {"vcodec": v.get("codec_name"), "pix_fmt": v.get("pix_fmt"), "profile": v.get("profile"),
            "acodec": a.get("codec_name"), "sample_rate": int(a.get("sample_rate") or 0), "channels": int(a.get("channels") or 0),
            "bitrate_kbps": round(int(fmt.get("bit_rate") or 0) / 1000), "size_mb": round(int(fmt.get("size") or 0) / 1e6, 2)}


def _analysis_pass(path: Path) -> dict:
    out = run(["ffmpeg", "-hide_banner", "-nostats", "-i", path, "-filter_complex",
               "[0:v]blackdetect=d=0.4:pix_th=0.10,freezedetect=n=-50dB:d=4[v];[0:a]ebur128=peak=true,silencedetect=n=-45dB:d=1.0[a]",
               "-map", "[v]", "-map", "[a]", "-f", "null", "-"], timeout=900)
    f = {"black": [], "freeze": [], "silence": []}
    m = re.search(r"Integrated loudness:\s+I:\s+(-?[\d.]+) LUFS", out)
    f["lufs"] = float(m.group(1)) if m else None
    m = re.search(r"True peak:\s+Peak:\s+(-?[\d.]+|-inf) dBFS", out)
    f["true_peak"] = float(m.group(1)) if m and m.group(1) != "-inf" else None
    m = re.search(r"Loudness range:\s+LRA:\s+([\d.]+) LU", out)
    f["lra"] = float(m.group(1)) if m else None
    for b in re.finditer(r"black_start:([\d.]+) black_end:([\d.]+)", out):
        f["black"].append([float(b.group(1)), float(b.group(2))])
    starts = [float(x) for x in re.findall(r"freeze_start: ([\d.]+)", out)]
    ends = [float(x) for x in re.findall(r"freeze_end: ([\d.]+)", out)]
    f["freeze"] = [[s, ends[i] if i < len(ends) else None] for i, s in enumerate(starts)]
    s_starts = [float(x) for x in re.findall(r"silence_start: (-?[\d.]+)", out)]
    s_ends = [float(x) for x in re.findall(r"silence_end: ([\d.]+)", out)]
    f["silence"] = [[max(0.0, s), s_ends[i] if i < len(s_ends) else None] for i, s in enumerate(s_starts)]
    return f


def _grab(video: Path, t: float, out: Path, width: int = 540) -> bool:
    try:
        run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-ss", f"{max(0.0, t):.3f}", "-i", video,
             "-frames:v", "1", "-vf", f"scale={width}:-2", "-q:v", "3", out], timeout=60)
        return out.exists()
    except Exception:
        return False


def dhash(gray: np.ndarray) -> int:
    import cv2
    small = cv2.resize(gray, (9, 8), interpolation=cv2.INTER_AREA)
    bits = (small[:, 1:] > small[:, :-1]).flatten()
    return int("".join("1" if b else "0" for b in bits), 2)


def hamming(a: int, b: int) -> int:
    return bin(int(a) ^ int(b)).count("1")


_face = None


def _faces(img) -> list:
    """Face boxes in an image with OpenCV's YuNet (the same model the layout planner uses)."""
    global _face
    import cv2
    model = ROOT / "assets" / "models" / "face_detection_yunet_2023mar.onnx"
    if not model.exists():
        return []
    h, w = img.shape[:2]
    if _face is None:
        _face = cv2.FaceDetectorYN.create(str(model), "", (w, h), 0.6, 0.3, 50)
    _face.setInputSize((w, h))
    _, found = _face.detect(img)
    return [] if found is None else [list(map(float, f[:4])) for f in found]


def _geometry(work: Path, clip, cfg: Config) -> list[dict]:
    """Where each layout sits on the output timeline: written by the editor, guessed for older renders."""
    meta = work / "render.json"
    if meta.exists():
        return json.loads(meta.read_text(encoding="utf-8"))["pieces"]
    kind = (clip["layout_used"] or "split").split(":")[0].split("+")[0].strip() or "split"
    return [{"out_start": 0.0, "out_end": float(clip["duration_s"]), "layout": kind, "src_start": clip["start_s"],
             "src_end": clip["end_s"], "cam_crop": None}]


def _piece_at(pieces: list[dict], t: float) -> dict:
    return next((p for p in pieces if p["out_start"] <= t < p["out_end"]), pieces[-1])


def _letterbox(gray: np.ndarray, mask_box: list[int] | None) -> float:
    """Height share of black bars at both the top and bottom of a source frame (0 when there are none)."""
    h, w = gray.shape
    keep = np.ones_like(gray, dtype=bool)
    if mask_box:  # the streamer's cam overlay sits on top of the bars; ignore it
        x, y, bw, bh = (int(v) for v in mask_box)
        keep[max(0, y):y + bh, max(0, x):x + bw] = False
    dark = (gray < 28) | ~keep
    row_dark = dark.mean(axis=1)
    centre = gray[h // 3: 2 * h // 3][keep[h // 3: 2 * h // 3]]
    if centre.size == 0 or centre.mean() < 30:  # a mostly black frame is a fade, not a letterbox
        return 0.0

    def bar(rows) -> int:
        n = 0
        for r in rows:
            if r < 0.97:
                break
            n += 1
        return n
    top, bottom = bar(row_dark), bar(row_dark[::-1])
    if min(top, bottom) < 0.06 * h or abs(top - bottom) > 0.05 * h:
        return 0.0
    return round((top + bottom) / (2 * h), 3)


def _music(path: Path) -> dict:
    """A beat detector: music has a steady pulse in its loudness onsets, speech and game noise mostly don't."""
    try:
        raw = subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-i", str(path), "-ac", "1", "-ar", "16000",
                              "-f", "s16le", "-"], capture_output=True, check=True, timeout=300).stdout
    except Exception:
        return {"music_windows": [], "music_seconds": 0.0, "beat_strength_max": 0.0}
    x = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
    hop, win = 160, 1024  # 10 ms hops
    if len(x) < win * 4:
        return {"music_windows": [], "music_seconds": 0.0, "beat_strength_max": 0.0}
    frames = np.lib.stride_tricks.sliding_window_view(x, win)[::hop] * np.hanning(win)
    spec = np.abs(np.fft.rfft(frames, axis=1))
    logspec = np.log1p(spec * 10)
    flux = np.maximum(0, np.diff(logspec, axis=0)).sum(axis=1)  # onset strength per hop
    flux = flux - np.convolve(flux, np.ones(50) / 50, mode="same")  # remove slow loudness swings
    flux = np.maximum(flux, 0)
    windows, strengths = [], []
    step, size = 300, 600  # 6 s windows every 3 s
    for start in range(0, max(1, len(flux) - size), step):
        seg = flux[start:start + size]
        seg = seg - seg.mean()
        denom = float((seg * seg).sum())
        if denom <= 1e-9:
            continue
        ac = np.correlate(seg, seg, mode="full")[size - 1:] / denom
        lo, hi = 33, 100  # lags of 0.33-1.0 s: 60-180 beats per minute
        k = int(np.argmax(ac[lo:hi])) + lo
        strength = float(ac[k])
        # A real beat repeats: the second multiple of the lag should be strong too.
        double = float(ac[min(len(ac) - 1, 2 * k)]) if 2 * k < len(ac) else 0.0
        score = round(min(strength, double + 0.1), 3)
        strengths.append(score)
        if score >= 0.32:
            windows.append([round(start / 100, 1), round((start + size) / 100, 1), score])
    merged: list[list[float]] = []
    for a, b, s in windows:
        if merged and a <= merged[-1][1]:
            merged[-1][1], merged[-1][2] = b, max(merged[-1][2], s)
        else:
            merged.append([a, b, s])
    return {"music_windows": merged, "music_seconds": round(sum(b - a for a, b, _ in merged), 1),
            "beat_strength_max": max(strengths, default=0.0)}


def _captions(ass: Path) -> dict:
    out = {"events": 0, "overlaps": 0, "shortest": None, "max_chars": 0, "first_caption": None, "hook_seconds": 0.0,
           "has_credit": False, "has_disclosure": False, "words_per_second": 0.0, "coverage": 0.0}
    if not ass.exists():
        return out
    t = lambda s: sum(float(p) * m for p, m in zip(s.split(":")[::-1], (1, 60, 3600)))
    caps = []
    for line in ass.read_text(encoding="utf-8").splitlines():
        if not line.startswith("Dialogue:"):
            continue
        parts = line.split(",", 9)
        start, end, style, text = t(parts[1]), t(parts[2]), parts[3], re.sub(r"\{[^}]*\}", "", parts[9])
        if style == "Cap":
            caps.append((start, end, text.strip()))
        elif style == "Hook":
            out["hook_seconds"] = round(end - start, 2)
            out["hook_text"] = text.strip()
        elif style == "Credit":
            out["has_credit"] = True
        elif style == "Disclosure":
            out["has_disclosure"] = True
            out["disclosure_until"] = round(end, 2)
    # Each word gets its own event (karaoke highlight), so group identical text into one on-screen line.
    lines: list[list] = []
    for s, e, txt in sorted(caps):
        if lines and lines[-1][2] == txt and s - lines[-1][1] < 0.05:
            lines[-1][1] = e
        else:
            lines.append([s, e, txt])
    out["events"] = len(lines)
    if lines:
        out["first_caption"] = round(lines[0][0], 2)
        out["shortest"] = round(min(e - s for s, e, _ in lines), 2)
        out["max_chars"] = max(len(x) for *_, x in lines)
        total = sum(e - s for s, e, _ in lines)
        out["coverage"] = round(total, 1)
        words = sum(len(x.split()) for *_, x in lines)
        out["words_per_second"] = round(words / max(total, 0.1), 2)
        out["overlaps"] = sum(1 for (a, b, _), (c, d, _) in zip(lines, lines[1:]) if c < b - 0.01)
    return out


def profile_clip(cfg: Config, conn, clip) -> dict:
    import cv2

    from .captions import remap_words
    path = Path(clip["file_path"])
    work = path.parent
    facts: dict = {"file": str(path)}
    info = ffprobe(path)
    facts.update(width=info["width"], height=info["height"], fps=round(info["fps"], 2), duration=round(info["duration"], 2),
                 has_audio=info["has_audio"])
    facts.update(_streams(path))
    facts.update(_analysis_pass(path))
    facts.update(_music(path))
    facts["captions"] = _captions(work / "captions.ass")

    pieces = _geometry(work, clip, cfg)
    facts["layouts"] = sorted({p["layout"] for p in pieces})
    H = cfg["editor"]["height"]
    cam_h = int(H * cfg["editor"]["cam_ratio"]) // 2 * 2
    frames_dir = work / "frames"
    frames_dir.mkdir(exist_ok=True)
    dur = max(0.5, info["duration"])
    times = [0.15] + [dur * (i + 0.5) / FRAMES for i in range(FRAMES)]
    samples = []
    for i, t in enumerate(times):
        out = frames_dir / f"f{i:02d}.jpg"
        if not _grab(path, t, out):
            continue
        img = cv2.imread(str(out))
        if img is None:
            continue
        gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
        h, w = gray.shape
        piece = _piece_at(pieces, t)
        scale = h / H
        if piece["layout"] == "split":
            cam, game = img[: int(cam_h * scale)], gray[int(cam_h * scale):]
        elif piece["layout"] == "fullcam":
            cam, game = img, gray
        else:
            cam, game = None, gray[int(h * 0.25): int(h * 0.75)]
        faces = _faces(cam) if cam is not None else []
        samples.append({"t": round(t, 2), "file": out.name, "layout": piece["layout"], "faces": len(faces),
                        "face_h": round(max((f[3] for f in faces), default=0) / max(1, cam.shape[0]), 3) if cam is not None else None,
                        "sharpness": round(float(cv2.Laplacian(game, cv2.CV_64F).var()), 1),
                        # Fingerprint the gameplay only: one streamer's facecam looks the same in every clip.
                        "brightness": round(float(gray.mean()), 1), "dhash": str(dhash(game))})
    facts["frames"] = samples
    if len(samples) >= 2:
        a = cv2.imread(str(frames_dir / samples[0]["file"]), cv2.IMREAD_GRAYSCALE)
        b = cv2.imread(str(frames_dir / samples[1]["file"]), cv2.IMREAD_GRAYSCALE)
        facts["opening_motion"] = round(float(np.abs(a.astype(np.int16) - b.astype(np.int16)).mean()), 2) if a is not None and b is not None and a.shape == b.shape else None

    # Cutscene check on the source footage: GTA story cutscenes play letterboxed (black bars top and bottom).
    vod = conn.execute("SELECT video_path FROM vods WHERE id=?", (clip["vod_id"],)).fetchone()
    lb = []
    if vod and vod["video_path"] and Path(vod["video_path"]).exists():
        segs = json.loads(clip["segments"])
        span = [(s["start"], s["end"]) for s in segs]
        total = sum(b - a for a, b in span)
        for i in range(8):
            target, acc = total * (i + 0.5) / 8, 0.0
            src_t = span[-1][1] - 0.1
            for a, b in span:
                if acc + (b - a) >= target:
                    src_t = a + (target - acc)
                    break
                acc += b - a
            out = frames_dir / f"src{i}.jpg"
            if _grab(Path(vod["video_path"]), src_t, out, width=640):
                g = cv2.imread(str(out), cv2.IMREAD_GRAYSCALE)
                if g is not None:
                    piece = next((p for p in pieces if p.get("src_start", 0) <= src_t < p.get("src_end", 1e12)), pieces[0])
                    box = piece.get("cam_crop")
                    if box:  # cam box is in source pixels; frames were scaled to 640 wide
                        src_w = piece.get("src_w") or 1920
                        k = 640 / src_w
                        box = [v * k for v in box]
                    lb.append(_letterbox(g, box))
    facts["letterbox"] = lb
    facts["letterbox_share"] = round(sum(1 for v in lb if v > 0) / len(lb), 2) if lb else None

    # Speech timing from the transcript (captions follow it, so this is what a viewer reads).
    tr = conn.execute("SELECT transcript_path FROM vods WHERE id=?", (clip["vod_id"],)).fetchone()
    words = []
    if tr and tr["transcript_path"] and Path(tr["transcript_path"]).exists():
        transcript = json.loads(Path(tr["transcript_path"]).read_text(encoding="utf-8"))
        words = remap_words([w for s in transcript["segments"] for w in s["words"]], json.loads(clip["segments"]))
    facts["speech"] = {"words": len(words), "first_word": round(words[0]["start"], 2) if words else None,
                       "last_word_end": round(words[-1]["end"], 2) if words else None,
                       "talk_seconds": round(sum(w["end"] - w["start"] for w in words), 1),
                       "text": " ".join(w["word"] for w in words)[:4000]}
    thumb = work / "thumb.jpg"
    if thumb.exists():
        img = cv2.imread(str(thumb))
        if img is not None:
            g = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
            facts["thumb"] = {"brightness": round(float(g.mean()), 1), "contrast": round(float(g.std()), 1),
                              "sharpness": round(float(cv2.Laplacian(g, cv2.CV_64F).var()), 1), "faces": len(_faces(img))}
    return facts


def save_facts(conn, clip_id: int, facts: dict) -> None:
    conn.execute("INSERT INTO clip_facts (clip_id, facts) VALUES (?,?) ON CONFLICT(clip_id) DO UPDATE SET facts=excluded.facts,"
                 " profiled_at=datetime('now')", (clip_id, json.dumps(facts, ensure_ascii=False)))
    conn.commit()
