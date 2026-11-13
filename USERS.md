# Dashboard users

The PixieVeil dashboard requires a login for every page and API route except
`/login` and `/health`. **There is no sign-up page.** Accounts are created by
whoever runs the server, in one of four ways:

| Method | Who | Needs | Best for |
|---|---|---|---|
| [1. Installer prompt](#1-installer-prompt) | whoever runs `./install` | shell | the very first admin |
| [2. Command line](#2-command-line) | server operator | shell | any user, scripting |
| [3. Users page](#3-users-page-web) | a logged-in admin | browser | day-to-day management |
| [4. Editing `users.yaml`](#4-editing-usersyaml-by-hand) | server operator | shell + editor | config management, bulk adds |

All four write the same file, `config/users.yaml` (or `http_server.users_file`
in `config/settings.yaml`). A running server picks up changes on the next
request. You never need to restart it.

## Rules that apply everywhere

- **Usernames:** 2–32 characters: lowercase letters, digits, `.`, `_`, `-`.
  Input is lowercased (`Alice` becomes `alice`).
- **Passwords:** at least 8 characters. They are stored only as scrypt hashes.
- **Roles:** a user is either a regular user or an **admin**. Both can review
  and export studies. Only admins can manage other users. There are no other
  permissions.
- **Last admin:** the last enabled admin can't be deleted, disabled or demoted.
- **Who owns the file:** on a system-wide install (systemd/OpenRC), run
  commands **as the service account** (e.g. `sudo -u pixieveil …`) so
  `users.yaml` stays owned by it and the server can keep writing it.

---

## 1. Installer prompt

When no users exist, `./install` (and `python install.py`) ends with:

```
Step 8 — Dashboard admin account
  Create the first dashboard admin now? Username (blank to skip):
```

Enter a username and type the password twice to create an **admin**. With
`--non-interactive` / `ASSUME_YES=1`, or without a terminal, the installer
skips this step and prints the command to run instead. Once at least one user
exists, re-running the installer doesn't ask again.

## 2. Command line

```bash
python -m pixieveil.auth <command> …        # inside the venv
./pixieveil.sh users <command> …            # same thing via the control script
sudo -u pixieveil ./pixieveil.sh users …    # system-wide install
```

| Command | Effect |
|---|---|
| `add <user> [--admin]` | create a user (prompts for the password twice) |
| `passwd <user>` | set a new password (logs the user out everywhere) |
| `list` | list users, roles, last login, token count |
| `admin <user> on\|off` | grant or revoke admin |
| `disable <user>` / `enable <user>` | block or unblock login (disabling logs out and stops their API tokens) |
| `delete <user>` | remove the account and its tokens |
| `logout <user>` | end all of the user's browser sessions |
| `token-add <user> <label>` | issue an API token (printed **once**) |
| `token-list <user>` | list a user's tokens (id, label, last used) |
| `token-revoke <user> <id>` | revoke one token |

For scripts, pass the password on stdin instead of the prompt:

```bash
echo 'initial-secret' | python -m pixieveil.auth add bob --password-stdin
```

`--file PATH` (before the command) works on a different users file.

## 3. Users page (web)

Admins see a **Users** link in the dashboard header, which opens `/users`. From
there they can:

- **Add user:** username, initial password, and an optional *Admin* checkbox.
- **Reset password:** sets a new password and logs that user out.
- **Log out:** ends all of that user's browser sessions.
- **Make admin / Revoke admin**, **Disable / Enable**, **Delete**.

Admins can't disable, demote or delete their own account from the page, so
they can't lock themselves out.

Every user, admin or not, has an account menu (their name in the header)
with:

- **Change password:** needs the current password. Their other sessions are
  logged out; the current one stays.
- **API tokens:** create a named token (shown once) or revoke existing ones.
- **Log out**

## 4. Editing `users.yaml` by hand

Write a user with a **plaintext** `password`:

```yaml
users:
  bob:
    password: "initial-secret"
    admin: false          # optional, default false
```

On the next request that touches the user list (a login, a page load), the
server:

1. hashes the password into `password_hash` and **removes the plaintext**,
2. fills in the remaining fields (`disabled`, `created_at`, `last_login`,
   `session_version`, `tokens`),
3. rewrites the file (mode 600).

Writing `password:` on an **existing** user resets their password and logs
them out everywhere. You can also edit `admin:` and `disabled:` by hand. Leave
`password_hash`, `session_version` and `tokens` alone.

Things to watch:

- The server process must be able to **write** the file. Otherwise the login
  still works, but the plaintext stays on disk and an error is logged.
- Passwords shorter than 8 characters are accepted here, with a warning in the
  log. Avoid them.
- A YAML syntax error makes the server keep its last good copy of the users
  and log an error, so fix the file promptly.

[`config/users.example.yaml`](config/users.example.yaml) has a commented
example. To start from it:

```bash
cp config/users.example.yaml config/users.yaml
chmod 600 config/users.yaml
# edit names and passwords; remove the sample "carol" entry
```

---

## API tokens (scripts and partner tooling)

Programs don't log in with a password. Give each one an API token belonging to
a user (method 2's `token-add`, or **API tokens** in the account menu) and send
it as a header:

```bash
curl -H "Authorization: Bearer pv_a1b2c3d4_…" http://<server>:8070/api/me
# {"username": "alice", "admin": true, "via": "token"}
```

A token acts as its user, and the audit trail shows that username. Disabling
or deleting the user, or revoking the token, stops it working. The old shared
`http_server.auth_token` setting is no longer used.

## Sessions and security

- Browser sessions last **12 h idle / 7 days total**
  (`http_server.session_idle_hours`, `session_max_days`).
- **Five** failed logins for one username from one IP lock that pair out for
  **5 minutes**.
- The cookie-signing key is in `config/.session_secret` (created
  automatically). Deleting it logs **everyone** out.
- Over plain HTTP, passwords and cookies travel unencrypted. Off a trusted
  LAN, serve the dashboard through a TLS reverse proxy and set
  `http_server.secure_cookies: true`.
- `config/users.yaml` and `config/.session_secret` are gitignored. Keep them
  mode 600 and out of backups that others can read.

## Audit trail

Exports, mark-exported calls and exam edits record the username in the
study's `<NNNN>.state.json` (`exported_by` plus an `audit` list), in the exam
sidecar (`manual.edited_by`) and in the log (`Audit: user=…`). Deleting a user
doesn't change these records.
