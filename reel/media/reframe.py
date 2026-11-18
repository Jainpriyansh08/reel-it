"""Reframe any source to a 1080x1920 (9:16) canvas.

Crop modes (recorded in edit.json "crop"):
  face      static 9:16 crop centred on the median face position
  center    static 9:16 crop from the middle of the frame
  fit_blur  whole frame scaled to width 1080, over a blurred fill copy
            (screen recordings / landscape content with no faces)
  fit       already-vertical source: scale to fit, pad (no cropping)

Crop x/w are in source pixel coordinates. One static crop per clip.
"""
from __future__ import annotations

from pathlib import Path

OUT_W, OUT_H = 1080, 1920
FACE_MIN_RATIO = 0.30
LANDSCAPE_RATIO = 1.3
CYCLE = ["face", "fit_blur", "center"]


def decide_mode(width: int, height: int, face_ratio: float, screen_like: bool = False) -> str:
    """Pure decision: which crop mode suits this source."""
    if height > width:
        return "fit"
    if face_ratio >= FACE_MIN_RATIO:
        return "face"
    if screen_like or width / max(height, 1) >= LANDSCAPE_RATIO:
        return "fit_blur"
    return "center"


def next_mode(mode: str) -> str:
    """bad_crop recut: face -> fit_blur -> center -> face."""
    if mode not in CYCLE:
        return "fit_blur"
    return CYCLE[(CYCLE.index(mode) + 1) % len(CYCLE)]


def _even(v: float) -> int:
    return max(2, int(round(v / 2.0)) * 2)


def crop_box(width: int, height: int, mode: str, face_x: float | None = None) -> dict:
    """x/w (source px) of the static crop. face_x is normalised 0..1."""
    if mode in ("fit", "fit_blur"):
        return {"mode": mode, "x": 0, "w": int(width)}
    cw = min(_even(height * 9 / 16), int(width) - int(width) % 2)
    centre = (face_x if (mode == "face" and face_x is not None) else 0.5) * width
    x = int(round(centre - cw / 2))
    x = max(0, min(int(width) - cw, x))
    return {"mode": mode, "x": x, "w": cw}


def sample_frames(video: Path, start: float, end: float, samples: int = 12):
    """Yield BGR frames evenly spread over [start, end]. Needs OpenCV."""
    import cv2
    cap = cv2.VideoCapture(str(video))
    try:
        span = max(end - start, 0.01)
        for i in range(samples):
            t = start + span * (i + 0.5) / samples
            cap.set(cv2.CAP_PROP_POS_MSEC, t * 1000.0)
            ok, frame = cap.read()
            if ok and frame is not None:
                yield frame
    finally:
        cap.release()


def analyse(video: Path, start: float, end: float, samples: int = 12) -> dict:
    """Face ratio, smoothed face centre (normalised x) and a screen-recording guess."""
    try:
        import cv2
        import numpy as np
    except ImportError:  # OpenCV is optional; without it we never claim a face
        return {"face_ratio": 0.0, "face_x": None, "screen_like": False, "samples": 0}
    cascade = cv2.CascadeClassifier(cv2.data.haarcascades + "haarcascade_frontalface_default.xml")
    xs, n, edge_density, sat = [], 0, [], []
    for frame in sample_frames(video, start, end, samples):
        n += 1
        h, w = frame.shape[:2]
        scale = 480.0 / max(w, 1) if w > 480 else 1.0
        small = cv2.resize(frame, (int(w * scale), int(h * scale))) if scale != 1.0 else frame
        gray = cv2.cvtColor(small, cv2.COLOR_BGR2GRAY)
        faces = cascade.detectMultiScale(gray, scaleFactor=1.1, minNeighbors=5,
                                         minSize=(max(20, gray.shape[0] // 12),) * 2)
        if len(faces):
            fx, fy, fw, fh = max(faces, key=lambda f: f[2] * f[3])
            xs.append((fx + fw / 2) / gray.shape[1])
        edges = cv2.Canny(gray, 100, 200)
        edge_density.append(float((edges > 0).mean()))
        hsv = cv2.cvtColor(small, cv2.COLOR_BGR2HSV)
        sat.append(float(hsv[..., 1].mean()) / 255.0)
    if not n:
        return {"face_ratio": 0.0, "face_x": None, "screen_like": False, "samples": 0}
    face_x = float(np.median(xs)) if xs else None
    # Screens: lots of fine edges (text/UI) and mostly desaturated pixels.
    screen_like = bool(np.median(edge_density) > 0.06 and np.median(sat) < 0.25)
    return {"face_ratio": len(xs) / n, "face_x": face_x, "screen_like": screen_like, "samples": n}


def choose_crop(video: Path, start: float, end: float, src_w: int, src_h: int,
                mode: str | None = None) -> dict:
    """Pick (or apply a forced) mode and compute the crop box in source px.
    `video` may be the proxy; positions are normalised so its size doesn't matter."""
    info = {"face_ratio": 0.0, "face_x": None, "screen_like": False}
    if src_h <= src_w or mode == "face":
        info = analyse(Path(video), start, end)
    if mode is None:
        mode = decide_mode(src_w, src_h, info["face_ratio"], info["screen_like"])
    box = crop_box(src_w, src_h, mode, info["face_x"])
    box["face_ratio"] = round(float(info["face_ratio"]), 3)
    return box


def filtergraph(crop: dict, src_w: int, src_h: int, inp: str = "0:v", out: str = "vr") -> str:
    """FFmpeg filter_complex fragment turning [inp] into a 1080x1920 [out]."""
    mode = crop.get("mode", "center")
    if mode == "fit":
        return (f"[{inp}]scale={OUT_W}:{OUT_H}:force_original_aspect_ratio=decrease,"
                f"pad={OUT_W}:{OUT_H}:(ow-iw)/2:(oh-ih)/2:black,setsar=1[{out}]")
    if mode == "fit_blur":
        return (f"[{inp}]split[rb][rf];"
                f"[rb]scale=270:480:force_original_aspect_ratio=increase,crop=270:480,"
                f"boxblur=10:2,scale={OUT_W}:{OUT_H},setsar=1[rbg];"
                f"[rf]scale={OUT_W}:{OUT_H}:force_original_aspect_ratio=decrease,setsar=1[rfg];"
                f"[rbg][rfg]overlay=(W-w)/2:(H-h)/2[{out}]")
    w = int(crop.get("w") or _even(src_h * 9 / 16))
    x = int(crop.get("x") or 0)
    w = min(w, src_w)
    x = max(0, min(src_w - w, x))
    return f"[{inp}]crop={w}:{src_h}:{x}:0,scale={OUT_W}:{OUT_H},setsar=1[{out}]"
