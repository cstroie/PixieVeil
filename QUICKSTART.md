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

Open `config/settings.yaml` and set at least:

```yaml
dicom_server:
  ae_title: "PIXIEVEIL"   # AE title your modality will target
  port: 4070

storage:
  base_path: "./data/pixieveil"
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

The image will appear in `./data/pixieveil/<study>/<series>/` after processing. Once no new images arrive for `study.completion_timeout` seconds (default 120 s), the study is zipped and — if remote storage is configured — uploaded.

## Output layout

```
data/
  pixieveil/
    0001/0001/0001.dcm   ← anonymized images (study/series/image, 4-digit padded)
    0001.zip             ← created on study completion
  nnUNet/
    Dataset001_DEFACE/   ← defacing model (downloaded by install.py)
  log/
    pixieveil.log
    anontrail.jsonl      ← original ↔ anonymized UID audit trail
  tmp/                   ← temporary NIfTI work directory (defacing)
```

## Optional: remote upload

Add to `config/settings.yaml` under `storage:`:

```yaml
storage:
  remote_storage:
    base_url: "https://your-storage-server"
    auth_token: "your-bearer-token"
```

Study ZIPs are uploaded via `POST {base_url}/upload` with Bearer-token auth. If omitted, archives stay local.
