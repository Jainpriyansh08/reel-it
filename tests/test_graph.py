import pytest

from reel.meta import graph
from reel.meta.graph import GraphClient, GraphError, TransientGraphError
from tests.fixtures.meta.fakes import FakeSession

BASE = "https://graph.facebook.com/v24.0"
IG = "17841400000000000"


def client(sess):
    return GraphClient("TOKEN", IG, "v24.0", session=sess)


def test_create_container_request_shape():
    s = FakeSession().route("POST", f"{IG}/media", "container_create")
    cid, uri = client(s).create_reel_container("hello #x", cover_url="https://x/c.jpg")
    assert cid == "17900000000000001"
    assert uri == "https://rupload.facebook.com/ig-api-upload/v24.0/17900000000000001"
    c = s.calls[0]
    assert c["url"] == f"{BASE}/{IG}/media"
    assert c["params"] == {"access_token": "TOKEN"}
    assert c["data"] == {"media_type": "REELS", "upload_type": "resumable", "caption": "hello #x",
                         "share_to_feed": "true", "cover_url": "https://x/c.jpg"}


def test_upload_file_rupload_headers(tmp_path):
    f = tmp_path / "final.mp4"
    f.write_bytes(b"x" * 1234)
    uri = "https://rupload.facebook.com/ig-api-upload/v24.0/179"
    s = FakeSession().route("POST", "rupload.facebook.com", "rupload_success")
    client(s).upload_file(uri, f)
    c = s.calls[0]
    assert c["url"] == uri
    assert c["headers"] == {"Authorization": "OAuth TOKEN", "offset": "0", "file_size": "1234"}
    assert c["data"] == b"x" * 1234
    assert "access_token" not in c["params"]


def test_status_publish_media_recent():
    s = (FakeSession()
         .route("POST", f"{IG}/media_publish", "media_publish")
         .route("GET", f"{IG}/media", "recent_media")
         .route("GET", "/179", "container_finished")
         .route("GET", "/18000000000000042", "media_permalink"))
    c = client(s)
    assert c.container_status("179")["status_code"] == "FINISHED"
    assert s.calls[-1]["params"]["fields"] == "status_code,status"
    assert c.publish("179") == "18000000000000042"
    assert s.calls[-1]["data"] == {"creation_id": "179"}
    assert c.media("18000000000000042", "permalink,timestamp")["permalink"].endswith("ABC123xyz/")
    items = c.recent_media(25)
    assert len(items) == 3
    p = s.calls[-1]["params"]
    assert p["limit"] == 25 and "media_product_type" in p["fields"]


def test_insights_parses_values_and_total_value():
    s = FakeSession().route("GET", "/insights", "insights")
    out = client(s).insights("18000000000000042", ["views", "reach", "saved", "shares"])
    assert out == {"views": 1520, "reach": 1210, "saved": 31, "shares": 12}
    assert s.calls[0]["url"] == f"{BASE}/18000000000000042/insights"
    assert s.calls[0]["params"]["metric"] == "views,reach,saved,shares"


def test_insights_drops_named_failing_metric():
    s = FakeSession().route("GET", "/insights", (400, "error_invalid_metric"), "insights")
    out = client(s).insights("m1")
    assert out["views"] == 1520
    assert out["_unavailable"] == ["ig_reels_avg_watch_time"]
    assert "ig_reels_avg_watch_time" in s.calls[0]["params"]["metric"]
    assert "ig_reels_avg_watch_time" not in s.calls[1]["params"]["metric"]


def test_insights_unnamed_failure_falls_back_per_metric():
    def ok_single(call):
        return call["params"]["metric"] == "views"
    s = (FakeSession()
         .route("GET", "/insights", {"data": [{"name": "views", "values": [{"value": 9}]}]},
                match=ok_single)
         .route("GET", "/insights", (400, "error_unsupported_media")))
    out = client(s).insights("m1", ["views", "reach"])
    assert out == {"views": 9, "_unavailable": ["reach"]}


def test_transient_vs_permanent_errors():
    s = FakeSession().route("GET", "/insights", (400, "error_rate_limit"))
    with pytest.raises(TransientGraphError) as e:
        client(s).insights("m1")
    assert e.value.code == 4

    s = FakeSession().route("POST", "media_publish", (500, "oops"))
    with pytest.raises(TransientGraphError):
        client(s).publish("c")

    s = FakeSession().route("POST", "media_publish", ConnectionError("boom"))
    with pytest.raises(TransientGraphError):
        client(s).publish("c")

    s = FakeSession().route("POST", "media_publish", (400, "error_permission"))
    with pytest.raises(GraphError) as e:
        client(s).publish("c")
    assert not isinstance(e.value, TransientGraphError)
    assert (e.value.code, e.value.subcode) == (10, 2207050)
    assert "TOKEN" not in str(e.value)


@pytest.mark.parametrize("code", [4, 17, 32, 613])
def test_rate_limit_codes_are_transient(code):
    s = FakeSession().route("GET", IG, (400, {"error": {"message": "limit", "code": code}}))
    with pytest.raises(TransientGraphError):
        client(s).recent_media()


def test_token_health():
    s = FakeSession().route("GET", IG, "username")
    ok, detail = client(s).token_health()
    assert ok and "my.account" in detail
    assert s.calls[0]["params"]["fields"] == "username"
    s = FakeSession().route("GET", IG, (400, "error_token"))
    ok, detail = client(s).token_health()
    assert not ok and "expired" in detail


def test_from_config(cfg, monkeypatch, tmp_path):
    monkeypatch.delenv("REEL_META_TOKEN", raising=False)
    cfg.publish = {**cfg.publish, "token_file": str(tmp_path / "none"), "ig_user_id": IG}
    assert graph.from_config(cfg) is None
    monkeypatch.setenv("REEL_META_TOKEN", "T")
    c = graph.from_config(cfg, session=FakeSession())
    assert c.token == "T" and c.ig_user_id == IG and c.api_version == "v24.0"
    cfg.publish = {**cfg.publish, "ig_user_id": ""}
    assert graph.from_config(cfg) is None
