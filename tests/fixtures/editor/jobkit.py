"""Builds a tiny job workdir (clips, ingest outputs, scout files) for the
editor / packager / QA tests."""
from __future__ import annotations

from pathlib import Path

from reel import db
from reel.util import write_json
from tests.conftest import make_clip

WORDS = [
    {"start": 0.2, "end": 0.5, "word": "Hello", "p": 0.95},
    {"start": 0.55, "end": 0.9, "word": "there.", "p": 0.95},
    {"start": 1.0, "end": 1.3, "word": "This", "p": 0.9},
    {"start": 1.35, "end": 1.6, "word": "is", "p": 0.9},
    {"start": 1.65, "end": 2.0, "word": "kaisen", "p": 0.3},
    {"start": 2.05, "end": 2.4, "word": "every", "p": 0.9},
    {"start": 2.45, "end": 2.9, "word": "day.", "p": 0.9},
    {"start": 3.0, "end": 3.3, "word": "Small", "p": 0.9},
    {"start": 3.35, "end": 3.7, "word": "wins.", "p": 0.9},
]


def build_job(tmp_path: Path, conn, n_clips: int = 3, seconds: float = 3,
              status: str = "SCOUTED", pick_extra: dict | None = None,
              sizes: dict | None = None) -> dict:
    src = tmp_path / "src"
    workdir = tmp_path / "job"
    ing = workdir / "ingest"
    ing.mkdir(parents=True, exist_ok=True)
    sizes = sizes or {}
    clips, cands = [], []
    for i in range(n_clips):
        name = f"clip{i + 1}"
        f = make_clip(src / f"{name}.mp4", seconds, size=sizes.get(name, "640x360"))
        tr = ing / f"{name}.transcript.json"
        write_json(tr, {"language": "en", "duration": seconds,
                        "segments": [{"start": 0.2, "end": 3.7, "text": "...",
                                      "words": WORDS if i == 0 else []}]})
        clips.append({"file": f.name, "source": str(f), "proxy": str(f),
                      "transcript": str(tr), "duration": seconds})
        cands.append({"id": f"c{i + 1}", "clip": f.name, "source": str(f), "proxy": str(f),
                      "start": 0.0, "end": seconds, "duration": seconds,
                      "kind": "speech" if i == 0 else "visual",
                      "text": " ".join(w["word"] for w in WORDS) if i == 0 else "",
                      "score": 1 - i / 10, "reasons": [], "features": {}})
    write_json(workdir / "ingest.json", {"clips": clips, "total_seconds": seconds * n_clips})
    write_json(workdir / "scout" / "candidates.json", {"candidates": cands})
    pick = {"source": "local_draft", "order": [c["id"] for c in cands],
            "hook": "Small wins daily", "alt_hooks": [],
            "caption": "Kaisen is about small wins.", "hashtags": ["#kaizen", "#habits"],
            "why": "test", "trim": {"c1": {"start": 0.1, "end": seconds - 0.1}}}
    pick.update(pick_extra or {})
    write_json(workdir / "scout" / "pick.json", pick)
    job_id = db.create_job(conn, f"dir:{tmp_path.name}", src, workdir)
    set_status(conn, job_id, status)
    return {"id": job_id, "workdir": workdir, "src": src}


def set_status(conn, job_id: int, status: str) -> None:
    conn.execute("UPDATE jobs SET status=? WHERE id=?", (str(status), job_id))


def job(conn, job_id):
    return db.get_job(conn, job_id)
