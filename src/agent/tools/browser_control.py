"""
Opens a real, visible browser window to a URL -- Floorp by default, anything
else on request. This is intentionally simple (launch `<browser> <url>`)
rather than driving a headless/automated browser: it works with literally
any installed browser regardless of what it's based on (Floorp, Firefox,
Chromium, Brave, ...), and it's genuinely visible on screen like everything
else in ZELIA's toolkit.

For actually reading a page's content back (e.g. to answer a question about
documentation), pair this with fetch_url in browser_tool.py -- that's a
plain HTTP request, not a hidden GUI action, so it doesn't conflict with
"nothing invisible" the way a background terminal command would.

For reading/scrolling a specific already-open Brave tab's actual content
(a long chat conversation, an article), see cdp_reader.py -- it talks to
Brave's remote-debugging protocol directly, no synthetic input at all.
That needs Brave to have been launched with --remote-debugging-port, which
open_browser() below always includes for Brave specifically now (harmless
if Brave was already running from before this -- Chromium apps are
single-instance, so a flag on a later launch only takes effect if Brave
wasn't already running; see cdp_reader.py for what happens if it wasn't).

For interacting with a page after it's open by typing/clicking (search
boxes, buttons), use desktop_control's type_text/press_key/click_at/
find_text_on_screen.
"""
import shutil
import subprocess
from urllib.parse import urlparse

import yaml

from src.agent.tools.app_launcher import _list_desktop_entries
from src.utils.logger import get_logger

log = get_logger("browser_control")

KNOWN_BINARIES = {
    "floorp": ["floorp"],
    "firefox": ["firefox"],
    "chromium": ["chromium", "chromium-browser"],
    "chrome": ["google-chrome-stable", "google-chrome"],
    "brave": ["brave", "brave-browser"],
}

# See cdp_reader.py -- must match its CDP_PORT constant.
BRAVE_CDP_PORT = 9222
BRAVE_FLATPAK_ID = "com.brave.Browser"

# In-memory override for "use X browser for this" / "for now" -- doesn't
# touch config.yaml, just this running session. Reset on restart.
_session_browser_override: str | None = None


def _resolve_launch(name: str) -> tuple[str, str] | None:
    """Returns (mode, target). mode='binary' means target is a direct PATH
    executable, launch as [target, url]. mode='desktop' means target is a
    .desktop id, launch via `gtk-launch target url` instead -- needed for
    Flatpak-installed browsers (e.g. Brave, confirmed to have no direct
    PATH executable at all on this machine, only a .desktop entry whose
    Exec line runs `flatpak run ... @@u %U @@`). gtk-launch reads the
    desktop file itself and handles that placeholder/URL-passing syntax
    correctly instead of us hand-parsing Exec lines."""
    name = name.lower().strip()
    for candidate in KNOWN_BINARIES.get(name, [name]):
        if shutil.which(candidate):
            return "binary", candidate
    entries = _list_desktop_entries()
    for display_name, desktop_id in entries.items():
        if name in display_name.lower():
            return "desktop", desktop_id
    return None


CHROMIUM_FAMILY = {"chromium", "chrome", "brave", BRAVE_FLATPAK_ID}
FIREFOX_FAMILY = {"floorp", "firefox"}


# Reserved documentation/example domains (RFC 2606) -- extremely common in
# training data as filler/placeholder URLs, and confirmed live as one of
# the actual shapes the small model fabricates when it doesn't know a real
# URL (e.g. "http://chat.example.com/HIPv6" for a request it had no real
# address for).
_PLACEHOLDER_HOSTS = {"example.com", "example.org", "example.net", "example.edu"}
_ALLOWED_NO_DOT_HOSTS = {"localhost"}


def _looks_like_real_url(url: str) -> bool:
    """Coarse sanity check, not real URL validation -- only catches the
    specific fabrication shapes confirmed live, repeatedly, this session:
    a made-up custom scheme (e.g. 'gemini://your-chat-url-here' -- the
    model confusing Google's Gemini product with the real, unrelated
    gemini:// network protocol), a host with no TLD at all (e.g.
    'https://dolphin:' when asked to open a native app that isn't a
    website), and RFC 2606 placeholder domains. Does NOT guarantee a URL
    is real/reachable -- a well-formed but still-wrong URL will pass this
    and just 404 normally, which is a far less confusing failure than
    silently "opening" a nonsense scheme and then confidently claiming
    success."""
    try:
        parsed = urlparse(url)
    except ValueError:
        return False
    if parsed.scheme and parsed.scheme not in ("http", "https"):
        return False
    hostname = (parsed.hostname or "").lower()
    if not hostname and not parsed.scheme:
        # No scheme at all (e.g. a bare "gemini.google.com") -- urlparse
        # can't extract a hostname without one; fall back to the raw
        # string's leading host-shaped segment instead.
        hostname = url.split("/")[0].lower()
    if not hostname:
        return False
    if any(hostname == h or hostname.endswith("." + h) for h in _PLACEHOLDER_HOSTS):
        return False  # catches subdomains too, e.g. "chat.example.com"
    if hostname in _ALLOWED_NO_DOT_HOSTS or hostname.replace(".", "").isdigit():
        return True
    return "." in hostname


def open_browser(url: str, browser: str | None = None, default_browser: str = "floorp", new_window: bool = False) -> dict:
    """new_window=True forces a genuinely new window rather than a new tab
    in whatever's already open. Matters specifically for Chromium-based
    browsers (Brave, Chromium, Chrome): they're single-instance, so a
    plain relaunch while one's already running just opens a new tab in
    the existing window, not a new window -- confirmed live as a real
    bug, the small brain tried to work around that limitation itself by
    pressing ctrl+tab (which cycles tabs, not opens windows) instead of
    just asking for a new window properly. --new-window/-new-window are
    real flags both browser families support for exactly this."""
    if not _looks_like_real_url(url):
        return {
            "ok": False,
            "error": (
                f"'{url}' doesn't look like a real web address -- if you meant to open an "
                "installed application (not a website), use show_me instead. If you don't "
                "actually know the real URL for something specific, don't guess one; open "
                "the site's real root URL and navigate/read from there instead."
            ),
        }
    global _session_browser_override
    target = browser or _session_browser_override or default_browser
    resolved = _resolve_launch(target)
    if not resolved:
        return {"ok": False, "error": f"Couldn't find an installed browser matching '{target}'."}
    mode, ident = resolved

    if not (url.startswith("http://") or url.startswith("https://")):
        url = f"https://{url}"

    extra_flags = []
    if new_window:
        if ident in CHROMIUM_FAMILY:
            extra_flags = ["--new-window"]
        elif ident in FIREFOX_FAMILY:
            extra_flags = ["-new-window"]

    if mode == "desktop" and ident == BRAVE_FLATPAK_ID:
        # `flatpak run` (not gtk-launch) so the debug-port flag actually
        # reaches Brave's own argv -- gtk-launch just substitutes the URL
        # into the .desktop file's fixed Exec line, no room for extra
        # flags. Only takes effect if Brave wasn't already running
        # (Chromium apps are single-instance; a later launch just opens a
        # tab in whatever session is already up) -- see cdp_reader.py for
        # how that's detected and surfaced.
        subprocess.Popen([
            "flatpak", "run", BRAVE_FLATPAK_ID,
            f"--remote-debugging-port={BRAVE_CDP_PORT}",
            # Chromium rejects CDP WebSocket connections whose Origin header
            # doesn't match an explicit allowlist by default (an anti-DNS-
            # rebinding protection) -- confirmed live: without this,
            # cdp_reader.py's connection got a 403 even with the port open
            # and reachable over plain HTTP.
            f"--remote-allow-origins=http://127.0.0.1:{BRAVE_CDP_PORT}",
            *extra_flags,
            url,
        ])
    elif mode == "binary":
        subprocess.Popen([ident, *extra_flags, url])
    else:
        # gtk-launch doesn't forward arbitrary flags through to the
        # underlying app (only URIs, via the .desktop file's own Exec
        # line) -- new_window isn't achievable through this path.
        subprocess.Popen(["gtk-launch", ident, url])
    log.info("Opened %s in %s (%s, new_window=%s)", url, ident, mode, new_window)
    return {"ok": True, "browser": ident, "url": url}


def set_browser_for_now(browser: str) -> dict:
    """'use X browser for this' -- session-only, doesn't touch config.yaml."""
    global _session_browser_override
    if not _resolve_launch(browser):
        return {"ok": False, "error": f"Couldn't find an installed browser matching '{browser}'."}
    _session_browser_override = browser
    return {"ok": True, "browser": browser, "scope": "this session only"}


def set_default_browser(browser: str, config_path: str) -> dict:
    """'always use X from now on' -- persists to config.yaml."""
    if not _resolve_launch(browser):
        return {"ok": False, "error": f"Couldn't find an installed browser matching '{browser}'."}

    with open(config_path) as f:
        cfg = yaml.safe_load(f)
    cfg.setdefault("desktop", {})["default_browser"] = browser
    with open(config_path, "w") as f:
        yaml.safe_dump(cfg, f, sort_keys=False)

    global _session_browser_override
    _session_browser_override = None  # clear any temporary override, the new default wins
    return {"ok": True, "browser": browser, "scope": "persisted as the default"}
