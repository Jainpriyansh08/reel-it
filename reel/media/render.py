"""Render an edit recipe (edit.json) into final.mp4 + captions.ass + preview.mp4.

final.mp4   1080x1920, 30 fps, H.264 yuv420p, AAC 48 kHz, loudnorm -14 LUFS,
            burned captions, +faststart
preview.mp4 720x1280, small enough (< 45 MB) for Telegram
Every file is written under a temp name and renamed into place.
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

from . import captions, ffmpeg, reframe

PREVIEW_MAX_BYTES = 45 * 1024 * 1024


def _run(cmd: list[str], cwd: Path | None = None) -> str:
    res = subprocess.run(cmd, capture_output=True, text=True, cwd=cwd)
    if res.returncode != 0:
        raise ffmpeg.MediaError(f"ffmpeg failed ({res.returncode}): {res.stderr.strip()[-800:]}")
    return res.stderr


def _partial(dest: Path) -> Path:
    tmp = dest.with_name(dest.stem + ".partial" + dest.suffix)
    tmp.unlink(missing_ok=True)
    return tmp


def write_captions(recipe: dict, transcript_words: list[dict], out: Path) -> str:
    start, end = float(recipe["start"]), float(recipe["end"])
    speed = float(recipe.get("speed") or 1.0)
    style = recipe.get("caption_style") or {}
    words = captions.window_words(transcript_words, start, end, speed)
    text = captions.build_ass(words, recipe.get("hook_text") or "",
                              font=style.get("font") or "Montserrat",
                              size=style.get("size") or 72,
                              duration=(end - start) / speed,
                              word_fixes=recipe.get("word_fixes"))
    tmp = out.with_name(out.name + ".partial")
    tmp.write_text(text)
    os.replace(tmp, out)
    return text


def render_final(recipe: dict, ass_path: Path, dest: Path) -> Path:
    src = Path(recipe["source"])
    meta = ffmpeg.probe(src)
    start, end = float(recipe["start"]), float(recipe["end"])
    speed = float(recipe.get("speed") or 1.0)
    dur = max(0.1, end - start)
    out_dur = dur / speed
    has_events = "Dialogue:" in ass_path.read_text()

    vchain = reframe.filtergraph(recipe.get("crop") or {"mode": "center"},
                                 meta["width"], meta["height"], "0:v", "vr")
    post = []
    if speed != 1.0:
        post.append(f"setpts=PTS/{speed}")
    if has_events:
        post.append(f"ass={ass_path.name}")      # run with cwd = out dir: no path escaping
    post += ["fps=30", "format=yuv420p"]
    graph = vchain + ";[vr]" + ",".join(post) + "[v]"

    lufs = float(recipe.get("loudnorm") or -14)
    cmd = ["ffmpeg", "-y", "-v", "error", "-ss", f"{start:.3f}", "-t", f"{dur:.3f}",
           "-i", str(src)]
    if meta["has_audio"]:
        achain = []
        if speed != 1.0:
            achain.append(f"atempo={min(2.0, max(0.5, speed))}")
        achain += [f"loudnorm=I={lufs}:TP=-1.5:LRA=11", "aresample=48000"]
        graph += ";[0:a]" + ",".join(achain) + "[a]"
    else:
        cmd += ["-f", "lavfi", "-t", f"{out_dur:.3f}", "-i", "anullsrc=r=48000:cl=stereo"]
        graph += ";[1:a]anull[a]"
    tmp = _partial(dest)
    cmd += ["-filter_complex", graph, "-map", "[v]", "-map", "[a]",
            "-c:v", "libx264", "-preset", "medium", "-crf", "20", "-pix_fmt", "yuv420p",
            "-profile:v", "high", "-r", "30",
            "-c:a", "aac", "-b:a", "192k", "-ar", "48000",
            "-t", f"{out_dur:.3f}", "-movflags", "+faststart", tmp.name]
    _run(cmd, cwd=dest.parent)
    os.replace(tmp, dest)
    return dest


def render_preview(final: Path, dest: Path, max_bytes: int = PREVIEW_MAX_BYTES) -> Path:
    tmp = _partial(dest)
    for crf in (28, 32, 36, 40):
        _run(["ffmpeg", "-y", "-v", "error", "-i", str(final),
              "-vf", "scale=720:1280", "-c:v", "libx264", "-preset", "veryfast",
              "-crf", str(crf), "-pix_fmt", "yuv420p", "-maxrate", "4M", "-bufsize", "8M",
              "-c:a", "aac", "-b:a", "96k", "-movflags", "+faststart", str(tmp)])
        if tmp.stat().st_size < max_bytes:
            break
    os.replace(tmp, dest)
    return dest


def render(edit_recipe: dict, transcript_words: list[dict], cfg, out_dir: Path) -> dict:
    """Write captions.ass, final.mp4 and preview.mp4 into out_dir."""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    ass = out_dir / "captions.ass"
    write_captions(edit_recipe, transcript_words, ass)
    final = render_final(edit_recipe, ass, out_dir / "final.mp4")
    preview = render_preview(final, out_dir / "preview.mp4")
    return {"final": str(final), "preview": str(preview), "captions": str(ass)}


OUTPUTS = ("final.mp4", "preview.mp4", "captions.ass")


def is_rendered(out_dir: Path) -> bool:
    return all((Path(out_dir) / n).exists() for n in OUTPUTS)
