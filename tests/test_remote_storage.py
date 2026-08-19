"""Unit tests for RemoteStorage: config resolution and the HTTP upload path.

upload_file talks to a real HTTP endpoint via aiohttp.ClientSession, which a
unit test should never actually open. Instead of a mocking framework, we
substitute pixieveil.storage.remote_storage.aiohttp.ClientSession (via
pytest's monkeypatch) with a small fake session/response pair — same style
as the fake AE/association used for DicomStorage's tests.
"""

import asyncio

import aiohttp

import pixieveil.storage.remote_storage as remote_storage_module
from pixieveil.config import Settings
from pixieveil.storage.remote_storage import RemoteStorage


class FakeResponse:
    def __init__(self, status: int):
        self.status = status

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False


class FakeClientSession:
    """Substituted for aiohttp.ClientSession. post() returns a FakeResponse
    directly (not a coroutine) — matches aiohttp's actual dual-purpose
    request objects, which support `async with session.post(...) as r:`
    without awaiting the post() call itself."""

    last_instance: "FakeClientSession | None" = None

    def __init__(self, response_status: int = 200, post_exception: Exception | None = None):
        self.response_status = response_status
        self.post_exception = post_exception
        self.post_calls: list[dict] = []
        FakeClientSession.last_instance = self

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    def post(self, url, data=None, headers=None):
        self.post_calls.append({"url": url, "data": data, "headers": headers})
        if self.post_exception:
            raise self.post_exception
        return FakeResponse(self.response_status)


class NeverCallSession:
    """Fails the test loudly if upload_file ever reaches ClientSession()
    when it's expected to return early instead."""

    def __init__(self, *a, **k):
        raise AssertionError("aiohttp.ClientSession() should not have been constructed")


def install_fake_session(monkeypatch, response_status=200, post_exception=None):
    FakeClientSession.last_instance = None

    class ScriptedSession(FakeClientSession):
        def __init__(self):
            super().__init__(response_status=response_status, post_exception=post_exception)

    monkeypatch.setattr(remote_storage_module.aiohttp, "ClientSession", ScriptedSession)


def form_field(form_data: aiohttp.FormData, name: str):
    """Return (value, filename, content_type) for a field added via
    FormData.add_field(), or None if not present."""
    for meta, headers, value in form_data._fields:
        if meta["name"] == name:
            return value, meta.get("filename"), headers.get("Content-Type")
    return None


def make_storage(**http_cfg) -> RemoteStorage:
    cfg = {"base_url": "https://storage.example", "auth_token": "secret-token"}
    cfg.update(http_cfg)
    settings = Settings(storage={"remote_storage": {"http": cfg}})
    return RemoteStorage(settings)


class TestConfig:
    def test_enabled_when_base_url_set(self):
        assert make_storage().enabled is True

    def test_disabled_when_not_configured(self):
        assert RemoteStorage(Settings()).enabled is False

    def test_disabled_when_base_url_missing(self):
        settings = Settings(storage={"remote_storage": {"http": {"auth_token": "x"}}})
        assert RemoteStorage(settings).enabled is False

    def test_auth_token_captured(self):
        storage = make_storage(auth_token="tok-123")
        assert storage.auth_token == "tok-123"


class TestUploadFileNotConfigured:
    def test_returns_none_without_touching_network(self, tmp_path, monkeypatch):
        monkeypatch.setattr(remote_storage_module.aiohttp, "ClientSession", NeverCallSession)
        storage = RemoteStorage(Settings())  # no remote_storage.http configured
        local_file = tmp_path / "study.zip"
        local_file.write_bytes(b"zip-content")

        result = asyncio.run(storage.upload_file(local_file, "0001"))

        assert result is None


class TestUploadFileMissingLocalFile:
    def test_returns_false_without_touching_network(self, tmp_path, monkeypatch):
        monkeypatch.setattr(remote_storage_module.aiohttp, "ClientSession", NeverCallSession)
        storage = make_storage()

        result = asyncio.run(storage.upload_file(tmp_path / "missing.zip", "0001"))

        assert result is False


class TestUploadFileSuccess:
    def test_returns_true_on_200(self, tmp_path, monkeypatch):
        install_fake_session(monkeypatch, response_status=200)
        storage = make_storage()
        local_file = tmp_path / "study.zip"
        local_file.write_bytes(b"zip-content")

        result = asyncio.run(storage.upload_file(local_file, "0001.zip"))

        assert result is True

    def test_posts_to_upload_endpoint_with_bearer_token(self, tmp_path, monkeypatch):
        install_fake_session(monkeypatch, response_status=200)
        storage = make_storage(auth_token="tok-abc")
        local_file = tmp_path / "study.zip"
        local_file.write_bytes(b"zip-content")

        asyncio.run(storage.upload_file(local_file, "0001.zip"))

        call = FakeClientSession.last_instance.post_calls[0]
        assert call["url"] == "https://storage.example/upload"
        assert call["headers"] == {"Authorization": "Bearer tok-abc"}

    def test_form_data_carries_file_content_and_remote_path(self, tmp_path, monkeypatch):
        install_fake_session(monkeypatch, response_status=200)
        storage = make_storage()
        local_file = tmp_path / "study.zip"
        local_file.write_bytes(b"the-zip-bytes")

        asyncio.run(storage.upload_file(local_file, "remote/0001.zip"))

        form_data = FakeClientSession.last_instance.post_calls[0]["data"]
        file_value, filename, content_type = form_field(form_data, "file")
        assert file_value == b"the-zip-bytes"
        assert filename == "study.zip"
        assert content_type == "application/zip"

        remote_path_value, _fn, _ct = form_field(form_data, "remote_path")
        assert remote_path_value == "remote/0001.zip"


class TestUploadFileHttpFailure:
    def test_returns_false_on_non_200_status(self, tmp_path, monkeypatch):
        install_fake_session(monkeypatch, response_status=500)
        storage = make_storage()
        local_file = tmp_path / "study.zip"
        local_file.write_bytes(b"zip-content")

        result = asyncio.run(storage.upload_file(local_file, "0001"))

        assert result is False


class TestUploadFileException:
    def test_network_exception_returns_false(self, tmp_path, monkeypatch):
        install_fake_session(
            monkeypatch, post_exception=aiohttp.ClientConnectionError("connection refused")
        )
        storage = make_storage()
        local_file = tmp_path / "study.zip"
        local_file.write_bytes(b"zip-content")

        result = asyncio.run(storage.upload_file(local_file, "0001"))

        assert result is False
