"""Karaoke-style ASS captions for a 1080x1920 Reel.

Safe zone (Instagram UI): nothing in the top 250 px, the bottom 450 px or
the right 140 px. Captions sit at the bottom of the safe zone, the hook at
its top for the first few seconds.
"""
from __future__ import annotations

import re

PLAY_W, PLAY_H = 1080, 1920
SAFE_TOP, SAFE_BOTTOM, SAFE_RIGHT = 250, 450, 140
MARGIN_L = 80
CAPTION_MARGIN_V = SAFE_BOTTOM + 70     # bottom-aligned captions
HOOK_MARGIN_V = SAFE_TOP + 50           # top-aligned hook
HOOK_SECONDS = 2.5
WORDS_PER_LINE = 4
MAX_LINES = 2
HIGHLIGHT = "&H0000E5FF&"               # ASS BGR: warm yellow
WHITE = "&H00FFFFFF&"
_PUNCT = re.compile(r"^(\W*)(.*?)(\W*)$", re.S)


def clamp_size(size) -> int:
    try:
        size = int(size)
    except (TypeError, ValueError):
        size = 72
    return max(64, min(80, size))


def window_words(words: list[dict], start: float, end: float, speed: float = 1.0) -> list[dict]:
    """Words fully/mostly inside [start, end], shifted so the window starts at 0."""
    out = []
    speed = speed or 1.0
    for w in words or []:
        ws, we = float(w.get("start", 0)), float(w.get("end", 0))
        text = str(w.get("word", "")).strip()
        if not text or (ws + we) / 2 < start or (ws + we) / 2 > end:
            continue
        out.append({"start": max(0.0, ws - start) / speed,
                    "end": max(0.0, min(we, end) - start) / speed,
                    "word": text, "p": w.get("p", 1.0)})
    return out


def apply_fixes(words: list[dict], fixes: dict | None) -> list[dict]:
    """Replace words case-insensitively (punctuation around them is kept)."""
    if not fixes:
        return [dict(w) for w in words]
    table = {str(k).lower(): str(v) for k, v in fixes.items()}
    out = []
    for w in words:
        lead, core, trail = _PUNCT.match(w["word"]).groups()
        fixed = table.get(core.lower()) or table.get(w["word"].lower())
        if fixed is not None and core.lower() in table:
            w = {**w, "word": f"{lead}{fixed}{trail}"}
        elif fixed is not None:
            w = {**w, "word": fixed}
        else:
            w = dict(w)
        out.append(w)
    return out


def _ends_sentence(word: str) -> bool:
    return word.rstrip("\"')”’").endswith((".", "!", "?"))


def split_lines(words: list[dict], per_line: int = WORDS_PER_LINE,
                max_lines: int = MAX_LINES) -> list[list[list[dict]]]:
    """Group words into pages of ≤ max_lines lines of ≤ per_line words.
    A sentence end closes the current line and a long pause the page."""
    pages, page, line = [], [], []

    def close_line():
        nonlocal line, page
        if line:
            page.append(line)
            line = []
        if len(page) >= max_lines:
            close_page()

    def close_page():
        nonlocal page
        if page:
            pages.append(page)
            page = []

    prev_end = None
    for w in words:
        if prev_end is not None and w["start"] - prev_end > 1.0:
            close_line()
            close_page()
        line.append(w)
        prev_end = w["end"]
        if len(line) >= per_line or _ends_sentence(w["word"]):
            close_line()
    close_line()
    close_page()
    return pages


def _esc(text: str) -> str:
    return text.replace("\\", "/").replace("{", "(").replace("}", ")").replace("\n", " ")


def _ts(t: float) -> str:
    t = max(0.0, t)
    cs = int(round(t * 100))
    h, cs = divmod(cs, 360000)
    m, cs = divmod(cs, 6000)
    s, cs = divmod(cs, 100)
    return f"{h}:{m:02d}:{s:02d}.{cs:02d}"


def header(font: str, size: int) -> str:
    size = clamp_size(size)
    hook_size = int(size * 1.4)
    fmt = ("Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, "
           "BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, "
           "BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding")
    cap = (f"Style: Caption,{font},{size},&H00FFFFFF,&H00FFFFFF,&H00000000,&H64000000,"
           f"-1,0,0,0,100,100,0,0,1,5,2,2,{MARGIN_L},{SAFE_RIGHT + 20},{CAPTION_MARGIN_V},1")
    hook = (f"Style: Hook,{font},{hook_size},&H00FFFFFF,&H00FFFFFF,&H00000000,&H64000000,"
            f"-1,0,0,0,100,100,0,0,1,6,3,8,{MARGIN_L},{SAFE_RIGHT + 20},{HOOK_MARGIN_V},1")
    return "\n".join([
        "[Script Info]", "ScriptType: v4.00+", f"PlayResX: {PLAY_W}", f"PlayResY: {PLAY_H}",
        "WrapStyle: 0", "ScaledBorderAndShadow: yes", "",
        "[V4+ Styles]", fmt, cap, hook, "",
        "[Events]",
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text",
    ])


def events(words: list[dict], hook_text: str = "", duration: float | None = None) -> list[str]:
    lines = []
    if hook_text and hook_text.strip():
        end = min(HOOK_SECONDS, duration) if duration else HOOK_SECONDS
        lines.append(f"Dialogue: 1,{_ts(0)},{_ts(end)},Hook,,0,0,0,,{_esc(hook_text.strip())}")
    pages = split_lines(words)
    for pi, page in enumerate(pages):
        flat = [w for ln in page for w in ln]
        next_page_start = None
        if pi + 1 < len(pages):
            next_page_start = pages[pi + 1][0][0]["start"]
        for i, cur in enumerate(flat):
            start = cur["start"]
            if i + 1 < len(flat):
                end = flat[i + 1]["start"]
            else:
                end = cur["end"] + 0.3
                if next_page_start is not None:
                    end = min(end, next_page_start)
            if duration:
                end = min(end, duration)
            if end <= start:
                end = start + 0.05
            text = "\\N".join(
                " ".join((f"{{\\c{HIGHLIGHT}}}{_esc(w['word'])}{{\\c{WHITE}}}" if w is cur
                          else _esc(w["word"])) for w in ln)
                for ln in page)
            lines.append(f"Dialogue: 0,{_ts(start)},{_ts(end)},Caption,,0,0,0,,{text}")
    return lines


def build_ass(words: list[dict], hook_text: str = "", font: str = "Montserrat",
              size: int = 72, duration: float | None = None,
              word_fixes: dict | None = None) -> str:
    """words: already windowed (start at 0). No words -> no caption events."""
    words = apply_fixes(words, word_fixes)
    return header(font, size) + "\n" + "\n".join(events(words, hook_text, duration)) + "\n"


def burned_words(ass_text: str) -> list[str]:
    """The plain words shown on screen (for QA), from Caption + Hook events."""
    out = []
    for line in ass_text.splitlines():
        if not line.startswith("Dialogue:"):
            continue
        text = line.split(",", 9)[9]
        text = re.sub(r"\{[^}]*\}", "", text).replace("\\N", " ")
        out.extend(text.split())
    return out
