"""Agent 2 — Shot Scout.  INGESTED -> SCOUTED.

Input:  <workdir>/ingest.json and the per-clip transcript / scenes / wav / proxy.
Output: <workdir>/scout/candidates.json   top windows, best first
        <workdir>/scout/pick.json         the day's pick (Claude, or a fallback)

1. Candidate windows
   - speech: whole sentences (word timestamps, 0.15 s padding), merged up to a
     length near reel.target_seconds within [min_seconds, max_seconds];
   - visual: scene-based windows over stretches with no speech;
   - whole: on a short day (total < target) one window per clip, trimmed of
     leading/trailing silence.
2. Cheap local scoring (speech pace, filler/unclear words, audio RMS, and
   OpenCV on a few proxy frames: sharpness, exposure, motion, faces), dedup
   by IoU, keep the top reel.candidates.
3. One Claude call ("daily_pick") orders the candidates and writes hook,
   caption and hashtags. Any failure falls back locally and alerts you; the
   step itself never fails because Claude is unavailable.

Idempotent: an existing valid pick.json means done (no second Claude call);
an existing candidates.json is reused.
"""
from __future__ import annotations

import json
import math
import re
import wave
from pathlib import Path

from .. import db
from ..config import Config
from ..llm import claude
from ..states import State
from ..util import read_json, write_json

PAD = 0.15                 # seconds of air around the first/last word
MAX_GAP = 4.0              # don't merge sentences across a longer silence
TRIM_SLACK = 2.0           # Claude may move window edges by this much
MAX_FRAMES = 8             # frames sampled per window (≈1 fps, capped)
PROMPT_TEXT_CHARS = 400
FILLERS = {"um", "uh", "erm", "er", "uhm", "hmm", "mm", "ah"}
SENT_END = re.compile(r"[.?!…][\"')\]]*$")
HASHTAG = re.compile(r"^#[\w]+$", re.UNICODE)


# ======================================================================== windows

def _words(tr: dict) -> list[dict]:
    out = []
    for seg in tr.get("segments") or []:
        seg_words = [w for w in seg.get("words") or [] if str(w.get("word", "")).strip()]
        for i, w in enumerate(seg_words):
            out.append({"start": float(w["start"]), "end": float(w["end"]),
                        "word": str(w["word"]).strip(), "p": float(w.get("p", 1.0)),
                        "seg_end": i == len(seg_words) - 1})
    out.sort(key=lambda w: w["start"])
    return out


def _sentences(words: list[dict], max_len: float) -> list[list[dict]]:
    """Group words into sentence units. Punctuation decides; if the transcript
    has none, segment ends do. Units longer than max_len are split at the
    largest pauses."""
    if not words:
        return []
    has_punct = any(SENT_END.search(w["word"]) for w in words)
    units, cur = [], []
    for w in words:
        cur.append(w)
        if (SENT_END.search(w["word"]) if has_punct else w["seg_end"]):
            units.append(cur)
            cur = []
    if cur:
        units.append(cur)
    out = []
    for u in units:
        out.extend(_split_long(u, max_len))
    return out


def _split_long(unit: list[dict], max_len: float) -> list[list[dict]]:
    if unit[-1]["end"] - unit[0]["start"] + 2 * PAD <= max_len or len(unit) < 2:
        return [unit]
    # split at the biggest pause, preferring the middle half
    n = len(unit)
    lo, hi = max(1, n // 4), max(2, 3 * n // 4)
    best = max(range(lo, hi), key=lambda i: (unit[i]["start"] - unit[i - 1]["end"], -abs(i - n / 2)))
    return _split_long(unit[:best], max_len) + _split_long(unit[best:], max_len)


def _span(words_in: list[dict], all_words: list[dict], idx0: int, idx1: int,
          duration: float) -> tuple[float, float]:
    """Padded [start, end] that never eats into neighbouring words."""
    prev_end = all_words[idx0 - 1]["end"] if idx0 > 0 else 0.0
    next_start = all_words[idx1 + 1]["start"] if idx1 + 1 < len(all_words) else duration
    start = max(0.0, prev_end, words_in[0]["start"] - PAD)
    end = min(duration, next_start, words_in[-1]["end"] + PAD)
    return round(start, 3), round(end, 3)


def _text(words: list[dict]) -> str:
    return " ".join(w["word"] for w in words).strip()


def _speech_windows(clip: dict, words: list[dict], cfg: Config) -> list[dict]:
    rc = cfg.reel
    lo, hi, target = float(rc["min_seconds"]), float(rc["max_seconds"]), float(rc["target_seconds"])
    units = _sentences(words, hi)
    index = {id(w): i for i, w in enumerate(words)}
    out = []
    for i in range(len(units)):
        options = []
        for j in range(i, len(units)):
            if j > i and units[j][0]["start"] - units[j - 1][-1]["end"] > MAX_GAP:
                break
            ws = [w for u in units[i:j + 1] for w in u]
            s, e = _span(ws, words, index[id(ws[0])], index[id(ws[-1])], clip["duration"])
            if e - s > hi:
                break
            if e - s >= lo:
                options.append((abs((e - s) - target), s, e, ws))
        if options:
            _, s, e, ws = min(options, key=lambda o: o[0])
            out.append(_window(clip, s, e, "speech", ws))
    return out


def _silent_regions(words: list[dict], duration: float, min_len: float) -> list[tuple[float, float]]:
    edges, t = [], 0.0
    for w in words:
        if w["start"] - PAD - t >= min_len:
            edges.append((t, w["start"] - PAD))
        t = max(t, w["end"] + PAD)
    if duration - t >= min_len:
        edges.append((t, duration))
    return edges


def _visual_windows(clip: dict, scenes: list[dict], region: tuple[float, float],
                    cfg: Config) -> list[dict]:
    rc = cfg.reel
    lo, hi, target = float(rc["min_seconds"]), float(rc["max_seconds"]), float(rc["target_seconds"])
    r0, r1 = region
    cuts = sorted({r0, r1, *(float(s["start"]) for s in scenes if r0 < float(s["start"]) < r1)})
    # break very long scenes into target-sized pieces so windows can start inside them
    points = []
    for a, b in zip(cuts, cuts[1:]):
        n = max(1, math.ceil((b - a) / target))
        points += [a + k * (b - a) / n for k in range(n)]
    points.append(r1)
    out = []
    for i in range(len(points) - 1):
        best = None
        for j in range(i + 1, len(points)):
            d = points[j] - points[i]
            if d > hi:
                break
            if d >= lo and (best is None or abs(d - target) < abs(best - target)):
                best = d
        if best is None and points[-1] - points[i] >= lo and i + 1 < len(points):
            best = min(hi, points[i + 1] - points[i])  # one scene longer than max
        if best is not None:
            out.append(_window(clip, round(points[i], 3), round(points[i] + best, 3), "visual", []))
    return out


def _whole_window(clip: dict, words: list[dict]) -> dict:
    if words:
        s, e = _span(words, words, 0, len(words) - 1, clip["duration"])
    else:
        s, e = 0.0, round(clip["duration"], 3)
    return _window(clip, s, e, "whole", words)


def _window(clip: dict, start: float, end: float, kind: str, words: list[dict]) -> dict:
    return {"clip": clip["file"], "source": clip["source"], "proxy": clip.get("proxy"),
            "start": start, "end": end, "duration": round(end - start, 3), "kind": kind,
            "text": _text(words), "_words": words, "_audio": clip.get("audio")}


def _load_clip(clip: dict) -> tuple[list[dict], list[dict]]:
    tr = read_json(clip["transcript"]) if clip.get("transcript") and Path(clip["transcript"]).exists() else {}
    scenes = read_json(clip["scenes"]) if clip.get("scenes") and Path(clip["scenes"]).exists() else []
    return _words(tr), scenes or [{"start": 0.0, "end": clip["duration"]}]


def generate_windows(manifest: dict, cfg: Config) -> list[dict]:
    rc = cfg.reel
    clips = manifest["clips"]
    total = manifest.get("total_seconds") or sum(c["duration"] for c in clips)
    loaded = [(c, *_load_clip(c)) for c in clips]
    if total < float(rc["target_seconds"]):
        return [_whole_window(c, ws) for c, ws, _ in loaded]
    out = []
    for c, ws, scenes in loaded:
        out += _speech_windows(c, ws, cfg)
        for region in _silent_regions(ws, c["duration"], float(rc["min_seconds"])):
            out += _visual_windows(c, scenes, region, cfg)
    if not out:  # every clip shorter than min_seconds, etc.
        out = [_whole_window(c, ws) for c, ws, _ in loaded]
    return out


# ======================================================================== scoring

def _clamp(x: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, x))


def iou(a: dict, b: dict) -> float:
    if a["clip"] != b["clip"]:
        return 0.0
    inter = max(0.0, min(a["end"], b["end"]) - max(a["start"], b["start"]))
    union = max(a["end"], b["end"]) - min(a["start"], b["start"])
    return inter / union if union > 0 else 0.0


def _dedupe(windows: list[dict], key: str) -> list[dict]:
    kept: list[dict] = []
    for w in sorted(windows, key=lambda w: -w[key]):
        if all(iou(w, k) <= 0.5 for k in kept):
            kept.append(w)
    return kept


def speech_features(words: list[dict], duration: float) -> dict:
    n = len(words)
    norm = [re.sub(r"[^\w']", "", w["word"].lower()) for w in words]
    return {"words": n,
            "words_per_sec": round(n / duration, 2) if duration > 0 else 0.0,
            "low_conf_words": sum(1 for w in words if w["p"] < 0.5),
            "fillers": sum(1 for t in norm if t in FILLERS)}


class _Audio:
    """16-bit PCM WAV loaded once per clip; RMS of any window."""

    def __init__(self):
        self.cache: dict[str, tuple | None] = {}

    def rms_db(self, path: str | None, start: float, end: float) -> float | None:
        if not path:
            return None
        if path not in self.cache:
            self.cache[path] = self._load(path)
        loaded = self.cache[path]
        if loaded is None:
            return None
        import numpy as np
        data, sr = loaded
        seg = data[int(start * sr):int(end * sr)]
        if seg.size == 0:
            return None
        rms = float(np.sqrt(np.mean(seg.astype(np.float64) ** 2)))
        return 20 * math.log10(max(rms, 1e-6))

    @staticmethod
    def _load(path: str):
        try:
            import numpy as np
            with wave.open(path, "rb") as w:
                if w.getsampwidth() != 2:
                    return None
                ch, sr = w.getnchannels(), w.getframerate()
                data = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16)
            if ch > 1:
                data = data.reshape(-1, ch).mean(axis=1)
            return data.astype(np.float32) / 32768.0, sr
        except Exception:  # noqa: BLE001 - missing/odd audio just means no energy feature
            return None


class _Frames:
    """Samples frames from the proxy with OpenCV. Missing cv2 = no visual features."""

    def __init__(self):
        try:
            import cv2
            self.cv2 = cv2
            self.face = cv2.CascadeClassifier(cv2.data.haarcascades + "haarcascade_frontalface_default.xml")
        except Exception:  # noqa: BLE001
            self.cv2 = None
        self.caps: dict[str, object] = {}

    def close(self):
        for cap in self.caps.values():
            cap.release()

    def features(self, proxy: str | None, start: float, end: float) -> dict | None:
        cv2 = self.cv2
        if cv2 is None or not proxy or not Path(proxy).exists():
            return None
        cap = self.caps.get(proxy)
        if cap is None:
            cap = self.caps[proxy] = cv2.VideoCapture(proxy)
        if not cap.isOpened():
            return None
        dur = end - start
        n = max(1, min(MAX_FRAMES, math.ceil(dur)))
        sharp, bright, motion, faces = [], [], [], 0
        for k in range(n):
            t = start + (k + 0.5) * dur / n
            cap.set(cv2.CAP_PROP_POS_MSEC, t * 1000)
            ok, frame = cap.read()
            if not ok:
                continue
            gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
            h, w = gray.shape
            if h > 360:
                gray = cv2.resize(gray, (int(w * 360 / h), 360))
            sharp.append(cv2.Laplacian(gray, cv2.CV_64F).var())
            bright.append(float(gray.mean()))
            if self.face is not None and not self.face.empty():
                if len(self.face.detectMultiScale(gray, 1.2, 5, minSize=(30, 30))):
                    faces += 1
            for _ in range(6):          # ~0.2 s later at 30 fps
                cap.grab()
            ok2, frame2 = cap.read()
            if ok2:
                g2 = cv2.cvtColor(frame2, cv2.COLOR_BGR2GRAY)
                g2 = cv2.resize(g2, (gray.shape[1], gray.shape[0]))
                motion.append(float(cv2.absdiff(gray, g2).mean()))
        if not sharp:
            return None
        lap = sum(sharp) / len(sharp)
        mean_b = sum(bright) / len(bright)
        exposure = 1.0 if 50 <= mean_b <= 205 else _clamp(1 - min(abs(mean_b - 50), abs(mean_b - 205)) / 50)
        return {"sharpness": round(_clamp(math.log10(lap + 1) / 3), 3),
                "brightness": round(mean_b, 1),
                "exposure": round(exposure, 3),
                "motion": round(_clamp((sum(motion) / len(motion)) / 12) if motion else 0.0, 3),
                "faces": round(faces / len(sharp), 3)}


def _duration_fit(d: float, cfg: Config) -> float:
    target = float(cfg.reel["target_seconds"])
    return _clamp(1 - abs(d - target) / max(target, 1.0))


def _speech_quality(sf: dict) -> float:
    if not sf["words"]:
        return 0.0
    wps = sf["words_per_sec"]
    pace = 1.0 if 2.0 <= wps <= 3.5 else _clamp(1 - min(abs(wps - 2.0), abs(wps - 3.5)) / 2)
    bad = (sf["fillers"] + sf["low_conf_words"]) / sf["words"]
    return _clamp(pace - 2.5 * bad)


def _complete(w: dict) -> bool:
    return bool(w["text"]) and bool(SENT_END.search(w["text"])) and w["text"][0].isupper()


def prescore(w: dict, cfg: Config, audio: _Audio) -> None:
    """Cheap pass (no video decode) used to shortlist before OpenCV."""
    sf = speech_features(w["_words"], w["duration"])
    db_ = audio.rms_db(w["_audio"], w["start"], w["end"])
    energy = None if db_ is None else round(_clamp((db_ + 50) / 40), 3)
    w["_sf"], w["_energy"] = sf, energy
    e = 0.5 if energy is None else energy
    if sf["words"]:
        w["_pre"] = 0.45 * _speech_quality(sf) + 0.2 * _complete(w) + 0.2 * _duration_fit(w["duration"], cfg) + 0.15 * e
    else:
        w["_pre"] = 0.6 * _duration_fit(w["duration"], cfg) + 0.4 * e


def score(w: dict, cfg: Config, frames: _Frames) -> None:
    sf, energy = w["_sf"], w["_energy"]
    vf = frames.features(w["proxy"], w["start"], w["end"])
    e = 0.5 if energy is None else energy
    v = vf or {"sharpness": 0.5, "exposure": 1.0, "motion": 0.3, "faces": 0.0}
    fit = _duration_fit(w["duration"], cfg)
    if sf["words"]:
        q = _speech_quality(sf)
        s = (0.25 * q + 0.15 * _complete(w) + 0.15 * fit + 0.15 * e + 0.10 * v["faces"]
             + 0.10 * v["sharpness"] + 0.05 * v["exposure"] + 0.05 * min(1.0, v["motion"] * 2))
    else:
        s = (0.25 * v["motion"] + 0.25 * v["sharpness"] + 0.15 * v["exposure"] + 0.15 * fit
             + 0.10 * v["faces"] + 0.10 * e)
    w["score"] = round(_clamp(s), 3)
    w["features"] = {"words_per_sec": sf["words_per_sec"],
                     "sharpness": v["sharpness"] if vf else None,
                     "energy": energy,
                     "faces": v["faces"] if vf else None,
                     "motion": v["motion"] if vf else None,
                     "exposure": v["exposure"] if vf else None,
                     "low_conf_words": sf["low_conf_words"],
                     "fillers": sf["fillers"]}
    w["reasons"] = _reasons(w, sf, energy, vf, fit)


def _reasons(w, sf, energy, vf, fit) -> list[str]:
    r = []
    if sf["words"]:
        if _complete(w):
            r.append("complete sentence")
        wps = sf["words_per_sec"]
        r.append("good pace" if 2.0 <= wps <= 3.5 else ("fast talk" if wps > 3.5 else "slow pace"))
        if sf["fillers"]:
            r.append(f"{sf['fillers']} filler word{'s' * (sf['fillers'] > 1)}")
        if sf["low_conf_words"]:
            r.append(f"{sf['low_conf_words']} unclear word{'s' * (sf['low_conf_words'] > 1)}")
    else:
        r.append("no speech" if w["kind"] != "whole" else "whole clip")
    if energy is not None:
        r.append("high energy" if energy >= 0.6 else ("quiet audio" if energy < 0.3 else "steady audio"))
    if vf:
        r.append("sharp" if vf["sharpness"] >= 0.6 else ("soft focus" if vf["sharpness"] < 0.35 else "ok focus"))
        if vf["exposure"] < 0.7:
            r.append("too dark" if vf["brightness"] < 50 else "overexposed")
        if vf["faces"] >= 0.5:
            r.append("face on screen")
        if vf["motion"] >= 0.5:
            r.append("lots of motion")
        elif vf["motion"] < 0.05 and not sf["words"]:
            r.append("static shot")
    if fit >= 0.8:
        r.append("near target length")
    return r


def build_candidates(workdir: Path, cfg: Config) -> list[dict]:
    """Windows -> scored, deduplicated, top-N candidate dicts (contract shape)."""
    manifest = read_json(Path(workdir) / "ingest.json")
    windows = generate_windows(manifest, cfg)
    n = int(cfg.reel["candidates"])
    audio = _Audio()
    for w in windows:
        prescore(w, cfg, audio)
    shortlist = _dedupe(windows, "_pre")[:max(3 * n, 20)]
    frames = _Frames()
    try:
        for w in shortlist:
            score(w, cfg, frames)
    finally:
        frames.close()
    best = _dedupe(shortlist, "score")[:n]
    out = []
    for i, w in enumerate(best, 1):
        out.append({"id": f"c{i}", **{k: v for k, v in w.items() if not k.startswith("_")}})
    # contract key order: id, clip, source, proxy, start, end, duration, kind, text, score, reasons, features
    keys = ["id", "clip", "source", "proxy", "start", "end", "duration", "kind", "text",
            "score", "reasons", "features"]
    return [{k: c[k] for k in keys} for c in out]


# ======================================================================== pick

PICK_SCHEMA = {
    "type": "object",
    "required": ["order", "hook", "alt_hooks", "caption", "hashtags", "why"],
    "properties": {
        "order": {"type": "array", "minItems": 1, "items": {"type": "string"},
                  "description": "candidate ids, best first"},
        "hook": {"type": "string", "maxLength": 60, "description": "on-screen hook, max 8 words"},
        "alt_hooks": {"type": "array", "minItems": 2, "maxItems": 3,
                      "items": {"type": "string", "maxLength": 60}},
        "caption": {"type": "string", "minLength": 1, "maxLength": 2000,
                    "description": "post caption without hashtags"},
        "hashtags": {"type": "array", "minItems": 3, "maxItems": 8,
                     "items": {"type": "string", "pattern": "^#\\w+$"}},
        "why": {"type": "string", "maxLength": 300},
        "trim": {"type": "array", "description": "optional tighter in/out points, within 2 s of the window",
                 "items": {"type": "object", "required": ["id", "start", "end"],
                           "properties": {"id": {"type": "string"}, "start": {"type": "number"},
                                          "end": {"type": "number"}}}},
    },
}


def build_prompt(cands: list[dict], cfg: Config) -> str:
    voice = cfg.brand.get("voice") or {}
    adjectives = ", ".join(voice.get("adjectives") or []) or "(none given)"
    samples = "\n".join(f"- {s}" for s in (voice.get("sample_captions") or [])[:5]) or "- (none)"
    banned = ", ".join(cfg.brand.get("banned_words") or []) or "(none)"
    playbook = ""
    if cfg.playbook_path.exists():
        playbook = cfg.playbook_path.read_text()[:3000].strip()
    lines = [
        "You pick today's Instagram Reel from candidate clips of the creator's own footage.",
        f"Brand voice: {adjectives}.",
        "Captions they posted before:", samples,
        f"Never use these words anywhere: {banned}.",
    ]
    if playbook:
        lines += ["What has worked recently (playbook):", playbook]
    lines += [
        f"Target length ~{cfg.reel['target_seconds']} s (min {cfg.reel['min_seconds']}, "
        f"max {cfg.reel['max_seconds']}).",
        "Candidates (one JSON object per line, best local score first):",
    ]
    for c in cands:
        text = c["text"] if len(c["text"]) <= PROMPT_TEXT_CHARS else c["text"][:PROMPT_TEXT_CHARS] + "…"
        lines.append(json.dumps({"id": c["id"], "kind": c["kind"], "seconds": c["duration"],
                                 "start": c["start"], "end": c["end"], "score": c["score"],
                                 "reasons": c["reasons"], "text": text}, ensure_ascii=False))
    lines += [
        "",
        "Return JSON: order = candidate ids best first (use only the ids above); "
        "hook = on-screen hook, max 8 words; alt_hooks = 2-3 alternatives; caption = post "
        "caption in the brand voice, no hashtags; hashtags = 3-8 tags starting with #; "
        "why = one line; trim = optional [{id, start, end}] to tighten a clip "
        "(absolute seconds, within 2 s of its window).",
    ]
    return "\n".join(lines)


def _banned_in_text(text: str, banned: list[str]) -> str | None:
    for b in banned:
        if b and re.search(rf"(?<!\w){re.escape(b)}(?!\w)", text, re.I):
            return b
    return None


def _clean_pick(data: dict, cands: list[dict], cfg: Config) -> dict:
    """Validate Claude's answer against the candidates and brand rules.
    Raises ValueError (-> fallback) on anything we can't repair."""
    by_id = {c["id"]: c for c in cands}
    banned = [str(b) for b in cfg.brand.get("banned_words") or []]
    order = []
    for i in data.get("order") or []:
        if i in by_id and i not in order:
            order.append(i)
    order += [c["id"] for c in cands if c["id"] not in order]

    hook = str(data.get("hook", "")).strip()
    alt = [str(h).strip() for h in data.get("alt_hooks") or [] if str(h).strip()][:3]
    caption = str(data.get("caption", "")).strip()
    why = str(data.get("why", "")).strip()
    if len(hook) > 60:
        raise ValueError("hook longer than 60 chars")
    if not caption:
        raise ValueError("empty caption")
    for name, text in [("hook", hook), ("caption", caption), ("why", why)] + [("alt hook", a) for a in alt]:
        b = _banned_in_text(text, banned)
        if b:
            raise ValueError(f"{name} contains banned word {b!r}")
    tags = []
    for t in data.get("hashtags") or []:
        t = str(t).strip()
        if not HASHTAG.match(t):
            raise ValueError(f"bad hashtag {t!r}")
        if any(b and b.lower() in t.lower() for b in banned):
            continue  # drop it; too few left is rejected below
        if t.lower() not in {x.lower() for x in tags}:
            tags.append(t)
    if not 3 <= len(tags) <= 8:
        raise ValueError(f"{len(tags)} usable hashtags (need 3-8; banned words dropped)")

    lo = float(cfg.reel["min_seconds"])
    trim = {}
    for t in data.get("trim") or []:
        c = by_id.get(t.get("id")) if isinstance(t, dict) else None
        if not c:
            continue
        try:
            s, e = float(t["start"]), float(t["end"])
        except (KeyError, TypeError, ValueError):
            continue
        if c["start"] - TRIM_SLACK <= s < e <= c["end"] + TRIM_SLACK and s >= 0 and e - s >= lo:
            trim[c["id"]] = {"start": round(s, 3), "end": round(e, 3)}
    return {"source": "claude", "order": order, "hook": hook, "alt_hooks": alt,
            "caption": caption, "hashtags": tags, "why": why, "trim": trim}


def _first_sentence(text: str) -> str:
    m = re.match(r"(.+?[.?!…])(\s|$)", text.strip())
    return (m.group(1) if m else text).strip()


def _playbook_tags(cfg: Config) -> list[str]:
    if not cfg.playbook_path.exists():
        return []
    banned = [str(b).lower() for b in cfg.brand.get("banned_words") or [] if b]
    tags = []
    for t in re.findall(r"#\w+", cfg.playbook_path.read_text()):
        if t.lower() not in {x.lower() for x in tags} and not any(b in t.lower() for b in banned):
            tags.append(t)
    return tags[:8]


def fallback_pick(cands: list[dict], cfg: Config) -> dict:
    order = [c["id"] for c in cands]
    if cfg.claude.get("fallback", "human") == "heuristic":
        top = cands[0]
        sentence = _first_sentence(top["text"]) if top["text"] else ""
        hook = " ".join(sentence.split()[:8]) if top["kind"] != "visual" else ""
        if _banned_in_text(hook, cfg.brand.get("banned_words") or []):
            hook = ""
        caption = sentence if not _banned_in_text(sentence, cfg.brand.get("banned_words") or []) else ""
        return {"source": "local_draft", "order": order, "hook": hook, "alt_hooks": [],
                "caption": caption, "hashtags": _playbook_tags(cfg),
                "why": "local draft: highest local score", "trim": {}}
    return {"source": "needs_caption", "order": order, "hook": "", "alt_hooks": [],
            "caption": "", "hashtags": [], "why": "Claude unavailable: ordered by local score",
            "trim": {}}


def _valid_pick(pick, cands: list[dict]) -> bool:
    ids = {c["id"] for c in cands}
    return (isinstance(pick, dict)
            and pick.get("source") in ("claude", "local_draft", "needs_caption")
            and isinstance(pick.get("order"), list) and bool(pick["order"])
            and all(i in ids for i in pick["order"]))


def _read(path: Path):
    try:
        return read_json(path)
    except (OSError, ValueError):
        return None


# ======================================================================== step

def step(conn, job, cfg: Config, runner=None) -> None:
    wd = Path(job["workdir"])
    cpath, ppath = wd / "scout" / "candidates.json", wd / "scout" / "pick.json"

    data = _read(cpath)
    if isinstance(data, dict) and isinstance(data.get("candidates"), list) and data["candidates"]:
        cands = data["candidates"]
    else:
        cands = build_candidates(wd, cfg)
        if not cands:
            raise ValueError("no candidate windows in today's footage")
        write_json(cpath, {"candidates": cands})

    if _valid_pick(_read(ppath), cands):
        return None

    try:
        try:
            raw = claude.call(conn, cfg, "daily_pick", build_prompt(cands, cfg), PICK_SCHEMA,
                              job_id=job["id"], runner=runner)
        except claude.ClaudeUnavailable as e:
            raise _Fallback(str(e)) from e
        try:
            pick = _clean_pick(raw, cands, cfg)
        except (ValueError, TypeError, AttributeError) as e:
            raise _Fallback(f"output rejected: {e}") from e
    except _Fallback as e:
        pick = fallback_pick(cands, cfg)
        what = ("a local draft caption was written; check it before approving"
                if pick["source"] == "local_draft"
                else "clips are ordered by local score and the caption is empty; write it at approval")
        db.alert(conn, job["id"], f"Claude unavailable ({e}): {what}.")
    write_json(ppath, pick)
    return None


class _Fallback(Exception):
    pass


STEPS = {State.INGESTED: (step, State.SCOUTED)}


# ======================================================================== editor helper

def next_candidate(workdir, used_ids) -> dict | None:
    """Next unused candidate following pick["order"] (then any others by
    score), with the pick's trim applied. None when nothing is left."""
    wd = Path(workdir)
    data = _read(wd / "scout" / "candidates.json")
    if not isinstance(data, dict):
        return None
    cands = data.get("candidates") or []
    by_id = {c["id"]: c for c in cands}
    pick = _read(wd / "scout" / "pick.json") or {}
    order = [i for i in pick.get("order") or [] if i in by_id]
    order += [c["id"] for c in sorted(cands, key=lambda c: -c.get("score", 0)) if c["id"] not in order]
    used = set(used_ids or ())
    for i in order:
        if i in used:
            continue
        c = dict(by_id[i])
        t = (pick.get("trim") or {}).get(i)
        if t:
            c["start"], c["end"] = float(t["start"]), float(t["end"])
            c["duration"] = round(c["end"] - c["start"], 3)
        return c
    return None
