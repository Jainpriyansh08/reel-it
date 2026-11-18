import json
import subprocess

import pytest

from reel.llm import claude

SCHEMA = {"type": "object", "required": ["hook"],
          "properties": {"hook": {"type": "string", "minLength": 1, "maxLength": 80}}}


def runner_returning(payload, code=0):
    calls = []

    def run(cmd, stdin, timeout):
        calls.append((cmd, stdin))
        return subprocess.CompletedProcess(cmd, code, stdout=json.dumps(payload), stderr="")
    run.calls = calls
    return run


def test_structured_output_is_validated_and_logged(conn, cfg):
    run = runner_returning({"structured_output": {"hook": "Stop doing this"}, "is_error": False})
    assert claude.call(conn, cfg, "daily_pick", "prompt", SCHEMA, runner=run) == {"hook": "Stop doing this"}
    cmd, stdin = run.calls[0]
    assert "--json-schema" in cmd and stdin == "prompt"
    row = conn.execute("SELECT * FROM claude_calls").fetchone()
    assert row["ok"] == 1 and row["purpose"] == "daily_pick"


def test_result_text_fallback_parse(conn, cfg):
    run = runner_returning({"result": 'Sure!\n{"hook": "x"}', "is_error": False})
    assert claude.call(conn, cfg, "p", "q", SCHEMA, runner=run)["hook"] == "x"


@pytest.mark.parametrize("payload,code", [
    ({"structured_output": {"hook": ""}}, 0),           # schema violation
    ({"result": "rate limited", "is_error": True}, 0),  # CLI-reported error
    ({"result": "x"}, 1),                               # non-zero exit
])
def test_failures_raise_and_are_logged(conn, cfg, payload, code):
    with pytest.raises(claude.ClaudeUnavailable):
        claude.call(conn, cfg, "p", "q", SCHEMA, runner=runner_returning(payload, code))
    assert conn.execute("SELECT ok FROM claude_calls").fetchone()[0] == 0


def test_daily_cap(conn, cfg):
    run = runner_returning({"structured_output": {"hook": "a"}})
    for _ in range(cfg.claude_calls_per_day):
        claude.call(conn, cfg, "p", "q", SCHEMA, runner=run)
    with pytest.raises(claude.ClaudeUnavailable, match="cap"):
        claude.call(conn, cfg, "p", "q", SCHEMA, runner=run)
    assert len(run.calls) == cfg.claude_calls_per_day


def test_missing_cli(conn, cfg):
    def run(cmd, stdin, timeout):
        raise FileNotFoundError
    with pytest.raises(claude.ClaudeUnavailable, match="not found"):
        claude.call(conn, cfg, "p", "q", SCHEMA, runner=run)
