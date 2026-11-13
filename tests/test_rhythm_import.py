"""Import from RHYTHM: the pure planner (scanner/protocol pairing, actions,
indication fill rules), the paginated RHYTHM reader, and the
/api/protocols/rhythm-import route (fakes only — no network)."""
import asyncio
import logging

import pytest
import yaml
from aiohttp.test_utils import TestClient, TestServer

from pixieveil.dashboard import options
from pixieveil.dashboard.server import Dashboard
from pixieveil.processing import rhythm_import as ri
from pixieveil.storage import rhythm_storage
from pixieveil.storage.storage_manager import StorageManager

from .test_settings_editor import TOKEN, _write_settings

VOCAB = {"region_indications": options.REGION_INDICATIONS, "iv_contrast": options.IV_CONTRAST}
U1 = "11111111-1111-1111-1111-111111111111"
U2 = "22222222-2222-2222-2222-222222222222"
SHARED = "551a872c-0000-4000-8000-000000000000"


def _catalog():
    return {"site_code": "S014", "scanners": [
        {"id": "emotion", "name": "Siemens Emotion 16", "manufacturer": "SIEMENS",
         "model": "Emotion 16", "protocols": [
             {"key": "cap-nativ", "label": "CAP_SPIRAL_NATIV", "names": ["CAP_SPIRAL_NATIV"],
              "rhythm_uuid": None, "region": None, "clinical_indication": None,
              "iv_contrast": None},
             {"key": "head", "label": "Head trauma", "names": ["CEREBRAL_SPIRAL_NATIV"],
              "rhythm_uuid": SHARED, "region": "Head", "clinical_indication": "Trauma",
              "iv_contrast": "Non-contrast"},
             {"key": "chest", "label": "Chest", "names": ["THORAX_ROUTINE"],
              "rhythm_uuid": SHARED},
             {"key": "same", "label": "Same", "names": ["ABDOMEN"], "rhythm_uuid": U2},
             {"key": "local-only", "label": "Only here", "names": ["ONLY_HERE"]},
         ]},
        {"id": "optima", "name": "GE Optima", "manufacturer": "GE MEDICAL SYSTEMS",
         "model": "Optima CT520 Series", "protocols": []},
        {"id": "any", "name": "Any scanner", "manufacturer": "", "model": "", "protocols": [
            {"key": "dup-a", "label": "DUP", "names": ["DUP"]}]},
        {"id": "any2", "name": "Any 2", "manufacturer": "", "model": "", "protocols": [
            {"key": "dup-b", "label": "dup", "names": ["Dup"]}]},
    ]}


PROFILES = [
    {"id": "p-emotion", "manufacturer_name": "Siemens Healthineers",
     "scanner_model_name": "SOMATOM Emotion 16", "detector_rows": "16"},
    {"id": "p-optima", "manufacturer_name": "GE HealthCare",
     "scanner_model_name": "Optima CT520"},
    {"id": "p-force", "manufacturer_name": "Siemens Healthineers",
     "scanner_model_name": "SOMATOM Force"},
]


def _proto(uuid, name, scanner="p-emotion", **kw):
    return {"id": uuid, "scanner": scanner, "protocol_name": name, "site_code": "S014", **kw}


def _rows(plan, profile_id):
    sc = next(s for s in plan["scanners"] if s["profile"]["id"] == profile_id)
    return sc, {r["rhythm"]["name"]: r for r in sc["protocols"]}


class TestPairing:
    def test_scanners(self):
        plan = ri.plan_import(_catalog(), PROFILES, [], VOCAB)
        by = {s["profile"]["id"]: s for s in plan["scanners"]}
        assert by["p-emotion"]["match"] == "emotion"
        assert by["p-optima"]["match"] == "optima"
        assert by["p-force"]["match"] is None and by["p-force"]["candidates"] == []
        assert plan["unpaired_local"] == []

    def test_looser_model_is_only_a_candidate(self):
        cat = {"scanners": [{"id": "def", "manufacturer": "SIEMENS", "model": "SOMATOM Definition",
                             "protocols": []}]}
        plan = ri.plan_import(cat, [{"id": "x", "manufacturer_name": "Siemens Healthineers",
                                     "scanner_model_name": "SOMATOM Definition Edge"}], [], VOCAB)
        assert plan["scanners"][0]["match"] is None
        assert plan["scanners"][0]["candidates"] == ["def"]

    def test_different_manufacturer_or_model_number(self):
        assert ri._scanner_score({"manufacturer": "SIEMENS", "model": "Emotion 6"},
                                 PROFILES[0]) == 0
        assert ri._scanner_score({"manufacturer": "Philips", "model": "Emotion 16"},
                                 PROFILES[0]) == 0

    def test_unpaired_local_reported(self):
        plan = ri.plan_import(_catalog(), PROFILES[:1], [], VOCAB)
        assert [s["id"] for s in plan["unpaired_local"]] == ["optima"]

    @pytest.mark.parametrize("a,b", [("CAP_SPIRAL_CONTRAST", "cap spiral  contrast"),
                                     ("Head-Trauma", "head_trauma")])
    def test_name_normalization(self, a, b):
        assert ri.protocol_key(a) == ri.protocol_key(b)


class TestActions:
    def test_each_action(self):
        protos = [
            _proto(U1, "cap spiral nativ", anatomical_region="Head",
                   clinical_indication="trauma", contrast="non-contrast"),
            _proto(U1, "CEREBRAL_SPIRAL_NATIV"),
            _proto(U2, "abdomen"),
            _proto(U1, "Brand new"),
            _proto(U1, "dup"),
        ]
        plan = ri.plan_import(_catalog(), PROFILES, protos, VOCAB)
        sc, rows = _rows(plan, "p-emotion")
        r = rows["cap spiral nativ"]
        assert r["action"] == ri.SET_UUID and r["selected"]
        assert r["local"]["key"] == "cap-nativ"
        assert r["fill"] == {"region": "Head", "clinical_indication": "Trauma",
                             "iv_contrast": "Non-contrast"}
        r = rows["CEREBRAL_SPIRAL_NATIV"]
        assert r["action"] == ri.REPLACE_UUID and not r["selected"] and r["shared_old"]
        assert rows["abdomen"]["action"] == ri.SAME
        assert rows["Brand new"]["action"] == ri.ADD and not rows["Brand new"]["selected"]
        assert rows["Brand new"]["target_scanner"] == "emotion"
        assert rows["dup"]["action"] == ri.AMBIGUOUS
        assert {c["key"] for c in rows["dup"]["candidates"]} == {"dup-a", "dup-b"}
        assert [p["key"] for p in sc["local_not_on_rhythm"]] == ["chest", "local-only"]

    def test_indication_never_overwrites_and_unknown_not_copied(self):
        protos = [_proto(U1, "CEREBRAL_SPIRAL_NATIV", anatomical_region="Chest",
                         clinical_indication="Fungal infections", contrast="Contrast-enhanced"),
                  _proto(U2, "CAP_SPIRAL_NATIV", anatomical_region="Leg",
                         clinical_indication="Fracture", contrast="maybe")]
        _sc, rows = _rows(ri.plan_import(_catalog(), PROFILES, protos, VOCAB), "p-emotion")
        head = rows["CEREBRAL_SPIRAL_NATIV"]
        assert head["fill"] == {} and any("kept the local value" in n for n in head["notes"])
        cap = rows["CAP_SPIRAL_NATIV"]
        assert cap["fill"] == {}
        assert any("not one of the known" in n for n in cap["notes"])
        assert any("not a known value" in n for n in cap["notes"])

    def test_foreign_site_and_orphans(self):
        protos = [_proto(U1, "CAP_SPIRAL_NATIV", site_code="S999"),
                  _proto(U2, "X", scanner="unknown-profile")]
        plan = ri.plan_import(_catalog(), PROFILES, protos, VOCAB, site_code="s014")
        assert [p["name"] for p in plan["foreign_site"]] == ["CAP_SPIRAL_NATIV"]
        assert [p["name"] for p in plan["orphans"]] == ["X"]

    def test_unpaired_profile_add_has_no_target(self):
        plan = ri.plan_import(_catalog(), PROFILES, [_proto(U1, "Force head", "p-force")],
                              VOCAB)
        _sc, rows = _rows(plan, "p-force")
        assert rows["Force head"]["action"] == ri.ADD
        assert rows["Force head"]["target_scanner"] is None


# ------------------------------------------------------------------ fetch_all
class _Resp:
    def __init__(self, status, body):
        self.status, self._body = status, body

    async def json(self, content_type=None):
        return self._body

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


def _fake(pages: dict, seen: list, exc=None):
    class _Session:
        def __init__(self, headers=None, timeout=None):
            self.headers = headers

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        def get(self, url):
            seen.append((url, self.headers.get("Authorization")))
            if exc:
                raise exc
            return _Resp(*pages[url])
    return _Session


B = "https://r.example"


class TestFetchAll:
    def test_follows_next_pages(self, monkeypatch):
        seen = []
        pages = {
            f"{B}/api/v1/protocols-api/?page_size=100":
                (200, {"count": 3, "next": f"{B}/api/v1/protocols-api/?page=2&page_size=100",
                       "results": [{"id": "a"}]}),
            f"{B}/api/v1/protocols-api/?page=2&page_size=100":
                (200, {"next": "/api/v1/protocols-api/?page=3&page_size=100",
                       "results": [{"id": "b"}]}),
            f"{B}/api/v1/protocols-api/?page=3&page_size=100":
                (200, {"next": None, "results": [{"id": "c"}]}),
        }
        monkeypatch.setattr(rhythm_storage.aiohttp, "ClientSession", _fake(pages, seen))
        items = asyncio.run(rhythm_storage.fetch_all(B + "/", "tok", "/api/v1/protocols-api/"))
        assert [i["id"] for i in items] == ["a", "b", "c"]
        assert all(h == "Bearer tok" for _u, h in seen)
        r = asyncio.run(rhythm_storage.probe_connection(B, "tok", ["C", "d"]))
        assert r["ok"] and r["protocols"] == 3 and r["found"] == ["c"] and r["missing"] == ["d"]

    def test_refuses_foreign_next_and_reports_errors(self, monkeypatch):
        pages = {f"{B}/x/?page_size=100": (200, {"next": "https://evil.example/x/?page=2",
                                                 "results": []}),
                 f"{B}/y/?page_size=100": (401, {}),
                 f"{B}/z/?page_size=100": (200, [{"id": "plain"}])}
        monkeypatch.setattr(rhythm_storage.aiohttp, "ClientSession", _fake(pages, []))
        with pytest.raises(rhythm_storage.RhythmApiError, match="another host"):
            asyncio.run(rhythm_storage.fetch_all(B, "t", "/x/"))
        with pytest.raises(rhythm_storage.RhythmApiError) as exc:
            asyncio.run(rhythm_storage.fetch_all(B, "t", "/y/"))
        assert exc.value.status == 401
        assert asyncio.run(rhythm_storage.fetch_all(B, "t", "/z/")) == [{"id": "plain"}]
        monkeypatch.setattr(rhythm_storage.aiohttp, "ClientSession",
                            _fake({}, [], exc=OSError("down")))
        with pytest.raises(rhythm_storage.RhythmApiError) as exc:
            asyncio.run(rhythm_storage.fetch_all(B, "t", "/x/"))
        assert exc.value.status is None


# ------------------------------------------------------------------ route
def _dashboard(tmp_path, configured=True) -> Dashboard:
    path, settings = _write_settings(tmp_path)
    if not configured:
        doc = yaml.safe_load(path.read_text())
        doc["storage"]["remote_storage"] = {}
        path.write_text(yaml.safe_dump(doc))
    d = Dashboard(settings, StorageManager(settings))
    if not configured:
        d.app["storage_manager"].rhythm_storage.base_url = None
        d.app["storage_manager"].rhythm_storage.auth_token = None
    d.user_store.add("root", "root-password", admin=True)
    d.user_store.add("bob", "bob-password")
    d._admin_token, _ = d.user_store.issue_token("root", "t")
    d._user_token, _ = d.user_store.issue_token("bob", "t")
    return d


def _run(d, script):
    async def go():
        async with TestClient(TestServer(d.app)) as client:
            return await script(client)
    return asyncio.run(go())


def _post(d, tok, headers=None):
    async def script(c):
        r = await c.post("/api/protocols/rhythm-import",
                         headers={"Authorization": f"Bearer {tok}", **(headers or {})}, json={})
        return r.status, await r.text()
    return _run(d, script)


class TestRoute:
    def test_admin_only(self, tmp_path):
        d = _dashboard(tmp_path)

        async def script(c):
            out = []
            for tok in ("x", d._user_token):
                r = await c.post("/api/protocols/rhythm-import",
                                 headers={"Authorization": f"Bearer {tok}"}, json={})
                out.append(r.status)
            return out
        assert _run(d, script) == [401, 403]

    def test_unconfigured(self, tmp_path):
        d2 = _dashboard(tmp_path, configured=False)
        status, body = _post(d2, d2._admin_token)
        assert status == 409 and "Settings" in body

    def test_plan_from_saved_token(self, tmp_path, monkeypatch, caplog):
        seen = []
        base = "https://r.example"
        pages = {
            f"{base}/api/v1/scanner-profiles/?page_size=100": (200, {"results": PROFILES[:1]}),
            f"{base}/api/v1/protocols-api/?page_size=100":
                (200, {"results": [_proto(U1, "CAP_SPIRAL_NATIV")]}),
        }
        monkeypatch.setattr(rhythm_storage.aiohttp, "ClientSession", _fake(pages, seen))
        d = _dashboard(tmp_path)
        with caplog.at_level(logging.INFO):
            status, body = _post(d, d._admin_token)
        assert status == 200
        assert seen and all(h == f"Bearer {TOKEN}" for _u, h in seen)
        assert TOKEN not in body and TOKEN not in caplog.text
        assert '"profiles": 1' in body and "action=rhythm_import_preview" in caplog.text
        # Nothing written: the catalog file was not created by the import.
        assert not d.app["storage_manager"].protocol_catalog.path.exists()

    def test_rhythm_rejection_reported(self, tmp_path, monkeypatch):
        pages = {"https://r.example/api/v1/scanner-profiles/?page_size=100": (401, {})}
        monkeypatch.setattr(rhythm_storage.aiohttp, "ClientSession", _fake(pages, []))
        d = _dashboard(tmp_path)
        status, body = _post(d, d._admin_token)
        assert status == 502 and "rejected" in body
