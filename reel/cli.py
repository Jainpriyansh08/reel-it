"""reel — command line.

  reel init                 create folders, config.yaml, brand.yaml, database
  reel tick                 one scheduler pass (launchd runs this every 5 min)
  reel status               jobs and their states
  reel events [-n 20]       recent log, newest first
  reel retry JOB_ID         put a NEEDS_HUMAN job back where it got stuck
  reel bot [--whoami|--once] Telegram bot (launchd keeps it running)
  reel baseline             record your last 20 Reels' metrics before going live
"""
from __future__ import annotations

import argparse
import os
import shutil
import sys
from pathlib import Path

from . import db
from .config import Config
from .scheduler import tick

DEFAULT_CONFIG_PATH = Path(os.environ.get("REEL_CONFIG", "~/.config/reel/config.yaml")).expanduser()
TEMPLATES = Path(__file__).resolve().parent.parent / "config"


def cmd_init(args, cfg_path: Path) -> int:
    cfg_path.parent.mkdir(parents=True, exist_ok=True)
    for name in ("config.yaml", "brand.yaml"):
        dest = cfg_path.with_name(name)
        if dest.exists():
            print(f"kept     {dest}")
        else:
            shutil.copy(TEMPLATES / f"{name}.example", dest)
            print(f"created  {dest}")
    cfg = Config.load(cfg_path)
    cfg.inbox.mkdir(parents=True, exist_ok=True)
    cfg.work.mkdir(parents=True, exist_ok=True)
    db.connect(cfg.db).close()
    print(f"inbox    {cfg.inbox}\nwork     {cfg.work}\ndb       {cfg.db}")
    return 0


def cmd_tick(args, cfg: Config) -> int:
    res = tick(cfg)
    if res.locked_out:
        print("another tick is running; skipped")
        return 0
    for j in res.created:
        print(f"job {j}: created")
    for j, frm, to in res.advanced:
        print(f"job {j}: {frm} -> {to}")
    for j, st in res.failed:
        print(f"job {j}: step failed (now {st})", file=sys.stderr)
    return 1 if res.failed else 0


def cmd_status(args, cfg: Config) -> int:
    conn = db.connect(cfg.db)
    rows = conn.execute("SELECT * FROM jobs ORDER BY id DESC LIMIT ?", (args.n,)).fetchall()
    if not rows:
        print(f"no jobs yet. Drop footage into {cfg.inbox}")
    for r in rows:
        extra = f"  attempts={r['attempts']}" if r["attempts"] else ""
        if r["status"] == "NEEDS_HUMAN":
            extra += f"  stuck_at={r['stuck_at']}"
        print(f"{r['id']:>4}  {r['key']:<24} {r['status']:<18} {r['updated_at']}{extra}")
        if r["last_error"]:
            print("      " + r["last_error"].splitlines()[0][:160])
    return 0


def cmd_events(args, cfg: Config) -> int:
    conn = db.connect(cfg.db)
    for r in conn.execute("SELECT * FROM events ORDER BY id DESC LIMIT ?", (args.n,)):
        job = f"job {r['job_id']}" if r["job_id"] else "-"
        print(f"{r['ts']}  {r['kind']:<10} {job:<8} {r['message'].splitlines()[0][:160]}")
    return 0


def cmd_retry(args, cfg: Config) -> int:
    conn = db.connect(cfg.db)
    if db.retry(conn, args.job_id):
        print(f"job {args.job_id}: queued for retry")
        return 0
    print(f"job {args.job_id} is not in NEEDS_HUMAN", file=sys.stderr)
    return 1


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="reel")
    p.add_argument("--config", type=Path, default=DEFAULT_CONFIG_PATH)
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("init")
    sub.add_parser("tick")
    s = sub.add_parser("status"); s.add_argument("-n", type=int, default=20)
    e = sub.add_parser("events"); e.add_argument("-n", type=int, default=30)
    r = sub.add_parser("retry"); r.add_argument("job_id", type=int)
    b = sub.add_parser("bot")
    b.add_argument("--whoami", action="store_true", help="print chat ids that messaged the bot")
    b.add_argument("--once", action="store_true", help="handle one batch of updates and exit")
    sub.add_parser("baseline")
    args = p.parse_args(argv)

    cfg_path = args.config.expanduser()
    if args.cmd == "init":
        return cmd_init(args, cfg_path)
    cfg = Config.load(cfg_path)
    if args.cmd == "bot":
        from .telegram.bot import cli_main
        return cli_main(args, cfg) or 0
    if args.cmd == "baseline":
        from .agents.analyst import cli_baseline
        return cli_baseline(cfg) or 0
    return {"tick": cmd_tick, "status": cmd_status, "events": cmd_events,
            "retry": cmd_retry}[args.cmd](args, cfg)


if __name__ == "__main__":
    raise SystemExit(main())
