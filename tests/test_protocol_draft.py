"""Draft a protocol from a study: the pure drafter (RDSR/header → RHYTHM
options with provenance), submission validation, and the
/api/studies/{n}/protocol-draft routes against a scripted fake RHYTHM
(no network). Uses the bundled offline choices snapshot."""
import asyncio
import json
import logging
from pathlib import Path

import pytest
import yaml
from aiohttp.test_utils import TestClient, TestServer

from pixieveil.dashboard.server import Dashboard
from pixieveil.processing import protocol_draft as pd
from pixieveil.storage import rhythm_storage
from pixieveil.storage.exam_sidecar import ExamSidecar
from pixieveil.storage.storage_manager import StorageManager

from .test_settings_editor import TOKEN, _write_settings

BUNDLED = json.loads((Path(pd.__file__).parent.parent / "dashboard" / "rhythm_choices.json")
                     .read_text())
CH = pd.Choices(BUNDLED["categories"], BUNDLED["manufacturer_field_options"],
                BUNDLED["ma_modulation_inputs"])
RID = "33333333-3333-4333-8333-333333333333"


def _exam(age=2, weight=30, region="Head", ci="Trauma", iv="Non-contrast", rdsr=True):
    exam = {
        "study_number": 54,
        "patient": {"age_years": age, "weight_kg": weight},
        "scanner": {"manufacturer": "SIEMENS", "model": "Emotion 16"},
        "indication": {"region": region, "clinical_indication": ci, "iv_contrast": iv},
        "protocol_used_hint": {"protocol_name": "CEREBRAL_SPIRAL_NATIV"},
        "series": [
            {"series_number": 1, "is_topogram": True, "convolution_kernel": "T20f",
             "slice_thickness_mm": 0.6},
            {"series_number": 2, "is_topogram": False, "convolution_kernel": "H31s",
             "slice_thickness_mm": 5.0, "spiral_pitch_factor": 0.55,
             "series_description": "Head 5.0 H31s"},
        ],
        "rdsr": {"total_dlp_mgy_cm": 457.9, "acquisitions": [
            {"acquisition_protocol": "Topogram", "acquisition_type": "Constant Angle Acquisition",
             "kvp": 110.0, "modulation_type": "OFF"},
            {"acquisition_protocol": "Head", "acquisition_type": "Spiral Acquisition",
             "pitch": 0.55, "kvp": 110.0, "mean_ma": 60.0, "rotation_time_s": 1.5,
             "mean_ctdivol_mgy": 23.96, "modulation_type": "XYZ_EC"}]} if rdsr else {},
    }
    return exam


def _f(draft, key):
    return draft["rhythm"]["fields"][key]


class TestDraft:
    def test_pediatric_head_from_rdsr(self):
        d = pd.draft_from_exam(_exam(), {"ReconstructionDiameter": "171"}, CH,
                               {"scanners": []}, 54)
        assert d["local"]["names"] == ["CEREBRAL_SPIRAL_NATIV"]
        assert d["local"]["new_scanner"]["model"] == "Emotion 16"
        expect = {"protocol_type": ("PEDIATRIC_HEAD", "guess"), "age_group": ("1y_6y", "study"),
                  "examination_group": ("Group 3 – Early Childhood", "guess"),
                  "scan_type": ("Helical / spiral", "study"), "contrast": ("No contrast", "study"),
                  "number_of_phases": ("1", "study"), "kvp": ("110", "study"),
                  "auto_ma_modulation": ("CareDose4D", "guess"), "pitch": ("0.5-0.8", "study"),
                  "rotation_time": ("1.5", "study"), "slice_thickness": ("5", "study"),
                  "kernel_class": ("Soft tissue", "guess"),
                  "reconstruction_algorithm": ("Filtered back projection", "guess"),
                  "scan_fov": ("Small head", "guess")}
        for key, (value, source) in expect.items():
            assert (_f(d, key)["value"], _f(d, key)["source"]) == (value, source), key
        assert _f(d, "auto_kvp_selection")["source"] == "fill"
        assert d["missing"] == ["auto_kvp_selection"]
        mas = d["rhythm"]["fields"]["mas_inputs"]
        assert mas["labels"] == ["Quality Reference mAs (QR mAs)"]
        assert mas["value"]["Quality Reference mAs (QR mAs)"] == "164"   # 60*1.5/0.55
        assert "study 0054" in d["rhythm"]["notes"] and "23.96" in d["rhythm"]["notes"]

    def test_body_by_weight_and_adult(self):
        body = pd.draft_from_exam(_exam(age=10, weight=40, region="Chest",
                                        ci="Fungal infections"), {}, CH, {"scanners": []})
        assert _f(body, "protocol_type")["value"] == "PEDIATRIC_BODY"
        assert _f(body, "age_group")["value"] == "30_50kg"
        assert _f(body, "examination_group")["value"].startswith("Group 4")
        adult = pd.draft_from_exam(_exam(age=30, weight=90, region="Chest",
                                         ci="Fungal infections"), {}, CH, {"scanners": []})
        assert _f(adult, "protocol_type")["value"] == "YOUNG_ADULT"
        assert _f(adult, "age_group")["value"] == "gt_80kg"
        assert _f(adult, "examination_group")["value"].startswith("Group 6")

    def test_header_fallbacks_and_iterative_kernel(self):
        exam = _exam(rdsr=False, iv="Contrast-enhanced")
        exam["series"][1]["convolution_kernel"] = "Br40f/3"
        d = pd.draft_from_exam(exam, {"KVP": "100", "ExposureModulationType": "XYZ_EC"}, CH,
                               {"scanners": []})
        assert _f(d, "kvp")["value"] == "100"
        assert _f(d, "pitch")["value"] == "0.5-0.8"            # from the series header
        assert _f(d, "scan_type")["value"] == "Helical / spiral"
        assert _f(d, "contrast")["value"] == "IV contrast only"
        assert _f(d, "reconstruction_algorithm")["value"] == "Model-based iterative reconstruction"
        assert _f(d, "rotation_time")["source"] == "fill"       # nothing without RDSR

    def test_off_list_value_becomes_other(self):
        exam = _exam()
        exam["rdsr"]["acquisitions"][1]["rotation_time_s"] = 0.42
        d = pd.draft_from_exam(exam, {}, CH, {"scanners": []})
        assert _f(d, "rotation_time")["value"] == pd.OTHER
        assert _f(d, "rotation_time")["other"] == "0.42"

    def test_known_local_scanner(self):
        doc = {"scanners": [{"id": "emotion", "manufacturer": "siemens", "model": "EMOTION 16",
                             "protocols": []}]}
        d = pd.draft_from_exam(_exam(), {}, CH, doc)
        assert d["local"]["scanner_id"] == "emotion" and d["local"]["new_scanner"] is None

    @pytest.mark.parametrize("kernel,cls", [("B70f", "Bone"), ("I30f", "Soft tissue"),
                                            ("Br40f/3", "Standard"), ("B60s", "Bone")])
    def test_kernel_classes(self, kernel, cls):
        assert pd.kernel_class_for(kernel)[0] == cls


def _submission(**over):
    d = pd.draft_from_exam(_exam(), {}, CH, {"scanners": []}, 54)
    fields = {k: {"value": v.get("value"), "other": v.get("other", "")}
              for k, v in d["rhythm"]["fields"].items() if k != "mas_inputs"}
    fields["auto_kvp_selection"] = {"value": "Off"}
    rhythm = {"scanner": "p-emotion", "protocol_type": "PEDIATRIC_HEAD", "fields": fields,
              "mas_inputs": d["rhythm"]["fields"]["mas_inputs"]["value"],
              "protocol_name": "CEREBRAL_SPIRAL_NATIV", "anatomical_region": "Head",
              "clinical_indication": "Trauma", "notes": "n",
              "manufacturer": "Siemens Healthineers"}
    rhythm.update(over)
    return d, rhythm


class TestValidate:
    def test_valid_payload(self):
        _d, rhythm = _submission()
        payload, errors = pd.validate_submission(rhythm, CH)
        assert errors == {}
        assert payload["kvp"] == "110" and payload["auto_ma_modulation"] == "CareDose4D"
        assert payload["examination_group"] == "Group 3 – Early Childhood"
        assert payload["mas_inputs"] == {"Quality Reference mAs (QR mAs)": "164"}
        assert payload["scanner"] == "p-emotion"

    def test_errors(self):
        _d, rhythm = _submission(scanner="", protocol_type="X")
        rhythm["fields"]["kvp"] = {"value": "999"}
        rhythm["fields"]["pitch"] = {"value": pd.OTHER, "other": ""}
        rhythm["fields"]["kernel_class"] = {"value": ""}
        rhythm["mas_inputs"] = {}
        _p, errors = pd.validate_submission(rhythm, CH)
        assert {"scanner", "protocol_type", "kvp", "pitch", "kernel_class"} <= set(errors)

    def test_other_text_sent(self):
        _d, rhythm = _submission()
        rhythm["fields"]["rotation_time"] = {"value": pd.OTHER, "other": "0.42"}
        payload, errors = pd.validate_submission(rhythm, CH)
        assert errors == {} and payload["rotation_time"] == "0.42"


# ------------------------------------------------------------------ routes
class _Resp:
    def __init__(self, status, body):
        self.status, self._body = status, body

    async def json(self, content_type=None):
        return self._body

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


class FakeRhythm:
    """Scripted RHYTHM: GET by URL prefix, POST protocols-api/."""

    def __init__(self, create_status=201, create_body=None):
        self.posts: list = []
        self.headers: list = []
        self.create_status = create_status
        self.create_body = create_body

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
                fake.posts.append((url, json))
                body = fake.create_body if fake.create_body is not None else {
                    "id": RID, **(json or {})}
                return _Resp(fake.create_status, body)
        return _Session

    def _get(self, url):
        if "/scanner-profiles/" in url:
            return _Resp(200, {"results": [{"id": "p-emotion",
                                            "manufacturer_name": "Siemens Healthineers",
                                            "scanner_model_name": "SOMATOM Emotion 16"}]})
        if "/protocol-choices/" in url:
            return _Resp(200, {"results": BUNDLED["categories"]})
        if f"/protocols-api/{RID}/" in url:
            return _Resp(200, {"id": RID, "protocol_type": "PEDIATRIC_HEAD",
                               "age_group": "1y_6y"})
        if "/protocols-api/" in url:
            return _Resp(200, {"results": [{"id": "old-1", "scanner": "p-emotion",
                                            "protocol_type": "PEDIATRIC_HEAD",
                                            "protocol_name": "cerebral spiral nativ",
                                            "age_group": "gt_6y"}]})
        return _Resp(404, {})


@pytest.fixture(autouse=True)
def _fresh_choice_cache():
    rhythm_storage._choices_cache.clear()
    yield
    rhythm_storage._choices_cache.clear()


def _dashboard(tmp_path, monkeypatch, fake: FakeRhythm) -> Dashboard:
    monkeypatch.setattr(rhythm_storage.aiohttp, "ClientSession", fake.session_cls())
    _path, settings = _write_settings(tmp_path)
    sm = StorageManager(settings)
    d = Dashboard(settings, sm)
    d.user_store.add("root", "root-password", admin=True)
    d.user_store.add("bob", "bob-password")
    d._admin_token, _ = d.user_store.issue_token("root", "t")
    d._user_token, _ = d.user_store.issue_token("bob", "t")
    sm.base_path.mkdir(parents=True, exist_ok=True)
    ExamSidecar(54, _exam()).save(sm.base_path)
    return d


def _run(d, script):
    async def go():
        async with TestClient(TestServer(d.app)) as client:
            return await script(client)
    return asyncio.run(go())


def _h(tok):
    return {"Authorization": f"Bearer {tok}"}


def _post_body(**rhythm_over):
    _d, rhythm = _submission(**rhythm_over)
    local = {"names": ["CEREBRAL_SPIRAL_NATIV"], "label": "Head trauma", "key": "",
             "region": "Head", "clinical_indication": "Trauma", "iv_contrast": "Non-contrast",
             "scanner_id": None, "new_scanner": {"manufacturer": "SIEMENS",
                                                 "model": "Emotion 16",
                                                 "name": "Siemens Emotion 16"}}
    return {"local": local, "rhythm": rhythm}


class TestRoutes:
    def test_admin_only(self, tmp_path, monkeypatch):
        d = _dashboard(tmp_path, monkeypatch, FakeRhythm())

        async def script(c):
            a = await c.get("/api/studies/54/protocol-draft", headers=_h(d._user_token))
            b = await c.post("/api/studies/54/protocol-draft", headers=_h(d._user_token), json={})
            missing = await c.get("/api/studies/99/protocol-draft", headers=_h(d._admin_token))
            return a.status, b.status, missing.status
        assert _run(d, script) == (403, 403, 404)

    def test_get_draft(self, tmp_path, monkeypatch):
        d = _dashboard(tmp_path, monkeypatch, FakeRhythm())

        async def script(c):
            r = await c.get("/api/studies/54/protocol-draft", headers=_h(d._admin_token))
            return r.status, await r.text()
        status, text = _run(d, script)
        body = json.loads(text)
        assert status == 200
        assert body["choices_source"] == "live" and body["profile_match"] == "p-emotion"
        assert [e["id"] for e in body["existing"]] == ["old-1"]
        assert body["draft"]["rhythm"]["fields"]["kvp"]["value"] == "110"
        assert TOKEN not in text

    def test_create_adds_local_protocol(self, tmp_path, monkeypatch, caplog):
        fake = FakeRhythm()
        d = _dashboard(tmp_path, monkeypatch, fake)

        async def script(c):
            r = await c.post("/api/studies/54/protocol-draft", headers=_h(d._admin_token),
                             json=_post_body())
            return r.status, await r.text()
        with caplog.at_level(logging.INFO):
            status, text = _run(d, script)
        body = json.loads(text)
        assert status == 200 and body["ok"] and body["rhythm_id"] == RID
        url, payload = fake.posts[0]
        assert url.endswith("/api/v1/protocols-api/")
        assert payload["kvp"] == "110" and payload["examination_group"].startswith("Group 3")
        assert all(h == f"Bearer {TOKEN}" for h in fake.headers)
        assert any("examination group" in w for w in body["warnings"])
        doc = yaml.safe_load(d.app["storage_manager"].protocol_catalog.path.read_text())
        sc = doc["scanners"][0]
        assert sc["manufacturer"] == "SIEMENS" and sc["model"] == "Emotion 16"
        assert sc["protocols"][0]["rhythm_uuid"] == RID
        assert sc["protocols"][0]["names"] == ["CEREBRAL_SPIRAL_NATIV"]
        assert TOKEN not in text and TOKEN not in caplog.text
        assert "action=rhythm_protocol_created" in caplog.text

    def test_invalid_submission_calls_nothing(self, tmp_path, monkeypatch):
        fake = FakeRhythm()
        d = _dashboard(tmp_path, monkeypatch, fake)
        body = _post_body(scanner="")
        body["local"]["names"] = []

        async def script(c):
            r = await c.post("/api/studies/54/protocol-draft", headers=_h(d._admin_token),
                             json=body)
            return r.status, await r.json()
        status, out = _run(d, script)
        assert status == 400
        assert {"local.names", "rhythm.scanner"} <= set(out["errors"])
        assert fake.posts == []
        assert not d.app["storage_manager"].protocol_catalog.path.exists()

    def test_rhythm_400_leaves_catalog(self, tmp_path, monkeypatch):
        fake = FakeRhythm(create_status=400, create_body={"kvp": ["bad value"]})
        d = _dashboard(tmp_path, monkeypatch, fake)

        async def script(c):
            r = await c.post("/api/studies/54/protocol-draft", headers=_h(d._admin_token),
                             json=_post_body())
            return r.status, await r.json()
        status, out = _run(d, script)
        assert status == 400 and out["errors"] == {"rhythm.kvp": "bad value"}
        assert not d.app["storage_manager"].protocol_catalog.path.exists()

    def test_use_existing_skips_create(self, tmp_path, monkeypatch):
        fake = FakeRhythm()
        d = _dashboard(tmp_path, monkeypatch, fake)
        body = _post_body()
        body["use_existing"] = RID

        async def script(c):
            r = await c.post("/api/studies/54/protocol-draft", headers=_h(d._admin_token),
                             json=body)
            return r.status, await r.json()
        status, out = _run(d, script)
        assert status == 200 and out["linked_existing"] and fake.posts == []

    def test_local_save_failure_keeps_rhythm_id(self, tmp_path, monkeypatch):
        fake = FakeRhythm()
        d = _dashboard(tmp_path, monkeypatch, fake)
        from pixieveil.processing import protocol_catalog

        def boom(*a, **k):
            raise protocol_catalog.CatalogConflictError("changed meanwhile")
        monkeypatch.setattr(d.app["storage_manager"].protocol_catalog, "replace", boom)

        async def script(c):
            r = await c.post("/api/studies/54/protocol-draft", headers=_h(d._admin_token),
                             json=_post_body())
            return r.status, await r.json()
        status, out = _run(d, script)
        assert status == 200 and out["ok"] is False and out["rhythm_id"] == RID
        assert RID in out["error"]

    def test_unconfigured(self, tmp_path, monkeypatch):
        d = _dashboard(tmp_path, monkeypatch, FakeRhythm())
        path = d.settings_editor.path
        doc = yaml.safe_load(path.read_text())
        doc["storage"]["remote_storage"] = {}
        path.write_text(yaml.safe_dump(doc))
        rs = d.app["storage_manager"].rhythm_storage
        rs.base_url = rs.auth_token = None

        async def script(c):
            g = await c.get("/api/studies/54/protocol-draft", headers=_h(d._admin_token))
            p = await c.post("/api/studies/54/protocol-draft", headers=_h(d._admin_token),
                             json=_post_body())
            return await g.json(), p.status
        g, p = _run(d, script)
        assert g["choices_source"] == "offline" and g["rhythm_configured"] is False
        assert p == 409
