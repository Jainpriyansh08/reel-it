import json
import os

import pytest

from reel import db, scheduler
from reel.agents import editor
from reel.media import captions, reframe
from reel.media import render as rnd
from reel.states import State
from reel.util import edit_dir, read_json, write_json

from .conftest import make_clip
from .fixtures.editor.jobkit import WORDS, build_job, job, set_status


@pytest.fixture(autouse=True)
def no_alternates(cfg):
    cfg.reel["prerender_alternates"] = 0


def ffprobe(path):
    import subprocess
    out = subprocess.run(["ffprobe", "-v", "error", "-print_format", "json", "-show_streams",
                          "-show_format", str(path)], capture_output=True, text=True, check=True)
    return json.loads(out.stdout)


# ------------------------------------------------------------------ reframe

@pytest.mark.parametrize("w,h,ratio,screen,expected", [
    (1920, 1080, 0.8, False, "face"),
    (1920, 1080, 0.3, False, "face"),
    (1920, 1080, 0.29, False, "fit_blur"),   # landscape, no faces
    (1000, 900, 0.1, True, "fit_blur"),      # screen recording
    (1000, 900, 0.1, False, "center"),       # near-square, no faces, not a screen
    (1080, 1920, 0.9, False, "fit"),         # already vertical: scale/pad
])
def test_decide_mode(w, h, ratio, screen, expected):
    assert reframe.decide_mode(w, h, ratio, screen) == expected


def test_crop_box_face_and_clamp():
    box = reframe.crop_box(1920, 1080, "face", 0.75)
    assert box["w"] == 608 and box["x"] == 1440 - 304
    assert reframe.crop_box(1920, 1080, "face", 0.99)["x"] == 1920 - 608
    assert reframe.crop_box(1920, 1080, "center")["x"] == (1920 - 608) // 2
    assert reframe.crop_box(1920, 1080, "fit_blur") == {"mode": "fit_blur", "x": 0, "w": 1920}


def test_bad_crop_cycle():
    assert [reframe.next_mode(m) for m in ("face", "fit_blur", "center", "fit")] == \
        ["fit_blur", "center", "face", "fit_blur"]


def test_choose_crop_real_clips(tmp_path):
    land = make_clip(tmp_path / "land.mp4", 2, size="640x360", audio=False)
    box = reframe.choose_crop(land, 0, 2, 640, 360)
    assert box["mode"] != "face" and box["face_ratio"] < 0.3
    assert box["mode"] == "fit_blur"
    vert = make_clip(tmp_path / "vert.mp4", 2, size="360x640", audio=False)
    assert reframe.choose_crop(vert, 0, 2, 360, 640)["mode"] == "fit"


# ------------------------------------------------------------------ captions

def _style(ass, name):
    line = next(l for l in ass.splitlines() if l.startswith(f"Style: {name},"))
    f = line.split(",")
    return {"size": int(f[2]), "align": int(f[18]), "ml": int(f[19]), "mr": int(f[20]),
            "mv": int(f[21]), "font": f[1].split(": ")[-1] if ": " in f[0] else f[1]}


def test_window_and_split_lines():
    ws = captions.window_words(WORDS, 1.0, 3.8)
    assert ws[0]["word"] == "This" and ws[0]["start"] == pytest.approx(0.0)
    words = [{"start": i * 0.3, "end": i * 0.3 + 0.25, "word": f"w{i}"} for i in range(11)]
    pages = captions.split_lines(words)
    assert all(len(p) <= 2 for p in pages)
    assert all(1 <= len(line) <= 4 for p in pages for line in p)
    assert sum(len(l) for p in pages for l in p) == 11


def test_sentence_end_breaks_line():
    pages = captions.split_lines(captions.window_words(WORDS, 0, 4))
    first_lines = [[w["word"] for w in l] for p in pages for l in p]
    assert first_lines[0] == ["Hello", "there."]


def test_ass_safe_zone_fixes_and_highlight():
    ws = captions.window_words(WORDS, 0, 4)
    ass = captions.build_ass(ws, "Big hook", font="Montserrat", size=200,
                             duration=4, word_fixes={"KAISEN": "Kaizen"})
    cap, hook = _style(ass, "Caption"), _style(ass, "Hook")
    assert 64 <= cap["size"] <= 80
    assert cap["align"] == 2 and cap["mv"] >= 450 and cap["mr"] >= 140
    assert hook["align"] == 8 and hook["mv"] >= 250 and hook["mr"] >= 140
    assert hook["size"] > cap["size"]
    assert "Montserrat" in ass
    burned = captions.burned_words(ass)
    assert "Kaizen" in burned and "kaisen" not in burned
    hook_ev = [l for l in ass.splitlines() if ",Hook," in l and l.startswith("Dialogue")]
    assert hook_ev and "0:00:02.50" in hook_ev[0]
    cap_ev = [l for l in ass.splitlines() if ",Caption," in l and l.startswith("Dialogue")]
    assert len(cap_ev) == len(WORDS)               # one event per highlighted word
    assert all(l.count("\\c&H0000E5FF&") == 1 for l in cap_ev)
    assert all(l.count("\\N") <= 1 for l in cap_ev)  # max 2 lines


def test_no_words_no_captions():
    ass = captions.build_ass([], "", duration=3)
    assert "Dialogue" not in ass
    ass = captions.build_ass([], "Hook only", duration=3)
    assert ",Caption," not in ass.split("[Events]")[1]


# ------------------------------------------------------------------ render

def test_render_output_specs(tmp_path, cfg):
    src = make_clip(tmp_path / "a.mp4", 3, size="640x360")
    recipe = {"version": 1, "candidate_id": "c1", "source": str(src), "start": 0.2, "end": 2.8,
              "crop": {"mode": "center", "x": 220, "w": 202}, "speed": 1.0,
              "hook_text": "Hook", "caption_style": {"font": "Montserrat", "size": 72},
              "word_fixes": {}, "loudnorm": -14, "reason": "first"}
    out = tmp_path / "v1"
    rnd.render(recipe, WORDS, cfg, out)
    info = ffprobe(out / "final.mp4")
    v = next(s for s in info["streams"] if s["codec_type"] == "video")
    a = next(s for s in info["streams"] if s["codec_type"] == "audio")
    assert (v["width"], v["height"]) == (1080, 1920)
    assert v["codec_name"] == "h264" and v["pix_fmt"] == "yuv420p"
    assert v["avg_frame_rate"] == "30/1"
    assert a["codec_name"] == "aac" and a["sample_rate"] == "48000"
    assert float(info["format"]["duration"]) == pytest.approx(2.6, abs=0.15)
    p = next(s for s in ffprobe(out / "preview.mp4")["streams"] if s["codec_type"] == "video")
    assert (p["width"], p["height"]) == (720, 1280)
    assert (out / "preview.mp4").stat().st_size < 45 * 1024 * 1024
    assert "Dialogue" in (out / "captions.ass").read_text()
    assert not list(out.glob("*.partial*"))


def test_render_silent_vertical_source(tmp_path, cfg):
    src = make_clip(tmp_path / "v.mp4", 2, size="360x640", audio=False)
    recipe = {"source": str(src), "start": 0, "end": 2, "crop": {"mode": "fit", "x": 0, "w": 360},
              "hook_text": "", "word_fixes": {}}
    rnd.render(recipe, [], cfg, tmp_path / "out")
    info = ffprobe(tmp_path / "out" / "final.mp4")
    kinds = {s["codec_type"]: s for s in info["streams"]}
    assert kinds["video"]["width"] == 1080 and kinds["audio"]["codec_name"] == "aac"


# ------------------------------------------------------------------ steps

def test_scouted_step_first_version_and_idempotent(tmp_path, cfg, conn, monkeypatch):
    j = build_job(tmp_path, conn, n_clips=2)
    assert editor.scouted_step(conn, job(conn, j["id"]), cfg) is None
    v1 = edit_dir(j["workdir"], 1)
    rec = read_json(v1 / "edit.json")
    assert rec["candidate_id"] == "c1" and rec["reason"] == "first"
    assert (rec["start"], rec["end"]) == (0.1, 2.9)            # pick trim applied
    assert rec["crop"]["mode"] == "fit_blur"
    assert rec["hook_text"] == "Small wins daily"
    assert rec["caption_style"]["font"] == cfg.brand["font"]
    assert job(conn, j["id"])["edit_version"] == 1

    # same recipe again -> no render
    def boom(*a, **k):
        raise AssertionError("re-rendered")
    monkeypatch.setattr(rnd, "render", boom)
    mtime = (v1 / "final.mp4").stat().st_mtime_ns
    editor.make_version(j["workdir"], rec, cfg)
    # step re-run after a crash between set_job and transition: nothing new
    assert editor.scouted_step(conn, job(conn, j["id"]), cfg) is None
    assert (v1 / "final.mp4").stat().st_mtime_ns == mtime
    assert not edit_dir(j["workdir"], 2).exists()


def test_scouted_without_candidates_gives_up(tmp_path, cfg, conn):
    j = build_job(tmp_path, conn, n_clips=1, pick_extra={"order": ["c1"]})
    write_json(j["workdir"] / "scout" / "candidates.json", {"candidates": []})
    assert editor.scouted_step(conn, job(conn, j["id"]), cfg) is scheduler.STAY
    assert job(conn, j["id"])["status"] == State.NEEDS_HUMAN


def _start_v1(tmp_path, cfg, conn, **kw):
    j = build_job(tmp_path, conn, **kw)
    editor.scouted_step(conn, job(conn, j["id"]), cfg)
    set_status(conn, j["id"], State.RECUT_REQUESTED)
    return j


def _recut(conn, cfg, j, reason):
    db.set_job(conn, j["id"], recut_reason=json.dumps(reason))
    assert editor.recut_step(conn, job(conn, j["id"]), cfg) is None
    row = job(conn, j["id"])
    assert row["recut_reason"] is None
    return read_json(edit_dir(j["workdir"], row["edit_version"]) / "edit.json"), row


def test_recut_tighter_bad_crop_fix_captions(tmp_path, cfg, conn):
    j = _start_v1(tmp_path, cfg, conn, n_clips=2, seconds=4)
    v1 = read_json(edit_dir(j["workdir"], 1) / "edit.json")

    rec, row = _recut(conn, cfg, j, {"reason": "tighter"})
    assert row["edit_version"] == 2 and rec["reason"] == "recut:tighter"
    assert rec["candidate_id"] == "c1"
    assert rec["end"] - rec["start"] < v1["end"] - v1["start"]
    inside = [w for w in WORDS if rec["start"] <= w["start"] and w["end"] <= rec["end"]]
    partial = [w for w in WORDS if w not in inside
               and w["end"] > rec["start"] and w["start"] < rec["end"]]
    assert inside and not partial                                 # whole words only
    assert inside[-1]["word"].endswith(".")                       # ends on a sentence
    i = WORDS.index(inside[0])
    assert i == 0 or WORDS[i - 1]["word"].endswith(".")           # starts on a sentence

    rec, row = _recut(conn, cfg, j, {"reason": "bad_crop"})
    assert row["edit_version"] == 3 and rec["crop"]["mode"] == "center"   # fit_blur -> center
    assert rec["crop"]["w"] == 202
    assert (rec["start"], rec["end"]) == (read_json(edit_dir(j["workdir"], 2) / "edit.json")["start"],
                                          read_json(edit_dir(j["workdir"], 2) / "edit.json")["end"])

    rec, row = _recut(conn, cfg, j, {"reason": "fix_captions", "word_fixes": {"kaisen": "Kaizen"}})
    assert row["edit_version"] == 4 and rec["word_fixes"] == {"kaisen": "Kaizen"}
    assert rec["crop"]["mode"] == "center"
    ass = (edit_dir(j["workdir"], 4) / "captions.ass").read_text()
    assert "Kaizen" in captions.burned_words(ass)
    assert all((edit_dir(j["workdir"], v) / "final.mp4").exists() for v in (1, 2, 3, 4))


def test_tighten_long_window_on_sentences(cfg):
    words, t = [], 0.0
    for s in range(10):                      # ten 3.5 s sentences
        for k in range(5):
            words.append({"start": t, "end": t + 0.6,
                          "word": f"w{s}{k}" + ("." if k == 4 else ""), "p": 0.9})
            t += 0.7
    start, end = editor.tighten({"start": 0.0, "end": t}, words, cfg)
    assert end - start < 20
    assert end - start > 14
    assert any(abs(end - w["end"]) < 0.26 and end >= w["end"] for w in words if w["word"].endswith("."))
    # no words -> centre cut
    start, end = editor.tighten({"start": 0.0, "end": 60.0}, [], cfg)
    assert end - start < 20 and abs((start + end) / 2 - 30) < 1e-6


def test_recut_different_clip_renders_next(tmp_path, cfg, conn):
    j = _start_v1(tmp_path, cfg, conn, n_clips=3)
    rec, row = _recut(conn, cfg, j, {"reason": "different_clip"})
    assert row["edit_version"] == 2 and rec["candidate_id"] == "c2"
    assert rec["reason"] == "recut:different_clip"
    set_status(conn, j["id"], State.RECUT_REQUESTED)
    rec, row = _recut(conn, cfg, j, {"reason": "different_clip"})
    assert rec["candidate_id"] == "c3"


def test_alternates_and_fast_path(tmp_path, cfg, conn, monkeypatch):
    cfg.reel["prerender_alternates"] = 1
    j = _start_v1(tmp_path, cfg, conn, n_clips=3)
    alt = j["workdir"] / "edit" / "alt" / "c2"
    assert (alt / "final.mp4").exists() and not (j["workdir"] / "edit" / "alt" / "c3").exists()
    final_inode = os.stat(alt / "final.mp4").st_ino

    calls = []
    real = rnd.render

    def counting(recipe, *a, **k):
        calls.append(recipe.get("candidate_id"))
        return real(recipe, *a, **k)
    monkeypatch.setattr(rnd, "render", counting)
    rec, row = _recut(conn, cfg, j, {"reason": "different_clip"})
    v2 = edit_dir(j["workdir"], 2)
    assert rec["candidate_id"] == "c2" and rec["version"] == 2
    assert os.stat(v2 / "final.mp4").st_ino == final_inode      # moved, not re-rendered
    assert "c2" not in calls
    assert calls == ["c3"]                                        # next alternate pre-rendered
    assert not alt.exists()


def test_qa_failed_loop_then_give_up(tmp_path, cfg, conn):
    j = build_job(tmp_path, conn, n_clips=3)
    editor.scouted_step(conn, job(conn, j["id"]), cfg)
    for attempt in (1, 2):
        v = job(conn, j["id"])["edit_version"]
        write_json(edit_dir(j["workdir"], v) / "qa.json", {"status": "block", "checks": []})
        set_status(conn, j["id"], State.QA_FAILED)
        assert editor.qa_failed_step(conn, job(conn, j["id"]), cfg) == State.SCOUTED
        assert job(conn, j["id"])["qa_failures"] == attempt
        set_status(conn, j["id"], State.SCOUTED)
        assert editor.scouted_step(conn, job(conn, j["id"]), cfg) is None
        rec = read_json(edit_dir(j["workdir"], v + 1) / "edit.json")
        assert rec["candidate_id"] == f"c{attempt + 1}" and rec["reason"] == "qa_retry"
    set_status(conn, j["id"], State.QA_FAILED)
    assert editor.qa_failed_step(conn, job(conn, j["id"]), cfg) is scheduler.STAY
    row = job(conn, j["id"])
    assert row["status"] == State.NEEDS_HUMAN and row["stuck_at"] == State.QA_FAILED


def test_steps_export():
    assert editor.STEPS[State.SCOUTED][1] == State.EDITED
    assert editor.STEPS[State.RECUT_REQUESTED][1] == State.EDITED
    assert editor.STEPS[State.QA_FAILED][1] == State.SCOUTED


def test_local_candidate_reader(tmp_path, conn):
    j = build_job(tmp_path, conn, n_clips=3, pick_extra={"order": ["c3", "c1"]})
    assert editor._local_next_candidate(j["workdir"], set())["id"] == "c3"
    c = editor._local_next_candidate(j["workdir"], {"c3"})
    assert c["id"] == "c1" and c["start"] == 0.1
    assert editor._local_next_candidate(j["workdir"], {"c1", "c3"})["id"] == "c2"
    assert editor._local_next_candidate(j["workdir"], {"c1", "c2", "c3"}) is None
