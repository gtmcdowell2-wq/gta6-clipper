"""Stage 3a: word-timestamped transcript with faster-whisper (CUDA when available, CPU otherwise)."""
from __future__ import annotations

import json
import os
import sqlite3
import sys
import time
import wave
from pathlib import Path

import numpy as np

from .config import Config
from .util import hms, log, run


def _add_nvidia_dll_dirs() -> None:
    """On Windows the pip-installed cuBLAS/cuDNN DLLs aren't on the search path by default."""
    if os.name != "nt":
        return
    for site in sys.path:
        base = Path(site) / "nvidia"
        if base.is_dir():
            for bin_dir in base.glob("*/bin"):
                os.add_dll_directory(str(bin_dir))
                os.environ["PATH"] = str(bin_dir) + os.pathsep + os.environ["PATH"]


def extract_audio(video: Path, wav: Path) -> Path:
    """16 kHz mono WAV: what Whisper wants, and what signals.py reads for loudness."""
    if not wav.exists():
        tmp = wav.with_name(wav.stem + ".tmp.wav")
        run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", video,
             "-vn", "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", tmp])
        tmp.replace(wav)
    return wav


def load_model(cfg: Config):
    from faster_whisper import WhisperModel

    t = cfg["transcribe"]
    _add_nvidia_dll_dirs()
    try:
        import ctranslate2
        has_cuda = ctranslate2.get_cuda_device_count() > 0
    except Exception:
        has_cuda = False
    if has_cuda:
        try:
            model = WhisperModel(t["model_gpu"], device="cuda", compute_type="float16")
            return model, f"{t['model_gpu']} on cuda"
        except Exception as e:
            log(f"  CUDA load failed ({str(e)[:120]}); falling back to CPU")
    return load_cpu_model(cfg)


def load_cpu_model(cfg: Config):
    """The backup: the CPU model, used when there is no GPU or the GPU fails mid-run."""
    from faster_whisper import WhisperModel
    t = cfg["transcribe"]
    model = WhisperModel(t["model_cpu"], device="cpu", compute_type="int8", cpu_threads=os.cpu_count() or 4)
    return model, f"{t['model_cpu']} on cpu"


def transcribe_file(cfg: Config, video: Path, out_dir: Path, model=None) -> Path:
    wav = extract_audio(video, out_dir / "audio.wav")
    label = "preloaded model"
    if model is None:
        model, label = load_model(cfg)
    log(f"  transcribing with {label}")
    started = time.time()
    # Decode the WAV ourselves: faster-whisper's own file decoding depends on a matching PyAV build.
    with wave.open(str(wav), "rb") as w:
        audio = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16).astype(np.float32) / 32768
    segments_iter, info = model.transcribe(
        audio, language=cfg["transcribe"].get("language"), word_timestamps=True,
        vad_filter=True, vad_parameters={"min_silence_duration_ms": 500},
        condition_on_previous_text=False,  # stops one hallucination from poisoning the rest of a long stream
    )
    segments = []
    last_report = 0.0
    for s in segments_iter:
        words = [{"start": round(w.start, 3), "end": round(w.end, 3), "word": w.word.strip(),
                  "prob": round(w.probability, 3)} for w in (s.words or []) if w.word.strip()]
        if not words:
            continue
        segments.append({"start": round(s.start, 3), "end": round(s.end, 3), "text": s.text.strip(), "words": words})
        if s.end - last_report >= 300:
            last_report = s.end
            log(f"    {hms(s.end)} / {hms(info.duration)}")
    elapsed = time.time() - started

    data = {"language": info.language, "duration": info.duration, "model": label,
            "speed_x_realtime": round(info.duration / max(elapsed, 0.01), 1), "segments": segments}
    path = out_dir / "transcript.json"
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    (out_dir / "transcript.txt").write_text(
        "\n".join(f"[{hms(s['start'])}] {s['text']}" for s in segments) + "\n", encoding="utf-8")
    log(f"  transcript: {len(segments)} segments, {sum(len(s['words']) for s in segments)} words, "
        f"{data['speed_x_realtime']}x realtime")
    return path


def transcribe_vod(cfg: Config, conn: sqlite3.Connection, vod_id: int, model=None) -> Path:
    vod = conn.execute("SELECT * FROM vods WHERE id=?", (vod_id,)).fetchone()
    video = Path(vod["video_path"])
    existing = video.parent / "transcript.json"
    if vod["transcript_path"] and existing.exists():
        return existing
    path = transcribe_file(cfg, video, video.parent, model)
    conn.execute("UPDATE vods SET status='transcribed', transcript_path=?, updated_at=datetime('now') WHERE id=?",
                 (str(path), vod_id))
    conn.commit()
    return path
