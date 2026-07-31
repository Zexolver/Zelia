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

For interacting with a page after it's open (typing into a search/chat box,
clicking something only visible on screen), use desktop_control's
type_text/press_key/click_at/find_text_on_screen.
"""
import shutil
import subprocess

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


def open_browser(url: str, browser: str | None = None, default_browser: str = "floorp") -> dict:
    global _session_browser_override
    target = browser or _session_browser_override or default_browser
    resolved = _resolve_launch(target)
    if not resolved:
        return {"ok": False, "error": f"Couldn't find an installed browser matching '{target}'."}
    mode, ident = resolved

    if not (url.startswith("http://") or url.startswith("https://")):
        url = f"https://{url}"

    if mode == "binary":
        subprocess.Popen([ident, url])
    else:
        subprocess.Popen(["gtk-launch", ident, url])
    log.info("Opened %s in %s (%s)", url, ident, mode)
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
