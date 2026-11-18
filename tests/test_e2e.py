"""One day of footage through every agent with the real scheduler registry,
real FFmpeg, and fakes only at the edges (Whisper, scene detection, Claude,
Telegram). Proves the agents compose and nothing posts without approval."""
import json
import subprocess

import pytest

import reel.agents.approval as approval_mod
import reel.telegram.api as tg_api
from reel import db, scheduler
from reel.agents import ingest
from reel.llm import claude
from reel.states import State
from reel.telegram.bot import Bot
from reel.util import edit_dir, sha256_file

from .conftest import age, make_clip
from .test_approval import FakeAPI

CHAT = 42
SENTENCES = [
    (0.2, 2.8, "Most people get this wrong."),
    (3.0, 5.6, "Here is the one habit that fixed it."),
    (5.9, 8.6, "Try Kaizen for one week."),
    (8.9, 11.5, "Small steps beat big plans."),
]


def transcript(_audio, _settings, _vocab):
    segs = []
    for s, e, text in SENTENCES:
        words, toks = [], text.split()
        step = (e - s) / len(toks)
        for i, w in enumerate(toks):
            words.append({"start": round(s + i * step, 3), "end": round(s + (i + 1) * step - 0.02, 3),
                          "word": w, "p": 0.95})
        segs.append({"start": s, "end": e, "text": text, "words": words})
    return {"language": "en", "duration": 12.0, "segments": segs}


class Edges:
    def __init__(self):
        self.tg = FakeAPI()
        self.claude_prompts = []

    def claude_runner(self, cmd, stdin, timeout):
        self.claude_prompts.append(stdin)
        ids = sorted(set(__import__("re").findall(r"\bc\d+\b", stdin))) or ["c1"]
        out = {"order": ids, "hook": "The habit that fixed it",
               "alt_hooks": ["Small steps win", "Try this for a week"],
               "caption": "One small habit, every day.", "hashtags": ["#kaizen", "#habits", "#growth"],
               "why": "complete thought with a clear payoff"}
        return subprocess.CompletedProcess(cmd, 0, stdout=json.dumps({"structured_output": out}), stderr="")


@pytest.fixture
def edges(cfg, monkeypatch, tmp_path):
    e = Edges()
    cfg.telegram["chat_id"] = CHAT
    cfg.brand_path = tmp_path / "brand.yaml"
    cfg.reel.update({"min_seconds": 3, "target_seconds": 6, "max_seconds": 9, "prerender_alternates": 1})
    monkeypatch.setattr(ingest, "default_backends",
                        lambda: ingest.Backends(transcribe=transcript, detect_scenes=lambda v: []))
    monkeypatch.setattr(claude, "_default_runner", e.claude_runner)
    monkeypatch.setattr(tg_api, "from_config", lambda cfg, session=None: e.tg)
    monkeypatch.setattr(approval_mod, "from_config", lambda cfg, session=None: e.tg)
    return e


def run_until(cfg, conn, job_id, wanted, max_ticks=12):
    for _ in range(max_ticks):
        scheduler.tick(cfg, conn)
        if db.get_job(conn, job_id)["status"] == wanted:
            return
    job = db.get_job(conn, job_id)
    events = [r["message"] for r in conn.execute("SELECT message FROM events ORDER BY id")]
    raise AssertionError(f"stuck at {job['status']}: {job['last_error']}\n" + "\n".join(events[-15:]))


def tap(bot, data, msg_id=1):
    bot.handle({"update_id": 1, "callback_query": {
        "id": "cb", "data": data, "from": {"id": CHAT},
        "message": {"message_id": msg_id, "chat": {"id": CHAT}}}})


def test_footage_to_published(cfg, conn, edges):
    clip = make_clip(cfg.inbox / "2026-11-19" / "talk.mp4", 12, size="1280x720")
    age(clip, 3600)

    scheduler.tick(cfg, conn)
    job_id = conn.execute("SELECT id FROM jobs").fetchone()[0]
    run_until(cfg, conn, job_id, State.AWAITING_APPROVAL)

    job = db.get_job(conn, job_id)
    assert job["edit_version"] == 1
    v1 = edit_dir(job["workdir"], 1)
    probe = json.loads(subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=width,height",
         "-of", "json", str(v1 / "final.mp4")], capture_output=True, text=True).stdout)["streams"][0]
    assert (probe["width"], probe["height"]) == (1080, 1920)
    assert json.loads((v1 / "qa.json").read_text())["status"] in ("pass", "warn")
    assert len(edges.claude_prompts) == 1                    # exactly one Claude call
    previews = edges.tg.named("send_video")
    assert len(previews) == 1 and "The habit that fixed it" in previews[0]["caption"]

    # Nothing is published while waiting, however many ticks pass.
    for _ in range(3):
        scheduler.tick(cfg, conn)
    assert db.get_job(conn, job_id)["status"] == State.AWAITING_APPROVAL
    assert conn.execute("SELECT count(*) FROM publications").fetchone()[0] == 0

    # Recut: different clip -> a new version and a fresh preview.
    bot = Bot(cfg, edges.tg, conn)
    ap1 = conn.execute("SELECT id FROM approvals WHERE status='pending'").fetchone()[0]
    tap(bot, f"rr:{job_id}:{ap1}:dc")
    assert db.get_job(conn, job_id)["status"] == State.RECUT_REQUESTED
    run_until(cfg, conn, job_id, State.AWAITING_APPROVAL)
    assert db.get_job(conn, job_id)["edit_version"] == 2
    assert len(edges.tg.named("send_video")) == 2

    # The old approval can no longer be used.
    tap(bot, f"ap:{job_id}:{ap1}")
    assert db.get_job(conn, job_id)["status"] == State.AWAITING_APPROVAL

    # Approve the current version: tied to the exact file and caption.
    ap2 = conn.execute("SELECT * FROM approvals WHERE status='pending'").fetchone()
    assert ap2["file_sha256"] == sha256_file(edit_dir(job["workdir"], 2) / "final.mp4")
    tap(bot, f"ap:{job_id}:{ap2['id']}")
    tap(bot, f"ap:{job_id}:{ap2['id']}")                    # double tap is harmless
    assert db.get_job(conn, job_id)["status"] == State.APPROVED

    # Manual mode: the file + caption are handed over once, then we wait.
    scheduler.tick(cfg, conn)
    scheduler.tick(cfg, conn)
    handed = edges.tg.named("send_document") + edges.tg.named("send_video")[2:]
    assert len(handed) == 1
    assert db.get_job(conn, job_id)["status"] == State.APPROVED

    tap(bot, f"posted:{job_id}")
    assert db.get_job(conn, job_id)["status"] == State.PUBLISHED
    pub = conn.execute("SELECT * FROM publications").fetchone()
    assert pub["status"] == "published" and pub["approval_id"] == ap2["id"]

    # No silent failures anywhere on the way.
    assert conn.execute("SELECT count(*) FROM events WHERE kind='error'").fetchone()[0] == 0


def test_claude_down_still_reaches_your_phone(cfg, conn, edges, monkeypatch):
    def down(cmd, stdin, timeout):
        return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="rate limited")
    monkeypatch.setattr(claude, "_default_runner", down)
    clip = make_clip(cfg.inbox / "d" / "talk.mp4", 12, size="1280x720")
    age(clip, 3600)
    scheduler.tick(cfg, conn)
    job_id = conn.execute("SELECT id FROM jobs").fetchone()[0]
    run_until(cfg, conn, job_id, State.AWAITING_APPROVAL)
    texts = " ".join(t or "" for t in edges.tg.texts())
    assert "Claude" in texts                                 # you're told why
    pending = conn.execute("SELECT * FROM approvals WHERE status='pending'").fetchone()
    buttons = edges.tg.named("send_video")[-1]["buttons"]
    flat = [d for row in buttons for _, d in row]
    assert not any(d.startswith(f"ap:{job_id}:{pending['id']}") for d in flat)  # no caption -> no Approve yet
