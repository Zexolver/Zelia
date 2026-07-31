"""
Reads (and scrolls) a Brave tab's actual page content via Chrome DevTools
Protocol (CDP) -- talks to Brave's own remote-debugging port directly,
not screenshots+OCR+synthetic scrolling like page_reader.py/browser_tabs.py.

Why this exists alongside those: explicit user requirement that ZELIA's
actions shouldn't interfere with the user's real mouse/keyboard while
they're actively using the computer (gaming, Blender, etc). CDP sidesteps
that entirely for browser content specifically -- no synthetic input at
all, just asking the browser itself for the page's real text and telling
it to scroll via JavaScript (`window.scrollBy`), which is a completely
different mechanism from injecting a Page-Down keystroke. It's also
strictly better for this even setting the input-isolation question aside:
one call gets the exact DOM text instead of a lossy OCR guess.

Requires Brave to have been launched with --remote-debugging-port (see
browser_control.py's open_browser -- does this automatically now for
Brave specifically). Chromium apps are single-instance, so this only
takes effect on a genuinely fresh launch; if Brave was already running
from before this, the debug port won't be up and functions here return a
clear error saying so rather than silently falling back to something else
-- the caller (agent_loop.py) can decide whether to ask the user to
restart Brave, or fall back to page_reader.py's OCR-based approach for
this one request.
"""
import json
import time

import requests
import websocket

from src.utils.logger import get_logger

log = get_logger("cdp_reader")

# Must match browser_control.py's BRAVE_CDP_PORT.
CDP_PORT = 9222
CDP_HTTP_BASE = f"http://127.0.0.1:{CDP_PORT}"
NOT_AVAILABLE_ERROR = (
    "Brave's remote-debugging port isn't reachable -- either Brave isn't running, "
    "or it was already running before this feature was added/before its last "
    "restart (Chromium apps are single-instance, so the debug flag only takes "
    "effect on a genuinely fresh launch). Ask the user to fully quit Brave "
    "(not just close the window) and let ZELIA reopen it, or use "
    "read_full_page/read_all_browser_tabs as a fallback for this request."
)


def _cdp_request(path: str) -> dict | list | None:
    try:
        response = requests.get(f"{CDP_HTTP_BASE}{path}", timeout=3)
        response.raise_for_status()
        return response.json()
    except (requests.RequestException, ValueError):
        return None


def is_available() -> bool:
    return _cdp_request("/json/version") is not None


def list_tabs() -> list[dict]:
    """Real pages only -- CDP also lists devtools-internal targets
    ("background_page", "service_worker", etc.) that aren't actual tabs."""
    tabs = _cdp_request("/json/list") or []
    return [t for t in tabs if t.get("type") == "page"]


def find_tab(hint: str) -> dict | None:
    """Matches `hint` against each tab's title or URL, case-insensitive."""
    hint = hint.lower()
    for tab in list_tabs():
        if hint in tab.get("title", "").lower() or hint in tab.get("url", "").lower():
            return tab
    return None


class _CDPConnection:
    """One CDP JSON-RPC-over-WebSocket connection to a single tab. Short-lived
    on purpose -- opened per call, not held open, since these reads are
    infrequent and a stale/dead connection left open would be one more
    thing to detect and recover from for little benefit."""

    def __init__(self, ws_url: str):
        self._ws = websocket.create_connection(ws_url, timeout=10)
        self._next_id = 1

    def call(self, method: str, params: dict | None = None) -> dict:
        msg_id = self._next_id
        self._next_id += 1
        self._ws.send(json.dumps({"id": msg_id, "method": method, "params": params or {}}))
        # CDP can interleave unrelated event notifications on the same
        # socket -- skip anything that isn't the reply to *this* call.
        while True:
            reply = json.loads(self._ws.recv())
            if reply.get("id") == msg_id:
                if "error" in reply:
                    raise RuntimeError(f"CDP method '{method}' failed: {reply['error']}")
                return reply.get("result", {})

    def close(self) -> None:
        try:
            self._ws.close()
        except OSError:
            pass


def _evaluate(ws_url: str, expression: str):
    conn = _CDPConnection(ws_url)
    try:
        result = conn.call("Runtime.evaluate", {"expression": expression, "returnByValue": True})
        return result.get("result", {}).get("value")
    finally:
        conn.close()


MAX_CHARS = 20000  # keep a very long page from swamping the small brain's context


def read_tab(hint: str) -> dict:
    """Reads a tab's full rendered text content by hint (title/URL
    substring, e.g. 'gemini', 'claude.ai') -- the ENTIRE page, not just
    what's currently scrolled into view, since this reads the DOM
    directly rather than a screenshot of the visible viewport."""
    if not is_available():
        return {"ok": False, "error": NOT_AVAILABLE_ERROR}

    tab = find_tab(hint)
    if not tab:
        open_titles = [t.get("title", "?") for t in list_tabs()]
        return {"ok": False, "error": f"No open Brave tab matching '{hint}'. Open tabs: {open_titles}"}

    text = _evaluate(tab["webSocketDebuggerUrl"], "document.body.innerText")
    if text is None:
        return {"ok": False, "error": f"Could not read page content from tab '{tab.get('title')}'."}

    truncated = len(text) > MAX_CHARS
    if truncated:
        text = text[:MAX_CHARS] + "\n\n...(truncated, the page was longer than this)"

    log.info("Read tab '%s' via CDP (%d chars%s).", tab.get("title"), len(text), ", truncated" if truncated else "")
    return {"ok": True, "text": text, "title": tab.get("title"), "url": tab.get("url"), "truncated": truncated}


def list_tab_titles() -> dict:
    """Lighter-weight than read_tab -- just what's open, no content. Use
    this for 'what tabs do I have open' instead of reading every one."""
    if not is_available():
        return {"ok": False, "error": NOT_AVAILABLE_ERROR}
    tabs = list_tabs()
    return {"ok": True, "tabs": [{"title": t.get("title"), "url": t.get("url")} for t in tabs]}
