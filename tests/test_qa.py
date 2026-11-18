import pytest

from reel.agents import editor, qa
from reel.states import State
from reel.util import edit_dir, read_json

from .conftest import make_clip
from .fixtures.editor.jobkit import build_job, job, set_status


def _job(tmp_path, cfg, conn, **pick):
    cfg.reel["prerender_alternates"] = 0
    j = build_job(tmp_path, conn, n_clips=1, seconds=4, pick_extra=pick)
    editor.scouted_step(conn, job(conn, j["id"]), cfg)
    set_status(conn, j["id"], State.EDITED)
    return j


def _checks(res):
    return {c["name"]: c for c in res["checks"]}


def test_qa_pass_on_good_render(tmp_path, cfg, conn):
    j = _job(tmp_path, cfg, conn, caption="Small wins add up.")
    assert qa.edited_step(conn, job(conn, j["id"]), cfg) is None
    res = read_json(edit_dir(j["workdir"], 1) / "qa.json")
    c = _checks(res)
    assert res["status"] == "pass", res
    assert c["resolution"]["detail"] == "1080x1920"
    assert c["codecs"]["detail"] == "h264/aac"
    for name in ("duration", "file_size", "black_frames", "frozen_video", "audio",
                 "caption_length", "banned_words", "spelling", "preview_size"):
        assert c[name]["status"] == "pass", c[name]
    assert (edit_dir(j["workdir"], 1) / "package.json").exists()


def test_qa_spelling_warns_on_low_confidence_word(tmp_path, cfg, conn):
    j = _job(tmp_path, cfg, conn)                 # caption: "Kaisen is about small wins."
    assert qa.edited_step(conn, job(conn, j["id"]), cfg) is None
    res = read_json(edit_dir(j["workdir"], 1) / "qa.json")
    assert res["status"] == "warn"
    assert _checks(res)["spelling"] == {"name": "spelling", "status": "warn", "detail": "kaisen"}


def test_qa_blocks_banned_word(tmp_path, cfg, conn):
    cfg.brand["banned_words"] = ["kaisen"]
    j = _job(tmp_path, cfg, conn)
    assert qa.edited_step(conn, job(conn, j["id"]), cfg) == State.QA_FAILED
    c = _checks(read_json(edit_dir(j["workdir"], 1) / "qa.json"))
    assert c["banned_words"]["status"] == "block"
    assert "caption" in c["banned_words"]["detail"]
    assert "burned_captions" in c["banned_words"]["detail"]


def test_qa_blocks_wrong_resolution_and_long_caption(tmp_path, cfg, conn):
    j = _job(tmp_path, cfg, conn, caption="x" * 2300)
    out = edit_dir(j["workdir"], 1)
    make_clip(out / "final.mp4", 3, size="640x360")
    assert qa.edited_step(conn, job(conn, j["id"]), cfg) == State.QA_FAILED
    res = read_json(out / "qa.json")
    c = _checks(res)
    assert res["status"] == "block"
    assert c["resolution"] == {"name": "resolution", "status": "block", "detail": "640x360"}
    assert c["caption_length"]["status"] == "block"


def test_qa_black_and_silent(tmp_path, cfg, conn):
    import subprocess
    j = _job(tmp_path, cfg, conn, caption="ok")
    out = edit_dir(j["workdir"], 1)
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i",
                    "color=black:s=1080x1920:r=30:d=4", "-f", "lavfi", "-i",
                    "anullsrc=r=48000:cl=stereo", "-t", "4", "-c:v", "libx264",
                    "-pix_fmt", "yuv420p", "-c:a", "aac", str(out / "final.mp4")], check=True)
    res = qa.check(j["workdir"], 1, cfg)
    c = _checks(res)
    assert c["black_frames"]["status"] == "block"
    assert c["frozen_video"]["status"] == "warn"
    assert c["audio"]["status"] == "warn"
    assert res["status"] == "block"
