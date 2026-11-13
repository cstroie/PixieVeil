# Installing and running PixieVeil

## Prerequisites

- Python 3.12+ (hard requirement — `install.py` gates on it, and defacing's `torch`/`nnUNetv2` wheels are pinned against it)
- A DICOM-capable modality or test tool (e.g. `dcmtk`'s `storescu`/`echoscu`) to send images
- Optional: an NVIDIA GPU + CUDA drivers, if you want defacing to run on GPU rather than CPU

## Install

```bash
git clone https://github.com/cstroie/PixieVeil.git
cd PixieVeil
./install
```

`./install` creates a `.python` virtualenv (using `python3.12` specifically — override with `PYTHON=/path/to/python3.12` if it's not on `PATH` under that name), installs PixieVeil into it (`pip install -e .`), and then runs `install.py` inside that same venv, which:

- Detects CUDA and asks whether to enable defacing (CPU or GPU)
- Installs the matching `torch`/`nnUNetv2`/`gdown` wheels for that choice
- Creates the runtime directories under `data/`
- Downloads the nnU-Net defacing model from Google Drive if missing
- Writes the defacing choice back to `config/settings.yaml`
- Runs sanity checks (settings load, package imports)

Re-run `./install` any time — it's idempotent, reusing the existing venv and re-checking each step. `pixieveil.sh` and the systemd/OpenRC service files all use `.python/bin/python3` automatically once it exists.

The repo carries a few git submodules under `reference/`, all upstream projects kept for reference only — not imported, packaged, linted, or tested:

- `reference/uploader` — the RHYTHM uploader (`jsfakian/rhythm`)
- `reference/deface` — CT defacing pipeline (`jsfakian/CT-DEFACE`)
- `reference/anonymization` — DICOM de-identification tool (`jsfakian/dicom_anonymization`)

None are needed to run PixieVeil; clone with `--recurse-submodules`, or run `git submodule update --init` afterwards, if you want them. Refresh with `git submodule update --remote reference/<name>`.

Extra arguments pass straight through to `install.py`:

```bash
./install --non-interactive     # skip prompts, keep the current settings.yaml choice
./install --download-model      # just (re)download the defacing model and exit
```

## Configuration

```bash
cp config/settings.example.yaml config/settings.yaml
```

Then set at least:

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

### Dashboard users

The dashboard requires a login and has no sign-up page. `./install` (via
`install.py`) offers to create the first admin when none exists. Otherwise,
create one by hand, **as the account the service runs as** so the file is
owned correctly:

```bash
python -m pixieveil.auth add alice --admin          # prompts for the password
sudo -u pixieveil ./pixieveil.sh users add alice --admin   # system-wide install
```

Other subcommands: `passwd`, `list`, `disable`/`enable`, `delete`,
`admin <user> on|off`, `logout` (end all browser sessions), `token-add`,
`token-list`, `token-revoke`. Admins can do the same on `/users`. A running
server applies changes on the next request.

An admin with shell access can also add a user (or reset a password) by
editing `config/users.yaml` directly and writing a plaintext `password`:

```yaml
users:
  bob:
    password: "initial-secret"   # hashed and removed on next access
    admin: false
```

The next time the file is read (the next login or request, no restart
needed), the plaintext is replaced with a hash, missing fields are filled in
and the file is rewritten. Setting `password` on an existing user resets it
and logs them out. The file must be writable by the service account, or the
plaintext stays on disk; this is logged as an error.

Users live in `config/users.yaml` (scrypt-hashed passwords, mode 600). The
session-signing key is in `config/.session_secret`, which is created on first
start. Both files are gitignored. Deleting the secret logs everyone out.
Sessions end after 12 h idle or 7 days in total
(`http_server.session_idle_hours` / `session_max_days`). Five failed logins
for one username from one IP lock that pair out for 5 minutes.

Scripts and partner tooling (e.g. the `mark_exported` write-back) use a
per-user API token, sent as `Authorization: Bearer pv_<id>_<secret>`. The old
shared `http_server.auth_token` is no longer used; startup warns if it is
still set. Every export, mark-exported and exam edit is attributed to its user
in the study's `.state.json` (`exported_by`, `audit`) and in the log.

**HTTPS.** Passwords and session cookies travel in clear text over plain
HTTP. Off a trusted LAN, put the dashboard behind a TLS reverse proxy (nginx,
Caddy) and set `http_server.secure_cookies: true`.

`config/settings.yaml` is gitignored — it can hold a remote-export bearer token (`storage.remote_storage.http.auth_token` or `storage.remote_storage.rhythm.auth_token`), which is why `./install`'s systemd/OpenRC modes `chmod 600` it automatically. Everything else in `settings.example.yaml` has sensible defaults; see [QUICKSTART.md](QUICKSTART.md) for the full option reference (series filtering, defacing, anonymization profiles, remote export).

## Running directly

```bash
python3 pixieveil.py
```

Listens on `http://0.0.0.0:8070/` (dashboard) and DICOM port `4070` by default. Stop with `Ctrl-C` — services shut down gracefully (in-flight exports and sidecar writes finish before exit).

```bash
python3 pixieveil.py --pidfile pixieveil.pid   # write PID for pixieveil.sh, remove on clean exit
```

`SIGTERM`/`SIGINT` both trigger the same graceful-shutdown path (the asyncio signal handlers in `pixieveil.cli.main()`) — no abrupt kill.

## Running via ./pixieveil.sh

```bash
./pixieveil.sh start [extra args]     # foreground; refuses if already running
./pixieveil.sh stop
./pixieveil.sh restart [extra args]   # stop, then start (foreground)
./pixieveil.sh status
```

`start`/`restart` do **not** background the process themselves (no fork, no `nohup`) — they `exec` `pixieveil.py` in the foreground, same as `Type=simple` in the systemd unit below. Background it yourself if you need that:

```bash
./pixieveil.sh start &
nohup ./pixieveil.sh start &
```

The pidfile defaults to `<data>/pixieveil.pid`, where `<data>` is derived from `storage.base_path` in `config/settings.yaml` (e.g. `./data/dicom` → `./data`) — same directory as the rest of PixieVeil's runtime state. Falls back to `data/pixieveil.pid` if settings can't be loaded. Override with `PIDFILE=...`. It distinguishes a running instance from a stale one; `stop`/`restart` wait up to `STOP_TIMEOUT` (default 15s) before escalating to `SIGKILL`. Extra args after the subcommand pass straight through to `pixieveil.py`.

## System-wide install (systemd or OpenRC)

`./install systemd [user]` and `./install openrc [user]` (OpenRC covers Alpine
and other OpenRC systems) automate the whole system-wide setup on top of the
regular venv install. `./install service [user]` auto-detects which one to
use (checks for `systemctl`, then `rc-update`):

```bash
git clone https://github.com/cstroie/PixieVeil.git /opt/pixieveil
cd /opt/pixieveil
sudo ./install service            # auto-detect, or: sudo ./install systemd / openrc
sudo ./install systemd daemon     # reuse an existing account instead of creating "pixieveil"
```

These modes need root — creating/reusing a system account, chowning the
checkout, and writing into `/etc` all require it. Before touching anything,
the script prints a plan (the account it'll create or reuse — with `id`
output if it already exists — the chown, the files it'll write) and asks
`Proceed? [y/N]`; pass `ASSUME_YES=1` to skip the prompt for automation (this
also passes `--non-interactive` through to `install.py`, so defacing stays
at whatever `config/settings.yaml` currently says). A plain username that
doesn't look like `[a-z_][a-z0-9_-]*` is rejected, and picking a shared
account (`daemon`, `nobody`, `www-data`, ...) prints a warning but is still
allowed.

Once confirmed, for each mode:

- create the account (`useradd --system`/`adduser -S -D -H`, no shell, home
  = the checkout directory) if it doesn't already exist — if it does, it's
  reused as-is, untouched
- `chown -R` the checkout to that account (this includes `.git` — a later
  `git pull` as your own login will need `sudo` too) and `chmod 600`
  `config/settings.yaml` if present (it can hold a remote-export bearer
  token)
- create `data/log/` under the checkout, owned by that account — logs (both
  the app's own `logging.file` output and, for OpenRC, `supervise-daemon`'s
  captured stdout/stderr) stay there rather than under `/var/log`
- **(re)build `.python` and run `install.py` as the service account itself**,
  via `su`, not as root — root only ever chowns the checkout to that account
  first, so a stale or tampered venv from an earlier unprivileged run is
  never executed with root privileges
- render and install the unit/init script, backing up any file it would
  overwrite first (`<path>.bak.<timestamp>`):
  - systemd: `pixieveil.service` → `/etc/systemd/system/`, with its
    `/opt/pixieveil` paths and `User=`/`Group=pixieveil` rewritten to the
    actual checkout dir/account, then `systemctl daemon-reload`
  - OpenRC: `pixieveil.openrc` → `/etc/init.d/pixieveil` as-is, plus
    `/etc/conf.d/pixieveil` with `pixieveil_home`/`pixieveil_user`/
    `pixieveil_group` overrides for whichever differ from the shipped
    defaults
- (re)write `UNINSTALL.md` in the checkout (gitignored) with the exact
  commands to reverse this run — it only tells you to remove the account if
  this run actually created it, never for one that already existed

Neither mode enables, registers, or starts the service itself — that's a
separate, explicit last step (the install script prints these same commands
at the end):

```bash
# systemd
sudo systemctl enable --now pixieveil   # register at boot and start now
sudo systemctl status pixieveil         # verify it's running
journalctl -u pixieveil -f              # follow logs

# OpenRC
sudo rc-update add pixieveil default    # register to start at boot
sudo rc-service pixieveil start         # start now
sudo rc-service pixieveil status        # verify it's running
```

Both service files run PixieVeil from `.python/bin/python3` (not system
Python) — that's where `install.py` puts `torch`/`nnUNetv2` for defacing.
`Type=simple` (systemd) / `supervise-daemon` (OpenRC) cover backgrounding,
restart-on-crash, and log capture; neither uses a custom fork-based daemon
mode, since forking after the asyncio event loop starts is fragile.

Re-running `./install systemd`/`openrc` is safe — it reuses the account if
it already exists and just refreshes the venv, permissions, unit/init file,
and `UNINSTALL.md`.

## Verifying the install

```bash
curl http://localhost:8070/health
# {"status": "ok"}
curl -H "Authorization: Bearer pv_..." http://localhost:8070/api/me
# {"username": "alice", "admin": true, "via": "token"}

echoscu localhost 4070 -aec PIXIEVEIL        # requires dcmtk
storescu localhost 4070 -aec PIXIEVEIL /path/to/image.dcm
```

The dashboard is at `http://localhost:8070/`. See [QUICKSTART.md](QUICKSTART.md) for the full output layout, remote-export configuration, and defacing options.

## Linting

No automated test suite exists yet; manual linting (install the tools into
the venv first — they aren't part of the regular dependencies):

```bash
.python/bin/pip install flake8 mypy
.python/bin/flake8 pixieveil/ --max-line-length=100
.python/bin/mypy pixieveil/
```
