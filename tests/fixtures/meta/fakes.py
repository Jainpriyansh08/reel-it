"""Fake `requests` session serving recorded Graph API responses (no network)."""
from __future__ import annotations

import json
from pathlib import Path

HERE = Path(__file__).parent


def load(name: str) -> dict:
    return json.loads((HERE / f"{name}.json").read_text())


class FakeResponse:
    def __init__(self, status_code: int, body):
        self.status_code = status_code
        self._body = body
        self.text = body if isinstance(body, str) else json.dumps(body)

    def json(self):
        if isinstance(self._body, str):
            raise ValueError("not JSON")
        return self._body


class FakeSession:
    """route(method, url_part, *responses): the first matching route serves its
    responses in order; the last one repeats. Responses are fixture names,
    dicts, (status, fixture_or_body) tuples or exceptions to raise."""

    def __init__(self):
        self.routes: list[list] = []
        self.calls: list[dict] = []

    def route(self, method, url_part, *responses, match=None):
        self.routes.append([method, url_part, list(responses), match])
        return self

    def request(self, method, url, params=None, data=None, headers=None, timeout=None):
        if hasattr(data, "read"):
            data = data.read()
        call = {"method": method, "url": url, "params": dict(params or {}),
                "data": data, "headers": dict(headers or {})}
        self.calls.append(call)
        for m, part, resps, match in self.routes:
            if m == method and part in url and (match is None or match(call)):
                r = resps.pop(0) if len(resps) > 1 else resps[0]
                if isinstance(r, BaseException):
                    raise r
                status, body = r if isinstance(r, tuple) else (200, r)
                if isinstance(body, str) and (HERE / f"{body}.json").exists():
                    body = load(body)
                return FakeResponse(status, body)
        raise AssertionError(f"unexpected request {method} {url} {params}")

    def find(self, method, url_part):
        return [c for c in self.calls if c["method"] == method and url_part in c["url"]]
