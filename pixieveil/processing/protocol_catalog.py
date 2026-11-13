"""
Scanners & protocols catalog — ``config/protocols.yaml``.

One file, edited from the dashboard's ``/protocols`` page, replacing the
three hand-edited lookups that came before it (``integrations/rhythm/
scanner_lookup.yaml``, ``integrations/rhythm/indication_lookup.yaml`` and
``config/rhythm_protocols.yaml``)::

    site_code: S014                    # optional guard vs settings
    scanners:
      - id: siemens-emotion-16
        name: "Siemens Emotion 16"
        manufacturer: "SIEMENS"        # DICOM Manufacturer (case-insensitive)
        model: "Emotion 16"            # DICOM ManufacturerModelName
        rhythm_scanner_id: "7076…"     # optional, display only
        protocols:
          - key: cap-spiral-nativ      # unique in the file; exam rhythm.protocol_key
            label: "CAP_SPIRAL_NATIV"
            names: ["CAP_SPIRAL_NATIV"]   # exact ProtocolName aliases
            rhythm_uuid: "551a…"       # RHYTHM CTProtocol UUID; null = pending
            region: "Head"
            clinical_indication: "Trauma"
            iv_contrast: "Non-contrast"
            notes: ""

**Matching** — a study is matched to its scanner by (Manufacturer, Model),
compared case-insensitively against the pre-anonymization hints, and then to
one of that scanner's protocols by exact (case-insensitive,
whitespace-collapsed) name. The name comes from DICOM ``ProtocolName`` or,
when that is empty, the protocol part of ``StudyDescription``
(``"Head^CAP_SPIRAL_CONTRAST (Adult)"`` → ``"CAP_SPIRAL_CONTRAST"``). A
scanner with blank manufacturer *and* model is a wildcard group ("any
scanner") tried after the specific scanner. Legacy protocols migrated
without a known scanner land in one.

**RHYTHM** — ``uuid_for(key)`` resolves an exam's ``rhythm.protocol_key`` to
the ``protocol_id`` RHYTHM needs. A null or all-zeros UUID is *pending*: the
entry loads and can be picked, but resolves to ``None`` so export stays
blocked. ``entries`` / ``get`` / ``uuid_for`` keep the surface the old
``ProtocolMap`` had, so the RHYTHM export path didn't change shape.

**Migration** — when the catalog file doesn't exist, ``open_catalog()``
builds one from the legacy files (if any) and writes it. A file in the old
``rhythm_protocols.yaml`` shape (top-level ``protocols:`` and no
``scanners:``) is also read, in memory, as a wildcard group.
"""

import copy
import hashlib
import logging
import os
import re
import tempfile
import threading
import uuid as _uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

logger = logging.getLogger(__name__)

DEFAULT_PATH = Path("config/protocols.yaml")
LEGACY_PROTOCOL_MAP = Path("config/rhythm_protocols.yaml")
LEGACY_LOOKUP_DIR = Path("integrations/rhythm")

NIL_UUID = "00000000-0000-0000-0000-000000000000"
UNASSIGNED_SCANNER_ID = "any-scanner"

# The 8 (region, indication, contrast) combos and contrast values RHYTHM
# accepts. Imported lazily in validate() to avoid a processing→dashboard
# import at module load.

_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")


class CatalogError(ValueError):
    """The catalog file exists but can't be used (bad YAML / invalid content)."""


class CatalogValidationError(CatalogError):
    def __init__(self, errors: list[str]):
        super().__init__("; ".join(errors))
        self.errors = errors


class CatalogConflictError(CatalogError):
    """The document was changed by someone else since it was read."""


def slugify(name: str) -> str:
    """Lowercase; runs of non-alphanumerics → ``-``; trimmed. ``""`` if nothing usable."""
    return re.sub(r"[^a-z0-9]+", "-", (name or "").strip().lower()).strip("-")[:64]


def normalize_name(name: str | None) -> str:
    """Comparison form of a protocol name: casefolded, whitespace collapsed."""
    return " ".join(str(name or "").split()).casefold()


def protocol_name_from_description(description: str | None) -> str:
    """``"Head^CAP_SPIRAL_CONTRAST (Adult)"`` → ``"CAP_SPIRAL_CONTRAST"``.

    Takes the part after the last ``^`` (Siemens-style ``Body^Protocol``),
    then drops a trailing parenthesised qualifier such as ``(Adult)``.
    """
    text = str(description or "").strip()
    if "^" in text:
        text = text.rsplit("^", 1)[1]
    text = re.sub(r"\s*\([^()]*\)\s*$", "", text)
    return text.strip()


def is_pending_uuid(value: object) -> bool:
    if value in (None, ""):
        return True
    try:
        return _uuid.UUID(str(value)).int == 0
    except (ValueError, AttributeError, TypeError):
        return False


# ---------------------------------------------------------------------------
# Model

@dataclass(frozen=True)
class CatalogProtocol:
    key: str
    scanner_id: str
    label: str = ""
    names: tuple[str, ...] = ()
    rhythm_uuid: str | None = None
    region: str | None = None
    clinical_indication: str | None = None
    iv_contrast: str | None = None
    notes: str = ""

    # ProtocolMap-compatible surface (RHYTHM export path, /studies dropdown)
    @property
    def id(self) -> str:
        return self.key

    @property
    def name(self) -> str:
        return self.label or self.key

    @property
    def description(self) -> str:
        return self.notes

    @property
    def uuid(self) -> str:
        return self.rhythm_uuid or NIL_UUID

    @property
    def uuid_pending(self) -> bool:
        return is_pending_uuid(self.rhythm_uuid)

    @property
    def has_indication(self) -> bool:
        return bool(self.region and self.clinical_indication)


@dataclass(frozen=True)
class CatalogScanner:
    id: str
    name: str = ""
    manufacturer: str = ""
    model: str = ""
    rhythm_scanner_id: str | None = None
    protocols: tuple[CatalogProtocol, ...] = field(default_factory=tuple)

    @property
    def is_wildcard(self) -> bool:
        return not self.manufacturer.strip() and not self.model.strip()

    @property
    def label(self) -> str:
        return self.name or f"{self.manufacturer} {self.model}".strip() or self.id


@dataclass(frozen=True)
class CatalogMatch:
    """Outcome of ``ProtocolCatalog.match`` for one study."""

    scanner: CatalogScanner | None
    protocol: CatalogProtocol | None
    protocol_name: str          # the name that was (or would be) matched

    @property
    def matched(self) -> bool:
        return self.protocol is not None


# ---------------------------------------------------------------------------
# Parsing / validation

def _str(v: Any) -> str:
    return "" if v is None else str(v).strip()


def _opt(v: Any) -> str | None:
    s = _str(v)
    return s or None


def validate_document(doc: Any, *, expected_site_code: str | None = None) -> list[str]:
    """Return human-readable errors for a catalog document (``[]`` = valid)."""
    from pixieveil.dashboard import options  # lazy: avoid import cycle

    combos = {(c["region"], c["indication"]) for c in options.REGION_INDICATIONS}
    contrasts = set(options.IV_CONTRAST)
    errors: list[str] = []
    if not isinstance(doc, dict):
        return ["top level must be a mapping"]

    site_code = _opt(doc.get("site_code"))
    if (site_code and expected_site_code
            and site_code.casefold() != expected_site_code.strip().casefold()):
        errors.append(
            f"site_code '{site_code}' does not match storage.remote_storage.rhythm.site_code "
            f"'{expected_site_code}' in settings.yaml"
        )

    scanners = doc.get("scanners")
    if scanners is None:
        scanners = []
    if not isinstance(scanners, list):
        return errors + ["'scanners' must be a list"]

    scanner_ids: set[str] = set()
    makes: set[tuple[str, str]] = set()
    keys: set[str] = set()
    for i, sc in enumerate(scanners):
        where = f"scanners[{i}]"
        if not isinstance(sc, dict):
            errors.append(f"{where} must be a mapping")
            continue
        sid = _str(sc.get("id"))
        where = f"scanner '{sid or i}'"
        if not _SLUG_RE.match(sid):
            errors.append(f"{where}: id must be a lowercase slug (letters, digits, '-')")
        elif sid in scanner_ids:
            errors.append(f"{where}: duplicate scanner id")
        scanner_ids.add(sid)
        make = (_str(sc.get("manufacturer")).casefold(), _str(sc.get("model")).casefold())
        if make in makes:
            errors.append(f"{where}: another scanner already has manufacturer/model "
                          f"'{sc.get('manufacturer') or ''} | {sc.get('model') or ''}'")
        makes.add(make)
        rsid = _opt(sc.get("rhythm_scanner_id"))
        if rsid:
            try:
                _uuid.UUID(rsid)
            except ValueError:
                errors.append(f"{where}: rhythm_scanner_id '{rsid}' is not a UUID")

        protocols = sc.get("protocols") or []
        if not isinstance(protocols, list):
            errors.append(f"{where}: 'protocols' must be a list")
            continue
        names_here: dict[str, str] = {}
        for j, p in enumerate(protocols):
            pwhere = f"{where} protocols[{j}]"
            if not isinstance(p, dict):
                errors.append(f"{pwhere} must be a mapping")
                continue
            key = _str(p.get("key"))
            pwhere = f"protocol '{key or j}'"
            if not _SLUG_RE.match(key):
                errors.append(f"{pwhere}: key must be a lowercase slug (letters, digits, '-')")
            elif key in keys:
                errors.append(f"{pwhere}: duplicate protocol key")
            keys.add(key)
            names = p.get("names") or []
            if not isinstance(names, list):
                errors.append(f"{pwhere}: 'names' must be a list")
                names = []
            for n in names:
                norm = normalize_name(n)
                if not norm:
                    errors.append(f"{pwhere}: empty name")
                elif norm in names_here and names_here[norm] != key:
                    errors.append(f"{pwhere}: name '{n}' is already used by protocol "
                                  f"'{names_here[norm]}' on this scanner")
                else:
                    names_here[norm] = key
            ruuid = _opt(p.get("rhythm_uuid"))
            if ruuid:
                try:
                    _uuid.UUID(ruuid)
                except ValueError:
                    errors.append(f"{pwhere}: rhythm_uuid '{ruuid}' is not a UUID")
            region, indication = _opt(p.get("region")), _opt(p.get("clinical_indication"))
            if (region or indication) and (region, indication) not in combos:
                errors.append(f"{pwhere}: region/indication '{region} / {indication}' "
                              f"is not one of RHYTHM's combinations")
            contrast = _opt(p.get("iv_contrast"))
            if contrast and contrast not in contrasts:
                errors.append(f"{pwhere}: iv_contrast must be one of {sorted(contrasts)}")
    return errors


def _parse(doc: dict) -> tuple[str | None, list[CatalogScanner]]:
    scanners = []
    for sc in doc.get("scanners") or []:
        sid = _str(sc.get("id"))
        protos = tuple(
            CatalogProtocol(
                key=_str(p.get("key")),
                scanner_id=sid,
                label=_str(p.get("label")),
                names=tuple(_str(n) for n in (p.get("names") or []) if _str(n)),
                rhythm_uuid=_opt(p.get("rhythm_uuid")),
                region=_opt(p.get("region")),
                clinical_indication=_opt(p.get("clinical_indication")),
                iv_contrast=_opt(p.get("iv_contrast")),
                notes=_str(p.get("notes")),
            )
            for p in (sc.get("protocols") or [])
        )
        scanners.append(CatalogScanner(
            id=sid, name=_str(sc.get("name")), manufacturer=_str(sc.get("manufacturer")),
            model=_str(sc.get("model")), rhythm_scanner_id=_opt(sc.get("rhythm_scanner_id")),
            protocols=protos,
        ))
    return _opt(doc.get("site_code")), scanners


def normalize_document(doc: dict) -> dict:
    """Canonical, JSON/YAML-safe form of a (validated) document."""
    site_code, scanners = _parse(doc)
    out: dict[str, Any] = {}
    if site_code:
        out["site_code"] = site_code
    out["scanners"] = [
        {
            "id": s.id, "name": s.name, "manufacturer": s.manufacturer, "model": s.model,
            "rhythm_scanner_id": s.rhythm_scanner_id,
            "protocols": [
                {
                    "key": p.key, "label": p.label, "names": list(p.names),
                    "rhythm_uuid": None if p.uuid_pending else p.rhythm_uuid,
                    "region": p.region, "clinical_indication": p.clinical_indication,
                    "iv_contrast": p.iv_contrast, "notes": p.notes,
                }
                for p in s.protocols
            ],
        }
        for s in scanners
    ]
    return out


# ---------------------------------------------------------------------------
# Legacy migration

def _load_yaml(path: Path) -> Any:
    try:
        return yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError):
        logger.warning("Cannot read legacy file %s", path, exc_info=True)
        return None


def _legacy_protocols_to_scanner_protocols(rows: list) -> list[dict]:
    out = []
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        key = _str(row.get("id"))
        if key and not _SLUG_RE.match(key):
            # Old files allowed any id ("cap-spiral-nativ-6+"); catalog keys
            # are slugs. "+" becomes "plus" so "x-6+" and "x-6" stay distinct.
            fixed = slugify(key.replace("+", "plus"))
            logger.warning("Legacy protocol id %r is not a valid key; using %r — exams "
                           "that used the old id need their RHYTHM protocol chosen again",
                           key, fixed)
            key = fixed
        name = _str(row.get("name"))
        uuid_str = _opt(row.get("uuid"))
        out.append({
            "key": key, "label": name or key, "names": [name] if name else [],
            "rhythm_uuid": None if is_pending_uuid(uuid_str) else uuid_str,
            "region": None, "clinical_indication": None, "iv_contrast": None,
            "notes": _str(row.get("description")),
        })
    return out


def migrate_legacy(protocol_map: Path = LEGACY_PROTOCOL_MAP,
                   lookup_dir: Path = LEGACY_LOOKUP_DIR) -> dict | None:
    """Build a catalog document from the pre-catalog files, or None if none exist.

    - scanner_lookup.yaml ``"MAKE|Model": uuid`` → one scanner each.
    - rhythm_protocols.yaml entries → protocols (keys kept so existing exam
      ``rhythm.protocol_key`` values still resolve), attached to the only
      scanner when there is exactly one, else to a wildcard "Any scanner" group.
    - indication_lookup.yaml rules whose ``protocol_name_contains`` equals a
      protocol's name (case-insensitive) give it region/indication/contrast;
      other rules can't be expressed as exact names and are logged.
    """
    scanner_file = lookup_dir / "scanner_lookup.yaml"
    indication_file = lookup_dir / "indication_lookup.yaml"
    if not (protocol_map.exists() or scanner_file.exists()):
        return None

    scanners: list[dict] = []
    raw_scanners = _load_yaml(scanner_file) if scanner_file.exists() else None
    for k, v in (raw_scanners or {}).items() if isinstance(raw_scanners, dict) else []:
        make, _, model = str(k).partition("|")
        sid = slugify(f"{make} {model}") or f"scanner-{len(scanners) + 1}"
        scanners.append({"id": sid, "name": f"{make.title()} {model}".strip(),
                         "manufacturer": make, "model": model,
                         "rhythm_scanner_id": _opt(v), "protocols": []})

    site_code = None
    protocols: list[dict] = []
    raw_map = _load_yaml(protocol_map) if protocol_map.exists() else None
    if isinstance(raw_map, dict):
        site_code = _opt(raw_map.get("site_code"))
        protocols = _legacy_protocols_to_scanner_protocols(raw_map.get("protocols") or [])

    rules = []
    raw_rules = _load_yaml(indication_file) if indication_file.exists() else None
    if isinstance(raw_rules, dict):
        rules = [r for r in raw_rules.get("rules") or [] if isinstance(r, dict)]
    used_rules: set[int] = set()
    for p in protocols:
        for i, r in enumerate(rules):
            needle = normalize_name(r.get("protocol_name_contains"))
            if needle and any(normalize_name(n) == needle for n in p["names"]):
                p.update(region=_opt(r.get("region")),
                         clinical_indication=_opt(r.get("clinical_indication")),
                         iv_contrast=_opt(r.get("iv_contrast")))
                used_rules.add(i)
                break
    for i, r in enumerate(rules):
        if i not in used_rules:
            logger.warning("Catalog migration: indication rule %s has no matching protocol "
                           "name — re-create it on /protocols", r)

    if protocols:
        if len(scanners) == 1:
            scanners[0]["protocols"] = protocols
        else:
            scanners.append({"id": UNASSIGNED_SCANNER_ID, "name": "Any scanner",
                             "manufacturer": "", "model": "", "rhythm_scanner_id": None,
                             "protocols": protocols})
    doc: dict[str, Any] = {"scanners": scanners}
    if site_code:
        doc["site_code"] = site_code
    return doc


def _legacy_shape_to_document(raw: dict) -> dict:
    """An old rhythm_protocols.yaml-shaped document read as a catalog."""
    return {
        "site_code": raw.get("site_code"),
        "scanners": [{
            "id": UNASSIGNED_SCANNER_ID, "name": "Any scanner", "manufacturer": "",
            "model": "", "protocols": _legacy_protocols_to_scanner_protocols(
                raw.get("protocols") or []),
        }],
    }


# ---------------------------------------------------------------------------
# Catalog

class ProtocolCatalog:
    """Thread-safe view of the catalog file, reloaded when it changes on disk."""

    def __init__(self, path: Path | str = DEFAULT_PATH, *,
                 expected_site_code: str | None = None,
                 scanners: list[CatalogScanner] | None = None,
                 site_code: str | None = None) -> None:
        self.path = Path(path)
        self.expected_site_code = expected_site_code
        self._lock = threading.RLock()
        self._scanners: list[CatalogScanner] = scanners or []
        self.site_code = site_code
        self._version = "empty"
        self._mtime: float | None = None
        self._loaded = scanners is not None

    # ------------------------------------------------------------ loading
    @classmethod
    def load(cls, path: Path | str = DEFAULT_PATH, *,
             expected_site_code: str | None = None) -> "ProtocolCatalog":
        """Load *path* (missing → empty). Raises CatalogError if it's invalid."""
        cat = cls(path, expected_site_code=expected_site_code)
        with cat._lock:
            cat._reload(force=True, raise_errors=True)
        return cat

    def _read_document(self) -> tuple[dict, bytes]:
        data = self.path.read_bytes()
        try:
            raw = yaml.safe_load(data) or {}
        except yaml.YAMLError as exc:
            raise CatalogError(f"{self.path}: invalid YAML: {exc}") from exc
        if not isinstance(raw, dict):
            raise CatalogError(f"{self.path}: top level must be a mapping")
        if "scanners" not in raw and "protocols" in raw:
            raw = _legacy_shape_to_document(raw)
        return raw, data

    def _reload(self, force: bool = False, raise_errors: bool = False) -> None:
        try:
            mtime = self.path.stat().st_mtime
        except FileNotFoundError:
            if force or self._mtime is not None:
                self._scanners, self.site_code = [], None
                self._version, self._mtime = "empty", None
                if force:
                    logger.info("No protocol catalog at %s — map protocols on /protocols",
                                self.path)
            self._loaded = True
            return
        if self._loaded and not force and mtime == self._mtime:
            return
        try:
            raw, data = self._read_document()
            errors = validate_document(raw, expected_site_code=self.expected_site_code)
            if errors:
                raise CatalogValidationError([f"{self.path}: {e}" for e in errors])
        except CatalogError:
            if raise_errors:
                raise
            logger.error("Protocol catalog %s changed but is invalid — keeping the "
                         "previous version", self.path, exc_info=True)
            self._mtime = mtime
            return
        self.site_code, self._scanners = _parse(raw)
        self._version = hashlib.sha256(data).hexdigest()[:16]
        self._mtime = mtime
        self._loaded = True
        logger.info("Loaded protocol catalog %s: %d scanner(s), %d protocol(s)",
                    self.path, len(self._scanners), len(self.entries))

    def _fresh(self) -> None:
        with self._lock:
            if self._loaded and self._mtime is None and not self.path.exists():
                return
            self._reload()

    # ------------------------------------------------------------ queries
    @property
    def version(self) -> str:
        self._fresh()
        return self._version

    @property
    def scanners(self) -> list[CatalogScanner]:
        self._fresh()
        return list(self._scanners)

    @property
    def entries(self) -> list[CatalogProtocol]:
        """Every protocol across scanners, in file order."""
        return [p for s in self._scanners_now() for p in s.protocols]

    def _scanners_now(self) -> list[CatalogScanner]:
        self._fresh()
        return self._scanners

    def get(self, key: str | None) -> CatalogProtocol | None:
        if not key:
            return None
        return next((p for p in self.entries if p.key == key), None)

    def uuid_for(self, key: str | None) -> str | None:
        entry = self.get(key)
        if entry is None or entry.uuid_pending:
            return None
        return entry.rhythm_uuid

    def get_scanner(self, scanner_id: str | None) -> CatalogScanner | None:
        return next((s for s in self._scanners_now() if s.id == scanner_id), None)

    def find_scanner(self, manufacturer: str | None, model: str | None) -> CatalogScanner | None:
        make, mdl = _str(manufacturer).casefold(), _str(model).casefold()
        if not make and not mdl:
            return None
        scanners = [s for s in self._scanners_now() if not s.is_wildcard]
        exact = next((s for s in scanners
                      if s.manufacturer.casefold() == make and s.model.casefold() == mdl), None)
        if exact is not None:
            return exact
        # Tolerate vendor naming: "SIEMENS / Emotion 16" (DICOM) and "Siemens
        # Healthineers / SOMATOM Emotion 16" (RHYTHM catalogue) are the same
        # scanner — same manufacturer family and the same model words, brand
        # words ignored. Only an unambiguous, identical-model hit counts.
        from pixieveil.processing.rhythm_import import manufacturer_family, model_tokens
        fam, toks = manufacturer_family(manufacturer), model_tokens(model)
        if not toks:
            return None
        hits = [s for s in scanners if manufacturer_family(s.manufacturer) == fam
                and model_tokens(s.model) == toks]
        return hits[0] if len(hits) == 1 else None

    def match(self, manufacturer: str | None, model: str | None,
              protocol_name: str | None, study_description: str | None) -> CatalogMatch:
        """Find the scanner and protocol for a study (see module docstring)."""
        name = _str(protocol_name) or protocol_name_from_description(study_description)
        scanner = self.find_scanner(manufacturer, model)
        candidates = {normalize_name(name)} - {""}
        derived = normalize_name(protocol_name_from_description(study_description))
        if derived:
            candidates.add(derived)
        groups = ([scanner] if scanner else []) + [s for s in self._scanners_now()
                                                   if s.is_wildcard]
        for group in groups:
            for p in group.protocols:
                if candidates & {normalize_name(n) for n in p.names}:
                    return CatalogMatch(scanner=scanner, protocol=p, protocol_name=name)
        return CatalogMatch(scanner=scanner, protocol=None, protocol_name=name)

    def document(self) -> dict:
        """The current catalog as a plain document (for the API)."""
        with self._lock:
            self._fresh()
            doc = normalize_document({
                "site_code": self.site_code,
                "scanners": [
                    {**s.__dict__, "protocols": [p.__dict__ | {"names": list(p.names)}
                                                 for p in s.protocols]}
                    for s in self._scanners
                ],
            })
            return copy.deepcopy(doc)

    # ------------------------------------------------------------ writing
    def replace(self, doc: dict, *, expected_version: str | None) -> str:
        """Validate and atomically write *doc*. Returns the new version.

        Raises CatalogConflictError when *expected_version* is given and the
        file changed since, CatalogValidationError when *doc* is invalid.
        """
        with self._lock:
            self._fresh()
            if expected_version is not None and expected_version != self._version:
                raise CatalogConflictError(
                    "the catalog was changed by someone else — reload and try again")
            errors = validate_document(doc, expected_site_code=self.expected_site_code)
            if errors:
                raise CatalogValidationError(errors)
            self._write(normalize_document(doc))
            self._reload(force=True, raise_errors=True)
            return self._version

    def _write(self, doc: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix=".protocols.", suffix=".tmp", dir=self.path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write("# PixieVeil scanners & protocols — edit on the dashboard's "
                        "/protocols page\n")
                yaml.safe_dump(doc, f, sort_keys=False, allow_unicode=True,
                               default_flow_style=False)
            os.replace(tmp, self.path)
        except OSError:
            logger.error("Cannot write protocol catalog %s", self.path, exc_info=True)
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise


def open_catalog(path: Path | str | None = None, *, expected_site_code: str | None = None,
                 legacy_protocol_map: Path | str | None = None,
                 legacy_lookup_dir: Path | str | None = None) -> ProtocolCatalog:
    """Load the catalog, first migrating the legacy files into it if it doesn't exist.

    Raises CatalogError if the catalog (or the migrated document) is invalid.
    """
    path = Path(path or DEFAULT_PATH)
    if not path.exists():
        doc = migrate_legacy(Path(legacy_protocol_map or LEGACY_PROTOCOL_MAP),
                             Path(legacy_lookup_dir or LEGACY_LOOKUP_DIR))
        if doc is not None:
            errors = validate_document(doc, expected_site_code=expected_site_code)
            if errors:
                raise CatalogValidationError([f"migrated catalog: {e}" for e in errors])
            ProtocolCatalog(path)._write(normalize_document(doc))
            logger.warning(
                "Migrated legacy scanner/protocol lookups into %s — the old files "
                "(config/rhythm_protocols.yaml, integrations/rhythm/scanner_lookup.yaml, "
                "indication_lookup.yaml) are no longer read", path)
    return ProtocolCatalog.load(path, expected_site_code=expected_site_code)
