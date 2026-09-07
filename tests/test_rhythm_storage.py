"""Unit tests for RhythmStorage: config resolution and the chunked-upload
state machine (init -> chunks -> complete -> poll).

RhythmStorage talks to RHYTHM over aiohttp; a unit test must never open a
real connection. Following the same style as test_remote_storage.py, we
substitute pixieveil.storage.rhythm_storage.aiohttp.ClientSession via
monkeypatch with a small fake that routes each request to a scripted queue
of fake responses keyed by endpoint.
"""

import asyncio

import aiohttp

import pixieveil.storage.rhythm_storage as rhythm_storage_module
from pixieveil.config import Settings
from pixieveil.storage.rhythm_storage import RhythmStorage


# -- fakes -----------------------------------------------------------------


class FakeResp:
    def __init__(self, status=200, body=None, raise_exc=None):
        self.status = status
        self._body = body if body is not None else {}
        self._raise_exc = raise_exc

    async def __aenter__(self):
        if self._raise_exc is not None:
            raise self._raise_exc
        return self

    async def __aexit__(self, *a):
        return False

    async def json(self, content_type=None):
        return self._body


class FakeSession:
    """Substituted for aiohttp.ClientSession. Routes by endpoint to a queue
    of FakeResp; the last entry in a queue repeats once exhausted."""

    last_instance: "FakeSession | None" = None

    def __init__(self, routes, headers=None):
        self.routes = {k: list(v) for k, v in routes.items()}
        self.headers = headers
        self.calls: list[dict] = []
        FakeSession.last_instance = self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    def _route_key(self, method, url):
        if "/chunked/init/" in url:
            return "init"
        if "/chunk/" in url:
            return "chunk"
        if "/complete/" in url:
            return "complete"
        return "job"

    def _next(self, method, url):
        key = self._route_key(method, url)
        queue = self.routes.get(key, [FakeResp(200, {})])
        resp = queue.pop(0) if len(queue) > 1 else queue[0]
        return resp

    def post(self, url, json=None, params=None, data=None):
        self.calls.append(
            {"method": "POST", "url": url, "json": json, "params": params, "data": data}
        )
        return self._next("POST", url)

    def get(self, url):
        self.calls.append({"method": "GET", "url": url, "json": None})
        return self._next("GET", url)


class NeverCallSession:
    def __init__(self, *a, **k):
        raise AssertionError("aiohttp.ClientSession() should not have been constructed")


def install(monkeypatch, routes):
    FakeSession.last_instance = None

    def factory(headers=None):
        return FakeSession(routes, headers=headers)

    monkeypatch.setattr(rhythm_storage_module.aiohttp, "ClientSession", factory)


def make_storage(**rhythm_cfg) -> RhythmStorage:
    cfg = {
        "base_url": "https://rhythm.example/",
        "auth_token": "tok-xyz",
        "site_code": "S001",
        "poll_interval_s": 0,
        "poll_timeout_s": 5,
    }
    cfg.update(rhythm_cfg)
    settings = Settings(storage={"remote_storage": {"rhythm": cfg}})
    return RhythmStorage(settings)


def a_zip(tmp_path, content=b"zip-bytes"):
    p = tmp_path / "0042_anonymized.zip"
    p.write_bytes(content)
    return p


ITEM = {"ref": "ROW0042", "filename": "0042_anonymized.zip", "site_code": "S001"}


def happy_routes():
    return {
        "init": [FakeResp(201, {"session_id": "sess-1", "total_chunks": 1})],
        "chunk": [FakeResp(202, {"progress_percent": 100, "needs_reupload": False})],
        "complete": [
            FakeResp(200, {"job_id": "job-9", "job_status_url": "/api/v1/uploads/job-9/"})
        ],
    }


# -- config --------------------------------------------------------------


class TestConfig:
    def test_enabled_needs_all_three(self):
        assert make_storage().enabled is True

    def test_disabled_without_site_code(self):
        s = RhythmStorage(
            Settings(storage={"remote_storage": {"rhythm": {
                "base_url": "https://x", "auth_token": "t"}}})
        )
        assert s.enabled is False

    def test_disabled_when_absent(self):
        assert RhythmStorage(Settings()).enabled is False

    def test_defaults_and_overrides(self):
        assert make_storage().chunk_size == 10 * 1024 * 1024
        assert make_storage(chunk_size=4096).chunk_size == 4096
        assert make_storage().base_url == "https://rhythm.example"  # trailing / stripped

    def test_missing_protocol_map_is_empty_not_fatal(self, tmp_path):
        s = make_storage(protocol_map=str(tmp_path / "nope.yaml"))
        assert s.enabled is True
        assert s.protocol_map.entries == []

    def test_valid_protocol_map_loads(self, tmp_path):
        p = tmp_path / "protos.yaml"
        p.write_text(
            "site_code: S001\nprotocols:\n  - id: head\n"
            "    uuid: 7076d1dc-100a-4bcd-a8d7-2ef3672b1e3f\n"
        )
        s = make_storage(protocol_map=str(p))
        assert s.enabled is True
        assert s.protocol_map.uuid_for("head") == "7076d1dc-100a-4bcd-a8d7-2ef3672b1e3f"

    def test_malformed_protocol_map_disables_rhythm_but_does_not_raise(self, tmp_path):
        p = tmp_path / "protos.yaml"
        p.write_text("protocols:\n  - id: head\n    uuid: not-a-uuid\n")
        s = make_storage(protocol_map=str(p))          # must NOT raise
        assert s.enabled is False
        assert s.protocol_map.entries == []

    def test_protocol_map_site_code_mismatch_disables_rhythm(self, tmp_path):
        p = tmp_path / "protos.yaml"
        p.write_text(
            "site_code: S999\nprotocols:\n  - id: head\n"
            "    uuid: 7076d1dc-100a-4bcd-a8d7-2ef3672b1e3f\n"
        )
        s = make_storage(protocol_map=str(p))          # site_code is S001
        assert s.enabled is False


# -- guard rails -------------------------------------------------------


class TestGuards:
    def test_not_configured_returns_error_without_network(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            rhythm_storage_module.aiohttp, "ClientSession", NeverCallSession
        )
        s = RhythmStorage(Settings())
        r = asyncio.run(s.upload_study(a_zip(tmp_path), ITEM, "batch-1"))
        assert r.ok is False and "not configured" in r.error

    def test_missing_file_returns_error_without_network(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            rhythm_storage_module.aiohttp, "ClientSession", NeverCallSession
        )
        s = make_storage()
        r = asyncio.run(s.upload_study(tmp_path / "nope.zip", ITEM, "batch-1"))
        assert r.ok is False and "file not found" in r.error


# -- happy path ------------------------------------------------------


class TestHappyPath:
    def test_returns_after_complete_without_polling(self, tmp_path, monkeypatch):
        install(monkeypatch, happy_routes())
        r = asyncio.run(make_storage().upload_study(a_zip(tmp_path), ITEM, "batch-1"))
        assert r.ok is True                 # chunks assembled, job queued
        assert r.status is None             # not polled here — that's check_job
        assert r.session_id == "sess-1"
        assert r.job_id == "job-9"
        assert r.job_status_url == "/api/v1/uploads/job-9/"
        assert not [c for c in FakeSession.last_instance.calls if c["method"] == "GET"]

    def test_init_payload_and_auth_header(self, tmp_path, monkeypatch):
        install(monkeypatch, happy_routes())
        asyncio.run(make_storage().upload_study(a_zip(tmp_path), ITEM, "batch-7"))
        session = FakeSession.last_instance
        assert session.headers == {"Authorization": "Bearer tok-xyz"}
        init_call = next(c for c in session.calls if "/chunked/init/" in c["url"])
        body = init_call["json"]
        assert body["filename"] == "0042_anonymized.zip"
        assert body["batch"] == "batch-7"
        assert body["manifest_item"] == ITEM
        assert body["chunk_size"] == 10 * 1024 * 1024
        assert len(body["file_hash"]) == 64
        assert body["total_size"] == len(b"zip-bytes")

    def test_provided_file_hash_is_used_without_rehashing(self, tmp_path, monkeypatch):
        install(monkeypatch, happy_routes())
        asyncio.run(
            make_storage().upload_study(a_zip(tmp_path), ITEM, "b", file_hash="cafef00d")
        )
        calls = FakeSession.last_instance.calls
        init_call = next(c for c in calls if "/chunked/init/" in c["url"])
        complete_call = next(c for c in calls if "/complete/" in c["url"])
        assert init_call["json"]["file_hash"] == "cafef00d"
        assert complete_call["json"]["file_hash"] == "cafef00d"

    def test_multiple_chunks_all_sent_with_hashes(self, tmp_path, monkeypatch):
        routes = happy_routes()
        routes["init"] = [FakeResp(201, {"session_id": "s", "total_chunks": 3})]
        install(monkeypatch, routes)
        s = make_storage(chunk_size=10)
        asyncio.run(s.upload_study(a_zip(tmp_path, b"x" * 25), ITEM, "b"))
        chunk_calls = [
            c for c in FakeSession.last_instance.calls if "/chunk/" in c["url"]
        ]
        assert [c["params"]["chunk_number"] for c in chunk_calls] == [0, 1, 2]
        assert all(len(c["params"]["chunk_hash"]) == 64 for c in chunk_calls)
        assert chunk_calls[0]["data"] == b"x" * 10
        assert chunk_calls[2]["data"] == b"x" * 5


# -- failure modes -------------------------------------------------


class TestFailures:
    def test_init_rejects_manifest_item(self, tmp_path, monkeypatch):
        routes = happy_routes()
        routes["init"] = [
            FakeResp(400, {"code": "invalid_manifest_item",
                           "error": [{"field": "$.image_quality", "code": "required"}]})
        ]
        install(monkeypatch, routes)
        r = asyncio.run(make_storage().upload_study(a_zip(tmp_path), ITEM, "b"))
        assert r.ok is False
        assert "init failed" in r.error
        assert r.error_report == [{"field": "$.image_quality", "code": "required"}]

    def test_chunk_retries_then_gives_up(self, tmp_path, monkeypatch):
        routes = happy_routes()
        routes["chunk"] = [FakeResp(500, {})]
        install(monkeypatch, routes)
        r = asyncio.run(make_storage().upload_study(a_zip(tmp_path), ITEM, "b"))
        assert r.ok is False
        assert "chunk 0 failed after 3 attempts" in r.error
        assert r.session_id == "sess-1"
        chunk_calls = [
            c for c in FakeSession.last_instance.calls if "/chunk/" in c["url"]
        ]
        assert len(chunk_calls) == 3  # 3 retries

    def test_chunk_needs_reupload_then_succeeds(self, tmp_path, monkeypatch):
        routes = happy_routes()
        routes["chunk"] = [
            FakeResp(202, {"needs_reupload": True}),
            FakeResp(202, {"needs_reupload": False}),
        ]
        install(monkeypatch, routes)
        r = asyncio.run(make_storage().upload_study(a_zip(tmp_path), ITEM, "b"))
        assert r.ok is True

    def test_chunk_network_error_retried(self, tmp_path, monkeypatch):
        routes = happy_routes()
        routes["chunk"] = [
            FakeResp(raise_exc=aiohttp.ClientConnectionError("refused")),
            FakeResp(202, {"needs_reupload": False}),
        ]
        install(monkeypatch, routes)
        r = asyncio.run(make_storage().upload_study(a_zip(tmp_path), ITEM, "b"))
        assert r.ok is True

    def test_complete_non_200(self, tmp_path, monkeypatch):
        routes = happy_routes()
        routes["complete"] = [FakeResp(503, {})]
        install(monkeypatch, routes)
        r = asyncio.run(make_storage().upload_study(a_zip(tmp_path), ITEM, "b"))
        assert r.ok is False and "complete failed" in r.error

    def test_unexpected_exception_is_reported_not_raised(self, tmp_path, monkeypatch):
        def boom(headers=None):
            raise RuntimeError("kaboom")

        monkeypatch.setattr(
            rhythm_storage_module.aiohttp, "ClientSession", boom
        )
        r = asyncio.run(make_storage().upload_study(a_zip(tmp_path), ITEM, "b"))
        assert r.ok is False and "kaboom" in r.error


# -- job status polling (check_job) --------------------------------------


class TestCheckJob:
    def _check(self, monkeypatch, body, status=200):
        install(monkeypatch, {"job": [FakeResp(status, body)]})
        return asyncio.run(make_storage().check_job("/api/v1/uploads/job-9/"))

    def test_complete_is_ok(self, monkeypatch):
        r = self._check(monkeypatch, {"status": "COMPLETE", "error_report": None})
        assert r.ok is True and r.status == "COMPLETE"

    def test_failed_carries_report_and_is_not_ok(self, monkeypatch):
        r = self._check(
            monkeypatch,
            {"status": "FAILED", "error_report": [{"code": "gdpr", "message": "bad"}]},
        )
        assert r.ok is False
        assert r.status == "FAILED"
        assert r.error == "job ended FAILED"
        assert r.error_report == [{"code": "gdpr", "message": "bad"}]

    def test_partial_is_not_ok(self, monkeypatch):
        r = self._check(monkeypatch, {"status": "PARTIAL", "error_report": ["x"]})
        assert r.ok is False and r.status == "PARTIAL"

    def test_pending_is_not_ok_and_has_no_error(self, monkeypatch):
        r = self._check(monkeypatch, {"status": "PROCESSING"})
        assert r.ok is False
        assert r.status == "PROCESSING"
        assert r.error is None          # merely not done yet — caller re-polls

    def test_non_200_is_a_transient_error(self, monkeypatch):
        r = self._check(monkeypatch, {}, status=502)
        assert r.ok is False and "HTTP 502" in r.error

    def test_disabled_storage_short_circuits(self):
        r = asyncio.run(RhythmStorage(Settings()).check_job("/x/"))
        assert r.ok is False and "not configured" in r.error
