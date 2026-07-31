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

Takes an optional `browser` hint (e.g. "brave", "floorp") and (re-)focuses
that window itself via desktop_control.focus_window() before *every*
Ctrl+Tab, not just once up front -- confirmed live both matter separately
on a desktop this busy (~20 other windows, many terminals): without any
focus call, a just-launched browser window sometimes never got focus at
all and reading picked up terminal scrollback from the start; with only a
one-time focus call before the loop, a later Ctrl+Tab press occasionally
landed on a different window that had stolen focus mid-cycle (e.g. a
Konsole window, which also treats Ctrl+Tab as "next tab" -- so the drift
wasn't even obviously wrong from the keypress's point of view, reading
just silently continued on the wrong window's own tabs). Re-focusing each
iteration costs one extra D-Bus round trip per tab but is what actually
kept it locked onto the right window for the whole cycle in testing.
desktop_control.focus_window() itself uses KWin's window matcher on KDE
(see its docstring) -- xdotool-based activation still isn't reliable for
native-Wayland clients like Brave (confirmed: it runs with
--ozone-platform=wayland, not XWayland, so it's invisible to X11-rooted
tools the way a Tk or XWayland-bridged window isn't), which is exactly the
gap the KWin path closes.

The whole cycle runs wrapped in desktop_control.preserve_focus_if_user_active()
so the user's own window comes back automatically once reading finishes,
instead of leaving them stranded on whatever tab ZELIA ended up on -- this
matters a lot for "read my tabs while I keep working" style requests. This
does NOT make the read invisible: Wayland's input security model requires
a window to actually be focused before it can receive synthetic
keystrokes at all (confirmed: there's no way to send Ctrl+Tab to an
unfocused window), and KWin's screenshot API refuses unauthorized clients
outright (confirmed live: org.kde.KWin.ScreenShot2.CaptureWindow raised
"NoAuthorized" for this project's own process) -- so there's a real,
deliberate platform security boundary in the way of true invisible
background reading, not just a missing feature. The visible flicker while
reading is unavoidable; what preserve_focus_if_user_active() buys is that
it's brief and self-correcting rather than a lasting interruption.
"""
import time

from src.agent.tools import desktop_control, screen_tool
from src.utils.logger import get_logger

log = get_logger("browser_tabs")

MAX_TABS = 15
SETTLE_SECONDS = 0.5  # let the new tab actually render before reading it
SNIPPET_LEN = 200  # how much of each tab's text to compare for "have we cycled back around"
MAX_CHARS_PER_TAB = 1500  # keep 15 tabs' worth of OCR from swamping the small brain's context


def read_all_tabs(max_tabs: int = MAX_TABS, browser: str = "") -> dict:
    """Cycles forward through tabs with Ctrl+Tab, reading each one, until
    it sees a tab whose content matches one already read (cycled back to
    the start) or hits max_tabs. Restores whatever window the user was on
    beforehand once done (see module docstring) -- Ctrl+Tab a few more
    times or Ctrl+1 for the first tab if you need the browser itself to
    land somewhere specific afterward."""
    return desktop_control.preserve_focus_if_user_active(lambda: _read_all_tabs(max_tabs, browser))


def _read_all_tabs(max_tabs: int, browser: str) -> dict:
    def _refocus():
        if not browser:
            return
        result = desktop_control.focus_window(browser)
        if not result.get("ok"):
            log.warning("Could not focus '%s' (%s) -- proceeding anyway.", browser, result.get("error"))
        else:
            time.sleep(SETTLE_SECONDS)

    _refocus()

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

        _refocus()  # re-assert focus every cycle -- see module docstring, this isn't optional
        key_result = desktop_control.press_key("ctrl+tab")
        if not key_result.get("ok"):
            log.warning("Could not send ctrl+tab (%s) -- stopping after %d tab(s).", key_result.get("error"), i + 1)
            break
        time.sleep(SETTLE_SECONDS)

    if not tabs:
        return {"ok": False, "error": "Couldn't read any tab content -- is a browser window actually focused?"}

    log.info("Read %d browser tab(s).", len(tabs))
    return {"ok": True, "tabs": tabs, "count": len(tabs)}
