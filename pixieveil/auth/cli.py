"""``python -m pixieveil.auth`` — manage dashboard users from the shell.

    python -m pixieveil.auth add alice --admin
    python -m pixieveil.auth passwd alice
    python -m pixieveil.auth list
    python -m pixieveil.auth disable alice | enable alice | delete alice
    python -m pixieveil.auth admin alice on|off
    python -m pixieveil.auth logout alice          # kill all browser sessions
    python -m pixieveil.auth token-add alice "export watcher"
    python -m pixieveil.auth token-list alice
    python -m pixieveil.auth token-revoke alice <token-id>

The users file comes from ``http_server.users_file`` in config/settings.yaml
(default config/users.yaml); ``--file`` overrides it. A running server picks
up changes on the next request.
"""

import argparse
import getpass
import logging
import sys
from pathlib import Path

from pixieveil.auth.user_store import DEFAULT_PATH, UserStore, UserStoreError

logger = logging.getLogger(__name__)


def resolve_users_file(explicit: str | None = None) -> Path:
    if explicit:
        return Path(explicit)
    try:
        from pixieveil.config import Settings
        settings = Settings.load()
        configured = settings.http_server.get("users_file")
        if configured:
            return Path(configured)
    except Exception as e:  # noqa: BLE001 — no/invalid settings: fall back to default
        logger.debug(f"Settings not loaded ({e}); using default users file")
    return DEFAULT_PATH


def _read_password(args: argparse.Namespace) -> str:
    if args.password_stdin:
        return sys.stdin.readline().rstrip("\n")
    first = getpass.getpass("Password: ")
    if getpass.getpass("Repeat password: ") != first:
        raise UserStoreError("passwords do not match")
    return first


def _parse_onoff(value: str) -> bool:
    v = value.lower()
    if v in ("on", "yes", "true", "1"):
        return True
    if v in ("off", "no", "false", "0"):
        return False
    raise UserStoreError(f"expected on/off, got {value!r}")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="python -m pixieveil.auth",
                                description="Manage PixieVeil dashboard users.")
    p.add_argument("--file", help="users YAML file (default: from settings)")
    sub = p.add_subparsers(dest="cmd", required=True)

    a = sub.add_parser("add", help="create a user")
    a.add_argument("username")
    a.add_argument("--admin", action="store_true", help="may manage users on /users")
    a.add_argument("--password-stdin", action="store_true")

    pw = sub.add_parser("passwd", help="set a user's password (logs them out)")
    pw.add_argument("username")
    pw.add_argument("--password-stdin", action="store_true")

    sub.add_parser("list", help="list users")

    for name, helptext in (("disable", "disable a user (logs them out)"),
                           ("enable", "re-enable a user"),
                           ("delete", "delete a user"),
                           ("logout", "end all of a user's browser sessions"),
                           ("token-list", "list a user's API tokens")):
        sp = sub.add_parser(name, help=helptext)
        sp.add_argument("username")

    ad = sub.add_parser("admin", help="grant/revoke admin")
    ad.add_argument("username")
    ad.add_argument("state", help="on|off")

    ta = sub.add_parser("token-add", help="issue an API token (printed once)")
    ta.add_argument("username")
    ta.add_argument("name", help="label, e.g. 'export watcher'")

    tr = sub.add_parser("token-revoke", help="revoke an API token")
    tr.add_argument("username")
    tr.add_argument("token_id")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    path = resolve_users_file(args.file)
    store = UserStore(path)
    try:
        if args.cmd == "add":
            user = store.add(args.username, _read_password(args), admin=args.admin)
            print(f"Created {'admin ' if user.admin else ''}user {user.username} in {path}")
        elif args.cmd == "passwd":
            if store.get(args.username) is None:
                raise UserStoreError(f"no such user: {args.username}")
            store.set_password(args.username, _read_password(args))
            print(f"Password updated for {args.username.lower()}")
        elif args.cmd == "list":
            users = store.list_users()
            if not users:
                print(f"No users in {path}")
            for u in users:
                flags = ",".join(
                    f for f, on in (("admin", u.admin), ("disabled", u.disabled)) if on
                )
                print(f"{u.username:<20} {flags or '-':<16} last login: {u.last_login or 'never'}"
                      f"  tokens: {len(u.tokens)}")
        elif args.cmd == "disable":
            store.set_disabled(args.username, True)
            print(f"Disabled {args.username.lower()}")
        elif args.cmd == "enable":
            store.set_disabled(args.username, False)
            print(f"Enabled {args.username.lower()}")
        elif args.cmd == "delete":
            store.delete(args.username)
            print(f"Deleted {args.username.lower()}")
        elif args.cmd == "logout":
            store.revoke_sessions(args.username)
            print(f"Ended all sessions for {args.username.lower()}")
        elif args.cmd == "admin":
            store.set_admin(args.username, _parse_onoff(args.state))
            print(f"{args.username.lower()}: admin={_parse_onoff(args.state)}")
        elif args.cmd == "token-add":
            token, meta = store.issue_token(args.username, args.name)
            print(f"Token {meta['id']} ({meta['name']}) for {args.username.lower()}:")
            print(token)
            print("Store it now — it cannot be shown again. "
                  "Send as 'Authorization: Bearer <token>'.")
        elif args.cmd == "token-list":
            user = store.get(args.username)
            if user is None:
                raise UserStoreError(f"no such user: {args.username}")
            if not user.tokens:
                print("No tokens")
            for t in user.tokens:
                print(f"{t.get('id')}  {t.get('name')!r:<30} created {t.get('created_at')}"
                      f"  last used {t.get('last_used') or 'never'}")
        elif args.cmd == "token-revoke":
            store.revoke_token(args.username, args.token_id)
            print(f"Revoked token {args.token_id}")
    except UserStoreError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    return 0
