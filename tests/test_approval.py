from datetime import datetime, timedelta, timezone

import pytest

from reel import db, scheduler
from reel.agents import approval
from reel.states import State
from reel.telegram.api import TelegramError
from reel.util import edit_dir, read_json, write_json

CHAT = 42


class FakeAPI:
    def __init__(self):
        self.calls = []
        self.next_id = 100
        self.fail = False

    def _rec(self, name, **kw):
        if self.fail:
            raise TelegramError(f"{name}: boom")
        self.calls.append((name, kw))
        self.next_id += 1
        return self.next_id

    def send_message(self, text, buttons=None, chat_id=None):
        return self._rec("send_message", text=text, buttons=buttons, chat_id=chat_id)

    def send_video(self, path, caption="", buttons=None):
        return self._rec("send_video", path=str(path), caption=caption, buttons=buttons)

    def send_document(self, path, caption="", buttons=None):
        return self._rec("send_document", path=str(path), caption=caption, buttons=buttons)

    def edit_buttons(self, message_id, buttons=None):
        self._rec("edit_buttons", message_id=message_id, buttons=buttons)

    def get_updates(self, offset, timeout=50):
        return []

    def answer_callback(self, callback_id, text=""):
        self._rec("answer_callback", callback_id=callback_id, text=text)

    def named(self, name):
        return [kw for n, kw in self.calls if n == name]

    def texts(self):
        return [kw.get("text") or kw.get("caption") for n, kw in self.calls
                if n in ("send_message", "send_video")]


@pytest.fixture
def tcfg(cfg, tmp_path):
    cfg.telegram["chat_id"] = CHAT
    cfg.brand_path = tmp_path / "brand.yaml"
    return cfg


@pytest.fixture
def api():
    return FakeAPI()


def make_job(conn, cfg, status=State.QA_PASSED, source="claude", key="dir:2026-11-18",
             hashtags=("#a", "#b", "#c", "#d", "#e"), caption="First line. Second sentence here."):
    wd = cfg.work / "jobs" / key.split(":")[1]
    job_id = db.create_job(conn, key, cfg.inbox / "x", wd)
    db.set_job(conn, job_id, edit_version=1)
    conn.execute("UPDATE jobs SET status=? WHERE id=?", (str(status), job_id))
    v = edit_dir(wd, 1)
    v.mkdir(parents=True)
    (v / "final.mp4").write_bytes(b"final-video")
    (v / "preview.mp4").write_bytes(b"preview-video")
    write_json(v / "edit.json", {"start": 10.0, "end": 38.5, "speed": 1.0})
    write_json(v / "package.json", {
        "caption_version": 1, "caption": caption, "hashtags": list(hashtags),
        "full_caption": approval.full_caption(caption, hashtags), "hook": "Stop doing this",
        "cover": str(v / "cover.jpg"), "source": source})
    write_json(v / "qa.json", {"status": "warn", "checks": [
        {"name": "resolution", "status": "pass", "detail": "1080x1920"},
        {"name": "low_conf_words", "status": "warn", "detail": "2 low-confidence words: kaisen, gemba"}]})
    write_json(wd / "scout" / "pick.json", {"source": source, "order": ["c1"], "hook": "Stop doing this",
                                            "alt_hooks": ["Hook two", "Hook three"], "caption": caption,
                                            "hashtags": list(hashtags), "why": "best energy"})
    return job_id


def run(conn, cfg, job_id, api, now=None):
    """One scheduler step with the fake API injected."""
    steps = {State.QA_PASSED: (lambda c, j, f: approval.request_approval(c, j, f, api=api),
                               State.AWAITING_APPROVAL),
             State.AWAITING_APPROVAL: (lambda c, j, f: approval.await_approval(c, j, f, api=api, now=now),
                                       State.AWAITING_APPROVAL)}
    return scheduler.run_step(conn, db.get_job(conn, job_id), cfg, steps)


def test_send_preview_creates_approval_and_buttons(conn, tcfg, api):
    j = make_job(conn, tcfg)
    assert run(conn, tcfg, j, api) == (True, "AWAITING_APPROVAL")
    ap = approval.pending_approval(conn, j)
    assert ap["edit_version"] == 1 and ap["caption_version"] == 1 and ap["tg_message_id"] == 101
    (video,) = api.named("send_video")
    assert video["path"].endswith("preview.mp4")
    cap = video["caption"]
    assert "Stop doing this" in cap and "First line." in cap and "#a #b" in cap
    assert "best energy" in cap and "28.5s" in cap and "kaisen, gemba" in cap
    assert "LOCAL DRAFT" not in cap
    flat = [d for row in video["buttons"] for _, d in row]
    assert flat == [f"ap:{j}:{ap['id']}", f"ed:{j}:{ap['id']}", f"rc:{j}:{ap['id']}", f"rj:{j}:{ap['id']}"]


def test_preview_is_idempotent(conn, tcfg, api):
    j = make_job(conn, tcfg)
    job = db.get_job(conn, j)
    a1 = approval.send_preview(conn, job, tcfg, api)
    a2 = approval.send_preview(conn, job, tcfg, api)
    assert a1 == a2 and len(api.named("send_video")) == 1
    assert conn.execute("SELECT count(*) FROM approvals").fetchone()[0] == 1


def test_local_draft_banner_and_needs_caption_hides_approve(conn, tcfg, api):
    j = make_job(conn, tcfg, source="local_draft")
    run(conn, tcfg, j, api)
    assert "LOCAL DRAFT" in api.named("send_video")[0]["caption"]
    j2 = make_job(conn, tcfg, source="needs_caption", key="dir:2026-11-19")
    run(conn, tcfg, j2, api)
    v = api.named("send_video")[1]
    assert "write a caption" in v["caption"]
    assert not any(d.startswith("ap:") for row in v["buttons"] for _, d in row)


def test_unconfigured_telegram_raises(conn, tcfg):
    tcfg.telegram["chat_id"] = 0
    j = make_job(conn, tcfg)
    ok, st = scheduler.run_step(conn, db.get_job(conn, j), tcfg, approval.STEPS)
    assert not ok and st == "QA_PASSED"
    assert "not configured" in db.get_job(conn, j)["last_error"]


def _age_approval(conn, job_id, hours):
    ts = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat(timespec="seconds")
    conn.execute("UPDATE approvals SET created_at=? WHERE job_id=?", (ts, job_id))


def test_reminder_once(conn, tcfg, api):
    j = make_job(conn, tcfg)
    run(conn, tcfg, j, api)
    assert run(conn, tcfg, j, api) == (True, "AWAITING_APPROVAL")
    assert len(api.named("send_message")) == 0
    _age_approval(conn, j, 5)
    run(conn, tcfg, j, api)
    run(conn, tcfg, j, api)
    reminders = [t for t in api.texts() if t.startswith("⏰")]
    assert len(reminders) == 1
    assert approval.pending_approval(conn, j)["reminded_at"]


def test_expiry_never_approves(conn, tcfg, api):
    j = make_job(conn, tcfg)
    run(conn, tcfg, j, api)
    _age_approval(conn, j, 21)
    assert run(conn, tcfg, j, api) == (True, "EXPIRED_APPROVAL")
    assert conn.execute("SELECT status FROM approvals").fetchone()[0] == "expired"
    msg = api.named("send_message")[-1]
    assert "Skipped today" in msg["text"] and msg["buttons"] == [[("♻️ Revive", f"rv:{j}")]]
    assert db.get_job(conn, j)["status"] == "EXPIRED_APPROVAL"


def test_await_resends_when_no_pending(conn, tcfg, api):
    j = make_job(conn, tcfg, status=State.AWAITING_APPROVAL)
    run(conn, tcfg, j, api)
    assert len(api.named("send_video")) == 1 and approval.pending_approval(conn, j)


def test_alerts_delivered_once_and_marked_only_on_success(conn, tcfg, api):
    db.alert(conn, None, "disk almost full")
    api.fail = True
    assert approval.deliver_alerts(conn, tcfg, api=api) == 0
    assert conn.execute("SELECT sent FROM events WHERE kind='alert'").fetchone()[0] == 0
    api.fail = False
    assert approval.deliver_alerts(conn, tcfg, api=api) == 1
    assert approval.deliver_alerts(conn, tcfg, api=api) == 0
    assert [t for t in api.texts() if "disk almost full" in t] == ["⚠️ disk almost full"]


def _local(h, m=0):
    return datetime.now().astimezone().replace(hour=h, minute=m, second=0, microsecond=0)


def test_footage_deadline_alert_once(conn, tcfg):
    assert not approval.footage_deadline_check(conn, tcfg, now=_local(9, 59))
    assert approval.footage_deadline_check(conn, tcfg, now=_local(10, 1))
    assert not approval.footage_deadline_check(conn, tcfg, now=_local(11))
    rows = conn.execute("SELECT message FROM events WHERE kind='alert'").fetchall()
    assert len(rows) == 1 and "No footage by 10:00" in rows[0][0]


def test_footage_deadline_quiet_when_job_today(conn, tcfg):
    make_job(conn, tcfg)
    assert not approval.footage_deadline_check(conn, tcfg, now=_local(10, 1))


def test_pause_blocks_deadline_and_heartbeat(conn, tcfg, api):
    until = (datetime.now(timezone.utc) + timedelta(days=1)).isoformat(timespec="seconds")
    db.kv_set(conn, "paused_until", until)
    assert not approval.footage_deadline_check(conn, tcfg, now=_local(11))
    assert not approval.heartbeat(conn, tcfg, api=api, now=_local(22))
    assert api.calls == []


def test_heartbeat_once_a_day(conn, tcfg, api):
    make_job(conn, tcfg)
    assert not approval.heartbeat(conn, tcfg, api=api, now=_local(20))
    assert approval.heartbeat(conn, tcfg, api=api, now=_local(21, 5))
    assert not approval.heartbeat(conn, tcfg, api=api, now=_local(23))
    (msg,) = api.named("send_message")
    assert "QA_PASSED" in msg["text"] and "Claude calls today: 0" in msg["text"]


def test_steps_and_periodic_exported():
    assert set(approval.STEPS) == {State.QA_PASSED, State.AWAITING_APPROVAL}
    assert approval.PERIODIC == [approval.deliver_alerts, approval.footage_deadline_check,
                                 approval.heartbeat]
