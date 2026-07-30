"""
Draws a purple outline around whatever window ZELIA is currently using, so
it's visually obvious which window is hers versus the user's own -- pairs
with desktop_control.py's focus-steal guard, which gives input focus back
to the user's window but still leaves this visible so they can see what
she's doing.

Four thin, borderless, always-on-top strip windows (top/bottom/left/right)
positioned around the target window's geometry, not one overlay covering
it -- so the actual window content is never obscured, just framed. Xorg
only for now (needs xdotool's window geometry query; same "best-effort on
Wayland" situation as focus_window/preserve_focus_if_user_active
elsewhere in this project -- Wayland gives clients no way to position a
window at absolute screen coordinates outside wlroots-specific protocols).
"""
import os
import shutil
import subprocess
import threading

from src.utils.logger import get_logger

log = get_logger("window_highlight")

HIGHLIGHT_COLOR = "#a020f0"  # purple-ish
THICKNESS = 6  # px -- noticeable, but won't eat into a window's content

_stop_event = threading.Event()
_thread: threading.Thread | None = None
_thread_lock = threading.Lock()


def _session_type() -> str:
    return os.environ.get("XDG_SESSION_TYPE", "x11").lower()


def _get_geometry(window_id: str) -> dict | None:
    try:
        out = subprocess.check_output(["xdotool", "getwindowgeometry", "--shell", window_id], text=True)
        values = dict(line.split("=", 1) for line in out.strip().splitlines())
        return {k: int(v) for k, v in values.items() if k in ("X", "Y", "WIDTH", "HEIGHT")}
    except (subprocess.CalledProcessError, ValueError):
        return None


def clear_highlight() -> None:
    """Stops whatever highlight is currently showing. Safe to call even if
    none is active."""
    _stop_event.set()
    with _thread_lock:
        global _thread
        if _thread is not None:
            _thread.join(timeout=2)
            _thread = None


def _run(geo: dict) -> None:
    import tkinter as tk

    x, y, w, h = geo["X"], geo["Y"], geo["WIDTH"], geo["HEIGHT"]
    t = THICKNESS
    strips = [
        (x - t, y - t, w + 2 * t, t),   # top
        (x - t, y + h, w + 2 * t, t),   # bottom
        (x - t, y - t, t, h + 2 * t),   # left
        (x + w, y - t, t, h + 2 * t),   # right
    ]
    roots = []
    for sx, sy, sw, sh in strips:
        root = tk.Tk() if not roots else tk.Toplevel(roots[0])
        root.overrideredirect(True)
        root.geometry(f"{sw}x{sh}+{sx}+{sy}")
        root.configure(bg=HIGHLIGHT_COLOR)
        root.attributes("-topmost", True)
        roots.append(root)

    main = roots[0]

    def poll_stop():
        if _stop_event.is_set():
            for r in roots:
                try:
                    r.destroy()
                except Exception:  # noqa: BLE001
                    pass
            return
        main.after(150, poll_stop)

    main.after(150, poll_stop)
    main.mainloop()


def highlight_window(window_id: str | None = None) -> dict:
    """Draws a purple border around `window_id` (or the currently active
    window if not given). Replaces any previous highlight -- only one
    window is ever highlighted at a time."""
    clear_highlight()
    _stop_event.clear()

    if _session_type() == "wayland":
        return {
            "ok": False,
            "note": "Window highlighting needs absolute screen positioning, which "
                    "Wayland doesn't expose to clients outside wlroots-specific "
                    "protocols -- not available on this compositor.",
        }
    if shutil.which("xdotool") is None:
        return {"ok": False, "error": "xdotool not found."}

    if window_id is None:
        try:
            window_id = subprocess.check_output(["xdotool", "getactivewindow"], text=True).strip()
        except subprocess.CalledProcessError:
            return {"ok": False, "error": "Could not determine the active window."}

    geo = _get_geometry(window_id)
    if geo is None:
        return {"ok": False, "error": f"Could not get geometry for window {window_id}."}

    global _thread
    with _thread_lock:
        _thread = threading.Thread(target=_run, args=(geo,), daemon=True)
        _thread.start()
    return {"ok": True, "window_id": window_id}
