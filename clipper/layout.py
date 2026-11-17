"""Finds the streamer's facecam for one clip and decides the vertical layout.

Detection runs per clip, not per source: streamers move and resize their cam mid-stream.
A facecam is a face that stays in the same place for most of the clip; faces in the game move around.
"""
from __future__ import annotations

from pathlib import Path

import cv2
import numpy as np

from .config import ROOT

MODEL = ROOT / "assets" / "models" / "face_detection_yunet_2023mar.onnx"
DETECT_W = 1280
SAMPLES = 14


def sample_frames(video: Path, spans: list[tuple[float, float]], n: int = SAMPLES) -> list[np.ndarray]:
    cap = cv2.VideoCapture(str(video))
    total = sum(b - a for a, b in spans)
    frames = []
    for i in range(n):
        t = (i + 0.5) / n * total
        for a, b in spans:
            if t <= b - a:
                cap.set(cv2.CAP_PROP_POS_MSEC, (a + t) * 1000)
                ok, frame = cap.read()
                if ok:
                    frames.append(frame)
                break
            t -= b - a
    cap.release()
    return frames


def detect_faces(frames: list[np.ndarray]) -> list[list[tuple[float, float, float, float]]]:
    """Per frame: face boxes (x, y, w, h) in source pixels."""
    if not frames:
        return []
    h, w = frames[0].shape[:2]
    scale = DETECT_W / w
    size = (DETECT_W, int(round(h * scale)))
    detector = cv2.FaceDetectorYN.create(str(MODEL), "", size, score_threshold=0.7, nms_threshold=0.3)
    out = []
    for frame in frames:
        _, faces = detector.detect(cv2.resize(frame, size))
        out.append([tuple(float(v) / scale for v in f[:4]) for f in (faces if faces is not None else [])])
    return out


def stable_face(per_frame: list[list[tuple]]) -> tuple[float, float, float, float] | None:
    """The face that appears at the same spot in at least half the frames (median box), if any."""
    all_faces = [f for faces in per_frame for f in faces]
    best: list[tuple] = []
    for seed in all_faces:
        sx, sy, sw = seed[0] + seed[2] / 2, seed[1] + seed[3] / 2, seed[2]
        members = []
        for faces in per_frame:
            near = [f for f in faces
                    if abs(f[0] + f[2] / 2 - sx) < 0.7 * sw and abs(f[1] + f[3] / 2 - sy) < 0.7 * sw
                    and 0.6 < f[2] / sw < 1.6]
            if near:
                members.append(near[0])
        if len(members) > len(best):
            best = members
    if len(best) < max(3, len(per_frame) // 2):
        return None
    return tuple(float(np.median([m[i] for m in best])) for i in range(4))


def refine_cam_box(frames: list[np.ndarray], face: tuple[float, float, float, float]) -> tuple[int, int, int, int] | None:
    """Look for the cam overlay's rectangular border around the face.

    The border is a straight edge that stays put in every frame, while game edges average out.
    Returns (x, y, w, h) in source pixels, or None when no believable rectangle is found.
    """
    H, W = frames[0].shape[:2]
    gx = np.zeros((H, W), np.float32)
    gy = np.zeros((H, W), np.float32)
    for f in frames:
        g = cv2.cvtColor(f, cv2.COLOR_BGR2GRAY).astype(np.float32)
        gx += np.abs(cv2.Sobel(g, cv2.CV_32F, 1, 0, ksize=3))
        gy += np.abs(cv2.Sobel(g, cv2.CV_32F, 0, 1, ksize=3))
    gx /= len(frames)
    gy /= len(frames)

    fx, fy, fw, fh = face
    cx, cy = fx + fw / 2, fy + fh / 2
    y0, y1 = int(max(0, fy - 0.3 * fh)), int(min(H, fy + 1.6 * fh))   # rows the side borders must span
    x0, x1 = int(max(0, fx - 0.5 * fw)), int(min(W, fx + 1.5 * fw))   # columns the top/bottom borders must span
    reach = 7 * fw

    def find(profile: np.ndarray, start: int, stop: int, step: int, frame_edge: int) -> int | None:
        # profile[i]: share of the span where a persistent edge exists at line i
        for i in range(start, stop, step):
            if 0 <= i < len(profile) and profile[i] >= 0.8:
                return i
        return frame_edge if abs(stop - frame_edge) <= 2 else None

    col_profile = (gx[y0:y1, :] > 40).mean(axis=0)
    row_profile = (gy[:, x0:x1] > 40).mean(axis=1)
    left = find(col_profile, int(fx - 0.4 * fw), int(max(-1, cx - reach)), -1, 0)
    right = find(col_profile, int(fx + 1.4 * fw), int(min(W, cx + reach)), 1, W)
    top = find(row_profile, int(fy - 0.3 * fh), int(max(-1, cy - reach)), -1, 0)
    bottom = find(row_profile, int(fy + 1.5 * fh), int(min(H, cy + reach)), 1, H)
    if None in (left, right, top, bottom):
        return None
    w, h = right - left, bottom - top
    if w < 2.2 * fw or h < 2.0 * fh or not 1.0 <= w / h <= 2.4:
        return None
    return int(left), int(top), int(w), int(h)


def _fit_crop(box: tuple[float, float, float, float], aspect: float, center: tuple[float, float],
              frame: tuple[int, int]) -> tuple[int, int, int, int]:
    """Largest crop with the given aspect that fits inside `box`, as close to `center` as the box allows."""
    bx, by, bw, bh = box
    W, H = frame
    w, h = (bw, bw / aspect) if bw / bh <= aspect else (bh * aspect, bh)
    x = min(max(center[0] - w / 2, bx), bx + bw - w)
    y = min(max(center[1] - h / 2, by), by + bh - h)
    w, h = int(w) // 2 * 2, int(h) // 2 * 2
    return int(min(max(x, 0), W - w)), int(min(max(y, 0), H - h)), w, h


def _near(face, ref) -> bool:
    return (abs(face[0] + face[2] / 2 - ref[0] - ref[2] / 2) < 0.7 * ref[2]
            and abs(face[1] + face[3] / 2 - ref[1] - ref[3] / 2) < 0.7 * ref[2] and 0.6 < face[2] / ref[2] < 1.6)


def _layout_from_frames(frames: list[np.ndarray], per_frame: list[list[tuple]], cam_aspect: float,
                        forced_layout: str, forced_cam: list[int] | None) -> dict:
    H, W = frames[0].shape[:2]
    if forced_layout == "gameplay":
        return {"layout": "gameplay", "cam_crop": None, "focus_x": None, "how": "forced by sources.yaml"}
    face = stable_face(per_frame)
    if forced_cam:
        center = (face[0] + face[2] / 2, face[1] + face[3] * 0.6) if face else \
            (forced_cam[0] + forced_cam[2] / 2, forced_cam[1] + forced_cam[3] / 2)
        return {"layout": "split", "cam_crop": _fit_crop(tuple(forced_cam), cam_aspect, center, (W, H)),
                "focus_x": None, "how": "cam box from sources.yaml"}
    if face is None:
        return {"layout": "gameplay", "cam_crop": None, "focus_x": None, "how": "no stable face found"}

    fx, fy, fw, fh = face
    center = (fx + fw / 2, fy + fh * 0.6)
    box = refine_cam_box(frames, face)
    if fw > 0.09 * W or (box and box[2] * box[3] > 0.45 * W * H):
        # The cam fills the screen (just chatting, reaction, IRL): one full-height 9:16 crop on the streamer.
        return {"layout": "fullcam", "cam_crop": _fit_crop(box or (0, 0, W, H), 9 / 16, center, (W, H)),
                "focus_x": None, "how": "full-screen cam"}
    if box:
        return {"layout": "split", "cam_crop": _fit_crop(box, cam_aspect, center, (W, H)),
                "focus_x": None, "how": f"cam border found at {box}"}
    # No clean border (rounded/chroma-keyed cam): crop a head-and-shoulders box around the face.
    w = 3.4 * fw
    guess = (max(0, center[0] - w / 2), max(0, center[1] - w / cam_aspect / 2), min(w, W), min(w / cam_aspect, H))
    return {"layout": "split", "cam_crop": _fit_crop(guess, cam_aspect, center, (W, H)),
            "focus_x": None, "how": "face-centered crop (no cam border found)"}


def plan_segment(video: Path, start: float, end: float, cam_aspect: float,
                 forced_layout: str = "auto", forced_cam: list[int] | None = None) -> list[tuple[float, float, dict]]:
    """Layout for one segment, split into pieces where the stream's scene changes partway through.

    A scene change shows up as the stable facecam appearing or disappearing for a run of samples.
    Returns [(start, end, layout)] covering the segment.
    """
    step = 1.5
    times = [start + step / 2 + i * step for i in range(max(2, int((end - start) / step)))]
    times = [t for t in times if t < end]
    cap = cv2.VideoCapture(str(video))

    def grab(t: float):
        cap.set(cv2.CAP_PROP_POS_MSEC, t * 1000)
        ok, frame = cap.read()
        return frame if ok else None

    frames, kept = [], []
    for t in times:
        f = grab(t)
        if f is not None:
            frames.append(f)
            kept.append(t)
    if not frames:
        cap.release()
        return [(start, end, {"layout": "gameplay", "cam_crop": None, "focus_x": None, "how": "no frames readable"})]
    per_frame = detect_faces(frames)
    face = stable_face(per_frame)

    # Presence of the stable face per sample, with lone flips smoothed out.
    present = [bool(face) and any(_near(f, face) for f in faces) for faces in per_frame]
    for i in range(1, len(present) - 1):
        if present[i - 1] == present[i + 1] != present[i]:
            present[i] = present[i - 1]
    runs = []  # [first_index, last_index]
    for i, p in enumerate(present):
        if runs and present[runs[-1][1]] == p:
            runs[-1][1] = i
        else:
            runs.append([i, i])
    runs = [r for r in runs if r[1] - r[0] + 1 >= 3] if face else []
    if len(runs) < 2:
        cap.release()
        return [(start, end, _layout_from_frames(frames, per_frame, cam_aspect, forced_layout, forced_cam))]

    pieces, piece_start = [], start
    for r, nxt in zip(runs, runs[1:] + [None]):
        if nxt is None:
            cut = end
        else:
            lo, hi, state = kept[r[1]], kept[nxt[0]], present[r[1]]
            for _ in range(5):  # narrow the scene change down to a few frames
                mid = (lo + hi) / 2
                f = grab(mid)
                here = f is not None and any(_near(x, face) for x in detect_faces([f])[0])
                lo, hi = (mid, hi) if here == state else (lo, mid)
            cut = (lo + hi) / 2
        idx = [i for i, t in enumerate(kept) if piece_start <= t < cut]
        if idx:
            layout = _layout_from_frames([frames[i] for i in idx], [per_frame[i] for i in idx],
                                         cam_aspect, forced_layout, forced_cam)
            pieces.append((piece_start, cut, layout))
            piece_start = cut
    cap.release()
    if pieces:
        pieces[-1] = (pieces[-1][0], end, pieces[-1][2])
    return pieces or [(start, end, _layout_from_frames(frames, per_frame, cam_aspect, forced_layout, forced_cam))]
