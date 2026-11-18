"""Minimal Instagram Graph API client (Instagram API with Facebook Login).

Publishes Reels to your own professional account with a Page access token
(Standard Access, no App Review) and reads insights. Tests inject a fake
`session` (anything with `.request(method, url, **kw)` returning an object
with `.status_code`, `.json()` and `.text`), so nothing here hits the network
in the test suite.

Flow for a local file (resumable upload):
  1. POST /{ig-user-id}/media  media_type=REELS upload_type=resumable caption=..
       -> {"id": container_id, "uri": "https://rupload.facebook.com/ig-api-upload/<ver>/<id>"}
  2. POST <uri>  headers Authorization: OAuth <token>, offset: 0, file_size: <bytes>
       body = file bytes -> {"success": true}
  3. GET /{container-id}?fields=status_code,status until FINISHED (ERROR/EXPIRED = failure)
  4. POST /{ig-user-id}/media_publish creation_id=<container> -> {"id": media_id}
  5. GET /{media-id}?fields=permalink,timestamp
"""
from __future__ import annotations

import os
import re
from pathlib import Path

GRAPH_HOST = "https://graph.facebook.com"
RUPLOAD_HOST = "https://rupload.facebook.com"

# Throttling / temporary errors: 1 unknown, 2 service unavailable, 4 app rate
# limit, 17 user rate limit, 32 page rate limit, 341 app limit, 613 call rate.
RETRYABLE_CODES = {1, 2, 4, 17, 32, 341, 613}

INSIGHT_METRICS = ["views", "reach", "saved", "shares", "likes", "comments",
                   "total_interactions", "ig_reels_avg_watch_time"]
MEDIA_FIELDS = "id,caption,media_type,media_product_type,timestamp,permalink"


class GraphError(RuntimeError):
    def __init__(self, message: str, code: int | None = None, subcode: int | None = None,
                 http_status: int | None = None):
        super().__init__(f"Graph error {code}/{subcode}: {message}" if code is not None else message)
        self.message = message
        self.code = code
        self.subcode = subcode
        self.http_status = http_status


class TransientGraphError(GraphError):
    """Rate limit, 5xx or network trouble: safe to retry later."""


def _error_from(resp) -> GraphError:
    status = getattr(resp, "status_code", None)
    try:
        body = resp.json()
    except Exception:  # noqa: BLE001 - non-JSON error page
        body = {}
    err = body.get("error") if isinstance(body, dict) else None
    if isinstance(err, dict):
        code, sub = err.get("code"), err.get("error_subcode")
        msg = err.get("error_user_msg") or err.get("message") or "unknown error"
        transient = (bool(err.get("is_transient")) or code in RETRYABLE_CODES
                     or (status or 0) >= 500)
        cls = TransientGraphError if transient else GraphError
        return cls(msg, code, sub, status)
    dbg = body.get("debug_info") if isinstance(body, dict) else None  # rupload errors
    if isinstance(dbg, dict):
        msg = dbg.get("message") or dbg.get("type") or "upload failed"
        cls = TransientGraphError if dbg.get("retriable") or (status or 0) >= 500 else GraphError
        return cls(msg, None, None, status)
    text = (getattr(resp, "text", "") or "")[:300]
    cls = TransientGraphError if (status or 0) >= 500 or status == 429 else GraphError
    return cls(f"HTTP {status}: {text}", None, None, status)


def _failing_metrics(message: str, requested: list[str]) -> list[str]:
    """Which requested metrics an insights error is about. Graph says e.g.
    "(#100) metric[7] must be one of the following values: reach, saved, ..."."""
    idx = [int(i) for i in re.findall(r"metric\[(\d+)\]", message)]
    hit = [requested[i] for i in idx if i < len(requested)]
    if hit:
        return list(dict.fromkeys(hit))
    m = re.search(r"one of the following values:\s*([\w\s,]+)", message)
    if m:
        allowed = {v.strip() for v in m.group(1).split(",")}
        bad = [r for r in requested if r not in allowed]
        if bad and len(bad) < len(requested):
            return bad
    named = [r for r in requested if re.search(rf"\b{re.escape(r)}\b", message)]
    # naming every metric tells us nothing: let the caller go one at a time
    return named if len(named) < len(requested) else []


class GraphClient:
    def __init__(self, token: str, ig_user_id: str, api_version: str = "v24.0",
                 session=None, timeout: int = 60):
        if session is None:
            import requests
            session = requests.Session()
        self.token = token
        self.ig_user_id = str(ig_user_id)
        self.api_version = api_version
        self.session = session
        self.timeout = timeout
        self.base = f"{GRAPH_HOST}/{api_version}"

    # -- transport ---------------------------------------------------------
    def _request(self, method: str, url: str, params=None, data=None, headers=None):
        try:
            resp = self.session.request(method, url, params=params, data=data,
                                        headers=headers, timeout=self.timeout)
        except Exception as e:  # noqa: BLE001
            # requests' ConnectionError/Timeout etc. Never put the URL (token) in the message.
            raise TransientGraphError(f"network error: {type(e).__name__}") from e
        if not 200 <= resp.status_code < 300:
            raise _error_from(resp)
        try:
            body = resp.json()
        except Exception as e:  # noqa: BLE001
            raise TransientGraphError(f"invalid JSON from Graph (HTTP {resp.status_code})") from e
        if isinstance(body, dict) and "error" in body:
            raise _error_from(resp)
        return body

    def _get(self, path: str, **params):
        return self._request("GET", f"{self.base}/{path}",
                             params={**params, "access_token": self.token})

    def _post(self, path: str, **data):
        return self._request("POST", f"{self.base}/{path}",
                             params={"access_token": self.token}, data=data)

    # -- publishing ----------------------------------------------------------
    def create_reel_container(self, caption: str, cover_url: str | None = None,
                              share_to_feed: bool = True) -> tuple[str, str]:
        data = {"media_type": "REELS", "upload_type": "resumable", "caption": caption,
                "share_to_feed": "true" if share_to_feed else "false"}
        if cover_url:
            data["cover_url"] = cover_url
        body = self._post(f"{self.ig_user_id}/media", **data)
        cid = str(body["id"])
        uri = body.get("uri") or f"{RUPLOAD_HOST}/ig-api-upload/{self.api_version}/{cid}"
        return cid, uri

    def upload_file(self, upload_uri: str, path) -> None:
        path = Path(path)
        size = os.path.getsize(path)
        headers = {"Authorization": f"OAuth {self.token}", "offset": "0",
                   "file_size": str(size)}
        with open(path, "rb") as fh:
            body = self._request("POST", upload_uri, data=fh, headers=headers)
        if isinstance(body, dict) and body.get("success") is False:
            raise GraphError(f"upload rejected: {body.get('message', body)}")

    def container_status(self, container_id: str) -> dict:
        """{'status_code': IN_PROGRESS|FINISHED|ERROR|EXPIRED|PUBLISHED, 'status': ...}"""
        return self._get(str(container_id), fields="status_code,status")

    def publish(self, container_id: str) -> str:
        body = self._post(f"{self.ig_user_id}/media_publish", creation_id=str(container_id))
        return str(body["id"])

    # -- reading -------------------------------------------------------------
    def media(self, media_id: str, fields: str = "permalink,timestamp") -> dict:
        return self._get(str(media_id), fields=fields)

    def recent_media(self, limit: int = 25) -> list[dict]:
        body = self._get(f"{self.ig_user_id}/media", fields=MEDIA_FIELDS, limit=int(limit))
        return list(body.get("data", []))

    def _insights_once(self, media_id: str, metrics: list[str]) -> dict:
        body = self._get(f"{media_id}/insights", metric=",".join(metrics))
        out = {}
        for item in body.get("data", []):
            name = item.get("name")
            if "total_value" in item:
                out[name] = (item["total_value"] or {}).get("value")
            else:
                vals = item.get("values") or [{}]
                out[name] = vals[-1].get("value")
        return out

    def insights(self, media_id: str, metrics: list[str] | None = None) -> dict:
        """{metric: value}. A metric the API refuses (deprecated, not available
        for this media) is dropped and the call retried without it; such
        metrics are listed under '_unavailable'."""
        remaining = list(metrics or INSIGHT_METRICS)
        dropped: list[str] = []
        while remaining:
            try:
                out = self._insights_once(media_id, remaining)
                if dropped:
                    out["_unavailable"] = dropped
                return out
            except TransientGraphError:
                raise
            except GraphError as e:
                named = _failing_metrics(e.message, remaining)
                if named:
                    for m in named:
                        remaining.remove(m)
                    dropped += named
                    continue
                if len(remaining) == 1:
                    dropped += remaining
                    remaining = []
                    break
                # Can't tell which metric failed: ask one at a time.
                out = {}
                for m in remaining:
                    try:
                        out.update(self._insights_once(media_id, [m]))
                    except TransientGraphError:
                        raise
                    except GraphError:
                        dropped.append(m)
                if dropped:
                    out["_unavailable"] = dropped
                return out
        return {"_unavailable": dropped}

    def token_health(self) -> tuple[bool, str]:
        try:
            body = self._get(self.ig_user_id, fields="username")
        except TransientGraphError as e:
            return True, f"could not check right now ({e.message})"
        except GraphError as e:
            return False, str(e)
        return True, f"ok (@{body.get('username', '?')})"


def from_config(cfg, session=None) -> GraphClient | None:
    from ..config import read_secret
    pub = cfg.publish
    token = read_secret(pub.get("token_env", "REEL_META_TOKEN"),
                        pub.get("token_file", "~/.config/reel/meta_token"))
    ig_user_id = str(pub.get("ig_user_id") or "").strip()
    if not token or not ig_user_id:
        return None
    return GraphClient(token, ig_user_id, pub.get("api_version", "v24.0"), session=session)
