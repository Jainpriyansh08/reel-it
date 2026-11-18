"""Cover image: the sharpest of a few frames from final.mp4, hook text on top."""
from __future__ import annotations

import os
import subprocess
import tempfile
from functools import lru_cache
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from . import ffmpeg

W, H = 1080, 1920
SAFE_LEFT, SAFE_RIGHT_PX, SAFE_TOP = 80, 140, 250
HOOK_SKIP = 2.6
FONT_DIRS = ["~/Library/Fonts", "/Library/Fonts", "/System/Library/Fonts",
             "/usr/share/fonts", "/usr/local/share/fonts", "~/.fonts", "~/.local/share/fonts"]


@lru_cache(maxsize=16)
def find_font(name: str) -> str | None:
    """Path of a font file whose name matches `name` (bold preferred), or None."""
    key = "".join(name.lower().split())
    if not key:
        return None
    hits = []
    for d in FONT_DIRS:
        root = Path(d).expanduser()
        if not root.is_dir():
            continue
        for p in root.rglob("*"):
            if p.suffix.lower() in (".ttf", ".otf", ".ttc") and \
                    "".join(p.stem.lower().split()).startswith(key):
                hits.append(p)
    if not hits:
        return None
    hits.sort(key=lambda p: (0 if "bold" in p.stem.lower() and "italic" not in p.stem.lower()
                             else 1 if "italic" not in p.stem.lower() else 2, len(p.name)))
    return str(hits[0])


def load_font(name: str, size: int):
    path = find_font(name or "")
    if path:
        try:
            return ImageFont.truetype(path, size)
        except OSError:
            pass
    try:
        return ImageFont.load_default(size=size)
    except TypeError:  # very old Pillow
        return ImageFont.load_default()


def sharpness(image_path: Path) -> float:
    try:
        import cv2
    except ImportError:
        return 0.0
    img = cv2.imread(str(image_path), cv2.IMREAD_GRAYSCALE)
    return 0.0 if img is None else float(cv2.Laplacian(img, cv2.CV_64F).var())


def best_frame(video: Path, tmpdir: Path, samples: int = 5) -> Path:
    """Extract `samples` frames spread over the video; return the sharpest."""
    dur = ffmpeg.probe(video)["duration"] or 1.0
    # skip the first seconds where the burned-in hook is on screen (the
    # cover draws its own), when the video is long enough
    lo = HOOK_SKIP if dur > HOOK_SKIP + 1.0 else 0.0
    frames = []
    for i in range(samples):
        t = lo + (dur - lo) * (i + 0.5) / samples
        out = tmpdir / f"f{i}.png"
        res = subprocess.run(["ffmpeg", "-y", "-v", "error", "-ss", f"{t:.3f}", "-i", str(video),
                              "-frames:v", "1", str(out)], capture_output=True, text=True)
        if res.returncode == 0 and out.exists():
            frames.append(out)
    if not frames:
        raise ffmpeg.MediaError(f"could not extract a frame from {video.name}")
    return max(frames, key=sharpness)


def _wrap(draw, text: str, font, max_w: int) -> list[str]:
    lines, cur = [], ""
    for word in text.split():
        trial = f"{cur} {word}".strip()
        if draw.textlength(trial, font=font) <= max_w or not cur:
            cur = trial
        else:
            lines.append(cur)
            cur = word
    if cur:
        lines.append(cur)
    return lines


def draw_hook(img: Image.Image, hook: str, font_name: str) -> Image.Image:
    if not hook or not hook.strip():
        return img
    draw = ImageDraw.Draw(img)
    max_w = W - SAFE_LEFT - SAFE_RIGHT_PX
    for size in (110, 96, 84, 72, 60):
        font = load_font(font_name, size)
        lines = _wrap(draw, hook.strip(), font, max_w)
        if len(lines) <= 4 and all(draw.textlength(l, font=font) <= max_w for l in lines):
            break
    y = SAFE_TOP + 60
    for line in lines:
        tw = draw.textlength(line, font=font)
        x = SAFE_LEFT + (max_w - tw) / 2
        draw.text((x, y), line, font=font, fill=(255, 255, 255),
                  stroke_width=max(3, size // 14), stroke_fill=(0, 0, 0))
        y += int(size * 1.2)
    return img


def make_cover(video: Path, hook: str, font_name: str, dest: Path) -> Path:
    dest = Path(dest)
    with tempfile.TemporaryDirectory() as td:
        frame = best_frame(Path(video), Path(td))
        img = Image.open(frame).convert("RGB")
        if img.size != (W, H):
            img = img.resize((W, H))
        img = draw_hook(img, hook, font_name)
        tmp = dest.with_name(dest.stem + ".partial.jpg")
        img.save(tmp, "JPEG", quality=90)
        os.replace(tmp, dest)
    return dest
