"""
Shared page shell for the dashboard templates.

Every template carries two markers, ``<!--pv:head-->`` and
``<!--pv:topbar-->``; :func:`render` fills them in when a page is served.
The head block (theme bootstrap, favicon, stylesheet) and the top bar
(brand, navigation, connection indicator, account menu, theme switch) used
to be pasted into each template, and the copies had drifted: admin links
were present on some pages and injected by script on others, so the
navigation jumped after load.

Rendering here also means the bar is correct in the first paint: the
server already knows which page is current and whether the user is an
admin.
"""

import re
from html import escape
from pathlib import Path
from typing import Optional

_STATIC_DIR = Path(__file__).parent / "static"
_STATIC_REF = re.compile(r'(?<=["\'])/static/([\w.-]+\.(?:css|js))(?=["\'])')

HEAD_MARKER = "<!--pv:head-->"
TOPBAR_MARKER = "<!--pv:topbar-->"

# (href, label) in display order. Admin-only pages are shown only to admins;
# the routes enforce that themselves — hiding the link is a courtesy.
NAV_ITEMS: tuple[tuple[str, str], ...] = (
    ("/", "Status"),
    ("/studies", "Studies"),
    ("/protocols", "Protocols"),
)
ADMIN_NAV_ITEMS: tuple[tuple[str, str], ...] = (
    ("/lookup", "Lookup"),
    ("/users", "Users"),
    ("/settings", "Settings"),
)
TRAILING_NAV_ITEMS: tuple[tuple[str, str], ...] = (
    ("/about", "About"),
)

# The mark: a grid of pixels fading out under a diagonal "veil" stroke.
_MARK = (
    '<svg viewBox="0 0 32 32" aria-hidden="true">'
    '<rect x="1.5" y="1.5" width="29" height="29" rx="6" fill="var(--accent-soft)" '
    'stroke="var(--accent)" stroke-width="1.5"/>'
    '<g fill="var(--accent)">'
    '<rect x="8" y="8" width="4" height="4"/>'
    '<rect x="14" y="8" width="4" height="4" opacity=".6"/>'
    '<rect x="8" y="14" width="4" height="4" opacity=".6"/>'
    '<rect x="20" y="8" width="4" height="4" opacity=".3"/>'
    '<rect x="14" y="14" width="4" height="4" opacity=".3"/>'
    '<rect x="8" y="20" width="4" height="4" opacity=".3"/>'
    '</g>'
    '<path d="M8 25.5C14 24 21 18 25 8" stroke="var(--accent)" stroke-width="1.6" '
    'fill="none" stroke-linecap="round"/></svg>'
)

_FAVICON = (
    "data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 32 32'%3E"
    "%3Crect x='1' y='1' width='30' height='30' rx='6' fill='%2317191b' stroke='%2338c6b4' "
    "stroke-width='2'/%3E%3Cg fill='%2338c6b4'%3E%3Crect x='8' y='8' width='4' height='4'/%3E"
    "%3Crect x='14' y='8' width='4' height='4' opacity='.6'/%3E"
    "%3Crect x='8' y='14' width='4' height='4' opacity='.6'/%3E"
    "%3Crect x='20' y='8' width='4' height='4' opacity='.3'/%3E"
    "%3Crect x='14' y='14' width='4' height='4' opacity='.3'/%3E"
    "%3Crect x='8' y='20' width='4' height='4' opacity='.3'/%3E%3C/g%3E%3C/svg%3E"
)

# The theme is applied before first paint so a light-theme user never sees
# a dark flash; localStorage can throw (private mode), hence the try.
HEAD = f"""<meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <script>
        let theme = 'dark';
        try {{ theme = localStorage.getItem('pixieveil_theme') || 'dark'; }}
        catch (_e) {{ /* storage blocked */ }}
        document.documentElement.dataset.theme = theme;
    </script>
    <link rel="icon" href="{_FAVICON}">
    <link rel="stylesheet" href="/static/theme.css">"""


def _link(href: str, label: str, current: str) -> str:
    aria = ' aria-current="page"' if href == current else ""
    badge = '<span class="count" id="nav-needs" hidden></span>' if href == "/studies" else ""
    return f'<a href="{href}"{aria}>{escape(label)}{badge}</a>'


def topbar(path: str, username: Optional[str] = None, admin: bool = False) -> str:
    """The top bar for *path*. Without a *username* (login, first-run
    wizard) it is just the brand and the theme switch."""
    parts = [
        '<header class="topbar">',
        f'<a class="brand" href="/">{_MARK}<span>PixieVeil</span></a>',
    ]
    if username is not None:
        items = NAV_ITEMS + (ADMIN_NAV_ITEMS if admin else ()) + TRAILING_NAV_ITEMS
        links = "".join(_link(href, label, path) for href, label in items)
        parts.append(f'<nav class="nav" aria-label="Main">{links}</nav>')
    parts.append('<div class="spacer"></div>')
    if username is not None:
        # Hidden until a page reports its connection state (PV.setConn).
        parts.append('<span class="conn" id="conn" hidden><span class="dot"></span>'
                     '<span class="label"></span></span>')
        parts.append(
            '<div class="account">'
            '<button type="button" class="btn btn-ghost account-btn" aria-haspopup="true" '
            f'aria-expanded="false"><span>{escape(username)}</span></button>'
            '<div class="account-menu" hidden>'
            '<button type="button" data-act="password">Change password</button>'
            '<button type="button" data-act="tokens">API tokens</button>'
            '<button type="button" data-act="logout">Log out</button>'
            '</div></div>'
        )
    parts.append('<button type="button" class="btn btn-ghost btn-icon" data-theme-toggle '
                 'aria-label="Switch theme"></button>')
    parts.append('</header>')
    return "\n        ".join(parts)


def _versioned(match: re.Match) -> str:
    """``/static/x.css`` -> ``/static/x.css?v=<mtime>``, so a browser never
    pairs a new page with the stylesheet or script it cached before an
    upgrade (the static route sends no cache lifetime, and browsers guess)."""
    name = match.group(1)
    try:
        stamp = int((_STATIC_DIR / name).stat().st_mtime)
    except OSError:
        return match.group(0)
    return f"/static/{name}?v={stamp}"


def render(html: str, path: str, username: Optional[str] = None, admin: bool = False) -> str:
    """Fill a template's shell markers and version its static references."""
    html = (html.replace(HEAD_MARKER, HEAD, 1)
                .replace(TOPBAR_MARKER, topbar(path, username, admin), 1))
    return _STATIC_REF.sub(_versioned, html)
