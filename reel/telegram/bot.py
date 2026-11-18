"""`reel bot`: long-polls Telegram and turns your taps into state changes.

Every handler is idempotent and changes job state only through
db.transition (compare-and-set), so double taps and replays are harmless.
Only the configured chat is obeyed (except /whoami).
"""
from __future__ import annotations

import argparse
import json
import re
import time
import traceback
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

import yaml

from reel import db
from reel.agents import approval as ap_mod
from reel.states import State
from reel.telegram.api import TelegramAPI, TelegramError, from_config, token_from_config
from reel.util import sha256_file, sha256_text, write_json, write_text

PENDING_KEY = "tg_pending"
OFFSET_KEY = "tg_offset"

RECUT_KINDS = {
    "dc": ("different_clip", "Different clip"),
    "ti": ("tighter", "Tighter <20s"),
    "fc": ("fix_captions", "Fix captions"),
    "bc": ("bad_crop", "Bad crop"),
}


class _Abort(Exception):
    pass


@contextmanager
def tx(conn):
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    conn.execute("COMMIT")


# ------------------------------------------------------------ brand.yaml

def edit_brand_list(cfg, key: str, op: str, word: str | None = None) -> list[str]:
    """add / remove / list a word in brand.yaml[key], atomically, keeping
    the other keys. Returns the resulting list."""
    path = Path(cfg.brand_path) if cfg.brand_path else None
    if path is None:
        raise RuntimeError("brand.yaml location unknown")
    data = (yaml.safe_load(path.read_text()) if path.exists() else None) or {}
    items = [str(x) for x in (data.get(key) or [])]
    if op == "list":
        return items
    word = (word or "").strip().strip('"\'')
    if not word:
        raise ValueError("no word given")
    low = [x.lower() for x in items]
    if op == "add" and word.lower() not in low:
        items.append(word)
    elif op == "remove" and word.lower() in low:
        items = [x for x in items if x.lower() != word.lower()]
    else:
        return items  # nothing to change
    data[key] = items
    write_text(path, yaml.safe_dump(data, sort_keys=False, allow_unicode=True))
    cfg.brand[key] = list(items)
    return items


def parse_word_fixes(text: str) -> dict:
    fixes = {}
    for part in re.split(r"[,;\n]+", text):
        m = re.match(r"\s*(.+?)\s*(?:->|→|=>|=)\s*(.+?)\s*$", part)
        if m and m.group(1) and m.group(2):
            fixes[m.group(1)] = m.group(2)
    return fixes


def first_sentence(text: str) -> str:
    text = (text or "").strip()
    m = re.match(r"(.+?[.!?])(\s|$)", text, re.S)
    return m.group(1).strip() if m else text


# ------------------------------------------------------------ bot

class Bot:
    def __init__(self, cfg, api, conn, now=None):
        self.cfg, self.api, self.conn = cfg, api, conn
        self._now = now
        try:
            self.chat_id = int(cfg.telegram.get("chat_id") or 0)
        except (TypeError, ValueError):
            self.chat_id = 0

    def now(self) -> datetime:
        return self._now() if self._now else datetime.now(timezone.utc)

    # -- plumbing -------------------------------------------------------
    def say(self, text, buttons=None):
        return self.api.send_message(text, buttons=buttons)

    def answer(self, cq, text=""):
        try:
            self.api.answer_callback(cq["id"], text)
        except TelegramError:
            pass  # callback too old; harmless

    def strip(self, message_id):
        if not message_id:
            return
        try:
            self.api.edit_buttons(message_id, None)
        except TelegramError:
            pass

    def pending(self) -> dict | None:
        raw = db.kv_get(self.conn, PENDING_KEY)
        return json.loads(raw) if raw else None

    def set_pending(self, obj: dict | None):
        db.kv_set(self.conn, PENDING_KEY, json.dumps(obj) if obj else "")

    # -- dispatch -------------------------------------------------------
    def handle(self, update: dict) -> None:
        if "callback_query" in update:
            cq = update["callback_query"]
            chat = ((cq.get("message") or {}).get("chat") or {}).get("id") or (cq.get("from") or {}).get("id")
            if not self.chat_id or chat != self.chat_id:
                return
            self.on_callback(cq)
            return
        msg = update.get("message") or {}
        text = (msg.get("text") or "").strip()
        chat = (msg.get("chat") or {}).get("id")
        if text.split(" ")[0].split("@")[0] == "/whoami":
            self.api.send_message(f"This chat's id is {chat}. Put it in config.yaml as telegram.chat_id.",
                                  chat_id=chat)
            return
        if not self.chat_id or chat != self.chat_id or not text:
            return
        if text.startswith("/"):
            self.on_command(text)
        else:
            self.on_text(text)

    def on_callback(self, cq):
        data = cq.get("data") or ""
        msg_id = (cq.get("message") or {}).get("message_id")
        parts = data.split(":")
        action = parts[0]
        try:
            job_id = int(parts[1])
        except (IndexError, ValueError):
            self.answer(cq, "?")
            return
        args = parts[2:]
        aid = int(args[0]) if args and args[0].isdigit() else None
        handler = {
            "ap": self.cb_approve, "ed": self.cb_edit_menu, "bk": self.cb_back,
            "ew": self.cb_caption_write, "es": self.cb_caption_shorter,
            "eh": self.cb_caption_hook, "ef": self.cb_caption_fewer_tags,
            "rc": self.cb_recut_menu, "rr": self.cb_recut, "rj": self.cb_reject,
            "rv": self.cb_revive, "posted": self.cb_posted, "retry": self.cb_retry_publish,
            "rt": self.cb_retry_stuck, "va": self.cb_vocab_add,
        }.get(action)
        if handler is None:
            self.answer(cq, "Unknown button")
            return
        handler(cq, msg_id, job_id, aid, args)

    # -- approval checks ------------------------------------------------
    def live(self, cq, msg_id, job_id, aid):
        """(job, approval) if this preview is still the one being decided,
        else answers 'outdated' and returns None."""
        job = db.get_job(self.conn, job_id)
        ap = self.conn.execute("SELECT * FROM approvals WHERE id=? AND job_id=?", (aid, job_id)).fetchone()
        latest = ap_mod.latest_approval(self.conn, job_id)
        if (not job or not ap or ap["status"] != "pending" or job["status"] != State.AWAITING_APPROVAL
                or latest["id"] != ap["id"]):
            status = ap["status"] if ap else "unknown"
            self.answer(cq, "Already approved ✅" if status == "approved" else "This preview is outdated")
            if status != "pending":
                self.strip(msg_id)
            return None
        return job, ap

    def still_matches(self, job, ap) -> bool:
        if job["edit_version"] != ap["edit_version"]:
            return False
        vdir = ap_mod.version_dir(job)
        try:
            pkg = ap_mod.read_package(job)
            file_sha = sha256_file(vdir / "final.mp4")
        except (FileNotFoundError, ValueError):
            return False
        cap = pkg.get("full_caption") or ap_mod.full_caption(pkg.get("caption"), pkg.get("hashtags"))
        return (file_sha == ap["file_sha256"] and sha256_text(cap) == ap["caption_sha256"]
                and int(pkg.get("caption_version", 1)) == ap["caption_version"])

    # -- approve / reject -----------------------------------------------
    def cb_approve(self, cq, msg_id, job_id, aid, args):
        got = self.live(cq, msg_id, job_id, aid)
        if not got:
            return
        job, ap = got
        if not self.still_matches(job, ap):
            try:
                with tx(self.conn):
                    ap_mod.supersede(self.conn, job_id)
                    if not db.transition(self.conn, job_id, State.AWAITING_APPROVAL, State.QA_PASSED,
                                         "approval refused: file or caption changed"):
                        raise _Abort
            except _Abort:
                self.answer(cq, "This preview is outdated")
                return
            self.answer(cq, "Version changed")
            self.strip(msg_id)
            self.say("This version changed — sending the new one")
            return
        try:
            with tx(self.conn):
                cur = self.conn.execute("UPDATE approvals SET status='approved', decided_at=? "
                                        "WHERE id=? AND status='pending'", (db.now(), aid))
                if cur.rowcount != 1 or not db.transition(
                        self.conn, job_id, State.AWAITING_APPROVAL, State.APPROVED, f"approval {aid}"):
                    raise _Abort
        except _Abort:
            self.answer(cq, "Already handled")
            return
        self.answer(cq, "Approved ✅")
        self.strip(msg_id)
        mode = self.cfg.publish.get("mode", "manual")
        self.say(f"✅ Approved job {job_id}." + (" The post kit is on its way." if mode == "manual"
                                                 else f" Posting at/after {self.cfg.publish.get('post_time')}."))

    def cb_reject(self, cq, msg_id, job_id, aid, args):
        if not self.live(cq, msg_id, job_id, aid):
            return
        try:
            with tx(self.conn):
                cur = self.conn.execute("UPDATE approvals SET status='rejected', decided_at=? "
                                        "WHERE id=? AND status='pending'", (db.now(), aid))
                if cur.rowcount != 1 or not db.transition(
                        self.conn, job_id, State.AWAITING_APPROVAL, State.REJECTED, f"approval {aid}"):
                    raise _Abort
        except _Abort:
            self.answer(cq, "Already handled")
            return
        self.answer(cq, "Rejected")
        self.strip(msg_id)
        self.say(f"❌ Rejected job {job_id}. Nothing will be posted.")

    # -- caption edits --------------------------------------------------
    def cb_edit_menu(self, cq, msg_id, job_id, aid, args):
        if not self.live(cq, msg_id, job_id, aid):
            return
        self.answer(cq)
        self.api.edit_buttons(msg_id, [
            [("✍️ Write new", f"ew:{job_id}:{aid}"), ("✂️ Shorter", f"es:{job_id}:{aid}")],
            [("🪝 New hook", f"eh:{job_id}:{aid}"), ("#️⃣ Fewer hashtags", f"ef:{job_id}:{aid}")],
            [("↩ Back", f"bk:{job_id}:{aid}")]])

    def cb_back(self, cq, msg_id, job_id, aid, args):
        got = self.live(cq, msg_id, job_id, aid)
        if not got:
            return
        self.answer(cq)
        pkg = ap_mod.read_package(got[0])
        self.api.edit_buttons(msg_id, ap_mod.approval_buttons(job_id, aid, pkg))

    def change_caption(self, job, mutate, note: str) -> int:
        """Apply mutate(pkg) to package.json (caption_version+1, atomic),
        then supersede + send a fresh preview. Returns the new approval id."""
        path = ap_mod.version_dir(job) / "package.json"
        pkg = ap_mod.read_package(job)
        mutate(pkg)
        pkg["caption_version"] = int(pkg.get("caption_version", 1)) + 1
        pkg["full_caption"] = ap_mod.full_caption(pkg.get("caption"), pkg.get("hashtags"))
        write_json(path, pkg)
        return ap_mod.send_preview(self.conn, job, self.cfg, self.api, note=note)

    def cb_caption_write(self, cq, msg_id, job_id, aid, args):
        if not self.live(cq, msg_id, job_id, aid):
            return
        self.answer(cq)
        self.set_pending({"kind": "caption", "job": job_id, "aid": aid})
        self.say("✍️ Send the new caption as your next message (hashtags at the end are kept as hashtags).")

    def cb_caption_shorter(self, cq, msg_id, job_id, aid, args):
        got = self.live(cq, msg_id, job_id, aid)
        if not got:
            return
        pkg = ap_mod.read_package(got[0])
        short = first_sentence(pkg.get("caption", ""))
        if short == (pkg.get("caption") or "").strip():
            self.answer(cq, "Already one sentence")
            return
        self.answer(cq, "Shortened")
        self.change_caption(got[0], lambda p: p.update(caption=short), "✂️ Shorter caption")

    def cb_caption_hook(self, cq, msg_id, job_id, aid, args):
        """Rotate pick.json alt_hooks into the caption's first line (the
        burned-in on-screen hook is not changed)."""
        got = self.live(cq, msg_id, job_id, aid)
        if not got:
            return
        job = got[0]
        pick = ap_mod.read_pick(job)
        hooks = [h for h in (pick.get("alt_hooks") or []) if h]
        if not hooks:
            self.answer(cq, "No alternative hooks")
            return
        pkg = ap_mod.read_package(job)
        cur = pkg.get("caption_hook")
        nxt = hooks[(hooks.index(cur) + 1) % len(hooks)] if cur in hooks else hooks[0]

        def mutate(p):
            cap = (p.get("caption") or "").strip()
            lines = cap.split("\n")
            if cur and lines and lines[0].strip() == cur:
                lines = lines[1:]
            rest = "\n".join(lines).strip()
            p["caption"] = f"{nxt}\n{rest}".strip()
            p["caption_hook"] = nxt

        self.answer(cq, "New hook")
        self.change_caption(job, mutate, "🪝 New hook in the caption (on-screen hook unchanged)")

    def cb_caption_fewer_tags(self, cq, msg_id, job_id, aid, args):
        got = self.live(cq, msg_id, job_id, aid)
        if not got:
            return
        pkg = ap_mod.read_package(got[0])
        if len(pkg.get("hashtags") or []) <= 3:
            self.answer(cq, "Already 3 or fewer")
            return
        self.answer(cq, "Trimmed hashtags")
        self.change_caption(got[0], lambda p: p.update(hashtags=list(p["hashtags"])[:3]), "#️⃣ Fewer hashtags")

    # -- recut ----------------------------------------------------------
    def recut_allowed(self, cq, job) -> bool:
        limit = int(self.cfg.reel.get("max_recuts_per_day", 3))
        if job["recuts"] >= limit:
            self.answer(cq, "Recut limit reached")
            self.say(f"🔁 Recut limit reached ({limit} for job {job['id']}). Approve, edit the caption or reject.")
            return False
        return True

    def cb_recut_menu(self, cq, msg_id, job_id, aid, args):
        got = self.live(cq, msg_id, job_id, aid)
        if not got or not self.recut_allowed(cq, got[0]):
            return
        self.answer(cq)
        b = lambda k: (RECUT_KINDS[k][1], f"rr:{job_id}:{aid}:{k}")  # noqa: E731
        self.api.edit_buttons(msg_id, [[b("dc"), b("ti")], [b("fc"), b("bc")],
                                       [("↩ Back", f"bk:{job_id}:{aid}")]])

    def cb_recut(self, cq, msg_id, job_id, aid, args):
        got = self.live(cq, msg_id, job_id, aid)
        if not got or not self.recut_allowed(cq, got[0]):
            return
        kind = args[1] if len(args) > 1 else ""
        if kind not in RECUT_KINDS:
            self.answer(cq, "?")
            return
        self.answer(cq)
        if kind == "fc":
            self.set_pending({"kind": "word_fixes", "job": job_id, "aid": aid})
            self.say("✍️ Reply with corrections like: kaisen -> Kaizen, gonna -> going to")
            return
        self.do_recut(job_id, aid, {"reason": RECUT_KINDS[kind][0]}, msg_id)

    def do_recut(self, job_id, aid, reason: dict, msg_id=None) -> bool:
        try:
            with tx(self.conn):
                job = db.get_job(self.conn, job_id)
                ap = self.conn.execute("SELECT * FROM approvals WHERE id=?", (aid,)).fetchone()
                if job["status"] != State.AWAITING_APPROVAL or not ap or ap["status"] != "pending":
                    raise _Abort
                db.set_job(self.conn, job_id, recuts=job["recuts"] + 1, recut_reason=json.dumps(reason))
                ap_mod.supersede(self.conn, job_id)
                if not db.transition(self.conn, job_id, State.AWAITING_APPROVAL, State.RECUT_REQUESTED,
                                     reason["reason"]):
                    raise _Abort
        except _Abort:
            self.say("That preview is outdated — nothing changed.")
            return False
        self.strip(msg_id or (ap["tg_message_id"] if ap else None))
        self.say("Recutting… a new preview follows when it's rendered.")
        return True

    # -- other states ---------------------------------------------------
    def cb_revive(self, cq, msg_id, job_id, aid, args):
        if db.transition(self.conn, job_id, State.EXPIRED_APPROVAL, State.QA_PASSED, "revived"):
            self.answer(cq, "Revived")
            self.strip(msg_id)
            self.say(f"♻️ Job {job_id} revived — the preview comes back on the next tick.")
        else:
            self.answer(cq, "Nothing to revive")

    def cb_posted(self, cq, msg_id, job_id, aid, args):
        ap = self.conn.execute("SELECT id FROM approvals WHERE job_id=? AND status='approved' "
                               "ORDER BY id DESC LIMIT 1", (job_id,)).fetchone()
        if not ap:
            self.answer(cq, "Not approved")
            return
        ts = db.now()
        try:
            with tx(self.conn):
                if not db.transition(self.conn, job_id, State.APPROVED, State.PUBLISHED, "posted manually"):
                    raise _Abort
                self.conn.execute(
                    "INSERT INTO publications(job_id, approval_id, mode, status, published_at, updated_at)"
                    " VALUES (?,?, 'manual', 'published', ?, ?) ON CONFLICT(job_id) DO UPDATE SET"
                    " status='published', approval_id=excluded.approval_id,"
                    " published_at=COALESCE(publications.published_at, excluded.published_at),"
                    " updated_at=excluded.updated_at", (job_id, ap["id"], ts, ts))
        except _Abort:
            self.answer(cq, "Already marked")
            return
        self.answer(cq, "Posted 🎉")
        self.strip(msg_id)
        self.set_pending({"kind": "permalink", "job": job_id})
        self.say(f"🎉 Job {job_id} marked as posted. Paste the instagram.com link so stats can be tracked.")

    def cb_retry_publish(self, cq, msg_id, job_id, aid, args):
        if db.transition(self.conn, job_id, State.PUBLISH_FAILED, State.APPROVED, "retry from Telegram"):
            self.answer(cq, "Retrying")
            self.strip(msg_id)
            self.say(f"🔁 Job {job_id}: publishing again on the next tick.")
        else:
            self.answer(cq, "Nothing to retry")

    def cb_retry_stuck(self, cq, msg_id, job_id, aid, args):
        if db.retry(self.conn, job_id):
            self.answer(cq, "Queued")
            self.strip(msg_id)
            self.say(f"🔁 Job {job_id} queued for retry.")
        else:
            self.answer(cq, "Not stuck")

    def cb_vocab_add(self, cq, msg_id, job_id, aid, args):
        word = ":".join(args)
        items = edit_brand_list(self.cfg, "vocab", "add", word)
        self.answer(cq, f"Added {word}")
        self.say(f"📚 vocab: {', '.join(items)}")

    # -- text -----------------------------------------------------------
    def on_text(self, text: str):
        p = self.pending()
        if not p:
            self.say("Use the buttons, or /status · /vocab · /ban · /pause · /retry")
            return
        kind = p.get("kind")
        if kind == "permalink":
            m = re.search(r"https?://(?:www\.)?instagram\.com/\S+", text)
            self.set_pending(None)
            if not m:
                self.say("That's not an instagram.com link — skipped.")
                return
            self.conn.execute("UPDATE publications SET permalink=?, updated_at=? WHERE job_id=?",
                              (m.group(), db.now(), p["job"]))
            self.say(f"🔗 Saved the link for job {p['job']}.")
            return
        job = db.get_job(self.conn, p["job"])
        ap = self.conn.execute("SELECT * FROM approvals WHERE id=?", (p["aid"],)).fetchone()
        latest = ap_mod.latest_approval(self.conn, p["job"])
        if (not job or not ap or ap["status"] != "pending" or job["status"] != State.AWAITING_APPROVAL
                or latest["id"] != ap["id"]):
            self.set_pending(None)
            self.say("That preview changed in the meantime — tap the buttons on the newest one.")
            return
        if kind == "caption":
            tags = re.findall(r"#\w+", text)
            body = re.sub(r"(\s*#\w+)+\s*$", "", text).strip() if tags else text.strip()

            def mutate(pkg):
                pkg["caption"] = body
                if tags:
                    pkg["hashtags"] = tags
                pkg["caption_by_human"] = True
                pkg.pop("caption_hook", None)

            self.set_pending(None)
            self.change_caption(job, mutate, "✍️ Your caption")
        elif kind == "word_fixes":
            fixes = parse_word_fixes(text)
            if not fixes:
                self.say("Couldn't read that. Format: kaisen -> Kaizen, gonna -> going to")
                return
            self.set_pending(None)
            if not self.do_recut(job["id"], ap["id"], {"reason": "fix_captions", "word_fixes": fixes}):
                return
            vocab = {str(v).lower() for v in (self.cfg.brand.get("vocab") or [])}
            buttons = [[(f"➕ {w}", f"va:{job['id']}:{w}")] for w in dict.fromkeys(fixes.values())
                       if w.lower() not in vocab and len(f"va:{job['id']}:{w}".encode()) <= 64]
            if buttons:
                self.say("Add to vocab so the transcriber gets it right next time?", buttons=buttons)
        else:
            self.set_pending(None)

    # -- commands -------------------------------------------------------
    def on_command(self, text: str):
        cmd, _, rest = text.partition(" ")
        cmd = cmd.split("@")[0].lower()
        rest = rest.strip()
        if cmd == "/status":
            rows = self.conn.execute("SELECT * FROM jobs ORDER BY id DESC LIMIT 5").fetchall()
            lines = [f"#{r['id']} {r['key']} — {r['status']}" for r in rows] or ["no jobs yet"]
            if db.is_paused(self.conn):
                lines.append(f"⏸ paused until {db.kv_get(self.conn, 'paused_until')}")
            self.say("\n".join(lines))
        elif cmd in ("/vocab", "/ban"):
            key = "vocab" if cmd == "/vocab" else "banned_words"
            op, _, word = rest.partition(" ")
            op = op.lower() or "list"
            if op not in ("add", "remove", "list") or (op != "list" and not word.strip()):
                self.say(f"Usage: {cmd} add|remove|list WORD")
                return
            items = edit_brand_list(self.cfg, key, op, word)
            self.say(f"{key}: {', '.join(items) if items else '(empty)'}")
        elif cmd == "/pause":
            if rest.lower() in ("off", "resume", "0"):
                db.kv_set(self.conn, "paused_until", "")
                self.say("▶️ Resumed.")
                return
            m = re.fullmatch(r"(\d+)\s*([dh])", rest.lower())
            if not m:
                self.say("Usage: /pause 3d | 12h | off")
                return
            n, unit = int(m.group(1)), m.group(2)
            until = self.now() + (timedelta(days=n) if unit == "d" else timedelta(hours=n))
            db.kv_set(self.conn, "paused_until",
                      until.astimezone(timezone.utc).isoformat(timespec="seconds"))
            self.say(f"⏸ Paused until {until.astimezone().strftime('%a %d %b %H:%M')} "
                     "(no new jobs, no deadline alerts, no heartbeat).")
        elif cmd == "/retry":
            if not rest.isdigit():
                self.say("Usage: /retry JOB_ID")
            elif db.retry(self.conn, int(rest)):
                self.say(f"🔁 Job {rest} queued for retry.")
            else:
                self.say(f"Job {rest} is not in NEEDS_HUMAN.")
        elif cmd in ("/help", "/start"):
            self.say("/status · /vocab add|remove|list WORD · /ban add|remove|list WORD · "
                     "/pause 3d|12h|off · /retry JOB · /whoami")
        else:
            self.say("Unknown command. /help")


# ------------------------------------------------------------ loop

def run_bot(cfg, api=None, conn=None, once=False, now=None) -> None:
    api = api or from_config(cfg)
    if api is None:
        raise SystemExit("Telegram not configured: set the bot token and telegram.chat_id "
                         "(`reel bot --whoami` shows your chat id).")
    own = conn is None
    conn = conn or db.connect(cfg.db)
    bot = Bot(cfg, api, conn, now=now)
    failing = 0
    try:
        while True:
            offset = int(db.kv_get(conn, OFFSET_KEY, "0") or 0)
            try:
                updates = api.get_updates(offset, timeout=0 if once else 50)
                failing = 0
            except Exception as e:  # noqa: BLE001 - network down: back off, keep going
                if failing == 0:
                    db.log(conn, None, "error", f"bot getUpdates failed: {e}")
                failing += 1
                if once:
                    return
                time.sleep(min(60, 5 * failing))
                continue
            for u in updates:
                try:
                    bot.handle(u)
                except Exception as e:  # noqa: BLE001 - one bad update never kills the loop
                    db.log(conn, None, "error",
                           f"bot update {u.get('update_id')} failed: {type(e).__name__}: {e}\n"
                           + traceback.format_exc(limit=5))
                db.kv_set(conn, OFFSET_KEY, str(int(u["update_id"]) + 1))
            if once:
                return
    finally:
        if own:
            conn.close()


def whoami(cfg, api=None) -> int:
    if api is None:
        token = token_from_config(cfg)
        if not token:
            print(f"No bot token: set ${cfg.telegram.get('token_env')} or write it to "
                  f"{cfg.telegram.get('token_file')}")
            return 1
        api = TelegramAPI(token, 0)
    seen = {}
    for u in api.get_updates(0, timeout=0):
        m = u.get("message") or (u.get("callback_query") or {}).get("message") or {}
        c = m.get("chat") or {}
        if c.get("id") is not None:
            seen[c["id"]] = c.get("title") or c.get("username") or c.get("first_name") or ""
    if not seen:
        print("No messages yet. Send any message to your bot in Telegram, then run this again.")
        return 1
    for cid, name in seen.items():
        print(f"chat_id {cid}  {name}")
    print("Put yours in config.yaml under telegram: chat_id: ...")
    return 0


def cli_main(args=None, cfg=None) -> int:
    """`reel bot` (run forever) / `reel bot --whoami`. Accepts an argparse
    Namespace (with .config and .whoami) or an argv list."""
    if args is None or isinstance(args, (list, tuple)):
        p = argparse.ArgumentParser(prog="reel bot")
        p.add_argument("--config", type=Path, default=None)
        p.add_argument("--whoami", action="store_true")
        p.add_argument("--once", action="store_true")
        args = p.parse_args(args or [])
    if cfg is None:
        from reel.config import Config
        path = getattr(args, "config", None)
        if path is None:
            from reel.cli import DEFAULT_CONFIG_PATH
            path = DEFAULT_CONFIG_PATH
        cfg = Config.load(Path(path).expanduser())
    if getattr(args, "whoami", False):
        return whoami(cfg)
    run_bot(cfg, once=getattr(args, "once", False))
    return 0
