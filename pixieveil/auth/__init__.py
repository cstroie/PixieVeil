"""Dashboard authentication: user store, password hashing, signed sessions.

Users live in a YAML file (``http_server.users_file``, default
``config/users.yaml``) managed with ``python -m pixieveil.auth`` or, for
admins, the dashboard's ``/users`` page. There is no self-registration.
"""

from pixieveil.auth.user_store import User, UserStore, UserStoreError

__all__ = ["User", "UserStore", "UserStoreError"]
