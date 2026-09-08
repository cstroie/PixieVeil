"""Unit tests for the local-protocol -> RHYTHM CTProtocol UUID map loader."""

import pytest

from pixieveil.processing.rhythm_protocols import (
    NIL_UUID,
    ProtocolMap,
    ProtocolMapError,
    append_stub,
    slugify_protocol_name,
)


_UUID = "7076d1dc-100a-4bcd-a8d7-2ef3672b1e3f"
_UUID2 = "2b1e3f00-100a-4bcd-a8d7-7076d1dc4bcd"


def _write(tmp_path, text):
    p = tmp_path / "rhythm_protocols.yaml"
    p.write_text(text)
    return p


class TestLoad:
    def test_missing_file_is_empty_map_not_error(self, tmp_path):
        m = ProtocolMap.load(tmp_path / "nope.yaml")
        assert m.entries == []
        assert m.get("anything") is None
        assert m.uuid_for("anything") is None

    def test_happy_path(self, tmp_path):
        p = _write(tmp_path, (
            "site_code: S014\n"
            "protocols:\n"
            "  - id: head-trauma\n"
            "    name: Head trauma routine\n"
            "    description: non-contrast head\n"
            f"    uuid: {_UUID}\n"
            "  - id: chest-hrct\n"
            f"    uuid: {_UUID2}\n"
        ))
        m = ProtocolMap.load(p)
        assert [e.id for e in m.entries] == ["head-trauma", "chest-hrct"]
        assert m.uuid_for("head-trauma") == _UUID
        assert m.get("head-trauma").name == "Head trauma routine"
        assert m.get("chest-hrct").name == ""          # optional
        assert m.uuid_for("unknown") is None

    def test_site_code_match_is_case_insensitive(self, tmp_path):
        p = _write(tmp_path, f"site_code: s014\nprotocols:\n  - id: a\n    uuid: {_UUID}\n")
        m = ProtocolMap.load(p, expected_site_code="S014")
        assert m.site_code == "s014"

    def test_site_code_mismatch_raises(self, tmp_path):
        p = _write(tmp_path, f"site_code: S099\nprotocols:\n  - id: a\n    uuid: {_UUID}\n")
        with pytest.raises(ProtocolMapError, match="does not match"):
            ProtocolMap.load(p, expected_site_code="S014")

    def test_no_site_code_in_file_skips_the_check(self, tmp_path):
        p = _write(tmp_path, f"protocols:\n  - id: a\n    uuid: {_UUID}\n")
        m = ProtocolMap.load(p, expected_site_code="S014")
        assert m.uuid_for("a") == _UUID

    def test_bad_uuid_raises(self, tmp_path):
        p = _write(tmp_path, "protocols:\n  - id: a\n    uuid: not-a-uuid\n")
        with pytest.raises(ProtocolMapError, match="is not a UUID"):
            ProtocolMap.load(p)

    def test_missing_uuid_raises(self, tmp_path):
        p = _write(tmp_path, "protocols:\n  - id: a\n    name: x\n")
        with pytest.raises(ProtocolMapError, match="has no 'uuid'"):
            ProtocolMap.load(p)

    def test_missing_id_raises(self, tmp_path):
        p = _write(tmp_path, f"protocols:\n  - uuid: {_UUID}\n")
        with pytest.raises(ProtocolMapError, match="has no 'id'"):
            ProtocolMap.load(p)

    def test_duplicate_id_raises(self, tmp_path):
        p = _write(tmp_path, (
            "protocols:\n"
            f"  - id: a\n    uuid: {_UUID}\n"
            f"  - id: a\n    uuid: {_UUID2}\n"
        ))
        with pytest.raises(ProtocolMapError, match="duplicate protocol id"):
            ProtocolMap.load(p)

    def test_protocols_not_a_list_raises(self, tmp_path):
        p = _write(tmp_path, "protocols: oops\n")
        with pytest.raises(ProtocolMapError, match="must be a list"):
            ProtocolMap.load(p)

    def test_top_level_not_a_mapping_raises(self, tmp_path):
        p = _write(tmp_path, "- just\n- a\n- list\n")
        with pytest.raises(ProtocolMapError, match="must be a mapping"):
            ProtocolMap.load(p)

    def test_empty_file_is_empty_map(self, tmp_path):
        p = _write(tmp_path, "")
        m = ProtocolMap.load(p)
        assert m.entries == []


class TestNilUuid:
    def test_nil_uuid_loads_but_resolves_to_none(self, tmp_path):
        p = _write(tmp_path, f"protocols:\n  - id: a\n    uuid: \"{NIL_UUID}\"\n")
        m = ProtocolMap.load(p)
        assert m.get("a") is not None          # entry is present (dropdown)
        assert m.get("a").uuid_pending is True
        assert m.uuid_for("a") is None         # ...but blocks the manifest

    def test_real_uuid_is_not_pending(self, tmp_path):
        p = _write(tmp_path, f"protocols:\n  - id: a\n    uuid: {_UUID}\n")
        m = ProtocolMap.load(p)
        assert m.get("a").uuid_pending is False
        assert m.uuid_for("a") == _UUID


class TestSlugify:
    @pytest.mark.parametrize("raw,slug", [
        ("Head trauma routine", "head-trauma-routine"),
        ("  CHEST/HRCT (ILD) ", "chest-hrct-ild"),
        ("Thorax_1.0_B31f", "thorax-1-0-b31f"),
        ("", ""),
        ("///", ""),
    ])
    def test_slugify(self, raw, slug):
        assert slugify_protocol_name(raw) == slug


class TestAppendStub:
    def test_creates_missing_file(self, tmp_path):
        p = tmp_path / "rhythm_protocols.yaml"
        slug = append_stub(p, protocol_name="Head trauma")
        assert slug == "head-trauma"
        m = ProtocolMap.load(p)
        assert m.get("head-trauma").uuid_pending is True
        assert m.get("head-trauma").name == "Head trauma"

    def test_appends_and_keeps_existing_entries(self, tmp_path):
        p = _write(tmp_path, (
            "site_code: S014\n"
            "protocols:\n"
            "  - id: head-trauma\n"
            f"    uuid: {_UUID}\n"
        ))
        slug = append_stub(p, protocol_name="Chest HRCT", description="ILD")
        assert slug == "chest-hrct"
        m = ProtocolMap.load(p)                 # whole file still valid
        assert [e.id for e in m.entries] == ["head-trauma", "chest-hrct"]
        assert m.uuid_for("head-trauma") == _UUID          # untouched
        assert m.uuid_for("chest-hrct") is None            # pending
        assert m.get("chest-hrct").description == "ILD"
        assert "site_code: S014" in p.read_text()          # comment/keys kept

    def test_appends_when_protocols_key_absent(self, tmp_path):
        p = _write(tmp_path, "site_code: S014\n")
        slug = append_stub(p, protocol_name="Head trauma")
        assert slug == "head-trauma"
        assert ProtocolMap.load(p).get("head-trauma") is not None

    def test_idempotent(self, tmp_path):
        p = tmp_path / "rhythm_protocols.yaml"
        assert append_stub(p, protocol_name="Head Trauma") == "head-trauma"
        first = p.read_text()
        assert append_stub(p, protocol_name="head trauma") is None   # same slug
        assert p.read_text() == first

    def test_unusable_name_returns_none(self, tmp_path):
        p = tmp_path / "rhythm_protocols.yaml"
        assert append_stub(p, protocol_name="   ") is None
        assert append_stub(p, protocol_name="///") is None
        assert not p.exists()

    def test_malformed_file_is_left_alone(self, tmp_path):
        p = _write(tmp_path, "protocols: oops\n")
        assert append_stub(p, protocol_name="Head trauma") is None
        assert p.read_text() == "protocols: oops\n"
