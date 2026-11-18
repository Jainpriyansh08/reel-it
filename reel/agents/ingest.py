"""Agent 1 — Ingest.

Input:  a folder of raw clips for one day.
Output: <workdir>/ingest/
          <clip>.proxy.mp4        720p H.264 for analysis and previews
          <clip>.wav              16 kHz mono audio
          <clip>.transcript.json  words with timestamps
          <clip>.scenes.json      shot boundaries
        <workdir>/ingest.json     manifest; its presence means the step is done

Each clip's outputs are skipped if already present, so a crash mid-day
resumes from the next unfinished clip instead of starting over.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from ..config import VIDEO_EXTS, Config
from ..media import ffmpeg


@dataclass
class Backends:
    transcribe: Callable[[Path, dict, list[str]], dict]
    detect_scenes: Callable[[Path], list[dict]]


def default_backends() -> Backends:
    from ..media.scenes import detect_scenes
    from ..media.transcribe import transcribe
    return Backends(transcribe=transcribe, detect_scenes=detect_scenes)


def write_json(path: Path, data) -> None:
    tmp = path.with_name(path.name + ".partial")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False))
    os.replace(tmp, path)


def list_clips(source_dir: Path) -> list[Path]:
    return sorted(
        p for p in Path(source_dir).iterdir()
        if p.is_file() and p.suffix.lower() in VIDEO_EXTS and not p.name.startswith(".")
    )


def run(source_dir: Path, workdir: Path, cfg: Config, backends: Backends | None = None) -> dict:
    backends = backends or default_backends()
    out_dir = Path(workdir) / "ingest"
    out_dir.mkdir(parents=True, exist_ok=True)
    clips = list_clips(source_dir)
    if not clips:
        raise ValueError(f"no video files in {source_dir}")

    manifest = {"source_dir": str(source_dir), "clips": [], "skipped": []}
    vocab = list(cfg.brand.get("vocab") or [])
    for clip in clips:
        try:
            meta = ffmpeg.probe(clip)
        except ffmpeg.MediaError as e:
            # One unreadable file shouldn't sink the day; report it instead.
            manifest["skipped"].append({"file": clip.name, "reason": str(e)})
            continue

        stem = clip.stem
        proxy = out_dir / f"{stem}.proxy.mp4"
        wav = out_dir / f"{stem}.wav"
        tr = out_dir / f"{stem}.transcript.json"
        sc = out_dir / f"{stem}.scenes.json"

        if not proxy.exists():
            ffmpeg.make_proxy(clip, proxy, cfg.proxy_height)
        if meta["has_audio"]:
            if not wav.exists():
                ffmpeg.extract_audio(clip, wav)
            if not tr.exists():
                write_json(tr, backends.transcribe(wav, cfg.transcribe, vocab))
        elif not tr.exists():
            write_json(tr, {"language": None, "duration": meta["duration"], "segments": []})
        if not sc.exists():
            scenes = backends.detect_scenes(proxy) or [{"start": 0.0, "end": meta["duration"]}]
            write_json(sc, scenes)

        manifest["clips"].append({
            "file": clip.name,
            "source": str(clip),
            **meta,
            "proxy": str(proxy),
            "audio": str(wav) if meta["has_audio"] else None,
            "transcript": str(tr),
            "scenes": str(sc),
        })

    if not manifest["clips"]:
        raise ValueError("no readable clips: " + "; ".join(s["reason"] for s in manifest["skipped"]))
    manifest["total_seconds"] = round(sum(c["duration"] for c in manifest["clips"]), 3)
    write_json(Path(workdir) / "ingest.json", manifest)
    return manifest
