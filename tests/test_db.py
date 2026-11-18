from reel import db
from reel.states import State


def test_migrate_is_idempotent(conn):
    v = conn.execute("PRAGMA user_version").fetchone()[0]
    db.migrate(conn)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == v == len(db.MIGRATIONS)


def test_create_job_dedupes_on_key(conn, tmp_path):
    a = db.create_job(conn, "dir:x", tmp_path, tmp_path / "w")
    b = db.create_job(conn, "dir:x", tmp_path, tmp_path / "w")
    assert a and b is None
    assert db.get_job(conn, a)["status"] == State.NEW


def test_transition_is_compare_and_set(conn, tmp_path):
    j = db.create_job(conn, "dir:x", tmp_path, tmp_path)
    assert db.transition(conn, j, State.NEW, State.INGESTED)
    # a second, stale step that also started from NEW must not move it again
    assert not db.transition(conn, j, State.NEW, State.INGESTED)
    assert db.get_job(conn, j)["status"] == State.INGESTED


def test_failures_escalate_to_needs_human_and_retry_restores(conn, tmp_path):
    j = db.create_job(conn, "dir:x", tmp_path, tmp_path)
    assert db.record_failure(conn, j, State.NEW, "boom", 2) == State.NEW
    assert db.record_failure(conn, j, State.NEW, "boom", 2) == State.NEEDS_HUMAN
    job = db.get_job(conn, j)
    assert job["status"] == State.NEEDS_HUMAN and job["stuck_at"] == State.NEW
    alerts = conn.execute("SELECT * FROM events WHERE kind='alert'").fetchall()
    assert len(alerts) == 1  # never a silent failure

    assert db.retry(conn, j)
    job = db.get_job(conn, j)
    assert job["status"] == State.NEW and job["attempts"] == 0
    assert not db.retry(conn, j)  # only from NEEDS_HUMAN
