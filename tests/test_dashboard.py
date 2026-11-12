"""Dashboard HTTP layer: routes, payload shapes, auth middleware, validation.

Runs the real aiohttp application through aiohttp.test_utils' in-process
TestServer/TestClient (no port bound beyond localhost, no network), on top
of a real StorageManager rooted in tmp_path. Export stays disabled.
"""

import asyncio

from aiohttp.test_utils import TestClient, TestServer

from pixieveil.dashboard.server import Dashboard
from pixieveil.processing import exam_merge
from pixieveil.storage.exam_sidecar import ExamSidecar
from pixieveil.storage.storage_manager import StorageManager

from .conftest import make_settings
from .test_storage_manager import register_study


def _calls(dashboard: Dashboard, *calls: tuple) -> list[tuple[int, object]]:
    """Run (method, path, kwargs) requests in order against the dashboard app,
    all on one event loop (an aiohttp Application binds to the first loop
    that serves it). Returns [(status, json-or-text body), ...]."""
    async def go():
        out = []
        async with TestClient(TestServer(dashboard.app)) as client:
            for method, path, kwargs in calls:
                resp = await client.request(method, path, **kwargs)
                ctype = resp.headers.get("Content-Type", "")
                body = await resp.json() if "json" in ctype else await resp.text()
                out.append((resp.status, body))
        return out
    return asyncio.run(go())


def _request(dashboard: Dashboard, method: str, path: str, **kwargs):
    """Run one request against the dashboard app; returns (status, body)."""
    return _calls(dashboard, (method, path, kwargs))[0]


def _dashboard(tmp_path, **http_server) -> Dashboard:
    settings = make_settings(tmp_path, http_server=http_server)
    return Dashboard(settings, StorageManager(settings))


def _complete_exam(n: int) -> dict:
    return {
        "study_number": n,
        "image_quality": "Diagnostic",
        "patient": {"age_years": 8.0, "weight_kg": 25.0},
        "indication": {"region": "Chest", "clinical_indication": "Fungal infections",
                       "iv_contrast": "Non-contrast"},
        "scanner": {"manufacturer": "SIEMENS", "model": "Emotion 16",
                    "rhythm_scanner_id": "uuid-1"},
        "rhythm": {"protocol_key": "head_trauma"},
        "rdsr": {}, "series": [], "notes": [],
    }


class TestPages:
    def test_index_and_studies_pages_served(self, tmp_path):
        d = _dashboard(tmp_path)
        for status, body in _calls(d, ("GET", "/", {}), ("GET", "/studies", {})):
            assert status == 200
            assert "/static/theme.css" in body
            # Self-hosted only: the pages must not depend on a CDN.
            assert "cdn." not in body

    def test_static_assets_served(self, tmp_path):
        d = _dashboard(tmp_path)
        results = _calls(d, ("GET", "/static/theme.css", {}), ("GET", "/static/common.js", {}))
        for status, body in results:
            assert status == 200
            assert body


class TestStats:
    def test_shape_and_status_counts(self, tmp_path):
        d = _dashboard(tmp_path)
        sm = d.app["storage_manager"]
        register_study(sm, 1, "ready")
        register_study(sm, 2, "ready")
        register_study(sm, 3, "defacing_failed")
        sm.inc_counter("reception", "images", 10)

        status, body = _request(d, "GET", "/stats")

        assert status == 200
        counts = body["pipeline"]["status_counts"]
        assert counts["ready"] == 2
        assert counts["defacing_failed"] == 1
        assert counts["archived"] == 0
        assert set(body["pipeline"]["active"]) == {"receiving", "waiting", "defacing", "exporting"}
        kpis = {k["key"]: k for k in body["kpis"]}
        assert kpis["received"]["value"] == 10
        assert {"received", "anonymized", "exported", "errors"} <= set(kpis)
        assert all("title" in g and "rows" in g for g in body["details"])
        assert body["capabilities"] == {"dicom": False, "http": False, "rhythm": False}
        assert body["uptime_s"] >= 0


class TestOptions:
    def test_vocabularies_and_scanners(self, tmp_path):
        d = _dashboard(tmp_path)
        d.app["storage_manager"].exam_extractor.scanner_map = {"SIEMENS|Emotion 16": "uuid-1"}

        status, body = _request(d, "GET", "/api/options")

        assert status == 200
        assert len(body["region_indications"]) == 8
        assert body["image_quality"] == ["Diagnostic", "Suboptimal", "Non-diagnostic"]
        assert body["scanners"] == [{"label": "SIEMENS Emotion 16", "id": "uuid-1"}]
        assert body["required_fields"] == list(exam_merge.REQUIRED_PATHS)


class TestStudiesApi:
    def test_list_includes_completeness(self, tmp_path):
        d = _dashboard(tmp_path)
        sm = d.app["storage_manager"]
        register_study(sm, 1, "ready")
        exam = _complete_exam(1)
        exam["image_quality"] = None
        ExamSidecar(1, exam).save(sm.base_path)

        status, body = _request(d, "GET", "/api/studies")

        assert status == 200
        summary = body["studies"][0]["exam_summary"]
        assert summary["missing_fields"] == ["image_quality"]
        assert summary["completeness"] == {"filled": 6, "required": 7}

    def test_detail_reports_corrupt_exam_instead_of_500(self, tmp_path):
        d = _dashboard(tmp_path)
        sm = d.app["storage_manager"]
        register_study(sm, 1, "ready")
        ExamSidecar.path_for(sm.base_path, 1).write_text("{broken")

        status, body = _request(d, "GET", "/api/studies/1")

        assert status == 200
        assert body["exam"] is None
        assert body["exam_corrupt"] is True

    def test_detail_includes_missing_fields(self, tmp_path):
        d = _dashboard(tmp_path)
        sm = d.app["storage_manager"]
        register_study(sm, 1, "ready")
        ExamSidecar(1, _complete_exam(1)).save(sm.base_path)

        status, body = _request(d, "GET", "/api/studies/1")

        assert status == 200
        assert body["missing_fields"] == []
        assert body["exam"]["image_quality"] == "Diagnostic"

    def test_unknown_study_is_404(self, tmp_path):
        status, _ = _request(_dashboard(tmp_path), "GET", "/api/studies/99")
        assert status == 404

    def test_non_numeric_study_number_is_400(self, tmp_path):
        status, _ = _request(_dashboard(tmp_path), "GET", "/api/studies/abc")
        assert status == 400

    def test_unknown_action_is_400(self, tmp_path):
        status, body = _request(_dashboard(tmp_path), "POST", "/api/studies/1/actions/delete")
        assert status == 400
        assert "unknown action" in body["error"]

    def test_save_rejects_unwritable_field(self, tmp_path):
        status, body = _request(_dashboard(tmp_path), "PUT", "/api/studies/1/exam",
                                json={"fields": {"patient.name": "x"}})
        assert status == 400
        assert body["keys"] == ["patient.name"]

    def test_save_roundtrip_updates_missing_fields(self, tmp_path):
        d = _dashboard(tmp_path)
        sm = d.app["storage_manager"]
        register_study(sm, 1, "ready")
        exam = _complete_exam(1)
        exam["image_quality"] = None
        ExamSidecar(1, exam).save(sm.base_path)

        (status, body), (_, detail) = _calls(
            d,
            ("PUT", "/api/studies/1/exam", {"json": {"fields": {"image_quality": "Suboptimal"}}}),
            ("GET", "/api/studies/1", {}),
        )
        assert status == 200 and body["ok"]
        assert detail["exam"]["image_quality"] == "Suboptimal"
        assert detail["missing_fields"] == []


class TestAuthMiddleware:
    def test_mutation_requires_token_when_configured(self, tmp_path):
        d = _dashboard(tmp_path, auth_token="s3cret")
        status, body = _request(d, "PUT", "/api/studies/1/exam", json={"fields": {}})
        assert status == 401
        assert body == {"error": "unauthorized"}

    def test_get_is_never_guarded(self, tmp_path):
        d = _dashboard(tmp_path, auth_token="s3cret")
        status, _ = _request(d, "GET", "/api/studies")
        assert status == 200

    def test_x_auth_token_header_accepted(self, tmp_path):
        d = _dashboard(tmp_path, auth_token="s3cret")
        # Passes auth, then fails validation on the bogus key — proving the
        # request got past the middleware.
        status, _ = _request(d, "PUT", "/api/studies/1/exam",
                             json={"fields": {"bogus": 1}},
                             headers={"X-Auth-Token": "s3cret"})
        assert status == 400

    def test_bearer_token_accepted(self, tmp_path):
        d = _dashboard(tmp_path, auth_token="s3cret")
        status, _ = _request(d, "PUT", "/api/studies/1/exam",
                             json={"fields": {"bogus": 1}},
                             headers={"Authorization": "Bearer s3cret"})
        assert status == 400


class TestAbout:
    def test_page_served(self, tmp_path):
        status, body = _request(_dashboard(tmp_path), "GET", "/about")
        assert status == 200
        assert "Credits" in body
        assert "cdn." not in body

    def test_payload_shape(self, tmp_path):
        d = _dashboard(tmp_path)
        status, body = _request(d, "GET", "/api/about")
        assert status == 200
        assert body["software"]["name"] == "PixieVeil"
        assert body["software"]["version"]
        assert "CT Image Storage" in body["instance"]["sop_classes"]
        assert body["packages"]["core"]["aiohttp"]  # installed in any test run
        assert set(body["packages"]["deface"]) >= {"torch", "nnunetv2"}
        assert body["instance"]["export"] == {"dicom": None, "http": None}

    def test_never_leaks_secrets(self, tmp_path):
        settings = make_settings(
            tmp_path,
            http_server={"auth_token": "dash-s3cret"},
            storage={
                "base_path": str(tmp_path / "data"),
                "temp_path": str(tmp_path / "tmp"),
                "remote_storage": {"http": {"base_url": "https://up.example/api?key=q",
                                            "auth_token": "upload-s3cret"}},
            },
        )
        d = Dashboard(settings, StorageManager(settings))

        status, body = _request(d, "GET", "/api/about")

        assert status == 200
        assert body["instance"]["auth_required"] is True
        assert body["instance"]["export"]["http"] == {"host": "up.example"}
        text = str(body)
        assert "s3cret" not in text and "key=q" not in text


class TestEventsStream:
    """GET /api/events (SSE) — read raw frames off the in-process client."""

    @staticmethod
    async def _frame(resp) -> dict:
        """Read one SSE frame (skipping ': ping' comments) → {id?, event, data}."""
        import json as _json
        frame: dict = {}
        while True:
            line = (await asyncio.wait_for(resp.content.readline(), timeout=3)).decode()
            if line == "\n":
                if frame:
                    return frame
                continue
            if line.startswith(":"):
                continue
            key, _, value = line.rstrip("\n").partition(": ")
            frame[key] = _json.loads(value) if key == "data" else value

    def test_hello_then_study_event_on_status_change(self, tmp_path):
        d = _dashboard(tmp_path)
        sm = d.app["storage_manager"]
        register_study(sm, 1, "complete")

        async def go():
            async with TestClient(TestServer(d.app)) as client:
                sm.events.bind(asyncio.get_running_loop())
                resp = await client.get("/api/events")
                assert resp.status == 200
                assert resp.headers["Content-Type"].startswith("text/event-stream")
                assert resp.headers["Cache-Control"] == "no-cache"
                hello = await self._frame(resp)
                sm.study_manager.mark_study_ready("study-uid-1")
                study = await self._frame(resp)
                resp.close()
                return hello, study
        hello, study = asyncio.run(go())
        assert hello["event"] == "hello"
        assert hello["data"]["resync"] is False
        assert "server_started" in hello["data"]
        assert study["event"] == "study"
        assert study["data"] == {"n": 1, "status": "ready"}
        assert study["id"] == "1"

    def test_reconnect_with_last_event_id_replays_missed_events(self, tmp_path):
        d = _dashboard(tmp_path)
        sm = d.app["storage_manager"]

        async def go():
            async with TestClient(TestServer(d.app)) as client:
                sm.events.bind(asyncio.get_running_loop())
                sm.events.publish("exam", n=4)
                sm.events.publish("exam", n=5)
                resp = await client.get("/api/events", headers={"Last-Event-ID": "1"})
                await self._frame(resp)            # hello
                replayed = await self._frame(resp)
                resp.close()
                return replayed
        assert asyncio.run(go())["data"] == {"n": 5}

    def test_close_all_ends_the_stream_and_unsubscribes(self, tmp_path):
        d = _dashboard(tmp_path)
        sm = d.app["storage_manager"]

        async def go():
            async with TestClient(TestServer(d.app)) as client:
                sm.events.bind(asyncio.get_running_loop())
                resp = await client.get("/api/events")
                await self._frame(resp)
                assert sm.events.subscriber_count == 1
                sm.events.close_all()
                rest = await asyncio.wait_for(resp.content.read(), timeout=3)
                return rest, sm.events.subscriber_count
        rest, count = asyncio.run(go())
        assert rest == b"" and count == 0
