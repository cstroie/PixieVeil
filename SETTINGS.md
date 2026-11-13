# Settings page

Admins can change the routine parts of `config/settings.yaml` on the dashboard's
**Settings** page (`/settings`). Everything else stays a file edit on the server.

## What can be changed

| Card | Fields | Applies |
|---|---|---|
| RHYTHM connection | base URL, API token, site code | on restart |
| Storage & retention | keep after export (days), storage quota (GB) | **live** |
| Study reception | max study size (MB) | **live** |
| | study complete after (s idle), completion check interval | on restart |
| Series filter | excluded modalities, original series only, include/exclude rules | on restart |
| Defacing | enabled, device, keep undefaced backup, mask dilation | on restart |

**Live** fields take effect immediately. For the others the page shows a
**Restart required** banner until the server is restarted
(`sudo systemctl restart pixieveil` or `./pixieveil.sh restart`).

- **API token:** never shown again once saved. Type a new one to replace it, or click
  **Clear**.
- **Getting the token:** the RHYTHM card offers a **Copy RHYTHM token** bookmarklet
  (run it on your signed-in RHYTHM tab; it checks and copies the token) and
  **Sign in to RHYTHM…** (username, password and 2FA code; the token is saved
  directly and never shown). Details in [RHYTHM_API.md](RHYTHM_API.md).
- **Test connection** checks RHYTHM with the values in the form, or the saved ones
  when a field is left empty. It reports whether the server is reachable and the
  token accepted, and which of the protocol catalog's UUIDs RHYTHM knows.
- **Site code** must match the protocol catalog's `site_code` (see
  [PROTOCOLS.md](PROTOCOLS.md)), or the save is refused.
- **Include/exclude rules:** one `Attribute: pattern` per line, Python regular
  expressions; include rules win over exclude rules.

## What is read-only, and why

DICOM AE title, IP and port, dashboard IP and port, secure cookies, the
anonymization profile and its rules, storage paths and logging. A mistake in
these can lock you out of the dashboard or weaken anonymization, so they are
shown on the page but changed only by editing the file and restarting.

## Saving

- Only admins can open the page or call its API. Other users don't see the link.
- If the file was changed since the page was loaded (by hand or by another admin),
  the save is refused and the page asks you to reload.
- Before each save the previous file is copied to
  `config/settings.yaml.bak.<UTC time>` (mode 600, gitignored). The newest 10 are
  kept.
- The file is rewritten in full and stays mode 600. **Comments in
  `settings.yaml` are not preserved.** `config/settings.example.yaml` keeps the
  documented version of every option.
- Each save is logged as `Audit: user=… action=settings_change paths=[…]`. Only
  the names of the changed settings are logged, never their values.
- A server started without `config/settings.yaml` (running on the example file)
  has nothing to edit; the page says so.

## API

| Request | What |
|---|---|
| `GET /api/settings` | fields with current values (secrets only as `is_set`), read-only values, `version`, `restart_pending` |
| `PUT /api/settings` | `{"changes": {"<dotted.path>": value}, "version": "<from GET>"}`; `400` with `errors` by path, `409` if stale; returns `changed`, `applied_live`, `restart_required` |
| `POST /api/settings/rhythm-test` | optional `{"base_url", "auth_token"}`; returns `ok`, `status`, `message`, `found`/`missing` UUIDs |
| `POST /api/settings/rhythm-login` | `{"base_url", "username", "password"}`: signs in to RHYTHM; saves the token, or returns `{needs_2fa, pending}` |
| `POST /api/settings/rhythm-login/verify` | `{"pending", "code"}`: finishes the 2FA step and saves the token (never returned) |

All of these are admin-only and need a login or an API token (see [USERS.md](USERS.md)).
