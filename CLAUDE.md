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
| `pixieveil/dicom_server/server.py` | DICOM SCP (C-ECHO + C-STORE) |
| `pixieveil/dicom_server/handlers.py` | C-STORE event handler |
| `pixieveil/processing/anonymizer.py` | Profile-based field transforms (PSEUDO/PSEUDOUID/NEWUID/CLEAR/KEEP) |
| `pixieveil/processing/series_filter.py` | Modality, image-type, and regex include/exclude filtering |
| `pixieveil/processing/study_manager.py` | Study/series numbering, completion detection, sidecar I/O |
| `pixieveil/processing/defacer.py` | nnU-Net head-scan defacing (DICOM↔NIfTI + mask application) |
| `pixieveil/storage/storage_manager.py` | Central pipeline and export orchestration |
| `pixieveil/storage/study_sidecar.py` | Atomic per-study JSON sidecar (crash recovery) |
| `pixieveil/storage/dicom_storage.py` | DICOM C-STORE export to remote node |
| `pixieveil/storage/remote_storage.py` | HTTP multipart ZIP upload |
| `pixieveil/storage/zip_manager.py` | ZIP archive creation |
| `pixieveil/dashboard/server.py` | aiohttp web server: `/`, `/stats`, `/health` |

## Key design decisions

**Sidecar files** — Each study has `<study_number>.json` written atomically (write-to-tmp + rename). Tracks `status` (`receiving → complete → defacing → archived`), `archived_via` (`"dicom"` / `"http"` / `null`), and per-series defacing progress. On restart, `StudyManager.initialize_from_sidecars()` re-queues any study that did not finish.

**Export priority** — DICOM C-STORE takes priority over HTTP ZIP upload when both are configured. If neither is configured, archives are kept locally.

**Device fallback** — `Defacer._resolve_device()` validates `cuda`/`mps`/`cpu` at startup with a test tensor and falls back to CPU automatically.

**Thread safety** — Acquire `self.lock` before reading/writing `self.counters` in `StorageManager`.

## Configuration

Copy `config/settings.example.yaml` → `config/settings.yaml`. Key sections: `dicom_server`, `storage` (with optional `remote_storage.dicom` or `remote_storage.http`), `http_server`, `study`, `series_filter`, `defacing`, `anonymization` (profile: `research` or `gdpr`), `logging`.

## Code style

- Python 3.12+ — use built-in generics (`dict`, `list`, `tuple`) not `typing` aliases
- `logger = logging.getLogger(__name__)` at module level; no `print()`
- No bare `except:`; log at `ERROR` before re-raising
- `_underscore` prefix for private members, `UPPER_SNAKE` for module-level constants
