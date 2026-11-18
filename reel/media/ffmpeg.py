"""Thin FFmpeg/ffprobe wrappers. Outputs are written to a temp name and
renamed into place, so a crash never leaves a half-written file that looks done."""
from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path


class MediaError(RuntimeError):
    pass


def _run(cmd: list[str]) -> str:
    res = subprocess.run(cmd, capture_output=True, text=True)
    if res.returncode != 0:
        raise MediaError(f"{cmd[0]} failed ({res.returncode}): {res.stderr.strip()[-800:]}")
    return res.stdout


def probe(path: Path) -> dict:
    """Duration, size, fps and whether there is an audio track."""
    out = json.loads(_run([
        "ffprobe", "-v", "error", "-print_format", "json",
        "-show_format", "-show_streams", str(path),
    ]))
    video = next((s for s in out.get("streams", []) if s.get("codec_type") == "video"), None)
    audio = next((s for s in out.get("streams", []) if s.get("codec_type") == "audio"), None)
    if video is None:
        raise MediaError(f"no video stream in {path.name}")
    num, _, den = (video.get("avg_frame_rate") or "0/1").partition("/")
    fps = float(num) / float(den or 1) if float(den or 1) else 0.0
    rotation = 0
    for sd in video.get("side_data_list", []) or []:
        if "rotation" in sd:
            rotation = int(sd["rotation"])
    w, h = int(video["width"]), int(video["height"])
    if abs(rotation) in (90, 270):
        w, h = h, w
    return {
        "duration": float(out["format"].get("duration") or video.get("duration") or 0),
        "width": w,
        "height": h,
        "fps": round(fps, 3),
        "codec": video.get("codec_name"),
        "has_audio": audio is not None,
    }


def _atomic(dest: Path, build) -> Path:
    tmp = dest.with_name(dest.stem + ".partial" + dest.suffix)
    tmp.unlink(missing_ok=True)
    build(tmp)
    os.replace(tmp, dest)
    return dest


def make_proxy(src: Path, dest: Path, height: int = 720) -> Path:
    """Small, fast-to-seek H.264 copy used for analysis and previews."""
    return _atomic(dest, lambda tmp: _run([
        "ffmpeg", "-y", "-v", "error", "-i", str(src),
        "-vf", f"scale=-2:'min({height},ih)'",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "28", "-g", "30",
        "-c:a", "aac", "-b:a", "96k", "-movflags", "+faststart",
        str(tmp),
    ]))


def extract_audio(src: Path, dest: Path) -> Path:
    """16 kHz mono WAV, what the transcription model expects."""
    return _atomic(dest, lambda tmp: _run([
        "ffmpeg", "-y", "-v", "error", "-i", str(src),
        "-vn", "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", str(tmp),
    ]))
