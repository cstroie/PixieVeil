# PixieVeil Agent Guidelines

## Project Overview

PixieVeil is a DICOM anonymization server. It receives medical imaging data from modalities via C-STORE, anonymizes it according to DICOM PS3.15 standards, optionally defaces head scans using nnU-Net, organizes images into a numbered study/series hierarchy, and exports completed studies to a remote DICOM node or HTTP endpoint. A web dashboard exposes real-time metrics.

## Environment

- **Python**: 3.12+
- **Key dependencies**: pynetdicom, pydicom, aiohttp, pyyaml, pydantic, nibabel, torch, nnunetv2 (defacing only)
- **Entry point**: `python run.py`
- **Configuration**: `config/settings.yaml` (copy from `config/settings.example.yaml`)

---

## Commands

```bash
# Run the server
python run.py

# Interactive setup (installs torch/nnunetv2, downloads nnUNet model)
python install.py

# Download defacing model only
python install.py --download-model
```

No automated test suite exists. Lint manually if needed:

```bash
flake8 pixieveil/ --max-line-length=100
mypy pixieveil/
```

---

## Architecture

```
Modality ──C-STORE──► DicomServer
                           │
                      CStoreSCPHandler
                           │
                      StorageManager.save_temp_image()
                           │
                      StorageManager.process_image()
                        ├─ validate_dicom()
                        ├─ SeriesFilter.should_filter()
                        ├─ Anonymizer.anonymize()
                        ├─ StudyManager.add_image_to_study()
                        └─ move to base_path/<study>/<series>/<image>.dcm
                           │
                  (background loop — asyncio.create_task per study)
                  StorageManager._process_study()
                        ├─ Defacer.deface_series()        [optional, in thread]
                        ├─ DicomStorage.send_study()      [DICOM export, in thread]
                        └─ ZipManager.create_zip()        [HTTP export, in thread]
                             └─ RemoteStorage.upload_file()
```

All services share a single asyncio event loop. Every blocking operation (nnUNet inference, ZIP creation, file I/O) is offloaded to a thread pool via `asyncio.to_thread`. Study processing is launched as an independent `asyncio.create_task` so the DICOM SCP and HTTP dashboard remain responsive during long defacing runs.

---

## Module Map

| Path | Purpose |
|------|---------|
| `run.py` | Entry point; wires up and starts all services |
| `pixieveil/config/settings.py` | Loads and validates `settings.yaml` |
| `pixieveil/dicom_server/server.py` | DICOM SCP (C-ECHO + C-STORE) via pynetdicom |
| `pixieveil/dicom_server/handlers.py` | C-STORE event handler |
| `pixieveil/processing/anonymizer.py` | Profile-based DICOM field anonymization |
| `pixieveil/processing/series_filter.py` | Modality, image-type, and regex-based series filtering |
| `pixieveil/processing/study_manager.py` | Study/series numbering, completion detection, sidecar I/O |
| `pixieveil/processing/defacer.py` | nnU-Net head-scan defacing (DICOM↔NIfTI conversion + mask application) |
| `pixieveil/storage/storage_manager.py` | Central processing pipeline and export orchestration |
| `pixieveil/storage/study_sidecar.py` | Persistent per-study JSON sidecar (crash recovery) |
| `pixieveil/storage/zip_manager.py` | ZIP archive creation |
| `pixieveil/storage/remote_storage.py` | HTTP multipart ZIP upload |
| `pixieveil/storage/dicom_storage.py` | DICOM C-STORE export to a remote node |
| `pixieveil/dashboard/server.py` | aiohttp web dashboard (`/`, `/stats`, `/health`) |

---

## Key Design Decisions

### Async + threading
The event loop never blocks. All I/O-heavy work uses `asyncio.to_thread`. nnUNet inference is additionally serialised with a class-level `threading.Semaphore(1)` on `Defacer` so concurrent study completions never run two GPU jobs simultaneously.

### Sidecar files
Each study has a `<study_number>.json` sidecar written atomically (write-to-tmp + rename) alongside its directory. The sidecar tracks:
- `status`: `receiving → complete → defacing → archived`
- `archived_via`: `"dicom"`, `"http"`, or `null` (kept locally)
- Per-series head/topogram classification and defacing progress

On restart, `StudyManager.initialize_from_sidecars()` restores all in-memory state and re-queues any study that did not reach a successful export (`complete`, `defacing`, or `archived` with `archived_via: null` and the directory still on disk).

### Export priority
DICOM C-STORE (`DicomStorage`) takes priority over HTTP ZIP upload (`RemoteStorage`) when both are configured. If neither is configured, the study directory and ZIP are kept locally.

### Device fallback
`Defacer._resolve_device()` validates the configured device (`cuda`/`mps`/`cpu`) at startup with a test tensor allocation and falls back to CPU with a warning if the device is unavailable or fails.

---

## Configuration Reference

```yaml
dicom_server:
  ae_title: "PIXIEVEIL"
  port: 4070
  ip: "0.0.0.0"

storage:
  base_path: "./data/dicom"
  temp_path: "./data/tmp"
  max_storage_gb: 100
  # remote_storage:
  #   dicom:
  #     host: "192.168.1.100"
  #     port: 104
  #     ae_title: "ORTHANC"
  #     calling_ae: "PIXIEVEIL_SCU"   # defaults to dicom_server.ae_title
  #   http:
  #     base_url: "https://your-storage-server"
  #     auth_token: "your-bearer-token"

http_server:
  port: 8070
  ip: "0.0.0.0"

study:
  completion_timeout: 120
  completion_check_interval: 30
  max_study_size_mb: 4000

series_filter:
  exclude_modalities: ["PR", "RT"]
  only_original_series: true

defacing:
  enabled: false
  device: "cuda"        # falls back to cpu automatically if unavailable
  keep_backup: false
  rotation_mode: "auto90"
  model_dir: ./data/nnUNet
  body_parts: [HEAD, BRAIN, NECK, SKULL]
  series_description_pattern: "(?i)(head|brain|skull|cranial|cerebr)"

anonymization:
  profile: "research"   # research | gdpr

logging:
  level: "INFO"
  file: "./data/log/pixieveil.log"
  anontrail: "./data/log/anontrail.jsonl"
```

---

## Storage Layout

```
data/
  dicom/
    0001/               ← study directory (4-digit padded)
      0001/             ← series directory
        0001.dcm        ← anonymized image
      0001_pre_deface/  ← backup before defacing (keep_backup: true only)
    0001.json           ← study sidecar
    0001.zip            ← ZIP archive (HTTP export only)
  nnUNet/
    Dataset001_DEFACE/  ← nnUNet model
  log/
    pixieveil.log
    anontrail.jsonl     ← original ↔ anonymized UID audit trail (JSONL)
  tmp/                  ← temporary NIfTI work files (defacing)
```

---

## Code Style

- **Python 3.12+** — use `dict`/`list`/`tuple` for type hints, not `Dict`/`List`/`Tuple`
- **Imports**: stdlib → third-party → local, one blank line between groups
- **Naming**: `PascalCase` classes, `snake_case` functions/variables, `_underscore` private members, `UPPER_SNAKE` module-level constants
- **Logging**: `logger = logging.getLogger(__name__)` at module level; no `print()`
- **Comments**: only when the *why* is non-obvious; no docstrings restating what the signature already says
- **Error handling**: log at `ERROR` before re-raising; use specific exception types; no bare `except:`
- **Thread safety**: acquire `self.lock` (a `threading.Lock`) before reading or writing `self.counters` in `StorageManager`
