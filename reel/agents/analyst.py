"""Analyst: PUBLISHED -> MEASURED, plus the weekly playbook + digest.

- Stores insights at each checkpoint (cfg.analyst["checkpoints_hours"]) after
  publication; labels "24h", "72h", "168h".
- Weekly (once per ISO week, on playbook_weekday) asks Claude
  ("weekly_playbook") to update playbook.md from the features table, then
  sends a digest. If Claude is unavailable the digest still goes out.
- `baseline` stores insights for your last 20 Reels (checkpoint 'baseline',
  job_id NULL) so early weeks have something to compare against.
"""
from __future__ import annotations

import json
import shutil
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .. import db, scheduler
from ..states import State
from ..util import edit_dir, local_now, read_json, write_text
from .publisher import _get_graph, _get_tg, approved_row, parse_ts, publication

MATCH_WINDOW = timedelta(hours=2)
GIVE_UP_AFTER = timedelta(days=7)
FEATURE_METRICS = ["views", "reach", "saved", "shares", "likes", "comments",
                   "total_interactions", "ig_reels_avg_watch_time"]

PLAYBOOK_SCHEMA = {
    "type": "object",
    "required": ["playbook_markdown", "proposed_change", "confidence"],
    "properties": {
        "playbook_markdown": {"type": "string", "minLength": 1},
        "proposed_change": {"type": "string", "minLength": 1},
        "confidence": {"type": "string", "enum": ["hypothesis", "evidence"]},
    },
}


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _labels(cfg) -> list[tuple[int, str]]:
    return [(int(h), f"{int(h)}h") for h in cfg.analyst.get("checkpoints_hours", [24, 72, 168])]


# -- PUBLISHED ---------------------------------------------------------------------
def _match_manual_post(conn, job, cfg, pub, client, published_at) -> str | None:
    approval = approved_row(conn, job["id"])
    caption = (approval["caption"] if approval else "") or ""
    head = " ".join(caption.split())[:40]
    for m in client.recent_media(25):
        if m.get("media_product_type") != "REELS":
            continue
        ts = parse_ts(m.get("timestamp"))
        if ts is None or ts < published_at - MATCH_WINDOW:
            continue
        if head and not " ".join((m.get("caption") or "").split()).startswith(head):
            continue
        conn.execute("UPDATE publications SET media_id=?, permalink=COALESCE(permalink, ?),"
                     " updated_at=? WHERE job_id=?",
                     (str(m["id"]), m.get("permalink"), db.now(), job["id"]))
        db.log(conn, job["id"], "info", f"matched manual post to media {m['id']}")
        return str(m["id"])
    return None


def published_step(conn, job, cfg, graph=None, now: datetime | None = None):
    now = now or _utcnow()
    job_id = job["id"]
    pub = publication(conn, job_id)
    published_at = parse_ts(pub["published_at"] if pub else None) \
        or parse_ts(job["state_entered_at"]) or now
    client = _get_graph(cfg, graph)
    media_id = pub["media_id"] if pub else None

    if not media_id:
        if client is not None and pub is not None:
            media_id = _match_manual_post(conn, job, cfg, pub, client, published_at)
        if not media_id:
            if client is None:
                db.log(conn, job_id, "info", "no Graph client: insights not measured")
                return None
            if now - published_at > GIVE_UP_AFTER:
                db.log(conn, job_id, "info",
                       "posted Reel not found on Instagram after 7 days: insights not measured")
                return None
            return scheduler.STAY

    have = {r[0] for r in conn.execute(
        "SELECT checkpoint FROM insights WHERE media_id=?", (media_id,))}
    labels = _labels(cfg)
    for hours, label in labels:
        if label in have or now < published_at + timedelta(hours=hours):
            continue
        metrics = client.insights(media_id) if client else None
        if metrics is None:
            break
        conn.execute("INSERT OR IGNORE INTO insights(job_id, media_id, checkpoint, metrics,"
                     " fetched_at) VALUES (?,?,?,?,?)",
                     (job_id, media_id, label, json.dumps(metrics), db.now()))
        have.add(label)
    if labels and labels[-1][1] in have:
        return None
    return scheduler.STAY


# -- features -----------------------------------------------------------------------
def _safe_json(path: Path):
    try:
        return read_json(path)
    except (OSError, ValueError):
        return None


def features(conn) -> list[dict]:
    """One dict per published Reel that has insights (plain Python, no pandas)."""
    rows = conn.execute(
        "SELECT p.*, j.workdir, j.edit_version AS job_edit_version, a.edit_version AS a_ev"
        " FROM publications p JOIN jobs j ON j.id=p.job_id"
        " LEFT JOIN approvals a ON a.id=p.approval_id"
        " WHERE p.media_id IS NOT NULL ORDER BY p.job_id").fetchall()
    out = []
    for r in rows:
        ins = {c: json.loads(m) for c, m in conn.execute(
            "SELECT checkpoint, metrics FROM insights WHERE media_id=?", (r["media_id"],))}
        if not ins:
            continue
        ver = r["a_ev"] or r["job_edit_version"]
        d = edit_dir(Path(r["workdir"]), ver)
        pkg = _safe_json(d / "package.json") or {}
        edit = _safe_json(d / "edit.json") or {}
        cands = (_safe_json(Path(r["workdir"]) / "scout" / "candidates.json") or {}).get("candidates", [])
        kind = next((c.get("kind") for c in cands if c.get("id") == edit.get("candidate_id")), None)
        dur = None
        if "start" in edit and "end" in edit:
            dur = round((edit["end"] - edit["start"]) / float(edit.get("speed") or 1.0), 1)
        ts = parse_ts(r["published_at"])
        local = ts.astimezone() if ts else None
        f = {
            "job_id": r["job_id"], "media_id": r["media_id"], "permalink": r["permalink"],
            "hook": pkg.get("hook"), "caption_len": len(pkg.get("caption") or ""),
            "hashtags": len(pkg.get("hashtags") or []), "duration": dur,
            "hour": local.hour if local else None,
            "weekday": local.strftime("%a") if local else None,
            "kind": kind, "source": pkg.get("source"),
            "published_at": r["published_at"],
        }
        for cp in ("24h", "72h", "168h"):
            for m in FEATURE_METRICS:
                f[f"{m}_{cp}"] = (ins.get(cp) or {}).get(m)
        out.append(f)
    return out


def best_views(f: dict):
    for cp in ("168h", "72h", "24h"):
        if f.get(f"views_{cp}") is not None:
            return f[f"views_{cp}"]
    return None


def _measured(feats):
    return [f for f in feats if f.get("views_72h") is not None or f.get("views_168h") is not None]


# -- weekly playbook + digest ---------------------------------------------------------
def _table(feats: list[dict]) -> str:
    cols = ["job_id", "hook", "caption_len", "hashtags", "duration", "hour", "weekday", "kind",
            "source", "views_72h", "reach_72h", "saved_72h", "shares_72h",
            "ig_reels_avg_watch_time_72h", "views_168h", "saved_168h", "shares_168h"]
    lines = [" | ".join(cols)]
    for f in feats:
        lines.append(" | ".join("" if f.get(c) is None else str(f.get(c)).replace("|", "/")
                                for c in cols))
    return "\n".join(lines)


def _baseline_summary(conn) -> str:
    views = sorted(v for v in (json.loads(m).get("views") for (m,) in conn.execute(
        "SELECT metrics FROM insights WHERE checkpoint='baseline'")) if isinstance(v, (int, float)))
    if not views:
        return "none"
    return f"{len(views)} pre-pipeline Reels, median views {views[len(views) // 2]}"


def build_prompt(feats, playbook: str, baseline: str, n: int) -> str:
    return (
        "You maintain a short playbook for one person's daily Instagram Reels.\n"
        f"There are {n} measured Reels. With fewer than 10, treat every pattern as a "
        "hypothesis, not evidence.\n\n"
        f"Baseline before this pipeline: {baseline}\n\n"
        "Features and metrics (one row per Reel):\n" + _table(feats) + "\n\n"
        "Current playbook (markdown):\n" + (playbook or "(empty)") + "\n\n"
        "Return the full updated playbook_markdown (concise, actionable), exactly one "
        "proposed_change to try next week, and confidence."
    )


def _reason(f: dict) -> str:
    bits = []
    if f.get("hook"):
        bits.append(f"hook “{f['hook']}”")
    if f.get("duration") is not None:
        bits.append(f"{f['duration']}s")
    if f.get("kind"):
        bits.append(f["kind"])
    if f.get("weekday") and f.get("hour") is not None:
        bits.append(f"posted {f['weekday']} {f['hour']:02d}h")
    if f.get("saved_72h") is not None:
        bits.append(f"{f['saved_72h']} saves")
    return ", ".join(bits)


def digest(conn, feats, now: datetime, proposed: str | None, confidence: str | None) -> str:
    since = (now - timedelta(days=7)).astimezone(timezone.utc).isoformat(timespec="seconds")
    posted = conn.execute("SELECT published_at FROM publications WHERE status='published'"
                          " AND published_at IS NOT NULL").fetchall()
    days = {parse_ts(p[0]).astimezone().date() for p in posted
            if parse_ts(p[0]) and parse_ts(p[0]) >= parse_ts(since)}
    jobs = conn.execute("SELECT id, recuts FROM jobs WHERE created_at >= ?", (since,)).fetchall()
    ids = [j["id"] for j in jobs]
    first_cut = 0
    if ids:
        q = ",".join("?" * len(ids))
        first_cut = conn.execute(
            f"SELECT count(DISTINCT job_id) FROM approvals WHERE status='approved'"
            f" AND edit_version=1 AND job_id IN ({q})", ids).fetchone()[0]
    rate = f"{first_cut}/{len(ids)} ({round(100 * first_cut / len(ids))}%)" if ids else "n/a"
    lines = ["📊 Weekly digest",
             f"Days posted (last 7): {len(days)}",
             f"First-cut approval rate: {rate}",
             f"Recuts: {sum(j['recuts'] or 0 for j in jobs)}"]
    ranked = [f for f in feats if best_views(f) is not None]
    ranked.sort(key=best_views, reverse=True)
    if ranked:
        b, w = ranked[0], ranked[-1]
        lines.append(f"Best: {best_views(b)} views — {_reason(b)} {b.get('permalink') or ''}".rstrip())
        if w is not b:
            lines.append(f"Weakest: {best_views(w)} views — {_reason(w)} "
                         f"{w.get('permalink') or ''}".rstrip())
    if proposed:
        lines.append(f"Try next ({confidence}): {proposed}")
    else:
        lines.append("Playbook not updated this week (Claude unavailable).")
    return "\n".join(lines)


def weekly_playbook(conn, cfg, tg=None, runner=None, now: datetime | None = None):
    now = now or local_now()
    if now.weekday() != int(cfg.analyst.get("playbook_weekday", 0)):
        return
    iso = now.isocalendar()
    week = f"{iso[0]}-W{iso[1]:02d}"
    if db.kv_get(conn, "playbook_week") == week:
        return
    db.kv_set(conn, "playbook_week", week)  # once per week, even if Claude fails

    feats = features(conn)
    measured = _measured(feats)
    if len(measured) < 3:
        db.log(conn, None, "info", f"weekly playbook skipped: {len(measured)} measured Reels (< 3)")
        return

    from ..llm import claude
    path = Path(cfg.playbook_path)
    current = path.read_text() if path.exists() else ""
    prompt = build_prompt(measured, current, _baseline_summary(conn), len(measured))
    proposed = confidence = None
    try:
        res = claude.call(conn, cfg, "weekly_playbook", prompt, PLAYBOOK_SCHEMA, runner=runner)
        confidence = "hypothesis" if len(measured) < 10 else res["confidence"]
        proposed = res["proposed_change"]
        if path.exists():
            shutil.copyfile(path, path.with_name("playbook.prev.md.partial"))
            Path(path.with_name("playbook.prev.md.partial")).replace(path.with_name("playbook.prev.md"))
        header = (f"<!-- updated {week}; {len(measured)} measured Reels; "
                  f"confidence: {confidence} -->\n")
        write_text(path, header + res["playbook_markdown"].rstrip() + "\n")
    except claude.ClaudeUnavailable as e:
        db.alert(conn, None, f"weekly playbook not updated: Claude unavailable ({e})")

    text = digest(conn, measured, now, proposed, confidence)
    t = _get_tg(cfg, tg)
    if t is None:
        db.alert(conn, None, text)
    else:
        t.send_message(text)


# -- baseline -------------------------------------------------------------------------
def baseline(conn, cfg, client, count: int = 20) -> int:
    """Insights for your last `count` Reels, stored with checkpoint 'baseline'."""
    reels = [m for m in client.recent_media(50) if m.get("media_product_type") == "REELS"][:count]
    stored = 0
    for m in reels:
        metrics = client.insights(str(m["id"]))
        metrics["_timestamp"] = m.get("timestamp")
        cur = conn.execute(
            "INSERT OR IGNORE INTO insights(job_id, media_id, checkpoint, metrics, fetched_at)"
            " VALUES (NULL,?,?,?,?)", (str(m["id"]), "baseline", json.dumps(metrics), db.now()))
        stored += cur.rowcount
    db.log(conn, None, "info", f"baseline: stored insights for {stored} of {len(reels)} Reels")
    return stored


def cli_baseline(cfg) -> int:
    from ..meta.graph import from_config
    client = from_config(cfg)
    if client is None:
        print("No Meta token or publish.ig_user_id configured.")
        return 1
    conn = db.connect(cfg.db)
    try:
        n = baseline(conn, cfg, client)
    finally:
        conn.close()
    print(f"Stored baseline insights for {n} Reels.")
    return 0


STEPS = {State.PUBLISHED: (published_step, State.MEASURED)}
PERIODIC = [weekly_playbook]
