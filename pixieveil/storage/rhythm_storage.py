"""
RHYTHM upload transport.

Drives one studyset ZIP through RHYTHM's v2 resumable chunked-upload API:

    POST {base}/api/v1/uploads/chunked/init/      {filename,total_size,chunk_size,
                                                   file_hash,batch,manifest_item}
      -> 201 {session_id, total_chunks, ...}
    POST {base}/api/v1/uploads/chunked/{sid}/chunk/?chunk_number=&chunk_hash=
      (raw chunk bytes) -> 202 {progress_percent, needs_reupload, ...}
    POST {base}/api/v1/uploads/chunked/{sid}/complete/  {file_hash}
      -> 200 {job_id, job_status_url}
    GET  {base}{job_status_url}
      -> {status: PENDING|PROCESSING|COMPLETE|PARTIAL|FAILED, error_report, ...}

RHYTHM assigns the Repository Study ID asynchronously, so this polls the job
URL until it reaches a terminal state. Only ``COMPLETE`` is success:
``PARTIAL`` means some images were rejected (details in ``error_report``).

There is deliberately no resume path here — a ``session_id`` is returned in
the result so a caller can persist it, but a re-run starts a fresh session.
"""

import asyncio
import hashlib
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import aiohttp

from pixieveil.config import Settings

logger = logging.getLogger(__name__)

DEFAULT_CHUNK_SIZE = 10 * 1024 * 1024  # 10 MiB, matches RHYTHM's init default
_TERMINAL_STATUSES = {"COMPLETE", "PARTIAL", "FAILED"}


@dataclass
class RhythmUploadResult:
    """Outcome of one ``upload_study`` call.

    ``ok`` is True only on a terminal ``COMPLETE``. ``session_id`` is set once
    the chunked session exists (persist it for troubleshooting); ``job_id`` /
    ``job_status_url`` once ``complete/`` returns. ``error`` is a short reason
    on any failure; ``error_report`` carries RHYTHM's structured per-item /
    per-image detail when it supplied one.
    """

    ok: bool
    status: str | None = None
    session_id: str | None = None
    job_id: str | None = None
    job_status_url: str | None = None
    error: str | None = None
    error_report: Any = None


@dataclass
class _PollConfig:
    interval_s: float = 5.0
    timeout_s: float = 1800.0


class RhythmStorage:
    """Uploads a study ZIP to the RHYTHM CT upload platform (v2 batch API)."""

    def __init__(self, settings: Settings):
        self.settings = settings
        cfg = settings.storage.get("remote_storage", {}).get("rhythm", {})

        def _num(key: str, default: float) -> float:
            # `or default` would turn a configured 0 into the default — which
            # for poll_interval_s / poll_timeout_s matters (0 is meaningful).
            value = cfg.get(key)
            return type(default)(value) if value is not None else default

        self.base_url: str | None = (cfg.get("base_url") or "").rstrip("/") or None
        self.auth_token: str | None = cfg.get("auth_token") or None
        self.site_code: str | None = cfg.get("site_code") or None
        self.chunk_size: int = max(1, int(_num("chunk_size", DEFAULT_CHUNK_SIZE)))
        self.max_chunk_retries: int = int(_num("max_chunk_retries", 3))
        self._poll = _PollConfig(
            interval_s=_num("poll_interval_s", _PollConfig.interval_s),
            timeout_s=_num("poll_timeout_s", _PollConfig.timeout_s),
        )
        # A usable config needs all three: where to send, how to authenticate,
        # and the site code that every manifest item is stamped with.
        self.enabled: bool = bool(self.base_url and self.auth_token and self.site_code)

    # -- public API --------------------------------------------------------

    async def upload_study(
        self, zip_path: Path, manifest_item: dict[str, Any], batch: str
    ) -> RhythmUploadResult:
        """Upload *zip_path* as one item of *batch*, tagged with *manifest_item*.

        Never raises for an expected failure (misconfig, missing file, HTTP
        error, rejected manifest item, job FAILED/PARTIAL) — those come back as
        ``RhythmUploadResult(ok=False, ...)``. Only a genuinely unexpected
        error propagates.
        """
        if not self.enabled:
            logger.warning("RHYTHM storage not configured")
            return RhythmUploadResult(ok=False, error="RHYTHM storage not configured")
        if not zip_path.exists():
            logger.error("RHYTHM upload: file not found: %s", zip_path)
            return RhythmUploadResult(ok=False, error=f"file not found: {zip_path}")

        try:
            file_hash = await asyncio.to_thread(_sha256_file, zip_path)
            total_size = zip_path.stat().st_size
            headers = {"Authorization": f"Bearer {self.auth_token}"}

            async with aiohttp.ClientSession(headers=headers) as session:
                init = await self._init(
                    session, zip_path.name, total_size, file_hash, batch, manifest_item
                )
                if init.error:
                    return init

                sent = await self._send_chunks(session, zip_path, init.session_id)
                if sent is not None:
                    sent.session_id = init.session_id
                    return sent

                completed = await self._complete(session, init.session_id, file_hash)
                if completed.error:
                    return completed

                result = await self._poll_job(session, completed.job_status_url)
                result.session_id = init.session_id
                result.job_id = completed.job_id
                result.job_status_url = completed.job_status_url
                return result

        except Exception as e:  # noqa: BLE001 - reported, not swallowed
            logger.error("RHYTHM upload of %s failed: %s", zip_path, e, exc_info=True)
            return RhythmUploadResult(ok=False, error=str(e))

    # -- steps -----------------------------------------------------------

    async def _init(
        self,
        session: aiohttp.ClientSession,
        filename: str,
        total_size: int,
        file_hash: str,
        batch: str,
        manifest_item: dict[str, Any],
    ) -> RhythmUploadResult:
        payload = {
            "filename": filename,
            "total_size": total_size,
            "chunk_size": self.chunk_size,
            "file_hash": file_hash,
            "batch": batch,
            "manifest_item": manifest_item,
        }
        async with session.post(
            f"{self.base_url}/api/v1/uploads/chunked/init/", json=payload
        ) as resp:
            body = await _json(resp)
            if resp.status != 201:
                logger.error("RHYTHM init failed: %s %s", resp.status, body)
                return RhythmUploadResult(
                    ok=False,
                    error=f"init failed: HTTP {resp.status}",
                    error_report=body.get("error") if isinstance(body, dict) else body,
                )
            session_id = body.get("session_id")
            total_chunks = body.get("total_chunks")
            logger.info(
                "RHYTHM init ok: session=%s total_chunks=%s", session_id, total_chunks
            )
            # session_id doubles as the "chunks still to send" marker for
            # _send_chunks; total_chunks is recomputed there from the file.
            return RhythmUploadResult(ok=True, session_id=session_id)

    async def _send_chunks(
        self, session: aiohttp.ClientSession, zip_path: Path, session_id: str
    ) -> RhythmUploadResult | None:
        """Upload every chunk. Returns None on success, or a failure result."""
        total_size = zip_path.stat().st_size
        total_chunks = max(1, -(-total_size // self.chunk_size))  # ceil div
        url = f"{self.base_url}/api/v1/uploads/chunked/{session_id}/chunk/"

        with open(zip_path, "rb") as fh:
            for chunk_number in range(total_chunks):
                data = await asyncio.to_thread(fh.read, self.chunk_size)
                if not data:
                    break
                chunk_hash = hashlib.sha256(data).hexdigest()
                err = await self._send_one_chunk(
                    session, url, chunk_number, chunk_hash, data
                )
                if err is not None:
                    return err
        logger.info("RHYTHM: all %d chunk(s) uploaded for %s", total_chunks, session_id)
        return None

    async def _send_one_chunk(
        self,
        session: aiohttp.ClientSession,
        url: str,
        chunk_number: int,
        chunk_hash: str,
        data: bytes,
    ) -> RhythmUploadResult | None:
        params = {"chunk_number": chunk_number, "chunk_hash": chunk_hash}
        for attempt in range(1, self.max_chunk_retries + 1):
            try:
                async with session.post(url, params=params, data=data) as resp:
                    body = await _json(resp)
                    if resp.status in (201, 202) and not (
                        isinstance(body, dict) and body.get("needs_reupload")
                    ):
                        return None
                    reason = f"HTTP {resp.status}"
                    if isinstance(body, dict) and body.get("needs_reupload"):
                        reason = "server flagged needs_reupload"
            except aiohttp.ClientError as e:
                reason = str(e)
            logger.warning(
                "RHYTHM chunk %d attempt %d/%d failed: %s",
                chunk_number,
                attempt,
                self.max_chunk_retries,
                reason,
            )
        return RhythmUploadResult(
            ok=False,
            error=f"chunk {chunk_number} failed after {self.max_chunk_retries} attempts",
        )

    async def _complete(
        self, session: aiohttp.ClientSession, session_id: str, file_hash: str
    ) -> RhythmUploadResult:
        url = f"{self.base_url}/api/v1/uploads/chunked/{session_id}/complete/"
        async with session.post(url, json={"file_hash": file_hash}) as resp:
            body = await _json(resp)
            if resp.status != 200:
                logger.error("RHYTHM complete failed: %s %s", resp.status, body)
                return RhythmUploadResult(
                    ok=False, error=f"complete failed: HTTP {resp.status}"
                )
            job_id = body.get("job_id")
            job_status_url = body.get("job_status_url")
            logger.info("RHYTHM complete ok: job=%s", job_id)
            return RhythmUploadResult(
                ok=True, job_id=job_id, job_status_url=job_status_url
            )

    async def _poll_job(
        self, session: aiohttp.ClientSession, job_status_url: str | None
    ) -> RhythmUploadResult:
        if not job_status_url:
            return RhythmUploadResult(ok=False, error="no job_status_url returned")
        url = f"{self.base_url}{job_status_url}"
        loop = asyncio.get_event_loop()
        deadline = loop.time() + self._poll.timeout_s

        while True:
            async with session.get(url) as resp:
                body = await _json(resp)
                if resp.status != 200:
                    return RhythmUploadResult(
                        ok=False, error=f"job poll failed: HTTP {resp.status}"
                    )
            status = body.get("status") if isinstance(body, dict) else None
            if status in _TERMINAL_STATUSES:
                report = body.get("error_report") if isinstance(body, dict) else None
                if status == "COMPLETE":
                    logger.info("RHYTHM job COMPLETE")
                    return RhythmUploadResult(
                        ok=True, status=status, error_report=report
                    )
                logger.error("RHYTHM job terminal state %s: %s", status, report)
                return RhythmUploadResult(
                    ok=False,
                    status=status,
                    error=f"job ended {status}",
                    error_report=report,
                )
            if loop.time() >= deadline:
                return RhythmUploadResult(
                    ok=False,
                    status=status,
                    error=f"job still {status or 'pending'} after "
                    f"{self._poll.timeout_s:.0f}s",
                )
            await asyncio.sleep(self._poll.interval_s)


# -- helpers -----------------------------------------------------------------


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


async def _json(resp: aiohttp.ClientResponse) -> Any:
    """Best-effort JSON body; returns {} when there's nothing parseable so
    callers can always ``.get()`` without a type guard."""
    try:
        return await resp.json(content_type=None)
    except Exception:
        return {}
