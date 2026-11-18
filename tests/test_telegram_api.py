import json

import pytest
import requests

from reel.telegram import api as tg
from reel.telegram.api import TelegramAPI, TelegramError


class Resp:
    def __init__(self, body, status=200):
        self.body, self.status_code = body, status

    def json(self):
        return self.body


class FakeSession:
    def __init__(self, responses):
        self.responses = list(responses)
        self.posts = []

    def post(self, url, data=None, files=None, timeout=None):
        self.posts.append({"url": url, "data": data, "files": {k: v[0] for k, v in (files or {}).items()},
                           "timeout": timeout})
        r = self.responses.pop(0)
        if isinstance(r, Exception):
            raise r
        return r


def ok(result):
    return Resp({"ok": True, "result": result})


def make(responses):
    s = FakeSession(responses)
    slept = []
    return TelegramAPI("TOKEN", 42, session=s, sleep=slept.append), s, slept


def test_send_message_with_buttons():
    api, s, _ = make([ok({"message_id": 7})])
    assert api.send_message("hi", buttons=[[("Yes", "ap:1:2"), ("No", "rj:1:2")]]) == 7
    p = s.posts[0]
    assert p["url"] == "https://api.telegram.org/botTOKEN/sendMessage"
    assert p["data"]["chat_id"] == 42 and p["data"]["text"] == "hi"
    kb = json.loads(p["data"]["reply_markup"])
    assert kb == {"inline_keyboard": [[{"text": "Yes", "callback_data": "ap:1:2"},
                                       {"text": "No", "callback_data": "rj:1:2"}]]}


def test_send_video_multipart(tmp_path):
    f = tmp_path / "preview.mp4"
    f.write_bytes(b"x")
    api, s, _ = make([ok({"message_id": 9})])
    assert api.send_video(f, caption="c") == 9
    p = s.posts[0]
    assert p["url"].endswith("/sendVideo") and p["files"] == {"video": "preview.mp4"}
    assert p["data"]["supports_streaming"] == "true" and "reply_markup" not in p["data"]


def test_send_document(tmp_path):
    f = tmp_path / "final.mp4"
    f.write_bytes(b"x")
    api, s, _ = make([ok({"message_id": 3})])
    assert api.send_document(f) == 3 and s.posts[0]["files"] == {"document": "final.mp4"}


def test_retry_on_429_honours_retry_after():
    api, s, slept = make([Resp({"ok": False, "error_code": 429, "parameters": {"retry_after": 3}}, 429),
                          ok({"message_id": 1})])
    assert api.send_message("x") == 1
    assert slept == [3.0] and len(s.posts) == 2


def test_retry_once_on_connection_error_then_raise():
    api, s, _ = make([requests.ConnectionError("down"), ok({"message_id": 1})])
    assert api.send_message("x") == 1
    api, s, _ = make([requests.ConnectionError("down"), requests.ConnectionError("down")])
    with pytest.raises(TelegramError):
        api.send_message("x")


def test_other_errors_raise_without_retry():
    api, s, _ = make([Resp({"ok": False, "error_code": 400, "description": "Bad Request"}, 400)])
    with pytest.raises(TelegramError) as e:
        api.send_message("x")
    assert e.value.code == 400 and len(s.posts) == 1


def test_edit_buttons_not_modified_is_harmless():
    api, s, _ = make([Resp({"ok": False, "error_code": 400,
                            "description": "Bad Request: message is not modified"}, 400)])
    api.edit_buttons(5, None)
    assert json.loads(s.posts[0]["data"]["reply_markup"]) == {"inline_keyboard": []}


def test_get_updates_and_answer_callback():
    api, s, _ = make([ok([{"update_id": 1}]), ok(True)])
    assert api.get_updates(5, timeout=30) == [{"update_id": 1}]
    assert s.posts[0]["data"]["offset"] == 5 and s.posts[0]["timeout"] > 30
    api.answer_callback("cb1", "done")
    assert s.posts[1]["data"] == {"callback_query_id": "cb1", "text": "done"}


def test_callback_data_limit():
    with pytest.raises(ValueError):
        tg.keyboard([[("x", "a" * 65)]])


def test_from_config(cfg, monkeypatch, tmp_path):
    monkeypatch.delenv("REEL_TELEGRAM_TOKEN", raising=False)
    cfg.telegram["token_file"] = str(tmp_path / "nope")
    cfg.telegram["chat_id"] = 42
    assert tg.from_config(cfg) is None
    monkeypatch.setenv("REEL_TELEGRAM_TOKEN", "T")
    cfg.telegram["chat_id"] = 0
    assert tg.from_config(cfg) is None
    cfg.telegram["chat_id"] = 42
    a = tg.from_config(cfg)
    assert a.token == "T" and a.chat_id == 42
