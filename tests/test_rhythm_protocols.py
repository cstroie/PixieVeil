"""Unit tests for the local-protocol -> RHYTHM CTProtocol UUID map loader."""

import pytest

from pixieveil.processing.rhythm_protocols import ProtocolMap, ProtocolMapError


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
