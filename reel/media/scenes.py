"""Shot boundaries via PySceneDetect's content detector."""
from __future__ import annotations

from pathlib import Path


def detect_scenes(video: Path, threshold: float = 27.0) -> list[dict]:
    try:
        from scenedetect import ContentDetector, detect
    except ImportError as e:  # pragma: no cover - depends on install
        raise RuntimeError(
            "scenedetect is not installed. Run: pip install -e '.[ml]'"
        ) from e
    scenes = detect(str(video), ContentDetector(threshold=threshold))
    return [
        {"start": round(s.get_seconds(), 3), "end": round(e.get_seconds(), 3)}
        for s, e in scenes
    ]
