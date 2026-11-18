from reel import db, scheduler
from reel.states import State

from .conftest import age


def touch_video(path, seconds_ago):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x")
    age(path, seconds_ago)
    return path


def ok_step(conn, job, cfg):
    pass


STEPS_OK = {State.NEW: (ok_step, State.INGESTED)}


def test_folder_becomes_job_only_after_it_settles(cfg, conn):
    v = touch_video(cfg.inbox / "2026-11-19" / "a.mp4", 5)
    assert scheduler.discover(conn, cfg) == []          # still copying
    age(v, 120)
    assert len(scheduler.discover(conn, cfg)) == 1
    assert scheduler.discover(conn, cfg) == []          # no duplicate job


def test_loose_files_are_filed_into_a_dated_folder(cfg, conn):
    touch_video(cfg.inbox / "clip.MOV", 120)
    touch_video(cfg.inbox / "notes.txt", 120)
    created = scheduler.discover(conn, cfg)
    assert len(created) == 1
    job = db.get_job(conn, created[0])
    assert job["key"].startswith("dir:20")
    assert (cfg.inbox / job["key"][4:] / "clip.MOV").exists()
    assert (cfg.inbox / "notes.txt").exists()            # non-video left alone


def test_tick_twice_changes_nothing_the_second_time(cfg, conn):
    touch_video(cfg.inbox / "d1" / "a.mp4", 120)
    r1 = scheduler.tick(cfg, conn, STEPS_OK)
    assert len(r1.created) == 1 and len(r1.advanced) == 1
    events = conn.execute("SELECT count(*) FROM events").fetchone()[0]
    r2 = scheduler.tick(cfg, conn, STEPS_OK)
    assert r2.created == [] and r2.advanced == [] and r2.failed == []
    assert conn.execute("SELECT count(*) FROM events").fetchone()[0] == events


def test_crash_mid_step_leaves_state_and_resumes(cfg, conn):
    touch_video(cfg.inbox / "d1" / "a.mp4", 120)
    calls = []

    def flaky(conn, job, cfg):
        calls.append(1)
        if len(calls) == 1:
            raise OSError("disk hiccup")

    steps = {State.NEW: (flaky, State.INGESTED)}
    r1 = scheduler.tick(cfg, conn, steps)
    assert r1.failed and db.get_job(conn, r1.created[0])["status"] == State.NEW
    r2 = scheduler.tick(cfg, conn, steps)
    assert r2.advanced[0][2] == State.INGESTED


def test_repeated_failure_goes_to_needs_human_and_stops(cfg, conn):
    touch_video(cfg.inbox / "d1" / "a.mp4", 120)

    def broken(conn, job, cfg):
        raise ValueError("unreadable footage")

    steps = {State.NEW: (broken, State.INGESTED)}
    for _ in range(cfg.max_attempts):
        scheduler.tick(cfg, conn, steps)
    job = conn.execute("SELECT * FROM jobs").fetchone()
    assert job["status"] == State.NEEDS_HUMAN
    assert "unreadable footage" in job["last_error"]
    r = scheduler.tick(cfg, conn, steps)                # no more attempts
    assert r.failed == [] and r.advanced == []


def test_overlapping_tick_is_locked_out(cfg, conn):
    with scheduler.tick_lock(cfg.work) as got:
        assert got
        assert scheduler.tick(cfg, conn, STEPS_OK).locked_out
    assert not scheduler.tick(cfg, conn, STEPS_OK).locked_out


def test_step_can_stay_or_redirect(cfg, conn):
    touch_video(cfg.inbox / "d1" / "a.mp4", 120)
    steps = {State.NEW: (lambda c, j, k: scheduler.STAY, State.INGESTED)}
    r = scheduler.tick(cfg, conn, steps)
    assert r.advanced == [] and r.failed == []
    assert conn.execute("SELECT status FROM jobs").fetchone()[0] == State.NEW
    steps = {State.NEW: (lambda c, j, k: State.QA_FAILED, State.INGESTED)}
    scheduler.tick(cfg, conn, steps)
    assert conn.execute("SELECT status FROM jobs").fetchone()[0] == State.QA_FAILED
