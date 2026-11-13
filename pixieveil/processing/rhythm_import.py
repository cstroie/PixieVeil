"""Plan an "Import from RHYTHM" onto the local protocol catalog.

Pure functions, no I/O: given the catalog document (as served by
``GET /api/protocols``) and the site's scanner profiles and protocols read
from RHYTHM, propose how they map onto each other. Nothing is applied here;
the /protocols page shows the plan, the admin ticks what to take, and the
normal save path (validation, version check, re-matching) writes it.

Scanner pairing: manufacturer family (aliases) + model tokens, ignoring
brand words, so RHYTHM's "Siemens Healthineers / SOMATOM Emotion 16" pairs
with the DICOM "SIEMENS / Emotion 16".

Protocol pairing: RHYTHM ``protocol_name`` vs the local protocol's
``names`` and ``label``, ignoring case and treating ``_``/``-``/spaces alike.
"""
from __future__ import annotations

import re
from typing import Any

from pixieveil.processing.protocol_catalog import is_pending_uuid

# Manufacturer families: any alias (casefolded substring) → family key.
MANUFACTURER_ALIASES: dict[str, tuple[str, ...]] = {
    "siemens": ("siemens",),
    "ge": ("ge medical", "ge healthcare", "gehc", "general electric"),
    "canon": ("canon", "toshiba"),
    "philips": ("philips",),
    "united-imaging": ("united imaging", "uih"),
    "neusoft": ("neusoft",),
    "fujifilm": ("fujifilm", "hitachi"),
}
# Words that carry no model identity.
_BRAND_WORDS = {"somatom", "naeotom", "series", "ct", "scanner",
                # manufacturer words: RHYTHM's catalogue names models
                # "Siemens SOMATOM Force", DICOM says just "SOMATOM Force"
                "siemens", "healthineers", "ge", "healthcare", "medical", "systems",
                "philips", "canon", "toshiba", "united", "imaging", "uih", "neusoft",
                "fujifilm", "hitachi", "minfound", "samsung", "neurologica"}

SET_UUID = "set_uuid"
SAME = "same"
REPLACE_UUID = "replace_uuid"
ADD = "add"
AMBIGUOUS = "ambiguous"


def manufacturer_family(name: str | None) -> str | None:
    low = str(name or "").casefold().strip()
    if not low:
        return None
    if low == "ge" or low.startswith("ge "):
        return "ge"
    for family, aliases in MANUFACTURER_ALIASES.items():
        if any(a in low for a in aliases):
            return family
    return low


def model_tokens(model: str | None) -> frozenset[str]:
    words = re.findall(r"[a-z0-9]+(?:\.[a-z0-9]+)*", str(model or "").casefold())
    return frozenset(w for w in words if w not in _BRAND_WORDS)


def protocol_key(name: str | None) -> str:
    """Comparison form of a protocol name: case-, space-, _- and --insensitive."""
    return " ".join(re.split(r"[\s_\-]+", str(name or "").casefold())).strip()


def _is_wildcard(scanner: dict) -> bool:
    return not (scanner.get("manufacturer") or "").strip() and \
        not (scanner.get("model") or "").strip()


def _scanner_score(scanner: dict, profile: dict) -> int:
    """2: same manufacturer family and same model tokens; 1: one model's
    tokens contain the other's (e.g. "Definition" vs "Definition Edge"), a
    candidate only; 0: no match."""
    if _is_wildcard(scanner):
        return 0
    if manufacturer_family(scanner.get("manufacturer")) != \
            manufacturer_family(profile.get("manufacturer_name")):
        return 0
    local = model_tokens(scanner.get("model"))
    remote = model_tokens(profile.get("scanner_model_name"))
    if not local or not remote:
        return 0
    if local == remote:
        return 2
    return 1 if (local <= remote or remote <= local) else 0


def _combo_lookup(vocab: dict) -> dict[tuple[str, str], dict]:
    return {(c["region"].casefold(), c["indication"].casefold()): c
            for c in vocab.get("region_indications", [])}


def _indication_fill(local: dict, remote: dict, vocab: dict) -> tuple[dict, list[str]]:
    """Fields to fill on *local* from *remote*, and notes for differences."""
    fill: dict[str, str] = {}
    notes: list[str] = []
    region = str(remote.get("anatomical_region") or "").strip()
    indication = str(remote.get("clinical_indication") or "").strip()
    contrast = str(remote.get("contrast") or "").strip()
    combo = _combo_lookup(vocab).get((region.casefold(), indication.casefold()))
    if region or indication:
        if combo is None:
            notes.append(f"RHYTHM region/indication '{region} / {indication}' is not one of "
                         "the known combinations — not copied")
        elif not local.get("region") and not local.get("clinical_indication"):
            fill["region"] = combo["region"]
            fill["clinical_indication"] = combo["indication"]
        elif (str(local.get("region") or "").casefold(),
              str(local.get("clinical_indication") or "").casefold()) != \
                (combo["region"].casefold(), combo["indication"].casefold()):
            notes.append(f"RHYTHM says {combo['region']} / {combo['indication']}; "
                         "kept the local value")
    if contrast:
        known = {c.casefold(): c for c in vocab.get("iv_contrast", [])}
        value = known.get(contrast.casefold())
        if value is None:
            notes.append(f"RHYTHM contrast '{contrast}' is not a known value — not copied")
        elif not local.get("iv_contrast"):
            fill["iv_contrast"] = value
        elif str(local["iv_contrast"]).casefold() != value.casefold():
            notes.append(f"RHYTHM says {value}; kept the local contrast")
    return fill, notes


def plan_import(catalog: dict, profiles: list[dict], protocols: list[dict],
                vocab: dict, site_code: str | None = None) -> dict[str, Any]:
    """Proposals for importing RHYTHM *profiles* / *protocols* into *catalog*.

    *vocab* is ``{"region_indications": [...], "iv_contrast": [...]}`` (as in
    ``dashboard.options``). Returns ``{"scanners": [...], "unpaired_local":
    [...], "foreign_site": [...], "orphans": [...]}``; see the module docstring.
    """
    local_scanners = [s for s in catalog.get("scanners") or [] if isinstance(s, dict)]
    wildcards = [s for s in local_scanners if _is_wildcard(s)]
    site = (site_code or catalog.get("site_code") or "").strip().casefold()

    # How many local protocols carry each real UUID (the shared-UUID case).
    uuid_use: dict[str, int] = {}
    for s in local_scanners:
        for p in s.get("protocols") or []:
            u = str(p.get("rhythm_uuid") or "").lower()
            if u and not is_pending_uuid(u):
                uuid_use[u] = uuid_use.get(u, 0) + 1

    by_profile: dict[str, list[dict]] = {}
    foreign: list[dict] = []
    for proto in protocols:
        if site and str(proto.get("site_code") or "").strip().casefold() not in ("", site):
            foreign.append({"id": proto.get("id"), "name": proto.get("protocol_name"),
                            "site_code": proto.get("site_code")})
            continue
        by_profile.setdefault(str(proto.get("scanner")), []).append(proto)

    paired_local: set[str] = set()
    out_scanners = []
    for profile in profiles:
        pid = str(profile.get("id"))
        scored = [(_scanner_score(s, profile), s["id"]) for s in local_scanners]
        exact = [sid for score, sid in scored if score == 2]
        candidates = exact + [sid for score, sid in scored if score == 1]
        # Paired automatically only on one exact model match; anything
        # looser is offered for the admin to pick.
        match = exact[0] if len(exact) == 1 else None
        if match:
            paired_local.add(match)
        rows = []
        for proto in by_profile.pop(pid, []):
            rows.append(_protocol_row(proto, match, local_scanners, wildcards, uuid_use,
                                      vocab))
        out_scanners.append({
            "profile": {"id": pid, "manufacturer": profile.get("manufacturer_name"),
                        "model": profile.get("scanner_model_name"),
                        "detector_rows": profile.get("detector_rows")},
            "match": match,
            "candidates": candidates,
            "protocols": rows,
            "local_not_on_rhythm": _local_not_on_rhythm(match, local_scanners, rows),
        })

    orphans = [{"id": p.get("id"), "name": p.get("protocol_name"), "scanner": sid}
               for sid, ps in by_profile.items() for p in ps]
    unpaired = [{"id": s["id"], "name": s.get("name"), "manufacturer": s.get("manufacturer"),
                 "model": s.get("model")}
                for s in local_scanners if not _is_wildcard(s) and s["id"] not in paired_local]
    return {"scanners": out_scanners, "unpaired_local": unpaired,
            "foreign_site": foreign, "orphans": orphans}


def _protocol_candidates(name_key: str, scanner: dict) -> list[dict]:
    hits = []
    for p in scanner.get("protocols") or []:
        keys = {protocol_key(n) for n in (p.get("names") or [])} | {protocol_key(p.get("label"))}
        if name_key and name_key in keys:
            hits.append(p)
    return hits


def _protocol_row(proto: dict, match: str | None, local_scanners: list[dict],
                  wildcards: list[dict], uuid_use: dict[str, int], vocab: dict) -> dict:
    uuid = str(proto.get("id") or "").lower()
    name = str(proto.get("protocol_name") or "").strip()
    name_key = protocol_key(name)
    # A local protocol already carrying this UUID is the match, whatever its
    # names are: local names are the scanner's ProtocolNames ("CAP_N_PESTE_6A"),
    # which needn't resemble RHYTHM's protocol_name.
    by_uuid = [(s["id"], p) for s in local_scanners for p in s.get("protocols") or []
               if uuid and str(p.get("rhythm_uuid") or "").lower() == uuid]
    search = [s for s in local_scanners if s["id"] == match] + wildcards
    hits = by_uuid or [(s["id"], p) for s in search for p in _protocol_candidates(name_key, s)]
    row: dict[str, Any] = {
        "rhythm": {"id": uuid, "name": name, "protocol_type": proto.get("protocol_type"),
                   "age_group": proto.get("age_group"),
                   "region": proto.get("anatomical_region"),
                   "clinical_indication": proto.get("clinical_indication"),
                   "contrast": proto.get("contrast")},
        "candidates": [{"scanner": sid, "key": p.get("key"), "label": p.get("label")}
                       for sid, p in hits],
        "notes": [],
    }
    if not hits:
        fill, notes = _indication_fill({}, proto, vocab)
        row.update(action=ADD, local=None, fill=fill, selected=False,
                   target_scanner=match)
        row["notes"] = notes + ([] if match else
                                ["No local scanner paired — pick one to add it to"])
        return row
    if len(hits) > 1:
        row.update(action=AMBIGUOUS, local=None, fill={}, selected=False)
        row["notes"].append("Several local protocols have this name — pick one")
        return row
    sid, local = hits[0]
    fill, notes = _indication_fill(local, proto, vocab)
    old = str(local.get("rhythm_uuid") or "").lower()
    row["local"] = {"scanner": sid, "key": local.get("key"), "label": local.get("label"),
                    "rhythm_uuid": local.get("rhythm_uuid"),
                    "names": list(local.get("names") or [])}
    row["fill"] = fill
    row["notes"] = notes
    if not is_pending_uuid(old) and old == uuid:
        row.update(action=SAME, selected=bool(fill))
    elif is_pending_uuid(old):
        row.update(action=SET_UUID, selected=True)
    else:
        row.update(action=REPLACE_UUID, selected=False,
                   shared_old=uuid_use.get(old, 0) > 1)
        if row["shared_old"]:
            row["notes"].append(f"The current UUID is shared by {uuid_use[old]} local "
                                "protocols (left over from the migration)")
    return row


def _local_not_on_rhythm(match: str | None, local_scanners: list[dict],
                         rows: list[dict]) -> list[dict]:
    if not match:
        return []
    taken = {(r["local"] or {}).get("key") for r in rows if r.get("local")}
    taken |= {c["key"] for r in rows for c in r.get("candidates", [])}
    scanner = next(s for s in local_scanners if s["id"] == match)
    return [{"key": p.get("key"), "label": p.get("label")}
            for p in scanner.get("protocols") or [] if p.get("key") not in taken]
