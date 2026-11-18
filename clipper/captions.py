"""Builds the .ass file for a clip: word-by-word captions, the hook banner, and the credit tag."""
from __future__ import annotations

import re

W, H = 1080, 1920

# ASS colours are &HAABBGGRR. Each preset pairs a caption look with a hook-banner look so
# consecutive uploads don't share a template.
STYLES = {
    "bold_yellow": {
        "font": "Anton", "size": 104, "primary": "&H00FFFFFF", "active": "&H0000F0FF", "outline": 7, "shadow": 3,
        "upper": True, "pop": False, "spacing": 1,
        "hook": {"font": "Anton", "size": 78, "text": "&H00FFFFFF", "box": "&H00000000", "border_style": 3, "outline": 14},
    },
    "clean_white": {
        "font": "Poppins ExtraBold", "size": 80, "primary": "&H00FFFFFF", "active": "&H00FFE14D", "outline": 6, "shadow": 2,
        "upper": False, "pop": False, "spacing": 0,
        "hook": {"font": "Poppins ExtraBold", "size": 62, "text": "&H00000000", "box": "&H0000E5FF", "border_style": 3, "outline": 14},
    },
    "pop_green": {
        "font": "Bangers", "size": 112, "primary": "&H00FFFFFF", "active": "&H0050FF39", "outline": 7, "shadow": 4,
        "upper": True, "pop": True, "spacing": 2,
        "hook": {"font": "Bangers", "size": 88, "text": "&H00FFFFFF", "box": "&H003C14DC", "border_style": 3, "outline": 14},
    },
}

# Captions are burned in permanently, so strong language is masked on screen.
# The audio is untouched: clips with slurs still need a human call in review.
_CENSOR = [
    (re.compile(r"\bn[i1]gg(?:a|er|uh|ah)(s?)\b", re.I), lambda m: "n***a" + m.group(1)),
    (re.compile(r"\b(mother)?f+u+c+k+(\w*)", re.I), lambda m: (m.group(1) or "") + "f**k" + m.group(2)),
    (re.compile(r"\bsh[i1]+t+(\w*)", re.I), lambda m: "sh*t" + m.group(1)),
    (re.compile(r"\bb[i1]tch(\w*)", re.I), lambda m: "b*tch" + m.group(1)),
    (re.compile(r"\bf[a4]gg?[o0]?t?s?\b|\bretard(ed|s)?\b", re.I), lambda m: "****"),
]


def censor(word: str) -> str:
    for pattern, repl in _CENSOR:
        word = pattern.sub(repl, word)
    return word


def has_slur(text: str) -> bool:
    return bool(_CENSOR[0][0].search(text) or _CENSOR[4][0].search(text))


def _ts(t: float) -> str:
    t = max(0.0, t)
    cs = int(round(t * 100))
    return f"{cs // 360000}:{cs // 6000 % 60:02d}:{cs // 100 % 60:02d}.{cs % 100:02d}"


def _esc(text: str) -> str:
    return text.replace("\\", "").replace("{", "(").replace("}", ")").replace("\n", " ")


def remap_words(words: list[dict], segments: list[dict]) -> list[dict]:
    """Words inside the clip's segments, with times moved onto the output timeline."""
    out, offset = [], 0.0
    for seg in segments:
        for w in words:
            mid = (w["start"] + w["end"]) / 2
            if seg["start"] <= mid < seg["end"]:
                out.append({"word": w["word"], "start": max(0.0, w["start"] - seg["start"]) + offset,
                            "end": min(seg["end"], w["end"]) - seg["start"] + offset})
        offset += seg["end"] - seg["start"]
    return out


def chunk_words(words: list[dict], max_words: int = 3, max_chars: int = 16) -> list[list[dict]]:
    """2-4 words on screen at a time, broken at pauses and sentence ends."""
    chunks, cur = [], []
    for w in words:
        if cur:
            gap = w["start"] - cur[-1]["end"]
            chars = sum(len(x["word"]) for x in cur) + len(cur) + len(w["word"])
            ended = cur[-1]["word"][-1:] in ".?!"
            if len(cur) >= max_words or gap > 0.55 or (ended and len(cur) >= 2) or (chars > max_chars and len(cur) >= 2) or len(cur) >= 4:
                chunks.append(cur)
                cur = []
        cur.append(w)
    if cur:
        chunks.append(cur)
    return chunks


# Text positions keep clear of the apps' own buttons: TikTok's top bar covers roughly the first 130 px,
# and the caption/buttons area covers the bottom fifth and the right edge.
TOP_SAFE = 150


def build_ass(words: list[dict], duration: float, style_name: str, hook: str | None, hook_seconds: float,
              credit: str | None, caption_y: list[tuple[float, int]], disclosure: str | None = None,
              disclosure_seconds: float = 4.0, progress_y: int | None = None) -> str:
    """caption_y: [(output_time_end, y)] so captions follow the layout of each spliced segment.

    disclosure: paid-partnership label shown top-right for the first seconds (campaign clips, FTC).
    progress_y: draw a thin progress bar across the frame at this height (None = no bar).
    """
    st = STYLES.get(style_name) or STYLES["bold_yellow"]
    hk = st["hook"]
    head = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {W}
PlayResY: {H}
WrapStyle: 0
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Cap,{st['font']},{st['size']},{st['primary']},&H000000FF,&H00000000,&H96000000,0,0,0,0,100,100,{st['spacing']},0,1,{st['outline']},{st['shadow']},5,60,60,0,1
Style: Hook,{hk['font']},{hk['size']},{hk['text']},&H000000FF,{hk['box']},{hk['box']},0,0,0,0,100,100,1,0,{hk['border_style']},{hk['outline']},0,8,90,90,{TOP_SAFE + 80},1
Style: Credit,Poppins ExtraBold,38,&H40FFFFFF,&H000000FF,&H60000000,&H00000000,0,0,0,0,100,100,0,0,1,3,0,7,40,40,{TOP_SAFE},1
Style: Disclosure,Poppins ExtraBold,34,&H00FFFFFF,&H000000FF,&H8C000000,&H8C000000,0,0,0,0,100,100,0,0,3,10,0,9,40,40,{TOP_SAFE},1
Style: Bar,Poppins ExtraBold,10,{st['active']},&H000000FF,&H00000000,&H00000000,0,0,0,0,100,100,0,0,1,0,0,7,0,0,0,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    events = []
    if credit:
        events.append(f"Dialogue: 0,{_ts(0)},{_ts(duration)},Credit,,0,0,0,,{_esc(credit)}")
    if disclosure:
        events.append(f"Dialogue: 3,{_ts(0)},{_ts(min(disclosure_seconds, duration))},Disclosure,,0,0,0,,{{\\fad(0,300)}}{_esc(disclosure)}")
    if progress_y is not None:
        ms = int(duration * 1000)
        events.append(f"Dialogue: 4,{_ts(0)},{_ts(duration)},Bar,,0,0,0,,{{\\pos(0,{progress_y})\\clip(0,0,0,{H})"
                      f"\\t(0,{ms},\\clip(0,0,{W},{H}))\\p1}}m 0 0 l {W} 0 l {W} 10 l 0 10{{\\p0}}")
    if hook:
        text = _esc(hook.upper() if st["upper"] else hook)
        events.append(f"Dialogue: 2,{_ts(0)},{_ts(min(hook_seconds, duration))},Hook,,0,0,0,,{{\\fad(0,250)}}{text}")

    words = sorted(words, key=lambda w: w["start"])
    chunks = chunk_words(words)
    last_end = 0.0  # Whisper word times can overlap; events on screen must not
    for ci, chunk in enumerate(chunks):
        y = next((yy for t_end, yy in caption_y if chunk[0]["start"] < t_end), caption_y[-1][1])
        chunk_end = min(chunk[-1]["end"] + 0.35, chunks[ci + 1][0]["start"] if ci + 1 < len(chunks) else duration)
        for wi, w in enumerate(chunk):
            start = w["start"] if wi else max(0.0, w["start"] - 0.05)
            end = chunk[wi + 1]["start"] if wi + 1 < len(chunk) else chunk_end
            start = max(start, last_end)
            if end - start < 0.02:
                continue
            last_end = end
            parts = []
            for j, x in enumerate(chunk):
                token = _esc(censor(x["word"]))
                token = token.upper() if st["upper"] else token
                if j == wi:
                    pop = "\\fscx112\\fscy112\\t(0,90,\\fscx100\\fscy100)" if st["pop"] else ""
                    parts.append(f"{{\\c{st['active']}{pop}}}{token}{{\\c{st['primary']}\\fscx100\\fscy100}}")
                else:
                    parts.append(token)
            events.append(f"Dialogue: 1,{_ts(start)},{_ts(end)},Cap,,0,0,0,,{{\\pos({W // 2},{y})}}{' '.join(parts)}")
    return head + "\n".join(events) + "\n"
