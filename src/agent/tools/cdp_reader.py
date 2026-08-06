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


def _coerce_str_arg(value):
    """Small-model tool-calling isn't always well-typed -- confirmed live,
    a string argument arrived here as a dict instead (e.g. {"text": "..."}
    wrapping the intended value). Try the obvious keys before falling back
    to a plain str() of the whole thing."""
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        for key in ("text", "title", "name", "value", "hint"):
            if isinstance(value.get(key), str):
                return value[key]
    return str(value)


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
    hint = _coerce_str_arg(hint).lower()
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


def _navigate_and_wait(ws_url: str, url: str, timeout_s: float = 8.0, poll_interval: float = 0.5) -> None:
    """Hard-reloads the tab to `url` and waits for its rendered text to
    stop growing before returning. Confirmed live necessary: clicking a
    chat in Gemini's sidebar updates the URL/tab title via client-side
    routing immediately, but can leave the PREVIOUS conversation's content
    rendered on screen for several seconds (or longer -- observed once
    past 10s with no change) before the SPA actually swaps in the new
    one. A real Page.navigate forces an actual reload instead of trusting
    the client-side route transition; polling body.innerText's length
    until it stabilizes is the same "poll until it stops changing"
    approach page_reader.py's read_full_page already uses for an
    analogous SPA-timing problem (there: has scrolling revealed anything
    new; here: has navigation finished rendering)."""
    conn = _CDPConnection(ws_url)
    try:
        conn.call("Page.navigate", {"url": url})
    finally:
        conn.close()

    last_len = -1
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        time.sleep(poll_interval)
        text = _evaluate(ws_url, "document.body.innerText") or ""
        if len(text) > 0 and len(text) == last_len:
            return
        last_len = len(text)


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


def click_text(hint: str, text: str) -> dict:
    """Finds an element in the tab (matched by `hint`) whose visible text,
    aria-label, or title attribute contains `text` (case-insensitive) and
    clicks it via a real DOM click -- e.g. selecting a specific past
    conversation out of Gemini's chat history sidebar by its title, or an
    icon-only button that only has an aria-label (like a sidebar toggle).
    Explicit fix for a real, reported bug: asking ZELIA to open a specific
    existing Gemini chat opened a blank new one instead. Root cause found
    live: Gemini's chat-history sidebar starts collapsed, and
    `document.body.innerText` (what read_tab uses) correctly only returns
    *visible* text -- a collapsed sidebar's chat titles genuinely aren't
    there to read or click yet. This function's aria-label matching is
    what lets a caller open the sidebar itself first (e.g.
    click_text(hint, "Open sidebar")) before reading/clicking a specific
    chat inside it. No synthetic mouse input at all, same as read_tab.

    Picks the innermost matching element (walks past any large wrapper
    `<div>` that merely *contains* a match, down to the specific list item
    itself) so clicking "Portal 2" in a chat titled "My Portal 2
    playthrough" clicks that one specific list entry, not some ancestor
    covering half the page. Prefers an actually-clickable-looking tag/role
    if more than one innermost match remains tied."""
    # Confirmed live: the small model sometimes wraps this argument in an
    # object instead of passing a plain string (e.g. {"text": "..."}
    # instead of "..." directly), which crashed here (`.lower()` on a
    # dict) instead of failing gracefully with a message the model could
    # actually act on. Unwrap the obvious case before giving up and
    # stringifying the whole thing, which would never match real page text.
    # (find_tab does the same for `hint`.)
    text = _coerce_str_arg(text)

    if not is_available():
        return {"ok": False, "error": NOT_AVAILABLE_ERROR}

    tab = find_tab(hint)
    if not tab:
        open_titles = [t.get("title", "?") for t in list_tabs()]
        return {"ok": False, "error": f"No open Brave tab matching '{hint}'. Open tabs: {open_titles}"}

    needle = json.dumps(text.lower())
    expression = f"""
    (function() {{
        const target = {needle};
        const all = Array.from(document.querySelectorAll('*'));
        const getText = el => (el.textContent || '') + ' ' + (el.getAttribute('aria-label') || '') + ' ' + (el.getAttribute('title') || '');
        const matches = all.filter(el => getText(el).toLowerCase().includes(target));
        if (matches.length === 0) return {{found: false}};
        // Prefer a real interactive element (an <a href>, a <button>, a
        // role=button) wherever it appears among the matches, checked
        // BEFORE any "innermost" narrowing -- confirmed live against
        // Gemini's chat-history sidebar that the real clickable target is
        // an <a href="/app/..."> whose own child <span> duplicates its
        // aria-label text, which an innermost-first check would incorrectly
        // exclude (the <a> "has a matching child", so it looked like a
        // non-specific wrapper even though it's the actual link). Only fall
        // back to the innermost generic element (avoiding a giant wrapper
        // <div>) if nothing properly interactive matched at all.
        let clickable = matches.find(el => el.tagName === 'A' && el.hasAttribute('href'))
            || matches.find(el => ['A', 'BUTTON', 'LI'].includes(el.tagName) || el.getAttribute('role') === 'button');
        if (!clickable) {{
            const innermost = matches.filter(el => !Array.from(el.children).some(
                child => getText(child).toLowerCase().includes(target)
            ));
            clickable = innermost[0] || matches[0];
        }}
        clickable.scrollIntoView({{block: 'center'}});
        clickable.click();
        return {{found: true, tag: clickable.tagName, matchCount: matches.length}};
    }})()
    """
    ws_url = tab["webSocketDebuggerUrl"]
    before_url = _evaluate(ws_url, "location.href")
    result = _evaluate(ws_url, expression)
    if result is None:
        return {"ok": False, "error": f"Could not run the click on tab '{tab.get('title')}'."}
    if not result.get("found"):
        return {"ok": False, "error": f"No element containing '{text}' found on tab '{tab.get('title')}'."}

    # If the click changed the URL (e.g. selecting a different chat), force
    # a real reload and wait for it to actually finish rendering -- see
    # _navigate_and_wait's docstring for why the client-side route change
    # alone isn't trustworthy here.
    after_url = _evaluate(ws_url, "location.href")
    if after_url and after_url != before_url:
        _navigate_and_wait(ws_url, after_url)

    log.info(
        "Clicked element containing %r on tab '%s' (tag=%s, %d total matches).",
        text, tab.get("title"), result.get("tag"), result.get("matchCount"),
    )
    return {"ok": True, "tag": result.get("tag"), "match_count": result.get("matchCount")}


def list_tab_titles() -> dict:
    """Lighter-weight than read_tab -- just what's open, no content. Use
    this for 'what tabs do I have open' instead of reading every one."""
    if not is_available():
        return {"ok": False, "error": NOT_AVAILABLE_ERROR}
    tabs = list_tabs()
    return {"ok": True, "tabs": [{"title": t.get("title"), "url": t.get("url")} for t in tabs]}
