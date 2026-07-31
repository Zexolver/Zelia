"""
Reads every open tab in whatever browser window currently has focus, by
actually cycling through them (Ctrl+Tab, the standard Chromium/Firefox
shortcut) and reading each one -- same principle as everything else in
this project's screen-reading story: act like a human clicking through
tabs, not reach into the browser's internals. Necessary here specifically
because Brave (Chromium/CEF-based) doesn't expose an AT-SPI accessibility
tree at all (confirmed empty in atspi_tool.py's testing), so there's no
"ask the app directly" shortcut the way there is for native Qt/GTK apps --
screenshot+OCR of each tab in turn is the only way to actually see them.

Assumes the browser is already the focused window (call show_me/
open_browser first if it isn't) -- doesn't try to explicitly focus it
itself, since xdotool-based window queries/activation aren't reliable for
native-Wayland clients like Brave (confirmed: it runs with
--ozone-platform=wayland, not XWayland, so it's invisible to X11-rooted
tools the way a Tk or XWayland-bridged window isn't).
"""
import time

from src.agent.tools import desktop_control, screen_tool
from src.utils.logger import get_logger

log = get_logger("browser_tabs")

MAX_TABS = 15
SETTLE_SECONDS = 0.5  # let the new tab actually render before reading it
SNIPPET_LEN = 200  # how much of each tab's text to compare for "have we cycled back around"
MAX_CHARS_PER_TAB = 1500  # keep 15 tabs' worth of OCR from swamping the small brain's context


def read_all_tabs(max_tabs: int = MAX_TABS) -> dict:
    """Cycles forward through tabs with Ctrl+Tab, reading each one, until
    it sees a tab whose content matches one already read (cycled back to
    the start) or hits max_tabs. Leaves the browser on whatever tab it
    ends up on -- Ctrl+Tab a few more times or Ctrl+1 for the first tab
    if you need to land somewhere specific afterward."""
    tabs = []
    seen_snippets = set()

    for i in range(max_tabs):
        result = screen_tool.read_screen_text()
        text = result.get("text", "") if result.get("ok") else ""
        snippet = text[:SNIPPET_LEN].strip()

        if snippet and snippet in seen_snippets:
            log.info("Cycled back to an already-read tab after %d tab(s) -- stopping.", i)
            break
        if snippet:
            seen_snippets.add(snippet)

        truncated = len(text) > MAX_CHARS_PER_TAB
        tabs.append({
            "index": i,
            "text": text[:MAX_CHARS_PER_TAB] + ("..." if truncated else ""),
        })

        key_result = desktop_control.press_key("ctrl+tab")
        if not key_result.get("ok"):
            log.warning("Could not send ctrl+tab (%s) -- stopping after %d tab(s).", key_result.get("error"), i + 1)
            break
        time.sleep(SETTLE_SECONDS)

    if not tabs:
        return {"ok": False, "error": "Couldn't read any tab content -- is a browser window actually focused?"}

    log.info("Read %d browser tab(s).", len(tabs))
    return {"ok": True, "tabs": tabs, "count": len(tabs)}
