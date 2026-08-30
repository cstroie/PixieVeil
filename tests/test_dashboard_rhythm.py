"""Unit tests for the /studies dashboard's RHYTHM wiring: the upload_rhythm
action route, the rhythm-batch endpoint's body validation, and the RHYTHM
capability flags in the list response.

The dashboard has no aiohttp test harness elsewhere in the suite; following
the house style (test_dicom_server.py) these call the handler coroutines
directly with a tiny fake request + fake StorageManager and inspect the
returned web.Response.
"""

import asyncio
import json

from pixieveil.config import Settings
from pixieveil.dashboard.server import Dashboard


class _Cap:
    def __init__(self, enabled):
        self.enabled = enabled


class FakeSM:
    def __init__(self, *, rhythm=True, profile="gdpr"):
        self.dicom_storage = _Cap(False)
        self.remote_storage = _Cap(False)
        self.rhythm_storage = _Cap(rhythm)
        self.settings = Settings(anonymization={"profile": profile})
        self.calls: list = []

    def list_studies_summary_sync(self):
        return [{"study_number": 1, "status": "ready"}]

    async def manual_upload_rhythm(self, n, *, batch=None):
        self.calls.append(("single", n, batch))
        return {"ok": True, "status": "uploaded", "batch": batch or "b", "job_id": "j"}

    async def manual_upload_rhythm_batch(self, numbers):
        self.calls.append(("batch", tuple(numbers)))
        return {"ok": True, "batch": "S001-x",
                "results": {str(x): {"ok": True} for x in numbers}}

    # unused by these tests but referenced in the action method map
    async def manual_send_dicom(self, n):
        return {"ok": False}

    async def manual_upload_http(self, n):
        return {"ok": False}

    async def manual_reextract(self, n):
        return {"ok": False}


class FakeRequest:
    def __init__(self, sm, *, body=None, body_raises=False, match_info=None):
        self.app = {"storage_manager": sm}
        self._body = body
        self._body_raises = body_raises
        self.match_info = match_info or {}
        self.headers = {}
        self.method = "POST"

    async def json(self):
        if self._body_raises:
            raise ValueError("bad json")
        return self._body


def _dash(sm):
    return Dashboard(Settings(), sm)


def _payload(resp):
    return resp.status, json.loads(resp.body)


class TestRhythmBatchEndpoint:
    def test_valid_body_delegates_to_batch(self):
        sm = FakeSM()
        dash = _dash(sm)
        req = FakeRequest(sm, body={"study_numbers": [1, 2, 3]})
        status, data = _payload(asyncio.run(dash.handle_rhythm_batch(req)))
        assert status == 200
        assert data["ok"] is True
        assert data["batch"] == "S001-x"
        assert sm.calls == [("batch", (1, 2, 3))]

    def test_missing_key_is_400(self):
        sm = FakeSM()
        status, data = _payload(
            asyncio.run(_dash(sm).handle_rhythm_batch(FakeRequest(sm, body={})))
        )
        assert status == 400
        assert not sm.calls

    def test_non_int_entries_rejected(self):
        sm = FakeSM()
        req = FakeRequest(sm, body={"study_numbers": [1, "2"]})
        status, _ = _payload(asyncio.run(_dash(sm).handle_rhythm_batch(req)))
        assert status == 400
        assert not sm.calls

    def test_not_a_list_rejected(self):
        sm = FakeSM()
        req = FakeRequest(sm, body={"study_numbers": 5})
        status, _ = _payload(asyncio.run(_dash(sm).handle_rhythm_batch(req)))
        assert status == 400

    def test_invalid_json_is_400(self):
        sm = FakeSM()
        req = FakeRequest(sm, body_raises=True)
        status, _ = _payload(asyncio.run(_dash(sm).handle_rhythm_batch(req)))
        assert status == 400


class TestUploadRhythmAction:
    def test_action_routes_to_manual_upload_rhythm(self):
        sm = FakeSM()
        req = FakeRequest(sm, match_info={"n": "7", "action": "upload_rhythm"})
        status, data = _payload(asyncio.run(_dash(sm).handle_study_action(req)))
        assert status == 200
        assert data["ok"] is True
        assert sm.calls == [("single", 7, None)]

    def test_unknown_action_still_400(self):
        sm = FakeSM()
        req = FakeRequest(sm, match_info={"n": "7", "action": "nope"})
        status, _ = _payload(asyncio.run(_dash(sm).handle_study_action(req)))
        assert status == 400


class TestListCapabilities:
    def test_rhythm_flag_and_profile_exposed(self):
        sm = FakeSM(rhythm=True, profile="gdpr")
        req = FakeRequest(sm)
        status, data = _payload(asyncio.run(_dash(sm).handle_list_studies(req)))
        assert status == 200
        assert data["capabilities"]["rhythm"] is True
        assert data["capabilities"]["anonymization_profile"] == "gdpr"

    def test_rhythm_disabled_and_non_gdpr_profile(self):
        sm = FakeSM(rhythm=False, profile="research")
        req = FakeRequest(sm)
        _status, data = _payload(asyncio.run(_dash(sm).handle_list_studies(req)))
        assert data["capabilities"]["rhythm"] is False
        assert data["capabilities"]["anonymization_profile"] == "research"
