"""Edit a whitelisted subset of ``config/settings.yaml`` from the dashboard.

Only the fields in :data:`FIELDS` can be changed. Everything else (ports,
paths, the anonymization profile and its rules, ...) is shown read-only by
the ``/settings`` page and stays a file edit, because a mistake there can
lock the operator out or weaken anonymization.

Each field is either applied **live** (the running code re-reads it from the
shared ``Settings`` dicts on every use) or takes effect **on restart** (the
value is copied into an object at startup).

Saving: the whole file is re-read, its sha256 compared with the version the
page loaded (a mismatch is a :class:`SettingsConflictError`), the changes are
validated, the previous file is copied to ``settings.yaml.bak.<UTC stamp>``
(newest :data:`KEEP_BACKUPS` kept), and the new file is written atomically
with mode 0600 (it holds secrets).
"""
from __future__ import annotations

import hashlib
import logging
import os
import re
import tempfile
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

logger = logging.getLogger(__name__)

KEEP_BACKUPS = 10
LIVE = "live"
RESTART = "restart"

_MODALITY_RE = re.compile(r"^[A-Z0-9]{1,16}$")
_ATTR_RE = re.compile(r"^[A-Za-z][A-Za-z0-9]{0,63}$")


class SettingsError(Exception):
    """Base class for settings-editor errors."""


class SettingsConflictError(SettingsError):
    """The file changed since the caller read it."""


class SettingsValidationError(SettingsError):
    def __init__(self, errors: dict[str, str]):
        super().__init__("invalid settings: " + "; ".join(f"{k}: {v}" for k, v in errors.items()))
        self.errors = errors


@dataclass(frozen=True)
class Field:
    path: str                 # dotted path inside settings.yaml
    section: str              # section id on the page
    label: str
    kind: str                 # int|float|bool|str|url|enum|secret|modalities|regexmap
    apply: str                # LIVE | RESTART
    help: str = ""
    minimum: float | None = None
    maximum: float | None = None
    choices: tuple[str, ...] = ()
    nullable: bool = False    # empty → key removed (feature off / default)
    default: Any = None       # shown when the key is absent


SECTIONS: list[dict[str, str]] = [
    {"id": "rhythm", "title": "RHYTHM connection",
     "help": "Where studies are uploaded. All three are needed to enable RHYTHM export."},
    {"id": "retention", "title": "Storage & retention"},
    {"id": "study", "title": "Study reception"},
    {"id": "series_filter", "title": "Series filter",
     "help": "Include rules win over exclude rules. Patterns are Python regular expressions."},
    {"id": "defacing", "title": "Defacing"},
]

FIELDS: list[Field] = [
    Field("storage.remote_storage.rhythm.base_url", "rhythm", "Base URL", "url", RESTART,
          "e.g. https://rhythm.example.org", nullable=True),
    Field("storage.remote_storage.rhythm.auth_token", "rhythm", "API token", "secret", RESTART,
          "Never shown again once saved.", nullable=True),
    Field("storage.remote_storage.rhythm.site_code", "rhythm", "Site code", "str", RESTART,
          "Must match the site code of the protocols registered in RHYTHM.", nullable=True),

    Field("storage.retain_after_export_days", "retention", "Keep after export (days)", "int",
          LIVE, "Empty: delete a study's images as soon as it is exported.",
          minimum=1, maximum=3650, nullable=True),
    Field("storage.max_storage_gb", "retention", "Storage quota (GB)", "float", LIVE,
          "Oldest exported studies are purged above this. Empty: no quota.",
          minimum=0.1, maximum=100000, nullable=True),

    Field("study.completion_timeout", "study", "Study complete after (s idle)", "int", RESTART,
          minimum=10, maximum=86400, default=120),
    Field("study.completion_check_interval", "study", "Completion check every (s)", "int",
          RESTART, minimum=5, maximum=3600, default=30),
    Field("study.max_study_size_mb", "study", "Max study size (MB)", "int", LIVE,
          "Images beyond this are refused. Empty: no limit.",
          minimum=1, maximum=1000000, nullable=True),

    Field("series_filter.exclude_modalities", "series_filter", "Excluded modalities",
          "modalities", RESTART, "DICOM modality codes, e.g. PR, RT.", default=[]),
    Field("series_filter.only_original_series", "series_filter", "Original series only", "bool",
          RESTART, "Drop DERIVED/SECONDARY series.", default=True),
    Field("series_filter.include", "series_filter", "Include rules", "regexmap", RESTART,
          "One per line: Attribute: pattern", nullable=True, default={}),
    Field("series_filter.exclude", "series_filter", "Exclude rules", "regexmap", RESTART,
          "One per line: Attribute: pattern", nullable=True, default={}),

    Field("defacing.enabled", "defacing", "Deface head scans", "bool", RESTART,
          "Off: studies with a head scan are held as 'defacing disabled'.", default=False),
    Field("defacing.device", "defacing", "Device", "enum", RESTART,
          "Falls back to CPU if the device is unavailable.",
          choices=("cuda", "mps", "cpu"), default="cuda"),
    Field("defacing.keep_backup", "defacing", "Keep undefaced backup", "bool", RESTART,
          "Keeps a copy WITH the face next to each series. Off in production.",
          default=True),
    Field("defacing.mask_dilation_mm", "defacing", "Mask dilation (mm)", "float", RESTART,
          minimum=0, maximum=20, default=2.0),
]

FIELDS_BY_PATH: dict[str, Field] = {f.path: f for f in FIELDS}

# Shown read-only. Secrets never appear here.
READ_ONLY: list[tuple[str, str]] = [
    ("dicom_server.ae_title", "DICOM AE title"),
    ("dicom_server.ip", "DICOM listen IP"),
    ("dicom_server.port", "DICOM port"),
    ("http_server.ip", "Dashboard listen IP"),
    ("http_server.port", "Dashboard port"),
    ("http_server.secure_cookies", "Secure cookies (HTTPS only)"),
    ("anonymization.profile", "Anonymization profile"),
    ("storage.base_path", "Study storage path"),
    ("storage.export_path", "Export path"),
    ("storage.protocols_file", "Protocol catalog file"),
    ("logging.level", "Log level"),
    ("logging.file", "Log file"),
]


# ------------------------------------------------------------------ helpers
def _get(doc: dict, path: str) -> tuple[bool, Any]:
    node: Any = doc
    for part in path.split("."):
        if not isinstance(node, dict) or part not in node:
            return False, None
        node = node[part]
    return True, node


def _set(doc: dict, path: str, value: Any) -> None:
    parts = path.split(".")
    node = doc
    for part in parts[:-1]:
        nxt = node.get(part)
        if not isinstance(nxt, dict):
            nxt = {}
            node[part] = nxt
        node = nxt
    node[parts[-1]] = value


def _delete(doc: dict, path: str) -> None:
    parts = path.split(".")
    node: Any = doc
    for part in parts[:-1]:
        node = node.get(part) if isinstance(node, dict) else None
        if node is None:
            return
    if isinstance(node, dict):
        node.pop(parts[-1], None)


def _version(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _number(f: Field, value: Any, integer: bool) -> int | float:
    if isinstance(value, bool):
        raise ValueError("must be a number")
    if isinstance(value, str):
        value = value.strip()
    try:
        num = float(value)
    except (TypeError, ValueError):
        raise ValueError("must be a number") from None
    if integer:
        if num != int(num):
            raise ValueError("must be a whole number")
        num = int(num)
    if f.minimum is not None and num < f.minimum:
        raise ValueError(f"must be at least {f.minimum:g}")
    if f.maximum is not None and num > f.maximum:
        raise ValueError(f"must be at most {f.maximum:g}")
    return num


def validate_value(f: Field, value: Any) -> Any:
    """Return the normalized value to store, or None to remove the key.

    Raises ValueError with a user-facing message.
    """
    if value is None or (isinstance(value, str) and not value.strip()
                         and f.kind not in ("modalities", "regexmap")):
        if f.nullable:
            return None
        raise ValueError("is required")
    if f.kind == "int":
        return _number(f, value, integer=True)
    if f.kind == "float":
        return _number(f, value, integer=False)
    if f.kind == "bool":
        if not isinstance(value, bool):
            raise ValueError("must be true or false")
        return value
    if f.kind in ("str", "secret", "url"):
        if not isinstance(value, str):
            raise ValueError("must be text")
        value = value.strip()
        if len(value) > 2048 or any(c in value for c in "\r\n\0"):
            raise ValueError("is not a valid single-line value")
        if f.kind == "url":
            if not re.match(r"^https?://[^\s/]+", value):
                raise ValueError("must start with http:// or https://")
            value = value.rstrip("/")
        return value
    if f.kind == "enum":
        if value not in f.choices:
            raise ValueError("must be one of " + ", ".join(f.choices))
        return value
    if f.kind == "modalities":
        if isinstance(value, str):
            value = [v for v in re.split(r"[\s,]+", value) if v]
        if not isinstance(value, list):
            raise ValueError("must be a list of modality codes")
        out: list[str] = []
        for item in value:
            code = str(item).strip().upper()
            if not _MODALITY_RE.match(code):
                raise ValueError(f"'{item}' is not a modality code")
            if code not in out:
                out.append(code)
        return out
    if f.kind == "regexmap":
        if isinstance(value, str):
            parsed: dict[str, str] = {}
            for line in value.splitlines():
                if not line.strip():
                    continue
                attr, sep, pattern = line.partition(":")
                if not sep:
                    raise ValueError(f"'{line.strip()}' is not 'Attribute: pattern'")
                parsed[attr.strip()] = pattern.strip()
            value = parsed
        if not isinstance(value, dict):
            raise ValueError("must be Attribute: pattern rules")
        out_map: dict[str, str] = {}
        for attr, pattern in value.items():
            if not _ATTR_RE.match(str(attr)):
                raise ValueError(f"'{attr}' is not a DICOM attribute keyword")
            try:
                re.compile(str(pattern))
            except re.error as e:
                raise ValueError(f"{attr}: invalid pattern ({e})") from None
            out_map[str(attr)] = str(pattern)
        return out_map or (None if f.nullable else {})
    raise ValueError(f"unsupported field kind {f.kind}")


# ------------------------------------------------------------------ editor
@dataclass
class SaveResult:
    changed: list[str]
    applied_live: list[str]
    restart_required: list[str]
    version: str
    backup: str | None = None


@dataclass
class SettingsEditor:
    """Reads and writes the editable part of one settings file.

    *settings* is the running ``Settings`` instance; live fields are written
    into its section dicts so the next read in the pipeline sees them.
    *catalog_site_code* is a callable returning the protocol catalog's
    ``site_code`` (or None), checked against a new RHYTHM site code.
    """
    path: Path
    settings: Any = None
    catalog_site_code: Any = None
    restart_pending: set[str] = field(default_factory=set)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    # ---- reading
    def read_document(self) -> tuple[bytes, dict]:
        raw = self.path.read_bytes()
        doc = yaml.safe_load(raw) or {}
        if not isinstance(doc, dict):
            raise SettingsError(f"{self.path} does not hold a YAML mapping")
        return raw, doc

    def snapshot(self) -> dict[str, Any]:
        raw, doc = self.read_document()
        sections = []
        for sec in SECTIONS:
            fields = []
            for f in FIELDS:
                if f.section != sec["id"]:
                    continue
                present, value = _get(doc, f.path)
                item: dict[str, Any] = {
                    "path": f.path, "label": f.label, "kind": f.kind, "apply": f.apply,
                    "help": f.help, "nullable": f.nullable, "min": f.minimum,
                    "max": f.maximum, "choices": list(f.choices),
                    "restart_pending": f.path in self.restart_pending,
                }
                if f.kind == "secret":
                    item["value"] = None
                    item["is_set"] = bool(present and value)
                else:
                    item["value"] = value if present else f.default
                    item["is_default"] = not present
                fields.append(item)
            sections.append({**sec, "fields": fields})
        read_only = []
        for path, label in READ_ONLY:
            present, value = _get(doc, path)
            read_only.append({"path": path, "label": label,
                              "value": value if present else None})
        return {
            "path": str(self.path),
            "version": _version(raw),
            "sections": sections,
            "read_only": read_only,
            "restart_pending": sorted(self.restart_pending),
        }

    # ---- writing
    def apply_changes(self, changes: dict[str, Any], version: str | None) -> SaveResult:
        if not isinstance(changes, dict) or not changes:
            raise SettingsValidationError({"changes": "nothing to save"})
        with self._lock:
            raw, doc = self.read_document()
            if version != _version(raw):
                raise SettingsConflictError(
                    "settings.yaml changed since the page was loaded — reload and retry")

            errors: dict[str, str] = {}
            normalized: dict[str, Any] = {}
            for path, value in changes.items():
                f = FIELDS_BY_PATH.get(path)
                if f is None:
                    errors[path] = "cannot be edited here"
                    continue
                try:
                    normalized[path] = validate_value(f, value)
                except ValueError as e:
                    errors[path] = f"{f.label} {e}"
            site_path = "storage.remote_storage.rhythm.site_code"
            if site_path in normalized and normalized[site_path] and self.catalog_site_code:
                cat = self.catalog_site_code()
                if cat and str(cat).strip().upper() != normalized[site_path].upper():
                    errors[site_path] = (
                        f"Site code does not match the protocol catalog's site code '{cat}'")
            if errors:
                raise SettingsValidationError(errors)

            changed = []
            for path, value in normalized.items():
                present, old = _get(doc, path)
                if (old if present else None) == value:
                    continue
                if value is None:
                    _delete(doc, path)
                else:
                    _set(doc, path, value)
                changed.append(path)
            if not changed:
                return SaveResult([], [], sorted(self.restart_pending), _version(raw))

            backup = self._backup(raw)
            new_raw = yaml.safe_dump(doc, sort_keys=False, allow_unicode=True,
                                     default_flow_style=False).encode()
            self._write_atomic(new_raw)

            live, restart = [], []
            for path in changed:
                if FIELDS_BY_PATH[path].apply == LIVE:
                    self._apply_live(path, normalized[path])
                    live.append(path)
                else:
                    self.restart_pending.add(path)
                    restart.append(path)
            return SaveResult(changed, live, sorted(self.restart_pending),
                              _version(new_raw), backup)

    def _apply_live(self, path: str, value: Any) -> None:
        if self.settings is None:
            return
        section, _, rest = path.partition(".")
        target = getattr(self.settings, section, None)
        if not isinstance(target, dict):
            return
        if value is None:
            _delete(target, rest)
        else:
            _set(target, rest, value)

    def _backup(self, raw: bytes) -> str:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        dest = self.path.with_name(f"{self.path.name}.bak.{stamp}")
        fd = os.open(dest, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as fh:
            fh.write(raw)
        backups = sorted(self.path.parent.glob(f"{self.path.name}.bak.*T*Z"))
        for old in backups[:-KEEP_BACKUPS]:
            try:
                old.unlink()
            except OSError as e:
                logger.warning(f"Could not remove old settings backup {old}: {e}")
        return str(dest)

    def _write_atomic(self, data: bytes) -> None:
        fd, tmp = tempfile.mkstemp(prefix=f".{self.path.name}.", dir=self.path.parent)
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "wb") as fh:
                fh.write(data)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, self.path)
        except BaseException:
            logger.error(f"Failed to write {self.path}", exc_info=True)
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
