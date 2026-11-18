"""The only door to Claude at runtime.

- Hard daily cap (cfg.claude_calls_per_day), counted from claude_calls.
- Every call is logged, success or not.
- Output is validated against a JSON schema; invalid output is a failure.
- Any failure raises ClaudeUnavailable so callers can fall back.

Runs `claude -p` headless with no tools and no session persistence, so it
uses your normal subscription limits and nothing else.
"""
from __future__ import annotations

import json
import re
import subprocess
from datetime import datetime, timezone
from typing import Callable

from .. import db
from ..config import Config


class ClaudeUnavailable(RuntimeError):
    """Cap reached, rate-limited, CLI missing, timed out or bad output."""


Runner = Callable[[list[str], str, int], subprocess.CompletedProcess]


def _default_runner(cmd: list[str], stdin: str, timeout: int) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, input=stdin, capture_output=True, text=True, timeout=timeout)


def calls_today(conn) -> int:
    start = datetime.now().astimezone().replace(hour=0, minute=0, second=0, microsecond=0)
    start_utc = start.astimezone(timezone.utc).isoformat(timespec="seconds")
    return conn.execute("SELECT count(*) FROM claude_calls WHERE ts >= ?", (start_utc,)).fetchone()[0]


def validate(data, schema: dict, path: str = "$") -> None:
    """Minimal JSON-schema check: type, required, properties, items, enum,
    minLength/maxLength, minimum/maximum, minItems/maxItems."""
    t = schema.get("type")
    types = {"object": dict, "array": list, "string": str, "integer": int,
             "number": (int, float), "boolean": bool}
    if t and not isinstance(data, types[t]) or (t in ("integer", "number") and isinstance(data, bool)):
        raise ValueError(f"{path}: expected {t}")
    if "enum" in schema and data not in schema["enum"]:
        raise ValueError(f"{path}: {data!r} not in {schema['enum']}")
    if isinstance(data, str):
        if len(data) < schema.get("minLength", 0) or len(data) > schema.get("maxLength", 10**9):
            raise ValueError(f"{path}: length {len(data)} out of range")
    if isinstance(data, (int, float)) and not isinstance(data, bool):
        if data < schema.get("minimum", float("-inf")) or data > schema.get("maximum", float("inf")):
            raise ValueError(f"{path}: {data} out of range")
    if isinstance(data, dict):
        for k in schema.get("required", []):
            if k not in data:
                raise ValueError(f"{path}: missing {k}")
        for k, sub in schema.get("properties", {}).items():
            if k in data:
                validate(data[k], sub, f"{path}.{k}")
    if isinstance(data, list):
        if len(data) < schema.get("minItems", 0) or len(data) > schema.get("maxItems", 10**9):
            raise ValueError(f"{path}: {len(data)} items out of range")
        for i, item in enumerate(data):
            if "items" in schema:
                validate(item, schema["items"], f"{path}[{i}]")


def _extract(stdout: str) -> dict:
    """`claude -p --output-format json` prints one envelope. With --json-schema
    the answer is in structured_output; otherwise parse the result text."""
    env = json.loads(stdout)
    if env.get("is_error"):
        raise ClaudeUnavailable(f"claude reported an error: {str(env.get('result'))[:300]}")
    if isinstance(env.get("structured_output"), (dict, list)):
        return env["structured_output"]
    text = env.get("result") or ""
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        raise ClaudeUnavailable("no JSON object in claude output")
    return json.loads(m.group(0))


def call(conn, cfg: Config, purpose: str, prompt: str, schema: dict,
         job_id: int | None = None, runner: Runner | None = None) -> dict:
    runner = runner or _default_runner
    cap = cfg.claude_calls_per_day
    if calls_today(conn) >= cap:
        conn.execute("INSERT INTO claude_calls(ts, job_id, purpose, ok, detail) VALUES (?,?,?,?,?)",
                     (db.now(), job_id, purpose, 0, "skipped: daily cap reached"))
        # the skipped row counts too, so a cap is never exceeded by retries
        raise ClaudeUnavailable(f"daily Claude cap of {cap} reached")

    cmd = [cfg.claude.get("command", "claude"), "-p", "--output-format", "json",
           "--no-session-persistence", "--tools", "", "--json-schema", json.dumps(schema)]
    if cfg.claude.get("model"):
        cmd += ["--model", cfg.claude["model"]]
    ok, detail = 0, ""
    try:
        try:
            res = runner(cmd, prompt, int(cfg.claude.get("timeout_seconds", 300)))
        except FileNotFoundError as e:
            raise ClaudeUnavailable("claude CLI not found on PATH") from e
        except subprocess.TimeoutExpired as e:
            raise ClaudeUnavailable("claude timed out") from e
        if res.returncode != 0:
            msg = (res.stderr or res.stdout or "").strip()[-300:]
            raise ClaudeUnavailable(f"claude exited {res.returncode}: {msg}")
        try:
            data = _extract(res.stdout)
            validate(data, schema)
        except (ValueError, json.JSONDecodeError) as e:
            raise ClaudeUnavailable(f"invalid output: {e}") from e
        ok, detail = 1, "ok"
        return data
    except ClaudeUnavailable as e:
        detail = str(e)[:500]
        raise
    finally:
        conn.execute("INSERT INTO claude_calls(ts, job_id, purpose, ok, detail) VALUES (?,?,?,?,?)",
                     (db.now(), job_id, purpose, ok, detail))
