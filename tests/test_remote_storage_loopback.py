"""RemoteStorage against a real in-process aiohttp server (loopback only).

test_remote_storage.py swaps aiohttp.ClientSession for a fake that never
serialises the request, so it can't tell whether the multipart body is
parseable, what the auth header really says, or how a non-200 success
status is treated.
"""

import asyncio
import hashlib

from aiohttp import web
from aiohttp.test_utils import TestServer

from pixieveil.config import Settings
from pixieveil.storage.remote_storage import RemoteStorage


def _run(tmp_path, *, status=200, auth_token="tok"):
    got: dict = {}

    async def upload(request):
        got["auth"] = request.headers.get("Authorization")
        form = await request.post()
        f = form["file"]
        got["filename"] = f.filename
        got["content_type"] = f.content_type
        got["sha"] = hashlib.sha256(f.file.read()).hexdigest()
        got["remote_path"] = form["remote_path"]
        return web.Response(status=status)

    zip_path = tmp_path / "0001.zip"
    payload = b"PK\x03\x04" + bytes(range(256)) * 400
    zip_path.write_bytes(payload)

    async def main():
        app = web.Application(client_max_size=10 * 1024 * 1024)
        app.router.add_post("/upload", upload)
        async with TestServer(app, host="127.0.0.1") as server:
            http = {"base_url": str(server.make_url("")).rstrip("/")}
            if auth_token is not None:
                http["auth_token"] = auth_token
            rs = RemoteStorage(Settings(storage={"remote_storage": {"http": http}}))
            return await rs.upload_file(zip_path, "studies/0001.zip")

    ok = asyncio.run(main())
    return ok, got, hashlib.sha256(payload).hexdigest()


class TestLoopbackUpload:
    def test_multipart_body_is_parseable_and_intact(self, tmp_path):
        ok, got, sha = _run(tmp_path)
        assert ok is True
        assert got["filename"] == "0001.zip"
        assert got["content_type"] == "application/zip"
        assert got["sha"] == sha
        assert got["remote_path"] == "studies/0001.zip"
        assert got["auth"] == "Bearer tok"

    def test_201_created_counts_as_success(self, tmp_path):
        ok, _got, _sha = _run(tmp_path, status=201)
        assert ok is True

    def test_server_error_is_failure(self, tmp_path):
        ok, _got, _sha = _run(tmp_path, status=500)
        assert ok is False

    def test_no_token_sends_no_bogus_authorization_header(self, tmp_path):
        ok, got, _sha = _run(tmp_path, auth_token=None)
        assert ok is True
        assert got["auth"] is None          # not the literal "Bearer None"
