# Daily Reel Pipeline

Drop the day's footage in a folder; a ready-to-approve Instagram Reel arrives
on your phone. Nothing posts without your tap.

Plan and decisions live in the doc *Daily Reel Pipeline — Implementation Plan*.

## Status

All eight agents are built and tested together (`tests/test_e2e.py` runs a
day of footage from the inbox to PUBLISHED with real FFmpeg renders).

| Agent | Module | State |
|---|---|---|
| Ingest | `agents/ingest.py` | built |
| Shot Scout + daily Claude pick | `agents/scout.py` | built |
| Editor (reframe, captions, recuts) | `agents/editor.py`, `media/` | built |
| Packager + QA | `agents/packager.py`, `agents/qa.py` | built |
| Telegram approval bot | `agents/approval.py`, `telegram/` | built |
| Publisher (manual + Graph API) | `agents/publisher.py`, `meta/graph.py` | built |
| Analyst (insights, playbook, digest) | `agents/analyst.py` | built |

Not yet proven on real services: Whisper on your Mac, a real Telegram bot,
Meta's API (that is Week 0 spike 1). Start in `publish.mode: manual`.

## Install (Apple Silicon Mac)

```bash
git clone <this repo> ~/reel-pipeline && cd ~/reel-pipeline
./scripts/install_macos.sh
```

This installs FFmpeg and Python 3.12 via Homebrew, creates a venv in
`~/.reel/venv`, writes `~/.config/reel/{config,brand}.yaml`, downloads the
Whisper model, and registers a launchd job that runs `reel tick` every 5 min.

Keep the inbox outside Desktop/Documents/iCloud Drive (the default is
`~/ReelInbox`): macOS blocks background jobs from those folders unless you
grant Full Disk Access.

## Connect Telegram (5 minutes)

1. In Telegram, message **@BotFather** → `/newbot` → copy the token.
2. Save it: `echo 'TOKEN' > ~/.config/reel/telegram_token && chmod 600 ~/.config/reel/telegram_token`
3. Send your bot any message, then run `reel bot --whoami` and put the
   number in `~/.config/reel/config.yaml` as `telegram.chat_id`.
4. Re-run `./scripts/install_macos.sh` to start the bot service.

## Auto-publishing (later, after spike 1)

Keep `publish.mode: manual` until the Meta spike passes. Then: link your IG
professional account to a Facebook Page, create a Meta app in dev mode with
Facebook Login for Business, get a long-lived Page token into
`~/.config/reel/meta_token`, set `publish.ig_user_id` and `publish.mode: api`.
Run `reel baseline` once to record your last 20 Reels before going live.

## Daily use

- Copy a day's clips into `~/ReelInbox/` (loose files are filed into a
  dated folder) or into `~/ReelInbox/<any-name>/`.
- After 10 quiet minutes (`settle_seconds`) the folder becomes a job.
- Overnight it ingests, picks a clip (one Claude call), edits, checks, and
  sends a preview to Telegram: ✅ Approve · ✏️ Caption · 🔁 Recut · ❌ Reject.
- Manual mode: after Approve the bot sends the file and caption; post it in
  Instagram and tap ✅ Posted.
- Telegram commands: `/status`, `/vocab add Kaizen`, `/ban add hack`,
  `/pause 3d`, `/retry 12`.
- On the Mac: `reel status`, `reel events`, `reel retry ID`.

## How it works

```
NEW → INGESTED → SCOUTED → EDITED → QA_PASSED → AWAITING_APPROVAL → APPROVED → PUBLISHED → MEASURED
```

- Each state change is a compare-and-set in SQLite, so a step can never move
  a job twice.
- Step outputs are written to a temp name and renamed, so a crash leaves the
  job in its old state and the next tick re-runs it; finished clips are skipped.
- A failing step is retried on the next ticks; after `max_attempts` the job
  goes to `NEEDS_HUMAN` with an `alert` event (sent to Telegram from week 3).
- Overlapping ticks are prevented with a file lock.

Ingest output per job (`~/ReelWork/jobs/<day>/`): `ingest.json` manifest,
and per clip a 720p proxy, 16 kHz WAV, word-timestamped transcript
(faster-whisper `large-v3-turbo`, VAD on, hotwords from `brand.yaml` vocab)
and scene cuts (PySceneDetect).

## Development

```bash
pip install -e '.[dev]'     # tests use fake transcription/scene backends
pytest
```
