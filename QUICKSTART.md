# PixieVeil Quick Start

## Prerequisites

- Python 3.8+
- pip

## Installation

```bash
git clone https://github.com/cstroie/PixieVeil
cd PixieVeil
pip install -r requirements.txt
cp config/settings.yaml.example config/settings.yaml
```

## Minimum configuration

Open `config/settings.yaml` and set at least:

```yaml
dicom_server:
  ae_title: "PIXIEVEIL"   # AE title your modality will target
  port: 4070

storage:
  base_path: "./data/pixieveil"
  temp_path: "./tmp/pixieveil"

http_server:
  ip: "0.0.0.0"
  port: 8070
```

Everything else has sensible defaults (see `config/settings.yaml.example`).

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

The image will appear in `./data/pixieveil/<study>/<series>/` after processing. Once no new images arrive for `study.completion_timeout` seconds (default 300 s), the study is zipped and — if remote storage is configured — uploaded.

## Output layout

```
data/pixieveil/
  0001/0001/0001.dcm   ← anonymized images (study/series/image, 4-digit padded)
  0001.zip             ← created on study completion
mapping_log.jsonl      ← original ↔ anonymized UID audit trail
data/log/pixieveil.log
```

## Optional: remote upload

Add to `config/settings.yaml` under `storage:`:

```yaml
storage:
  ...
  remote_storage:
    base_url: "https://your-storage-server"
    auth_token: "your-bearer-token"
```

Study ZIPs are uploaded via `POST {base_url}/upload` with Bearer-token auth. If omitted, archives stay local.
