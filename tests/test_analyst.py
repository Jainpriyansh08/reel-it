import json
import subprocess
from datetime import datetime, timedelta, timezone

from reel import db, scheduler
from reel.agents import analyst
from reel.meta.graph import GraphClient
from reel.states import State
from reel.util import write_json
from tests.fixtures.meta.fakes import FakeSession
from tests.test_publisher import FakeTG

IG = "17841400000000000"
T0 = datetime(2026, 11, 10, 17, 0, tzinfo=timezone.utc)
MONDAY = datetime(2026, 11, 16, 9, 0).astimezone()  # playbook_weekday 0


def add_reel(conn, tmp_path, n, views, media_id=None, checkpoints=("24h", "72h"),
             status=State.MEASURED, published_at=T0, mode="api", caption=None):
    wd = tmp_path / "work" / "jobs" / f"d{n}"
    job_id = db.create_job(conn, f"dir:d{n}", tmp_path / "inbox" / f"d{n}", wd)
    conn.execute("UPDATE jobs SET status=?, edit_version=1, recuts=? WHERE id=?",
                 (status, n % 2, job_id))
    d = wd / "edit" / "v1"
    write_json(d / "package.json", {"caption_version": 1, "caption": f"Caption {n}",
                                    "hashtags": ["#a", "#b"], "full_caption": caption or f"Caption {n}",
                                    "hook": f"Hook {n}", "source": "claude"})
    write_json(d / "edit.json", {"candidate_id": "c1", "start": 10.0, "end": 32.0, "speed": 1.0})
    write_json(wd / "scout" / "candidates.json", {"candidates": [{"id": "c1", "kind": "speech"}]})
    cur = conn.execute(
        "INSERT INTO approvals(job_id, edit_version, caption_version, file_sha256, caption_sha256,"
        " caption, status, created_at, decided_at) VALUES (?,?,?,?,?,?,?,?,?)",
        (job_id, 1, 1, "f", "c", caption or f"Caption {n}", "approved", db.now(), db.now()))
    conn.execute("INSERT INTO publications(job_id, approval_id, mode, media_id, permalink, status,"
                 " published_at, updated_at) VALUES (?,?,?,?,?,?,?,?)",
                 (job_id, cur.lastrowid, mode, media_id, f"https://ig/r{n}", "published",
                  published_at.isoformat(timespec="seconds"), db.now()))
    for cp in checkpoints if media_id else ():
        conn.execute("INSERT INTO insights(job_id, media_id, checkpoint, metrics, fetched_at)"
                     " VALUES (?,?,?,?,?)", (job_id, media_id, cp,
                                             json.dumps({"views": views, "saved": n}), db.now()))
    return job_id


def runner(payload, code=0):
    calls = []

    def run(cmd, stdin, timeout):
        calls.append(stdin)
        return subprocess.CompletedProcess(cmd, code, stdout=json.dumps(payload), stderr="")
    run.calls = calls
    return run


GOOD = {"structured_output": {"playbook_markdown": "# Playbook\n- Hooks under 6 words",
                              "proposed_change": "Post at 08:00 instead of 18:00",
                              "confidence": "evidence"}}


# -- checkpoints ------------------------------------------------------------------
def test_checkpoints_stored_once_then_measured(conn, cfg, tmp_path):
    job_id = add_reel(conn, tmp_path, 1, 0, media_id="18000000000000042", checkpoints=(),
                      status=State.PUBLISHED)
    s = FakeSession().route("GET", "/insights", "insights")
    g = GraphClient("T", IG, session=s)
    step = lambda h: analyst.published_step(conn, db.get_job(conn, job_id), cfg, graph=g,  # noqa
                                            now=T0 + timedelta(hours=h))
    assert step(1) is scheduler.STAY and s.calls == []
    assert step(25) is scheduler.STAY
    assert step(30) is scheduler.STAY
    assert len(s.calls) == 1
    assert step(80) is scheduler.STAY
    assert step(170) is None
    assert len(s.calls) == 3
    rows = conn.execute("SELECT checkpoint, metrics FROM insights WHERE job_id=?"
                        " ORDER BY id", (job_id,)).fetchall()
    assert [r[0] for r in rows] == ["24h", "72h", "168h"]
    assert json.loads(rows[0][1])["views"] == 1520


def test_late_tick_fills_all_due_checkpoints(conn, cfg, tmp_path):
    job_id = add_reel(conn, tmp_path, 1, 0, media_id="m1", checkpoints=(), status=State.PUBLISHED)
    s = FakeSession().route("GET", "/insights", "insights")
    out = analyst.published_step(conn, db.get_job(conn, job_id), cfg,
                                 graph=GraphClient("T", IG, session=s), now=T0 + timedelta(days=8))
    assert out is None and len(s.calls) == 3


def test_manual_without_client_is_measured(conn, cfg, tmp_path, monkeypatch):
    job_id = add_reel(conn, tmp_path, 1, 0, status=State.PUBLISHED, mode="manual")
    monkeypatch.setattr(analyst, "_get_graph", lambda cfg, g: None)
    assert analyst.published_step(conn, db.get_job(conn, job_id), cfg) is None
    assert conn.execute("SELECT count(*) FROM insights").fetchone()[0] == 0


def test_manual_post_matched_from_recent_media(conn, cfg, tmp_path):
    pub_at = datetime(2026, 11, 18, 18, 0, tzinfo=timezone.utc)
    job_id = add_reel(conn, tmp_path, 1, 0, status=State.PUBLISHED, mode="manual",
                      published_at=pub_at,
                      caption="Ship small, ship daily.\n\n#kaizen #habits")
    s = (FakeSession().route("GET", f"{IG}/media", "recent_media")
         .route("GET", "/insights", "insights"))
    g = GraphClient("T", IG, session=s)
    out = analyst.published_step(conn, db.get_job(conn, job_id), cfg, graph=g,
                                 now=pub_at + timedelta(hours=2))
    assert out is scheduler.STAY
    row = conn.execute("SELECT media_id FROM publications WHERE job_id=?", (job_id,)).fetchone()
    assert row[0] == "18000000000000042"


def test_manual_post_not_found_gives_up_after_7_days(conn, cfg, tmp_path):
    job_id = add_reel(conn, tmp_path, 1, 0, status=State.PUBLISHED, mode="manual",
                      caption="Something never posted")
    s = FakeSession().route("GET", f"{IG}/media", "recent_media")
    g = GraphClient("T", IG, session=s)
    job = db.get_job(conn, job_id)
    assert analyst.published_step(conn, job, cfg, graph=g, now=T0 + timedelta(days=1)) \
        is scheduler.STAY
    assert analyst.published_step(conn, job, cfg, graph=g, now=T0 + timedelta(days=8)) is None


# -- features ---------------------------------------------------------------------
def test_features(conn, cfg, tmp_path):
    add_reel(conn, tmp_path, 1, 500, media_id="m1")
    add_reel(conn, tmp_path, 2, 0)  # no media id -> no row
    (f,) = analyst.features(conn)
    assert f["hook"] == "Hook 1" and f["hashtags"] == 2 and f["caption_len"] == len("Caption 1")
    assert f["duration"] == 22.0 and f["kind"] == "speech" and f["source"] == "claude"
    assert f["views_72h"] == 500 and f["views_168h"] is None
    assert f["hour"] is not None and f["weekday"]


# -- weekly playbook -------------------------------------------------------------
def test_weekly_playbook_writes_file_and_sends_digest(conn, cfg, tmp_path):
    for i, v in enumerate([100, 900, 300], start=1):
        add_reel(conn, tmp_path, i, v, media_id=f"m{i}")
    cfg.playbook_path.parent.mkdir(parents=True, exist_ok=True)
    cfg.playbook_path.write_text("# old playbook\n")
    tg, run = FakeTG(), runner(GOOD)
    analyst.weekly_playbook(conn, cfg, tg=tg, runner=run, now=MONDAY)
    assert len(run.calls) == 1 and "Hook 2" in run.calls[0] and "# old playbook" in run.calls[0]
    text = cfg.playbook_path.read_text()
    assert "Hooks under 6 words" in text and "confidence: hypothesis" in text  # < 10 posts
    assert (cfg.playbook_path.parent / "playbook.prev.md").read_text() == "# old playbook\n"
    (msg,) = tg.sent
    body = msg[1]
    assert "Best: 900 views" in body and "Weakest: 100 views" in body
    assert "Post at 08:00" in body and "(hypothesis)" in body
    assert "First-cut approval rate: 3/3" in body
    row = conn.execute("SELECT purpose, ok FROM claude_calls").fetchone()
    assert tuple(row) == ("weekly_playbook", 1)
    # once per ISO week
    analyst.weekly_playbook(conn, cfg, tg=tg, runner=run, now=MONDAY + timedelta(hours=3))
    assert len(run.calls) == 1 and len(tg.sent) == 1


def test_weekly_playbook_skips_with_fewer_than_3(conn, cfg, tmp_path):
    add_reel(conn, tmp_path, 1, 100, media_id="m1")
    add_reel(conn, tmp_path, 2, 200, media_id="m2", checkpoints=("24h",))  # not measured yet
    tg, run = FakeTG(), runner(GOOD)
    analyst.weekly_playbook(conn, cfg, tg=tg, runner=run, now=MONDAY)
    assert run.calls == [] and tg.sent == [] and not cfg.playbook_path.exists()


def test_weekly_playbook_wrong_weekday(conn, cfg, tmp_path):
    for i in range(1, 4):
        add_reel(conn, tmp_path, i, 100 * i, media_id=f"m{i}")
    tg, run = FakeTG(), runner(GOOD)
    analyst.weekly_playbook(conn, cfg, tg=tg, runner=run, now=MONDAY + timedelta(days=1))
    assert run.calls == [] and tg.sent == []


def test_claude_failure_still_sends_digest(conn, cfg, tmp_path):
    for i in range(1, 4):
        add_reel(conn, tmp_path, i, 100 * i, media_id=f"m{i}")
    tg, run = FakeTG(), runner({"result": "rate limited", "is_error": True})
    analyst.weekly_playbook(conn, cfg, tg=tg, runner=run, now=MONDAY)
    assert not cfg.playbook_path.exists()
    (msg,) = tg.sent
    assert "Playbook not updated" in msg[1] and "Best: 300 views" in msg[1]
    alerts = [r[0] for r in conn.execute("SELECT message FROM events WHERE kind='alert'")]
    assert any("Claude unavailable" in a for a in alerts)


# -- baseline -------------------------------------------------------------------------
def test_baseline_stores_reels_only_with_null_job(conn, cfg):
    s = (FakeSession().route("GET", f"{IG}/media", "recent_media")
         .route("GET", "/insights", "insights"))
    n = analyst.baseline(conn, cfg, GraphClient("T", IG, session=s))
    assert n == 2
    rows = conn.execute("SELECT job_id, media_id, checkpoint FROM insights ORDER BY id").fetchall()
    assert [tuple(r) for r in rows] == [(None, "18000000000000042", "baseline"),
                                        (None, "18000000000000040", "baseline")]
    assert analyst.baseline(conn, cfg, GraphClient("T", IG, session=s)) == 0  # idempotent


def test_steps_export():
    assert analyst.STEPS[State.PUBLISHED][1] == State.MEASURED
    assert analyst.PERIODIC == [analyst.weekly_playbook]
