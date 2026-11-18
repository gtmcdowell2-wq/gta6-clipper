"""Loads config.yaml, sources.yaml and .env, and validates the parts the safety rules depend on."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent

PERMISSIONS = ("campaign", "public_ok", "unknown")
PLATFORMS = ("twitch", "kick", "youtube")
LAYOUTS = ("auto", "split", "gameplay")


class ConfigError(ValueError):
    pass


@dataclass
class Source:
    name: str
    handle: str
    platform: str
    url: str
    permission: str = "unknown"
    layout: str = "auto"
    cam: list[int] | None = None
    campaign: str | dict | None = None   # a name, or the campaign brief as a mapping (see sources.yaml)
    caption_suffix: str | None = None  # campaign-required credit, hashtags or paid disclosure
    notes: str | None = None
    live_marathon: bool = False

    @property
    def key(self) -> str:
        return f"{self.platform}:{self.handle}"

    @property
    def campaign_name(self) -> str | None:
        if isinstance(self.campaign, dict):
            return str(self.campaign.get("name") or "campaign")
        return self.campaign

    @property
    def brief(self) -> dict:
        """The campaign brief with defaults filled in. Empty when there is no campaign."""
        if not self.campaign:
            return {}
        b = dict(self.campaign) if isinstance(self.campaign, dict) else {"name": self.campaign}
        b.setdefault("submit_within_minutes", 30)   # Content Rewards: submit within 30 min of posting
        b.setdefault("platforms", ["youtube", "tiktok", "instagram"])
        b.setdefault("required_text", [])
        b.setdefault("required_tags", [])
        b.setdefault("banned", [])
        b.setdefault("disclosure", "#ad")             # FTC: paid clips say so, at the start of the clip
        return b


@dataclass
class Config:
    raw: dict[str, Any]
    sources: list[Source] = field(default_factory=list)

    def __getitem__(self, section: str) -> dict[str, Any]:
        return self.raw[section]

    def get(self, section: str, default=None):
        return self.raw.get(section, default if default is not None else {})

    def source(self, key: str) -> Source | None:
        return next((s for s in self.sources if s.key == key), None)

    @property
    def data_dir(self) -> Path:
        return _abs(self.raw["paths"]["data_dir"])

    @property
    def db_path(self) -> Path:
        return _abs(self.raw["paths"]["db"])

    def can_post(self, permission: str) -> bool:
        """The one place that decides whether a permission level may be posted."""
        return permission != "unknown" and permission in self.raw["safety"]["postable_permissions"]

    def is_leak(self, *texts: str | None) -> bool:
        blob = " ".join(t for t in texts if t).lower()
        return any(k.lower() in blob for k in self.raw["safety"]["leak_keywords"])


def _abs(p: str) -> Path:
    path = Path(p)
    return path if path.is_absolute() else ROOT / path


def _load_yaml(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise ConfigError(f"missing {path.name} (expected at {path})")
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(data, dict):
        raise ConfigError(f"{path.name} must be a mapping at the top level")
    return data


REQUIRED_SECTIONS = ("paths", "download", "transcribe", "signals", "selector", "editor",
                     "safety", "watcher", "posting", "review_app")


def load_sources(path: Path) -> list[Source]:
    entries = _load_yaml(path).get("sources") or []
    sources, seen = [], set()
    for i, e in enumerate(entries):
        where = f"sources.yaml entry {i + 1} ({e.get('name', '?')})"
        for req in ("name", "handle", "platform", "url"):
            if not e.get(req):
                raise ConfigError(f"{where}: missing '{req}'")
        try:
            s = Source(**e)
        except TypeError as err:
            raise ConfigError(f"{where}: {err}") from None
        if s.platform not in PLATFORMS:
            raise ConfigError(f"{where}: platform must be one of {PLATFORMS}")
        if s.permission not in PERMISSIONS:
            raise ConfigError(f"{where}: permission must be one of {PERMISSIONS}")
        if s.layout not in LAYOUTS:
            raise ConfigError(f"{where}: layout must be one of {LAYOUTS}")
        if s.cam is not None and (len(s.cam) != 4 or not all(isinstance(v, int) for v in s.cam)):
            raise ConfigError(f"{where}: cam must be [x, y, w, h] integers")
        if s.campaign is not None and not isinstance(s.campaign, (str, dict)):
            raise ConfigError(f"{where}: campaign must be a name or a mapping")
        if isinstance(s.campaign, dict):
            for k in ("cpm", "cap_per_clip", "budget", "submit_within_minutes", "min_seconds", "max_seconds"):
                if k in s.campaign and s.campaign[k] is not None and not isinstance(s.campaign[k], (int, float)):
                    raise ConfigError(f"{where}: campaign.{k} must be a number")
            for k in ("platforms", "required_text", "required_tags", "banned"):
                if k in s.campaign and not isinstance(s.campaign[k], list):
                    raise ConfigError(f"{where}: campaign.{k} must be a list")
        if s.key in seen:
            raise ConfigError(f"{where}: duplicate source {s.key}")
        seen.add(s.key)
        sources.append(s)
    return sources


def load_config(config_path: Path | None = None, sources_path: Path | None = None) -> Config:
    load_dotenv(ROOT / ".env")
    raw = _load_yaml(config_path or ROOT / "config.yaml")
    missing = [s for s in REQUIRED_SECTIONS if s not in raw]
    if missing:
        raise ConfigError(f"config.yaml is missing sections: {', '.join(missing)}")

    safety = raw["safety"]
    bad = [p for p in safety["postable_permissions"] if p not in PERMISSIONS]
    if bad:
        raise ConfigError(f"safety.postable_permissions has unknown values: {bad}")
    if "unknown" in safety["postable_permissions"]:
        raise ConfigError("safety.postable_permissions must not include 'unknown'")
    sel = raw["selector"]
    if not 0 < sel["min_seconds"] < sel["max_seconds"]:
        raise ConfigError("selector.min_seconds must be below selector.max_seconds")
    thr = safety.get("auto_approve_threshold")
    if thr is not None and not 0 <= thr <= 10:
        raise ConfigError("safety.auto_approve_threshold must be null or 0-10")

    return Config(raw=raw, sources=load_sources(sources_path or ROOT / "sources.yaml"))


def secret(name: str) -> str | None:
    return os.environ.get(name) or None
