"""Agent 3 — Editor.

SCOUTED          -> EDITED   render edit/v<N>/ from the next unused candidate
RECUT_REQUESTED  -> EDITED   different_clip | tighter | bad_crop | fix_captions
QA_FAILED        -> SCOUTED  retry with the next candidate (twice), then give up

Every version lives in edit/v<N>/ with its recipe (edit.json, hand-editable)
next to the rendered files. jobs.edit_version is bumped only after a render
finished, so a crash simply re-runs the step; a version whose recipe and
files are already there is not rendered again.
"""
from __future__ import annotations

import json
import os
import re
import shutil
from pathlib import Path

from .. import db, scheduler
from ..media import ffmpeg, reframe, render as rnd
from ..states import State
from ..util import edit_dir, read_json, write_json

TIGHT_MAX = 19.5


# ---------------------------------------------------------------- candidates

def _read(path: Path, default=None):
    try:
        return read_json(path)
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def _local_next_candidate(workdir: Path, used_ids) -> dict | None:
    """Same contract as scout.next_candidate: pick order, skip used, apply trim."""
    cands = (_read(Path(workdir) / "scout" / "candidates.json", {}) or {}).get("candidates", [])
    pick = _read(Path(workdir) / "scout" / "pick.json", {}) or {}
    by_id = {c["id"]: c for c in cands}
    order = [i for i in pick.get("order", []) if i in by_id]
    order += [c["id"] for c in cands if c["id"] not in order]
    used = set(used_ids or ())
    for cid in order:
        if cid in used:
            continue
        c = dict(by_id[cid])
        trim = (pick.get("trim") or {}).get(cid)
        if trim:
            c["start"], c["end"] = float(trim["start"]), float(trim["end"])
            c["duration"] = round(c["end"] - c["start"], 3)
        return c
    return None


def next_candidate(workdir: Path, used_ids) -> dict | None:
    try:
        from . import scout
        fn = scout.next_candidate
    except (ImportError, AttributeError):
        fn = _local_next_candidate
    return fn(Path(workdir), set(used_ids or ()))


def versions(workdir: Path) -> list[int]:
    root = Path(workdir) / "edit"
    if not root.is_dir():
        return []
    return sorted(int(m.group(1)) for p in root.iterdir()
                  if (m := re.fullmatch(r"v(\d+)", p.name)) and (p / "edit.json").exists())


def used_candidates(workdir: Path) -> set[str]:
    """Candidates that failed: QA-blocked versions and clips replaced by a
    different_clip recut."""
    used, prev = set(), None
    for v in versions(workdir):
        d = edit_dir(workdir, v)
        rec = _read(d / "edit.json", {}) or {}
        if rec.get("reason") == "recut:different_clip" and prev:
            used.add(prev)
        if ((_read(d / "qa.json", {}) or {}).get("status")) == "block":
            used.add(rec.get("candidate_id"))
        prev = rec.get("candidate_id")
    used.discard(None)
    return used


def load_words(workdir: Path, source: str) -> list[dict]:
    """All transcript words (source time) of the clip `source`."""
    workdir = Path(workdir)
    path = None
    for c in (_read(workdir / "ingest.json", {}) or {}).get("clips", []):
        if c.get("source") == str(source) or c.get("file") == Path(source).name:
            path = c.get("transcript")
            break
    if not path:
        path = workdir / "ingest" / f"{Path(source).stem}.transcript.json"
    tr = _read(Path(path), {}) or {}
    return [w for seg in tr.get("segments", []) for w in seg.get("words", []) or []]


# ---------------------------------------------------------------- recipes

def _pick(workdir: Path) -> dict:
    return _read(Path(workdir) / "scout" / "pick.json", {}) or {}


def build_recipe(workdir: Path, cand: dict, cfg, version, reason: str,
                 crop_mode: str | None = None, base: dict | None = None) -> dict:
    src = cand["source"]
    meta = ffmpeg.probe(Path(src))
    proxy = cand.get("proxy") or src
    if not Path(proxy).exists():
        proxy = src
    start, end = float(cand["start"]), float(cand["end"])
    crop = reframe.choose_crop(Path(proxy), start, end, meta["width"], meta["height"], crop_mode)
    base = base or {}
    return {
        "version": version,
        "candidate_id": cand["id"],
        "source": str(src),
        "proxy": str(proxy),
        "start": round(start, 3),
        "end": round(end, 3),
        "crop": {"mode": crop["mode"], "x": crop["x"], "w": crop["w"]},
        "speed": base.get("speed", 1.0),
        "hook_text": base.get("hook_text", _pick(workdir).get("hook", "")),
        "caption_style": base.get("caption_style")
                         or {"font": cfg.brand.get("font", "Montserrat"), "size": 72},
        "word_fixes": dict(base.get("word_fixes") or {}),
        "loudnorm": base.get("loudnorm", -14),
        "reason": reason,
    }


def _same(a: dict, b: dict) -> bool:
    return json.dumps(a, sort_keys=True) == json.dumps(b, sort_keys=True)


def make_version(workdir: Path, recipe: dict, cfg) -> Path:
    """Write edit.json and render, unless that exact version is already done."""
    out = edit_dir(workdir, recipe["version"])
    existing = _read(out / "edit.json")
    if existing is not None and _same(existing, recipe) and rnd.is_rendered(out):
        return out
    write_json(out / "edit.json", recipe)
    rnd.render(recipe, load_words(workdir, recipe["source"]), cfg, out)
    return out


def tighten(recipe: dict, words: list[dict], cfg) -> tuple[float, float]:
    """New (start, end) < 20 s, on sentence boundaries when there are words."""
    start, end = float(recipe["start"]), float(recipe["end"])
    dur = end - start
    floor = min(float(cfg.reel.get("min_seconds", 7)), dur)
    target = TIGHT_MAX if dur >= 20 else max(dur * 0.75, min(floor, dur * 0.9))
    inside = [w for w in words
              if float(w["start"]) >= start - 0.05 and float(w["end"]) <= end + 0.05]
    sentences, cur = [], []
    for w in inside:
        cur.append(w)
        if str(w.get("word", "")).rstrip("\"')”’").endswith((".", "!", "?")):
            sentences.append(cur)
            cur = []
    if cur:
        sentences.append(cur)
    best = None
    for i in range(len(sentences)):
        for j in range(i, len(sentences)):
            s, e = float(sentences[i][0]["start"]), float(sentences[j][-1]["end"])
            if e - s + 0.4 > target:
                break
            if best is None or e - s > best[1] - best[0] + 1e-6:
                best = (s, e)
    if best and best[1] - best[0] >= min(3.0, target / 2):
        s, e = best
        before = [float(w["end"]) for w in words if float(w["end"]) <= s]
        after = [float(w["start"]) for w in words if float(w["start"]) >= e]
        lo = max([start, s - 0.15] + [b + 0.02 for b in before[-1:]])
        hi = min([end, e + 0.25] + [a - 0.02 for a in after[:1]])
        return round(min(lo, s), 3), round(max(hi, e), 3)
    mid = (start + end) / 2
    return round(mid - target / 2, 3), round(mid + target / 2, 3)


# ---------------------------------------------------------------- alternates

def alt_dir(workdir: Path, cid: str) -> Path:
    return Path(workdir) / "edit" / "alt" / cid


def prerender_alternates(conn, job, cfg, used: set[str]) -> list[str]:
    n = int(cfg.reel.get("prerender_alternates", 0) or 0)
    workdir = Path(job["workdir"])
    done, skip = [], set(used)
    for _ in range(n):
        cand = next_candidate(workdir, skip)
        if cand is None:
            break
        skip.add(cand["id"])
        out = alt_dir(workdir, cand["id"])
        if rnd.is_rendered(out) and (out / "edit.json").exists():
            done.append(cand["id"])
            continue
        try:
            recipe = build_recipe(workdir, cand, cfg, None, "alternate")
            write_json(out / "edit.json", recipe)
            rnd.render(recipe, load_words(workdir, recipe["source"]), cfg, out)
            done.append(cand["id"])
        except Exception as e:  # noqa: BLE001 - an alternate is a nice-to-have
            db.log(conn, job["id"], "info", f"alternate {cand['id']} not pre-rendered: {e}")
    return done


def _adopt_alt(workdir: Path, cid: str, recipe: dict) -> bool:
    """Fast path: move a pre-rendered alternate into edit/v<N>/."""
    src = alt_dir(workdir, cid)
    alt_recipe = _read(src / "edit.json")
    if alt_recipe is None or not rnd.is_rendered(src):
        return False
    keep = {k: v for k, v in alt_recipe.items() if k not in ("version", "reason")}
    want = {k: v for k, v in recipe.items() if k not in ("version", "reason")}
    if not _same(keep, want):
        return False
    out = edit_dir(workdir, recipe["version"])
    out.mkdir(parents=True, exist_ok=True)
    for name in rnd.OUTPUTS:
        os.replace(src / name, out / name)
    write_json(out / "edit.json", recipe)
    shutil.rmtree(src, ignore_errors=True)
    return True


# ---------------------------------------------------------------- steps

def _finish(conn, job, cfg, version: int, used: set[str], **extra) -> None:
    db.set_job(conn, job["id"], edit_version=version, **extra)
    rec = _read(edit_dir(job["workdir"], version) / "edit.json", {}) or {}
    prerender_alternates(conn, job, cfg, used | {rec.get("candidate_id")})


def scouted_step(conn, job, cfg):
    workdir = Path(job["workdir"])
    cur = int(job["edit_version"] or 0)
    if cur and rnd.is_rendered(edit_dir(workdir, cur)) and \
            not (edit_dir(workdir, cur) / "qa.json").exists():
        return None  # this pass already rendered (crash after set_job)
    used = used_candidates(workdir)
    cand = next_candidate(workdir, used)
    if cand is None:
        db.give_up(conn, job["id"], State.SCOUTED, "no unused candidate left to edit")
        return scheduler.STAY
    n = cur + 1
    recipe = build_recipe(workdir, cand, cfg, n, "first" if cur == 0 else "qa_retry")
    if not _adopt_alt(workdir, cand["id"], recipe):
        make_version(workdir, recipe, cfg)
    _finish(conn, job, cfg, n, used)
    return None


def recut_step(conn, job, cfg):
    workdir = Path(job["workdir"])
    cur = int(job["edit_version"] or 0)
    raw = job["recut_reason"]
    if not raw:
        if cur:
            return None  # already done (reason cleared after the render)
        raise ValueError("RECUT_REQUESTED without a recut_reason and no edit yet")
    req = json.loads(raw) if isinstance(raw, str) else dict(raw)
    reason = req.get("reason")
    base = _read(edit_dir(workdir, cur) / "edit.json") if cur else None
    if base is None:
        raise ValueError(f"recut needs edit/v{cur}/edit.json")
    n = cur + 1
    used = used_candidates(workdir)

    if reason == "different_clip":
        used = used | {base["candidate_id"]}
        cand = next_candidate(workdir, used)
        if cand is None:
            db.give_up(conn, job["id"], State.RECUT_REQUESTED, "no other candidate for a different clip")
            return scheduler.STAY
        keep = {k: base[k] for k in ("hook_text", "caption_style", "loudnorm", "speed") if k in base}
        recipe = build_recipe(workdir, cand, cfg, n, "recut:different_clip", base=keep)
        if not _adopt_alt(workdir, cand["id"], recipe):
            make_version(workdir, recipe, cfg)
    elif reason in ("tighter", "bad_crop", "fix_captions"):
        recipe = {**base, "version": n, "reason": f"recut:{reason}"}
        if reason == "tighter":
            recipe["start"], recipe["end"] = tighten(base, load_words(workdir, base["source"]), cfg)
        elif reason == "bad_crop":
            mode = reframe.next_mode((base.get("crop") or {}).get("mode", "center"))
            meta = ffmpeg.probe(Path(base["source"]))
            box = reframe.choose_crop(Path(base.get("proxy") or base["source"]),
                                      float(base["start"]), float(base["end"]),
                                      meta["width"], meta["height"], mode)
            recipe["crop"] = {"mode": box["mode"], "x": box["x"], "w": box["w"]}
        else:
            recipe["word_fixes"] = {**(base.get("word_fixes") or {}), **(req.get("word_fixes") or {})}
        make_version(workdir, recipe, cfg)
    else:
        raise ValueError(f"unknown recut reason: {raw}")

    _finish(conn, job, cfg, n, used, recut_reason=None)
    return None


def qa_failed_step(conn, job, cfg):
    failures = int(job["qa_failures"] or 0)
    if failures < 2:
        db.set_job(conn, job["id"], qa_failures=failures + 1)
        return State.SCOUTED
    db.give_up(conn, job["id"], State.QA_FAILED,
               f"QA blocked {failures + 1} versions in a row (see edit/v{job['edit_version']}/qa.json)")
    return scheduler.STAY


STEPS = {
    State.SCOUTED: (scouted_step, State.EDITED),
    State.RECUT_REQUESTED: (recut_step, State.EDITED),
    State.QA_FAILED: (qa_failed_step, State.SCOUTED),
}
