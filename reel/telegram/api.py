"""Minimal Telegram Bot API client (plain `requests`, no framework).

`buttons` everywhere is a list of rows, each row a list of
`(label, callback_data)`; callback_data must be <= 64 bytes.
"""
from __future__ import annotations

import json
import time
from pathlib import Path

import requests

API_BASE = "https://api.telegram.org"


class TelegramError(Exception):
    """A Telegram call failed (after the one allowed retry)."""

    def __init__(self, message: str, code: int | None = None):
        super().__init__(message)
        self.code = code


def keyboard(buttons) -> dict:
    rows = []
    for row in buttons or []:
        out = []
        for label, data in row:
            if len(data.encode()) > 64:
                raise ValueError(f"callback_data longer than 64 bytes: {data!r}")
            out.append({"text": label, "callback_data": data})
        rows.append(out)
    return {"inline_keyboard": rows}


class TelegramAPI:
    def __init__(self, token: str, chat_id: int, session=None, sleep=time.sleep):
        self.token = token
        self.chat_id = int(chat_id or 0)
        self.session = session or requests.Session()
        self.sleep = sleep

    # -- transport -----------------------------------------------------
    def _call(self, method: str, data: dict | None = None, file_field: str | None = None,
              file_path: Path | None = None, timeout: float = 60):
        url = f"{API_BASE}/bot{self.token}/{method}"
        data = {k: v for k, v in (data or {}).items() if v is not None}
        for attempt in (1, 2):
            fh = None
            try:
                files = None
                if file_path is not None:
                    fh = open(file_path, "rb")
                    files = {file_field: (Path(file_path).name, fh)}
                resp = self.session.post(url, data=data, files=files, timeout=timeout)
            except (requests.ConnectionError, requests.Timeout) as e:
                if attempt == 1:
                    self.sleep(1)
                    continue
                raise TelegramError(f"{method}: connection failed: {e}") from e
            finally:
                if fh:
                    fh.close()
            try:
                body = resp.json()
            except ValueError:
                body = {"ok": False, "error_code": getattr(resp, "status_code", None),
                        "description": "non-JSON response"}
            if body.get("ok"):
                return body.get("result")
            code = body.get("error_code") or getattr(resp, "status_code", None)
            if code == 429 and attempt == 1:
                wait = (body.get("parameters") or {}).get("retry_after", 1)
                self.sleep(min(float(wait), 120))
                continue
            raise TelegramError(f"{method}: {code} {body.get('description', '')}".strip(), code)
        raise TelegramError(f"{method}: failed")  # pragma: no cover

    def _markup(self, buttons):
        return json.dumps(keyboard(buttons)) if buttons else None

    # -- public --------------------------------------------------------
    def send_message(self, text, buttons=None, chat_id=None) -> int:
        res = self._call("sendMessage", {
            "chat_id": chat_id or self.chat_id, "text": text[:4096],
            "reply_markup": self._markup(buttons), "disable_web_page_preview": "true"})
        return res["message_id"]

    def send_video(self, path, caption="", buttons=None) -> int:
        res = self._call("sendVideo", {
            "chat_id": self.chat_id, "caption": caption[:1024], "supports_streaming": "true",
            "reply_markup": self._markup(buttons)}, "video", Path(path), timeout=600)
        return res["message_id"]

    def send_document(self, path, caption="", buttons=None) -> int:
        res = self._call("sendDocument", {
            "chat_id": self.chat_id, "caption": caption[:1024],
            "reply_markup": self._markup(buttons)}, "document", Path(path), timeout=600)
        return res["message_id"]

    def edit_buttons(self, message_id, buttons=None) -> None:
        try:
            self._call("editMessageReplyMarkup", {
                "chat_id": self.chat_id, "message_id": message_id,
                "reply_markup": json.dumps(keyboard(buttons))})
        except TelegramError as e:
            if "not modified" in str(e):  # double tap: already in that state
                return
            raise

    def get_updates(self, offset, timeout=50) -> list[dict]:
        return self._call("getUpdates", {
            "offset": offset, "timeout": timeout,
            "allowed_updates": json.dumps(["message", "callback_query"])},
            timeout=timeout + 15) or []

    def answer_callback(self, callback_id, text="") -> None:
        self._call("answerCallbackQuery", {"callback_query_id": callback_id, "text": text[:200]})


def token_from_config(cfg) -> str | None:
    from reel.config import read_secret
    t = cfg.telegram
    return read_secret(t.get("token_env", "REEL_TELEGRAM_TOKEN"),
                       t.get("token_file", "~/.config/reel/telegram_token"))


def from_config(cfg, session=None) -> TelegramAPI | None:
    """None if the bot token or chat_id is not configured."""
    token = token_from_config(cfg)
    try:
        chat_id = int(cfg.telegram.get("chat_id") or 0)
    except (TypeError, ValueError):
        chat_id = 0
    if not token or not chat_id:
        return None
    return TelegramAPI(token, chat_id, session=session)
