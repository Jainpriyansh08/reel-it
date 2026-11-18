"""Loads config.yaml (machine settings) and brand.yaml (your voice and vocab)."""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import yaml

DEFAULT_CONFIG = {
    "inbox": "~/ReelInbox",
    "work": "~/ReelWork",
    "db": "~/ReelWork/reels.db",
    "settle_seconds": 600,
    "max_attempts": 3,
    "transcribe": {
        "model": "large-v3-turbo",
        "language": "en",
        "device": "auto",
        "compute_type": "int8",
    },
    "proxy_height": 720,
    "claude_calls_per_day": 2,
}

DEFAULT_BRAND = {
    "voice": {"adjectives": [], "sample_captions": []},
    "vocab": [],
    "banned_words": [],
    "font": "Montserrat",
}

VIDEO_EXTS = {".mp4", ".mov", ".m4v", ".mkv", ".avi", ".webm"}


def _merge(base: dict, over: dict) -> dict:
    out = dict(base)
    for k, v in (over or {}).items():
        out[k] = _merge(out[k], v) if isinstance(v, dict) and isinstance(out.get(k), dict) else v
    return out


@dataclass
class Config:
    inbox: Path
    work: Path
    db: Path
    settle_seconds: int
    max_attempts: int
    transcribe: dict
    proxy_height: int
    claude_calls_per_day: int
    brand: dict = field(default_factory=lambda: dict(DEFAULT_BRAND))

    @classmethod
    def from_dict(cls, data: dict, brand: dict | None = None) -> "Config":
        d = _merge(DEFAULT_CONFIG, data)
        return cls(
            inbox=Path(d["inbox"]).expanduser(),
            work=Path(d["work"]).expanduser(),
            db=Path(d["db"]).expanduser(),
            settle_seconds=int(d["settle_seconds"]),
            max_attempts=int(d["max_attempts"]),
            transcribe=d["transcribe"],
            proxy_height=int(d["proxy_height"]),
            claude_calls_per_day=int(d["claude_calls_per_day"]),
            brand=_merge(DEFAULT_BRAND, brand or {}),
        )

    @classmethod
    def load(cls, config_path: Path) -> "Config":
        config_path = Path(config_path).expanduser()
        data = yaml.safe_load(config_path.read_text()) if config_path.exists() else {}
        brand_path = config_path.with_name("brand.yaml")
        brand = yaml.safe_load(brand_path.read_text()) if brand_path.exists() else {}
        return cls.from_dict(data or {}, brand or {})
