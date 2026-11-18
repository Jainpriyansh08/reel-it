import json
from datetime import datetime, timezone

import yaml

from reel import db
from reel.agents import approval
from reel.states import State
from reel.telegram import bot as botmod
from reel.util import edit_dir, read_json, write_json

from tests.test_approval import CHAT, FakeAPI, api, make_job, run, tcfg  # noqa: F401 (fixtures)


class UpdatesAPI(FakeAPI):
    def __init__(self, batches=()):
        super().__init__()
        self.batches = list(batches)
        self.offsets = []

    def get_updates(self, offset, timeout=50):
        self.offsets.append(offset)
        return self.batches.pop(0) if self.batches else []


_uid = [0]


def cb(data, chat=CHAT, msg_id=101):
    _uid[0] += 1
    return {"update_id": _uid[0], "callback_query": {
        "id": f"cq{_uid[0]}", "data": data, "from": {"id": chat},
        "message": {"message_id": msg_id, "chat": {"id": chat}}}}


def msg(text, chat=CHAT):
    _uid[0] += 1
    return {"update_id": _uid[0], "message": {"message_id": 5, "chat": {"id": chat}, "text": text}}


def setup_awaiting(conn, cfg, api, **kw):
    j = make_job(conn, cfg, **kw)
    run(conn, cfg, j, api)
    return j, approval.pending_approval(conn, j)["id"]


def handle(conn, cfg, api, *updates):
    b = botmod.Bot(cfg, api, conn)
    for u in updates:
        b.handle(u)
    return b


def status(conn, j):
    return db.get_job(conn, j)["status"]


def test_approve_happy_path(conn, tcfg, api):
    j, a = setup_awaiting(conn, tcfg, api)
    handle(conn, tcfg, api, cb(f"ap:{j}:{a}"))
    assert status(conn, j) == "APPROVED"
    row = conn.execute("SELECT * FROM approvals WHERE id=?", (a,)).fetchone()
    assert row["status"] == "approved" and row["decided_at"]
    assert {"message_id": 101, "buttons": None} in api.named("edit_buttons")


def test_double_approve_harmless(conn, tcfg, api):
    j, a = setup_awaiting(conn, tcfg, api)
    handle(conn, tcfg, api, cb(f"ap:{j}:{a}"), cb(f"ap:{j}:{a}"))
    assert status(conn, j) == "APPROVED"
    assert conn.execute("SELECT count(*) FROM events WHERE message LIKE '%-> APPROVED%'").fetchone()[0] == 1
    assert api.named("answer_callback")[-1]["text"].startswith("Already approved")


def test_approve_after_file_changed_refused(conn, tcfg, api):
    j, a = setup_awaiting(conn, tcfg, api)
    job = db.get_job(conn, j)
    (edit_dir(job["workdir"], 1) / "final.mp4").write_bytes(b"re-rendered")
    handle(conn, tcfg, api, cb(f"ap:{j}:{a}"))
    assert status(conn, j) == "QA_PASSED"
    assert conn.execute("SELECT status FROM approvals WHERE id=?", (a,)).fetchone()[0] == "superseded"
    assert any("This version changed" in t for t in api.texts())
    # the scheduler step sends the new version with a fresh approval
    run(conn, tcfg, j, api)
    new = approval.pending_approval(conn, j)
    assert new["id"] != a and status(conn, j) == "AWAITING_APPROVAL"


def test_approve_after_caption_changed_on_disk_refused(conn, tcfg, api):
    j, a = setup_awaiting(conn, tcfg, api)
    p = edit_dir(db.get_job(conn, j)["workdir"], 1) / "package.json"
    pkg = read_json(p)
    pkg["full_caption"] = "sneaky"
    write_json(p, pkg)
    handle(conn, tcfg, api, cb(f"ap:{j}:{a}"))
    assert status(conn, j) == "QA_PASSED"


def test_stale_approval_button_refused(conn, tcfg, api):
    j, a = setup_awaiting(conn, tcfg, api)
    handle(conn, tcfg, api, cb(f"ef:{j}:{a}"))
    handle(conn, tcfg, api, cb(f"ap:{j}:{a}"))
    assert status(conn, j) == "AWAITING_APPROVAL"


def test_caption_fewer_hashtags_bumps_version(conn, tcfg, api):
    j, a = setup_awaiting(conn, tcfg, api)
    handle(conn, tcfg, api, cb(f"ed:{j}:{a}"))
    assert any(kw["buttons"] and kw["buttons"][0][0][1] == f"ew:{j}:{a}" for kw in api.named("edit_buttons"))
    handle(conn, tcfg, api, cb(f"ef:{j}:{a}"))
    pkg = approval.read_package(db.get_job(conn, j))
    assert pkg["caption_version"] == 2 and pkg["hashtags"] == ["#a", "#b", "#c"]
    assert pkg["full_caption"].endswith("#a #b #c")
    old = conn.execute("SELECT status FROM approvals WHERE id=?", (a,)).fetchone()[0]
    new = approval.pending_approval(conn, j)
    assert old == "superseded" and new["caption_version"] == 2 and new["tg_message_id"]
    assert status(conn, j) == "AWAITING_APPROVAL"
    assert len(api.named("send_video")) == 2
    # the new one can be approved
    handle(conn, tcfg, api, cb(f"ap:{j}:{new['id']}", msg_id=new["tg_message_id"]))
    assert status(conn, j) == "APPROVED"


def test_caption_shorter_and_hook(conn, tcfg, api):
    j, a = setup_awaiting(conn, tcfg, api)
    handle(conn, tcfg, api, cb(f"es:{j}:{a}"))
    job = db.get_job(conn, j)
    assert approval.read_package(job)["caption"] == "First line."
    a2 = approval.pending_approval(conn, j)["id"]
    handle(conn, tcfg, api, cb(f"eh:{j}:{a2}"))
    pkg = approval.read_package(job)
    assert pkg["caption"] == "Hook two\nFirst line." and pkg["hook"] == "Stop doing this"
    a3 = approval.pending_approval(conn, j)["id"]
    handle(conn, tcfg, api, cb(f"eh:{j}:{a3}"))
    assert approval.read_package(job)["caption"] == "Hook three\nFirst line."
    assert approval.read_package(job)["caption_version"] == 4


def test_write_new_caption_unlocks_approve_for_needs_caption(conn, tcfg, api):
    j, a = setup_awaiting(conn, tcfg, api, source="needs_caption")
    handle(conn, tcfg, api, cb(f"ew:{j}:{a}"), msg("My own words #kaizen #daily"))
    pkg = approval.read_package(db.get_job(conn, j))
    assert pkg["caption"] == "My own words" and pkg["hashtags"] == ["#kaizen", "#daily"]
    assert pkg["caption_version"] == 2
    v = api.named("send_video")[-1]
    new = approval.pending_approval(conn, j)
    assert ("✅ Approve", f"ap:{j}:{new['id']}") in v["buttons"][0]
    assert "write a caption" not in v["caption"]


def test_recut_sets_reason_and_state(conn, tcfg, api):
    j, a = setup_awaiting(conn, tcfg, api)
    handle(conn, tcfg, api, cb(f"rc:{j}:{a}"), cb(f"rr:{j}:{a}:ti"), cb(f"rr:{j}:{a}:ti"))
    job = db.get_job(conn, j)
    assert job["status"] == "RECUT_REQUESTED" and job["recuts"] == 1
    assert json.loads(job["recut_reason"]) == {"reason": "tighter"}
    assert conn.execute("SELECT status FROM approvals WHERE id=?", (a,)).fetchone()[0] == "superseded"
    assert any("Recutting" in t for t in api.texts())


def test_recut_limit_enforced(conn, tcfg, api):
    j, a = setup_awaiting(conn, tcfg, api)
    db.set_job(conn, j, recuts=3)
    handle(conn, tcfg, api, cb(f"rc:{j}:{a}"), cb(f"rr:{j}:{a}:dc"))
    assert status(conn, j) == "AWAITING_APPROVAL"
    assert any("limit" in t for t in api.texts())


def test_fix_captions_with_vocab_offer(conn, tcfg, api):
    j, a = setup_awaiting(conn, tcfg, api)
    tcfg.brand_path.write_text(yaml.safe_dump({"font": "Montserrat", "vocab": ["Kaizen"]}))
    handle(conn, tcfg, api, cb(f"rr:{j}:{a}:fc"), msg("kaisen -> Kaizen, gemba → Gemba"))
    job = db.get_job(conn, j)
    assert job["status"] == "RECUT_REQUESTED"
    assert json.loads(job["recut_reason"]) == {"reason": "fix_captions",
                                               "word_fixes": {"kaisen": "Kaizen", "gemba": "Gemba"}}
    offer = api.named("send_message")[-1]
    assert offer["buttons"] == [[("➕ Gemba", f"va:{j}:Gemba")]]
    handle(conn, tcfg, api, cb(f"va:{j}:Gemba"))
    data = yaml.safe_load(tcfg.brand_path.read_text())
    assert data == {"font": "Montserrat", "vocab": ["Kaizen", "Gemba"]}


def test_reject(conn, tcfg, api):
    j, a = setup_awaiting(conn, tcfg, api)
    handle(conn, tcfg, api, cb(f"rj:{j}:{a}"), cb(f"ap:{j}:{a}"))
    assert status(conn, j) == "REJECTED"
    assert conn.execute("SELECT status FROM approvals WHERE id=?", (a,)).fetchone()[0] == "rejected"


def test_revive(conn, tcfg, api):
    j = make_job(conn, tcfg, status=State.EXPIRED_APPROVAL)
    handle(conn, tcfg, api, cb(f"rv:{j}"), cb(f"rv:{j}"))
    assert status(conn, j) == "QA_PASSED"


def test_other_chats_ignored(conn, tcfg, api):
    j, a = setup_awaiting(conn, tcfg, api)
    n = len(api.calls)
    handle(conn, tcfg, api, cb(f"ap:{j}:{a}", chat=999), msg("/pause 3d", chat=999))
    assert status(conn, j) == "AWAITING_APPROVAL" and len(api.calls) == n
    assert not db.is_paused(conn)


def test_whoami_any_chat(conn, tcfg, api):
    handle(conn, tcfg, api, msg("/whoami", chat=999))
    (m,) = api.named("send_message")
    assert m["chat_id"] == 999 and "999" in m["text"]


def test_vocab_and_ban_commands(conn, tcfg, api):
    tcfg.brand_path.write_text(yaml.safe_dump({"voice": {"adjectives": ["warm"]}, "vocab": [],
                                               "banned_words": [], "font": "Montserrat"}))
    handle(conn, tcfg, api, msg("/vocab add Kaizen"), msg("/vocab add kaizen"), msg("/ban add hack"),
           msg("/vocab list"))
    data = yaml.safe_load(tcfg.brand_path.read_text())
    assert data["vocab"] == ["Kaizen"] and data["banned_words"] == ["hack"]
    assert data["voice"] == {"adjectives": ["warm"]} and data["font"] == "Montserrat"
    assert tcfg.brand["vocab"] == ["Kaizen"]
    handle(conn, tcfg, api, msg("/vocab remove KAIZEN"))
    assert yaml.safe_load(tcfg.brand_path.read_text())["vocab"] == []


def test_pause_blocks_deadline_alert(conn, tcfg, api):
    handle(conn, tcfg, api, msg("/pause 3d"))
    assert db.is_paused(conn)
    late = datetime.now().astimezone().replace(hour=11, minute=0)
    assert not approval.footage_deadline_check(conn, tcfg, now=late)
    handle(conn, tcfg, api, msg("/pause off"))
    assert not db.is_paused(conn)
    assert approval.footage_deadline_check(conn, tcfg, now=late)


def test_retry_command(conn, tcfg, api):
    j = make_job(conn, tcfg, status=State.NEEDS_HUMAN)
    conn.execute("UPDATE jobs SET stuck_at='SCOUTED' WHERE id=?", (j,))
    handle(conn, tcfg, api, msg(f"/retry {j}"))
    assert status(conn, j) == "SCOUTED"


def test_status_command(conn, tcfg, api):
    make_job(conn, tcfg)
    handle(conn, tcfg, api, msg("/status"))
    assert "QA_PASSED" in api.named("send_message")[-1]["text"]


def test_manual_posted_flow(conn, tcfg, api):
    j, a = setup_awaiting(conn, tcfg, api)
    handle(conn, tcfg, api, cb(f"ap:{j}:{a}"), cb(f"posted:{j}"), cb(f"posted:{j}"))
    assert status(conn, j) == "PUBLISHED"
    pub = conn.execute("SELECT * FROM publications WHERE job_id=?", (j,)).fetchone()
    assert pub["status"] == "published" and pub["published_at"] and pub["approval_id"] == a
    assert pub["mode"] == "manual"
    handle(conn, tcfg, api, msg("here https://www.instagram.com/reel/ABC123/ thanks"))
    pub = conn.execute("SELECT * FROM publications WHERE job_id=?", (j,)).fetchone()
    assert pub["permalink"] == "https://www.instagram.com/reel/ABC123/"


def test_retry_publish(conn, tcfg, api):
    j = make_job(conn, tcfg, status=State.PUBLISH_FAILED)
    handle(conn, tcfg, api, cb(f"retry:{j}"))
    assert status(conn, j) == "APPROVED"


def test_run_bot_once_persists_offset_and_survives_bad_update(conn, tcfg):
    j = make_job(conn, tcfg, status=State.EXPIRED_APPROVAL)
    bad = {"update_id": 500, "callback_query": {"id": "x", "data": "ap:notanint:zz:",
                                                "message": {"message_id": 1, "chat": {"id": CHAT}}}}
    boom = {"update_id": 501, "message": {"chat": {"id": CHAT}, "text": "/vocab add X"}}  # no brand_path
    tcfg.brand_path = None
    api = UpdatesAPI([[bad, boom, cb(f"rv:{j}") | {"update_id": 502}]])
    botmod.run_bot(tcfg, api=api, conn=conn, once=True)
    assert status(conn, j) == "QA_PASSED"
    assert db.kv_get(conn, "tg_offset") == "503"
    assert conn.execute("SELECT count(*) FROM events WHERE kind='error' AND message LIKE 'bot update 501%'"
                        ).fetchone()[0] == 1
    botmod.run_bot(tcfg, api=api, conn=conn, once=True)
    assert api.offsets == [0, 503]


def test_whoami_cli(capsys, tcfg):
    api = UpdatesAPI([[msg("hi", chat=777)]])
    assert botmod.whoami(tcfg, api=api) == 0
    assert "chat_id 777" in capsys.readouterr().out


def test_parse_word_fixes():
    assert botmod.parse_word_fixes("kaisen -> Kaizen, gonna → going to\nx=y") == {
        "kaisen": "Kaizen", "gonna": "going to", "x": "y"}
