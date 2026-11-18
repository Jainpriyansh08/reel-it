import os
import subprocess
import time
from pathlib import Path

import pytest

from reel import db
from reel.agents.ingest import Backends
from reel.config import Config


@pytest.fixture
def cfg(tmp_path) -> Config:
    return Config.from_dict({
        "inbox": str(tmp_path / "inbox"),
        "work": str(tmp_path / "work"),
        "db": str(tmp_path / "work" / "reels.db"),
        "settle_seconds": 60,
        "max_attempts": 3,
    }, {"vocab": ["Kaizen"]})


@pytest.fixture
def conn(cfg):
    c = db.connect(cfg.db)
    yield c
    c.close()


def age(path: Path, seconds: float) -> None:
    """Pretend a file was last written `seconds` ago."""
    t = time.time() - seconds
    os.utime(path, (t, t))


def make_clip(path: Path, seconds: float = 2, size="640x360", audio=True) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    cmd = ["ffmpeg", "-y", "-v", "error",
           "-f", "lavfi", "-i", f"testsrc2=size={size}:rate=30:duration={seconds}"]
    if audio:
        cmd += ["-f", "lavfi", "-i", f"sine=frequency=440:duration={seconds}"]
    cmd += ["-c:v", "libx264", "-pix_fmt", "yuv420p", "-shortest"]
    if audio:
        cmd += ["-c:a", "aac"]
    subprocess.run(cmd + [str(path)], check=True)
    return path


class FakeBackends(Backends):
    def __init__(self):
        self.transcribed: list[Path] = []
        self.scanned: list[Path] = []
        super().__init__(transcribe=self._tr, detect_scenes=self._sc)

    def _tr(self, audio, settings, vocab):
        self.transcribed.append(audio)
        return {"language": "en", "duration": 2.0, "vocab_seen": vocab,
                "segments": [{"start": 0.0, "end": 1.5, "text": "hello Kaizen",
                              "words": [{"start": 0.0, "end": 0.5, "word": "hello", "p": 0.9},
                                        {"start": 0.6, "end": 1.5, "word": "Kaizen", "p": 0.8}]}]}

    def _sc(self, video):
        self.scanned.append(video)
        return [{"start": 0.0, "end": 1.0}, {"start": 1.0, "end": 2.0}]


@pytest.fixture
def fake_backends():
    return FakeBackends()
