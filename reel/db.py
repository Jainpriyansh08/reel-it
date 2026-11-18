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
    # 2: everything weeks 2-6 need. Agents own their rows; see docs/CONTRACTS.md.
    """
    ALTER TABLE jobs ADD COLUMN edit_version INTEGER NOT NULL DEFAULT 0;
    ALTER TABLE jobs ADD COLUMN qa_failures INTEGER NOT NULL DEFAULT 0;
    ALTER TABLE jobs ADD COLUMN recuts INTEGER NOT NULL DEFAULT 0;
    ALTER TABLE jobs ADD COLUMN recut_reason TEXT;
    ALTER TABLE jobs ADD COLUMN state_entered_at TEXT;
    CREATE TABLE approvals (
        id            INTEGER PRIMARY KEY,
        job_id        INTEGER NOT NULL REFERENCES jobs(id),
        edit_version  INTEGER NOT NULL,
        caption_version INTEGER NOT NULL,
        file_sha256   TEXT NOT NULL,
        caption_sha256 TEXT NOT NULL,
        caption       TEXT NOT NULL,
        status        TEXT NOT NULL,   -- pending | approved | superseded | rejected | expired
        tg_message_id INTEGER,
        reminded_at   TEXT,
        created_at    TEXT NOT NULL,
        decided_at    TEXT
    );
    CREATE INDEX approvals_job ON approvals(job_id, status);
    CREATE TABLE publications (
        id            INTEGER PRIMARY KEY,
        job_id        INTEGER NOT NULL UNIQUE REFERENCES jobs(id),
        approval_id   INTEGER NOT NULL REFERENCES approvals(id),
        mode          TEXT NOT NULL,   -- manual | api
        container_id  TEXT,
        media_id      TEXT,
        permalink     TEXT,
        status        TEXT NOT NULL,   -- started | uploaded | published | failed
        published_at  TEXT,
        updated_at    TEXT NOT NULL
    );
    CREATE TABLE insights (
        id          INTEGER PRIMARY KEY,
        job_id      INTEGER REFERENCES jobs(id),   -- NULL for pre-pipeline baseline posts
        media_id    TEXT NOT NULL,
        checkpoint  TEXT NOT NULL,     -- 24h | 72h | 7d | baseline
        metrics     TEXT NOT NULL,     -- JSON
        fetched_at  TEXT NOT NULL,
        UNIQUE(media_id, checkpoint)
    );
    CREATE TABLE kv (
        key   TEXT PRIMARY KEY,
        value TEXT NOT NULL
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
        "UPDATE jobs SET status=?, attempts=0, last_error=NULL, updated_at=?, state_entered_at=?"
        " WHERE id=? AND status=?",
        (str(to), now(), now(), job_id, str(frm)),
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


def kv_get(conn, key: str, default: str | None = None) -> str | None:
    row = conn.execute("SELECT value FROM kv WHERE key=?", (key,)).fetchone()
    return row[0] if row else default


def kv_set(conn, key: str, value: str) -> None:
    conn.execute("INSERT INTO kv(key, value) VALUES (?,?) "
                 "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))


def alert(conn, job_id: int | None, message: str) -> None:
    """Something you must see. The Telegram bot delivers unsent alerts."""
    log(conn, job_id, "alert", message)


def give_up(conn, job_id: int, frm: State, message: str) -> bool:
    """Stop work on a job now: NEEDS_HUMAN (retryable from `frm`) + alert."""
    cur = conn.execute(
        "UPDATE jobs SET status=?, stuck_at=?, last_error=?, updated_at=?, state_entered_at=?"
        " WHERE id=? AND status=?",
        (State.NEEDS_HUMAN, str(frm), message, now(), now(), job_id, str(frm)),
    )
    if cur.rowcount == 1:
        alert(conn, job_id, f"needs you at {frm}: {message}")
        return True
    return False


def set_job(conn, job_id: int, **cols) -> None:
    """Update non-status bookkeeping columns (edit_version, qa_failures,
    recuts, recut_reason). Status changes must use transition()."""
    allowed = {"edit_version", "qa_failures", "recuts", "recut_reason"}
    bad = set(cols) - allowed
    if bad:
        raise ValueError(f"set_job cannot change {bad}")
    sets = ", ".join(f"{k}=?" for k in cols)
    conn.execute(f"UPDATE jobs SET {sets}, updated_at=? WHERE id=?", (*cols.values(), now(), job_id))


def is_paused(conn) -> bool:
    until = kv_get(conn, "paused_until")
    return bool(until) and until > now()
