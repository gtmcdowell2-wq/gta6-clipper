"""Inspection crew: dozens of small inspectors, each checking exactly one thing on every finished clip.

They all read the measurements the Clip Profiler took once (table clip_facts), so adding an inspector costs
milliseconds, not another ffmpeg pass. Each writes one verdict (pass / warn / fail) onto the clip's review
card. None of them can post anything; the Gatekeeper reads their verdicts and makes the recommendation.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from statistics import median
from urllib.parse import urlparse

from ..captions import has_slur
from .base import Agent, Ctx

PASS, WARN, FAIL = "pass", "warn", "fail"


class Inspector(Agent):
    crew, lane, interval = "inspect", "inspect", 8
    need_facts = True
    reviews_clips = True
    backup = "If the measurement it needs is missing it records 'skipped' instead of guessing, and the Gatekeeper treats that as a reason for a human look."

    def check(self, ctx: Ctx, clip, facts: dict):
        raise NotImplementedError

    def work(self, ctx: Ctx):
        clip = ctx.unreviewed(need_facts=self.need_facts)
        if not clip:
            return None
        facts = ctx.facts(clip["id"]) if self.need_facts else {}
        try:
            result = self.check(ctx, clip, facts)
        except KeyError as e:  # a measurement this inspector needs wasn't taken
            result = ("skipped", f"measurement {e} is missing")
        verdict, summary, *rest = result
        ctx.review(clip["id"], verdict, summary, details=rest[0] if rest else None)
        return f"Clip {clip['id']}: {verdict}" + (f" ({summary[:120]})" if verdict != PASS else "")


def _verdict(fails: list[str], warns: list[str], ok: str) -> tuple[str, str]:
    if fails:
        return FAIL, "; ".join(fails + warns)
    if warns:
        return WARN, "; ".join(warns)
    return PASS, ok


def _text(clip) -> str:
    return f"{clip['post_title'] or ''} {clip['caption'] or ''} {clip['hook_used'] or ''}"


# ------------------------------------------------------------------ technical

class FormatInspector(Inspector):
    name, title = "format_inspector", "Format Inspector"
    job = "Checks the file is a 1080x1920 H.264 video in yuv420p at 30 or 60 fps: the format every platform accepts without re-encoding it badly."

    def check(self, ctx, clip, f):
        ed = ctx.cfg["editor"]
        fails, warns = [], []
        if (f["width"], f["height"]) != (ed["width"], ed["height"]):
            fails.append(f"resolution is {f['width']}x{f['height']}, not {ed['width']}x{ed['height']}")
        if f.get("vcodec") != "h264":
            fails.append(f"video codec is {f.get('vcodec')}, not H.264")
        if f.get("pix_fmt") not in ("yuv420p", None):
            fails.append(f"pixel format {f.get('pix_fmt')} plays badly on phones (needs yuv420p)")
        if round(f["fps"]) not in (30, 60):
            warns.append(f"unusual frame rate {f['fps']:g}")
        return (*_verdict(fails, warns, f"{f['width']}x{f['height']} H.264 {round(f['fps'])} fps"), {"fps": f["fps"]})


class LengthInspector(Inspector):
    name, title = "length_inspector", "Length Inspector"
    job = "Checks the rendered length matches the plan, stays under YouTube's 3-minute Shorts limit, and flags clips outside the 15-60 second sweet spot."

    def check(self, ctx, clip, f):
        d, fails, warns = f["duration"], [], []
        if abs(d - clip["duration_s"]) > 1.0:
            fails.append(f"length {d:.1f}s does not match the planned {clip['duration_s']:.1f}s")
        if d > 180:
            fails.append("longer than 3 minutes: YouTube won't treat it as a Short")
        elif d > 90:
            warns.append(f"{d:.0f}s is long for TikTok and Reels (15-60s does best)")
        if d < 10:
            warns.append(f"only {d:.0f}s: may feel unfinished")
        return (*_verdict(fails, warns, f"{d:.0f}s, inside the sweet spot" if 15 <= d <= 60 else f"{d:.0f}s"), {"seconds": d})


class LoudnessInspector(Inspector):
    name, title = "loudness_inspector", "Loudness Inspector"
    job = "Measures integrated loudness against the -14 LUFS target the platforms normalize to, and flags wild volume swings."

    def check(self, ctx, clip, f):
        target = ctx.cfg["editor"]["target_lufs"]
        if f.get("lufs") is None:
            return "skipped", "loudness could not be measured"
        off, warns = abs(f["lufs"] - target), []
        if off > 2:
            warns.append(f"loudness {f['lufs']} LUFS is {off:.1f} LU off target {target}")
        if (f.get("lra") or 0) > 20:
            warns.append(f"volume swings a lot (LRA {f['lra']} LU): quiet talk then very loud screams")
        return (*_verdict([], warns, f"{f['lufs']} LUFS, on target"), {"lufs": f["lufs"], "lra": f.get("lra")})


class PeakInspector(Inspector):
    name, title = "peak_inspector", "Peak Inspector"
    job = "Checks the true peak stays below -1 dBTP so screams don't distort after each platform re-encodes the audio."

    def check(self, ctx, clip, f):
        p = f.get("true_peak")
        if p is None:
            return "skipped", "true peak could not be measured"
        if p > 0:
            return FAIL, f"true peak {p} dBTP: the audio clips (distorts)", {"true_peak": p}
        if p > -1.0:
            return WARN, f"true peak {p} dBTP is above -1: may crackle after re-encoding", {"true_peak": p}
        return PASS, f"true peak {p} dBTP", {"true_peak": p}


class SilenceInspector(Inspector):
    name, title = "silence_inspector", "Silence Inspector"
    job = "Finds dead air: a silent first second kills the hook, and long silences make people swipe."

    def check(self, ctx, clip, f):
        warns = []
        for s, e in f.get("silence", []):
            if s < 0.5:
                warns.append("the first second is silent (dead air in the hook)")
            elif e and e - s > 3:
                warns.append(f"{e - s:.0f}s of silence at {s:.0f}s")
        return (*_verdict([], warns[:3], "no dead air"),)


class BlackFrameInspector(Inspector):
    name, title = "black_frame_inspector", "Black Frame Inspector"
    job = "Catches clips that open on a black screen or cut to black in the middle (loading screens, scene transitions)."

    def check(self, ctx, clip, f):
        fails, warns = [], []
        for a, b in f.get("black", []):
            if a < 1.0:
                fails.append("opens on a black screen")
            elif b - a > 1.5:
                warns.append(f"black screen for {b - a:.1f}s at {a:.0f}s")
        return (*_verdict(fails, warns, "no black frames"),)


class FreezeInspector(Inspector):
    name, title = "freeze_inspector", "Freeze Inspector"
    job = "Spots a frozen picture (stream lag, paused game, AFK) lasting 4 seconds or more."

    def check(self, ctx, clip, f):
        warns = [f"picture frozen from {s:.0f}s" + (f" for {e - s:.0f}s" if e else " to the end") for s, e in f.get("freeze", [])]
        return (*_verdict([], warns[:3], "picture keeps moving"),)


class SharpnessInspector(Inspector):
    name, title = "sharpness_inspector", "Sharpness Inspector"
    job = "Measures how sharp the gameplay looks; a blurry or low-resolution source makes a clip look like a re-upload."

    def check(self, ctx, clip, f):
        values = [s["sharpness"] for s in f.get("frames", []) if s.get("sharpness") is not None]
        if not values:
            return "skipped", "no frames sampled"
        m = median(values)
        if m < 25:
            return WARN, f"gameplay looks blurry (sharpness {m:.0f}); source may be low resolution", {"median": m}
        return PASS, f"sharp enough (sharpness {m:.0f})", {"median": m}


class BitrateInspector(Inspector):
    name, title = "bitrate_inspector", "Bitrate Inspector"
    job = "Checks bitrate and file size: too low looks blocky after re-encoding, too big gets refused by Instagram's upload."

    def check(self, ctx, clip, f):
        warns, fails = [], []
        if f.get("bitrate_kbps", 0) and f["bitrate_kbps"] < 2500:
            warns.append(f"bitrate {f['bitrate_kbps']} kbps is low for 1080x1920 (blocky after re-encoding)")
        if f.get("size_mb", 0) > 300:
            fails.append(f"{f['size_mb']:.0f} MB is over Instagram's upload limit")
        elif f.get("size_mb", 0) > 250:
            warns.append(f"{f['size_mb']:.0f} MB is close to Instagram's upload limit")
        return (*_verdict(fails, warns, f"{f.get('bitrate_kbps')} kbps, {f.get('size_mb')} MB"),)


class AudioTrackInspector(Inspector):
    name, title = "audio_track_inspector", "Audio Track Inspector"
    job = "Checks there is an AAC audio track at 44.1 or 48 kHz, mono or stereo: the combination all three apps accept."

    def check(self, ctx, clip, f):
        fails = []
        if not f.get("has_audio"):
            fails.append("no audio track")
        else:
            if f.get("acodec") != "aac":
                fails.append(f"audio codec is {f.get('acodec')}, not AAC")
            if f.get("sample_rate") not in (44100, 48000):
                fails.append(f"audio sample rate {f.get('sample_rate')} Hz")
            if f.get("channels") not in (1, 2):
                fails.append(f"{f.get('channels')} audio channels")
        return (*_verdict(fails, [], f"AAC {f.get('sample_rate')} Hz, {f.get('channels')} ch"),)


# ------------------------------------------------------------------ visual

class FacecamInspector(Inspector):
    name, title = "facecam_inspector", "Facecam Inspector"
    job = "Looks for the streamer's face in the cam pane of every sampled frame; a cam pane with no face usually means the cam was cropped in the wrong place."

    def check(self, ctx, clip, f):
        cams = [s for s in f.get("frames", []) if s.get("layout") in ("split", "fullcam")]
        if not cams:
            return PASS, "gameplay-only layout (no cam pane to check)"
        share = sum(1 for s in cams if s["faces"]) / len(cams)
        if share < 0.5:
            return WARN, f"a face is visible in only {share:.0%} of cam frames: check the cam crop", {"share": round(share, 2)}
        small = [s["face_h"] for s in cams if s.get("face_h")]
        if small and median(small) < 0.12:
            return WARN, "the face is tiny in the cam pane: the crop may include too much background", {"share": round(share, 2)}
        return PASS, f"face visible in {share:.0%} of cam frames", {"share": round(share, 2)}


class SafeZoneInspector(Inspector):
    name, title = "safe_zone_inspector", "Safe Zone Inspector"
    job = "Checks burned-in text stays out of the areas the TikTok, Reels and Shorts buttons and captions cover (top bar, bottom 20%, right edge)."

    TOP, BOTTOM = 130, 1500  # px on a 1920-high frame

    def check(self, ctx, clip, f):
        ass = Path(clip["file_path"]).parent / "captions.ass"
        if not ass.exists():
            return "skipped", "no captions file"
        warns = []
        styles = {}
        for line in ass.read_text(encoding="utf-8").splitlines():
            if line.startswith("Style:"):
                p = [x.strip() for x in line[6:].split(",")]
                styles[p[0]] = {"align": int(p[18]), "margin_v": int(p[21])}
            elif line.startswith("Dialogue:"):
                m = re.search(r"\\pos\((\d+),(\d+)\)", line)
                if m and not self.TOP <= int(m.group(2)) <= self.BOTTOM:
                    warns.append(f"caption at y={m.group(2)} sits under the app's buttons")
                    break
        for name, st in styles.items():
            if name in ("Cap", "Bar"):  # these are placed with \pos, checked above
                continue
            if st["align"] in (7, 8, 9) and st["margin_v"] < self.TOP:
                warns.append(f"{name} text at the very top ({st['margin_v']}px) is covered by the app's top bar")
        return (*_verdict([], warns, "all text inside the safe zone"),)


class CutsceneDetector(Inspector):
    name, title = "cutscene_detector", "Cutscene Detector"
    job = "Spots story cutscenes in the source footage by their letterbox bars. Cutscene-heavy clips spoil the story, and endings and cutscenes are what Rockstar files takedowns against."

    def check(self, ctx, clip, f):
        share = f.get("letterbox_share")
        if share is None:
            return "skipped", "source footage was not available to check"
        if share >= 0.5:
            return WARN, f"{share:.0%} of the clip looks like a story cutscene: spoiler and takedown risk", {"share": share}
        if share > 0:
            return WARN, f"part of the clip ({share:.0%}) looks like a cutscene: check it doesn't spoil the story", {"share": share}
        return PASS, "gameplay, not a cutscene", {"share": share}


class DuplicateDetector(Inspector):
    name, title = "duplicate_detector", "Duplicate Detector"
    job = "Compares picture fingerprints with every recent clip, so the same moment cut twice (overlapping chunks, two streamers in one lobby) never gets posted twice."

    def check(self, ctx, clip, f):
        from ..profile import hamming
        mine = [int(s["dhash"]) for s in f.get("frames", [])[1:]]
        if not mine:
            return "skipped", "no fingerprints"
        best, worst_id = 0, None
        for row in ctx.conn.execute("SELECT k.clip_id, k.facts FROM clip_facts k JOIN clips c ON c.id=k.clip_id"
                                    " WHERE k.clip_id != ? AND c.status != 'rejected' AND k.profiled_at >= datetime('now','-14 days')",
                                    (clip["id"],)):
            theirs = [int(s["dhash"]) for s in json.loads(row["facts"]).get("frames", [])[1:]]
            if not theirs:
                continue
            matches = sum(1 for h in mine if min(hamming(h, t) for t in theirs) <= 5)
            if matches > best:
                best, worst_id = matches, row["clip_id"]
        if best >= 6:
            return FAIL, f"{best} of {len(mine)} frames match clip {worst_id}: it's the same moment", {"clip": worst_id, "matches": best}
        if best >= 3:
            return WARN, f"{best} frames look like clip {worst_id}: check it isn't the same moment", {"clip": worst_id, "matches": best}
        return PASS, "no recent clip looks like this one"


class ThumbnailInspector(Inspector):
    name, title = "thumbnail_inspector", "Thumbnail Inspector"
    job = "Checks the cover frame is bright, sharp and shows a face: the still frame people see in grids and search."

    def check(self, ctx, clip, f):
        t = f.get("thumb")
        if not t:
            return WARN, "no thumbnail was made"
        warns = []
        if t["brightness"] < 35:
            warns.append("cover frame is very dark")
        if t["brightness"] > 220:
            warns.append("cover frame is washed out")
        if t["sharpness"] < 40:
            warns.append("cover frame is blurry")
        if not t["faces"] and set(f.get("layouts", [])) & {"split", "fullcam"}:
            warns.append("no face on the cover frame")
        return (*_verdict([], warns, "cover frame is bright, sharp and has a face"), t)


class OpeningFrameInspector(Inspector):
    name, title = "opening_frame_inspector", "Opening Frame Inspector"
    job = "Judges the first second the way a scrolling viewer does: is there light and movement, or a still, dark frame?"

    def check(self, ctx, clip, f):
        frames = f.get("frames", [])
        if not frames:
            return "skipped", "no frames"
        warns = []
        if frames[0]["brightness"] < 20:
            warns.append("the first frame is almost black")
        motion = f.get("opening_motion")
        if motion is not None and motion < 3:
            warns.append("almost nothing moves in the opening second")
        return (*_verdict([], warns, "opening frame is lit and moving"), {"motion": motion})


# ------------------------------------------------------------------ captions & text

class CaptionTimingInspector(Inspector):
    name, title = "caption_timing_inspector", "Caption Timing Inspector"
    job = "Checks on-screen captions never overlap and no caption flashes by too fast to read."

    def check(self, ctx, clip, f):
        c = f["captions"]
        if not c["events"]:
            return WARN, "no captions (no speech was transcribed)"
        warns = []
        if c["overlaps"]:
            warns.append(f"{c['overlaps']} caption(s) overlap the next one")
        if c["shortest"] is not None and c["shortest"] < 0.15:
            warns.append(f"a caption is on screen for only {c['shortest']}s")
        return (*_verdict([], warns, f"{c['events']} caption lines, none overlapping"),)


class CaptionReadabilityInspector(Inspector):
    name, title = "caption_readability_inspector", "Caption Readability Inspector"
    job = "Checks captions are short enough to read at a glance on a phone: at most about 18 characters per line and a calm word rate."

    def check(self, ctx, clip, f):
        c = f["captions"]
        if not c["events"]:
            return "skipped", "no captions"
        warns = []
        if c["max_chars"] > 20:
            warns.append(f"a caption line has {c['max_chars']} characters (hard to read)")
        if c["words_per_second"] > 4.5:
            warns.append(f"captions go by at {c['words_per_second']} words/second")
        return (*_verdict([], warns, f"max {c['max_chars']} chars per line, {c['words_per_second']} words/s"),)


class HookTimingInspector(Inspector):
    name, title = "hook_timing_inspector", "Hook Timing Inspector"
    job = "Checks something is said and a hook is on screen within the first two seconds, where most viewers decide to stay or swipe."

    def check(self, ctx, clip, f):
        warns = []
        first = f["speech"]["first_word"]
        if first is None:
            warns.append("nobody speaks in this clip")
        elif first > 2.5:
            warns.append(f"the first words come {first:.1f}s in")
        if f["captions"].get("hook_seconds", 0) < 2:
            warns.append("no on-screen hook for the first 2 seconds")
        return (*_verdict([], warns, f"speech at {first}s and a {f['captions'].get('hook_seconds')}s hook banner"),)


class HookTextInspector(Inspector):
    name, title = "hook_text_inspector", "Hook Text Inspector"
    job = "Keeps the on-screen hook to seven words or fewer, readable, and free of hashtags or empty clickbait."

    BAIT = ("you won't believe", "you wont believe", "gone wrong", "must watch", "not clickbait")

    def check(self, ctx, clip, f):
        hook = (clip["hook_used"] or "").strip()
        if not hook:
            return WARN, "no hook text"
        warns = []
        if len(hook.split()) > 7:
            warns.append(f"hook is {len(hook.split())} words (7 max reads in time)")
        if "#" in hook or "@" in hook:
            warns.append("hook contains a hashtag or handle")
        if any(b in hook.lower() for b in self.BAIT):
            warns.append("hook uses stock clickbait wording")
        return (*_verdict([], warns, f"hook '{hook}' is tight"),)


class TitleInspector(Inspector):
    name, title = "title_inspector", "Title Inspector"
    job = "Checks the post title fits YouTube's 100-character limit, reads well under 70 characters, and isn't shouting in all caps."

    def check(self, ctx, clip, f):
        t = (clip["post_title"] or "").strip()
        fails, warns = [], []
        if not t:
            fails.append("no title")
        if len(t) > 100:
            fails.append(f"title is {len(t)} characters (YouTube's limit is 100)")
        elif len(t) > 70:
            warns.append(f"title is {len(t)} characters; under 70 shows in full on phones")
        letters = [ch for ch in t if ch.isalpha()]
        if letters and sum(ch.isupper() for ch in letters) / len(letters) > 0.7 and len(letters) > 12:
            warns.append("title is mostly capital letters")
        return (*_verdict(fails, warns, f"{len(t)}-character title"),)


class CaptionTextInspector(Inspector):
    name, title = "caption_text_inspector", "Caption Text Inspector"
    job = "Checks the post caption fits every platform: 2,200 characters, no more than 5 hashtags (Instagram's limit since December 2025), at most 20 @mentions, no repeated tags."

    def check(self, ctx, clip, f):
        cap = clip["caption"] or ""
        tags = [t.lower() for t in re.findall(r"#\w+", cap)]
        fails, warns = [], []
        if len(cap) > 2200:
            fails.append(f"caption is {len(cap)} characters (2,200 max)")
        if len(tags) > 5:
            warns.append(f"{len(tags)} hashtags: Instagram allows 5, so the Metadata Writer will trim it there")
        if len(set(tags)) < len(tags):
            warns.append("a hashtag is repeated")
        if len(re.findall(r"@\w+", cap)) > 20:
            fails.append("more than 20 @mentions")
        return (*_verdict(fails, warns, f"{len(cap)} characters, {len(tags)} hashtags"), {"tags": tags})


class HashtagInspector(Inspector):
    name, title = "hashtag_inspector", "Hashtag Strategist"
    job = "Makes sure every caption carries the game tag and the streamer's tag, the two tags people actually search during launch week."

    def check(self, ctx, clip, f):
        source = ctx.source_for_clip(clip)
        tags = {t.lower() for t in re.findall(r"#\w+", clip["caption"] or "")}
        missing = []
        if not tags & {"#gta6", "#gtavi", "#gta"}:
            missing.append("#GTA6")
        handle = source["key"].split(":", 1)[1].lower()
        name_tag = "#" + re.sub(r"\W", "", source["name"]).lower()
        if f"#{handle}" not in tags and name_tag not in tags:
            missing.append(f"#{re.sub(r'\W', '', source['name'])}")
        if missing:
            return WARN, "missing " + " and ".join(missing), {"suggest": missing}
        return PASS, "game and streamer tags present"


class LanguageInspector(Inspector):
    name, title = "language_inspector", "Language Inspector"
    job = "Listens to the transcript for slurs: captions are masked automatically, but an audible slur can limit reach or break a campaign brief."

    def check(self, ctx, clip, f):
        text = f["speech"]["text"]
        if has_slur(text):
            return WARN, "a slur is audible in the audio (masked in captions): TikTok and Instagram may limit reach"
        swears = len(re.findall(r"\bf+u+c+k|\bsh[i1]t\b", text, re.I))
        if swears > 8:
            return WARN, f"{swears} swear words: fine for most audiences, but check the campaign brief"
        return PASS, "no slurs in the audio"


class CreditInspector(Inspector):
    name, title = "credit_inspector", "Credit Inspector"
    job = "Checks the streamer is credited by name or @handle in the title or caption, which every clipping campaign requires."

    def check(self, ctx, clip, f):
        source = ctx.source_for_clip(clip)
        text = _text(clip).lower()
        handle = source["key"].split(":", 1)[1].lower()
        if source["name"].lower() in text or handle in text:
            return PASS, f"{source['name']} is credited"
        return WARN, "the streamer isn't credited in the title or caption"


# ------------------------------------------------------------------ policy & rights

class PermissionGuard(Inspector):
    name, title = "permission_guard", "Permission Guard"
    job = "Checks the streamer's permission level. Clips from 'unknown' sources can be reviewed but are blocked from posting until you confirm permission in sources.yaml."
    need_facts = False

    def check(self, ctx, clip, f):
        source = ctx.source_for_clip(clip)
        if not ctx.cfg.can_post(source["permission"]):
            return WARN, f"{source['name']} is '{source['permission']}': this clip can never be posted until you confirm permission", {"blocked": True}
        return PASS, f"{source['name']} is '{source['permission']}'" + (f" ({source['campaign']})" if source["campaign"] else "")


class LeakGuard(Inspector):
    name, title = "leak_guard", "Leak Guard"
    job = "Rejects anything whose title, caption, stream title or speech mentions leaks, early copies or stolen builds (rule: never use leaked footage)."

    def check(self, ctx, clip, f):
        vod = ctx.conn.execute("SELECT title, description FROM vods WHERE id=?", (clip["vod_id"],)).fetchone()
        blobs = {"title/caption/hook": _text(clip), "stream title": f"{vod['title']} {vod['description'] or ''}",
                 "speech": f.get("speech", {}).get("text", "")}
        hit = [where for where, text in blobs.items() if ctx.cfg.is_leak(text)]
        if hit:
            return FAIL, "leak wording in the " + ", ".join(hit)
        return PASS, "no leak wording anywhere"


class SpoilerGuard(Inspector):
    name, title = "spoiler_guard", "Spoiler Guard"
    job = "Flags story spoilers from the picker's flag and spoiler words in the title or speech; Rockstar targets endings and major reveals, and fans report spoiler clips."

    def check(self, ctx, clip, f):
        words = ctx.cfg["safety"]["spoiler_keywords"]
        spoken = f.get("speech", {}).get("text", "").lower()
        hits = sorted({k for k in words if k.lower() in spoken or k.lower() in _text(clip).lower()})
        if clip["spoiler_risk"] or hits:
            return WARN, "possible story spoiler" + (f": {', '.join(hits)}" if hits else " (flagged by the Moment Picker)"), {"hits": hits}
        return PASS, "no spoiler signs"


class ViolenceLeadGuard(Inspector):
    name, title = "violence_lead_guard", "Violence Lead Guard"
    job = "Keeps graphic violence out of the first 7 seconds (the rule that protects reach and ad suitability), using the speech in the opening seconds."

    WORDS = re.compile(r"\b(blood|bloody|gore|headshot|decapitat\w*|dismember\w*|beheaded|killed (him|her|them)|shot (him|her|them))\b", re.I)

    def check(self, ctx, clip, f):
        from ..captions import remap_words
        lead = ctx.cfg["safety"].get("graphic_violence_lead_seconds", 7)
        vod = ctx.conn.execute("SELECT transcript_path FROM vods WHERE id=?", (clip["vod_id"],)).fetchone()
        words = []
        if vod and vod["transcript_path"] and Path(vod["transcript_path"]).exists():
            t = json.loads(Path(vod["transcript_path"]).read_text(encoding="utf-8"))
            words = remap_words([w for s in t["segments"] for w in s["words"]], json.loads(clip["segments"]))
        opening = " ".join(w["word"] for w in words if w["start"] < lead)
        if self.WORDS.search(opening) or self.WORDS.search(clip["hook_used"] or ""):
            return WARN, f"violent wording in the first {lead}s: check the opening isn't graphic"
        return PASS, f"no violent wording in the first {lead}s"


class DisclosureGuard(Inspector):
    name, title = "disclosure_guard", "Disclosure Guard"
    job = "For paid campaign clips, checks the paid disclosure is in the caption and on screen at the start, as FTC rules and campaign terms require."

    def check(self, ctx, clip, f):
        source = ctx.source_for_clip(clip)
        if source["permission"] != "campaign":
            return PASS, "not a paid campaign clip"
        disclosure = ctx.brief(source).get("disclosure") or "#ad"
        fails = []
        if disclosure.lower() not in (clip["caption"] or "").lower() and "#ad" not in (clip["caption"] or "").lower():
            fails.append(f"caption is missing the paid disclosure ({disclosure})")
        if not f["captions"].get("has_disclosure"):
            fails.append("no on-screen paid disclosure at the start: re-render")
        return (*_verdict(fails, [], f"disclosed as '{disclosure}' in the caption and on screen"),)


class CampaignRulesGuard(Inspector):
    name, title = "campaign_rules_guard", "Campaign Rules Guard"
    job = "Checks each clip against its campaign brief: required text and tags, banned words, length limits and the platforms the campaign pays for."

    def check(self, ctx, clip, f):
        source = ctx.source_for_clip(clip)
        brief = ctx.brief(source)
        if source["permission"] != "campaign" or not brief:
            return PASS, "no campaign brief applies"
        cap, text, speech = (clip["caption"] or "").lower(), _text(clip).lower(), f.get("speech", {}).get("text", "").lower()
        fails, warns = [], []
        for item in brief.get("required_text", []) + brief.get("required_tags", []):
            if str(item).lower() not in cap:
                fails.append(f"brief requires '{item}' in the caption")
        for item in brief.get("banned", []):
            if str(item).lower() in text or str(item).lower() in speech:
                fails.append(f"brief bans '{item}'")
        d = f["duration"]
        if brief.get("min_seconds") and d < brief["min_seconds"]:
            fails.append(f"brief wants at least {brief['min_seconds']}s")
        if brief.get("max_seconds") and d > brief["max_seconds"]:
            fails.append(f"brief allows at most {brief['max_seconds']}s")
        return (*_verdict(fails, warns, f"meets the {brief.get('name', 'campaign')} brief"), {"brief": brief.get("name")})


class MusicGuard(Inspector):
    name, title = "music_guard", "Music Guard"
    job = "Listens for a steady musical beat (in-game radio, stream music). Licensed songs trigger YouTube Content ID claims and muted audio on TikTok/Instagram, and many briefs say to mute the radio."

    def check(self, ctx, clip, f):
        secs = f.get("music_seconds", 0)
        spans = ", ".join(f"{a:.0f}-{b:.0f}s" for a, b, _ in f.get("music_windows", [])[:3])
        if secs >= 15:
            return WARN, f"music likely playing for {secs:.0f}s ({spans}): Content ID / mute risk", {"windows": f.get("music_windows")}
        if secs >= 6:
            return WARN, f"possible music at {spans}: listen before approving", {"windows": f.get("music_windows")}
        return PASS, "no steady music beat detected"


class SourceAuthenticityGuard(Inspector):
    name, title = "source_authenticity_guard", "Source Authenticity Guard"
    job = "Confirms the footage came from the streamer's own channel on the expected site (never a re-upload, compilation or AI video)."
    need_facts = False

    HOSTS = {"twitch": ("twitch.tv",), "kick": ("kick.com",), "youtube": ("youtube.com", "youtu.be")}
    BAD = ("reupload", "re-upload", "compilation", "ai generated", "ai-generated", "fan made", "fanmade", "concept trailer")

    def check(self, ctx, clip, f):
        source = ctx.source_for_clip(clip)
        vod = ctx.conn.execute("SELECT url, title FROM vods WHERE id=?", (clip["vod_id"],)).fetchone()
        host = urlparse(vod["url"]).netloc.lower()
        if not any(host.endswith(h) for h in self.HOSTS.get(source["platform"], ())):
            return FAIL, f"footage came from {host}, not {source['platform']}"
        if any(b in (vod["title"] or "").lower() for b in self.BAD):
            return FAIL, "the stream title suggests a re-upload, compilation or AI video"
        return PASS, f"from {source['name']}'s own {source['platform']} channel"


class GameCheck(Inspector):
    name, title = "game_check", "Game Check"
    job = "Confirms the moment is GTA 6 itself (stream chapter or title), not another game the streamer played that day."
    need_facts = False

    def check(self, ctx, clip, f):
        vod = ctx.conn.execute("SELECT title, video_path, transcript_path, chunk_start_s FROM vods WHERE id=?", (clip["vod_id"],)).fetchone()
        patterns = [p.lower() for p in ctx.cfg["watcher"]["game_patterns"]]
        title = (vod["title"] or "").lower()
        chapters = []
        folder = vod["video_path"] or vod["transcript_path"]
        meta = Path(folder).parent / "meta.json" if folder else None
        if meta and meta.exists():
            try:
                chapters = json.loads(meta.read_text(encoding="utf-8")).get("chapters") or []
            except ValueError:
                chapters = []
        at = (vod["chunk_start_s"] or 0) + clip["start_s"]  # chapter times are positions in the whole VOD
        for ch in chapters:
            if ch.get("start", ch.get("start_time", 0)) <= at < ch.get("end", ch.get("end_time", 1e12)):
                name = (ch.get("title") or "").lower()
                if any(p in name for p in patterns):
                    return PASS, f"stream chapter says {ch.get('title')}"
                return WARN, f"stream chapter at this point is '{ch.get('title')}', not GTA 6"
        if any(p in title for p in patterns):
            return PASS, "stream title is about GTA 6"
        return WARN, "nothing says this is GTA 6 footage (stream title and chapters don't mention it)"


# ------------------------------------------------------------------ originality

class TitleOriginality(Inspector):
    name, title = "title_originality", "Title Originality"
    job = "Guards against YouTube's inauthentic-content rule by flagging titles that are near-copies of another clip from the past week."
    need_facts = False

    @staticmethod
    def tokens(text: str) -> set[str]:
        return {w for w in re.findall(r"[a-z0-9']+", (text or "").lower()) if len(w) > 2}

    def check(self, ctx, clip, f):
        mine = self.tokens(clip["post_title"])
        for o in ctx.conn.execute("SELECT id, post_title FROM clips WHERE id != ? AND status != 'rejected' AND created_at >= datetime('now','-7 day')", (clip["id"],)):
            theirs = self.tokens(o["post_title"])
            if mine and theirs and len(mine & theirs) / len(mine | theirs) > 0.7:
                return WARN, f"title is very close to clip {o['id']}"
        return PASS, "title is distinct"


class HookOriginality(Inspector):
    name, title = "hook_originality", "Hook Originality"
    job = "Flags an on-screen hook that another recent clip already used word for word."
    need_facts = False

    def check(self, ctx, clip, f):
        hook = (clip["hook_used"] or "").strip().lower()
        row = ctx.conn.execute("SELECT id FROM clips WHERE id != ? AND status != 'rejected' AND lower(trim(hook_used)) = ?"
                               " AND created_at >= datetime('now','-7 day')", (clip["id"], hook)).fetchone()
        return (WARN, f"clip {row['id']} uses the same hook") if row and hook else (PASS, "hook is distinct")


class StyleRotationGuard(Inspector):
    name, title = "style_rotation_guard", "Style Rotation Guard"
    job = "Makes sure consecutive uploads don't share one caption template, so the channel doesn't look mass-produced."
    need_facts = False

    def check(self, ctx, clip, f):
        prev = ctx.conn.execute("SELECT caption_style FROM clips WHERE id < ? AND status != 'rejected' ORDER BY id DESC LIMIT 2", (clip["id"],)).fetchall()
        if len(prev) == 2 and all(p["caption_style"] == clip["caption_style"] for p in prev):
            return WARN, "third clip in a row with the same caption style"
        return PASS, f"style {clip['caption_style']} rotates with its neighbours"


class StretchGuard(Inspector):
    name, title = "stretch_guard", "Stretch Guard"
    job = "Flags when too many clips come from the same 10 minutes of a stream, which reads as spam to both viewers and the platforms."
    need_facts = False

    def check(self, ctx, clip, f):
        n = ctx.conn.execute("SELECT count(*) FROM clips WHERE id != ? AND vod_id=? AND status != 'rejected' AND abs(start_s - ?) < 600",
                             (clip["id"], clip["vod_id"], clip["start_s"])).fetchone()[0]
        return (WARN, f"{n + 1} clips from the same 10 minutes of stream") if n >= 3 else (PASS, "spread across the stream")


class CadenceGuard(Inspector):
    name, title = "cadence_guard", "Cadence Guard"
    job = "Watches how many clips of one streamer were approved today; a feed that is one creator all day looks like a re-upload channel."
    need_facts = False

    def check(self, ctx, clip, f):
        source = ctx.source_for_clip(clip)
        n = ctx.conn.execute("SELECT count(*) FROM clips c JOIN vods v ON v.id=c.vod_id WHERE v.source_id=? AND c.status='approved'"
                             " AND c.reviewed_at >= datetime('now','-1 day')", (source["id"],)).fetchone()[0]
        if n >= 12:
            return WARN, f"{n} clips of {source['name']} already approved in the last 24h"
        return PASS, f"{n} clips of {source['name']} approved in the last 24h"


INSPECTORS = [FormatInspector, LengthInspector, LoudnessInspector, PeakInspector, SilenceInspector, BlackFrameInspector,
              FreezeInspector, SharpnessInspector, BitrateInspector, AudioTrackInspector,
              FacecamInspector, SafeZoneInspector, CutsceneDetector, DuplicateDetector, ThumbnailInspector, OpeningFrameInspector,
              CaptionTimingInspector, CaptionReadabilityInspector, HookTimingInspector, HookTextInspector, TitleInspector,
              CaptionTextInspector, HashtagInspector, LanguageInspector, CreditInspector,
              PermissionGuard, LeakGuard, SpoilerGuard, ViolenceLeadGuard, DisclosureGuard, CampaignRulesGuard, MusicGuard,
              SourceAuthenticityGuard, GameCheck,
              TitleOriginality, HookOriginality, StyleRotationGuard, StretchGuard, CadenceGuard]
