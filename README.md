# Daily Reel Pipeline

Drop the day's footage in a folder; a ready-to-approve Instagram Reel arrives
on your phone. Nothing posts without your tap.

Plan and decisions live in the doc *Daily Reel Pipeline — Implementation Plan*.

## Status

| Week | Piece | State |
|---|---|---|
| 1 | SQLite state machine, `reel tick`, Ingest, launchd install | **done** |
| 2 | Shot Scout, daily Claude pick (capped, with fallback), Editor | next |
| 3 | Telegram bot: approve / caption / recut / reject, alerts, heartbeat | |
| 4–5 | QA Reviewer, Packager, Publisher | |
| 6+ | Analyst, weekly playbook, digest | |

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

## Daily use

- Copy a day's clips into `~/ReelInbox/` (loose files are filed into a
  dated folder) or into `~/ReelInbox/<any-name>/`.
- After 10 quiet minutes (`settle_seconds`) the folder becomes a job.
- `reel status` lists jobs; `reel events` shows the log; `reel retry ID`
  re-queues a job that gave up.

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
