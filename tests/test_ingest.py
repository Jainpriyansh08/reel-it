import json

import pytest

from reel import db, scheduler
from reel.agents import ingest
from reel.media import ffmpeg
from reel.states import State

from .conftest import age, make_clip


def test_ingest_produces_all_outputs(cfg, tmp_path, fake_backends):
    src = tmp_path / "day"
    make_clip(src / "a.mp4", 2, size="1920x1080")
    make_clip(src / "b.mp4", 1, audio=False)
    (src / "broken.mp4").write_bytes(b"not a video")

    m = ingest.run(src, tmp_path / "job", cfg, fake_backends)

    assert [c["file"] for c in m["clips"]] == ["a.mp4", "b.mp4"]
    assert m["skipped"][0]["file"] == "broken.mp4"          # reported, not fatal
    a = m["clips"][0]
    p = ffmpeg.probe(ingest.Path(a["proxy"]))
    assert p["height"] == 720 and p["width"] == 1280
    assert json.loads(ingest.Path(a["transcript"]).read_text())["vocab_seen"] == ["Kaizen"]
    assert len(json.loads(ingest.Path(a["scenes"]).read_text())) == 2
    # silent clip: no transcription call, empty transcript
    assert len(fake_backends.transcribed) == 1
    assert m["clips"][1]["audio"] is None
    assert (tmp_path / "job" / "ingest.json").exists()
    assert not list((tmp_path / "job").rglob("*.partial*"))


def test_rerun_skips_finished_clips(cfg, tmp_path, fake_backends):
    src = tmp_path / "day"
    make_clip(src / "a.mp4")
    ingest.run(src, tmp_path / "job", cfg, fake_backends)
    ingest.run(src, tmp_path / "job", cfg, fake_backends)
    assert len(fake_backends.transcribed) == 1 and len(fake_backends.scanned) == 1


def test_no_readable_clips_is_an_error(cfg, tmp_path, fake_backends):
    src = tmp_path / "day"
    src.mkdir()
    (src / "x.mp4").write_bytes(b"junk")
    with pytest.raises(ValueError, match="no readable clips"):
        ingest.run(src, tmp_path / "job", cfg, fake_backends)


def test_end_to_end_tick_ingests_a_day(cfg, conn, fake_backends, monkeypatch):
    clip = make_clip(cfg.inbox / "2026-11-19" / "talk.mov")
    age(clip, 120)
    monkeypatch.setattr(ingest, "default_backends", lambda: fake_backends)

    r = scheduler.tick(cfg, conn)

    assert r.failed == []
    job = db.get_job(conn, r.created[0])
    assert job["status"] == State.INGESTED
    assert (cfg.work / "jobs" / "2026-11-19" / "ingest.json").exists()
