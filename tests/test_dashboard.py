"""Dashboard HTTP layer: routes, payload shapes, validation.

Runs the real aiohttp application through aiohttp.test_utils' in-process
TestServer/TestClient (no port bound beyond localhost, no network), on top
of a real StorageManager rooted in tmp_path. Export stays disabled.

Every dashboard here has one admin user (``admin``) and requests carry that
user's API token by default (pass ``auth=False`` to go anonymous); the
login/session/CSRF behaviour itself is covered in test_dashboard_auth.py.
"""

import asyncio

from aiohttp.test_utils import TestClient, TestServer

from pixieveil.dashboard.server import Dashboard
from pixieveil.processing import exam_merge
from pixieveil.storage.exam_sidecar import ExamSidecar
from pixieveil.storage.storage_manager import StorageManager

from .conftest import make_settings
from .test_storage_manager import register_study


def _calls(dashboard: Dashboard, *calls: tuple, auth: bool = True) -> list[tuple[int, object]]:
    """Run (method, path, kwargs) requests in order against the dashboard app,
    all on one event loop (an aiohttp Application binds to the first loop
    that serves it). Returns [(status, json-or-text body), ...]."""
    async def go():
        out = []
        async with TestClient(TestServer(dashboard.app)) as client:
            for method, path, kwargs in calls:
                kwargs = dict(kwargs)
                if auth:
                    kwargs["headers"] = {**auth_headers(dashboard), **kwargs.get("headers", {})}
                resp = await client.request(method, path, **kwargs)
                ctype = resp.headers.get("Content-Type", "")
                body = await resp.json() if "json" in ctype else await resp.text()
                out.append((resp.status, body))
        return out
    return asyncio.run(go())


def _request(dashboard: Dashboard, method: str, path: str, auth: bool = True, **kwargs):
    """Run one request against the dashboard app; returns (status, body)."""
    return _calls(dashboard, (method, path, kwargs), auth=auth)[0]


def auth_headers(dashboard: Dashboard) -> dict:
    return {"Authorization": f"Bearer {dashboard._test_token}"}


def make_dashboard(settings) -> Dashboard:
    """A Dashboard over a real StorageManager, with one admin user + API token."""
    d = Dashboard(settings, StorageManager(settings))
    d.user_store.add("admin", "admin-password", admin=True)
    d._test_token, _ = d.user_store.issue_token("admin", "tests")
    return d


def _dashboard(tmp_path, **http_server) -> Dashboard:
    return make_dashboard(make_settings(tmp_path, http_server=http_server))


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
        d.app["storage_manager"].protocol_catalog.replace({"scanners": [
            {"id": "siemens-emotion-16", "name": "SIEMENS Emotion 16",
             "manufacturer": "SIEMENS", "model": "Emotion 16",
             "rhythm_scanner_id": "7076d1dc-100a-4bcd-a8d7-2ef3672b1e3f", "protocols": []},
            {"id": "any-scanner", "manufacturer": "", "model": "", "protocols": []},
        ]}, expected_version=None)

        status, body = _request(d, "GET", "/api/options")

        assert status == 200
        assert len(body["region_indications"]) == 8
        assert body["image_quality"] == ["Diagnostic", "Suboptimal", "Non-diagnostic"]
        assert body["scanners"] == [{"label": "SIEMENS Emotion 16",
                                     "id": "7076d1dc-100a-4bcd-a8d7-2ef3672b1e3f",
                                     "catalog_id": "siemens-emotion-16"}]
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


class TestAuthRequired:
    def test_api_without_credentials_is_401(self, tmp_path):
        status, body = _request(_dashboard(tmp_path), "GET", "/api/studies", auth=False)
        assert status == 401
        assert body == {"error": "unauthorized"}

    def test_bad_token_is_401(self, tmp_path):
        status, _ = _request(_dashboard(tmp_path), "GET", "/api/studies", auth=False,
                             headers={"Authorization": "Bearer pv_dead_beef"})
        assert status == 401

    def test_x_auth_token_header_accepted(self, tmp_path):
        d = _dashboard(tmp_path)
        status, _ = _request(d, "GET", "/api/studies", auth=False,
                             headers={"X-Auth-Token": d._test_token})
        assert status == 200

    def test_token_mutation_needs_no_csrf_header(self, tmp_path):
        # Passes auth, then fails validation on the bogus key — proving the
        # request got past the middleware.
        status, _ = _request(_dashboard(tmp_path), "PUT", "/api/studies/1/exam",
                             json={"fields": {"bogus": 1}})
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
            storage={
                "base_path": str(tmp_path / "data"),
                "temp_path": str(tmp_path / "tmp"),
                "remote_storage": {"http": {"base_url": "https://up.example/api?key=q",
                                            "auth_token": "upload-s3cret"}},
            },
        )
        d = make_dashboard(settings)

        status, body = _request(d, "GET", "/api/about")

        assert status == 200
        assert body["instance"]["auth"] == {"mode": "users", "user_count": 1}
        assert body["instance"]["export"]["http"] == {"host": "up.example"}
        text = str(body)
        assert "s3cret" not in text and "key=q" not in text
        assert d._test_token not in text and "scrypt$" not in text


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
                resp = await client.get("/api/events", headers=auth_headers(d))
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
                resp = await client.get("/api/events", headers={**auth_headers(d), "Last-Event-ID": "1"})
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
                resp = await client.get("/api/events", headers=auth_headers(d))
                await self._frame(resp)
                assert sm.events.subscriber_count == 1
                sm.events.close_all()
                rest = await asyncio.wait_for(resp.content.read(), timeout=3)
                return rest, sm.events.subscriber_count
        rest, count = asyncio.run(go())
        assert rest == b"" and count == 0


class TestLookup:
    def test_search_csv_and_validation(self, tmp_path):
        d = _dashboard(tmp_path)
        sm = d.app["storage_manager"]
        register_study(sm, 1, "archived", archived_via="rhythm")
        sm.study_manager.set_rhythm_job("study-uid-1", batch="b1", job_id="j1",
                                        status="COMPLETE", repository_study_id="RHY-1")
        h = auth_headers(d)
        (s1, found), (s2, none), (s3, _), (s4, csv_body), (s5, page) = _calls(
            d,
            ("GET", "/api/lookup?q=pid", {"headers": h}),
            ("GET", "/api/lookup?q=other", {"headers": h}),
            ("GET", "/api/lookup", {"headers": h}),
            ("GET", "/api/lookup?all=1&format=csv", {"headers": h}),
            ("GET", "/lookup", {"headers": h}),
        )
        assert s1 == 200 and [r["study_number"] for r in found["rows"]] == [1]
        assert found["rows"][0]["rhythm_repository_study_ids"] == ["RHY-1"]
        assert s2 == 200 and none["rows"] == []
        assert s3 == 400
        assert s4 == 200 and "RHY-1" in csv_body and "original_patient_id" in csv_body
        assert s5 == 200 and "/api/lookup" in page
