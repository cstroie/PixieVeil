# PixieVeil

PixieVeil is a DICOM anonymization server. It receives DICOM images from medical modalities via C-STORE, anonymizes them according to DICOM PS3.15 standards, organizes them into a numbered study/series/image hierarchy, and optionally uploads archives to a remote storage endpoint. A built-in web dashboard provides real-time processing metrics.

## Features

- **DICOM SCP** — Receives CT, MR, and Secondary Capture images via C-STORE (pynetdicom). Verifies connectivity with C-ECHO.
- **Anonymization** — Removes or replaces patient demographics, institution data, dates, private tags, and overlay groups. Maintains consistent UIDs and patient IDs across all files in the same study.
- **Audit trail** — Writes a JSONL mapping log (`mapping_log.jsonl`) that records the original-to-anonymized UID/patient-ID mappings for every processed image.
- **Series filtering** — Configurable exclusion of modalities (e.g. SR, PR, RT) and optional filtering to original-acquisition series only.
- **Study lifecycle management** — Detects study completion by inactivity timeout and assembles a ZIP archive per study.
- **Remote upload** — Optional HTTP POST upload of study ZIPs to a configurable endpoint with Bearer-token authentication.
- **Web dashboard** — `aiohttp`-based HTTP server with a `/stats` JSON API and a live dashboard page that polls metrics periodically.
- **Structured logging** — Rotating file + console logging with a configurable level and path.

## Architecture

```
Modality ──C-STORE──► DicomServer
                           │
                      CStoreSCPHandler
                           │
                      StorageManager ──► save_temp_image()
                           │
                      process_image()
                        ├─ validate
                        ├─ SeriesFilter.should_filter()
                        ├─ Anonymizer.anonymize()
                        ├─ StudyManager.add_image_to_study()
                        └─ move to base_path/<study>/<series>/<image>.dcm
                           │
                  (background loop)
                  check_study_completions()
                        ├─ ZipManager.create_zip()
                        ├─ RemoteStorage.upload_file()
                        └─ cleanup local files
```

All services run concurrently in a single asyncio event loop. Blocking I/O (ZIP creation, file reads) is offloaded to a thread pool via `asyncio.to_thread`.

## Requirements

- Python 3.8+
- pynetdicom >= 2.0.0
- pydicom >= 2.0.0
- aiohttp >= 3.0.0
- pyyaml
- pydantic

## Installation

```bash
git clone https://github.com/cstroie/PixieVeil
cd PixieVeil
pip install -r requirements.txt
cp config/settings.yaml.example config/settings.yaml
# Edit config/settings.yaml for your environment
python run.py
```

## Configuration

All configuration lives in `config/settings.yaml`. Copy `config/settings.yaml.example` as a starting point.

```yaml
# DICOM Server Settings
dicom_server:
  ae_title: "PIXIEVEIL"
  port: 4070
  ip: "0.0.0.0"

# Anonymization Settings
anonymization:
  profile: "research"   # Options: "gdpr_strict", "research"

# Storage Settings
storage:
  base_path: "./data/pixieveil"   # Organized study tree
  temp_path: "./tmp/pixieveil"    # Incoming image staging area
  max_storage_gb: 1000
  # Optional: omit entire block to disable remote upload
  remote_storage:
    base_url: "https://your-storage-server"
    auth_token: "your-bearer-token"

# HTTP Dashboard
http_server:
  port: 8070
  ip: "0.0.0.0"

# Study completion
study:
  completion_timeout: 300          # seconds of inactivity before a study is considered done
  completion_check_interval: 30    # how often (seconds) to check for completed studies
  max_study_size_mb: 4000

# Series filtering
series_filter:
  exclude_modalities: ["SR", "PR", "RT"]
  keep_original_series: true

# Logging
logging:
  level: "INFO"
  file: "./data/log/pixieveil.log"
```

### Remote storage

If `storage.remote_storage.base_url` is set, completed study ZIPs are uploaded via `POST {base_url}/upload` as a multipart form with fields `file` (the ZIP) and `remote_path` (the filename). A `Bearer` token from `auth_token` is sent in the `Authorization` header. If `base_url` is absent or empty, archives are kept locally and no upload is attempted.

## Storage layout

Anonymized images are stored under `base_path` in a four-digit padded hierarchy:

```
base_path/
  0001/           ← study number
    0001/         ← series number
      0001.dcm
      0002.dcm
    0002/
      0001.dcm
  0002/
    ...
  0001.zip        ← created once the study is complete
mapping_log.jsonl ← audit trail (one JSON object per image)
```

The `mapping_log.jsonl` file records each original → anonymized UID and patient-ID mapping and is written one line at a time so it remains valid even if the process is interrupted.

## Dashboard

The web dashboard is available at `http://<http_server.ip>:<http_server.port>/` once the application is running.

| Endpoint | Description |
|---|---|
| `GET /` | Dashboard HTML page |
| `GET /stats` | JSON metrics snapshot |
| `GET /health` | `{"status": "ok"}` health check |

The `/stats` response contains counters grouped into sections: **Processed**, **Reception**, **Storage**, **Archive**, **Performance**, and **Errors**.

## Running

```bash
python run.py
```

Stop with `Ctrl-C`. All services shut down gracefully with a 10-second timeout.

## Supported DICOM SOP Classes

- Verification (C-ECHO)
- CT Image Storage
- MR Image Storage
- Secondary Capture Image Storage
