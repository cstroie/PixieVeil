"""Scanners & protocols catalog (config/protocols.yaml): model, validation,
matching, legacy migration, the exam-merge bridge, StorageManager's
re-apply / unmatched inbox / usage, and the /protocols dashboard API."""

import asyncio
import os
import time

import pytest
import yaml

from pixieveil.processing import exam_merge
from pixieveil.processing import protocol_catalog as pc
from pixieveil.processing.exam_extractor import ExamExtractor
from pixieveil.storage.exam_sidecar import ExamSidecar

from .conftest import make_settings, write_minimal_dicom
from .test_dashboard import _calls, _request, make_dashboard
from .test_storage_manager import register_study

UUID_A = "551a872c-91e0-434b-bf9d-d0a370b9ae0f"
UUID_B = "7076d1dc-100a-4bcd-a8d7-2ef3672b1e3f"
CHEST = ("Chest", "Fungal infections", "Non-contrast")


def _doc(**over):
    doc = {
        "site_code": "S014",
        "scanners": [
            {"id": "siemens-emotion-16", "name": "Siemens Emotion 16",
             "manufacturer": "SIEMENS", "model": "Emotion 16",
             "rhythm_scanner_id": UUID_B,
             "protocols": [
                 {"key": "cap-spiral-nativ", "label": "CAP_SPIRAL_NATIV",
                  "names": ["CAP_SPIRAL_NATIV"], "rhythm_uuid": UUID_A,
                  "region": "Head", "clinical_indication": "Trauma",
                  "iv_contrast": "Non-contrast"},
                 {"key": "thorax", "label": "Thorax", "names": ["ThoraxRoutine", "Thorax Low"],
                  "rhythm_uuid": None},
             ]},
            {"id": "ge-optima-ct520", "name": "GE Optima CT520",
             "manufacturer": "GE MEDICAL SYSTEMS", "model": "Optima CT520 Series",
             "protocols": [
                 {"key": "ge-cap-spiral-contrast", "label": "CAP spiral contrast",
                  "names": ["CAP_SPIRAL_CONTRAST"], "rhythm_uuid": UUID_B,
                  "region": CHEST[0], "clinical_indication": CHEST[1],
                  "iv_contrast": CHEST[2]},
             ]},
        ],
    }
    doc.update(over)
    return doc


def _write(path, doc):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(doc))
    t = time.time() + 5
    os.utime(path, (t, t))


@pytest.fixture
def catalog(tmp_path):
    p = tmp_path / "protocols.yaml"
    _write(p, _doc())
    return pc.ProtocolCatalog.load(p, expected_site_code="S014")


# ---------------------------------------------------------------- helpers

class TestNameHelpers:
    @pytest.mark.parametrize("desc,expected", [
        ("Head^CAP_SPIRAL_CONTRAST (Adult)", "CAP_SPIRAL_CONTRAST"),
        ("Head^CAP_SPIRAL_NATIV (Adult)", "CAP_SPIRAL_NATIV"),
        ("Thorax^ThoraxRoutine", "ThoraxRoutine"),
        ("ThoraxRoutine (Child)", "ThoraxRoutine"),
        ("A^B^Last One (x)", "Last One"),
        ("", ""), (None, ""),
    ])
    def test_protocol_name_from_description(self, desc, expected):
        assert pc.protocol_name_from_description(desc) == expected

    def test_normalize_and_slug(self):
        assert pc.normalize_name("  Thorax   LOW ") == "thorax low"
        assert pc.slugify("CAP_SPIRAL (Adult)") == "cap-spiral-adult"

    @pytest.mark.parametrize("v,pending", [
        (None, True), ("", True), (pc.NIL_UUID, True), (UUID_A, False),
    ])
    def test_pending(self, v, pending):
        assert pc.is_pending_uuid(v) is pending


# ---------------------------------------------------------------- matching

class TestMatching:
    def test_protocol_name_exact_case_insensitive(self, catalog):
        m = catalog.match("siemens", "EMOTION 16", "cap_spiral_nativ", None)
        assert m.scanner.id == "siemens-emotion-16" and m.protocol.key == "cap-spiral-nativ"

    def test_study_description_fallback(self, catalog):
        m = catalog.match("GE MEDICAL SYSTEMS", "Optima CT520 Series", "",
                          "Head^CAP_SPIRAL_CONTRAST (Adult)")
        assert m.matched and m.protocol.key == "ge-cap-spiral-contrast"
        assert m.protocol_name == "CAP_SPIRAL_CONTRAST"

    def test_description_used_when_protocol_name_differs(self, catalog):
        m = catalog.match("SIEMENS", "Emotion 16", "SomethingElse",
                          "Head^CAP_SPIRAL_NATIV (Adult)")
        assert m.protocol.key == "cap-spiral-nativ"

    def test_alias_and_whitespace(self, catalog):
        assert catalog.match("SIEMENS", "Emotion 16", "thorax   low", None).protocol.key == "thorax"

    def test_same_name_on_other_scanner_does_not_match(self, catalog):
        m = catalog.match("SIEMENS", "Emotion 16", "CAP_SPIRAL_CONTRAST", None)
        assert m.scanner.id == "siemens-emotion-16" and m.protocol is None

    def test_unknown_scanner(self, catalog):
        m = catalog.match("PHILIPS", "Ingenuity", "CAP_SPIRAL_NATIV", None)
        assert m.scanner is None and m.protocol is None

    def test_wildcard_group_matches_any_scanner_after_own(self, tmp_path):
        doc = _doc()
        doc["scanners"].append({"id": "any-scanner", "manufacturer": "", "model": "",
                                "protocols": [{"key": "generic", "names": ["Generic"]},
                                              {"key": "dupe", "names": ["CAP_SPIRAL_NATIV"]}]})
        p = tmp_path / "p.yaml"
        _write(p, doc)
        cat = pc.ProtocolCatalog.load(p)
        assert cat.match("PHILIPS", "X", "generic", None).protocol.key == "generic"
        # the scanner's own protocol wins over the wildcard's same name
        assert cat.match("SIEMENS", "Emotion 16", "CAP_SPIRAL_NATIV", None).protocol.key \
            == "cap-spiral-nativ"

    def test_rhythm_surface(self, catalog):
        assert catalog.uuid_for("cap-spiral-nativ") == UUID_A
        assert catalog.uuid_for("thorax") is None              # pending
        assert catalog.get("thorax").uuid_pending
        assert catalog.uuid_for("nope") is None and catalog.uuid_for(None) is None
        assert [e.id for e in catalog.entries] == [
            "cap-spiral-nativ", "thorax", "ge-cap-spiral-contrast"]


# ---------------------------------------------------------------- validation / persistence

class TestValidationAndPersistence:
    @pytest.mark.parametrize("mutate,needle", [
        (lambda d: d["scanners"][1].update(id="siemens-emotion-16"), "duplicate scanner id"),
        (lambda d: d["scanners"][1].update(id="Bad Id"), "lowercase slug"),
        (lambda d: d["scanners"][1].update(manufacturer="siemens", model="EMOTION 16"),
         "manufacturer/model"),
        (lambda d: d["scanners"][1]["protocols"][0].update(key="thorax"), "duplicate protocol key"),
        (lambda d: d["scanners"][0]["protocols"][1]["names"].append("cap_spiral_nativ"),
         "already used"),
        (lambda d: d["scanners"][0]["protocols"][0].update(rhythm_uuid="not-a-uuid"),
         "not a UUID"),
        (lambda d: d["scanners"][0]["protocols"][0].update(region="Head",
                                                           clinical_indication="Lymphoma"),
         "combinations"),
        (lambda d: d["scanners"][0]["protocols"][0].update(iv_contrast="Maybe"), "iv_contrast"),
        (lambda d: d.update(site_code="S999"), "site_code"),
        (lambda d: d.update(scanners="nope"), "must be a list"),
    ])
    def test_rejects(self, mutate, needle):
        doc = _doc()
        mutate(doc)
        errors = pc.validate_document(doc, expected_site_code="S014")
        assert any(needle in e for e in errors), errors

    def test_valid_document(self):
        assert pc.validate_document(_doc(), expected_site_code="s014") == []

    def test_same_name_allowed_on_different_scanners(self):
        doc = _doc()
        doc["scanners"][1]["protocols"][0]["names"].append("CAP_SPIRAL_NATIV")
        assert pc.validate_document(doc) == []

    def test_load_invalid_raises(self, tmp_path):
        p = tmp_path / "p.yaml"
        _write(p, _doc(site_code="S999"))
        with pytest.raises(pc.CatalogError):
            pc.ProtocolCatalog.load(p, expected_site_code="S014")
        p.write_text("scanners: [unclosed")
        with pytest.raises(pc.CatalogError):
            pc.ProtocolCatalog.load(p)

    def test_missing_file_is_empty(self, tmp_path):
        cat = pc.ProtocolCatalog.load(tmp_path / "none.yaml")
        assert cat.scanners == [] and cat.entries == []
        assert not cat.match("SIEMENS", "Emotion 16", "X", None).matched

    def test_replace_roundtrip_and_versioning(self, catalog):
        v1 = catalog.version
        doc = catalog.document()
        doc["scanners"][0]["protocols"][1]["rhythm_uuid"] = UUID_B
        v2 = catalog.replace(doc, expected_version=v1)
        assert v2 != v1 and catalog.uuid_for("thorax") == UUID_B
        assert "edit on the dashboard" in catalog.path.read_text()
        with pytest.raises(pc.CatalogConflictError):
            catalog.replace(doc, expected_version=v1)
        bad = catalog.document()
        bad["scanners"][0]["protocols"][0]["rhythm_uuid"] = "zzz"
        with pytest.raises(pc.CatalogValidationError) as ei:
            catalog.replace(bad, expected_version=v2)
        assert ei.value.errors
        assert catalog.version == v2                  # nothing written

    def test_nil_uuid_saved_as_null(self, catalog):
        doc = catalog.document()
        doc["scanners"][0]["protocols"][0]["rhythm_uuid"] = pc.NIL_UUID
        catalog.replace(doc, expected_version=None)
        raw = yaml.safe_load(catalog.path.read_text())
        assert raw["scanners"][0]["protocols"][0]["rhythm_uuid"] is None

    def test_reloads_hand_edits_and_keeps_last_good_on_breakage(self, catalog):
        doc = _doc()
        doc["scanners"][0]["protocols"][1]["rhythm_uuid"] = UUID_A
        _write(catalog.path, doc)
        assert catalog.uuid_for("thorax") == UUID_A
        catalog.path.write_text("scanners: [broken")
        t = time.time() + 10
        os.utime(catalog.path, (t, t))
        assert catalog.uuid_for("thorax") == UUID_A     # previous version kept

    def test_legacy_shape_file_reads_as_wildcard(self, tmp_path):
        p = tmp_path / "rhythm_protocols.yaml"
        p.write_text(f"site_code: S014\nprotocols:\n  - id: head\n    name: HeadRoutine\n"
                     f"    uuid: {UUID_A}\n  - id: stub\n    name: Stub\n    uuid: {pc.NIL_UUID}\n")
        cat = pc.ProtocolCatalog.load(p, expected_site_code="S014")
        assert cat.uuid_for("head") == UUID_A and cat.get("stub").uuid_pending
        assert cat.match("ANY", "THING", "headroutine", None).protocol.key == "head"


# ---------------------------------------------------------------- migration

class TestMigration:
    def _legacy(self, tmp_path, scanners=None):
        lookup = tmp_path / "integrations"
        lookup.mkdir()
        (lookup / "scanner_lookup.yaml").write_text(yaml.safe_dump(
            scanners if scanners is not None else {"SIEMENS|Emotion 16": UUID_B}))
        (lookup / "indication_lookup.yaml").write_text(yaml.safe_dump({"rules": [
            {"protocol_name_contains": "HeadRoutine", "body_part_examined_contains": "HEAD",
             "region": "Head", "clinical_indication": "Trauma", "iv_contrast": "Non-contrast"},
            {"study_description_contains": "chest", "region": "Chest",
             "clinical_indication": "Fungal infections", "iv_contrast": "Non-contrast"},
        ]}))
        pmap = tmp_path / "rhythm_protocols.yaml"
        pmap.write_text(yaml.safe_dump({"site_code": "S014", "protocols": [
            {"id": "headroutine", "name": "HeadRoutine", "description": "head",
             "uuid": UUID_A},
            {"id": "abdomen", "name": "Abdomen_contrast", "uuid": pc.NIL_UUID},
        ]}))
        return pmap, lookup

    def test_single_scanner_gets_all_protocols(self, tmp_path, caplog):
        pmap, lookup = self._legacy(tmp_path)
        out = tmp_path / "config" / "protocols.yaml"
        cat = pc.open_catalog(out, expected_site_code="S014",
                              legacy_protocol_map=pmap, legacy_lookup_dir=lookup)
        assert out.exists()
        (scanner,) = cat.scanners
        assert (scanner.manufacturer, scanner.model, scanner.rhythm_scanner_id) == \
            ("SIEMENS", "Emotion 16", UUID_B)
        head = cat.get("headroutine")             # key preserved
        assert head.rhythm_uuid == UUID_A and head.names == ("HeadRoutine",)
        assert (head.region, head.clinical_indication, head.iv_contrast) == \
            ("Head", "Trauma", "Non-contrast")
        assert cat.get("abdomen").uuid_pending and cat.get("abdomen").region is None
        assert "no matching protocol" in caplog.text     # the contains-rule on description
        assert cat.site_code == "S014"

    def test_several_scanners_put_protocols_in_wildcard(self, tmp_path):
        pmap, lookup = self._legacy(tmp_path, {"SIEMENS|Emotion 16": UUID_B,
                                               "GE MEDICAL SYSTEMS|Optima CT520 Series": UUID_A})
        cat = pc.open_catalog(tmp_path / "p.yaml", legacy_protocol_map=pmap,
                              legacy_lookup_dir=lookup)
        wild = cat.get_scanner(pc.UNASSIGNED_SCANNER_ID)
        assert wild.is_wildcard and {p.key for p in wild.protocols} == {"headroutine", "abdomen"}
        assert len(cat.scanners) == 3

    def test_existing_catalog_is_not_overwritten(self, tmp_path):
        pmap, lookup = self._legacy(tmp_path)
        out = tmp_path / "p.yaml"
        _write(out, _doc())
        cat = pc.open_catalog(out, legacy_protocol_map=pmap, legacy_lookup_dir=lookup)
        assert cat.get("headroutine") is None and cat.get("thorax") is not None

    def test_nothing_to_migrate(self, tmp_path):
        out = tmp_path / "p.yaml"
        cat = pc.open_catalog(out, legacy_protocol_map=tmp_path / "x",
                              legacy_lookup_dir=tmp_path / "y")
        assert not out.exists() and cat.entries == []


# ---------------------------------------------------------------- extractor + merge

def _study(tmp_path, *, manufacturer="", model="", protocol="", description=""):
    d = tmp_path / "0001"
    write_minimal_dicom(d / "1" / "1.dcm", Manufacturer=manufacturer,
                        ManufacturerModelName=model, ProtocolName=protocol,
                        StudyDescription=description, Modality="CT", SeriesNumber=1)
    return d


class TestExtractorUsesCatalog:
    def test_match_fills_protocol_indication_and_scanner(self, tmp_path, catalog):
        d = _study(tmp_path)
        hints = {"study": {"manufacturer": "GE MEDICAL SYSTEMS", "model": "Optima CT520 Series",
                           "study_description": "Head^CAP_SPIRAL_CONTRAST (Adult)"}}
        data = ExamExtractor(catalog=catalog).extract(d, 1, "uid", hints=hints)
        assert data["rhythm"]["protocol_key"] == "ge-cap-spiral-contrast"
        assert (data["indication"]["region"], data["indication"]["clinical_indication"],
                data["indication"]["iv_contrast"]) == CHEST
        assert data["indication"]["matched"] is True
        assert data["indication"]["source"] == "catalog"
        assert data["scanner"]["catalog_id"] == "ge-optima-ct520"
        assert data["catalog"]["protocol_name"] == "CAP_SPIRAL_CONTRAST"
        assert data["protocol_used_hint"]["study_description"] == \
            "Head^CAP_SPIRAL_CONTRAST (Adult)"
        assert not [n for n in data["notes"] if n.startswith("Protocol catalog:")]

    def test_matched_protocol_without_indication_still_sets_key(self, tmp_path, catalog):
        d = _study(tmp_path, manufacturer="SIEMENS", model="Emotion 16",
                   protocol="ThoraxRoutine")
        data = ExamExtractor(catalog=catalog).extract(d, 1, "uid")
        assert data["rhythm"]["protocol_key"] == "thorax"
        assert data["indication"]["region"] is None and data["indication"]["matched"] is False

    def test_unknown_scanner_note(self, tmp_path, catalog):
        d = _study(tmp_path, manufacturer="PHILIPS", model="Ingenuity", protocol="X")
        data = ExamExtractor(catalog=catalog).extract(d, 1, "uid")
        assert data["rhythm"]["protocol_key"] is None
        assert any("no scanner" in n and "/protocols" in n for n in data["notes"])

    def test_unknown_protocol_note(self, tmp_path, catalog):
        d = _study(tmp_path, manufacturer="SIEMENS", model="Emotion 16", protocol="Weird")
        data = ExamExtractor(catalog=catalog).extract(d, 1, "uid")
        assert any("no protocol named 'Weird'" in n for n in data["notes"])


class TestApplyCatalogMatch:
    def _exam(self):
        return {"scanner": {"manufacturer": "SIEMENS", "model": "Emotion 16"},
                "indication": {"region": None, "clinical_indication": None,
                               "iv_contrast": "Non-contrast", "matched": False},
                "rhythm": {"protocol_key": None},
                "patient": {"age_years": 30.0, "weight_kg": 70.0},
                "notes": ["No scanner_lookup.yaml entry for x — old note", "keep me"]}

    def test_apply_then_clear(self, catalog):
        exam = self._exam()
        m = catalog.match("SIEMENS", "Emotion 16", "CAP_SPIRAL_NATIV", None)
        assert exam_merge.apply_catalog_match(exam, m) is True
        assert exam["rhythm"]["protocol_key"] == "cap-spiral-nativ"
        assert exam["indication"]["region"] == "Head"
        assert exam["protocol_type"] is not None            # buckets recomputed
        assert "keep me" in exam["notes"]
        assert not any("scanner_lookup" in n for n in exam["notes"])
        assert exam_merge.apply_catalog_match(exam, m) is False   # idempotent

        miss = catalog.match("SIEMENS", "Emotion 16", "Gone", None)
        assert exam_merge.apply_catalog_match(exam, miss) is True
        assert exam["rhythm"]["protocol_key"] is None
        assert exam["indication"]["region"] is None and exam["indication"]["matched"] is False
        assert any("no protocol named 'Gone'" in n for n in exam["notes"])

    def test_manual_fields_win(self, catalog):
        exam = self._exam()
        exam["manual"] = {"fields": {"rhythm.protocol_key": "thorax",
                                     "indication.region": "Chest",
                                     "indication.clinical_indication": "Fungal infections"}}
        exam["rhythm"]["protocol_key"] = "thorax"
        exam["indication"].update(region="Chest", clinical_indication="Fungal infections")
        m = catalog.match("SIEMENS", "Emotion 16", "CAP_SPIRAL_NATIV", None)
        exam_merge.apply_catalog_match(exam, m)
        assert exam["rhythm"]["protocol_key"] == "thorax"
        assert exam["indication"]["region"] == "Chest"

    def test_hand_picked_key_survives_unmatch(self, catalog):
        exam = self._exam()
        exam["rhythm"]["protocol_key"] = "thorax"     # chosen before catalog existed
        exam_merge.apply_catalog_match(exam, catalog.match("SIEMENS", "Emotion 16", "x", None))
        assert exam["rhythm"]["protocol_key"] == "thorax"


# ---------------------------------------------------------------- StorageManager + API

def _exam_for(n, *, manufacturer="GE MEDICAL SYSTEMS", model="Optima CT520 Series",
              name=None, description="Head^CAP_SPIRAL_CONTRAST (Adult)", manual=None):
    exam = {"study_number": n,
            "scanner": {"manufacturer": manufacturer, "model": model},
            "indication": {"region": None, "clinical_indication": None,
                           "iv_contrast": "Non-contrast", "matched": False},
            "rhythm": {"protocol_key": None},
            "protocol_used_hint": {"protocol_name": name, "study_description": description},
            "patient": {"age_years": 8.0, "weight_kg": 25.0},
            "image_quality": "Diagnostic", "series": [], "notes": []}
    if manual:
        exam["manual"] = {"fields": manual}
        for k, v in manual.items():
            exam_merge._set_dotted(exam, k, v)
    return exam


def _dash_with_catalog(tmp_path, doc=None):
    settings = make_settings(tmp_path, storage={
        "base_path": str(tmp_path / "data"), "temp_path": str(tmp_path / "tmp"),
        "protocols_file": str(tmp_path / "cfg" / "protocols.yaml")})
    d = make_dashboard(settings)
    if doc is not None:
        d.app["storage_manager"].protocol_catalog.replace(doc, expected_version=None)
    return d


class TestStorageManagerCatalog:
    def test_reapply_inbox_and_usage(self, tmp_path):
        doc = _doc()
        ge_protocols = doc["scanners"][1]["protocols"]
        doc["scanners"][1]["protocols"] = []        # GE protocol not mapped yet
        d = _dash_with_catalog(tmp_path, doc)
        sm = d.app["storage_manager"]
        for n in (1, 2, 3, 5):
            (sm.base_path / f"{n:04d}").mkdir(parents=True)
        for n in (1, 2):
            register_study(sm, n, "ready")
            ExamSidecar(n, _exam_for(n)).save(sm.base_path)
        register_study(sm, 3, "ready")              # hand-picked protocol stays
        ExamSidecar(3, _exam_for(3, manual={"rhythm.protocol_key": "thorax"})).save(sm.base_path)
        register_study(sm, 4, "archived")           # exported: never touched
        ExamSidecar(4, _exam_for(4)).save(sm.base_path)
        register_study(sm, 5, "ready")              # unknown scanner
        ExamSidecar(5, _exam_for(5, manufacturer="PHILIPS", model="Ingenuity",
                                 name="Brain")).save(sm.base_path)

        inbox = sm.unmatched_protocols_sync()
        ge = next(g for g in inbox if g["manufacturer"] == "GE MEDICAL SYSTEMS")
        assert ge["protocol_name"] == "CAP_SPIRAL_CONTRAST"
        assert ge["studies"] == [1, 2, 3] and ge["scanner_id"] == "ge-optima-ct520"
        ph = next(g for g in inbox if g["manufacturer"] == "PHILIPS")
        assert ph["scanner_id"] is None and ph["studies"] == [5]

        doc["scanners"][1]["protocols"] = ge_protocols
        sm.protocol_catalog.replace(doc, expected_version=None)
        # 5 only gains its "map it on /protocols" note
        assert sm.reapply_catalog_sync() == [1, 2, 3, 5]

        for n in (1, 2):
            exam = ExamSidecar.load(ExamSidecar.path_for(sm.base_path, n)).data
            assert exam["rhythm"]["protocol_key"] == "ge-cap-spiral-contrast"
            assert exam["indication"]["region"] == "Chest"
        exam3 = ExamSidecar.load(ExamSidecar.path_for(sm.base_path, 3)).data
        assert exam3["rhythm"]["protocol_key"] == "thorax"
        assert exam3["indication"]["region"] == "Chest"     # not manual → filled
        exam4 = ExamSidecar.load(ExamSidecar.path_for(sm.base_path, 4)).data
        assert exam4["rhythm"]["protocol_key"] is None

        assert sm.reapply_catalog_sync() == []              # nothing left to change
        assert [g["studies"] for g in sm.unmatched_protocols_sync()] == [[5]]
        assert sm.catalog_usage_sync() == {"ge-cap-spiral-contrast": 2, "thorax": 1}

    def test_matched_study_blocked_until_uuid_set(self, tmp_path):
        d = _dash_with_catalog(tmp_path, _doc())
        sm = d.app["storage_manager"]
        register_study(sm, 1, "ready")
        ExamSidecar(1, _exam_for(1, manufacturer="SIEMENS", model="Emotion 16",
                                 name="ThoraxRoutine", description=None)).save(sm.base_path)
        sm.reapply_catalog_sync()
        _item, err = sm._rhythm_manifest_item_sync(1)
        assert err and "no RHYTHM UUID yet" in err and "/protocols" in err

    def test_options_use_catalog(self, tmp_path):
        d = _dash_with_catalog(tmp_path, _doc())
        status, body = _request(d, "GET", "/api/options")
        assert status == 200
        assert [s["catalog_id"] for s in body["scanners"]] == [
            "ge-optima-ct520", "siemens-emotion-16"]


class TestProtocolsApi:
    def test_page_and_get(self, tmp_path):
        d = _dash_with_catalog(tmp_path, _doc())
        (s1, page), (s2, body) = _calls(d, ("GET", "/protocols", {}),
                                        ("GET", "/api/protocols", {}))
        assert s1 == 200 and "/api/protocols" in page and "cdn." not in page
        assert s2 == 200 and body["editable"] is True and body["version"]
        assert [s["id"] for s in body["catalog"]["scanners"]] == [
            "siemens-emotion-16", "ge-optima-ct520"]
        assert body["usage"] == {}

    def test_put_saves_and_reapplies(self, tmp_path):
        d = _dash_with_catalog(tmp_path, {"scanners": []})
        sm = d.app["storage_manager"]
        register_study(sm, 1, "ready")
        ExamSidecar(1, _exam_for(1)).save(sm.base_path)
        (_, got), (status, body), (_, unmatched) = _calls(
            d, ("GET", "/api/protocols", {}),
            ("PUT", "/api/protocols", {"json": {"catalog": _doc(), "version": None}}),
            ("GET", "/api/protocols/unmatched", {}))
        assert status == 200 and body["restudied"] == [1]
        assert body["version"] != got["version"]
        assert unmatched == {"unmatched": []}
        exam = ExamSidecar.load(ExamSidecar.path_for(sm.base_path, 1)).data
        assert exam["rhythm"]["protocol_key"] == "ge-cap-spiral-contrast"

    def test_put_validation_and_conflict(self, tmp_path):
        d = _dash_with_catalog(tmp_path, _doc())
        bad = _doc()
        bad["scanners"][0]["protocols"][0]["rhythm_uuid"] = "nope"
        (_, got), (s_bad, b_bad), (s_ok, _), (s_stale, b_stale), (s_shape, _) = _calls(
            d, ("GET", "/api/protocols", {}),
            ("PUT", "/api/protocols", {"json": {"catalog": bad, "version": None}}),
            ("PUT", "/api/protocols", {"json": {"catalog": _doc(), "version": None}}),
            ("PUT", "/api/protocols", {"json": {"catalog": _doc(), "version": "stale"}}),
            ("PUT", "/api/protocols", {"json": {"catalog": []}}))
        assert s_bad == 400 and any("not a UUID" in e for e in b_bad["errors"])
        assert s_ok == 200
        assert s_stale == 409 and "changed" in b_stale["error"]
        assert s_shape == 400

    def test_non_admin_reads_but_cannot_write(self, tmp_path):
        d = _dash_with_catalog(tmp_path, _doc())
        d.user_store.add("alice", "alice-password")
        token, _ = d.user_store.issue_token("alice", "t")
        h = {"Authorization": f"Bearer {token}"}

        async def script():
            from aiohttp.test_utils import TestClient, TestServer
            async with TestClient(TestServer(d.app)) as c:
                got = await (await c.get("/api/protocols", headers=h)).json()
                put = await c.put("/api/protocols", headers=h,
                                  json={"catalog": _doc(), "version": None})
                return got["editable"], put.status
        assert asyncio.run(script()) == (False, 403)

    def test_studies_capability_carries_scanner_id(self, tmp_path):
        d = _dash_with_catalog(tmp_path, _doc())
        _, body = _request(d, "GET", "/api/studies")
        protos = {p["id"]: p for p in body["capabilities"]["rhythm_protocols"]}
        assert protos["thorax"]["scanner_id"] == "siemens-emotion-16"
        assert protos["thorax"]["uuid_pending"] is True

    def test_nav_has_protocols_between_studies_and_about(self, tmp_path):
        d = _dash_with_catalog(tmp_path)
        pages = ("/", "/studies", "/about", "/users", "/protocols")
        for page, (_, html) in zip(pages, _calls(d, *[("GET", p, {}) for p in pages])):
            nav = html[html.index('<nav class="nav">'):html.index("</nav>")]
            order = [nav.index(f'href="/{p}"') for p in ("studies", "protocols", "about")]
            assert order == sorted(order), page


def test_legacy_ids_that_are_not_slugs_are_fixed_on_migration():
    from pixieveil.processing.protocol_catalog import (
        _legacy_protocols_to_scanner_protocols, validate_document)
    protos = _legacy_protocols_to_scanner_protocols(
        [{"id": "cap-spiral-nativ-6+", "name": "CAP_N_PESTE_6A"},
         {"id": "cap-spiral-nativ-6", "name": "CAP_6"}])
    assert [p["key"] for p in protos] == ["cap-spiral-nativ-6plus", "cap-spiral-nativ-6"]
    doc = {"scanners": [{"id": "s", "manufacturer": "SIEMENS", "model": "Emotion 16",
                         "protocols": protos}]}
    assert validate_document(doc) == []
