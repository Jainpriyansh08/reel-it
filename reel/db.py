"""SQLite job table. Every state change is a compare-and-set, so a step can
only advance a job that is still in the state it started from."""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from .states import State

# Append-only. Each entry runs once, tracked by PRAGMA user_version.
MIGRATIONS = [
    """
    CREATE TABLE jobs (
        id          INTEGER PRIMARY KEY,
        key         TEXT NOT NULL UNIQUE,
        source_dir  TEXT NOT NULL,
        workdir     TEXT NOT NULL,
        status      TEXT NOT NULL,
        attempts    INTEGER NOT NULL DEFAULT 0,
        last_error  TEXT,
        stuck_at    TEXT,           -- state it was in when it went to NEEDS_HUMAN
        created_at  TEXT NOT NULL,
        updated_at  TEXT NOT NULL
    );
    CREATE TABLE events (
        id      INTEGER PRIMARY KEY,
        job_id  INTEGER REFERENCES jobs(id),
        ts      TEXT NOT NULL,
        kind    TEXT NOT NULL,      -- info | transition | error | alert
        message TEXT NOT NULL,
        sent    INTEGER NOT NULL DEFAULT 0   -- delivered to Telegram (week 3)
    );
    CREATE TABLE claude_calls (
        id        INTEGER PRIMARY KEY,
        ts        TEXT NOT NULL,
        job_id    INTEGER REFERENCES jobs(id),
        purpose   TEXT NOT NULL,
        ok        INTEGER NOT NULL,
        detail    TEXT
    );
    """,
]


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def connect(path: Path) -> sqlite3.Connection:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, isolation_level=None, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    migrate(conn)
    return conn


def migrate(conn: sqlite3.Connection) -> None:
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    for i, sql in enumerate(MIGRATIONS[version:], start=version + 1):
        conn.executescript(f"BEGIN; {sql}; PRAGMA user_version={i}; COMMIT;")


def log(conn, job_id: int | None, kind: str, message: str) -> None:
    conn.execute(
        "INSERT INTO events(job_id, ts, kind, message) VALUES (?,?,?,?)",
        (job_id, now(), kind, message),
    )


def create_job(conn, key: str, source_dir: Path, workdir: Path) -> int | None:
    """Insert a NEW job. Returns its id, or None if the key already exists."""
    ts = now()
    cur = conn.execute(
        "INSERT OR IGNORE INTO jobs(key, source_dir, workdir, status, created_at, updated_at)"
        " VALUES (?,?,?,?,?,?)",
        (key, str(source_dir), str(workdir), State.NEW, ts, ts),
    )
    if cur.rowcount == 0:
        return None
    log(conn, cur.lastrowid, "transition", f"created as {State.NEW}")
    return cur.lastrowid


def get_job(conn, job_id: int) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()


def jobs_in(conn, statuses) -> list[sqlite3.Row]:
    statuses = list(statuses)
    if not statuses:
        return []
    q = ",".join("?" * len(statuses))
    return conn.execute(
        f"SELECT * FROM jobs WHERE status IN ({q}) ORDER BY id", [str(s) for s in statuses]
    ).fetchall()


def transition(conn, job_id: int, frm: State, to: State, note: str = "") -> bool:
    """Move a job from `frm` to `to`. False if it was no longer in `frm`."""
    cur = conn.execute(
        "UPDATE jobs SET status=?, attempts=0, last_error=NULL, updated_at=?"
        " WHERE id=? AND status=?",
        (str(to), now(), job_id, str(frm)),
    )
    if cur.rowcount == 1:
        log(conn, job_id, "transition", f"{frm} -> {to}" + (f": {note}" if note else ""))
        return True
    return False


def record_failure(conn, job_id: int, frm: State, error: str, max_attempts: int) -> State:
    """Count a failed attempt. After max_attempts the job goes to NEEDS_HUMAN."""
    conn.execute(
        "UPDATE jobs SET attempts=attempts+1, last_error=?, updated_at=? WHERE id=? AND status=?",
        (error[-2000:], now(), job_id, str(frm)),
    )
    job = get_job(conn, job_id)
    summary = (error.strip().splitlines() or ["unknown error"])[0][:300]
    log(conn, job_id, "error", f"{frm} attempt {job['attempts']} failed: {summary}")
    if job["attempts"] >= max_attempts:
        conn.execute(
            "UPDATE jobs SET status=?, stuck_at=?, updated_at=? WHERE id=? AND status=?",
            (State.NEEDS_HUMAN, str(frm), now(), job_id, str(frm)),
        )
        log(conn, job_id, "alert", f"gave up at {frm} after {job['attempts']} attempts: {summary}")
        return State.NEEDS_HUMAN
    return frm


def retry(conn, job_id: int) -> bool:
    """Manual reset (e.g. after you fixed the footage): NEEDS_HUMAN -> the
    state it got stuck in, with a fresh attempt count."""
    job = get_job(conn, job_id)
    if not job or job["status"] != State.NEEDS_HUMAN or not job["stuck_at"]:
        return False
    return transition(conn, job_id, State.NEEDS_HUMAN, State(job["stuck_at"]), "manual retry")
