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
    "reel": {
        "min_seconds": 7,
        "target_seconds": 30,
        "max_seconds": 60,
        "candidates": 10,          # Shot Scout keeps the top N
        "prerender_alternates": 2, # Editor renders next candidates for fast recuts
        "max_recuts_per_day": 3,
    },
    "claude": {
        "command": "claude",
        "model": "",               # empty = your Claude Code default
        "timeout_seconds": 300,
        "fallback": "human",       # human: you pick from top 3 | heuristic: "local draft"
    },
    "telegram": {
        "token_env": "REEL_TELEGRAM_TOKEN",   # bot token is read from this env var
        "token_file": "~/.config/reel/telegram_token",
        "chat_id": 0,              # your chat; `reel bot --whoami` prints it
        "remind_after_hours": 4,
        "expire_after_hours": 20,
        "footage_deadline": "10:00",   # alert if no footage by then
        "heartbeat": "21:00",
    },
    "publish": {
        "mode": "manual",          # manual: bot hands you file + caption | api: Graph API
        "post_time": "18:00",      # local time; api mode posts at/after this
        "api_version": "v24.0",
        "ig_user_id": "",
        "token_env": "REEL_META_TOKEN",
        "token_file": "~/.config/reel/meta_token",
    },
    "analyst": {
        "checkpoints_hours": [24, 72, 168],
        "playbook_weekday": 0,     # Monday
    },
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
    reel: dict = field(default_factory=dict)
    claude: dict = field(default_factory=dict)
    telegram: dict = field(default_factory=dict)
    publish: dict = field(default_factory=dict)
    analyst: dict = field(default_factory=dict)
    brand_path: Path | None = None
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
            reel=d["reel"], claude=d["claude"], telegram=d["telegram"],
            publish=d["publish"], analyst=d["analyst"],
            brand=_merge(DEFAULT_BRAND, brand or {}),
        )

    @classmethod
    def load(cls, config_path: Path) -> "Config":
        config_path = Path(config_path).expanduser()
        data = yaml.safe_load(config_path.read_text()) if config_path.exists() else {}
        brand_path = config_path.with_name("brand.yaml")
        brand = yaml.safe_load(brand_path.read_text()) if brand_path.exists() else {}
        cfg = cls.from_dict(data or {}, brand or {})
        cfg.brand_path = brand_path
        return cfg

    @property
    def playbook_path(self) -> Path:
        return self.work / "playbook.md"


def read_secret(env_name: str, file_path: str) -> str | None:
    """Tokens come from an env var or a chmod-600 file, never from config.yaml."""
    import os
    if os.environ.get(env_name):
        return os.environ[env_name].strip()
    p = Path(file_path).expanduser()
    return p.read_text().strip() if p.exists() else None
