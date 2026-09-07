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
"""

import logging
import uuid as _uuid
from dataclasses import dataclass
from pathlib import Path

import yaml

logger = logging.getLogger(__name__)

DEFAULT_PATH = Path("config/rhythm_protocols.yaml")


@dataclass(frozen=True)
class ProtocolEntry:
    id: str
    uuid: str
    name: str = ""
    description: str = ""


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
        return entry.uuid if entry is not None else None

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
