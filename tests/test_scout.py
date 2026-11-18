import json
import subprocess
import wave
from pathlib import Path

import numpy as np
import pytest

from reel import db
from reel.agents import scout
from reel.states import State
from reel.util import read_json, write_json
from tests.conftest import make_clip


# ---------------------------------------------------------------- helpers

def words_for(text, start, step=0.4, p=0.9, low=()):
    out, t = [], start
    for w in text.split():
        out.append({"start": round(t, 2), "end": round(t + step - 0.05, 2), "word": w,
                    "p": 0.3 if w.strip(".,?!").lower() in low else p})
        t += step
    return out


def transcript(sentences, duration, low=()):
    """sentences: [(start, text)] -> one segment per sentence."""
    segs = []
    for start, text in sentences:
        ws = words_for(text, start, low=low)
        segs.append({"start": ws[0]["start"], "end": ws[-1]["end"], "text": " " + text, "words": ws})
    return {"language": "en", "duration": duration, "segments": segs}


def make_wav(path, seconds, amp=0.3):
    sr = 16000
    t = np.arange(int(sr * seconds)) / sr
    data = (amp * 32767 * np.sin(2 * np.pi * 220 * t)).astype(np.int16)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(data.tobytes())
    return path


_CLIPS: dict = {}


@pytest.fixture(scope="session", autouse=True)
def _clip_dir(tmp_path_factory):
    _CLIPS["dir"] = tmp_path_factory.mktemp("scout_clips")


def cached_clip(seconds):
    """Rendering test clips is the slow part; render each length once per session."""
    path = _CLIPS["dir"] / f"{seconds}s.mp4"
    if not path.exists():
        make_clip(path, seconds=seconds, size="320x180", audio=False)
    return path


def add_clip(workdir, name, seconds, tr=None, scenes=None, audio=True):
    ing = workdir / "ingest"
    ing.mkdir(parents=True, exist_ok=True)
    stem = Path(name).stem
    src = workdir / "src" / name
    src.parent.mkdir(parents=True, exist_ok=True)
    src.symlink_to(cached_clip(seconds))
    wav = make_wav(ing / f"{stem}.wav", seconds) if audio else None
    tr = tr or {"language": None, "duration": seconds, "segments": []}
    write_json(ing / f"{stem}.transcript.json", tr)
    write_json(ing / f"{stem}.scenes.json", scenes or [{"start": 0.0, "end": float(seconds)}])
    return {"file": name, "source": str(src), "duration": float(seconds), "width": 320,
            "height": 180, "fps": 30.0, "has_audio": audio, "proxy": str(src),
            "audio": str(wav) if wav else None,
            "transcript": str(ing / f"{stem}.transcript.json"),
            "scenes": str(ing / f"{stem}.scenes.json")}


def write_ingest(workdir, clips):
    write_json(workdir / "ingest.json", {"source_dir": str(workdir / "src"), "clips": clips,
                                         "skipped": [],
                                         "total_seconds": sum(c["duration"] for c in clips)})


TALK = [
    (0.5, "So here is the thing nobody tells you about mornings."),
    (5.0, "You do not need more discipline, you need a better first step."),
    (10.5, "Um I used to uh fight my alarm every single day."),
    (16.0, "Then I started putting my shoes next to the bed."),
    (21.0, "That one tiny change made getting up feel automatic."),
    (26.5, "Try it tomorrow and tell me how it goes."),
    (31.0, "Small steps beat big plans every time."),
    (35.0, "See you in the next one."),
]


@pytest.fixture
def job(conn, tmp_path):
    wd = tmp_path / "job"
    wd.mkdir()
    jid = db.create_job(conn, "dir:test", tmp_path / "src", wd)
    db.transition(conn, jid, State.NEW, State.INGESTED)
    return db.get_job(conn, jid)


@pytest.fixture
def talk_job(job):
    wd = Path(job["workdir"])
    write_ingest(wd, [add_clip(wd, "talk.mp4", 40, transcript(TALK, 40.0, low=("uh",)))])
    return job


def fake_runner(payload=None, code=0):
    calls = []

    def run(cmd, stdin, timeout):
        calls.append((cmd, stdin))
        out = payload(stdin) if callable(payload) else payload
        return subprocess.CompletedProcess(cmd, code, stdout=json.dumps(out), stderr="")
    run.calls = calls
    return run


def good_pick(stdin):
    ids = [line.split('"id": "')[1].split('"')[0] for line in stdin.splitlines() if '"id": "' in line]
    return {"structured_output": {
        "order": ["zzz-unknown", ids[-1], ids[0]],
        "hook": "Your shoes are the secret",
        "alt_hooks": ["Stop fighting your alarm", "One tiny morning change"],
        "caption": "The easiest way to get up early is not discipline.",
        "hashtags": ["#mornings", "#habits", "#productivity"],
        "why": "Clear story with a payoff.",
        "trim": [{"id": ids[0], "start": 0.0, "end": 1.0}],
    }, "is_error": False}


def alerts(conn):
    return [r["message"] for r in conn.execute("SELECT message FROM events WHERE kind='alert'")]


# ---------------------------------------------------------------- windows

def test_speech_windows_snap_to_sentences_and_respect_limits(talk_job, cfg):
    wd = Path(talk_job["workdir"])
    cands = scout.build_candidates(wd, cfg)
    assert cands
    starts = {w["start"] for _, t in TALK for w in words_for(t, _)[:1]}
    ends = {words_for(t, s)[-1]["end"] for s, t in TALK}
    for c in cands:
        assert c["kind"] == "speech"
        assert cfg.reel["min_seconds"] <= c["duration"] <= cfg.reel["max_seconds"]
        # starts ~0.15 s before a sentence's first word, ends ~0.15 s after its last
        assert any(abs(c["start"] - (s - 0.15)) < 0.02 or c["start"] == 0.0 and s < 0.15
                   for s in starts), c["start"]
        assert any(abs(c["end"] - (e + 0.15)) < 0.02 for e in ends), c["end"]
        assert c["text"][0].isupper()
        assert 0.0 <= c["score"] <= 1.0 and c["reasons"]
    assert [c["id"] for c in cands] == [f"c{i}" for i in range(1, len(cands) + 1)]
    assert [c["score"] for c in cands] == sorted((c["score"] for c in cands), reverse=True)
    assert len(cands) <= cfg.reel["candidates"]


def test_no_overlapping_duplicates(talk_job, cfg):
    cands = scout.build_candidates(Path(talk_job["workdir"]), cfg)
    for i, a in enumerate(cands):
        for b in cands[i + 1:]:
            if a["clip"] == b["clip"]:
                assert scout.iou(a, b) <= 0.5


def test_long_sentence_without_punctuation_is_split(job, cfg):
    wd = Path(job["workdir"])
    text = " ".join(["word"] * 200)  # 80 s of speech, no punctuation
    write_ingest(wd, [add_clip(wd, "ramble.mp4", 85, transcript([(1.0, text)], 85.0))])
    cands = scout.build_candidates(wd, cfg)
    assert cands and all(c["duration"] <= cfg.reel["max_seconds"] for c in cands)


def test_no_speech_clip_yields_visual_candidates(job, cfg):
    wd = Path(job["workdir"])
    scenes = [{"start": 0.0, "end": 12.0}, {"start": 12.0, "end": 30.0}, {"start": 30.0, "end": 45.0}]
    write_ingest(wd, [add_clip(wd, "broll.mp4", 45, scenes=scenes, audio=False)])
    cands = scout.build_candidates(wd, cfg)
    assert cands and all(c["kind"] == "visual" for c in cands)
    for c in cands:
        assert cfg.reel["min_seconds"] <= c["duration"] <= cfg.reel["max_seconds"]
        assert c["text"] == ""
        assert set(c["features"]) >= {"sharpness", "motion", "faces", "energy"}


def test_short_day_yields_whole_candidate_trimmed_of_silence(job, cfg):
    wd = Path(job["workdir"])
    tr = transcript([(2.0, "Quick tip for today."), (4.0, "Drink water first.")], 12.0)
    write_ingest(wd, [add_clip(wd, "short.mp4", 12, tr)])
    cands = scout.build_candidates(wd, cfg)
    assert len(cands) == 1
    c = cands[0]
    assert c["kind"] == "whole"
    assert c["start"] == pytest.approx(2.0 - 0.15, abs=0.01)
    last_end = tr["segments"][-1]["words"][-1]["end"]
    assert c["end"] == pytest.approx(last_end + 0.15, abs=0.01)
    assert "Quick tip" in c["text"]


def test_short_day_two_clips_one_whole_each(job, cfg):
    wd = Path(job["workdir"])
    write_ingest(wd, [add_clip(wd, "a.mp4", 8), add_clip(wd, "b.mp4", 9, audio=False)])
    cands = scout.build_candidates(wd, cfg)
    assert sorted(c["clip"] for c in cands) == ["a.mp4", "b.mp4"]
    assert all(c["kind"] == "whole" for c in cands)
    assert {c["duration"] for c in cands} == {8.0, 9.0}


# ---------------------------------------------------------------- pick

def test_claude_success_writes_validated_pick(talk_job, conn, cfg):
    cfg.brand = {**cfg.brand, "voice": {"adjectives": ["direct", "warm"], "sample_captions": []}}
    run = fake_runner(good_pick)
    assert scout.step(conn, talk_job, cfg, runner=run) is None
    wd = Path(talk_job["workdir"])
    cands = read_json(wd / "scout" / "candidates.json")["candidates"]
    pick = read_json(wd / "scout" / "pick.json")
    ids = [c["id"] for c in cands]
    assert pick["source"] == "claude"
    assert "zzz-unknown" not in pick["order"]
    assert pick["order"][:2] == [ids[-1], ids[0]]
    assert sorted(pick["order"]) == sorted(ids)
    assert pick["hook"] == "Your shoes are the secret"
    assert pick["hashtags"] == ["#mornings", "#habits", "#productivity"]
    assert pick["trim"] == {}  # 0..1 s is shorter than min_seconds -> dropped
    _, prompt = run.calls[0]
    assert "direct" in prompt and ids[0] in prompt
    assert not alerts(conn)


def test_trim_within_window_is_kept(talk_job, conn, cfg):
    def payload(stdin):
        out = good_pick(stdin)
        cands = read_json(Path(talk_job["workdir"]) / "scout" / "candidates.json")["candidates"]
        c = cands[0]
        out["structured_output"]["trim"] = [{"id": c["id"], "start": c["start"] + 0.5,
                                             "end": c["end"] - 0.5}]
        return out
    scout.step(conn, talk_job, cfg, runner=fake_runner(payload))
    pick = read_json(Path(talk_job["workdir"]) / "scout" / "pick.json")
    assert len(pick["trim"]) == 1


def test_claude_failure_falls_back_to_needs_caption(talk_job, conn, cfg):
    run = fake_runner({"result": "rate limited", "is_error": True})
    assert scout.step(conn, talk_job, cfg, runner=run) is None
    wd = Path(talk_job["workdir"])
    pick = read_json(wd / "scout" / "pick.json")
    cands = read_json(wd / "scout" / "candidates.json")["candidates"]
    assert pick["source"] == "needs_caption"
    assert pick["order"] == [c["id"] for c in cands]
    assert pick["hook"] == "" and pick["caption"] == "" and pick["hashtags"] == []
    msgs = alerts(conn)
    assert len(msgs) == 1 and msgs[0].startswith("Claude unavailable (")


def test_heuristic_fallback(talk_job, conn, cfg):
    cfg.claude = {**cfg.claude, "fallback": "heuristic"}
    cfg.work.mkdir(parents=True, exist_ok=True)
    cfg.playbook_path.write_text("# Playbook\nUse #mornings and #habits. Avoid #spam.\n")
    cfg.brand = {**cfg.brand, "banned_words": ["spam"]}
    scout.step(conn, talk_job, cfg, runner=fake_runner({}, code=1))
    wd = Path(talk_job["workdir"])
    pick = read_json(wd / "scout" / "pick.json")
    top = read_json(wd / "scout" / "candidates.json")["candidates"][0]
    assert pick["source"] == "local_draft"
    assert pick["order"][0] == top["id"]
    assert 0 < len(pick["hook"].split()) <= 8
    assert top["text"].startswith(pick["caption"])
    assert pick["hashtags"] == ["#mornings", "#habits"]
    assert alerts(conn)


def test_idempotent_second_run_makes_no_claude_call(talk_job, conn, cfg):
    run = fake_runner(good_pick)
    scout.step(conn, talk_job, cfg, runner=run)
    wd = Path(talk_job["workdir"])
    before = (wd / "scout" / "pick.json").read_text()
    cand_mtime = (wd / "scout" / "candidates.json").stat().st_mtime_ns
    scout.step(conn, talk_job, cfg, runner=run)
    assert len(run.calls) == 1
    assert (wd / "scout" / "pick.json").read_text() == before
    assert (wd / "scout" / "candidates.json").stat().st_mtime_ns == cand_mtime


def test_existing_candidates_are_reused(talk_job, conn, cfg, monkeypatch):
    scout.step(conn, talk_job, cfg, runner=fake_runner({}, code=1))
    wd = Path(talk_job["workdir"])
    (wd / "scout" / "pick.json").unlink()
    monkeypatch.setattr(scout, "build_candidates", lambda *a, **k: pytest.fail("rebuilt"))
    scout.step(conn, talk_job, cfg, runner=fake_runner(good_pick))
    assert read_json(wd / "scout" / "pick.json")["source"] == "claude"


def test_banned_word_in_caption_triggers_fallback(talk_job, conn, cfg):
    cfg.brand = {**cfg.brand, "banned_words": ["Discipline"]}
    scout.step(conn, talk_job, cfg, runner=fake_runner(good_pick))
    pick = read_json(Path(talk_job["workdir"]) / "scout" / "pick.json")
    assert pick["source"] == "needs_caption"
    assert any("banned" in m for m in alerts(conn))


def test_banned_word_in_hashtags_triggers_fallback(talk_job, conn, cfg):
    cfg.brand = {**cfg.brand, "banned_words": ["habit"]}
    scout.step(conn, talk_job, cfg, runner=fake_runner(good_pick))
    assert read_json(Path(talk_job["workdir"]) / "scout" / "pick.json")["source"] == "needs_caption"


def test_bad_hashtags_trigger_fallback(talk_job, conn, cfg):
    def payload(stdin):
        out = good_pick(stdin)
        out["structured_output"]["hashtags"] = ["mornings", "#habits", "#x"]
        return out
    scout.step(conn, talk_job, cfg, runner=fake_runner(payload))
    assert read_json(Path(talk_job["workdir"]) / "scout" / "pick.json")["source"] == "needs_caption"


def test_steps_export():
    fn, nxt = scout.STEPS[State.INGESTED]
    assert fn is scout.step and nxt == State.SCOUTED


# ---------------------------------------------------------------- next_candidate

def test_next_candidate_follows_order_and_applies_trim(tmp_path):
    cands = [
        {"id": "c1", "clip": "a.mp4", "start": 0.0, "end": 20.0, "duration": 20.0, "score": 0.9},
        {"id": "c2", "clip": "a.mp4", "start": 30.0, "end": 50.0, "duration": 20.0, "score": 0.8},
        {"id": "c3", "clip": "a.mp4", "start": 60.0, "end": 80.0, "duration": 20.0, "score": 0.7},
    ]
    write_json(tmp_path / "scout" / "candidates.json", {"candidates": cands})
    assert scout.next_candidate(tmp_path, [])["id"] == "c1"  # no pick yet: by score
    write_json(tmp_path / "scout" / "pick.json", {
        "source": "claude", "order": ["c3", "c1"], "hook": "", "alt_hooks": [], "caption": "",
        "hashtags": [], "why": "", "trim": {"c3": {"start": 61.0, "end": 79.0}}})
    first = scout.next_candidate(tmp_path, [])
    assert first["id"] == "c3" and first["start"] == 61.0 and first["end"] == 79.0
    assert first["duration"] == pytest.approx(18.0)
    assert scout.next_candidate(tmp_path, ["c3"])["id"] == "c1"
    assert scout.next_candidate(tmp_path, {"c3", "c1"})["id"] == "c2"  # not in order: appended
    assert scout.next_candidate(tmp_path, ["c1", "c2", "c3"]) is None
    assert scout.next_candidate(tmp_path / "missing", []) is None
