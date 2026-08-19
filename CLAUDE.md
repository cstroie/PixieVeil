# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this project is

PixieVeil is a DICOM anonymization server: it receives medical images via DICOM C-STORE, anonymizes them (profile-based), optionally defaces head scans with nnU-Net, organizes them into a numbered study/series hierarchy, and exports completed studies to a remote DICOM node or HTTP endpoint. A web dashboard exposes live metrics.

## Commands

```bash
# Bootstrap: .python venv (python3.12) + pip install -e . + install.py
./install

# Run the server directly
python pixieveil.py

# Or via the control script (foreground; writes/checks a pidfile)
./pixieveil.sh start    # also: stop, restart, status

# Interactive setup (installs torch/nnunetv2, downloads nnUNet model) —
# normally run automatically by ./install, but can be re-run standalone
python install.py

# Download defacing model only
python install.py --download-model

# Install core dependencies without the ./install wrapper
pip install -e .

# Install the deface extra (simpleitk/nibabel/numpy/gdown/nnunetv2) — still
# needs install.py afterwards for a CUDA-matched torch build
pip install -e ".[deface]"
```

See [INSTALL.md](INSTALL.md) for the full bootstrap/systemd/OpenRC flow.

No automated test suite exists. Manual linting:

```bash
flake8 pixieveil/ --max-line-length=100
mypy pixieveil/
```

## Architecture

```
Modality ──C-STORE──► DicomServer (pynetdicom)
                           │
                      CStoreSCPHandler
                           │
                      StorageManager.process_image()
                        ├─ SeriesFilter.should_filter()
                        ├─ Anonymizer.anonymize()
                        └─ StudyManager.add_image_to_study()
                           │
                  (asyncio.create_task per study)
                  StorageManager._process_study()
                        ├─ ExamExtractor.extract()        [asyncio.to_thread] → <NNNN>_exam.json
                        ├─ Defacer.deface_series()        [asyncio.to_thread]
                        ├─ DicomStorage.send_study()      [asyncio.to_thread]
                        └─ ZipManager → RemoteStorage     [asyncio.to_thread]
```

Single asyncio event loop. All blocking I/O (nnUNet inference, ZIP, file I/O) uses `asyncio.to_thread`. GPU jobs are serialized via a class-level `threading.Semaphore(1)` on `Defacer`.

## Module map

| Path | Purpose |
|------|---------|
| `pixieveil.py` | Entry point |
| `pixieveil/config/settings.py` | Loads/validates `config/settings.yaml` via pydantic |
| `pixieveil/dicom_server/server.py` | DICOM SCP (C-ECHO + C-STORE). Supported storage SOP classes: CT, MR, Secondary Capture, X-Ray Radiation Dose SR (RDSR) — anything else is refused at association negotiation, not received-then-filtered |
| `pixieveil/dicom_server/handlers.py` | C-STORE event handler |
| `pixieveil/processing/anonymizer.py` | Profile-based field transforms (PSEUDO/PSEUDOUID/NEWUID/CLEAR/KEEP) |
| `pixieveil/processing/series_filter.py` | Modality, image-type, and regex include/exclude filtering |
| `pixieveil/processing/study_manager.py` | Study/series numbering, completion detection, sidecar I/O |
| `pixieveil/processing/defacer.py` | nnU-Net head-scan defacing (DICOM↔NIfTI + mask application) |
| `pixieveil/processing/exam_extractor.py` | Derives RHYTHM exam-entry fields (indication, protocol type/group, scanner, dose, patient age/weight) from a completed study's DICOM headers |
| `pixieveil/storage/storage_manager.py` | Central pipeline and export orchestration |
| `pixieveil/storage/study_sidecar.py` | Atomic per-study JSON sidecar (crash recovery) |
| `pixieveil/storage/exam_sidecar.py` | Atomic `<study_number>_exam.json` sidecar holding ExamExtractor's output |
| `pixieveil/storage/dicom_storage.py` | DICOM C-STORE export to remote node |
| `pixieveil/storage/remote_storage.py` | HTTP multipart ZIP upload |
| `pixieveil/storage/zip_manager.py` | ZIP archive creation |
| `pixieveil/dashboard/server.py` | aiohttp web server: `/`, `/stats`, `/health`, plus the `/studies` review page and its `/api/studies*` JSON API (list/detail/save/actions) |
| `pixieveil/processing/exam_merge.py` | Manual-edit/provenance-overlay semantics for the exam sidecar — shared by the `/api/studies/{n}/exam` save handler and the manual re-extract action |

## Key design decisions

**Sidecar files** — Each study has `<study_number>.json` written atomically (write-to-tmp + rename). Tracks `status` (`receiving → complete → defacing → archived`), `archived_via` (`"dicom"` / `"http"` / `null`), and per-series defacing progress. On restart, `StudyManager.initialize_from_sidecars()` re-queues any study that did not finish.

**Export priority** — DICOM C-STORE takes priority over HTTP ZIP upload when both are configured. If neither is configured, archives are kept locally.

**RHYTHM exam extraction** — On every completed study, `ExamExtractor.extract()` reads the anonymized DICOM headers under `study_dir` and writes `<study_number>_exam.json` (JSON, not YAML — machine-written/machine-read, mirrors `StudySidecar`'s format) with whatever RHYTHM Manual-Exam-Entry fields are derivable (contrast, patient age, CTDIvol, scanner make/model, indication/protocol-type/exam-group when `integrations/rhythm/indication_lookup.yaml` and `scanner_lookup.yaml` have a matching curated rule). **Terminology**: the sidecar's `series` list is keyed by DICOM `SeriesNumber` — it is *not* the same thing as an irradiation event. One RDSR acquisition (one real dose event) can be reconstructed into several DICOM series at different kernel/thickness, so several `series[]` entries legitimately share one event; `rdsr.acquisitions[]` is the authoritative per-irradiation-event record. Every CT-modality series — including topograms/localizers — gets a `series[]` entry flagged `is_topogram`; a topogram is a real irradiation event and stays in the dose record rather than being dropped. Only genuinely non-CT objects riding along in the study directory (e.g. a rendered Dose Report screen capture) are excluded. Each `series[]` entry also carries per-series technique parameters read straight off the image headers — `slice_thickness_mm`, `exposure_time_ms`, `convolution_kernel`, `patient_position`, `single_collimation_width_mm`, `total_collimation_width_mm`, `spiral_pitch_factor` — and a best-effort `dlp_mgy_cm` estimated as CTDIvol × the z-extent spanned by `ImagePositionPatient` across the series (skipped for topograms, where that relationship doesn't hold); `notes` flags it as an estimate and says to prefer `rdsr.acquisitions[*].dlp_mgy_cm` when an RDSR is present. Fields it still cannot determine (image quality, unmatched indications/scanners) are left `null` with an explanation in `notes`. Runs first in `_process_study()` — before defacing, not just before export — since it only needs anonymized headers (defacing rewrites `PixelData` only, never metadata); this way the sidecar survives a defacing crash and isn't blocked behind CPU-mode nnU-Net inference. Skipped if the sidecar already exists, so a re-queued retry never overwrites paper-note data merged in later.

**RDSR content parsing** — Confirmed on our Siemens Emotion 16: its "Dose Report" object is `SOPClassUID`-wise a Secondary Capture image (a rendered screenshot), but the same object also carries the full X-Ray Radiation Dose Report structured-content tree (TID 10011/10013) that a native RDSR would — Siemens converts SR→SC for compatibility but keeps the content sequence attached (`DerivationDescription: "Convert syngo SR to DICOM SC"`). `ExamExtractor._parse_rdsr_content()` walks that tree by DCM concept code (works on a native RDSR too, not just this vendor quirk) and populates the sidecar's `rdsr` key: `total_dlp_mgy_cm` plus, per acquisition, `pitch`, `kvp`, `mean_ma`/`max_ma`, `rotation_time_s`, `mean_ctdivol_mgy`, `dlp_mgy_cm`, `acquisition_type`, `modulation_type`. This is why the DICOM SCP (`pixieveil/dicom_server/server.py`) also registers `XRayRadiationDoseSRStorage` now — without that presentation context a *native* RDSR would be refused at C-STORE association negotiation even though this vendor's SC-hybrid already got through on the pre-existing `SecondaryCaptureImageStorage` context. Important caveat this surfaced: the flat per-image `CTDIvol` tag on this scanner is **cumulative dose-to-date**, not per-acquisition — always prefer `rdsr.acquisitions[*].mean_ctdivol_mgy` over `series[*].ctdi_vol_mgy` when both are present.

**Post-export retention** — `storage.retain_after_export_days` (optional) keeps a copy of each already-anonymized study under `storage.retain_path` for N days after a successful export instead of deleting it immediately; `StorageManager.enforce_retention_purge()` runs alongside quota enforcement in the completion loop and removes retained studies once the window elapses. Off by default (immediate delete, prior behavior). Retained studies live outside `base_path` and are not counted toward `max_storage_gb`.

**Manual provenance overlay** — `ExamSidecar`'s data carries a top-level `manual` key (`{"edited_at": ..., "fields": {"patient.weight_kg": 34.0, ...}}`) recording which dotted paths a human set via the `/studies` dashboard page, distinct from the canonical (effective) values those paths already hold. This exists because `_process_study()` skips extraction entirely when `<study_number>_exam.json` already exists — a naive re-extraction would otherwise silently discard hand-entered fields (weight, image quality, an indication the lookup table couldn't match, per-series DLP/CTDIvol read off a console screen). `pixieveil/processing/exam_merge.py` is the single place this overlay logic lives: `apply_manual()` writes `manual.fields` into the canonical dict (per-series fields keyed `series.<series_number>.<leaf>`, never list index, since `extract()` rebuilds `series[]` from disk and the set can change between runs), `merge_extracted()` re-applies a previous sidecar's `manual` block onto a freshly extracted one, and `recompute_buckets()` reruns `exam_extractor._determine_bucket()` (a module-level function, not a method — it needs no lookup-table state) so a manually-entered weight or age immediately updates `protocol_type`/`examination_group` instead of leaving them stale. Both the PUT-exam handler (`StorageManager.save_exam`) and the manual re-extract action (`StorageManager.manual_reextract`) go through this module so the semantics never drift between the two call sites.

**Device fallback** — `Defacer._resolve_device()` validates `cuda`/`mps`/`cpu` at startup with a test tensor and falls back to CPU automatically.

**Thread safety** — Acquire `self.lock` before reading/writing `self.counters` in `StorageManager`.

## Configuration

Copy `config/settings.example.yaml` → `config/settings.yaml`. Key sections: `dicom_server`, `storage` (with optional `remote_storage.dicom` or `remote_storage.http`), `http_server`, `study`, `series_filter`, `defacing`, `anonymization` (profile: `research` or `gdpr`), `logging`.

## Code style

- Python 3.12+ — use built-in generics (`dict`, `list`, `tuple`) not `typing` aliases
- `logger = logging.getLogger(__name__)` at module level; no `print()`
- No bare `except:`; log at `ERROR` before re-raising
- `_underscore` prefix for private members, `UPPER_SNAKE` for module-level constants
