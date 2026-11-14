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
import re
import hashlib
import logging
import time
from urllib.parse import quote, urlencode, urlparse
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable

import aiohttp

from pixieveil.config import Settings
from pixieveil.processing import protocol_catalog

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

        # Scanners & protocols catalog (local protocol key -> RHYTHM
        # CTProtocol UUID, plus matching data the exam extractor uses).
        # Loaded here so all RHYTHM config resolution — including the
        # catalog/settings site_code cross-check — lives in one place;
        # StorageManager shares this instance with ExamExtractor and the
        # /protocols page. A missing catalog is empty (built from the legacy
        # lookup files on first start if those exist). An *invalid* one
        # disables RHYTHM export with a loud ERROR — it must not stop the
        # DICOM SCP or dashboard from starting, and the /protocols page can
        # then be used to write a valid one.
        self.protocol_map_path = Path(
            settings.storage.get("protocols_file")
            or cfg.get("protocol_map")
            or protocol_catalog.DEFAULT_PATH
        )
        try:
            self.protocol_map = protocol_catalog.open_catalog(
                self.protocol_map_path, expected_site_code=self.site_code
            )
        except (protocol_catalog.CatalogError, OSError):
            logger.error(
                "Protocol catalog %s is invalid — RHYTHM export disabled until it "
                "is fixed (the /protocols page can write a valid one)",
                self.protocol_map_path, exc_info=True,
            )
            self.protocol_map = protocol_catalog.ProtocolCatalog(
                self.protocol_map_path, expected_site_code=self.site_code, scanners=[]
            )
            self.enabled = False

    # -- public API --------------------------------------------------------

    async def upload_study(
        self,
        zip_path: Path,
        manifest_item: dict[str, Any],
        batch: str,
        *,
        file_hash: str | None = None,
        progress: Callable[[int, int], None] | None = None,
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
        unexpected error propagates. *progress*, if given, is called as
        ``progress(chunks_sent, total_chunks)`` after each chunk.
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
                    session, zip_path, init.session_id, server_total_chunks,
                    progress=progress,
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

    async def fetch_study_ids(self, study_uid: str) -> dict[str, Any] | None:
        """RHYTHM's identifiers for an ingested study, looked up by the
        (anonymized) StudyInstanceUID we shipped — RHYTHM keys its
        ``StudyMapping`` on that UID unchanged.

        Returns ``{"repository_study_id", "orthanc_study_id"}`` (either may
        be None), or None if the study can't be read (not ingested yet, not
        visible to this token, RHYTHM unreachable). The Repository Study ID
        is RHYTHM's own ID for the study (``Patient.pseudo_id`` ==
        ``CTExamination.repository_study_id``) — the one a data-erasure
        request to RHYTHM has to name.
        """
        if not self.enabled or not study_uid:
            return None
        if not _UID_RE.fullmatch(study_uid):
            # Sender-controlled under a profile that KEEPs StudyInstanceUID:
            # never let it add path segments to an authenticated request.
            logger.warning("RHYTHM study lookup skipped: %r is not a DICOM UID", study_uid)
            return None
        try:
            status, body = await _send_json(
                "GET", self.base_url, self.auth_token, f"{STUDIES_PATH}{study_uid}/"
            )
        except RhythmApiError:
            return None
        if status != 200 or not isinstance(body, dict):
            logger.warning("RHYTHM study lookup for %s: HTTP %s", study_uid, status)
            return None
        patient = body.get("patient") if isinstance(body.get("patient"), dict) else {}
        return {
            "repository_study_id": body.get("patient_pseudo_id") or patient.get("pseudo_id"),
            "orthanc_study_id": body.get("orthanc_study_id"),
        }

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
        progress: Callable[[int, int], None] | None = None,
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
                if progress is not None:
                    progress(chunk_number + 1, total_chunks)
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


PROBE_PATH = "/api/v1/protocols-api/"
PROFILES_PATH = "/api/v1/scanner-profiles/"
PROTOCOLS_PATH = "/api/v1/protocols-api/"
_UID_RE = re.compile(r"[0-9]+(\.[0-9]+)*")  # DICOM UI: digits, single dots
STUDIES_PATH = "/api/v1/studies/"
PAGE_SIZE = 100          # RHYTHM's max_page_size


class RhythmApiError(Exception):
    """A RHYTHM read failed: *status* is the HTTP status (None = unreachable)."""

    def __init__(self, status: int | None, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


async def fetch_all(base_url: str, auth_token: str, path: str, *,
                    max_pages: int = 50, timeout_s: float = 15.0) -> list[dict]:
    """GET every page of a RHYTHM list endpoint (``count``/``next``/``results``,
    or a plain list). ``next`` is followed only on *base_url*'s own host so a
    response can't steer the token elsewhere. Raises :class:`RhythmApiError`."""
    base = base_url.rstrip("/")
    host = urlparse(base).netloc
    sep = "&" if "?" in path else "?"
    url: str | None = f"{base}{path}{sep}page_size={PAGE_SIZE}"
    out: list[dict] = []
    headers = {"Authorization": f"Bearer {auth_token}"}
    try:
        async with aiohttp.ClientSession(
            headers=headers, timeout=aiohttp.ClientTimeout(total=timeout_s)
        ) as session:
            for _page in range(max_pages):
                if url is None:
                    break
                async with session.get(url) as resp:
                    body = await _json(resp)
                    status = resp.status
                if status in (401, 403):
                    raise RhythmApiError(status, f"Reached RHYTHM, but the token was "
                                                 f"rejected (HTTP {status})")
                if status != 200:
                    raise RhythmApiError(status, f"RHYTHM answered HTTP {status} for {path}")
                if isinstance(body, list):
                    out.extend(i for i in body if isinstance(i, dict))
                    break
                items = body.get("results") if isinstance(body, dict) else None
                if not isinstance(items, list):
                    raise RhythmApiError(status, f"Unexpected answer from {path}")
                out.extend(i for i in items if isinstance(i, dict))
                nxt = body.get("next")
                if nxt and urlparse(nxt).netloc not in ("", host):
                    raise RhythmApiError(status, f"RHYTHM pointed to another host ({nxt})")
                url = (f"{base}{nxt}" if nxt and nxt.startswith("/") else nxt) or None
            else:
                logger.warning("RHYTHM %s: stopped after %d pages", path, max_pages)
    except RhythmApiError:
        raise
    except Exception as e:  # noqa: BLE001 - reported to the operator
        logger.warning("RHYTHM read of %s%s failed: %s", base, path, e)
        raise RhythmApiError(None, f"Cannot reach {base_url}: "
                                   f"{e.__class__.__name__}: {e}") from None
    return out


async def fetch_site_catalog(base_url: str, auth_token: str) -> dict[str, list[dict]]:
    """The site's scanner profiles and protocols from RHYTHM (all pages)."""
    profiles = await fetch_all(base_url, auth_token, PROFILES_PATH)
    protocols = await fetch_all(base_url, auth_token, PROTOCOLS_PATH)
    return {"profiles": profiles, "protocols": protocols}


async def probe_connection(base_url: str, auth_token: str,
                           known_uuids: Iterable[str] = (),
                           timeout_s: float = 10.0) -> dict[str, Any]:
    """Check that *base_url* is reachable and *auth_token* is accepted, by
    reading every page of ``/api/v1/protocols-api/``.

    Returns ``{"ok", "status", "message"}`` plus ``protocols`` (how many
    RHYTHM lists) and ``found``/``missing`` among *known_uuids* (the
    catalog's UUIDs). Never raises; never logs the token.
    """
    try:
        items = await fetch_all(base_url, auth_token, PROBE_PATH, timeout_s=timeout_s)
    except RhythmApiError as e:
        if e.status in (401, 403):
            return {"ok": False, "status": e.status, "message": e.message}
        if e.status is None:
            return {"ok": False, "status": None, "message": e.message}
        return {"ok": False, "status": e.status,
                "message": f"Reached the server, but it answered HTTP {e.status}"}
    ids = {str(i.get(k)).lower() for i in items for k in ("id", "uuid") if i.get(k)}
    known = sorted({u.lower() for u in known_uuids if u})
    return {"ok": True, "status": 200, "message": "Connected; the token is accepted",
            "protocols": len(items),
            "found": [u for u in known if u in ids],
            "missing": [u for u in known if u not in ids],
            "partial": False}


# -- obtaining a token: RHYTHM's own login (+ 2FA) ---------------------------
#
# RHYTHM hands out its API token only at the end of an interactive login:
#   POST /api/v1/auth/login/       {username, password}
#       -> {token, ...}                 (2FA not required)
#       -> {requires_2fa: true}         (enter the TOTP code next)
#       -> {requires_2fa_setup: true}   (2FA not enrolled yet)
#   POST /api/v1/auth/verify-2fa/  {code}   (same Django session cookie)
#       -> {token, username, ...}
# The cookie jar carries that session between the two calls.

LOGIN_PATH = "/api/v1/auth/login/"
VERIFY_2FA_PATH = "/api/v1/auth/verify-2fa/"


async def _auth_post(base_url: str, path: str, payload: dict, jar: aiohttp.CookieJar,
                     timeout_s: float) -> dict[str, Any]:
    url = f"{base_url.rstrip('/')}{path}"
    try:
        async with aiohttp.ClientSession(
            cookie_jar=jar, timeout=aiohttp.ClientTimeout(total=timeout_s)
        ) as session:
            async with session.post(url, json=payload) as resp:
                body = await _json(resp)
                status = resp.status
    except Exception as e:  # noqa: BLE001 - reported to the operator
        logger.warning("RHYTHM sign-in request to %s failed: %s", url, e.__class__.__name__)
        return {"ok": False, "message": f"Cannot reach {base_url}: {e.__class__.__name__}: {e}"}
    if not isinstance(body, dict):
        body = {}
    if status == 200 and body.get("token"):
        return {"ok": True, "token": str(body["token"]),
                "username": body.get("username"), "jar": jar}
    if status == 200 and body.get("requires_2fa"):
        return {"ok": False, "needs_2fa": True, "jar": jar,
                "message": "Enter the 6-digit code from your authenticator app"}
    if status == 200 and body.get("requires_2fa_setup"):
        return {"ok": False, "message": "Two-factor authentication is not set up for this "
                                        "account yet: sign in once on RHYTHM's own login page "
                                        "to enrol, then try again"}
    err = body.get("error") or body.get("detail") or body.get("non_field_errors")
    if isinstance(err, list):
        err = " ".join(str(e) for e in err)
    return {"ok": False, "status": status,
            "message": str(err) if err else f"RHYTHM answered HTTP {status}"}


async def rhythm_login(base_url: str, username: str, password: str,
                       timeout_s: float = 15.0) -> dict[str, Any]:
    """Step 1 of RHYTHM's login. Returns ``{"ok", "token"?, "needs_2fa"?,
    "jar"?, "message"?}``; keep ``jar`` for :func:`rhythm_verify_2fa`.
    The password is used for this one request only and never logged."""
    jar = aiohttp.CookieJar(unsafe=True)   # unsafe: allow cookies from IP hosts
    return await _auth_post(base_url, LOGIN_PATH,
                            {"username": username, "password": password}, jar, timeout_s)


async def rhythm_verify_2fa(base_url: str, jar: aiohttp.CookieJar, code: str,
                            timeout_s: float = 15.0) -> dict[str, Any]:
    """Step 2: send the TOTP *code* in the session started by rhythm_login."""
    return await _auth_post(base_url, VERIFY_2FA_PATH, {"code": code}, jar, timeout_s)


# -- protocol choices + creating a CTProtocol --------------------------------

CHOICES_PATH = "/api/v1/protocol-choices/"
CHOICES_TTL_S = 600.0
_choices_cache: dict[str, tuple[float, list[dict]]] = {}


async def fetch_choices(base_url: str, auth_token: str) -> list[dict]:
    """RHYTHM's protocol option categories (``{key, label, options[...]}``),
    cached per base URL for CHOICES_TTL_S. Raises RhythmApiError."""
    key = base_url.rstrip("/")
    hit = _choices_cache.get(key)
    if hit and hit[0] > time.time():
        return hit[1]
    cats = await fetch_all(base_url, auth_token, CHOICES_PATH)
    _choices_cache[key] = (time.time() + CHOICES_TTL_S, cats)
    return cats


async def _send_json(method: str, base_url: str, auth_token: str, path: str,
                     payload: dict | None = None, timeout_s: float = 20.0) -> tuple[int, Any]:
    url = f"{base_url.rstrip('/')}{path}"
    headers = {"Authorization": f"Bearer {auth_token}"}
    try:
        async with aiohttp.ClientSession(
            headers=headers, timeout=aiohttp.ClientTimeout(total=timeout_s)
        ) as session:
            async with session.request(method, url, json=payload) as resp:
                return resp.status, await _json(resp)
    except Exception as e:  # noqa: BLE001 - reported to the operator
        logger.warning("RHYTHM %s %s failed: %s", method, url, e)
        raise RhythmApiError(None, f"Cannot reach {base_url}: "
                                   f"{e.__class__.__name__}: {e}") from None


def _field_errors(body: Any) -> dict[str, str]:
    if not isinstance(body, dict):
        return {}
    out = {}
    for k, v in body.items():
        out[str(k)] = " ".join(str(x) for x in v) if isinstance(v, list) else str(v)
    return out


async def create_protocol(base_url: str, auth_token: str, payload: dict) -> dict:
    """POST a CTProtocol; returns the created record (with its ``id``).

    RHYTHM's 400 field errors are raised as RhythmApiError with ``.errors``.
    """
    status, body = await _send_json("POST", base_url, auth_token, PROTOCOLS_PATH, payload)
    if status in (200, 201) and isinstance(body, dict) and body.get("id"):
        return body
    err = RhythmApiError(status, f"RHYTHM refused the protocol (HTTP {status})"
                         if status not in (401, 403) else
                         f"RHYTHM rejected the token (HTTP {status})")
    err.errors = _field_errors(body) if status == 400 else {}  # type: ignore[attr-defined]
    raise err


async def get_protocol(base_url: str, auth_token: str, protocol_id: str) -> dict | None:
    """Read one CTProtocol back (None if it can't be read)."""
    try:
        status, body = await _send_json("GET", base_url, auth_token,
                                        f"{PROTOCOLS_PATH}{quote(str(protocol_id), safe='')}/")
    except RhythmApiError:
        return None
    return body if status == 200 and isinstance(body, dict) else None


async def find_protocols(base_url: str, auth_token: str, scanner_id: str,
                         protocol_type: str | None = None) -> list[dict]:
    """The site's protocols on one scanner profile (optionally one type)."""
    params = {"scanner": scanner_id}
    if protocol_type:
        params["protocol_type"] = protocol_type
    q = "?" + urlencode(params)
    items = await fetch_all(base_url, auth_token, PROTOCOLS_PATH + q)
    return [p for p in items if str(p.get("scanner")) == str(scanner_id)
            and (not protocol_type or p.get("protocol_type") == protocol_type)]


# -- scanner catalogue + creating a scanner profile ---------------------------

MANUFACTURERS_PATH = "/api/v1/manufacturers/"
SCANNER_MODELS_PATH = "/api/v1/scanner-models/"
_catalogue_cache: dict[str, tuple[float, dict]] = {}


async def fetch_catalogue(base_url: str, auth_token: str) -> dict[str, list[dict]]:
    """RHYTHM's scanner catalogue ``{"manufacturers": [...], "models": [...]}``
    (admin-maintained; read-only over the API), cached like the choices."""
    key = base_url.rstrip("/")
    hit = _catalogue_cache.get(key)
    if hit and hit[0] > time.time():
        return hit[1]
    out = {"manufacturers": await fetch_all(base_url, auth_token, MANUFACTURERS_PATH),
           "models": await fetch_all(base_url, auth_token, SCANNER_MODELS_PATH)}
    _catalogue_cache[key] = (time.time() + CHOICES_TTL_S, out)
    return out


async def create_scanner_profile(base_url: str, auth_token: str, payload: dict) -> dict:
    """POST a CTScannerProfile for the token user's site; returns it (with
    ``id``). RHYTHM's 400 field errors are raised as RhythmApiError.errors."""
    status, body = await _send_json("POST", base_url, auth_token, PROFILES_PATH, payload)
    if status in (200, 201) and isinstance(body, dict) and body.get("id"):
        return body
    err = RhythmApiError(status, f"RHYTHM refused the scanner (HTTP {status})"
                         if status not in (401, 403) else
                         f"RHYTHM rejected the token (HTTP {status})")
    err.errors = _field_errors(body) if status == 400 else {}  # type: ignore[attr-defined]
    raise err
