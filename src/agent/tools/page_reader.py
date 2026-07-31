"""
Reads a long page or conversation (a Gemini/Claude chat, a long article)
by scrolling through it and reading each screen's worth of text --
scrolling WITHIN one tab, as opposed to browser_tabs.py's Ctrl+Tab
cycling BETWEEN tabs. Necessary because read_screen_text only ever sees
whatever's currently visible on screen -- a real conversation or article
routinely extends far beyond one screen's height, and the model was
observed only ever reading the first visible screen and answering as if
that were the whole thing (see CLAUDE.md known issues).
"""
import time

from src.agent.tools import desktop_control, screen_tool
from src.utils.logger import get_logger

log = get_logger("page_reader")

MAX_SCROLLS = 30
SETTLE_SECONDS = 0.4
SNIPPET_LEN = 200  # for cycle detection, same principle as browser_tabs.py
MAX_CHARS_PER_SCREEN = 2000
MAX_TOTAL_CHARS = 20000  # cap combined text so the small brain's context doesn't get swamped


def read_full_page(browser: str = "", max_scrolls: int = MAX_SCROLLS) -> dict:
    """Scrolls down (Page Down) through the current page/tab, reading each
    screen's worth of text, until scrolling stops changing what's visible
    (reached the bottom) or hits max_scrolls. Returns the combined text of
    everything read. Restores whatever window the user was on before this
    ran, same as read_all_browser_tabs -- see desktop_control's
    preserve_focus_if_user_active docstring."""

    def _refocus():
        if not browser:
            return
        result = desktop_control.focus_window(browser)
        if not result.get("ok"):
            log.warning("Could not focus '%s' (%s) -- proceeding anyway.", browser, result.get("error"))
        else:
            time.sleep(SETTLE_SECONDS)

    def _read_action():
        _refocus()
        chunks = []
        seen_snippets = set()
        last_text = None

        for i in range(max_scrolls):
            result = screen_tool.read_screen_text()
            text = result.get("text", "") if result.get("ok") else ""
            snippet = text[:SNIPPET_LEN].strip()

            if text and text == last_text:
                log.info("Scroll position stopped changing content after %d screen(s) -- reached the bottom.", i)
                break
            if snippet and snippet in seen_snippets:
                log.info("Cycled back to already-read content after %d screen(s) -- stopping.", i)
                break
            if snippet:
                seen_snippets.add(snippet)
            last_text = text

            truncated = len(text) > MAX_CHARS_PER_SCREEN
            chunks.append(text[:MAX_CHARS_PER_SCREEN] + ("..." if truncated else ""))

            _refocus()  # re-assert focus every scroll -- see browser_tabs.py, same reasoning
            key_result = desktop_control.press_key("pagedown")
            if not key_result.get("ok"):
                log.warning("Could not scroll (%s) -- stopping after %d screen(s).", key_result.get("error"), i + 1)
                break
            time.sleep(SETTLE_SECONDS)

        if not chunks:
            return {"ok": False, "error": "Couldn't read any content -- is the right window actually focused?"}

        combined = "\n\n".join(chunks)
        truncated_overall = len(combined) > MAX_TOTAL_CHARS
        if truncated_overall:
            combined = combined[:MAX_TOTAL_CHARS] + "\n\n...(truncated, the page was longer than this)"

        log.info("Read %d screen(s) of scrolled content (%d chars total).", len(chunks), len(combined))
        return {"ok": True, "text": combined, "screens_read": len(chunks), "truncated": truncated_overall}

    return desktop_control.preserve_focus_if_user_active(_read_action)
