# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this project is

PixieVeil is a DICOM anonymization server: it receives medical images via DICOM C-STORE, anonymizes them (profile-based), optionally defaces head scans with nnU-Net, organizes them into a numbered study/series hierarchy, and holds completed studies ready for export to a remote DICOM node or HTTP endpoint. A web dashboard exposes live metrics and is also where a user manually triggers each study's export — nothing is sent automatically.

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

# Install test dependencies (pytest) and run the suite
pip install -e ".[test]"
pytest
```

See [INSTALL.md](INSTALL.md) for the full bootstrap/systemd/OpenRC flow.

## Testing

Unit tests live under `tests/`, run with `pytest` (config in `pyproject.toml`
under `[tool.pytest.ini_options]`; `pip install -e ".[test]"` for `pytest`
plus `jsonschema` (the latter only used by the RHYTHM manifest-schema
cross-check) — note some fixtures additionally exercise code paths that only run
when the `[deface]` extra's heavy libs (torch/SimpleITK/nibabel/nnunetv2)
happen to be installed too, skipping gracefully via `pytest.importorskip`
when they aren't). `tests/conftest.py` holds shared fixtures: a
`StorageManager` fixture (dicom/http export and defacing left disabled — no
test should ever touch the network or a GPU) and `write_minimal_dicom()` for
building tiny-but-valid `.dcm` fixtures.

Coverage spans: `StudyManager`/`StorageManager` (numbering, the
`number_map` index, quota/retention rules, the manual-export-only pipeline,
the weight/age DICOM write-back), `exam_extractor` (including
`_scan_dicom_files`, the highest-complexity function in the codebase),
`exam_merge`'s manual-overlay semantics, `StudySidecar`'s JSON round-trip,
`Anonymizer` and `SeriesFilter` (field-value strategies, UID-mapping
consistency, original/derived series detection; the `rhythm` profile's
`RemoveTags`/`strip_tags` and its cross-check + synthetic-CT round-trip
against RHYTHM's vendored `GDPR-strict.json` in `test_anonymizer_rhythm.py`),
`DicomStorage` (config
resolution and the C-STORE send loop, via a small fake AE/association
substituted with `monkeypatch` — never a real network call), and `Defacer`
(config, head/topogram detection, model-path resolution, `_prepare_for_write`,
`_load_series_groups`, device resolution via monkeypatched `torch`, and
`deface_series`'s directory bookkeeping/atomic-swap safety with the three
heavy conversion steps stubbed at the instance level), and `dicom_server`
(`CStoreSCPHandler.validate_dicom`/`handle_c_store` against a small fake
StorageManager that records calls and can be told to raise, plus one
integration test wiring up the real `StorageManager` to catch signature
drift; `DicomServer`'s config, the C-ECHO/C-STORE event adapters, the
port-already-bound guard in `start()` via a real socket bind, and `stop()`'s
shutdown bookkeeping via a fake `AE`), `ZipManager` (pure filesystem/zip
logic — arcname layout, `_pre_deface` exclusion, the default-vs-explicit
`source_dir` split used for retained studies, empty/missing-source-dir
edge cases), `RemoteStorage` (config resolution and the HTTP upload
path against a small fake `aiohttp.ClientSession`/response substituted with
`monkeypatch` — never a real HTTP request), the RHYTHM export path
(`rhythm_manifest`'s pure item builder + `manifest_item_blockers`;
`rhythm_protocols.ProtocolMap` loading/validation — UUIDs, unique keys,
site_code cross-check, missing-file-is-empty; `RhythmStorage`'s
init→chunk→complete state machine and `check_job` polling against a scripted fake
`aiohttp.ClientSession` keyed by endpoint; `StorageManager.manual_upload_rhythm`
/ `_batch` / `reconcile_rhythm_jobs` / `_rhythm_excluded_series_sync` against
a fake `RhythmStorage` (including the `retain_after_export_days`
retain-as-`<NNNN>.zip`+`<NNNN>.json` path: ZIP stashed on upload, audit
manifest written at COMPLETE with the real checksum, the purge loop ageing
off the manifest mtime and falling back to the ZIP's for an orphan);
the dashboard's `upload_rhythm` action route +
`/api/studies/rhythm-batch` body validation + capability flags, via handler
coroutines called directly with a fake request/StorageManager; and — when the
`reference/uploader` submodule is present and `jsonschema` installed — a
cross-check that `build_manifest_item` / `build_batch_manifest` output
validates against RHYTHM's *own* `MANIFEST_SCHEMA_V2` loaded straight from
the vendored submodule, so a `git submodule update --remote` that moves the
schema fails here rather than at upload time).

Deliberately still out of scope: `dicom_to_nifti`, the pixel-array half of
`nifti_to_dicom`, `run_nnunet_inference`, `_run_nnunet_and_apply_mask`, and
`_dilate_mask` — real volumetric conversion math that needs actual scanner
image data to test meaningfully, not synthetic 2x2 arrays; same for
`DicomServer.start_blocking_server`'s live pynetdicom association loop,
which needs a real socket server thread and a real C-STORE client — that
belongs in an end-to-end test, not a unit test. Extend the
suite when touching logic in scope for it instead of leaving it untested;
treat adding coverage for the still-excluded paths as a separate,
deliberate task (real fixture volumes, a fake association) rather than
folding it into an unrelated change. No mocking framework is used anywhere
in this suite — fakes are small, purpose-built classes/functions, monkeypatched
in via pytest's built-in `monkeypatch` fixture.

Manual linting:

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
                        └─ Defacer.deface_series()        [asyncio.to_thread]
                           │  (study now "ready" — sits in base_path)
                           ▼
                  /studies dashboard → user clicks Send/Upload/RHYTHM
                  StorageManager.manual_send_dicom() / manual_upload_http()
                        ├─ DicomStorage.send_study()      [asyncio.to_thread]
                        └─ ZipManager → RemoteStorage     [asyncio.to_thread]
                  StorageManager.manual_upload_rhythm()   (returns once queued)
                        └─ ZipManager → RhythmStorage.upload_study()
                  StorageManager.reconcile_rhythm_jobs()  (completion loop)
                        └─ RhythmStorage.check_job() → on COMPLETE: retain/delete + archive
```

Single asyncio event loop. All blocking I/O (nnUNet inference, ZIP, file I/O) uses `asyncio.to_thread`. GPU jobs are serialized via a class-level `threading.Semaphore(1)` on `Defacer`.

## Module map

| Path | Purpose |
|------|---------|
| `pixieveil.py` | Entry point |
| `pixieveil/config/settings.py` | Loads/validates `config/settings.yaml` via pydantic |
| `pixieveil/dicom_server/server.py` | DICOM SCP (C-ECHO + C-STORE). Supported storage SOP classes: CT, MR, Secondary Capture, X-Ray Radiation Dose SR (RDSR) — anything else is refused at association negotiation, not received-then-filtered |
| `pixieveil/dicom_server/handlers.py` | C-STORE event handler |
| `pixieveil/processing/anonymizer.py` | Profile-based field transforms (PSEUDO/PSEUDOUID/NEWUID/CLEAR/KEEP) + a profile `RemoveTags` list deleted outright as the final pass (the `rhythm` profile uses it to satisfy RHYTHM's GDPR-strict validator); widened overlay/curve/waveform stripping |
| `pixieveil/processing/series_filter.py` | Modality, image-type, and regex include/exclude filtering |
| `pixieveil/processing/study_manager.py` | Study/series numbering, completion detection, sidecar I/O |
| `pixieveil/processing/defacer.py` | nnU-Net head-scan defacing (DICOM↔NIfTI + mask application) |
| `pixieveil/processing/exam_extractor.py` | Derives RHYTHM exam-entry fields (indication, protocol type/group, scanner, dose, patient age/weight) from a completed study's DICOM headers |
| `pixieveil/storage/storage_manager.py` | Central pipeline and export orchestration |
| `pixieveil/storage/study_sidecar.py` | Atomic per-study JSON sidecar (crash recovery) |
| `pixieveil/storage/exam_sidecar.py` | Atomic `<study_number>_exam.json` sidecar holding ExamExtractor's output |
| `pixieveil/storage/dicom_storage.py` | DICOM C-STORE export to remote node |
| `pixieveil/storage/remote_storage.py` | HTTP multipart ZIP upload |
| `pixieveil/storage/rhythm_storage.py` | RHYTHM upload transport — v2 resumable chunked-upload (init→chunks→complete) + `check_job()` job-status poll |
| `pixieveil/processing/rhythm_manifest.py` | Builds one RHYTHM v2 server-assigned-batch manifest item per study from the effective exam sidecar (`ref`/`filename`/`site_code`/`protocol_id`/`image_quality` + optional dose/patient numerics — RHYTHM derives region/indication/contrast/type/group/scanner from the registered protocol behind `protocol_id`) |
| `pixieveil/processing/rhythm_protocols.py` | Loads `config/rhythm_protocols.yaml` — local protocol key → registered RHYTHM `CTProtocol` UUID; validates UUIDs, unique keys, and (if the file sets one) that its `site_code` matches settings |
| `pixieveil/storage/zip_manager.py` | ZIP archive creation (optional `exclude` predicate — the RHYTHM path uses it to drop non-CT objects) |
| `pixieveil/dashboard/server.py` | aiohttp web server: `/`, `/stats`, `/health`, plus the `/studies` review page and its `/api/studies*` JSON API (list/detail/save/actions, `/api/studies/rhythm-batch`, `/api/studies[/{n}]/rhythm-manifest` preview/download) |
| `pixieveil/processing/exam_merge.py` | Manual-edit/provenance-overlay semantics for the exam sidecar — shared by the `/api/studies/{n}/exam` save handler and the manual re-extract action |

## Key design decisions

**Sidecar files** — Each study has `<study_number>.json` written atomically (write-to-tmp + rename). Tracks `status` (`receiving → complete → defacing → ready → archived`, with `defacing_failed` as a side branch off `defacing` — see below), `archived_via` (`"dicom"` / `"http"` / `"rhythm"` / `null`), the `rhythm_*` upload-job fields (see RHYTHM export below), and per-series defacing progress. On restart, `StudyManager.initialize_from_sidecars()` re-queues any study that did not finish reaching `ready` (i.e. still `complete`, `defacing`, or `defacing_failed`); studies already `ready` or `archived` are left alone.

**Manual-only export** — `StorageManager._process_study()` never sends a study anywhere: once exam extraction and defacing finish, the study is marked `ready` and sits in `base_path`. Export only happens when a user clicks Send/Upload on the `/studies` dashboard, which calls `StorageManager.manual_send_dicom()` or `manual_upload_http()`. The first successful export for a study also performs the retain-or-delete + `mark_study_archived()` bookkeeping (DICOM takes priority if the user sends via both); a later manual re-send of an already-`archived` study just resends without moving files again, since they're already in their resting place (`base_path` or `storage.retain_path`) — **except** a RHYTHM-retained study, which is kept as a flat `<NNNN>.zip` + `<NNNN>.json` pair rather than a directory: `resolve_study_dir` only finds directories, so any re-send (DICOM/HTTP/RHYTHM) of it returns "study directory not found". That's intentional — the retained ZIP is a CT-only, report-stripped audit artifact, not a re-export source; to send it elsewhere, re-receive the source DICOM.

**Defacing failure blocks export, not just logs it** — After `_deface_study()` finishes, `_process_study()` checks `StudySidecar.has_undefaced_head_series()` (any series flagged `is_head=True` with `defaced=False`) before deciding what status to mark. If any head-scan series failed to deface, the study is marked `defacing_failed` instead of `ready`, and `manual_send_dicom`/`manual_upload_http` both refuse to export a study in that state — otherwise it would sit in the dashboard looking identical to a fully-defaced `ready` study, and an operator could export it with a patient's face still in the pixel data. `defacing_failed` is included in the crash-recovery requeue set (alongside `complete`/`defacing`), so a restart retries just the series that still need it — `is_series_defaced` already makes re-running defacing idempotent for series that succeeded the first time, so this reuses the existing recovery path instead of needing a separate "redo defacing" action.

**RHYTHM exam extraction** — On every completed study, `ExamExtractor.extract()` reads the anonymized DICOM headers under `study_dir` and writes `<study_number>_exam.json` (JSON, not YAML — machine-written/machine-read, mirrors `StudySidecar`'s format) with whatever RHYTHM Manual-Exam-Entry fields are derivable (contrast, patient age, CTDIvol, scanner make/model, indication/protocol-type/exam-group when `integrations/rhythm/indication_lookup.yaml` and `scanner_lookup.yaml` have a matching curated rule). **Terminology**: the sidecar's `series` list is keyed by DICOM `SeriesNumber` — it is *not* the same thing as an irradiation event. One RDSR acquisition (one real dose event) can be reconstructed into several DICOM series at different kernel/thickness, so several `series[]` entries legitimately share one event; `rdsr.acquisitions[]` is the authoritative per-irradiation-event record. Every CT-modality series — including topograms/localizers — gets a `series[]` entry flagged `is_topogram`; a topogram is a real irradiation event and stays in the dose record rather than being dropped. Only genuinely non-CT objects riding along in the study directory (e.g. a rendered Dose Report screen capture) are excluded. Each `series[]` entry also carries per-series technique parameters read straight off the image headers — `slice_thickness_mm`, `exposure_time_ms`, `convolution_kernel`, `patient_position`, `single_collimation_width_mm`, `total_collimation_width_mm`, `spiral_pitch_factor` — and a best-effort `dlp_mgy_cm` estimated as CTDIvol × the z-extent spanned by `ImagePositionPatient` across the series (skipped for topograms, where that relationship doesn't hold); `notes` flags it as an estimate and says to prefer `rdsr.acquisitions[*].dlp_mgy_cm` when an RDSR is present. Fields it still cannot determine (image quality, unmatched indications/scanners) are left `null` with an explanation in `notes`. (These extracted indication/scanner/bucket fields feed the `/studies` dashboard display and the sidecar's own `protocol_type`/`examination_group` recompute — they no longer feed the RHYTHM manifest, which since submodule `f4120f5` carries only a `protocol_id` from which RHYTHM derives all of that; see "RHYTHM export" below.) Runs first in `_process_study()` — before defacing, not just before export — since it only needs anonymized headers (defacing rewrites `PixelData` only, never metadata); this way the sidecar survives a defacing crash and isn't blocked behind CPU-mode nnU-Net inference. Skipped if the sidecar already exists, so a re-queued retry never overwrites paper-note data merged in later.

**RDSR content parsing** — Confirmed on our Siemens Emotion 16: its "Dose Report" object is `SOPClassUID`-wise a Secondary Capture image (a rendered screenshot), but the same object also carries the full X-Ray Radiation Dose Report structured-content tree (TID 10011/10013) that a native RDSR would — Siemens converts SR→SC for compatibility but keeps the content sequence attached (`DerivationDescription: "Convert syngo SR to DICOM SC"`). `ExamExtractor._parse_rdsr_content()` walks that tree by DCM concept code (works on a native RDSR too, not just this vendor quirk) and populates the sidecar's `rdsr` key: `total_dlp_mgy_cm` plus, per acquisition, `pitch`, `kvp`, `mean_ma`/`max_ma`, `rotation_time_s`, `mean_ctdivol_mgy`, `dlp_mgy_cm`, `acquisition_type`, `modulation_type`. This is why the DICOM SCP (`pixieveil/dicom_server/server.py`) also registers `XRayRadiationDoseSRStorage` now — without that presentation context a *native* RDSR would be refused at C-STORE association negotiation even though this vendor's SC-hybrid already got through on the pre-existing `SecondaryCaptureImageStorage` context. Important caveat this surfaced: the flat per-image `CTDIvol` tag on this scanner is **cumulative dose-to-date**, not per-acquisition — always prefer `rdsr.acquisitions[*].mean_ctdivol_mgy` over `series[*].ctdi_vol_mgy` when both are present.

**Post-export retention** — `storage.retain_after_export_days` (optional) keeps a copy of each already-anonymized study under `storage.retain_path` for N days after a successful export instead of deleting it immediately; `StorageManager.enforce_retention_purge()` runs alongside quota enforcement in the completion loop and removes retained studies once the window elapses. Off by default (immediate delete, prior behavior). Retained studies live outside `base_path` and are not counted toward `max_storage_gb`. **Two on-disk shapes**: the DICOM/HTTP paths move the whole study *directory* into `retain_path` with a `.retained_at` marker inside it (`_retain_or_delete_sync`); the RHYTHM path instead retains a *flat pair* `retain_path/<NNNN>.zip` + `retain_path/<NNNN>.json` — the exact CT-only ZIP RHYTHM ingested (moved straight out of `base_path` at upload time by `_stash_rhythm_zip_sync`, so its bytes/`sha256` match what RHYTHM verified — latest upload attempt overwrites) plus the v2 batch manifest for it (`_retain_rhythm_audit_sync`, written at COMPLETE with the real `sha256`/`size_bytes`/`batch`). `enforce_retention_purge_sync` handles both: for the flat pair it ages off the `<NNNN>.json` mtime (written at finalize) — or the ZIP's own mtime for an orphan ZIP whose job never reached COMPLETE — and purges the two files together.

**`rhythm` anonymization profile** — A third profile alongside `research`/`gdpr`, required for RHYTHM export. RHYTHM re-validates every uploaded slice against its own `GDPR-strict.json` (vendored at `reference/uploader/ct_upload_platform/GDPR-strict.json`) and rejects the studyset if any tag it marks "must be removed" still carries a value — the `research`/`gdpr` profiles fail this because they *replace* dates with a placeholder instead of removing them, don't touch device-identity tags (`Manufacturer`, `StationName`, `DeviceSerialNumber`, …), and leave a pseudo `PatientID` (`reference/anon_rhythm.json` is a captured 49-file rejection). The `rhythm` profile adds `RemoveTags` — a list of DICOM keywords `Anonymizer.strip_tags()` deletes outright as the final `anonymize()` pass (so it wins over `anonymize_dates()`'s substitution). The list is exactly the pydicom-resolvable `null`-directive keywords from RHYTHM's `GDPR-strict.json` (the display-name-only entries the validator can't address by `hasattr` are skipped), plus `PatientName`/`PatientID`; `tests/test_anonymizer_rhythm.py` cross-checks it against the vendored file (fails on a `git submodule update --remote` that adds a tag) and round-trips a synthetic CT carrying the real rejected tags. `PatientAge`/`PatientWeight`/`PatientSize` are `KEEP` (GDPR-strict marks them so; the manifest uses age/weight). `PatientID` uses `PSEUDO` (kept for the anontrail) then `RemoveTags` deletes it from every shipped file — RHYTHM keys studies on its own Repository Study ID. `Anonymizer.remove_overlays()` was also widened to the full 60xx/50xx repeating-group ranges + waveform/audio groups (a strict anonymization improvement, applied to all profiles).

**RHYTHM export (third manual path)** — Alongside DICOM C-STORE and HTTP ZIP, a study can be pushed to the RHYTHM CT upload platform via its v2 "server-assigned batch" API. There is no manifest-ingest endpoint on RHYTHM's side: its partner tooling fans out one resumable chunked upload per studyset, tagging each with a shared `batch` string and a `manifest_item` dict. `pixieveil/processing/rhythm_manifest.py` builds that item from the *effective* exam sidecar (canonical values already carry the manual overlay); `RhythmStorage.upload_study()` (`rhythm_storage.py`) drives `chunked/init → per-chunk POST (sha256, retried) → complete/` and **returns once the ingest job is queued — not once it passed**. **`protocol_id`, not coded fields** — RHYTHM v2 (submodule `f4120f5`+) dropped the per-item `clinical_indication_code`/`anatomical_region`/`contrast_code`/`patient_group_code`/`scanner_id`/`protocol_name`; an item now names one `protocol_id`, the UUID of a `CTProtocol` registered on RHYTHM's Protocol Records page, and the server derives region/indication/contrast/protocol type/exam group/scanner from it. PixieVeil resolves that UUID from `config/rhythm_protocols.yaml` (`rhythm_protocols.ProtocolMap`, loaded by `RhythmStorage`) keyed by the local protocol the operator picks for the study on `/studies` (the exam sidecar's `rhythm.protocol_key`, a manual-overlay field). A key set but absent from a populated map is a hard error; an unset key (or an empty map) leaves `protocol_id` null → a blocker. `item["site_code"]` still comes from `settings…rhythm.site_code` (authoritative) and must match the registered protocol's own `site_code` case-insensitively or RHYTHM fails the job — if the map file carries a top-level `site_code` that disagrees with settings, `ProtocolMap.load()` raises at startup rather than let that reach RHYTHM. The old code tables (`integrations/rhythm/indication_lookup.yaml` etc.) and their ported constants are gone from `rhythm_manifest.py`; the `integrations/rhythm/*_protocol.yaml` reference files stay — they're the human-facing definition of what to register upstream, and you record each registered protocol's UUID in the map. `StorageManager.manual_upload_rhythm()` / `manual_upload_rhythm_batch()` (one `batch` id per dashboard action) refuse a study when: RHYTHM isn't configured, the anonymization profile isn't `rhythm` (only that profile strips every tag RHYTHM's `GDPR-strict` ingest validator requires absent — `research`/`gdpr` both leave dates, device identity, descriptions and a pseudo `PatientID` that the validator rejects; proof is `reference/anon_rhythm.json`, a real 49-file rejection captured under `research`. This gate reads the *current* `settings.anonymization.profile`, not what each study was actually anonymized with, so a study received under another profile then switched to `rhythm` passes the gate but is rejected by RHYTHM at ingest; anonymization is one-shot at receive time and only the anonymized copy is kept, so the fix is to re-send the source DICOM under `rhythm`, not re-export), the study is `defacing_failed`, a RHYTHM-required manifest field is still unset (notably `image_quality` — left null by extraction, filled on `/studies`), or the study holds no CT image series. The export ZIP is built with a `ZipManager` `exclude` predicate that keeps only CT / Enhanced CT / Legacy Converted Enhanced CT SOP classes — the rendered Dose Report Secondary Capture and any native RDSR are dropped (RHYTHM wants DICOM CT only), and an unreadable series fails safe (excluded). The ZIP's `sha256`/`size_bytes` are computed *after* it exists and set on the manifest item (RHYTHM verifies the checksum before extracting). Because ingest is async, `manual_upload_rhythm` only persists `rhythm_batch`/`rhythm_session_id`/`rhythm_job_id`/`rhythm_job_status_url`/`rhythm_status` on the study sidecar (via `StudyManager.set_rhythm_job()`) — plus `rhythm_zip_sha256`/`rhythm_zip_size` when retention is on and the ZIP was stashed — and returns; `StorageManager.reconcile_rhythm_jobs()` runs each completion-loop tick, polls every in-flight job with `RhythmStorage.check_job()`, and on a terminal `COMPLETE` runs the retain-or-delete + `mark_study_archived(via="rhythm")` + counter bookkeeping (`_finalize_rhythm_export`). `PARTIAL`/`FAILED` just record the state — the study stays `ready` so the operator can retry. `archived_via` gains `"rhythm"` as a fourth value. **Retained audit copy**: when `retain_after_export_days` is set, the uploaded ZIP is moved to `retain_path/<NNNN>.zip` (not unlinked) as soon as `upload_study` returns OK, and at `COMPLETE` `_finalize_rhythm_export` writes `retain_path/<NNNN>.json` beside it — the v2 batch manifest, built in Python from the effective exam sidecar and carrying the real `sha256`/`size_bytes` — then drops the working directory. (No `rhythm_zip_sha256` on the sidecar — stash failed, or an in-flight job predating this — falls back to the shared directory-move retention.) For manual testing against RHYTHM's own validator, `StorageManager.build_rhythm_manifest_preview_sync()` builds the batch manifest **itself** (top-level `type`/`items`, so RHYTHM detects it as v2) without uploading — no `sha256`/`size_bytes`, `site_code` left null and flagged rather than back-filled, and an extra top-level `blockers` key (allowed by v2's `additionalProperties: true`) listing anything that would stop a real upload. The dashboard exposes it at `GET /api/studies[/{n}]/rhythm-manifest` (`?studies=1,2,3`; `&download=1` returns a pristine `rhythm_manifest.json` with `blockers` stripped) plus per-study "preview / download" links — **the manifest is only ever built in the Python backend** (`rhythm_manifest.build_*`); the `/studies` page just links to those endpoints, it never assembles one client-side. For a single already-retained study the endpoint serves `retain_path/<NNNN>.json` verbatim (`load_retained_rhythm_manifest_sync`) — the real manifest with checksums and the batch it actually went up under — instead of minting a fresh checksum-less preview.

**Manual provenance overlay** — `ExamSidecar`'s data carries a top-level `manual` key (`{"edited_at": ..., "fields": {"patient.weight_kg": 34.0, ...}}`) recording which dotted paths a human set via the `/studies` dashboard page, distinct from the canonical (effective) values those paths already hold. This exists because `_process_study()` skips extraction entirely when `<study_number>_exam.json` already exists — a naive re-extraction would otherwise silently discard hand-entered fields (weight, image quality, an indication the lookup table couldn't match, per-series DLP/CTDIvol read off a console screen). `pixieveil/processing/exam_merge.py` is the single place this overlay logic lives: `apply_manual()` writes `manual.fields` into the canonical dict (per-series fields keyed `series.<series_number>.<leaf>`, never list index, since `extract()` rebuilds `series[]` from disk and the set can change between runs), `merge_extracted()` re-applies a previous sidecar's `manual` block onto a freshly extracted one, and `recompute_buckets()` reruns `exam_extractor._determine_bucket()` (a module-level function, not a method — it needs no lookup-table state) so a manually-entered weight or age immediately updates `protocol_type`/`examination_group` instead of leaving them stale. Both the PUT-exam handler (`StorageManager.save_exam`) and the manual re-extract action (`StorageManager.manual_reextract`) go through this module so the semantics never drift between the two call sites.

**Patient weight/age write-back** — `StorageManager._save_exam_sync()` also patches a manually-corrected `patient.weight_kg`/`patient.age_years` straight into `PatientWeight`/`PatientAge` on every anonymized `.dcm` file under the study's directory (`_writeback_patient_fields_sync`), not just the exam sidecar — otherwise the correction would never reach whatever gets exported. Only the field(s) present in *this* save's `manual_fields` are patched (not the full merged overlay), and only while the study is still sitting in `base_path` (`sidecar.status != "archived"`); once a study is archived its DICOM copies are already gone, so nothing to patch. Other manual fields (indication, protocol name, per-series DLP, etc.) stay sidecar-only — they don't have a clean 1:1 DICOM tag to write back to.

**Device fallback** — `Defacer._resolve_device()` validates `cuda`/`mps`/`cpu` at startup with a test tensor and falls back to CPU automatically.

**Thread safety** — Acquire `self.lock` before reading/writing `self.counters` in `StorageManager`.

## Configuration

Copy `config/settings.example.yaml` → `config/settings.yaml`. Key sections: `dicom_server`, `storage` (with optional `remote_storage.dicom`, `remote_storage.http`, and/or `remote_storage.rhythm` — the last needs `base_url` + `auth_token` + `site_code`, only works with the `rhythm` anonymization profile, and also needs `config/rhythm_protocols.yaml` (copy from `config/rhythm_protocols.example.yaml`) mapping each local protocol key to the UUID of a `CTProtocol` registered in the RHYTHM app), `http_server`, `study`, `series_filter`, `defacing`, `anonymization` (profile: `research`, `gdpr`, or `rhythm`), `logging`.

## Code style

- Python 3.12+ — use built-in generics (`dict`, `list`, `tuple`) not `typing` aliases
- `logger = logging.getLogger(__name__)` at module level; no `print()`
- No bare `except:`; log at `ERROR` before re-raising
- `_underscore` prefix for private members, `UPPER_SNAKE` for module-level constants
