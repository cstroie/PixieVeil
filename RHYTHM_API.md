# RHYTHM API reference (for PixieVeil)

What PixieVeil needs to know about the RHYTHM CT upload platform's API. It is
taken from RHYTHM's own code, vendored as the `reference/uploader` git submodule
(`ct_upload_platform/uploads/urls.py`, `chunked_upload_views.py`,
`protocol_api_views.py`, `auth.py`). **The live RHYTHM server may be newer** than
that copy: when in doubt, `git submodule update --remote reference/uploader` and
check again.

All endpoints below are relative to `<base_url>/api/v1/`, where `base_url` is
`storage.remote_storage.rhythm.base_url` (for example `https://rhythm.example.org`).

---

## Authentication

- Every call sends **`Authorization: Bearer <token>`** (`Authorization: Token <token>`
  works too).
- The token is a Django REST Framework token: **one per RHYTHM user, no expiry**.
  It stays valid until an admin deletes it.
- It acts as that user. Data is scoped to the user's **institution (site code)**:
  you see and create only your site's scanner profiles and protocols, unless the
  user is a RHYTHM admin. Ask for the token of a user at **your** site (`S014`),
  because the site code on each upload must match the protocol's site code.
- Browser pages on RHYTHM itself also accept the logged-in session cookie. That
  only matters inside RHYTHM.

### One token per user

RHYTHM has **exactly one API token per user**. Two names suggest otherwise, but
they don't refer to a separate token:
- `UPLOAD_TOKEN_EXPIRY_DAYS` in RHYTHM's settings is unused;
- `manage.py create_upload_token` creates or shows that same token (`--force`
  replaces it).

The login, the bookmarklet, the sign-in helper and the admin's command all give
the same value. Don't confuse it with RHYTHM's browser session cookie (website
only, 1-hour idle timeout) or with PixieVeil's own `pv_…` API tokens (for
scripts calling PixieVeil).

### Lifetime: permanent, no refresh needed

- RHYTHM uses DRF's stock `TokenAuthentication` (`uploads/auth.py`
  `BearerTokenAuthentication` only adds the `Bearer` keyword). A token is a
  database row checked by lookup. It has **no expiry date**, and the only other
  check is that the user is active.
- RHYTHM's settings file defines `TOKEN_EXPIRY_DAYS` (30) and
  `UPLOAD_TOKEN_EXPIRY_DAYS` (90), but **nothing in the vendored code reads them**.
  If a newer RHYTHM starts enforcing `TOKEN_EXPIRY_DAYS`, the token would expire
  after 30 days. Re-check after a submodule update.
- Logging in again **reuses** the existing token (`Token.objects.get_or_create`),
  so fetching it again (bookmarklet, sign-in) always gives the same value.
- The token stops working only when:
  - an admin **rotates** it (`manage.py create_upload_token <user> --force`) or
    deletes it in Django admin;
  - the RHYTHM user is **deactivated or deleted**;
  - RHYTHM's optional **IP whitelist** (`IP_WHITELIST`, off by default) is enabled
    without this server's address.

  Then uploads fail with an authentication error and **Test connection** reports
  the token as rejected. Get it again with either helper below and restart.
- RHYTHM's 1-hour idle session timeout (`SESSION_TIMEOUT_SECONDS`) affects only
  the RHYTHM website in a browser, not the API token.
- **Ask the RHYTHM admin** not to rotate the token PixieVeil uses, and whether the
  IP whitelist is on (if so, give them this server's address).

### How the token is issued

There is no "show my token" page. RHYTHM returns the token only at the end of
its login:

```
POST /api/v1/auth/login/        {"username": "...", "password": "..."}
  → 200 {"token": "...", "username": ...}      2FA not required (rare)
  → 200 {"requires_2fa": true}                 enter the authenticator code next
  → 200 {"requires_2fa_setup": true}           2FA not enrolled yet: use RHYTHM's login page once
  → 401 {"error": "..."}                       wrong credentials
POST /api/v1/auth/verify-2fa/   {"code": "123456"}   ← same session cookie as the login call
  → 200 {"token": "...", "user_id", "username", "email", "is_staff", ...}
```

RHYTHM's own login page then stores the response in that site's browser storage:
`localStorage['token']` and `localStorage['user']`.

### Getting the token into PixieVeil

On **Settings → RHYTHM connection** ([SETTINGS.md](SETTINGS.md)):

1. **From your open RHYTHM tab (recommended).** Drag **Copy RHYTHM token** to the
   bookmarks bar, switch to the RHYTHM tab where you are signed in, and click it.
   It reads `localStorage['token']`, checks it against
   `GET /api/v1/scanner-profiles/`, and copies it to the clipboard. It also shows
   RHYTHM's address for *Base URL*. Paste the token into *API token* and click
   **Save changes**. Your password never reaches PixieVeil. If bookmarklets are
   blocked, paste the same code (shown under the link) into the RHYTHM tab's
   browser console (F12).
2. **Sign in to RHYTHM from PixieVeil.** Enter your RHYTHM username, password and
   then the 2FA code. The PixieVeil server makes the two calls above and saves
   the token straight into `settings.yaml`. The token is never shown in the
   browser or written to the log. The password is used for that one request and
   not stored.
3. **By hand.** In the RHYTHM tab's console: `copy(localStorage.getItem('token'))`.
4. **From the RHYTHM admin.** They can create a token for your user in Django
   admin.

The token is applied **after a restart**. Then use **Test connection**.

---

## Uploading a study (what PixieVeil does)

`RhythmStorage.upload_study()` in `pixieveil/storage/rhythm_storage.py`, one ZIP per
study:

| Step | Request | Body / query | Response |
|---|---|---|---|
| 1. Start | `POST uploads/chunked/init/` | JSON `filename`, `total_size`, `chunk_size` (default 10 MiB), `file_hash` (sha256 of the ZIP), `batch`, `manifest_item` | `session_id`, `total_chunks`, `chunk_size`, `expires_at`. `manifest_item` is validated here: `400 {"code": "invalid_manifest_item"}` |
| 2. Chunks | `POST uploads/chunked/{session_id}/chunk/?chunk_number=N&chunk_hash=<sha256>` | raw bytes (any content type), 0-based `N` | `uploaded_chunks`, `progress_percent`, `status` |
| 3. Finish | `POST uploads/chunked/{session_id}/complete/` | `{"file_hash": "<sha256 of whole ZIP>"}` | `status: "COMPLETED"`, **`job_id`**, **`job_status_url`** (`/api/v1/uploads/{job_id}/`). `409` if chunks are missing |
| 4. Result | `GET uploads/{job_id}/` | | `status` `PENDING` / `PROCESSING` / `COMPLETE` / `PARTIAL` / `FAILED`, `error_report`, `image_count`, `submitted_at`, `completed_at` |

Step 3 only means *queued*. RHYTHM then validates every slice (including its
GDPR-strict tag check) and ingests asynchronously. PixieVeil polls step 4 each
completion-loop tick (`reconcile_rhythm_jobs`).

**`manifest_item`** (v2, built by `pixieveil/processing/rhythm_manifest.py`) carries
`ref`, `filename`, `site_code`, **`protocol_id`** (the UUID of a protocol registered on
RHYTHM; RHYTHM derives region, indication, contrast, protocol type, exam group and
scanner from it), `image_quality`, and optional dose and patient numbers.

**RHYTHM's ID for the study.** The job reply has no study id. Once a job is `COMPLETE`,
PixieVeil calls `GET studies/<anonymized StudyInstanceUID>/` (RHYTHM stores our UID unchanged)
and keeps `patient_pseudo_id` (the **Repository Study ID**) and `orthanc_study_id` on the study's
`.state.json`. Look a patient up on **/lookup** (admin) or with
`python -m pixieveil.storage.correspondence <PatientID>` to find the IDs an erasure request to
RHYTHM must name.

Other upload endpoints, not used yet:

| Request | Purpose |
|---|---|
| `GET uploads/chunked/{id}/progress/` | progress of a session |
| `GET uploads/chunked/{id}/status/` | per-chunk status plus `needs_reupload: [..]`, for resuming |
| `POST uploads/chunked/{id}/verify/?chunk_numbers=1,2` | re-check stored chunks (SHA-256 + CRC32) |
| `DELETE uploads/chunked/{id}/` | cancel a session (204) |
| `POST uploads/validate-manifest/` | `{"manifest": {...}}` → `{valid, errors[{field, code, message}]}`: check a manifest before uploading |
| `GET uploads/` | your upload jobs (paginated) |
| `GET studies/`, `GET studies/{study_uid}/` | ingested studies |

---

## Scanners and protocols

| Request | Returns / does |
|---|---|
| `GET manufacturers/` (`?search=`) | the manufacturer catalogue |
| `GET manufacturers/{id}/scanner_models/` | that manufacturer's catalogue models |
| `GET scanner-models/` (`?search=SOMATOM`, `?manufacturer=<id>`) | the scanner **model catalogue**, maintained by the RHYTHM admin. A model must be here before you can create a profile for it |
| `GET / POST / PATCH / DELETE scanner-profiles/` | **your site's scanners**: `id`, `manufacturer`, `scanner_model`, `detector_rows`, `year_of_installation`, `local_protocol_note`, `site_code` (set by RHYTHM) |
| `GET / POST / PATCH / DELETE protocols-api/` (`?scanner=<profile id>`, `?protocol_type=`, `?search=`, `?ordering=`) | **your site's protocols**. `id` is the **`protocol_id`** an upload names. Also `scanner`, `protocol_name`, `protocol_type`, `age_group`, `anatomical_region`, `clinical_indication`, `contrast`, technique fields (`kvp`, `pitch`, `rotation_time`, `slice_thickness`, `kernel_class`, …), `site_code` |
| `GET protocols-api/by-type/?protocol_type=` | protocols of one type |
| `GET protocol-choices/`, `protocol-choices/by-key/{key}/`, `protocol-choices/{id}/options/?protocol_type=` | the allowed values for protocol fields |

**Creating a protocol** (used by PixieVeil's *Draft protocol from study*):
`POST protocols-api/` with `scanner` (profile id), `protocol_type`
(`PEDIATRIC_HEAD` / `PEDIATRIC_BODY` / `YOUNG_ADULT`), `age_group`, `protocol_name`,
`anatomical_region`, `clinical_indication`, `scan_type`, `contrast`,
`number_of_phases`, `auto_kvp_selection`, `kvp`, `auto_ma_modulation`, `mas_inputs`
(`{label: value}`), `pitch`, `rotation_time`, `slice_thickness`, `scan_fov`,
`kernel_class`, `reconstruction_algorithm`, `protocol_intent`, `notes` → `201` with
the record (`id`). `site_code` and `created_by` come from the token's user. Caveats
in the vendored code:
- the API does **not** check values against the option lists, and does **not**
  check for duplicates (RHYTHM's website does both), so PixieVeil checks both
  before sending;
- `examination_group` and `clinical_comments` are **not** part of the API
  serializer. They are set only by RHYTHM's website save (`/protocols/api/save/`,
  which needs a browser session), so set the examination group on RHYTHM's site
  afterwards.

**Creating a scanner profile** (used when a draft's scanner is new to RHYTHM):
`POST scanner-profiles/` with `manufacturer` and `scanner_model` (catalogue ids from
`manufacturers/` / `scanner-models/`), `detector_rows` and `year_of_installation`
(protocol-choices options, or the free text after "Other: Please Specify"), and
`local_protocol_note` → `201` with the profile (`id`). `site_code` / `created_by` come
from the token's user. The **catalogue** (manufacturers, models) is read-only over the
API; only RHYTHM admins extend it. Catalogue model names may carry the brand
("Siemens SOMATOM Force"), so PixieVeil compares model words without manufacturer or
brand words.

**Protocol options:** `GET protocol-choices/` →
`[{key, label, options: [{value, display, applicable_protocol_types}]}]`. Keys are
`scan_type`, `contrast`, `number_of_phases`, `auto_kvp_selection`, `kvp`,
`auto_ma_modulation`, `pitch` (ranges such as `0.5-0.8`), `rotation_time`,
`slice_thickness`, `scan_fov`, `kernel_class`, `reconstruction_algorithm`,
`protocol_intent`, `age_group_pediatric_head` (`lt_3m`, `3m_1y`, `1y_6y`, `gt_6y`),
`age_group_pediatric_body` (`lt_5kg` … `50_80kg`), `age_group_young_adult`
(`gt_80kg`), `examination_group_*`, and others. Vendor-specific auto kV / mA
lists (for example Siemens `CareDose4D`) and the mA inputs each modulation needs
(CareDose4D → "Quality Reference mAs (QR mAs)") are **not** served by the API.
PixieVeil keeps them, with an offline copy of all the options, in
`pixieveil/dashboard/rhythm_choices.json` (generated from RHYTHM's
`populate_protocol_choices.py`).

**Pagination:** lists come 20 per page as `{"count", "next", "previous", "results"}`.
Follow `next`, or ask for up to `?page_size=100`.

On RHYTHM's website the same things live at `/scanners/` (scanner profiles) and
`/protocols/records/` (protocols).

---

## Our scanner

Read from our studies' DICOM headers:

| Tag | Value |
|---|---|
| Manufacturer | `SIEMENS` |
| ManufacturerModelName | `Emotion 16` |
| SoftwareVersions | `syngo CT 2013A` |
| Collimation | 16 × 1.2 mm (16 detector rows) |

The vendored catalogue has no Emotion 16, so ask the RHYTHM admin to add:

> **Manufacturer:** Siemens Healthineers · **Model:** SOMATOM Emotion 16
> (16-slice; DICOM Manufacturer `SIEMENS`, ManufacturerModelName `Emotion 16`,
> syngo CT 2013A)

Check with `GET scanner-models/?search=Emotion`. Then create your scanner profile
(detector rows 16, installation year) and register its protocols on RHYTHM. Put
each protocol's `id` into PixieVeil's [`/protocols`](PROTOCOLS.md) page as its
RHYTHM protocol UUID.

---

## Follow-ups for PixieVeil

- ~~Test connection reads only the first page~~ — done: `rhythm_storage.fetch_all()`
  follows `next` (only on RHYTHM's own host), 100 per page.
- ~~Import from RHYTHM~~ — done: `/protocols` → **Import from RHYTHM** reads
  `scanner-profiles/` + `protocols-api/` and proposes UUIDs per scanner/protocol
  (see [PROTOCOLS.md](PROTOCOLS.md#import-from-rhythm)).
- Optionally call `uploads/validate-manifest/` before an upload to surface
  RHYTHM's own manifest errors early.
- Resume an interrupted upload via `uploads/chunked/{id}/status/` instead of
  starting over.
