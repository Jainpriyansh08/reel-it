"""QA: technical and brand checks on edit/v<N>/ -> qa.json.

EDITED -> QA_PASSED (pass/warn) | QA_FAILED (any block).
"""
from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

from .. import db
from ..media import captions
from ..states import State
from ..util import edit_dir, read_json, write_json
from . import packager

MAX_FINAL_BYTES = 300 * 1024 * 1024
MAX_PREVIEW_BYTES = 45 * 1024 * 1024
_WORD = re.compile(r"[A-Za-z0-9][A-Za-z0-9'’-]*")


def _read(path: Path, default=None):
    try:
        return read_json(path)
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def ffprobe(path: Path) -> dict:
    res = subprocess.run(["ffprobe", "-v", "error", "-print_format", "json",
                          "-show_format", "-show_streams", str(path)],
                         capture_output=True, text=True)
    if res.returncode != 0:
        raise RuntimeError(f"ffprobe failed: {res.stderr.strip()[-300:]}")
    return json.loads(res.stdout)


def analyse(path: Path, has_audio: bool) -> dict:
    """One decode pass: blackdetect + freezedetect (+ volumedetect)."""
    cmd = ["ffmpeg", "-hide_banner", "-nostats", "-i", str(path),
           "-vf", "blackdetect=d=0.1:pix_th=0.10,freezedetect=n=-60dB:d=2"]
    cmd += ["-af", "volumedetect"] if has_audio else ["-an"]
    cmd += ["-f", "null", "-"]
    err = subprocess.run(cmd, capture_output=True, text=True).stderr
    black = [float(m) for m in re.findall(r"black_duration:\s*([\d.]+)", err)]
    freezes = [float(m) for m in re.findall(r"freeze_duration:\s*([\d.]+)", err)]
    starts = re.findall(r"freeze_start:\s*([\d.]+)", err)
    if len(starts) > len(freezes):        # frozen until the end: no freeze_end line
        dur = re.search(r"time=(\d+):(\d+):([\d.]+)", err)
        dur = re.search(r"Duration: (\d+):(\d+):([\d.]+)", err) or dur
        total = int(dur.group(1)) * 3600 + int(dur.group(2)) * 60 + float(dur.group(3)) if dur else 0
        freezes.append(max(0.0, total - float(starts[-1])))
    mx = re.search(r"max_volume:\s*(-?[\d.]+|-inf) dB", err)
    max_volume = None
    if mx:
        max_volume = float("-inf") if mx.group(1) == "-inf" else float(mx.group(1))
    return {"black": black, "freezes": freezes, "max_volume": max_volume}


def _contains_word(text: str, word: str) -> bool:
    return re.search(r"(?<![A-Za-z0-9])" + re.escape(word) + r"(?![A-Za-z0-9])", text, re.I) is not None


def _norm(w: str) -> str:
    return re.sub(r"[^a-z0-9']", "", w.lower().replace("’", "'"))


def check(workdir: Path, version: int, cfg) -> dict:
    workdir = Path(workdir)
    out = edit_dir(workdir, version)
    checks: list[dict] = []

    def add(name, status, detail=""):
        checks.append({"name": name, "status": status, "detail": str(detail)})

    final, preview = out / "final.mp4", out / "preview.mp4"
    recipe = _read(out / "edit.json", {}) or {}
    pkg = _read(out / "package.json", {}) or {}

    if not final.exists():
        add("final_exists", "block", "final.mp4 missing")
    else:
        info = ffprobe(final)
        streams = info.get("streams", [])
        v = next((s for s in streams if s.get("codec_type") == "video"), {})
        a = next((s for s in streams if s.get("codec_type") == "audio"), None)
        w, h = v.get("width"), v.get("height")
        add("resolution", "pass" if (w, h) == (1080, 1920) else "block", f"{w}x{h}")
        dur = float(info.get("format", {}).get("duration") or 0)
        add("duration", "pass" if 3 <= dur <= 90 else "block", f"{dur:.2f}s")
        vc, ac = v.get("codec_name"), (a or {}).get("codec_name")
        add("codecs", "pass" if vc == "h264" and ac == "aac" else "block", f"{vc}/{ac}")
        size = final.stat().st_size
        add("file_size", "pass" if size < MAX_FINAL_BYTES else "block", f"{size / 1e6:.1f} MB")

        an = analyse(final, a is not None)
        total_black = sum(an["black"])
        if dur and total_black / dur > 0.30:
            add("black_frames", "block", f"{total_black:.1f}s of {dur:.1f}s black")
        elif any(b > 1.0 for b in an["black"]):
            add("black_frames", "warn", f"black segment of {max(an['black']):.1f}s")
        else:
            add("black_frames", "pass", f"{total_black:.1f}s")
        if an["freezes"]:
            add("frozen_video", "warn", f"frozen for {max(an['freezes']):.1f}s")
        else:
            add("frozen_video", "pass")
        if a is None:
            add("audio", "warn", "no audio track")
        elif an["max_volume"] is None or an["max_volume"] < -40:
            add("audio", "warn", f"silent (max {an['max_volume']} dB)")
        else:
            add("audio", "pass", f"max {an['max_volume']} dB")

    if not preview.exists():
        add("preview_size", "block", "preview.mp4 missing")
    else:
        ps = preview.stat().st_size
        add("preview_size", "pass" if ps < MAX_PREVIEW_BYTES else "block", f"{ps / 1e6:.1f} MB")

    caption = pkg.get("full_caption") or pkg.get("caption") or ""
    tags = pkg.get("hashtags") or []
    n_tags = max(len(tags), len(re.findall(r"#\w+", caption)))
    ok = len(caption) <= 2200 and n_tags <= 30
    add("caption_length", "pass" if ok else "block", f"{len(caption)} chars, {n_tags} hashtags")

    # banned words anywhere the audience will read them
    banned = packager.banned_list(cfg)
    ass_path = out / "captions.ass"
    burned = " ".join(captions.burned_words(ass_path.read_text())) if ass_path.exists() else ""
    places = {"caption": caption, "hook": pkg.get("hook") or recipe.get("hook_text") or "",
              "burned_captions": burned}
    hits = []
    for b in banned:
        for where, text in places.items():
            if _contains_word(text, b):
                hits.append(f"{b} in {where}")
        if any(b in str(t).lower() for t in tags):
            hits.append(f"{b} in hashtags")
    add("banned_words", "block" if hits else "pass", "; ".join(hits))

    # spelling: caption words the transcriber was unsure about and that aren't brand vocab
    vocab = {_norm(v) for v in (cfg.brand.get("vocab") or [])}
    unsure = set()
    if recipe.get("source"):
        from .editor import load_words
        for w in load_words(workdir, recipe["source"]):
            if float(w.get("p", 1.0)) < 0.5:
                unsure.add(_norm(str(w.get("word", ""))))
    body = pkg.get("caption") or caption
    flagged = sorted({m for m in (_norm(x) for x in _WORD.findall(body))
                      if m and m in unsure and m not in vocab})
    add("spelling", "warn" if flagged else "pass", ", ".join(flagged))

    statuses = {c["status"] for c in checks}
    status = "block" if "block" in statuses else "warn" if "warn" in statuses else "pass"
    result = {"status": status, "version": version, "checks": checks}
    write_json(out / "qa.json", result)
    return result


def edited_step(conn, job, cfg):
    version = int(job["edit_version"] or 0)
    if version < 1:
        raise ValueError("EDITED job has no edit_version")
    workdir = Path(job["workdir"])
    packager.package(workdir, version, cfg)
    res = check(workdir, version, cfg)
    if res["status"] == "block":
        bad = ", ".join(f"{c['name']} ({c['detail']})" for c in res["checks"] if c["status"] == "block")
        db.log(conn, job["id"], "info", f"QA blocked v{version}: {bad}")
        return State.QA_FAILED
    if res["status"] == "warn":
        warn = ", ".join(f"{c['name']} ({c['detail']})" for c in res["checks"] if c["status"] == "warn")
        db.log(conn, job["id"], "info", f"QA warnings v{version}: {warn}")
    return None


STEPS = {State.EDITED: (edited_step, State.QA_PASSED)}
