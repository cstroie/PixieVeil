"""Draft a local catalog protocol + a RHYTHM CTProtocol from one study.

Pure functions, no I/O. Input: the study's *effective* exam sidecar (see
``exam_extractor`` / ``exam_merge``), a few raw header tags read while the
study is on disk, RHYTHM's protocol choices (live ``/api/v1/protocol-choices/``
or the bundled ``dashboard/rhythm_choices.json``) and the catalog document.

Every RHYTHM field gets a provenance shown on the form:

* ``study`` — read straight from the study (RDSR / headers) and matched to a
  RHYTHM option;
* ``guess`` — a heuristic (kernel → class, modulation → vendor option, …);
* ``fill``  — unknown; the operator must choose before creating.

Values are only ever RHYTHM option *values* (or "Other: Please Specify" +
free text), because RHYTHM's REST create does not validate choices itself.
"""
from __future__ import annotations

import re
from collections import Counter
from typing import Any

from pixieveil.processing.protocol_catalog import protocol_name_from_description, slugify
from pixieveil.processing.rhythm_import import manufacturer_family

STUDY, GUESS, FILL = "study", "guess", "fill"
OTHER = "Other: Please Specify"

PROTOCOL_TYPES = ("PEDIATRIC_HEAD", "PEDIATRIC_BODY", "YOUNG_ADULT")
# RHYTHM GUI's required fields (protocol_clinical_gui.js REQUIRED_PROTOCOL_FIELDS)
# plus the tab's examination/age group.
REQUIRED = ("examination_group", "age_group", "scan_type", "contrast", "auto_kvp_selection",
            "kvp", "auto_ma_modulation", "pitch", "rotation_time", "slice_thickness",
            "kernel_class", "reconstruction_algorithm")
TECHNIQUE_FIELDS = ("scan_type", "contrast", "number_of_phases", "auto_kvp_selection", "kvp",
                    "auto_ma_modulation", "pitch", "rotation_time", "slice_thickness",
                    "scan_fov", "kernel_class", "reconstruction_algorithm", "protocol_intent")
HEAD_REGIONS = ("head", "mastoid")

# RHYTHM manufacturer names (as in its catalogue / field options) per family.
RHYTHM_MANUFACTURER = {"siemens": "Siemens Healthineers", "ge": "GE HealthCare",
                       "canon": "Canon Medical", "philips": "Philips",
                       "united-imaging": "United Imaging", "neusoft": "Neusoft Medical",
                       "fujifilm": "Fujifilm / Hitachi"}


# ------------------------------------------------------------------ choices
class Choices:
    """RHYTHM option lists, with the vendor-specific auto kV / mA lists and
    the mA-modulation input labels (only in the bundled snapshot)."""

    def __init__(self, categories: list[dict], manufacturer_field_options: dict | None = None,
                 ma_modulation_inputs: dict | None = None):
        self.cats = {c.get("key"): c for c in categories if isinstance(c, dict)}
        self.vendor = manufacturer_field_options or {}
        self.ma_inputs = ma_modulation_inputs or {}

    def values(self, key: str, protocol_type: str | None = None,
               manufacturer: str | None = None) -> list[str]:
        if manufacturer and key in ("auto_kvp_selection", "auto_ma_modulation"):
            vendor = (self.vendor.get(manufacturer) or {}).get(key)
            if vendor:
                return list(vendor)
        opts = (self.cats.get(key) or {}).get("options") or []
        out = []
        for o in opts:
            types = o.get("applicable_protocol_types") or []
            if protocol_type and types and protocol_type not in types:
                continue
            out.append(str(o.get("value")))
        return out

    def display(self, key: str) -> dict[str, str]:
        return {str(o.get("value")): str(o.get("display"))
                for o in (self.cats.get(key) or {}).get("options") or []}

    def match(self, key: str, value: Any, **kw) -> str | None:
        """The option equal to *value* (case/space-insensitive, by value or
        display), else None."""
        if value in (None, ""):
            return None
        want = " ".join(str(value).split()).casefold()
        disp = self.display(key)
        for v in self.values(key, **kw):
            if want in (" ".join(v.split()).casefold(),
                        " ".join(disp.get(v, v).split()).casefold()):
                return v
        return None

    def to_dict(self) -> dict:
        return {"categories": list(self.cats.values()),
                "manufacturer_field_options": self.vendor,
                "ma_modulation_inputs": self.ma_inputs}


# ------------------------------------------------------------------ helpers
def _num(v: Any) -> float | None:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _fmt(x: float) -> str:
    return f"{x:g}"


def _field(value: Any, prov: str, note: str = "") -> dict:
    return {"value": value, "source": prov if value not in (None, "", {}) else FILL,
            "note": note}


def _nearest_option(choices: Choices, key: str, x: float | None, tol: float,
                    **kw) -> tuple[str | None, str]:
    """Closest numeric option within *tol*; else (Other, text)."""
    if x is None:
        return None, ""
    best, dist = None, None
    for v in choices.values(key, **kw):
        n = _num(v)
        if n is not None and (dist is None or abs(n - x) < dist):
            best, dist = v, abs(n - x)
    if best is not None and dist is not None and dist <= tol:
        return best, ""
    return (OTHER if OTHER in choices.values(key, **kw) else None), _fmt(x)


def _range_option(choices: Choices, key: str, x: float | None) -> str | None:
    """Options like '0.5-0.8' / '>1.5' / '<5 kg' for a number."""
    if x is None:
        return None
    for v in choices.values(key):
        m = re.fullmatch(r"\s*([\d.]+)\s*[-–]\s*([\d.]+)\s*", v)
        if m and float(m.group(1)) <= x < float(m.group(2)) + 1e-9:
            return v
        m = re.fullmatch(r"\s*>\s*([\d.]+)\s*", v)
        if m and x > float(m.group(1)):
            return v
    return None


def _diagnostic_series(exam: dict) -> dict | None:
    series = [s for s in exam.get("series") or [] if not s.get("is_topogram")]
    series.sort(key=lambda s: (s.get("series_number") is None, s.get("series_number") or 0))
    return series[0] if series else None


def _acquisitions(exam: dict) -> list[dict]:
    acqs = (exam.get("rdsr") or {}).get("acquisitions") or []
    return [a for a in acqs if "constant angle" not in str(a.get("acquisition_type")).casefold()
            and str(a.get("acquisition_protocol") or "").casefold() != "topogram"]


def _mode(values: list) -> Any:
    vals = [v for v in values if v is not None]
    return Counter(vals).most_common(1)[0][0] if vals else None


# ------------------------------------------------------------------ classifiers
def protocol_type_for(region: str | None, age: float | None,
                      weight: float | None) -> tuple[str | None, str]:
    head = any(h in str(region or "").casefold() for h in HEAD_REGIONS)
    if head:
        if age is None:
            return None, "Patient age unknown"
        return ("PEDIATRIC_HEAD" if age < 18 else "YOUNG_ADULT"), ""
    if not region:
        return None, "Region unknown — choose the protocol type"
    if weight is not None:
        return ("PEDIATRIC_BODY" if weight <= 80 else "YOUNG_ADULT"), ""
    if age is not None:
        return ("PEDIATRIC_BODY" if age < 18 else "YOUNG_ADULT"), "Weight unknown"
    return None, "Patient age and weight unknown"


def age_group_for(ptype: str | None, age: float | None, weight: float | None) -> str | None:
    if ptype == "PEDIATRIC_HEAD" and age is not None:
        return ("lt_3m" if age < 0.25 else "3m_1y" if age < 1 else
                "1y_6y" if age < 6 else "gt_6y")
    if ptype == "PEDIATRIC_BODY" and weight is not None:
        return ("lt_5kg" if weight < 5 else "5_15kg" if weight < 15 else
                "15_30kg" if weight < 30 else "30_50kg" if weight < 50 else "50_80kg")
    if ptype == "YOUNG_ADULT":
        return "gt_80kg"
    return None


_GROUP_KEY = {"PEDIATRIC_HEAD": "examination_group_pediatric_head",
              "PEDIATRIC_BODY": "examination_group_pediatric_body",
              "YOUNG_ADULT": "examination_group_young_adult"}
_AGE_KEY = {"PEDIATRIC_HEAD": "age_group_pediatric_head",
            "PEDIATRIC_BODY": "age_group_pediatric_body",
            "YOUNG_ADULT": "age_group_young_adult"}


def examination_group_for(choices: Choices, ptype: str | None,
                          age_group: str | None) -> str | None:
    """The examination group in the same position as the age band (RHYTHM
    lists both in matching order); the single option for young adults."""
    if not ptype:
        return None
    groups = choices.values(_GROUP_KEY[ptype], protocol_type=ptype)
    ages = choices.values(_AGE_KEY[ptype], protocol_type=ptype)
    if len(groups) == 1:
        return groups[0]
    if age_group in ages and len(groups) == len(ages):
        return groups[ages.index(age_group)]
    return None


def split_kernel(kernel: Any) -> tuple[str, str | None]:
    """(kernel, iterative strength) from a ConvolutionKernel value, which
    may be multi-valued — Siemens writes ``['Br40f', '3']`` (kernel + ADMIRE
    strength) — and may arrive as that list's string form."""
    if isinstance(kernel, (list, tuple)):
        parts = [str(p) for p in kernel]
    else:
        parts = re.findall(r"[A-Za-z]*\d+[A-Za-z]*", str(kernel or "")) or [str(kernel or "")]
    k = parts[0].strip() if parts else ""
    strength = parts[1].strip() if len(parts) > 1 and parts[1].strip().isdigit() else None
    return k, strength


def kernel_class_for(kernel: str | None) -> tuple[str | None, str]:
    """Siemens-style kernels: letter(s) + sharpness number (+ s/f/h)."""
    k, _strength = split_kernel(kernel)
    m = re.match(r"([A-Za-z]+)(\d{2})", k)
    if not m:
        return None, f"Kernel '{k}' not recognised" if k else "Kernel unknown"
    letters, sharp = m.group(1).upper(), int(m.group(2))
    if letters in ("B", "BR", "BV", "I", "Q") and sharp >= 60 or sharp >= 70:
        return "Bone", ""
    if letters in ("BL",) or (letters in ("B", "BR", "I") and sharp >= 50):
        return "Lung", ""
    if sharp <= 20:
        return "Very soft", ""
    if sharp <= 35:
        return "Soft tissue", ""
    if sharp <= 45:
        return "Standard", ""
    return "Detail", ""


def recon_algorithm_for(kernel: Any, family: str | None) -> tuple[str | None, str]:
    k, strength = split_kernel(kernel)
    k = k.upper()
    if family == "siemens" and strength:
        return "Model-based iterative reconstruction", f"ADMIRE strength {strength}"
    if family == "siemens" and k:
        # SAFIRE/ADMIRE kernels: I__ / J__ / Q__ (and Hr/Br/Qr with a strength
        # in the description); classic B/H/C/D/T/U kernels → FBP.
        if re.match(r"[IJQ]\d{2}", k):
            return "Hybrid iterative reconstruction", "SAFIRE/ADMIRE kernel"
        if re.match(r"(B|H|C|D|U)\d{2}", k):
            return "Filtered back projection", "classic Siemens kernel"
    return None, "Reconstruction algorithm not derivable from the headers"


def modulation_for(choices: Choices, manufacturer: str | None, rdsr_type: str | None,
                   header_type: str | None) -> tuple[str | None, str]:
    text = " ".join(str(x or "") for x in (rdsr_type, header_type)).upper()
    opts = choices.values("auto_ma_modulation", manufacturer=manufacturer)
    if not text.strip():
        return None, "mA modulation not recorded"
    if text.strip() in ("OFF", "NONE"):
        return choices.match("auto_ma_modulation", "Off", manufacturer=manufacturer), ""
    if manufacturer == "Siemens Healthineers" and ("XYZ" in text or "CARE" in text
                                                   or "ANGULAR" in text):
        for want in ("CareDose4D", "Siemens CARE Dose4D"):
            hit = choices.match("auto_ma_modulation", want, manufacturer=manufacturer)
            if hit:
                return hit, f"recorded as {text.strip()}"
    return (OTHER if OTHER in opts else None), f"recorded as {text.strip()}"


def scan_fov_for(choices: Choices, diameter: float | None, head: bool,
                 pediatric: bool) -> str | None:
    if diameter is None:
        return None
    if head:
        want = "Small head" if diameter < 200 else "Head" if diameter <= 280 else "Large head"
    elif pediatric:
        want = "Pediatric body"
    else:
        want = ("Small body" if diameter < 350 else "Medium body" if diameter < 450
                else "Large body")
    return choices.match("scan_fov", want)


# ------------------------------------------------------------------ draft
def draft_from_exam(exam: dict, headers: dict, choices: Choices, catalog_doc: dict,
                    study_number: int | None = None) -> dict[str, Any]:
    patient = exam.get("patient") or {}
    age, weight = _num(patient.get("age_years")), _num(patient.get("weight_kg"))
    indication = exam.get("indication") or {}
    scanner = exam.get("scanner") or {}
    hint = exam.get("protocol_used_hint") or {}
    manufacturer = scanner.get("manufacturer") or headers.get("Manufacturer")
    model = scanner.get("model") or headers.get("ManufacturerModelName")
    family = manufacturer_family(manufacturer)
    rhythm_manu = RHYTHM_MANUFACTURER.get(family or "")

    name = (hint.get("protocol_name")
            or protocol_name_from_description(hint.get("study_description"))
            or headers.get("ProtocolName") or "")
    region = indication.get("region")
    ci = indication.get("clinical_indication")
    iv = indication.get("iv_contrast")
    if not iv and headers.get("ContrastBolusAgent"):
        iv = "Contrast-enhanced"

    # ---- local protocol
    local_scanner = None
    for s in catalog_doc.get("scanners") or []:
        if (str(s.get("manufacturer") or "").casefold() == str(manufacturer or "").casefold()
                and str(s.get("model") or "").casefold() == str(model or "").casefold()
                and (manufacturer or model)):
            local_scanner = s.get("id")
            break
    local = {
        "names": [name] if name else [],
        "label": name,
        "key": slugify(name) if name else "",
        "region": region, "clinical_indication": ci, "iv_contrast": iv,
        "scanner_id": local_scanner,
        "new_scanner": None if local_scanner else {
            "manufacturer": manufacturer or "", "model": model or "",
            "name": f"{manufacturer or ''} {model or ''}".strip()},
    }

    # ---- RHYTHM protocol
    f: dict[str, dict] = {}
    ptype, why = protocol_type_for(region, age, weight)
    f["protocol_type"] = _field(ptype, GUESS, why or (
        f"age {_fmt(age)} y" if age is not None else "") + (
        f", weight {_fmt(weight)} kg" if weight is not None else ""))
    ag = age_group_for(ptype, age, weight)
    ag = ag if ag in choices.values(_AGE_KEY.get(ptype or "", ""), protocol_type=ptype) else None
    f["age_group"] = _field(ag, STUDY)
    f["examination_group"] = _field(examination_group_for(choices, ptype, ag), GUESS,
                                    "same position as the age group")

    acqs = _acquisitions(exam)
    series = _diagnostic_series(exam) or {}

    kinds = {str(a.get("acquisition_type") or "").casefold() for a in acqs}
    if any("spiral" in k or "helical" in k for k in kinds):
        st = choices.match("scan_type", "Helical / spiral")
    elif any("sequen" in k or "axial" in k for k in kinds):
        st = choices.match("scan_type", "Sequential / axial")
    elif series.get("spiral_pitch_factor"):
        st = choices.match("scan_type", "Helical / spiral")
    else:
        st = None
    f["scan_type"] = _field(st, STUDY)

    contrast = ("IV contrast only" if str(iv or "").casefold().startswith("contrast")
                else "No contrast" if iv else None)
    f["contrast"] = _field(choices.match("contrast", contrast), STUDY)

    n_events = len(acqs)
    phases = (choices.match("number_of_phases", str(n_events)) if 0 < n_events <= 5
              else choices.match("number_of_phases", ">5 / dynamic") if n_events > 5 else None)
    f["number_of_phases"] = _field(phases, STUDY,
                                   f"{n_events} irradiation event(s)" if n_events else "")

    kvp = _mode([_num(a.get("kvp")) for a in acqs]) or _num(headers.get("KVP"))
    kv_opt, kv_other = _nearest_option(choices, "kvp", kvp, 0.5)
    f["kvp"] = _field(kv_opt, STUDY, f"{_fmt(kvp)} kV" if kvp else "")
    if kv_other:
        f["kvp"]["other"] = kv_other

    f["auto_kvp_selection"] = _field(None, FILL, "Not recorded in the images (CARE kV on/off?)")
    f["auto_kvp_selection"]["options_for"] = rhythm_manu

    mod, mod_note = modulation_for(choices, rhythm_manu,
                                   _mode([a.get("modulation_type") for a in acqs]),
                                   headers.get("ExposureModulationType"))
    f["auto_ma_modulation"] = _field(mod, GUESS, mod_note)
    f["auto_ma_modulation"]["options_for"] = rhythm_manu

    pitch = _mode([_num(a.get("pitch")) for a in acqs]) or _num(series.get("spiral_pitch_factor"))
    if st and "sequential" in st.casefold():
        f["pitch"] = _field(choices.match("pitch", "N/A sequential"), STUDY)
    else:
        f["pitch"] = _field(_range_option(choices, "pitch", pitch), STUDY,
                            f"pitch {_fmt(pitch)}" if pitch else "")

    rot = _mode([_num(a.get("rotation_time_s")) for a in acqs])
    rot_opt, rot_other = _nearest_option(choices, "rotation_time", rot, 0.011)
    f["rotation_time"] = _field(rot_opt, STUDY, f"{_fmt(rot)} s" if rot else "")
    if rot_other:
        f["rotation_time"]["other"] = rot_other

    thick = _num(series.get("slice_thickness_mm"))
    th_opt, th_other = _nearest_option(choices, "slice_thickness", thick, 0.011)
    f["slice_thickness"] = _field(th_opt, STUDY,
                                  f"series {series.get('series_number')}: "
                                  f"{series.get('series_description') or ''}".strip()
                                  if series else "")
    if th_other:
        f["slice_thickness"]["other"] = th_other

    kernel = series.get("convolution_kernel") or headers.get("ConvolutionKernel")
    kc, kc_note = kernel_class_for(kernel)
    f["kernel_class"] = _field(choices.match("kernel_class", kc), GUESS,
                               f"kernel {split_kernel(kernel)[0]}" if kernel else kc_note)
    ra, ra_note = recon_algorithm_for(kernel, family)
    f["reconstruction_algorithm"] = _field(choices.match("reconstruction_algorithm", ra),
                                           GUESS, ra_note)

    head = any(h in str(region or "").casefold() for h in HEAD_REGIONS)
    # ReconstructionDiameter is the displayed FOV; DataCollectionDiameter is
    # usually the whole gantry field (~500 mm) and says little.
    diameter = _num(headers.get("ReconstructionDiameter"))
    f["scan_fov"] = _field(scan_fov_for(choices, diameter, head, ptype != "YOUNG_ADULT"),
                           GUESS, f"reconstruction diameter {_fmt(diameter)} mm"
                           if diameter else "")
    intent = "Trauma" if "trauma" in str(ci or "").casefold() else (
        "Pediatric optimized" if ptype and ptype.startswith("PEDIATRIC") else None)
    f["protocol_intent"] = _field(choices.match("protocol_intent", intent), GUESS)

    # mAs inputs for the chosen modulation
    labels = choices.ma_inputs.get(mod or "", []) if mod else []
    mean_ma = _mode([_num(a.get("mean_ma")) for a in acqs])
    mas: dict[str, str] = {}
    for label in labels:
        low = label.casefold()
        if mean_ma and rot and ("ref" in low or "effective" in low) and pitch:
            mas[label] = _fmt(round(mean_ma * rot / pitch))
        elif mean_ma and low == "ma":
            mas[label] = _fmt(round(mean_ma))
        else:
            mas[label] = ""
    f["mas_inputs"] = {"value": mas, "labels": labels,
                       "source": GUESS if any(mas.values()) else FILL,
                       "note": ("effective mAs from the RDSR mean mA × rotation / pitch — "
                                "replace with the console's reference value")
                       if any(mas.values()) else ""}

    ctdi = [a.get("mean_ctdivol_mgy") for a in acqs if a.get("mean_ctdivol_mgy")]
    dlp = (exam.get("rdsr") or {}).get("total_dlp_mgy_cm")
    note_bits = [f"Drafted by PixieVeil from study {study_number:04d}" if study_number
                 else "Drafted by PixieVeil"]
    if ctdi:
        note_bits.append("CTDIvol " + ", ".join(f"{c:g}" for c in ctdi) + " mGy")
    if dlp:
        note_bits.append(f"total DLP {dlp:g} mGy·cm")
    rhythm = {
        "fields": f,
        "protocol_name": name,
        "anatomical_region": region,
        "clinical_indication": ci,
        "notes": "; ".join(note_bits),
        "manufacturer": rhythm_manu,
        "model": model,
    }
    missing = [k for k in REQUIRED if f.get(k, {}).get("source") == FILL]
    return {"local": local, "rhythm": rhythm, "missing": missing,
            "patient": {"age_years": age, "weight_kg": weight}}


# ------------------------------------------------------------------ submit
def validate_submission(rhythm: dict, choices: Choices) -> tuple[dict, dict[str, str]]:
    """Turn the form's ``rhythm`` block into the RHYTHM REST payload.

    ``rhythm`` = ``{"scanner": profile id, "protocol_type", "fields": {key:
    {"value", "other"?}}, "mas_inputs": {...}, "protocol_name",
    "anatomical_region", "clinical_indication", "notes"}``. Returns
    (payload, errors by field). "Other: Please Specify" needs its text, which
    is sent in place of the option (as RHYTHM's GUI does).
    """
    errors: dict[str, str] = {}
    ptype = rhythm.get("protocol_type")
    if ptype not in PROTOCOL_TYPES:
        errors["protocol_type"] = "Choose the protocol type"
    if not rhythm.get("scanner"):
        errors["scanner"] = "Choose the RHYTHM scanner profile"
    manu = rhythm.get("manufacturer")
    fields = rhythm.get("fields") or {}
    payload: dict[str, Any] = {
        "scanner": rhythm.get("scanner"), "protocol_type": ptype,
        "protocol_name": str(rhythm.get("protocol_name") or "").strip(),
        "anatomical_region": str(rhythm.get("anatomical_region") or "").strip(),
        "clinical_indication": str(rhythm.get("clinical_indication") or "").strip(),
        "notes": str(rhythm.get("notes") or "").strip(),
    }
    if not payload["protocol_name"]:
        errors["protocol_name"] = "Protocol name is required"
    keys = set(REQUIRED) | set(TECHNIQUE_FIELDS)
    for key in sorted(keys):
        entry = fields.get(key) or {}
        value = str(entry.get("value") or "").strip()
        other = str(entry.get("other") or "").strip()
        if not value:
            if key in REQUIRED:
                errors[key] = "Required"
            continue
        if key in ("examination_group", "age_group") and ptype in PROTOCOL_TYPES:
            ck = (_GROUP_KEY if key == "examination_group" else _AGE_KEY)[ptype]
            allowed = choices.values(ck, protocol_type=ptype)
        else:
            allowed = choices.values(key, protocol_type=ptype, manufacturer=manu)
        if allowed and value not in allowed:
            errors[key] = f"'{value}' is not a RHYTHM option"
            continue
        if value == OTHER:
            if not other:
                errors[key] = "Specify the value"
                continue
            value = other
        payload[key] = value
    labels = choices.ma_inputs.get(str((fields.get("auto_ma_modulation") or {})
                                       .get("value") or ""), [])
    mas = rhythm.get("mas_inputs") or {}
    payload["mas_inputs"] = {}
    for label in labels:
        v = str(mas.get(label) or "").strip()
        if not v:
            errors[f"mas_inputs.{label}"] = f"{label} is required"
        payload["mas_inputs"][label] = v
    return payload, errors
