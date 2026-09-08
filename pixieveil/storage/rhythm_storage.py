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

``upload_study`` stops once ``complete/`` returns — the chunks are assembled
and RHYTHM has queued an ingest job, but the Repository Study ID is assigned
asynchronously afterwards (GDPR re-validation + Orthanc push). The caller
persists ``job_status_url`` and polls it later with ``check_job``; only a
terminal ``COMPLETE`` is success (``PARTIAL`` = some images rejected, detail
in ``error_report``).

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
from pixieveil.processing import rhythm_protocols

logger = logging.getLogger(__name__)

DEFAULT_CHUNK_SIZE = 10 * 1024 * 1024  # 10 MiB, matches RHYTHM's init default
_TERMINAL_STATUSES = {"COMPLETE", "PARTIAL", "FAILED"}


@dataclass
class RhythmUploadResult:
    """Outcome of an ``upload_study`` or ``check_job`` call.

    From ``upload_study``: ``ok`` means the chunks were assembled and an
    ingest job was queued (``job_id`` / ``job_status_url`` set) — not that
    ingest succeeded. ``session_id`` is set once the chunked session exists.

    From ``check_job``: ``ok`` is True only for a terminal ``COMPLETE``;
    ``status`` is the raw job state, ``error_report`` RHYTHM's per-image
    detail on ``PARTIAL`` / ``FAILED``.

    ``error`` is a short reason on any failure.
    """

    ok: bool
    status: str | None = None
    session_id: str | None = None
    job_id: str | None = None
    job_status_url: str | None = None
    error: str | None = None
    error_report: Any = None


class RhythmStorage:
    """Uploads a study ZIP to the RHYTHM CT upload platform (v2 batch API)."""

    def __init__(self, settings: Settings):
        self.settings = settings
        cfg = settings.storage.get("remote_storage", {}).get("rhythm", {})

        def _num(key: str, default: int) -> int:
            # `or default` would turn a configured 0 into the default.
            value = cfg.get(key)
            return int(value) if value is not None else default

        self.base_url: str | None = (cfg.get("base_url") or "").rstrip("/") or None
        self.auth_token: str | None = cfg.get("auth_token") or None
        self.site_code: str | None = cfg.get("site_code") or None
        self.chunk_size: int = max(1, _num("chunk_size", DEFAULT_CHUNK_SIZE))
        self.max_chunk_retries: int = _num("max_chunk_retries", 3)
        # A usable config needs all three: where to send, how to authenticate,
        # and the site code that every manifest item is stamped with.
        self.enabled: bool = bool(self.base_url and self.auth_token and self.site_code)

        # Local CT-protocol -> RHYTHM CTProtocol UUID map. Loaded here (not in
        # StorageManager) so all RHYTHM config resolution — including the
        # protocol-map/settings site_code cross-check — lives in one place.
        # A missing map is an empty map (every RHYTHM export then blocked
        # until it exists). A *malformed* map (bad UUID, duplicate key,
        # site_code disagreeing with settings) disables RHYTHM export
        # entirely with a loud ERROR — it must not stop the DICOM SCP or
        # dashboard from starting.
        self.protocol_map_path = Path(
            cfg.get("protocol_map") or rhythm_protocols.DEFAULT_PATH
        )
        try:
            self.protocol_map = rhythm_protocols.ProtocolMap.load(
                self.protocol_map_path, expected_site_code=self.site_code
            )
        except rhythm_protocols.ProtocolMapError:
            logger.error(
                "RHYTHM protocol map %s is invalid — RHYTHM export disabled",
                self.protocol_map_path, exc_info=True,
            )
            self.protocol_map = rhythm_protocols.ProtocolMap([])
            self.enabled = False

    def register_pending_protocol(
        self, protocol_name: str, *, description: str = ""
    ) -> str | None:
        """Append a placeholder entry for *protocol_name* to the protocol map
        file and reload the map so the new key appears in the dashboard's
        RHYTHM-protocol dropdown.

        Returns the new local key, or ``None`` when nothing was added
        (unusable name, key already present, unparseable file). The operator
        still has to paste the real ``CTProtocol`` UUID into the file — until
        then the stub resolves to no ``protocol_id`` and the study stays
        blocked. The reload never flips ``enabled``: on a reload error the
        previously loaded map is kept.
        """
        slug = rhythm_protocols.append_stub(
            self.protocol_map_path,
            protocol_name=protocol_name,
            description=description,
        )
        if slug is None:
            return None
        try:
            self.protocol_map = rhythm_protocols.ProtocolMap.load(
                self.protocol_map_path, expected_site_code=self.site_code
            )
        except rhythm_protocols.ProtocolMapError:
            logger.error(
                "Protocol map %s did not reload cleanly after appending '%s'",
                self.protocol_map_path, slug, exc_info=True,
            )
        return slug

    # -- public API --------------------------------------------------------

    async def upload_study(
        self,
        zip_path: Path,
        manifest_item: dict[str, Any],
        batch: str,
        *,
        file_hash: str | None = None,
    ) -> RhythmUploadResult:
        """Upload *zip_path* as one item of *batch*, tagged with *manifest_item*.

        *file_hash* is the SHA-256 of the whole ZIP; pass it if already
        computed (the caller sets the same value on ``manifest_item['sha256']``)
        to avoid hashing the file twice, otherwise it is computed here.

        Returns once ``complete/`` succeeds: ``ok=True`` with ``job_id`` /
        ``job_status_url`` means the job is queued, not that ingest passed —
        poll ``check_job`` for that. Never raises for an expected failure
        (misconfig, missing file, HTTP error, rejected manifest item) — those
        come back as ``RhythmUploadResult(ok=False, ...)``. Only a genuinely
        unexpected error propagates.
        """
        if not self.enabled:
            logger.warning("RHYTHM storage not configured")
            return RhythmUploadResult(ok=False, error="RHYTHM storage not configured")
        if not zip_path.exists():
            logger.error("RHYTHM upload: file not found: %s", zip_path)
            return RhythmUploadResult(ok=False, error=f"file not found: {zip_path}")

        try:
            if file_hash is None:
                file_hash = await asyncio.to_thread(sha256_file, zip_path)
            total_size = zip_path.stat().st_size
            headers = {"Authorization": f"Bearer {self.auth_token}"}

            async with aiohttp.ClientSession(headers=headers) as session:
                init, server_total_chunks = await self._init(
                    session, zip_path.name, total_size, file_hash, batch, manifest_item
                )
                if init.error:
                    return init

                sent = await self._send_chunks(
                    session, zip_path, init.session_id, server_total_chunks
                )
                if sent is not None:
                    sent.session_id = init.session_id
                    return sent

                completed = await self._complete(session, init.session_id, file_hash)
                completed.session_id = init.session_id
                return completed

        except Exception as e:  # noqa: BLE001 - reported, not swallowed
            logger.error("RHYTHM upload of %s failed: %s", zip_path, e, exc_info=True)
            return RhythmUploadResult(ok=False, error=str(e))

    async def check_job(self, job_status_url: str) -> RhythmUploadResult:
        """One poll of a queued ingest job's status URL.

        ``ok`` is True only for a terminal ``COMPLETE``. A non-terminal state
        (``PENDING`` / ``PROCESSING``) or a transient poll error comes back
        ``ok=False`` with ``status`` set to what was seen (or None) and no
        ``error`` for the merely-not-done-yet case — the caller re-polls
        later. ``PARTIAL`` / ``FAILED`` carry ``error`` + ``error_report``.
        """
        if not self.enabled:
            return RhythmUploadResult(ok=False, error="RHYTHM storage not configured")
        if not job_status_url:
            return RhythmUploadResult(ok=False, error="no job_status_url")

        headers = {"Authorization": f"Bearer {self.auth_token}"}
        url = f"{self.base_url}{job_status_url}"
        try:
            async with aiohttp.ClientSession(headers=headers) as session:
                async with session.get(url) as resp:
                    body = await _json(resp)
                    if resp.status != 200:
                        return RhythmUploadResult(
                            ok=False, error=f"job poll failed: HTTP {resp.status}"
                        )
        except Exception as e:  # noqa: BLE001 - reported, not swallowed
            logger.warning("RHYTHM job poll of %s failed: %s", url, e)
            return RhythmUploadResult(ok=False, error=str(e))

        status = body.get("status") if isinstance(body, dict) else None
        report = body.get("error_report") if isinstance(body, dict) else None
        if status not in _TERMINAL_STATUSES:
            return RhythmUploadResult(ok=False, status=status, error_report=report)
        if status == "COMPLETE":
            return RhythmUploadResult(ok=True, status=status, error_report=report)
        return RhythmUploadResult(
            ok=False, status=status, error=f"job ended {status}", error_report=report
        )

    # -- steps -----------------------------------------------------------

    async def _init(
        self,
        session: aiohttp.ClientSession,
        filename: str,
        total_size: int,
        file_hash: str,
        batch: str,
        manifest_item: dict[str, Any],
    ) -> tuple[RhythmUploadResult, int | None]:
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
                detail = _err_detail(body)
                return (
                    RhythmUploadResult(
                        ok=False,
                        error=f"init failed: HTTP {resp.status}"
                        + (f" ({detail})" if detail else ""),
                        error_report=body if isinstance(body, dict) else None,
                    ),
                    None,
                )
            session_id = body.get("session_id")
            total_chunks = body.get("total_chunks")
            logger.info(
                "RHYTHM init ok: session=%s total_chunks=%s", session_id, total_chunks
            )
            return (
                RhythmUploadResult(ok=True, session_id=session_id),
                int(total_chunks) if isinstance(total_chunks, int) else None,
            )

    async def _send_chunks(
        self, session: aiohttp.ClientSession, zip_path: Path, session_id: str,
        server_total_chunks: int | None = None,
    ) -> RhythmUploadResult | None:
        """Upload every chunk. Returns None on success, or a failure result."""
        total_size = zip_path.stat().st_size
        total_chunks = max(1, -(-total_size // self.chunk_size))  # ceil div
        # The server computed its own count from the chunk_size we sent it; a
        # disagreement means it clamped/overrode chunk_size, and pushing our
        # count would 409 at complete/ with an opaque message. Fail clearly.
        if server_total_chunks is not None and server_total_chunks != total_chunks:
            return RhythmUploadResult(
                ok=False,
                error=(
                    f"chunk-count disagreement: server expects "
                    f"{server_total_chunks}, we would send {total_chunks} "
                    f"(chunk_size {self.chunk_size}) — check "
                    f"remote_storage.rhythm.chunk_size"
                ),
            )
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
                detail = _err_detail(body)
                return RhythmUploadResult(
                    ok=False,
                    error=f"complete failed: HTTP {resp.status}"
                    + (f" ({detail})" if detail else ""),
                    error_report=body if isinstance(body, dict) else None,
                )
            job_id = body.get("job_id")
            job_status_url = body.get("job_status_url")
            logger.info("RHYTHM complete ok: job=%s", job_id)
            return RhythmUploadResult(
                ok=True, job_id=job_id, job_status_url=job_status_url
            )


# -- helpers -----------------------------------------------------------------


def _err_detail(body: Any) -> str | None:
    """A short human string from a RHYTHM error body — its ``error`` and/or
    ``code`` — for appending to a ``HTTP <n>`` message."""
    if not isinstance(body, dict):
        return None
    parts = []
    for key in ("code", "error"):
        val = body.get(key)
        if isinstance(val, str) and val.strip():
            parts.append(val.strip())
    return "; ".join(parts) or None


def sha256_file(path: Path) -> str:
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
