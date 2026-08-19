# RHYTHM data requirements: automatic vs. manual

Two RHYTHM forms need data from us: the **Protocol GUI** (one record per
scanner × indication × age/weight group — a *template*, filled in once
per combination) and **Manual Exam Entry** (one record per actual patient
exam). This doc tracks, field by field, what `ExamExtractor`
(`pixieveil/processing/exam_extractor.py`) already pulls from DICOM
headers automatically, what it can only partially resolve, and what has
to come from a person.

**Workflow this assumes:** tech fills in a paper note at the console during/after
the scan (the numbers that only ever exist there — mainly the console's own
mode labels and mAs setting now; see below) → that paper gets transcribed
into the `<NNNN>_exam.json` sidecar PixieVeil already writes → the merged
sidecar is what gets typed into the RHYTHM web forms. The "entered into
sidecar" transcription step isn't built yet; this doc is what it needs to
fill in.

**Validated against a real exam** (study 0049, 2026-08-18 — full pipeline, not the leftover multi-vendor `data/tmp/` test files used earlier): confirmed real scanner identity (`SIEMENS`/`Emotion 16`, not the "SOMATOM Perspective" label from RHYTHM's sample data), confirmed `PatientWeight` absent and `PatientAge` present/parseable, confirmed post-export retention + exam-sidecar generation both work end-to-end, and — after first assuming the "Dose Report" object was just an unreadable screenshot — discovered it actually carries the full structured dose-report content too (see below). `indication_lookup.yaml` and `scanner_lookup.yaml` now each carry one entry seeded from this run.

**RDSR gap found and fixed at the source (2026-08-18):** checking every series' actual `SOPClassUID` (not just `Modality`) on study 0049 showed PixieVeil's DICOM SCP never advertised support for `XRayRadiationDoseSRStorage` (the real RDSR SOP class), only `CTImageStorage`/`MRImageStorage`/`SecondaryCaptureImageStorage`/`Verification`. A presentation context we never offered gets refused during C-STORE association negotiation. `pixieveil/dicom_server/server.py` now also registers `XRayRadiationDoseSRStorage`.

**Then it turned out we didn't even need that fix to get the data (2026-08-18, same day):** running `dcm2xml` on study 0049's "Dose Report" object (`data/retained/0049/0005/0001.dcm`) showed our Siemens Emotion 16 doesn't send a bare screenshot — it sends a Secondary Capture image with the **full RDSR content tree attached to the same object** (`DerivationDescription: "Convert syngo SR to DICOM SC"`, referencing the original RDSR's SOP instance it was converted from). That tree — real TID 10011/10013 structured content, not OCR-able pixels — was already inside every exam we've ever received; we just weren't reading it. `ExamExtractor` now walks that content tree (`pixieveil/processing/exam_extractor.py`, `_parse_rdsr_content` and friends) and pulls, **per acquisition**: acquisition protocol name, target region, acquisition type (spiral/sequenced), procedure context (contrast), pitch, kVp, mean/max mA, **rotation time**, mean CTDIvol, **DLP**, and X-ray modulation type code. Confirmed on study 0049: 3 acquisitions (topogram + 2 spiral), pitch 0.55, rotation time 1.5 s, kVp 130, DLP 824.14 mGy·cm each. This lands under a new `rdsr` key in the exam sidecar, null when a study doesn't carry this content.

**This also caught a real accuracy bug:** the flat per-image `CTDIvol` tag `ExamExtractor` was reading for `phases[*].ctdi_vol_mgy` turned out to be the **cumulative** dose-to-date on this scanner (42.36 mGy on every image, regardless of acquisition), not the per-acquisition value — `rdsr.acquisitions[*].mean_ctdivol_mgy` (36.58 mGy) is the true per-acquisition figure. `phases[*].ctdi_vol_mgy` is left as-is for reference but a note now tells you to prefer the RDSR value when both are present.

---

## Manual Exam Entry (per patient exam)

| RHYTHM field | Status | Source / notes |
|---|---|---|
| Region / Clinical Indication | 🟡 Partial | Guessed from `ProtocolName`/`StudyDescription`/`BodyPartExamined` via `integrations/rhythm/indication_lookup.yaml`. Only as good as the curated rules — **needs review**, not blind trust, until the lookup table is well populated. |
| IV Contrast | 🟢 Automatic | `ContrastBolusAgent` non-empty → Contrast-enhanced. Only detects IV; doesn't distinguish oral/rectal contrast. |
| Protocol Type (Pediatric Head/Body/Young Adult) | 🟡 Partial | Derived from indication + age/weight. The Pediatric-vs-Young-Adult split is a **heuristic** (age ≥ 18 or weight > 80 kg) — RHYTHM's own GUI never auto-picks this either; a person should confirm it. |
| Examination Group (age/weight bucket) | 🟡 Partial | Bucketed from age or weight once the above is known. Null if age/weight is missing. |
| CT Scanner | 🟡 Partial | `Manufacturer`+`ManufacturerModelName` → `scanner_lookup.yaml`. One entry now filled in (`SIEMENS`/`Emotion 16`), but the UUID is inferred, not independently confirmed against RHYTHM's own scanner dropdown/API — see the caveat in that file. |
| Protocol Used (optional link) | 🔴 Manual | We only surface a text hint (`ProtocolName`/`SeriesDescription`); matching it to a *saved RHYTHM protocol record* needs a person, or a second lookup table once protocol UUIDs are known. |
| Patient Weight (kg) | 🟡 Partial | `PatientWeight` tag — **absent on most of our sampled Emotion 16 studies** (~22% populated in the broader sample). Needs to be read off the console/paper when missing. |
| Patient Age (years) | 🟢 Automatic | `PatientAge`, parsed from Y/M/W/D. |
| CTDI_vol (mGy), per phase | 🟢 Automatic | Prefer `rdsr.acquisitions[*].mean_ctdivol_mgy` (true per-acquisition value) over `phases[*].ctdi_vol_mgy` (the flat per-image `CTDIvol` tag, confirmed **cumulative-to-date** on this scanner, not per-acquisition — see note above). `phases` still excludes topogram/localizer and non-`CT` series so they don't show up as spurious null-dose rows. |
| DLP (mGy·cm), per phase | 🟢 Automatic (when RDSR present) | Comes from `rdsr.acquisitions[*].dlp_mgy_cm` (per acquisition) and `rdsr.total_dlp_mgy_cm` (study total) — see the RDSR discovery note above. Falls back to 🔴 manual, off the console dose screen, only for the rare study where no RDSR content is found (`rdsr: null` in the sidecar, with a note explaining why). |
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
| Scan Type, kVp, Pitch, Rotation Time | 🟡 seedable — these describe the *intended, standard* protocol, not any one exam, so there's no field that's "the" answer, but a representative real exam's `rdsr.acquisitions[*]` (`acquisition_type`/`kvp`/`pitch`/`rotation_time_s`) is real evidence of what the standing protocol actually runs, not a guess from memory. |
| Automatic kVp Selection, Automatic mA Modulation (console mode label) | 🔴 manual — the RDSR only gives the DICOM-standard modulation code (e.g. `Z_EC`), not the console's own label (CareDose vs CareDose4D, CareKV vs CareKV Semi); a person still has to say which one that code corresponds to on this scanner. |
| mAs value(s), Slice Thickness, Kernel, Reconstruction Algorithm, Strength | 🔴 manual — mAs is patient-specific by design (that's what automatic modulation does); the rest aren't in the RDSR content template at all. Someone with console/physicist access fills these in once per combination. |

---

## What the console/paper note needs to record

Now shorter than it used to be — rotation time and DLP turned out to be
recoverable from the RDSR content tree (see above), so they've moved off
this list. What's left genuinely only exists on the console screen:

1. **Actual auto-kVp / auto-mA mode used** (e.g. "CareDose4D" vs "CareDose", "CareKV" vs "CareKV Semi") — the RDSR gives a coarse standard code (`X-Ray Modulation Type`, e.g. `Z_EC`), not the console's own mode label. Write down exactly what was selected.
2. **The mAs value(s) tied to that mode** (Effective mAs / Quality Reference mAs / etc., per `MA_INPUT_SPECS` in the GUI) — the RDSR's `mean_ma`/`max_ma` are the *realized* current in mA, not the console's target mAs setting.
3. **DLP and rotation time — only when `rdsr` comes back `null`** in the exam sidecar (i.e. this particular study didn't carry RDSR content at all). Check the sidecar first before writing these down; on our Emotion 16 they're normally already there.
4. **Patient weight**, when the console didn't log it (frequent gap).
5. **Image quality** rating.
6. **Which saved protocol was intended** (so it can be linked in "Protocol Used"), and **confirmation of Pediatric vs. Young Adult** — override our heuristic rather than trusting it blindly.
7. **Reconstruction algorithm / kernel**, if it can vary per exam rather than being fixed per protocol (on older FBP-only scanners like the Emotion 16 it's probably constant — worth confirming once, not re-writing every time).

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
