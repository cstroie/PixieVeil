# PixieVeil Quick Start

## Prerequisites

- Python 3.12+
- pip

## Installation

```bash
git clone https://github.com/cstroie/PixieVeil
cd PixieVeil
pip install -e .
python install.py
```

`install.py` is an interactive setup script that:
- Asks whether to enable defacing and which compute backend (CPU / CUDA)
- Installs PyTorch and nnUNetv2 for the chosen backend
- Creates required runtime directories
- Downloads the nnUNet defacing model from Google Drive if missing
- Writes your choice back to `config/settings.yaml`
- Runs sanity checks

To download the model separately at any time:

```bash
python install.py --download-model
```

## Minimum configuration

Copy `config/settings.example.yaml` to `config/settings.yaml` and set at least:

```yaml
dicom_server:
  ae_title: "PIXIEVEIL"   # AE title your modality will target
  port: 4070

storage:
  base_path: "./data/dicom"
  temp_path: "./data/tmp"

http_server:
  ip: "0.0.0.0"
  port: 8070
```

Everything else has sensible defaults.

## Start the server

```bash
python run.py
```

Stop with `Ctrl-C`. Services shut down gracefully.

## Verify it works

**Dashboard:** `http://localhost:8070/`

**Health check:**
```bash
curl http://localhost:8070/health
# {"status": "ok"}
```

**DICOM echo** (requires `dcmtk`):
```bash
echoscu localhost 4070 -aec PIXIEVEIL
```

## Send a test image

```bash
storescu localhost 4070 -aec PIXIEVEIL /path/to/image.dcm
```

The image will appear in `./data/dicom/<study>/<series>/` after processing. Once no new images arrive for `study.completion_timeout` seconds (default 120 s), the study is exported.

## Output layout

```
data/
  dicom/
    0001/           ← study directory (4-digit padded)
      0001/         ← series directory
        0001.dcm    ← anonymized (and defaced) images
    0001.json       ← study sidecar (persistent state, crash recovery)
    0001.zip        ← created on study completion (HTTP export only)
  nnUNet/
    Dataset001_DEFACE/   ← defacing model (downloaded by install.py)
  log/
    pixieveil.log
    anontrail.jsonl      ← original ↔ anonymized UID audit trail
  tmp/                   ← temporary NIfTI work directory (defacing)
```

## Optional: remote export

Two export transports are supported. Only one is active at a time; DICOM takes priority if both are configured.

### DICOM C-STORE (recommended)

Sends individual `.dcm` files to a PACS or DICOM node after each study completes. The study directory is removed locally on success.

```yaml
storage:
  remote_storage:
    dicom:
      host: "192.168.1.100"
      port: 104
      ae_title: "ORTHANC"          # called AE title (remote node)
      calling_ae: "PIXIEVEIL_SCU"  # our AE title (defaults to dicom_server.ae_title)
```

### HTTP ZIP upload

Zips the study and uploads it via `POST {base_url}/upload` with Bearer-token auth. The ZIP and study directory are removed locally on success.

```yaml
storage:
  remote_storage:
    http:
      base_url: "https://your-storage-server"
      auth_token: "your-bearer-token"
```

If neither transport is configured, archives stay local.

### Recovery on restart

Each study has a JSON sidecar (`<study_number>.json`) that tracks its status. On restart:
- Studies that were mid-processing (`complete` or `defacing` state) are automatically re-queued.
- Studies kept locally with no remote configured (`archived_via: null`) are re-queued if a remote is now configured — allowing deferred export after adding remote storage settings.

## Defacing

Defacing removes facial features from CT/MR head scans using nnU-Net segmentation. It runs automatically after anonymization on series identified as head scans.

```yaml
defacing:
  enabled: true
  device: "cuda"        # "cuda", "mps" (Apple Silicon), or "cpu"
                        # falls back to cpu automatically if the configured device is unavailable
  keep_backup: false    # set true to keep a <series>_pre_deface/ backup
  rotation_mode: "auto90"
  model_dir: ./data/nnUNet
  body_parts:
    - HEAD
    - BRAIN
    - NECK
    - SKULL
  series_description_pattern: "(?i)(head|brain|skull|cranial|cerebr)"
```

At startup, PixieVeil checks whether the configured device is actually usable (including a test CUDA allocation) and falls back to CPU with a warning if not. Only one nnU-Net inference runs at a time regardless of how many studies complete simultaneously.
