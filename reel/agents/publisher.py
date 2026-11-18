"""Publisher: APPROVED -> PUBLISHED (or PUBLISH_FAILED).

Hard rule: nothing is sent or published unless final.mp4 and the full caption
still hash to exactly what was approved.

manual mode: the bot hands you the file + a copy-ready caption with a
[Posted] button; the bot moves the job to PUBLISHED when you tap it.
api mode: at/after publish.post_time (local), posts through the Graph API
with a crash-safe, idempotent sequence recorded in `publications`.
"""
from __future__ import annotations

import time
from datetime import datetime
from pathlib import Path

from .. import db, scheduler
from ..states import State
from ..util import edit_dir, local_now, read_json, sha256_file, sha256_text, today_local

TELEGRAM_VIDEO_LIMIT = 50 * 1024 * 1024
POLL_ATTEMPTS = 10          # in-tick container polling; then STAY until next tick
POLL_SECONDS = 6


# -- helpers -----------------------------------------------------------------
def _get_tg(cfg, tg):
    if tg is not None:
        return tg
    try:  # built in parallel by the Telegram agent; import lazily
        from ..telegram.api import from_config
    except ImportError:
        return None
    return from_config(cfg)


def _get_graph(cfg, graph):
    if graph is not None:
        return graph
    from ..meta.graph import from_config
    return from_config(cfg)


def parse_ts(s: str | None) -> datetime | None:
    """ISO timestamps from db.now() ('+00:00') and from Graph ('+0000')."""
    if not s:
        return None
    for fmt in ("%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M:%S.%f%z"):
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            pass
    return datetime.fromisoformat(s)


def approved_row(conn, job_id: int):
    return conn.execute(
        "SELECT * FROM approvals WHERE job_id=? AND status='approved' ORDER BY id DESC LIMIT 1",
        (job_id,)).fetchone()


def publication(conn, job_id: int):
    return conn.execute("SELECT * FROM publications WHERE job_id=?", (job_id,)).fetchone()


def _set_pub(conn, job_id: int, **cols) -> None:
    sets = ", ".join(f"{k}=?" for k in cols)
    conn.execute(f"UPDATE publications SET {sets}, updated_at=? WHERE job_id=?",
                 (*cols.values(), db.now(), job_id))


def verify_approved(job, approval) -> tuple[bool, str, Path, dict]:
    """Re-hash the approved files. Returns (ok, why, final_path, package)."""
    d = edit_dir(Path(job["workdir"]), approval["edit_version"])
    final, pkg_path = d / "final.mp4", d / "package.json"
    if not final.exists() or not pkg_path.exists():
        return False, "approved files are missing", final, {}
    pkg = read_json(pkg_path)
    if sha256_file(final) != approval["file_sha256"]:
        return False, "final.mp4 hash differs from the approved one", final, pkg
    full = pkg.get("full_caption", "")
    if sha256_text(full) != approval["caption_sha256"]:
        return False, "caption differs from the approved one", final, pkg
    if int(pkg.get("caption_version", approval["caption_version"])) != approval["caption_version"]:
        return False, "caption version differs from the approved one", final, pkg
    return True, "", final, pkg


def _norm(s: str | None) -> str:
    return " ".join((s or "").split())


def find_posted(client, caption: str, after: datetime | None) -> dict | None:
    """A REELS item already on the account with this caption, posted after
    `after`: the one we published before a crash."""
    for m in client.recent_media(25):
        if m.get("media_product_type") != "REELS":
            continue
        if _norm(m.get("caption")) != _norm(caption):
            continue
        ts = parse_ts(m.get("timestamp"))
        if after is not None and ts is not None and ts < after:
            continue
        return m
    return None


def _post_time_reached(cfg, now: datetime) -> bool:
    hh, mm = (int(x) for x in str(cfg.publish.get("post_time", "18:00")).split(":"))
    return (now.hour, now.minute) >= (hh, mm)


def _safe_send(conn, job_id, fn, *a, **kw):
    try:
        return fn(*a, **kw)
    except Exception as e:  # noqa: BLE001 - message delivery must not undo a publish
        db.log(conn, job_id, "error", f"telegram send failed: {e}")
        return None


# -- APPROVED ------------------------------------------------------------------
def approved_step(conn, job, cfg, tg=None, graph=None, now: datetime | None = None,
                  sleep=None):
    job_id = job["id"]
    approval = approved_row(conn, job_id)
    if approval is None:
        db.give_up(conn, job_id, State.APPROVED, "no approved approval row — re-approve")
        return scheduler.STAY
    pub = publication(conn, job_id)
    mode = cfg.publish.get("mode", "manual")

    if pub is not None and pub["media_id"]:
        # already published (possibly by an earlier tick that crashed afterwards)
        return _finish(conn, job_id, cfg, pub, tg, graph)
    if mode == "manual" and pub is not None and db.kv_get(conn, f"manual_sent:{job_id}"):
        return scheduler.STAY  # waiting for your "Posted" tap

    ok, why, final, pkg = verify_approved(job, approval)
    if not ok:
        db.give_up(conn, job_id, State.APPROVED, f"approved file changed — re-approve ({why})")
        return scheduler.STAY
    caption = pkg["full_caption"]

    if mode == "manual":
        return _manual(conn, job, cfg, approval, pub, final, caption, tg)
    return _api(conn, job, cfg, approval, pub, final, caption, pkg, tg, graph,
                now or local_now(), sleep or time.sleep)


def _manual(conn, job, cfg, approval, pub, final: Path, caption: str, tg):
    job_id = job["id"]
    if pub is None:
        conn.execute(
            "INSERT OR IGNORE INTO publications(job_id, approval_id, mode, status, updated_at)"
            " VALUES (?,?,?,?,?)", (job_id, approval["id"], "manual", "started", db.now()))
    tg = _get_tg(cfg, tg)
    if tg is None:
        raise RuntimeError("manual publish needs Telegram, but no bot token/chat_id is configured")
    # Row exists but sending didn't complete last time (crash/Telegram error):
    # send again — a duplicate message is harmless, an undelivered Reel is not.
    if final.stat().st_size < TELEGRAM_VIDEO_LIMIT:
        tg.send_video(str(final), caption="Your Reel, approved and ready to post.")
    else:
        tg.send_document(str(final), caption="Your Reel, approved and ready to post.")
    tg.send_message(caption, buttons=[[("✅ Posted", f"posted:{job_id}")]])
    db.kv_set(conn, f"manual_sent:{job_id}", db.now())
    db.log(conn, job_id, "info", "manual publish: file + caption sent, waiting for Posted")
    return scheduler.STAY


def _fail(conn, job_id: int, reason: str):
    _set_pub(conn, job_id, status="failed")
    db.kv_set(conn, f"publish_error:{job_id}", reason[:500])
    db.alert(conn, job_id, f"Publish failed: {reason}")
    return State.PUBLISH_FAILED


def _api(conn, job, cfg, approval, pub, final, caption, pkg, tg, graph, now, sleep):
    from ..meta.graph import GraphError, TransientGraphError

    job_id = job["id"]
    if pub is None and not _post_time_reached(cfg, now):
        return scheduler.STAY
    client = _get_graph(cfg, graph)
    if client is None:
        db.give_up(conn, job_id, State.APPROVED,
                   "api mode needs publish.ig_user_id and a Meta token")
        return scheduler.STAY

    recovering = pub is not None
    if pub is None:
        # the lock: the row exists before any API call
        conn.execute(
            "INSERT INTO publications(job_id, approval_id, mode, status, updated_at)"
            " VALUES (?,?,?,?,?)", (job_id, approval["id"], "api", "started", db.now()))
    elif pub["status"] == "failed":
        _set_pub(conn, job_id, status="started", approval_id=approval["id"])
    pub = publication(conn, job_id)
    approved_at = parse_ts(approval["decided_at"]) or parse_ts(approval["created_at"])

    try:
        if recovering:
            found = find_posted(client, caption, approved_at)
            if found:
                db.log(conn, job_id, "info", f"adopted already-posted media {found['id']}")
                _set_pub(conn, job_id, media_id=str(found["id"]),
                         permalink=found.get("permalink"), status="published")
                return _finish(conn, job_id, cfg, publication(conn, job_id), tg, client)

        container = pub["container_id"]
        if container and pub["status"] == "uploaded":
            st = client.container_status(container)
            code = st.get("status_code")
            if code in ("ERROR", "EXPIRED"):
                # ERROR is a verdict on the video; EXPIRED just means start a new container
                db.log(conn, job_id, "info", f"container {container} {code}: {st.get('status')}")
                if code == "ERROR":
                    return _fail(conn, job_id, f"Instagram rejected the video: {st.get('status')}")
                container = None
            elif code == "PUBLISHED":
                # published but we never saved the media id, and it's not in recent media
                raise RuntimeError(f"container {container} is PUBLISHED but its media was not found")
        elif container:
            # crashed mid-upload: the old container can't be trusted, make a new one
            container = None

        if not container:
            container, uri = client.create_reel_container(caption)
            _set_pub(conn, job_id, container_id=container, status="started")
            db.log(conn, job_id, "info", f"created container {container}")
            client.upload_file(uri, final)
            _set_pub(conn, job_id, status="uploaded")

        for i in range(POLL_ATTEMPTS):
            st = client.container_status(container)
            code = st.get("status_code")
            if code == "FINISHED":
                break
            if code in ("ERROR", "EXPIRED"):
                return _fail(conn, job_id, f"container {code}: {st.get('status')}")
            if i < POLL_ATTEMPTS - 1:
                sleep(POLL_SECONDS)
        else:
            return scheduler.STAY  # still processing; poll again next tick

        media_id = client.publish(container)
        _set_pub(conn, job_id, media_id=media_id)
        db.log(conn, job_id, "info", f"published media {media_id}")
        return _finish(conn, job_id, cfg, publication(conn, job_id), tg, client)
    except TransientGraphError:
        raise
    except GraphError as e:
        return _fail(conn, job_id, str(e))


def _finish(conn, job_id: int, cfg, pub, tg, graph):
    """media_id is saved: fill in permalink/published_at, tell you, done."""
    permalink, published_at = pub["permalink"], pub["published_at"]
    if not permalink or not published_at:
        try:
            client = _get_graph(cfg, graph)
            info = client.media(pub["media_id"], "permalink,timestamp") if client else {}
        except Exception as e:  # noqa: BLE001 - permalink is nice-to-have
            db.log(conn, job_id, "error", f"could not fetch permalink: {e}")
            info = {}
        permalink = permalink or info.get("permalink")
        ts = parse_ts(info.get("timestamp"))
        published_at = published_at or (ts.astimezone().isoformat(timespec="seconds")
                                        if ts else db.now())
    _set_pub(conn, job_id, status="published", permalink=permalink, published_at=published_at)
    if not db.kv_get(conn, f"posted_notice:{job_id}"):
        t = _get_tg(cfg, tg)
        if t is not None and _safe_send(conn, job_id, t.send_message,
                                        f"Posted ✅ {permalink or ''}".strip()) is not None:
            db.kv_set(conn, f"posted_notice:{job_id}", db.now())
    return None


# -- PUBLISH_FAILED ----------------------------------------------------------------
def publish_failed_step(conn, job, cfg, tg=None):
    job_id = job["id"]
    key = f"publish_failed_notice:{job_id}"
    marker = job["state_entered_at"] or "?"
    if db.kv_get(conn, key) == marker:
        return scheduler.STAY
    reason = db.kv_get(conn, f"publish_error:{job_id}", "unknown error")
    t = _get_tg(cfg, tg)
    if t is None:
        db.alert(conn, job_id, f"Publish failed: {reason} (no Telegram to offer Retry)")
    else:
        t.send_message(f"Publish failed: {reason}", buttons=[[("🔁 Retry", f"retry:{job_id}")]])
    db.kv_set(conn, key, marker)
    return scheduler.STAY


# -- periodic --------------------------------------------------------------------
def token_health_check(conn, cfg, graph=None):
    if cfg.publish.get("mode") != "api":
        return
    today = today_local()
    if db.kv_get(conn, "token_health_checked") == today:
        return
    db.kv_set(conn, "token_health_checked", today)
    client = _get_graph(cfg, graph)
    if client is None:
        db.alert(conn, None, "Instagram token invalid: no token or ig_user_id configured")
        return
    ok, detail = client.token_health()
    if not ok:
        db.alert(conn, None, f"Instagram token invalid: {detail}")


STEPS = {
    State.APPROVED: (approved_step, State.PUBLISHED),
    State.PUBLISH_FAILED: (publish_failed_step, State.APPROVED),
}
PERIODIC = [token_health_check]
