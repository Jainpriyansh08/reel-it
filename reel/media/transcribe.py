"""faster-whisper with word timestamps, VAD and hotwords from brand.yaml vocab.

The model is loaded once per process and reused across clips."""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path


@lru_cache(maxsize=2)
def _model(name: str, device: str, compute_type: str):
    try:
        from faster_whisper import WhisperModel
    except ImportError as e:  # pragma: no cover - depends on install
        raise RuntimeError(
            "faster-whisper is not installed. Run: pip install -e '.[ml]'"
        ) from e
    return WhisperModel(name, device=device, compute_type=compute_type)


def transcribe(audio: Path, settings: dict, vocab: list[str]) -> dict:
    model = _model(settings["model"], settings.get("device", "auto"),
                   settings.get("compute_type", "int8"))
    segments, info = model.transcribe(
        str(audio),
        language=settings.get("language") or None,
        word_timestamps=True,
        vad_filter=True,                 # Silero VAD, bundled with faster-whisper
        hotwords=" ".join(vocab) or None,
        beam_size=5,
    )
    out = []
    for seg in segments:  # generator: decoding happens here
        out.append({
            "start": round(seg.start, 3),
            "end": round(seg.end, 3),
            "text": seg.text.strip(),
            "words": [
                {"start": round(w.start, 3), "end": round(w.end, 3),
                 "word": w.word.strip(), "p": round(w.probability, 3)}
                for w in (seg.words or [])
            ],
        })
    return {"language": info.language, "duration": info.duration, "segments": out}
