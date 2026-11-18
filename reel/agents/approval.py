"""Approval gate: sends the preview to Telegram and waits for your tap.

QA_PASSED -> AWAITING_APPROVAL: send preview + buttons, record an approvals
row tied to the exact final.mp4 hash and caption version.
AWAITING_APPROVAL: remind once, expire after a while. Never auto-approves.
The bot (reel/telegram/bot.py) does the actual approve/reject/recut.
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

from reel import db, scheduler
from reel.states import State
from reel.telegram.api import TelegramError, from_config
from reel.util import edit_dir, read_json, sha256_file, sha256_text

# --------------------------------------------------------------- helpers


def require_api(cfg, api=None):
    api = api or from_config(cfg)
    if api is None:
        raise RuntimeError("Telegram is not configured (token / telegram.chat_id missing)")
    return api


def version_dir(job) -> Path:
    return edit_dir(Path(job["workdir"]), int(job["edit_version"]))


def read_package(job) -> dict:
    return read_json(version_dir(job) / "package.json")


def _read_opt(path: Path, default):
    try:
        return read_json(path)
    except (FileNotFoundError, ValueError):
        return default


def read_pick(job) -> dict:
    return _read_opt(Path(job["workdir"]) / "scout" / "pick.json", {})


def full_caption(caption: str, hashtags) -> str:
    caption = (caption or "").strip()
    tags = " ".join(hashtags or [])
    return f"{caption}\n\n{tags}".strip() if tags else caption


def latest_approval(conn, job_id):
    return conn.execute("SELECT * FROM approvals WHERE job_id=? ORDER BY id DESC LIMIT 1",
                        (job_id,)).fetchone()


def pending_approval(conn, job_id):
    return conn.execute("SELECT * FROM approvals WHERE job_id=? AND status='pending'"
                        " ORDER BY id DESC LIMIT 1", (job_id,)).fetchone()


def supersede(conn, job_id, api=None) -> list[int]:
    """Mark every pending approval of the job superseded (and strip its
    buttons in Telegram, best effort). Returns the old message ids."""
    rows = conn.execute("SELECT id, tg_message_id FROM approvals WHERE job_id=? AND status='pending'",
                        (job_id,)).fetchall()
    conn.execute("UPDATE approvals SET status='superseded', decided_at=? WHERE job_id=? AND status='pending'",
                 (db.now(), job_id))
    msgs = [r["tg_message_id"] for r in rows if r["tg_message_id"]]
    if api is not None:
        for m in msgs:
            try:
                api.edit_buttons(m, None)
            except Exception:  # noqa: BLE001 - cosmetic only
                pass
    return msgs


def needs_caption(pkg: dict) -> bool:
    return pkg.get("source") == "needs_caption" and not pkg.get("caption_by_human")


def approval_buttons(job_id: int, approval_id: int, pkg: dict) -> list:
    row1 = [] if needs_caption(pkg) else [("✅ Approve", f"ap:{job_id}:{approval_id}")]
    row1.append(("✏️ Caption", f"ed:{job_id}:{approval_id}"))
    return [row1, [("🔁 Recut", f"rc:{job_id}:{approval_id}"),
                   ("❌ Reject", f"rj:{job_id}:{approval_id}")]]


def duration_of(vdir: Path, qa: dict) -> float | None:
    edit = _read_opt(vdir / "edit.json", {})
    try:
        return (float(edit["end"]) - float(edit["start"])) / float(edit.get("speed") or 1.0)
    except (KeyError, TypeError, ValueError, ZeroDivisionError):
        pass
    for c in qa.get("checks", []):
        if c.get("name") == "duration":
            m = re.search(r"[\d.]+", str(c.get("detail", "")))
            if m:
                return float(m.group())
    return None


def qa_warnings(qa: dict) -> list[str]:
    out = []
    for c in qa.get("checks", []):
        if c.get("status") not in (None, "pass"):
            detail = c.get("detail") or c.get("name")
            out.append(f"⚠ {detail}" if detail else f"⚠ {c.get('name')}")
    return out


def preview_text(job, pkg: dict, qa: dict, pick: dict, note: str = "") -> str:
    vdir = version_dir(job)
    lines = []
    if note:
        lines += [note, ""]
    if not pkg.get("caption_by_human"):
        if pkg.get("source") == "needs_caption":
            lines += ["✍️ Claude was unavailable — write a caption (✏️ Caption → Write new)", ""]
        elif pkg.get("source") not in (None, "claude"):
            lines += ["📝 LOCAL DRAFT — caption written without Claude, check it", ""]
    dur = duration_of(vdir, qa)
    head = f"🎬 Job {job['id']} · v{job['edit_version']} · caption v{pkg.get('caption_version', 1)}"
    if dur is not None:
        head += f" · {dur:.1f}s"
    lines.append(head)
    if pkg.get("hook"):
        lines.append(f"Hook: {pkg['hook']}")
    lines += ["", pkg.get("full_caption") or full_caption(pkg.get("caption"), pkg.get("hashtags")) or "(no caption)"]
    if pick.get("why"):
        lines += ["", f"Why: {pick['why']}"]
    warns = qa_warnings(qa)
    if warns:
        lines += [""] + warns
    return "\n".join(lines)


def send_preview(conn, job, cfg, api, note: str = "") -> int:
    """Create (or reuse) the pending approval for the job's current version
    and caption, and send the preview with buttons. Returns approval id.
    Idempotent: an identical pending approval that was already sent is
    not sent again."""
    job = db.get_job(conn, job["id"])
    vdir = version_dir(job)
    pkg = read_json(vdir / "package.json")
    qa = _read_opt(vdir / "qa.json", {})
    pick = read_pick(job)
    caption = pkg.get("full_caption") or full_caption(pkg.get("caption"), pkg.get("hashtags"))
    file_sha = sha256_file(vdir / "final.mp4")
    cap_sha = sha256_text(caption)
    cap_ver = int(pkg.get("caption_version", 1))

    same = conn.execute(
        "SELECT * FROM approvals WHERE job_id=? AND status='pending' AND edit_version=?"
        " AND caption_version=? AND file_sha256=? AND caption_sha256=? ORDER BY id DESC LIMIT 1",
        (job["id"], job["edit_version"], cap_ver, file_sha, cap_sha)).fetchone()
    if same and same["tg_message_id"]:
        return same["id"]
    if same:
        approval_id = same["id"]  # row made but the send never finished
        conn.execute("UPDATE approvals SET status='superseded', decided_at=? "
                     "WHERE job_id=? AND status='pending' AND id<>?", (db.now(), job["id"], approval_id))
    else:
        supersede(conn, job["id"], api)
        cur = conn.execute(
            "INSERT INTO approvals(job_id, edit_version, caption_version, file_sha256, caption_sha256,"
            " caption, status, created_at) VALUES (?,?,?,?,?,?, 'pending', ?)",
            (job["id"], job["edit_version"], cap_ver, file_sha, cap_sha, caption, db.now()))
        approval_id = cur.lastrowid

    text = preview_text(job, pkg, qa, pick, note)
    buttons = approval_buttons(job["id"], approval_id, pkg)
    preview = vdir / "preview.mp4"
    if not preview.exists():
        preview = vdir / "final.mp4"
    if len(text) <= 1024:
        msg_id = api.send_video(preview, caption=text, buttons=buttons)
    else:  # Telegram caps media captions at 1024 chars: video, then text + buttons
        api.send_video(preview, caption=f"🎬 Job {job['id']} · v{job['edit_version']}")
        msg_id = api.send_message(text, buttons=buttons)
    conn.execute("UPDATE approvals SET tg_message_id=? WHERE id=?", (msg_id, approval_id))
    db.log(conn, job["id"], "info", f"preview sent (approval {approval_id}, caption v{cap_ver})")
    return approval_id


# --------------------------------------------------------------- steps


def request_approval(conn, job, cfg, api=None):
    """QA_PASSED -> AWAITING_APPROVAL."""
    api = require_api(cfg, api)
    send_preview(conn, job, cfg, api)
    return None


def _parse(ts: str) -> datetime:
    return datetime.fromisoformat(ts)


def await_approval(conn, job, cfg, api=None, now: datetime | None = None):
    """AWAITING_APPROVAL: remind once, expire eventually, never approve."""
    now = now or datetime.now(timezone.utc)
    ap = pending_approval(conn, job["id"])
    if ap is None or not ap["tg_message_id"]:
        # e.g. the bot crashed between superseding and re-sending
        send_preview(conn, job, cfg, require_api(cfg, api))
        return scheduler.STAY
    if ap["edit_version"] != job["edit_version"]:
        send_preview(conn, job, cfg, require_api(cfg, api), note="🔄 New version")
        return scheduler.STAY
    age = now - _parse(ap["created_at"])
    t = cfg.telegram
    if age >= timedelta(hours=float(t.get("expire_after_hours", 20))):
        api = require_api(cfg, api)
        try:
            api.edit_buttons(ap["tg_message_id"], None)
        except TelegramError:
            pass
        api.send_message(f"⏭ Skipped today — job {job['id']} was not approved in time.",
                         buttons=[[("♻️ Revive", f"rv:{job['id']}")]])
        conn.execute("UPDATE approvals SET status='expired', decided_at=? WHERE id=? AND status='pending'",
                     (db.now(), ap["id"]))
        return State.EXPIRED_APPROVAL
    if not ap["reminded_at"] and age >= timedelta(hours=float(t.get("remind_after_hours", 4))):
        api = require_api(cfg, api)
        pkg = _read_opt(version_dir(job) / "package.json", {})
        api.send_message(f"⏰ Job {job['id']} is still waiting for your OK (preview above).",
                         buttons=approval_buttons(job["id"], ap["id"], pkg))
        conn.execute("UPDATE approvals SET reminded_at=? WHERE id=?", (db.now(), ap["id"]))
    return scheduler.STAY


STEPS = {
    State.QA_PASSED: (request_approval, State.AWAITING_APPROVAL),
    State.AWAITING_APPROVAL: (await_approval, State.AWAITING_APPROVAL),
}

# --------------------------------------------------------------- periodic


def _local_now(now=None) -> datetime:
    return (now or datetime.now()).astimezone()


def _past(local: datetime, hhmm: str) -> bool:
    h, m = (int(x) for x in str(hhmm).split(":"))
    return (local.hour, local.minute) >= (h, m)


def _jobs_created_on(conn, day) -> list:
    rows = conn.execute("SELECT * FROM jobs ORDER BY id DESC LIMIT 50").fetchall()
    return [r for r in rows if _parse(r["created_at"]).astimezone().date() == day]


def deliver_alerts(conn, cfg, api=None) -> int:
    """Send unsent `alert` events; mark sent only after Telegram accepted."""
    api = api or from_config(cfg)
    if api is None:
        return 0
    rows = conn.execute("SELECT * FROM events WHERE kind='alert' AND sent=0 ORDER BY id LIMIT 20").fetchall()
    n = 0
    for r in rows:
        prefix = f"⚠️ Job {r['job_id']}: " if r["job_id"] else "⚠️ "
        buttons = None
        if r["job_id"]:
            job = db.get_job(conn, r["job_id"])
            if job and job["status"] == State.NEEDS_HUMAN:
                buttons = [[("🔁 Retry", f"rt:{r['job_id']}")]]
        try:
            api.send_message(prefix + r["message"], buttons=buttons)
        except TelegramError as e:
            db.log(conn, r["job_id"], "error", f"alert delivery failed: {e}")
            break  # keep the rest unsent; next tick tries again
        conn.execute("UPDATE events SET sent=1 WHERE id=?", (r["id"],))
        n += 1
    return n


def footage_deadline_check(conn, cfg, now: datetime | None = None) -> bool:
    if db.is_paused(conn):
        return False
    local = _local_now(now)
    deadline = cfg.telegram.get("footage_deadline", "10:00")
    today = local.date().isoformat()
    if not _past(local, deadline) or db.kv_get(conn, "footage_alert_date") == today:
        return False
    if _jobs_created_on(conn, local.date()):
        return False
    db.alert(conn, None, f"No footage by {deadline} — drop today's clips in {cfg.inbox}")
    db.kv_set(conn, "footage_alert_date", today)
    return True


def status_line(conn, day=None) -> str:
    from reel.llm.claude import calls_today
    day = day or _local_now().date()
    jobs = _jobs_created_on(conn, day)
    js = ", ".join(f"#{j['id']} {j['status']}" for j in jobs) or "no jobs"
    return f"💓 {day.isoformat()}: {js} · Claude calls today: {calls_today(conn)}"


def heartbeat(conn, cfg, api=None, now: datetime | None = None) -> bool:
    if db.is_paused(conn):
        return False
    local = _local_now(now)
    today = local.date().isoformat()
    if not _past(local, cfg.telegram.get("heartbeat", "21:00")) or db.kv_get(conn, "heartbeat_date") == today:
        return False
    api = api or from_config(cfg)
    if api is None:
        return False
    api.send_message(status_line(conn, local.date()))
    db.kv_set(conn, "heartbeat_date", today)
    return True


PERIODIC = [deliver_alerts, footage_deadline_check, heartbeat]

