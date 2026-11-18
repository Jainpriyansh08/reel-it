"""`reel tick`: runs every 5 minutes (launchd). One tick:

1. takes a non-blocking lock, so overlapping ticks exit immediately;
2. discovers new footage in the inbox and creates NEW jobs;
3. runs at most one step for each job whose state has a handler.

Steps are idempotent and their outputs are written atomically, so a crash
anywhere leaves the job in its old state and the next tick simply re-runs it.
"""
from __future__ import annotations

import fcntl
import shutil
import time
import traceback
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable

from . import db
from .agents import ingest
from .config import VIDEO_EXTS, Config
from .states import State

# state -> (step function, default next state).
#
# A step is fn(conn, job, cfg) and returns:
#   None   -> move to the default next state
#   State  -> move to that state instead (e.g. QA_FAILED)
#   STAY   -> nothing to do yet (e.g. waiting for your tap); not a failure
# Raising records a failed attempt; after max_attempts -> NEEDS_HUMAN.
STAY = object()
Step = Callable[..., object]


def _ingest_step(conn, job, cfg: Config, backends=None) -> None:
    ingest.run(Path(job["source_dir"]), Path(job["workdir"]), cfg, backends)


STEPS: dict[State, tuple[Step, State]] = {
    State.NEW: (_ingest_step, State.INGESTED),
}

# fn(conn, cfg), run once per tick after the steps (deadline alerts,
# heartbeat, token health, insights...). Must be cheap and idempotent.
PERIODIC: list[Callable] = []


@dataclass
class TickResult:
    locked_out: bool = False
    created: list[int] = None
    advanced: list[tuple[int, str, str]] = None
    failed: list[tuple[int, str]] = None


@contextmanager
def tick_lock(work: Path):
    work.mkdir(parents=True, exist_ok=True)
    fh = open(work / ".tick.lock", "w")
    try:
        try:
            fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            yield False
            return
        yield True
    finally:
        fh.close()


def _settled(paths: list[Path], settle_seconds: int, now: float) -> bool:
    """True when nothing has been written for settle_seconds (copy finished)."""
    return all(now - p.stat().st_mtime >= settle_seconds for p in paths)


def _videos(d: Path) -> list[Path]:
    return [p for p in d.iterdir()
            if p.is_file() and p.suffix.lower() in VIDEO_EXTS and not p.name.startswith(".")]


def discover(conn, cfg: Config, now: float | None = None) -> list[int]:
    """Turn settled footage in the inbox into jobs.

    - A sub-folder of the inbox is one job (key: the folder name).
    - Loose videos dropped straight in the inbox are first moved into a
      folder named after their date, e.g. inbox/2026-11-19/.
    A folder becomes a job once nothing in it has changed for settle_seconds.
    """
    now = time.time() if now is None else now
    inbox = cfg.inbox
    inbox.mkdir(parents=True, exist_ok=True)

    for f in _videos(inbox):
        if _settled([f], cfg.settle_seconds, now):
            day = datetime.fromtimestamp(f.stat().st_mtime).strftime("%Y-%m-%d")
            dest_dir = inbox / day
            dest_dir.mkdir(exist_ok=True)
            dest = dest_dir / f.name
            if dest.exists():
                dest = dest_dir / f"{f.stem}-{int(f.stat().st_mtime)}{f.suffix}"
            shutil.move(str(f), str(dest))
            # moving keeps mtime, so the folder is ready on the same tick

    created = []
    for d in sorted(p for p in inbox.iterdir() if p.is_dir() and not p.name.startswith(".")):
        vids = _videos(d)
        if not vids or not _settled(vids, cfg.settle_seconds, now):
            continue
        key = f"dir:{d.name}"
        if conn.execute("SELECT 1 FROM jobs WHERE key=?", (key,)).fetchone():
            continue
        job_id = db.create_job(conn, key, d, cfg.work / "jobs" / d.name)
        if job_id:
            created.append(job_id)
    return created


def run_step(conn, job, cfg: Config, steps=None) -> tuple[bool, str]:
    steps = steps or STEPS
    frm = State(job["status"])
    fn, to = steps[frm]
    try:
        out = fn(conn, job, cfg)
    except Exception as e:  # noqa: BLE001 - every failure is recorded, none is silent
        err = f"{type(e).__name__}: {e}\n{traceback.format_exc(limit=5)}"
        new = db.record_failure(conn, job["id"], frm, err, cfg.max_attempts)
        return False, str(new)
    if out is STAY:
        return True, str(frm)
    to = out if isinstance(out, State) else to
    db.transition(conn, job["id"], frm, to)
    return True, str(to)


def tick(cfg: Config, conn=None, steps=None, now: float | None = None) -> TickResult:
    steps = steps or STEPS
    res = TickResult(created=[], advanced=[], failed=[])
    with tick_lock(cfg.work) as got:
        if not got:
            res.locked_out = True
            return res
        own = conn is None
        conn = conn or db.connect(cfg.db)
        try:
            res.created = [] if db.is_paused(conn) else discover(conn, cfg, now)
            for job in db.jobs_in(conn, steps.keys()):
                ok, new = run_step(conn, job, cfg, steps)
                if not ok:
                    res.failed.append((job["id"], new))
                elif new != job["status"]:
                    res.advanced.append((job["id"], job["status"], new))
            for hook in PERIODIC:
                try:
                    hook(conn, cfg)
                except Exception as e:  # noqa: BLE001
                    db.alert(conn, None, f"periodic task {hook.__name__} failed: {e}")
        finally:
            if own:
                conn.close()
    return res
