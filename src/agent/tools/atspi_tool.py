"""
Reads the live UI of the currently-focused app via AT-SPI (the Linux
accessibility framework) instead of screenshotting it and running OCR.

This is the preferred way to "see" a GUI app's current state: it queries
the actual live, currently-rendered widget tree directly (real text
content, not an image guess), the same underlying channel real assistive
technology (screen readers) uses -- not a backend data file, not bypassing
the app. Native Qt/GTK apps (Dolphin, Konsole, most system apps) expose
this by default. Many Electron/CEF-based apps (Steam among them) don't
expose an AT-SPI tree at all -- there's no way to make this work for those
short of the app itself enabling it, so screen_tool.py's screenshot+OCR
path is the fallback for anything atspi_tool can't see into.
"""
from src.utils.logger import get_logger

log = get_logger("atspi_tool")

MAX_DEPTH = 25
MAX_ITEMS = 200
MAX_CHARS = 4000  # keep the small brain's context from getting swamped by a deep tree


def _atspi():
    import gi
    gi.require_version("Atspi", "2.0")
    from gi.repository import Atspi
    return Atspi


def get_focused_window():
    """Returns (app_name, window_accessible) for whatever window is
    currently active, or (None, None) if AT-SPI isn't usable or nothing
    reports itself as active."""
    try:
        Atspi = _atspi()
        desktop = Atspi.get_desktop(0)
    except Exception as exc:  # noqa: BLE001
        log.warning("AT-SPI unavailable: %s", exc)
        return None, None

    for i in range(desktop.get_child_count()):
        app = desktop.get_child_at_index(i)
        if app is None:
            continue
        try:
            app_name = app.get_name()
            child_count = app.get_child_count()
        except Exception:  # noqa: BLE001
            continue
        for j in range(child_count):
            window = app.get_child_at_index(j)
            if window is None:
                continue
            try:
                if window.get_state_set().contains(Atspi.StateType.ACTIVE):
                    return app_name, window
            except Exception:  # noqa: BLE001
                continue
    return None, None


def _collect_text(accessible, depth: int = 0, items: list | None = None) -> list:
    if items is None:
        items = []
    if depth > MAX_DEPTH or len(items) > MAX_ITEMS:
        return items

    got_text = False
    try:
        if accessible.is_text():
            iface = accessible.get_text_iface()
            count = iface.get_character_count()
            if count > 0:
                content = iface.get_text(0, count).strip()
                if content:
                    items.append(content)
                    got_text = True
    except Exception:  # noqa: BLE001
        pass

    if not got_text:
        try:
            name = accessible.get_name()
            if name and name.strip():
                items.append(name.strip())
        except Exception:  # noqa: BLE001
            pass

    try:
        for i in range(accessible.get_child_count()):
            child = accessible.get_child_at_index(i)
            if child is not None:
                _collect_text(child, depth + 1, items)
            if len(items) > MAX_ITEMS:
                break
    except Exception:  # noqa: BLE001
        pass
    return items


def read_focused_app() -> dict:
    """Returns the visible text content of whatever app currently has
    focus, via its live accessibility tree. {"ok": False} if AT-SPI can't
    see into it (app doesn't expose accessibility, e.g. most Electron/CEF
    apps) -- caller should fall back to screen_tool's screenshot+OCR path."""
    app_name, window = get_focused_window()
    if window is None:
        return {"ok": False, "error": "No AT-SPI-accessible window is currently focused."}

    texts = _collect_text(window)
    if not texts:
        return {"ok": False, "error": f"'{app_name}' didn't expose any readable content via AT-SPI."}

    # de-dupe while preserving order -- deeply nested trees repeat labels often
    seen = set()
    deduped = []
    for t in texts:
        if t not in seen:
            seen.add(t)
            deduped.append(t)

    joined = "\n".join(deduped)
    truncated = len(joined) > MAX_CHARS
    if truncated:
        joined = joined[:MAX_CHARS]
    return {
        "ok": True,
        "app": app_name,
        "text": joined,
        "note": "truncated -- this app has a lot of content, ask about a specific part if you need more" if truncated else None,
    }
