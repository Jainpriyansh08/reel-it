"""Small helpers every agent uses."""
from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime
from pathlib import Path


def write_json(path: Path, data) -> Path:
    """Atomic JSON write: temp file + rename."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".partial")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False))
    os.replace(tmp, path)
    return path


def read_json(path: Path):
    return json.loads(Path(path).read_text())


def write_text(path: Path, text: str) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".partial")
    tmp.write_text(text)
    os.replace(tmp, path)
    return path


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def local_now() -> datetime:
    return datetime.now().astimezone()


def today_local() -> str:
    return local_now().strftime("%Y-%m-%d")


def edit_dir(workdir: Path, version: int) -> Path:
    return Path(workdir) / "edit" / f"v{version}"
