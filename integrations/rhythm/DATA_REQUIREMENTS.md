# RHYTHM data requirements: automatic vs. manual

Two RHYTHM forms need data from us: the **Protocol GUI** (one record per
scanner × indication × age/weight group — a *template*, filled in once
per combination) and **Manual Exam Entry** (one record per actual patient
exam). This doc tracks, field by field, what `ExamExtractor`
(`pixieveil/processing/exam_extractor.py`) already pulls from DICOM
headers automatically, what it can only partially resolve, and what has
to come from a person.

**Workflow this assumes:** tech fills in a paper note at the console during/after
the scan (the numbers that live only there — rotation time, console dose
screen, etc.) → that paper gets transcribed into the `<NNNN>_exam.json`
sidecar PixieVeil already writes → the merged sidecar is what gets typed
into the RHYTHM web forms. The "entered into sidecar" transcription step
isn't built yet; this doc is what it needs to fill in.

---

## Manual Exam Entry (per patient exam)

| RHYTHM field | Status | Source / notes |
|---|---|---|
| Region / Clinical Indication | 🟡 Partial | Guessed from `ProtocolName`/`StudyDescription`/`BodyPartExamined` via `integrations/rhythm/indication_lookup.yaml`. Only as good as the curated rules — **needs review**, not blind trust, until the lookup table is well populated. |
| IV Contrast | 🟢 Automatic | `ContrastBolusAgent` non-empty → Contrast-enhanced. Only detects IV; doesn't distinguish oral/rectal contrast. |
| Protocol Type (Pediatric Head/Body/Young Adult) | 🟡 Partial | Derived from indication + age/weight. The Pediatric-vs-Young-Adult split is a **heuristic** (age ≥ 18 or weight > 80 kg) — RHYTHM's own GUI never auto-picks this either; a person should confirm it. |
| Examination Group (age/weight bucket) | 🟡 Partial | Bucketed from age or weight once the above is known. Null if age/weight is missing. |
| CT Scanner | 🟡 Partial | `Manufacturer`+`ManufacturerModelName` → `scanner_lookup.yaml`. Currently **empty** — real registered scanner UUID not yet confirmed (see note below). |
| Protocol Used (optional link) | 🔴 Manual | We only surface a text hint (`ProtocolName`/`SeriesDescription`); matching it to a *saved RHYTHM protocol record* needs a person, or a second lookup table once protocol UUIDs are known. |
| Patient Weight (kg) | 🟡 Partial | `PatientWeight` tag — **absent on most of our sampled Emotion 16 studies** (~22% populated in the broader sample). Needs to be read off the console/paper when missing. |
| Patient Age (years) | 🟢 Automatic | `PatientAge`, parsed from Y/M/W/D. |
| CTDI_vol (mGy), per phase | 🟢 Automatic | Max `CTDIvol` per series. One phase only really makes sense for single-acquisition studies (e.g. non-contrast Head/Trauma); multi-phase contrast studies need per-phase mapping reviewed, since we currently emit one entry per DICOM series, not per clinical "phase." |
| DLP (mGy·cm), per phase | 🔴 Manual | **No standard DICOM tag on image datasets.** Only available if the scanner sends a Dose SR (RDSR) object, which it currently doesn't appear to. Must be read off the scanner's console dose summary screen and written down at scan time — it can't be reconstructed later from PACS. |
| Image Quality | 🔴 Manual | Subjective judgment call — never automatable. |
| Study Set (zip/tar upload) | 🟢 Automatic (to prepare) | PixieVeil already has the anonymized files (`ZipManager`); a person still has to pick the file in the browser upload dialog. |
| Repository Study ID | N/A | RHYTHM generates this itself on save — not something we provide. |

🟢 automatic · 🟡 partial (needs a lookup table or has real gaps) · 🔴 always manual

---

## Protocol GUI (one record per scanner × indication × age/weight group — not per exam)

This is filled in once per combination, not per patient, so it's not part of
the sidecar/paper-note loop below. The `integrations/rhythm/*_protocol.yaml`
skeletons already list every field with its valid options; the short version:

| Field | Status |
|---|---|
| Region / indication / IV contrast / comments | 🟢 fixed, already filled in the skeleton YAMLs |
| Scanner | 🟡 same scanner-identity caveat as above |
| Scan Type, Automatic kVp Selection, kVp, Automatic mA Modulation, mAs value(s), Pitch, Rotation Time, Slice Thickness, Kernel, Reconstruction Algorithm, Strength | 🔴 all manual — these describe the *intended, standard* protocol on the console, not any one exam's actual values, so they can't come from a DICOM header at all. Someone with console/physicist access fills these in once per combination. |

---

## What the console/paper note needs to record

DICOM cannot answer these at all, or can't answer them reliably — they only
exist on the console screen at scan time:

1. **Rotation time (s)** — confirmed absent from this scanner's headers entirely (checked real files: tag not written). Must be read off the console.
2. **Actual auto-kVp / auto-mA mode used** (e.g. "CareDose4D" vs "CareDose", "CareKV" vs "CareKV Semi") — DICOM only gives a coarse code (`ExposureModulationType`, e.g. `Z_EC`), not the console's mode label. Write down exactly what was selected.
3. **The mAs value(s) tied to that mode** (Effective mAs / Quality Reference mAs / etc., per `MA_INPUT_SPECS` in the GUI) — not the same as the DICOM `Exposure` tag, which is the realized per-image value, not the console setting.
4. **DLP**, read directly from the console's dose summary screen (see table above).
5. **Patient weight**, when the console didn't log it (frequent gap).
6. **Image quality** rating.
7. **Which saved protocol was intended** (so it can be linked in "Protocol Used"), and **confirmation of Pediatric vs. Young Adult** — override our heuristic rather than trusting it blindly.
8. **Reconstruction algorithm / kernel**, if it can vary per exam rather than being fixed per protocol (on older FBP-only scanners like the Emotion 16 it's probably constant — worth confirming once, not re-writing every time).

## Additional things worth noting on the paper, beyond what RHYTHM's forms ask for

These aren't RHYTHM fields, but they'll save real time once this becomes a
regular workflow:

- **PixieVeil study number** (the `NNNN` in `<NNNN>_exam.json`) — without this, there's no reliable way to match a paper note back to the right anonymized study once several exams have queued up. This is the single most important thing to write down; everything else is data, this is the *key*.
- **Exam date** — RHYTHM's exam form doesn't ask for it, but it's useful for sanity-checking that the paper note actually corresponds to the sidecar you're merging it into (`StudyDate` is in the sidecar already; cross-check against what's on the paper).
- **Any repeat/redo acquisitions** (motion, positioning) — if a series was reacquired, the dose and image-quality numbers should reflect the *final diagnostic* run, not an average of all of them. Flag which series number is the one that counts.
- **Deviation notes** — free text for "protocol X was used but Y was changed because Z" (e.g., bumped mAs for a large/uncooperative patient). This context is what later explains outlier dose values instead of leaving them looking like data-entry errors.
- **Route of contrast**, if given (IV/oral/rectal) — RHYTHM only has a binary contrast flag; ContrastBolusAgent only reliably reflects IV. Worth a note if oral/rectal contrast was also used, since it changes clinical interpretation even if it doesn't change the RHYTHM field.
- **The RHYTHM Repository Study ID, written back after submission** — not something you have *before* filling the form (RHYTHM generates it on save), but recording it afterward against the PixieVeil study number closes the loop and makes future audits/corrections possible without hunting through the RHYTHM UI.
- **Who scanned / entered the data** (initials, not full name) — useful if a question comes up later about a specific record; keep it to initials only so this stays consistent with the anonymization posture of everything else in the pipeline.
