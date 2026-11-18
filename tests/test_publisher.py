import json
from datetime import datetime

import pytest

from reel import db, scheduler
from reel.agents import publisher
from reel.meta.graph import GraphClient, TransientGraphError
from reel.states import State
from reel.util import sha256_file, sha256_text, write_json
from tests.fixtures.meta.fakes import FakeSession

IG = "17841400000000000"
CAPTION = "Ship small, ship daily.\n\n#kaizen #habits"
AFTER = datetime(2026, 11, 18, 19, 0).astimezone()
BEFORE = datetime(2026, 11, 18, 10, 0).astimezone()


class FakeTG:
    def __init__(self):
        self.sent = []

    def send_message(self, text, buttons=None):
        self.sent.append(("message", text, buttons))
        return len(self.sent)

    def send_video(self, path, caption="", buttons=None):
        self.sent.append(("video", path, buttons))
        return len(self.sent)

    def send_document(self, path, caption="", buttons=None):
        self.sent.append(("document", path, buttons))
        return len(self.sent)


def make_job(conn, cfg, tmp_path, mode="api", decided_at="2026-11-18T09:00:00+00:00"):
    # replace, don't mutate: Config shares nested default dicts
    cfg.publish = {**cfg.publish, "mode": mode, "ig_user_id": IG}
    wd = tmp_path / "work" / "jobs" / "d1"
    job_id = db.create_job(conn, "dir:d1", tmp_path / "inbox" / "d1", wd)
    conn.execute("UPDATE jobs SET status=?, edit_version=1, state_entered_at=? WHERE id=?",
                 (State.APPROVED, db.now(), job_id))
    d = wd / "edit" / "v1"
    d.mkdir(parents=True)
    (d / "final.mp4").write_bytes(b"\x00video" * 100)
    write_json(d / "package.json", {"caption_version": 1, "caption": "Ship small, ship daily.",
                                    "hashtags": ["#kaizen", "#habits"], "full_caption": CAPTION,
                                    "hook": "Stop waiting", "source": "claude"})
    conn.execute(
        "INSERT INTO approvals(job_id, edit_version, caption_version, file_sha256, caption_sha256,"
        " caption, status, created_at, decided_at) VALUES (?,?,?,?,?,?,?,?,?)",
        (job_id, 1, 1, sha256_file(d / "final.mp4"), sha256_text(CAPTION), CAPTION, "approved",
         "2026-11-18T08:00:00+00:00", decided_at))
    return job_id


def job_row(conn, job_id):
    return db.get_job(conn, job_id)


def pub(conn, job_id):
    return conn.execute("SELECT * FROM publications WHERE job_id=?", (job_id,)).fetchone()


def happy_session(conn=None, job_id=None, order=None):
    """Routes for the whole API flow; records the publications row at each call."""
    s = FakeSession()

    def snap(call):
        if order is not None:
            row = pub(conn, job_id)
            order.append((call["method"], call["url"].rsplit("/", 1)[-1],
                          row["container_id"] if row else None, row["status"] if row else None,
                          row["media_id"] if row else None))
        return True
    s.route("POST", f"{IG}/media_publish", "media_publish", match=snap)
    s.route("POST", f"{IG}/media", "container_create", match=snap)
    s.route("GET", f"{IG}/media", {"data": []}, match=snap)
    s.route("POST", "rupload.facebook.com", "rupload_success", match=snap)
    s.route("GET", "/17900000000000001", "container_in_progress", "container_finished", match=snap)
    s.route("GET", "/18000000000000042", "media_permalink", match=snap)
    return s


def run(conn, cfg, job_id, tg, graph, now=AFTER):
    return publisher.approved_step(conn, job_row(conn, job_id), cfg, tg=tg, graph=graph,
                                   now=now, sleep=lambda s: None)


def test_hash_mismatch_never_publishes(conn, cfg, tmp_path):
    job_id = make_job(conn, cfg, tmp_path)
    (tmp_path / "work/jobs/d1/edit/v1/final.mp4").write_bytes(b"tampered")
    s, tg = FakeSession(), FakeTG()
    assert run(conn, cfg, job_id, tg, GraphClient("T", IG, session=s)) is scheduler.STAY
    assert s.calls == [] and tg.sent == []
    assert job_row(conn, job_id)["status"] == State.NEEDS_HUMAN
    assert "approved file changed" in job_row(conn, job_id)["last_error"]
    assert pub(conn, job_id) is None


def test_caption_change_never_publishes_manual(conn, cfg, tmp_path):
    job_id = make_job(conn, cfg, tmp_path, mode="manual")
    p = tmp_path / "work/jobs/d1/edit/v1/package.json"
    data = json.loads(p.read_text())
    data["full_caption"] = "edited after approval"
    write_json(p, data)
    tg = FakeTG()
    assert run(conn, cfg, job_id, tg, None) is scheduler.STAY
    assert tg.sent == [] and job_row(conn, job_id)["status"] == State.NEEDS_HUMAN


def test_manual_sends_once_and_stays(conn, cfg, tmp_path):
    job_id = make_job(conn, cfg, tmp_path, mode="manual")
    tg = FakeTG()
    assert run(conn, cfg, job_id, tg, None) is scheduler.STAY
    kinds = [s[0] for s in tg.sent]
    assert kinds == ["video", "message"]
    assert tg.sent[1][1] == CAPTION
    assert tg.sent[1][2] == [[("✅ Posted", f"posted:{job_id}")]]
    row = pub(conn, job_id)
    assert row["mode"] == "manual" and row["status"] == "started"
    assert run(conn, cfg, job_id, tg, None) is scheduler.STAY
    assert len(tg.sent) == 2  # never resent


def test_manual_big_file_goes_as_document(conn, cfg, tmp_path, monkeypatch):
    monkeypatch.setattr(publisher, "TELEGRAM_VIDEO_LIMIT", 10)
    job_id = make_job(conn, cfg, tmp_path, mode="manual")
    tg = FakeTG()
    run(conn, cfg, job_id, tg, None)
    assert tg.sent[0][0] == "document"


def test_api_before_post_time_stays(conn, cfg, tmp_path):
    job_id = make_job(conn, cfg, tmp_path)
    s = FakeSession()
    assert run(conn, cfg, job_id, FakeTG(), GraphClient("T", IG, session=s), now=BEFORE) \
        is scheduler.STAY
    assert s.calls == [] and pub(conn, job_id) is None


def test_api_happy_path_saves_ids_in_order(conn, cfg, tmp_path):
    job_id = make_job(conn, cfg, tmp_path)
    order, tg = [], FakeTG()
    s = happy_session(conn, job_id, order)
    out = run(conn, cfg, job_id, tg, GraphClient("T", IG, session=s))
    assert out is None
    steps = [(m, tail) for m, tail, *_ in order]
    assert steps == [("POST", "media"), ("POST", "17900000000000001"),
                     ("GET", "17900000000000001"), ("GET", "17900000000000001"),
                     ("POST", "media_publish"), ("GET", "18000000000000042")]
    # row existed before the first API call; container saved before upload,
    # marked uploaded before polling, media id saved before the permalink fetch
    assert order[0][2:] == (None, "started", None)
    assert order[1][2:] == ("17900000000000001", "started", None)
    assert order[2][3] == "uploaded"
    assert order[4][4] is None
    assert order[5][4] == "18000000000000042"
    row = pub(conn, job_id)
    assert row["status"] == "published" and row["media_id"] == "18000000000000042"
    assert row["permalink"] == "https://www.instagram.com/reel/ABC123xyz/"
    assert row["published_at"]
    assert tg.sent == [("message", "Posted ✅ https://www.instagram.com/reel/ABC123xyz/", None)]
    create = s.find("POST", f"{IG}/media")[0]
    assert create["data"]["caption"] == CAPTION and create["data"]["media_type"] == "REELS"


def test_api_runs_through_scheduler(conn, cfg, tmp_path, monkeypatch):
    job_id = make_job(conn, cfg, tmp_path)
    s = happy_session()
    monkeypatch.setattr(publisher, "_get_graph", lambda cfg, g: GraphClient("T", IG, session=s))
    monkeypatch.setattr(publisher, "_get_tg", lambda cfg, t: FakeTG())
    monkeypatch.setattr(publisher, "_post_time_reached", lambda cfg, now: True)
    monkeypatch.setattr(publisher.time, "sleep", lambda s: None)
    ok, new = scheduler.run_step(conn, job_row(conn, job_id), cfg, publisher.STEPS)
    assert ok and new == State.PUBLISHED


def test_crash_after_publish_adopts_instead_of_double_posting(conn, cfg, tmp_path):
    job_id = make_job(conn, cfg, tmp_path)
    # state left by a crash right after media_publish succeeded
    conn.execute("INSERT INTO publications(job_id, approval_id, mode, container_id, status,"
                 " updated_at) VALUES (?,?,?,?,?,?)",
                 (job_id, 1, "api", "17900000000000001", "uploaded", db.now()))
    s = (FakeSession()
         .route("POST", "media_publish", AssertionError("must not publish twice"))
         .route("POST", f"{IG}/media", AssertionError("must not create a container"))
         .route("GET", f"{IG}/media", "recent_media")
         .route("GET", "/18000000000000042", "media_permalink"))
    tg = FakeTG()
    assert run(conn, cfg, job_id, tg, GraphClient("T", IG, session=s)) is None
    row = pub(conn, job_id)
    assert row["media_id"] == "18000000000000042" and row["status"] == "published"
    assert not s.find("POST", "media_publish")
    assert "Posted ✅" in tg.sent[0][1]


def test_recovery_ignores_old_post_with_same_caption(conn, cfg, tmp_path):
    # approval decided after the matching Reel's timestamp -> not ours, publish normally
    job_id = make_job(conn, cfg, tmp_path, decided_at="2026-11-18T17:30:00+00:00")
    conn.execute("INSERT INTO publications(job_id, approval_id, mode, status, updated_at)"
                 " VALUES (?,?,?,?,?)", (job_id, 1, "api", "started", db.now()))
    s = happy_session()
    s.routes.insert(0, ["GET", f"{IG}/media", ["recent_media"], None])
    assert run(conn, cfg, job_id, FakeTG(), GraphClient("T", IG, session=s)) is None
    assert len(s.find("POST", "media_publish")) == 1


def test_media_id_saved_is_done_without_api_publish(conn, cfg, tmp_path):
    job_id = make_job(conn, cfg, tmp_path)
    conn.execute("INSERT INTO publications(job_id, approval_id, mode, container_id, media_id,"
                 " status, updated_at) VALUES (?,?,?,?,?,?,?)",
                 (job_id, 1, "api", "179", "18000000000000042", "uploaded", db.now()))
    s = FakeSession().route("GET", "/18000000000000042", "media_permalink")
    assert run(conn, cfg, job_id, FakeTG(), GraphClient("T", IG, session=s)) is None
    assert [c["method"] for c in s.calls] == ["GET"]


def test_still_processing_stays_then_publishes(conn, cfg, tmp_path, monkeypatch):
    monkeypatch.setattr(publisher, "POLL_ATTEMPTS", 2)
    job_id = make_job(conn, cfg, tmp_path)
    s = happy_session()
    s.routes = [r for r in s.routes if "17900000000000001" not in r[1]]
    s.route("GET", "/17900000000000001", "container_in_progress", "container_in_progress",
            "container_finished")
    g = GraphClient("T", IG, session=s)
    assert run(conn, cfg, job_id, FakeTG(), g) is scheduler.STAY
    assert pub(conn, job_id)["status"] == "uploaded"
    assert run(conn, cfg, job_id, FakeTG(), g) is None
    assert len(s.find("POST", f"{IG}/media_publish")) == 1
    assert len(s.find("POST", "rupload")) == 1


def test_transient_error_raises(conn, cfg, tmp_path):
    job_id = make_job(conn, cfg, tmp_path)
    s = FakeSession().route("POST", f"{IG}/media", (400, "error_rate_limit"))
    with pytest.raises(TransientGraphError):
        run(conn, cfg, job_id, FakeTG(), GraphClient("T", IG, session=s))


def test_permanent_error_goes_to_publish_failed_with_alert(conn, cfg, tmp_path):
    job_id = make_job(conn, cfg, tmp_path)
    s = FakeSession().route("POST", f"{IG}/media", (400, "error_permission"))
    out = run(conn, cfg, job_id, FakeTG(), GraphClient("T", IG, session=s))
    assert out == State.PUBLISH_FAILED
    assert pub(conn, job_id)["status"] == "failed"
    alerts = [r[0] for r in conn.execute("SELECT message FROM events WHERE kind='alert'")]
    assert any("Publish failed" in a and "permission" in a for a in alerts)


def test_container_error_goes_to_publish_failed(conn, cfg, tmp_path):
    job_id = make_job(conn, cfg, tmp_path)
    s = happy_session()
    s.routes = [r for r in s.routes if "17900000000000001" not in r[1]]
    s.route("GET", "/17900000000000001", "container_error")
    out = run(conn, cfg, job_id, FakeTG(), GraphClient("T", IG, session=s))
    assert out == State.PUBLISH_FAILED
    assert not s.find("POST", "media_publish")


def test_publish_failed_notifies_once(conn, cfg, tmp_path):
    job_id = make_job(conn, cfg, tmp_path)
    conn.execute("UPDATE jobs SET status=? WHERE id=?", (State.PUBLISH_FAILED, job_id))
    db.kv_set(conn, f"publish_error:{job_id}", "Graph error 10/2207050: no permission")
    tg = FakeTG()
    for _ in range(2):
        assert publisher.publish_failed_step(conn, job_row(conn, job_id), cfg, tg=tg) \
            is scheduler.STAY
    assert len(tg.sent) == 1
    _, text, buttons = tg.sent[0]
    assert text.startswith("Publish failed: Graph error 10")
    assert buttons == [[("🔁 Retry", f"retry:{job_id}")]]


def test_token_health_check_daily(conn, cfg):
    cfg.publish = {**cfg.publish, "mode": "api"}
    s = FakeSession().route("GET", IG, (400, "error_token"))
    g = GraphClient("T", IG, session=s)
    publisher.token_health_check(conn, cfg, graph=g)
    publisher.token_health_check(conn, cfg, graph=g)
    assert len(s.calls) == 1
    alerts = [r[0] for r in conn.execute("SELECT message FROM events WHERE kind='alert'")]
    assert alerts and alerts[0].startswith("Instagram token invalid:")


def test_token_health_skipped_in_manual_mode(conn, cfg):
    s = FakeSession()
    publisher.token_health_check(conn, cfg, graph=GraphClient("T", IG, session=s))
    assert s.calls == []


def test_steps_export():
    assert publisher.STEPS[State.APPROVED][1] == State.PUBLISHED
    assert publisher.STEPS[State.PUBLISH_FAILED][1] == State.APPROVED
    assert publisher.token_health_check in publisher.PERIODIC
