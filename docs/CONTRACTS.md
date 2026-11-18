# Contracts between agents

Every agent module follows these rules. Shared code (`reel/db.py`,
`reel/config.py`, `reel/scheduler.py`, `reel/util.py`, `reel/llm/`,
`reel/cli.py`) is owned by the integrator; agents don't edit it.

## Step functions

```python
def step(conn, job, cfg) -> None | State | scheduler.STAY
```

- `job` is a sqlite3.Row of `jobs` (id, key, source_dir, workdir, status,
  edit_version, qa_failures, recuts, recut_reason, state_entered_at, ...).
- Return `None` = go to the default next state; a `State` = go there instead;
  `scheduler.STAY` = nothing to do yet. Raise on failure (recorded, retried,
  then NEEDS_HUMAN). To stop immediately: `db.give_up(conn, id, frm, msg)` then
  `return scheduler.STAY`.
- Must be idempotent and write files atomically (`reel.util.write_json`,
  `write_text`, or temp file + `os.replace`).
- Non-status columns change only via `db.set_job(conn, id, edit_version=..)`.
- Something you must see → `db.alert(conn, job_id, text)`. The bot delivers it.
- Each agent module exports `STEPS = {State.X: (step, State.Y)}` and
  optionally `PERIODIC = [fn(conn, cfg)]`. The integrator merges them.
- Claude only via `reel.llm.claude.call(...)` (cap + log + schema). Only two
  purposes exist: `daily_pick` (Scout) and `weekly_playbook` (Analyst).

## State ownership

| From | Step owner | Default next | Other outcomes |
|---|---|---|---|
| NEW | ingest | INGESTED | |
| INGESTED | scout | SCOUTED | |
| SCOUTED | editor | EDITED | |
| EDITED | packager + qa | QA_PASSED | QA_FAILED |
| QA_FAILED | editor | SCOUTED (next candidate) | give_up after 2 |
| RECUT_REQUESTED | editor | EDITED | |
| QA_PASSED | approval (telegram) | AWAITING_APPROVAL | |
| AWAITING_APPROVAL | approval | STAY | EXPIRED_APPROVAL (timeout) |
| APPROVED | publisher | PUBLISHED (api) / STAY (manual, waiting for "Posted") | PUBLISH_FAILED |
| PUBLISH_FAILED | publisher | APPROVED (retry w/ backoff) | give_up |
| PUBLISHED | analyst | STAY until last checkpoint, then MEASURED | |

The bot (outside the scheduler) moves AWAITING_APPROVAL → APPROVED / REJECTED
/ RECUT_REQUESTED, APPROVED → PUBLISHED (manual "Posted"),
PUBLISH_FAILED → APPROVED ("Retry"), EXPIRED_APPROVAL → QA_PASSED ("Revive"),
always with `db.transition` (compare-and-set).

## Files in a job's workdir

```
ingest.json                 (exists) clips[]: file, source, duration, width, height, fps,
                            has_audio, proxy, audio, transcript, scenes; total_seconds
ingest/<clip>.transcript.json  {language, duration, segments:[{start,end,text,
                               words:[{start,end,word,p}]}]}
ingest/<clip>.scenes.json      [{start,end}]
scout/candidates.json
scout/pick.json
edit/v<N>/edit.json         recipe (hand-editable; re-render from it)
edit/v<N>/final.mp4         1080x1920 H.264/AAC, -14 LUFS, burned captions
edit/v<N>/preview.mp4       720x1280, < 45 MB, for Telegram
edit/v<N>/captions.ass
edit/v<N>/cover.jpg
edit/v<N>/package.json
edit/v<N>/qa.json
edit/alt/<candidate_id>/    pre-rendered alternates (same files as v<N>)
```

`jobs.edit_version` = N of the current version (0 = none yet).

### scout/candidates.json
```json
{"candidates": [{
  "id": "c1", "clip": "a.mp4", "source": "/abs/a.mp4", "proxy": "/abs/a.proxy.mp4",
  "start": 12.3, "end": 41.0, "duration": 28.7,
  "kind": "speech | visual | whole",
  "text": "transcript inside the window",
  "score": 0.83, "reasons": ["complete sentence", "high energy", "sharp"],
  "features": {"words_per_sec": 2.9, "sharpness": 0.7, "energy": 0.6, "faces": 0.9, "motion": 0.2,
               "low_conf_words": 1}
}]}
```
Sorted best first, ≤ `cfg.reel["candidates"]`.

### scout/pick.json
```json
{"source": "claude | local_draft | needs_caption",
 "order": ["c3", "c1", "c2"],
 "hook": "on-screen hook, ≤ 8 words",
 "alt_hooks": ["...", "..."],
 "caption": "post caption without hashtags",
 "hashtags": ["#tag"],
 "why": "one line on why this clip",
 "trim": {"c3": {"start": 13.1, "end": 39.8}}}
```
`order[0]` is the chosen clip; recuts/QA retries take the next unused id.

### edit/v<N>/edit.json
```json
{"version": 2, "candidate_id": "c3", "source": "/abs/a.mp4", "start": 13.1, "end": 39.8,
 "crop": {"mode": "face | center | fit_blur | fit", "x": 420, "w": 608},
 "speed": 1.0, "hook_text": "...", "caption_style": {"font": "Montserrat", "size": 72},
 "word_fixes": {"kaisen": "Kaizen"}, "loudnorm": -14, "reason": "first | recut:tighter | qa_retry"}
```

### edit/v<N>/package.json
```json
{"caption_version": 1, "caption": "...", "hashtags": ["#a"], "full_caption": "caption\n\n#a #b",
 "hook": "...", "cover": "/abs/cover.jpg", "source": "claude | local_draft | needs_caption"}
```
A caption edit writes caption_version+1 in place (atomic).

### edit/v<N>/qa.json
```json
{"status": "pass | warn | block", "checks": [{"name": "resolution", "status": "pass", "detail": "1080x1920"}]}
```

## Recut reasons (`jobs.recut_reason`, JSON)
`{"reason": "different_clip"}`, `{"reason": "tighter"}` (target < 20 s),
`{"reason": "bad_crop"}` (cycle face → fit_blur → center),
`{"reason": "fix_captions", "word_fixes": {"wrong": "right"}}`.

## Telegram client (`reel/telegram/api.py`, built by the Telegram agent)
```python
class TelegramAPI:
    def __init__(self, token: str, chat_id: int, session=None): ...
    def send_message(self, text, buttons=None) -> int          # message_id
    def send_video(self, path, caption="", buttons=None) -> int
    def send_document(self, path, caption="", buttons=None) -> int
    def edit_buttons(self, message_id, buttons=None) -> None
    def get_updates(self, offset, timeout=50) -> list[dict]
    def answer_callback(self, callback_id, text="") -> None
def from_config(cfg) -> TelegramAPI | None   # None if no token/chat_id configured
```
`buttons` = list of rows, each a list of `(label, callback_data)`.
callback_data ≤ 64 bytes: `"<action>:<job_id>:<arg>"`.
Other agents use a fake with the same methods in tests.

## Footage reality
Mixed: talking head, vlog, screen recording; 30 s to 20 min a day.
- Short day (total < target): one "whole" candidate = everything, lightly trimmed.
- No speech: choose by scenes + motion + sharpness (kind "visual").
- Screen recordings (no faces, lots of text): crop mode fit_blur, never a
  center crop that cuts off the screen.
