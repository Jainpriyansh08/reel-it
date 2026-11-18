"""Packager: cover.jpg + package.json for edit/v<N>/ (no Claude calls).

package.json is never overwritten: once written, it may hold your edited
caption with a higher caption_version.
"""
from __future__ import annotations

import json
from pathlib import Path

from ..media import cover as cover_mod
from ..util import edit_dir, read_json, write_json

MAX_HASHTAGS = 8


def _read(path: Path, default=None):
    try:
        return read_json(path)
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def banned_list(cfg) -> list[str]:
    return [str(b).lower() for b in (cfg.brand.get("banned_words") or []) if str(b).strip()]


def clean_hashtags(tags, banned: list[str], limit: int = MAX_HASHTAGS) -> list[str]:
    out, seen = [], set()
    for t in tags or []:
        t = str(t).strip().replace(" ", "")
        if not t.strip("#"):
            continue
        t = "#" + t.lstrip("#")
        low = t.lower()
        if low in seen or any(b in low for b in banned):
            continue
        seen.add(low)
        out.append(t)
    return out[:limit]


def full_caption(caption: str, hashtags: list[str]) -> str:
    caption = (caption or "").strip()
    tags = " ".join(hashtags)
    return f"{caption}\n\n{tags}" if caption and tags else (caption or tags)


def package(workdir: Path, version: int, cfg) -> dict:
    workdir = Path(workdir)
    out = edit_dir(workdir, version)
    recipe = _read(out / "edit.json", {}) or {}
    pick = _read(workdir / "scout" / "pick.json", {}) or {}
    existing = _read(out / "package.json")
    hook = (existing or {}).get("hook") or recipe.get("hook_text") or pick.get("hook", "")

    cover = out / "cover.jpg"
    if not cover.exists():
        font = (recipe.get("caption_style") or {}).get("font") or cfg.brand.get("font", "Montserrat")
        cover_mod.make_cover(out / "final.mp4", hook, font, cover)
    if existing is not None:
        return existing

    caption = (pick.get("caption") or "").strip()
    tags = clean_hashtags(pick.get("hashtags"), banned_list(cfg))
    pkg = {
        "caption_version": 1,
        "caption": caption,
        "hashtags": tags,
        "full_caption": full_caption(caption, tags),
        "hook": hook,
        "cover": str(cover.resolve()),
        "source": pick.get("source") or "needs_caption",
    }
    write_json(out / "package.json", pkg)
    return pkg
