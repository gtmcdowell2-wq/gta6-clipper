"""Stage 5: cut a selected clip into a 1080x1920 short with facecam, gameplay, captions, hook and credit."""
from __future__ import annotations

import functools
import json
import re
import shutil
import sqlite3
from pathlib import Path

from .captions import build_ass, has_slur, remap_words
from .config import ROOT, Config
from .layout import plan_segment
from .util import ffprobe, log, run


@functools.lru_cache(maxsize=1)
def _video_encoder() -> list[str]:
    from .doctor import nvenc_works
    if shutil.which("nvidia-smi") and nvenc_works():
        return ["-c:v", "h264_nvenc", "-preset", "p5", "-rc", "vbr", "-cq", "21", "-b:v", "0", "-maxrate", "14M", "-bufsize", "28M"]
    return ["-c:v", "libx264", "-preset", "medium", "-crf", "20", "-maxrate", "14M", "-bufsize", "28M"]


def _pane_filters(layout: dict, src: tuple[int, int], out: tuple[int, int], cam_h: int, k: int, pane_aspect: float) -> str:
    """Filter chain turning input k's video into one vertical frame, labelled [v{k}]."""
    W, H = out
    sw, sh = src
    if layout["layout"] == "split":
        cx, cy, cw, ch = layout["cam_crop"]
        game_h = H - cam_h
        gw = min(sw, int(sh * W / game_h) // 2 * 2)
        gx = (sw - gw) // 2
        # If the centered gameplay crop would include the cam overlay, slide it away from the cam.
        if cx < gx + gw and cx + cw > gx:
            gx = min(sw - gw, cx + cw) if cx + cw / 2 < sw / 2 else max(0, cx - gw)
        return (f"[{k}:v]split=2[c{k}][g{k}];"
                f"[c{k}]crop={cw}:{ch}:{cx}:{cy},scale={W}:{cam_h}:flags=lanczos,setsar=1[t{k}];"
                f"[g{k}]crop={gw}:{sh}:{gx}:0,scale={W}:{game_h}:flags=bicubic,setsar=1[b{k}];"
                f"[t{k}][b{k}]vstack[v{k}]")
    focus = layout.get("focus_x") or sw / 2
    if layout["layout"] == "fullcam":
        cx, cy, cw, ch = layout["cam_crop"]
        return f"[{k}:v]crop={cw}:{ch}:{cx}:{cy},scale={W}:{H}:flags=lanczos,setsar=1[v{k}]"
    # One pane over a blurred copy of itself.
    fw = min(sw, int(sh * pane_aspect) // 2 * 2)
    fx = int(min(max(focus - fw / 2, 0), sw - fw))
    fg_h = int(W * sh / fw) // 2 * 2
    return (f"[{k}:v]split=2[bg{k}][fg{k}];"
            f"[bg{k}]scale={W // 4}:{H // 4}:force_original_aspect_ratio=increase,crop={W // 4}:{H // 4},"
            f"boxblur=8:2,scale={W}:{H},eq=brightness=-0.12,setsar=1[bb{k}];"
            f"[fg{k}]crop={fw}:{sh}:{fx}:0,scale={W}:{fg_h}:flags=bicubic,setsar=1[ff{k}];"
            f"[bb{k}][ff{k}]overlay=0:(H-h)/2[v{k}]")


def _measure_loudness(video: Path, segments: list[dict], target: float) -> dict | None:
    cmd = ["ffmpeg", "-hide_banner", "-nostats"]
    for s in segments:
        cmd += ["-ss", f"{s['start']:.3f}", "-t", f"{s['end'] - s['start']:.3f}", "-i", video]
    graph = "".join(f"[{k}:a]" for k in range(len(segments))) + \
        f"concat=n={len(segments)}:v=0:a=1,loudnorm=I={target}:TP=-1.5:LRA=11:print_format=json[a]"
    out = run(cmd + ["-filter_complex", graph, "-map", "[a]", "-f", "null", "-"])
    match = re.search(r"\{[^{}]*\"input_i\"[^{}]*\}", out, re.S)
    if not match:
        return None
    stats = json.loads(match.group(0))
    return None if "inf" in str(stats.get("input_i")) else stats


def render_clip(cfg: Config, conn: sqlite3.Connection, clip_id: int) -> Path:
    ed = cfg["editor"]
    clip = conn.execute("SELECT * FROM clips WHERE id=?", (clip_id,)).fetchone()
    vod = conn.execute("SELECT * FROM vods WHERE id=?", (clip["vod_id"],)).fetchone()
    source = conn.execute("SELECT * FROM sources WHERE id=?", (vod["source_id"],)).fetchone()
    video = Path(vod["video_path"])
    if not video.exists():
        raise RuntimeError(f"source video is gone ({video}); it may have been cleaned up")
    segments = json.loads(clip["segments"])
    duration = sum(s["end"] - s["start"] for s in segments)
    info = ffprobe(video)
    W, H = ed["width"], ed["height"]
    cam_h = int(H * ed["cam_ratio"]) // 2 * 2
    fps = ed["fps"] if info["fps"] >= ed["fps"] - 1 else 30

    conn.execute("UPDATE clips SET status='rendering', updated_at=datetime('now') WHERE id=?", (clip_id,))
    conn.commit()
    try:
        # A layout per piece: splices and mid-segment scene changes (cam moved, went full screen) each get their own.
        forced_cam = json.loads(source["cam"]) if source["cam"] else None
        pieces = []  # (start, end, layout, fade_in, fade_out)
        for seg in segments:
            parts = plan_segment(video, seg["start"], seg["end"], W / cam_h, source["layout"], forced_cam)
            for i, (a, b, lay) in enumerate(parts):
                pieces.append((a, b, lay, i == 0, i == len(parts) - 1))
        layouts = [p[2] for p in pieces]

        clips_dir = cfg.data_dir / "clips"
        work = clips_dir / str(clip_id)
        work.mkdir(parents=True, exist_ok=True)
        fonts = clips_dir / "_fonts"  # next to the clips so ffmpeg gets drive-letter-free relative paths
        if not fonts.exists():
            shutil.copytree(ROOT / "assets" / "fonts", fonts)

        transcript = json.loads(Path(vod["transcript_path"]).read_text(encoding="utf-8"))
        words = remap_words([w for s in transcript["segments"] for w in s["words"]], segments)
        # Split layout: captions sit on the seam between cam and gameplay. Single pane: lower third.
        caption_y, t = [], 0.0
        for a, b, lay, _, _ in pieces:
            t += b - a
            caption_y.append((t, cam_h + 20 if lay["layout"] == "split" else int(H * 0.70)))
        handle = source["key"].split(":", 1)[1]
        (work / "captions.ass").write_text(
            build_ass(words, duration, clip["caption_style"], clip["hook_used"], ed["hook_seconds"],
                      f"@{handle}", caption_y), encoding="utf-8")

        loud = _measure_loudness(video, segments, ed["target_lufs"])
        norm = f"loudnorm=I={ed['target_lufs']}:TP=-1.5:LRA=11"
        if loud:
            norm += (f":measured_I={loud['input_i']}:measured_TP={loud['input_tp']}:measured_LRA={loud['input_lra']}"
                     f":measured_thresh={loud['input_thresh']}:offset={loud['target_offset']}:linear=true")

        cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y"]
        graph = []
        for k, (a, b, lay, fade_in, fade_out) in enumerate(pieces):
            d = b - a
            cmd += ["-ss", f"{a:.3f}", "-t", f"{d:.3f}", "-i", video]
            graph.append(_pane_filters(lay, (info["width"], info["height"]), (W, H), cam_h, k,
                                       ed.get("single_pane_aspect", 1.0)))
            # Tiny fades stop clicks where two segments are spliced together.
            fades = (["afade=t=in:d=0.03"] if fade_in else []) + ([f"afade=t=out:st={max(0, d - 0.03):.3f}:d=0.03"] if fade_out else [])
            graph.append(f"[{k}:a]{','.join(fades) or 'anull'}[a{k}]")
        n = len(pieces)
        graph.append("".join(f"[v{k}][a{k}]" for k in range(n)) + f"concat=n={n}:v=1:a=1[vc][ac]")
        graph.append(f"[vc]fps={fps},ass={clip_id}/captions.ass:fontsdir=_fonts,format=yuv420p[vout]")
        graph.append(f"[ac]{norm},aresample=48000[aout]")
        out_tmp = work / "clip.tmp.mp4"
        cmd += ["-filter_complex", ";".join(graph), "-map", "[vout]", "-map", "[aout]", *_video_encoder(),
                "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart", out_tmp]
        run(cmd, cwd=clips_dir)
        out = work / "clip.mp4"
        out_tmp.replace(out)
        run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-ss", f"{min(1.5, duration / 2):.2f}", "-i", out,
             "-frames:v", "1", "-vf", "scale=540:-2", work / "thumb.jpg"])
    except Exception as e:
        conn.execute("UPDATE clips SET status='failed', error=?, updated_at=datetime('now') WHERE id=?", (str(e)[:1000], clip_id))
        conn.commit()
        raise

    kinds = " + ".join(dict.fromkeys(lay["layout"] for lay in layouts))
    notes = "; ".join(dict.fromkeys(lay["how"] for lay in layouts)) + ("; contains a slur (masked in captions, audible in audio)" if has_slur(" ".join(w["word"] for w in words)) else "")
    conn.execute("UPDATE clips SET status='pending_review', file_path=?, layout_used=?, duration_s=?, error=NULL,"
                 " updated_at=datetime('now') WHERE id=?", (str(out), f"{kinds}: {notes}", round(duration, 2), clip_id))
    conn.commit()
    return out


def render_vod(cfg: Config, conn: sqlite3.Connection, vod_id: int) -> int:
    done = 0
    for row in conn.execute("SELECT id, post_title FROM clips WHERE vod_id=? AND status IN ('selected','failed')", (vod_id,)).fetchall():
        try:
            path = render_clip(cfg, conn, row["id"])
            used = conn.execute("SELECT layout_used FROM clips WHERE id=?", (row["id"],)).fetchone()[0]
            log(f"  clip {row['id']}: {path}  [{used}]")
            done += 1
        except Exception as e:
            log(f"  clip {row['id']} FAILED: {str(e)[:300]}")
    if not conn.execute("SELECT 1 FROM clips WHERE vod_id=? AND status IN ('selected','failed')", (vod_id,)).fetchone():
        conn.execute("UPDATE vods SET status='done', updated_at=datetime('now') WHERE id=?", (vod_id,))
        conn.commit()
    return done
