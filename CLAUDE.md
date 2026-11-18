# Daily Reel Pipeline — notes for Claude

- Python 3.11+, one package `reel/`. Agents are plain modules in `reel/agents/`,
  wired into the scheduler via `STEPS` in `reel/scheduler.py`
  (state -> (step function, next state)).
- Every step must be idempotent and write outputs atomically (temp name +
  `os.replace`). Never change job state except through `db.transition` /
  `db.record_failure`.
- No failure may be silent: exceptions in steps are recorded as events;
  repeated failures become `NEEDS_HUMAN` + an `alert` event.
- Nothing ever publishes without an approval tied to the exact file hash and
  caption version (week 3+).
- Runtime Claude usage is capped by `claude_calls_per_day` and every call is
  logged in `claude_calls`. Don't add Claude calls to other steps.
- $0 rule: no paid APIs, no AGPL or non-commercial components.
- Tests: `pytest`. Heavy models are injected as fake backends; real FFmpeg is
  used. Work tests-first, one module per session.
