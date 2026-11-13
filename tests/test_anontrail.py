"""The anonymization trail maps pseudonyms back to identities: owner-only."""
import json
import stat


def test_anontrail_written_owner_only(storage_manager, tmp_path):
    storage_manager.anontrail_path = tmp_path / "log" / "anontrail.jsonl"
    storage_manager.anontrail_path.parent.mkdir()
    storage_manager.log_anonymization_mapping("1.2.3", "1.2.3.4", "PID1", "img1", 1, 1)
    storage_manager.log_anonymization_mapping("1.2.3", "1.2.3.4", "PID1", "img2", 1, 1)
    path = storage_manager.anontrail_path
    lines = path.read_text().splitlines()
    assert len(lines) == 2
    assert "img2" in lines[1]
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
