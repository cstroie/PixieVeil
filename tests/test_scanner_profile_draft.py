"""RHYTHM scanner profiles: pre-fill from a study (catalogue model match,
detector rows from the collimation), validation, and the separate
/api/protocols/scanners/{id}/rhythm-profile create/link routes against a
scripted fake RHYTHM (no network). Protocol drafts no longer create them."""
import asyncio
import json
import logging

import pytest
import yaml
from aiohttp.test_utils import TestClient, TestServer

from pixieveil.dashboard.server import Dashboard
from pixieveil.processing import protocol_draft as pd
from pixieveil.processing.rhythm_import import model_tokens
from pixieveil.storage import rhythm_storage
from pixieveil.storage.exam_sidecar import ExamSidecar
from pixieveil.storage.storage_manager import StorageManager

from .test_protocol_draft import BUNDLED, CH, RID, _exam, _post_body, _Resp
from .test_settings_editor import TOKEN, _write_settings

NEW_PROFILE = "55555555-5555-4555-8555-555555555555"
CATALOGUE = {
    "manufacturers": [{"id": "m-siemens", "name": "Siemens Healthineers"},
                      {"id": "m-ge", "name": "GE HealthCare"}],
    "models": [{"id": "mod-emotion", "manufacturer": "m-siemens",
                "name": "Siemens SOMATOM Emotion 16"},
               {"id": "mod-force", "manufacturer": "m-siemens", "name": "Siemens SOMATOM Force"},
               {"id": "mod-optima", "manufacturer": "m-ge", "name": "GE Optima CT520"}],
}


def _exam_rows(total=19.2, single=1.2):
    exam = _exam()
    exam["series"][1].update(total_collimation_width_mm=total,
                             single_collimation_width_mm=single)
    return exam


class TestModelTokens:
    def test_brand_and_manufacturer_words_ignored(self):
        assert model_tokens("Siemens SOMATOM Emotion 16") == model_tokens("Emotion 16")
        assert model_tokens("GE Revolution") != model_tokens("Revolution EVO")


class TestProfileDraft:
    def test_emotion_16(self):
        d = pd.scanner_profile_draft(_exam_rows(), {"SoftwareVersions": "syngo CT 2013A"},
                                     CH, CATALOGUE, 54)
        assert d["manufacturer_id"] == "m-siemens" and d["model_id"] == "mod-emotion"
        assert not d["missing_model"]
        assert d["detector_rows"] == {"value": "16 rows", "other": "", "source": "study",
                                      "note": d["detector_rows"]["note"]}
        assert d["year_of_installation"]["source"] == "fill"
        assert "study 0054" in d["note"] and "syngo CT 2013A" in d["note"]

    def test_missing_model_gives_request_text(self):
        cat = {**CATALOGUE, "models": [m for m in CATALOGUE["models"]
                                       if m["id"] != "mod-emotion"]}
        d = pd.scanner_profile_draft(_exam_rows(), {}, CH, cat, 54)
        assert d["missing_model"] and d["model_id"] is None
        assert "Siemens Healthineers" in d["request_text"]
        assert "Emotion 16" in d["request_text"] and "Detector rows: 16" in d["request_text"]

    @pytest.mark.parametrize("total,single,value,other", [
        (38.4, 0.6, "64 rows", ""), (24.0, 1.0, pd.OTHER, "24 rows"), (None, None, None, "")])
    def test_detector_rows(self, total, single, value, other):
        d = pd.scanner_profile_draft(_exam_rows(total, single), {}, CH, CATALOGUE)
        assert d["detector_rows"]["value"] == value
        assert d["detector_rows"]["other"] == other


class TestValidateProfile:
    def _p(self, **over):
        p = {"manufacturer": "m-siemens", "scanner_model": "mod-emotion",
             "detector_rows": {"value": "16 rows"}, "year_of_installation": {"value": "2013"},
             "note": "n"}
        p.update(over)
        return p

    def test_ok(self):
        payload, errors = pd.validate_profile(self._p(), CH, CATALOGUE)
        assert errors == {}
        assert payload == {"manufacturer": "m-siemens", "scanner_model": "mod-emotion",
                           "detector_rows": "16 rows", "year_of_installation": "2013",
                           "local_protocol_note": "n"}

    def test_errors(self):
        _p, errors = pd.validate_profile(
            self._p(scanner_model="mod-optima", year_of_installation={"value": ""},
                    detector_rows={"value": pd.OTHER, "other": ""}), CH, CATALOGUE)
        assert set(errors) == {"scanner_model", "year_of_installation", "detector_rows"}


# ------------------------------------------------------------------ routes
class FakeRhythm:
    def __init__(self, profiles=(), profile_status=201, protocol_status=201):
        self.profiles = list(profiles)
        self.posts: list = []
        self.headers: list = []
        self.profile_status, self.protocol_status = profile_status, protocol_status

    def session_cls(self):
        fake = self

        class _Session:
            def __init__(self, headers=None, timeout=None, cookie_jar=None):
                fake.headers.append((headers or {}).get("Authorization"))

            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

            def get(self, url):
                return fake._get(url)

            def request(self, method, url, json=None):
                if method == "GET":
                    return fake._get(url)
                fake.posts.append((url.split("/api/v1/")[1], json))
                if "scanner-profiles" in url:
                    if fake.profile_status != 201:
                        return _Resp(fake.profile_status, {"detector_rows": ["bad"]})
                    return _Resp(201, {"id": NEW_PROFILE, **json})
                if fake.protocol_status != 201:
                    return _Resp(fake.protocol_status, {"kvp": ["bad value"]})
                return _Resp(201, {"id": RID, **json})
        return _Session

    def _get(self, url):
        if "/scanner-profiles/" in url:
            return _Resp(200, {"results": self.profiles})
        if "/manufacturers/" in url:
            return _Resp(200, {"results": CATALOGUE["manufacturers"]})
        if "/scanner-models/" in url:
            return _Resp(200, {"results": CATALOGUE["models"]})
        if "/protocol-choices/" in url:
            return _Resp(200, {"results": BUNDLED["categories"]})
        if f"/protocols-api/{RID}/" in url:
            return _Resp(200, {"id": RID})
        if "/protocols-api/" in url:
            return _Resp(200, {"results": []})
        return _Resp(404, {})


@pytest.fixture(autouse=True)
def _fresh_caches():
    rhythm_storage._choices_cache.clear()
    rhythm_storage._catalogue_cache.clear()
    yield
    rhythm_storage._choices_cache.clear()
    rhythm_storage._catalogue_cache.clear()


def _dashboard(tmp_path, monkeypatch, fake) -> Dashboard:
    monkeypatch.setattr(rhythm_storage.aiohttp, "ClientSession", fake.session_cls())
    _path, settings = _write_settings(tmp_path)
    sm = StorageManager(settings)
    d = Dashboard(settings, sm)
    d.user_store.add("root", "root-password", admin=True)
    d._admin_token, _ = d.user_store.issue_token("root", "t")
    sm.base_path.mkdir(parents=True, exist_ok=True)
    ExamSidecar(54, _exam_rows()).save(sm.base_path)
    return d


def _run(d, script):
    async def go():
        async with TestClient(TestServer(d.app)) as client:
            return await script(client)
    return asyncio.run(go())


def _new_profile_body():
    body = _post_body(scanner="")
    body["new_profile"] = {"manufacturer": "m-siemens", "scanner_model": "mod-emotion",
                           "detector_rows": {"value": "16 rows"},
                           "year_of_installation": {"value": "2013"}, "note": "n"}
    return body


def _post(d, body):
    async def script(c):
        r = await c.post("/api/studies/54/protocol-draft",
                         headers={"Authorization": f"Bearer {d._admin_token}"}, json=body)
        return r.status, await r.text()
    status, text = _run(d, script)
    return status, json.loads(text), text


def _catalog(d):
    path = d.app["storage_manager"].protocol_catalog.path
    return yaml.safe_load(path.read_text()) if path.exists() else None


def _seed_scanner(d, rhythm_scanner_id=None):
    """Local catalog scanner 'emotion' (what /protocols would have saved)."""
    cat = d.app["storage_manager"].protocol_catalog
    cat.replace({"scanners": [{"id": "emotion", "name": "Emotion 16", "manufacturer": "SIEMENS",
                               "model": "Emotion 16", "rhythm_scanner_id": rhythm_scanner_id,
                               "protocols": []}]}, expected_version=cat.version)


def _req(d, method, path, body=None):
    async def script(c):
        r = await c.request(method, path, json=body,
                            headers={"Authorization": f"Bearer {d._admin_token}"})
        return r.status, await r.text()
    status, text = _run(d, script)
    return status, json.loads(text), text


PROFILE_URL = "/api/protocols/scanners/emotion/rhythm-profile"
PROFILE_FORM = {"manufacturer": "m-siemens", "scanner_model": "mod-emotion",
                "detector_rows": {"value": "16 rows"},
                "year_of_installation": {"value": "2013"}, "note": "n"}


class TestScannerProfileRoutes:
    """Creating the scanner on RHYTHM is its own step (/protocols), separate
    from creating a protocol."""

    def test_get_drafts_from_a_study(self, tmp_path, monkeypatch):
        d = _dashboard(tmp_path, monkeypatch, FakeRhythm())
        _seed_scanner(d)
        status, body, _ = _req(d, "GET", PROFILE_URL + "?study=54")
        assert status == 200, body
        assert body["study"] == 54 and body["existing"] == []
        assert body["profile_draft"]["model_id"] == "mod-emotion"
        assert body["profile_draft"]["detector_rows"]["value"] == "16 rows"
        assert len(body["catalogue"]["models"]) == 3

    def test_get_without_study_uses_catalog_scanner(self, tmp_path, monkeypatch):
        d = _dashboard(tmp_path, monkeypatch, FakeRhythm())
        _seed_scanner(d)
        status, body, _ = _req(d, "GET", PROFILE_URL)
        assert status == 200 and body["study"] is None
        assert body["profile_draft"]["model_id"] == "mod-emotion"
        assert body["profile_draft"]["detector_rows"]["value"] is None

    def test_get_lists_paired_site_profile(self, tmp_path, monkeypatch):
        fake = FakeRhythm(profiles=[{"id": NEW_PROFILE, "scanner_model": "mod-emotion",
                                     "manufacturer_name": "Siemens Healthineers",
                                     "scanner_model_name": "Siemens SOMATOM Emotion 16"}])
        d = _dashboard(tmp_path, monkeypatch, fake)
        _seed_scanner(d)
        _s, body, _ = _req(d, "GET", PROFILE_URL)
        assert [p["id"] for p in body["existing"]] == [NEW_PROFILE]

    def test_unknown_scanner_404(self, tmp_path, monkeypatch):
        d = _dashboard(tmp_path, monkeypatch, FakeRhythm())
        status, _b, _ = _req(d, "GET", "/api/protocols/scanners/nope/rhythm-profile")
        assert status == 404

    def test_create_records_id_and_only_creates_the_profile(self, tmp_path, monkeypatch,
                                                            caplog):
        fake = FakeRhythm()
        d = _dashboard(tmp_path, monkeypatch, fake)
        _seed_scanner(d)
        with caplog.at_level(logging.INFO):
            status, body, text = _req(d, "POST", PROFILE_URL, {"profile": PROFILE_FORM})
        assert status == 200 and body["ok"] and body["created"]
        assert body["profile_id"] == NEW_PROFILE
        assert [p[0] for p in fake.posts] == ["scanner-profiles/"]     # no protocol
        assert fake.posts[0][1]["detector_rows"] == "16 rows"
        assert _catalog(d)["scanners"][0]["rhythm_scanner_id"] == NEW_PROFILE
        assert "action=rhythm_scanner_profile_created" in caplog.text
        assert TOKEN not in text and TOKEN not in caplog.text

    def test_create_reuses_site_profile_for_same_model(self, tmp_path, monkeypatch):
        fake = FakeRhythm(profiles=[{"id": NEW_PROFILE, "scanner_model": "mod-emotion"}])
        d = _dashboard(tmp_path, monkeypatch, fake)
        _seed_scanner(d)
        status, body, _ = _req(d, "POST", PROFILE_URL, {"profile": PROFILE_FORM})
        assert status == 200 and not body["created"] and body["linked_existing"]
        assert fake.posts == []
        assert _catalog(d)["scanners"][0]["rhythm_scanner_id"] == NEW_PROFILE

    def test_link_existing(self, tmp_path, monkeypatch):
        fake = FakeRhythm(profiles=[{"id": NEW_PROFILE}])
        d = _dashboard(tmp_path, monkeypatch, fake)
        _seed_scanner(d)
        status, body, _ = _req(d, "POST", PROFILE_URL, {"link": NEW_PROFILE})
        assert status == 200 and body["profile_id"] == NEW_PROFILE and fake.posts == []

    def test_link_must_be_a_site_profile(self, tmp_path, monkeypatch):
        d = _dashboard(tmp_path, monkeypatch, FakeRhythm(profiles=[{"id": NEW_PROFILE}]))
        _seed_scanner(d)
        status, body, _ = _req(d, "POST", PROFILE_URL, {"link": "not-a-site-profile"})
        assert status == 400 and "link" in body["errors"]

    def test_rejected_profile_changes_nothing(self, tmp_path, monkeypatch):
        fake = FakeRhythm(profile_status=400)
        d = _dashboard(tmp_path, monkeypatch, fake)
        _seed_scanner(d)
        status, body, _ = _req(d, "POST", PROFILE_URL, {"profile": PROFILE_FORM})
        assert status == 400 and body["errors"] == {"profile.detector_rows": "bad"}
        assert _catalog(d)["scanners"][0]["rhythm_scanner_id"] is None

    def test_invalid_profile_calls_nothing(self, tmp_path, monkeypatch):
        fake = FakeRhythm()
        d = _dashboard(tmp_path, monkeypatch, fake)
        _seed_scanner(d)
        status, body, _ = _req(d, "POST", PROFILE_URL,
                               {"profile": {**PROFILE_FORM, "scanner_model": "mod-optima"}})
        assert status == 400 and "profile.scanner_model" in body["errors"]
        assert fake.posts == []


class TestProtocolDraftNoLongerCreatesScanners:
    def test_new_profile_in_protocol_post_is_refused(self, tmp_path, monkeypatch):
        fake = FakeRhythm()
        d = _dashboard(tmp_path, monkeypatch, fake)
        body = _post_body(scanner="")
        body["new_profile"] = PROFILE_FORM
        status, out, _ = _post(d, body)
        assert status == 400 and "new_profile" in out["errors"]
        assert fake.posts == []

    def test_get_points_to_local_scanner_and_pairs_recorded_profile(self, tmp_path,
                                                                    monkeypatch):
        fake = FakeRhythm(profiles=[{"id": NEW_PROFILE, "manufacturer_name": "Other",
                                     "scanner_model_name": "Renamed"}])
        d = _dashboard(tmp_path, monkeypatch, fake)
        _seed_scanner(d, rhythm_scanner_id=NEW_PROFILE)
        exam = _exam_rows()
        exam["scanner"]["catalog_id"] = "emotion"
        ExamSidecar(54, exam).save(d.app["storage_manager"].base_path)
        status, body, _ = _req(d, "GET", "/api/studies/54/protocol-draft")
        assert status == 200
        assert body["local_scanner_id"] == "emotion"
        assert body["profile_match"] == NEW_PROFILE       # recorded id, not name match
        assert "profile_draft" not in body
