# PixieVeil Quick Start

## Prerequisites

- Python 3.12+
- pip

## Installation

```bash
git clone https://github.com/cstroie/PixieVeil
cd PixieVeil
./install
```

`./install` builds a `.python` virtualenv (Python 3.12), installs PixieVeil into it (`pip install -e .`), then runs `install.py` inside it. See [INSTALL.md](INSTALL.md) for full details, including systemd/OpenRC setup. `install.py` is an interactive setup script that:
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

Copy `config/settings.example.yaml` to `config/settings.yaml` and set at least the values below. Once the server runs, admins can change the RHYTHM connection, retention, series filter and defacing on the dashboard's **Settings** page instead ([SETTINGS.md](SETTINGS.md)).

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

## Create a dashboard user

The dashboard requires a login, and there is no sign-up page. Create the first
admin on the server (`./install` offers to do this for you):

```bash
python -m pixieveil.auth add alice --admin     # or: ./pixieveil.sh users add alice --admin
```

Admins can add more users on the dashboard's **Users** page. See [USERS.md](USERS.md) for every option.

### Or: the first-run wizard

Skip both steps above and just start the server. With no users yet, it prints a
**setup code** to the console and log (also saved in `config/.setup_code`):

```
No dashboard users yet. Open http://<this server>:8070/setup and enter the setup code ABCD-EFGH
```

Open the dashboard; every page leads to `/setup`. Enter the code, then the wizard
walks through:

1. the first admin account;
2. receiving: DICOM AE title, port, anonymization profile (`rhythm` recommended);
3. RHYTHM connection (optional), with **Test connection**;
4. defacing (it shows whether this server has a CUDA GPU and the model);
5. retention after export and the storage quota;
6. a review, then **Finish**.

Finish creates `config/settings.yaml` from the example if it doesn't exist (the
previous version is backed up otherwise), creates the admin and logs you in.
**Restart the server** afterwards so the new settings take effect. The wizard then
disappears for good (`config/.setup_complete`).

The code is required because, until the first admin exists, anyone who can reach
the dashboard could otherwise claim the server. If an admin already exists
(e.g. created by `./install`) but `config/settings.yaml` doesn't, the wizard is
offered to that admin after login instead, and can be skipped. An existing
installation (users and `settings.yaml` both present) never sees it.

## Start the server

```bash
python pixieveil.py
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

Three export transports are supported. Export is always manual — a Send/Upload click on the `/studies` dashboard; nothing leaves automatically. A study can be sent via more than one transport; the first successful export does the retain-or-delete bookkeeping (DICOM wins if sent via both DICOM and HTTP).

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

### RHYTHM CT upload platform

Uploads a per-study ZIP to the [RHYTHM](https://github.com/jsfakian/rhythm) platform over its v2 resumable chunked-upload API. RHYTHM re-validates GDPR anonymization, assigns the Repository Study ID asynchronously, and pushes to its own Orthanc. On the dashboard the study shows an `uploaded` RHYTHM badge until PixieVeil polls the job to `COMPLETE`, at which point the local copy is removed (or retained per `retain_after_export_days`).

```yaml
storage:
  remote_storage:
    rhythm:
      base_url: "https://rhythm.your-domain"
      auth_token: "rhythm-upload-token"   # from `manage.py create_upload_token`, not a login
      site_code: "S014"                   # your institution's code, assigned by RHYTHM
      # chunk_size: 10485760              # optional, default 10 MiB
```

**`site_code`** is assigned by RHYTHM when your institution registers — you can't pick your own. Find it on your RHYTHM profile page, ask the repository admin, or read it off an existing Repository Study ID (the `S###` segment of `RHY-S014-HEADTRAUMA-NC-PH-G4-000001`). All three keys must be set before `RhythmStorage.enabled` is true and the dashboard button activates.

**Protocols.** RHYTHM v2 manifest items name one `protocol_id`: the UUID of a `CTProtocol` registered on RHYTHM. RHYTHM derives anatomical region, clinical indication, contrast, protocol type, examination group and scanner from it. On the dashboard's **Protocols** page (`/protocols`, admins edit), each local protocol links the ProtocolName(s) your scanner sends (e.g. `CAP_N_PESTE_6A`) to that UUID, plus the region/indication/contrast to pre-fill. Studies that match nothing land in its **Unmatched** inbox. From there:
- **Add as alias** gives an existing protocol another name;
- **Import from RHYTHM** takes protocols already registered there and adds your scanner's names to them;
- **Draft from study** pre-fills a new protocol from the study's dose report and headers and creates it on RHYTHM, creating the scanner profile too if RHYTHM doesn't have it yet.

Matched studies get their RHYTHM protocol and indication filled in automatically, and an operator can still override them per study on `/studies`. The full workflow is in [PROTOCOLS.md](PROTOCOLS.md#the-workflow-from-a-new-study-to-an-upload). Everything is stored in `config/protocols.yaml`, which is created on first start from the older `config/rhythm_protocols.yaml` / `integrations/rhythm/*_lookup.yaml` if those exist.

Requirements before a study can be sent to RHYTHM:
- `anonymization.profile: rhythm` — only this profile strips every tag RHYTHM's GDPR-strict ingest validator requires absent (dates/times, device identity like `Manufacturer`/`StationName`/`DeviceSerialNumber`, all descriptions, and `PatientName`/`PatientID` entirely — RHYTHM keys studies on its own Repository Study ID). The `research` and `gdpr` profiles both fail that validator. The dashboard button is disabled otherwise. **This gate reads the *current* config, not what each study was anonymized with**: a study received under `research`/`gdpr` and only later switched to `rhythm` will pass the button but be rejected by RHYTHM at ingest. Anonymization runs once, at receive time, and PixieVeil keeps only the anonymized copy — so re-send the original DICOM through PixieVeil under the `rhythm` profile rather than trying to re-export an old study.
- a per-study **Image quality** value, set on the `/studies` page (extraction leaves it blank)
- a **RHYTHM protocol** with a UUID: matched automatically from the Protocols page, or picked on `/studies` (its UUID becomes the manifest's `protocol_id`)
- defacing completed (a `defacing_failed` study is blocked)

The Dose Report screenshot and any RDSR object are excluded from the RHYTHM ZIP automatically — it carries CT images only.

**Preview the manifest before uploading.** `GET /api/studies/{n}/rhythm-manifest` (or `?studies=1,2,3` for a batch) returns the exact v2 manifest PixieVeil would send, with a `blockers` key listing anything still missing; add `&download=1` for a clean `rhythm_manifest.json` to paste into RHYTHM's own `/json-validator` page or `POST /api/v1/uploads/validate-manifest/`. The `/studies` page has per-study and bulk "preview / download" links.

If no transport is configured, archives stay local.

### Recovery on restart

Each study has a JSON sidecar (`<study_number>.json`) that tracks its status. On restart:
- Studies that were mid-processing (`complete` or `defacing` state) are automatically re-queued.
- Studies kept locally with no remote configured (`archived_via: null`) are re-queued if a remote is now configured — allowing deferred export after adding remote storage settings.

## Defacing

Defacing removes facial features from CT/MR head scans using nnU-Net segmentation. It runs automatically after anonymization on series identified as head scans.

```yaml
defacing:
  enabled: true
  device: "cuda"             # "cuda", "mps" (Apple Silicon), or "cpu"
                             # falls back to cpu automatically if the configured device is unavailable
  keep_backup: true          # keep anonymized-but-not-defaced series as <series>_pre_deface/; set false in production
  rotation_mode: "iop"       # "none" to skip; anything else applies IOP-based transpose (recommended)
  mask_dilation_mm: 2.0      # grow the face mask by this many mm before applying; eliminates skin-edge residue
  model_dir: ./data/nnUNet
  body_parts:
    - HEAD
    - BRAIN
    - NECK
    - SKULL
  series_description_pattern: "(?i)(head|brain|skull|cranial|cerebr)"
```

At startup, PixieVeil checks whether the configured device is actually usable (including a test CUDA allocation) and falls back to CPU with a warning if not. Only one nnU-Net inference runs at a time regardless of how many studies complete simultaneously.
