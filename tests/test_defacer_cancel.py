"""Ctrl-C / shutdown stops defacing: nnU-Net runs in a child process group
that Defacer.cancel_all() terminates (a worker thread could not be
interrupted), the interrupted series is left untouched, and StorageManager
cancels on stop()."""
import os
import sys
import threading
import time

import pytest

from pixieveil.processing.defacer import Defacer, DefacingCancelled
from pixieveil.storage.storage_manager import StorageManager

from .conftest import make_settings

# A child that starts a grandchild (like nnU-Net's worker pool), records its
# pid, then sleeps: cancelling must take down the whole group.
SLEEPER = (
    "import subprocess, sys, time\n"
    "g = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])\n"
    "open(sys.argv[1], 'w').write(str(g.pid))\n"
    "time.sleep(60)\n"
)


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    # A zombie still answers kill(0); check its state.
    try:
        with open(f"/proc/{pid}/stat") as f:
            return f.read().split(")")[-1].split()[0] != "Z"
    except OSError:
        return False


def test_cancel_terminates_running_process_group(tmp_path):
    pidfile = tmp_path / "grandchild.pid"
    result = {}

    def worker():
        try:
            Defacer._run_cancellable([sys.executable, "-c", SLEEPER, str(pidfile)],
                                     tmp_path / "log.txt", poll_s=0.1)
            result["outcome"] = "finished"
        except DefacingCancelled:
            result["outcome"] = "cancelled"

    t = threading.Thread(target=worker)
    t.start()
    for _ in range(100):
        if pidfile.exists() and pidfile.read_text():
            break
        time.sleep(0.05)
    grandchild = int(pidfile.read_text())
    assert _alive(grandchild)

    started = time.monotonic()
    assert Defacer.cancel_all() == 1
    t.join(timeout=10)
    assert not t.is_alive()
    assert result["outcome"] == "cancelled"
    assert time.monotonic() - started < 8
    for _ in range(50):
        if not _alive(grandchild):
            break
        time.sleep(0.1)
    assert not _alive(grandchild)
    assert not Defacer._procs


def test_cancelled_before_start_runs_nothing(tmp_path):
    Defacer.cancel_all()
    with pytest.raises(DefacingCancelled):
        Defacer._run_cancellable([sys.executable, "-c", "open('ran', 'w')"],
                                 tmp_path / "log.txt")
    assert not (tmp_path / "ran").exists()


def test_failure_reports_output(tmp_path):
    with pytest.raises(RuntimeError, match="status 3.*boom"):
        Defacer._run_cancellable(
            [sys.executable, "-c", "import sys; print('boom'); sys.exit(3)"],
            tmp_path / "log.txt", poll_s=0.05)


def test_deface_series_propagates_cancel_and_leaves_series(tmp_path, monkeypatch):
    series = tmp_path / "0001"
    series.mkdir()
    (series / "1.dcm").write_bytes(b"x")
    d = Defacer({"enabled": True, "keep_backup": False}, temp_path=tmp_path / "tmp")
    monkeypatch.setattr(d, "dicom_to_nifti", lambda src, out: str(tmp_path / "a.nii.gz"))

    def cancelled(*a, **k):
        raise DefacingCancelled("shutdown")
    monkeypatch.setattr(d, "_run_defacing_tool", cancelled)
    with pytest.raises(DefacingCancelled):
        d.deface_series(series, data_dir=tmp_path)
    assert (series / "1.dcm").read_bytes() == b"x"


def test_storage_manager_stop_cancels_and_start_resets(tmp_path):
    import asyncio

    async def go():
        sm = StorageManager(make_settings(tmp_path))
        await sm.start()
        assert not Defacer._cancel.is_set()
        await sm.stop()
        return Defacer._cancel.is_set()
    assert asyncio.run(go()) is True
