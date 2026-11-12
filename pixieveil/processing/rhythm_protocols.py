"""
Local CT-protocol → RHYTHM ``CTProtocol`` UUID map.

RHYTHM v2 manifest items name one ``protocol_id`` — the UUID of a
``CTProtocol`` registered in the RHYTHM app (Protocol Records page,
``/protocols/records/``). The server derives anatomical region, clinical
indication, contrast, protocol type, examination group and scanner from that
protocol, so none of those are repeated in the manifest.

``config/rhythm_protocols.yaml`` records, per locally-defined protocol, the
UUID RHYTHM assigned when it was registered. The operator picks one of these
for a study on the ``/studies`` page; ``build_manifest_item`` puts its
``uuid`` into ``protocol_id``. The human-facing definition of each protocol
(what to register upstream, and why) lives in
``integrations/rhythm/*_protocol.yaml``.

File shape::

    site_code: S014          # optional; if set it must equal
                             # storage.remote_storage.rhythm.site_code in
                             # settings.yaml (case-insensitive) or startup fails
    protocols:
      - id: head-trauma-routine
        name: "Paediatric head — trauma, routine dose"
        description: "Non-contrast head CT, trauma indication"
        uuid: 7076d1dc-100a-4bcd-a8d7-2ef3672b1e3f

A missing file is not an error — it yields an empty map, and every RHYTHM
export is then blocked on "protocol not selected / unknown" until the file
exists. A malformed file *is* an error (raised at load): a silently-dropped
protocol would surface as a rejected RHYTHM job minutes later.

**Placeholder UUID** — an entry whose ``uuid`` is the all-zeros
``NIL_UUID`` still loads (so the dashboard dropdown can offer it) but
``uuid_for()`` resolves it to ``None``, so the manifest stays blocked
rather than uploading a study with a bogus ``protocol_id`` that RHYTHM
would reject minutes later at ingest. ``append_stub()`` writes exactly
such an entry: when a RHYTHM upload is blocked only because no protocol is
registered, ``StorageManager`` derives a key from the study's DICOM
``ProtocolName`` and appends a placeholder here, so the operator only has
to paste the real UUID from RHYTHM's Protocol Records page.
"""

import logging
import re
import uuid as _uuid
from dataclasses import dataclass
from pathlib import Path

import yaml

logger = logging.getLogger(__name__)

DEFAULT_PATH = Path("config/rhythm_protocols.yaml")

# All-zeros UUID used as a "real UUID still needed" placeholder in stub
# entries. Loads fine, but uuid_for() treats it as unset so the study
# stays blocked from upload.
NIL_UUID = "00000000-0000-0000-0000-000000000000"


def _is_nil_uuid(value: object) -> bool:
    try:
        return _uuid.UUID(str(value)).int == 0
    except (ValueError, AttributeError, TypeError):
        return False


def slugify_protocol_name(name: str) -> str:
    """Derive a stable local protocol key from a free-text ``ProtocolName``.

    Lowercased; every run of non-alphanumerics becomes a single ``-``;
    leading/trailing ``-`` trimmed. Returns ``""`` when *name* has no
    usable characters — the caller should not create a stub in that case.
    """
    return re.sub(r"[^a-z0-9]+", "-", (name or "").strip().lower()).strip("-")


@dataclass(frozen=True)
class ProtocolEntry:
    id: str
    uuid: str
    name: str = ""
    description: str = ""

    @property
    def uuid_pending(self) -> bool:
        """True when ``uuid`` is the all-zeros placeholder — a stub whose
        real ``CTProtocol`` UUID has not been pasted in from RHYTHM's
        Protocol Records page yet. Such an entry loads (so the dashboard
        can offer it) but ``ProtocolMap.uuid_for`` returns ``None`` for it,
        keeping the study blocked from upload."""
        return _is_nil_uuid(self.uuid)


class ProtocolMapError(ValueError):
    """Raised when config/rhythm_protocols.yaml is present but malformed."""


class ProtocolMap:
    """Loaded ``config/rhythm_protocols.yaml`` — local protocol id → UUID."""

    def __init__(self, entries: list[ProtocolEntry], *, site_code: str | None = None):
        self.site_code = site_code
        self._by_id: dict[str, ProtocolEntry] = {e.id: e for e in entries}

    @property
    def entries(self) -> list[ProtocolEntry]:
        """All entries, in file order — for the dashboard protocol dropdown."""
        return list(self._by_id.values())

    def get(self, local_id: str | None) -> ProtocolEntry | None:
        if not local_id:
            return None
        return self._by_id.get(local_id)

    def uuid_for(self, local_id: str | None) -> str | None:
        entry = self.get(local_id)
        if entry is None or entry.uuid_pending:
            return None
        return entry.uuid

    @classmethod
    def load(
        cls,
        path: Path | str = DEFAULT_PATH,
        *,
        expected_site_code: str | None = None,
    ) -> "ProtocolMap":
        """Parse *path*. Missing file → empty map. Malformed → ProtocolMapError.

        If the file carries a ``site_code`` and *expected_site_code* is given,
        the two must match case-insensitively or ProtocolMapError is raised —
        a mismatch means every manifest item would be rejected by RHYTHM for
        a protocol/site mismatch.
        """
        path = Path(path)
        if not path.exists():
            logger.info("No RHYTHM protocol map at %s — RHYTHM export needs one", path)
            return cls([], site_code=None)

        try:
            raw = yaml.safe_load(path.read_text()) or {}
        except yaml.YAMLError as exc:
            raise ProtocolMapError(f"{path}: invalid YAML: {exc}") from exc
        if not isinstance(raw, dict):
            raise ProtocolMapError(f"{path}: top level must be a mapping")

        site_code = raw.get("site_code") or None
        if (
            site_code
            and expected_site_code
            and site_code.strip().casefold() != expected_site_code.strip().casefold()
        ):
            raise ProtocolMapError(
                f"{path}: site_code '{site_code}' does not match "
                f"storage.remote_storage.rhythm.site_code '{expected_site_code}' "
                f"in settings.yaml"
            )

        raw_protocols = raw.get("protocols") or []
        if not isinstance(raw_protocols, list):
            raise ProtocolMapError(f"{path}: 'protocols' must be a list")

        entries: list[ProtocolEntry] = []
        seen: set[str] = set()
        for i, row in enumerate(raw_protocols):
            if not isinstance(row, dict):
                raise ProtocolMapError(f"{path}: protocols[{i}] must be a mapping")
            local_id = str(row.get("id") or "").strip()
            uuid_str = str(row.get("uuid") or "").strip()
            if not local_id:
                raise ProtocolMapError(f"{path}: protocols[{i}] has no 'id'")
            if not uuid_str:
                raise ProtocolMapError(f"{path}: protocol '{local_id}' has no 'uuid'")
            try:
                _uuid.UUID(uuid_str)
            except ValueError as exc:
                raise ProtocolMapError(
                    f"{path}: protocol '{local_id}' uuid '{uuid_str}' is not a UUID"
                ) from exc
            if local_id in seen:
                raise ProtocolMapError(f"{path}: duplicate protocol id '{local_id}'")
            seen.add(local_id)
            entries.append(
                ProtocolEntry(
                    id=local_id,
                    uuid=uuid_str,
                    name=str(row.get("name") or "").strip(),
                    description=str(row.get("description") or "").strip(),
                )
            )

        logger.info("Loaded %d RHYTHM protocol(s) from %s", len(entries), path)
        return cls(entries, site_code=site_code)


def append_stub(
    path: Path | str,
    *,
    protocol_name: str,
    description: str = "",
) -> str | None:
    """Append a placeholder protocol entry derived from *protocol_name* to the
    protocol map at *path*, so an operator only has to paste the real
    ``CTProtocol`` UUID from RHYTHM's Protocol Records page instead of
    hand-authoring the whole block.

    The stub's ``uuid`` is :data:`NIL_UUID`, which :meth:`ProtocolMap.load`
    accepts but :meth:`ProtocolMap.uuid_for` resolves to ``None`` — so the
    study stays blocked from upload until the real UUID is filled in.

    Returns the new entry's ``id`` (the slug), or ``None`` when nothing was
    written: an unusable *protocol_name*, the slug already present
    (idempotent — the same name twice yields one stub), an unparseable map
    file (never append to a file that would then fail to load), or an append
    that unexpectedly breaks ``load`` (rolled back).

    Comments and block scalars in the file are preserved — the entry is
    appended as text, the document is not round-tripped through YAML.
    """
    slug = slugify_protocol_name(protocol_name)
    if not slug:
        return None

    path = Path(path)
    original: str | None = path.read_text() if path.exists() else None

    raw: dict = {}
    if original is not None:
        try:
            parsed = yaml.safe_load(original)
        except yaml.YAMLError:
            logger.error("Cannot append protocol stub — %s is not valid YAML", path)
            return None
        raw = parsed or {}
        if not isinstance(raw, dict):
            logger.error(
                "Cannot append protocol stub — %s top level is not a mapping", path
            )
            return None
        for row in raw.get("protocols") or []:
            if isinstance(row, dict) and str(row.get("id") or "").strip() == slug:
                return None  # already registered

    name_q = protocol_name.strip().replace('"', "'")
    entry_block = f'  - id: {slug}\n    name: "{name_q}"\n'
    if description.strip():
        entry_block += f'    description: "{description.strip().replace(chr(34), chr(39))}"\n'
    entry_block += f'    uuid: "{NIL_UUID}"   # <- paste from RHYTHM Protocol Records\n'

    protocols_val = raw.get("protocols")
    protocols_nonempty = isinstance(protocols_val, list) and len(protocols_val) > 0
    protocols_empty = "protocols" in raw and (protocols_val is None or protocols_val == [])
    if "protocols" in raw and not protocols_nonempty and not protocols_empty:
        logger.error(
            "Cannot append protocol stub — %s 'protocols' is not a list", path
        )
        return None

    if original is None or not original.strip():
        new_text = (
            "# RHYTHM protocol map — auto-appended stubs still need their real\n"
            "# CTProtocol UUID pasted in from RHYTHM's Protocol Records page.\n"
            "protocols:\n" + entry_block
        )
    elif protocols_nonempty:
        # Append the entry after the existing list (which the real files end
        # with). ProtocolMap.load below catches a layout this breaks.
        sep = "" if original.endswith("\n") else "\n"
        blank = "" if original.endswith("\n\n") else "\n"
        new_text = original + sep + blank + entry_block
    elif protocols_empty:
        # Key present but empty (`protocols:` / `protocols: []` / `null`) —
        # normalise that one line and drop the entry right under it, so keys
        # after it stay valid.
        lines = original.splitlines(keepends=True)
        for i, ln in enumerate(lines):
            if re.match(r"\s*protocols\s*:", ln):
                lines[i] = "protocols:\n"
                lines.insert(i + 1, entry_block)
                break
        else:
            # `protocols` key parsed but no line to anchor to (flow style, an
            # anchor, ...) — bail rather than report a success that wrote nothing.
            logger.error(
                "Cannot append protocol stub — no 'protocols:' line to extend in %s",
                path,
            )
            return None
        new_text = "".join(lines)
    else:
        sep = "" if original.endswith("\n") else "\n"
        new_text = original + sep + "\nprotocols:\n" + entry_block

    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(new_text)
    tmp.replace(path)

    try:
        ProtocolMap.load(path)
    except ProtocolMapError:
        logger.error(
            "Appending protocol stub '%s' broke %s — rolled back", slug, path,
            exc_info=True,
        )
        if original is None:
            path.unlink(missing_ok=True)
        else:
            path.write_text(original)
        return None

    logger.info(
        "Appended RHYTHM protocol stub '%s' to %s — real UUID still needed",
        slug, path,
    )
    return slug
