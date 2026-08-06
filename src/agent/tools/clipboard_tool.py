"""
Read/write the desktop clipboard -- "what's on my clipboard", "copy this
for me", "put X on my clipboard". Uses wl-clipboard (wl-copy/wl-paste) on
Wayland and xclip on Xorg -- both confirmed installed on the reference
machine. Same session-type detection pattern used throughout
desktop_control.py/screen_tool.py/window_highlight.py (each of those
defines its own tiny _session_type() rather than sharing one -- matching
that existing convention here instead of introducing a new cross-module
import for a one-line helper).
"""
import subprocess

from src.utils.logger import get_logger

log = get_logger("clipboard_tool")


def _session_type() -> str:
    import os
    return os.environ.get("XDG_SESSION_TYPE", "x11").lower()


def read_clipboard() -> dict:
    session = _session_type()
    try:
        if session == "wayland":
            result = subprocess.run(["wl-paste", "--no-newline"], capture_output=True, text=True, timeout=5)
        else:
            result = subprocess.run(["xclip", "-selection", "clipboard", "-o"], capture_output=True, text=True, timeout=5)
        if result.returncode != 0:
            return {"ok": False, "error": result.stderr.strip() or "Clipboard is empty or unavailable."}
        return {"ok": True, "text": result.stdout}
    except FileNotFoundError:
        return {"ok": False, "error": "No clipboard tool available (wl-clipboard/xclip not installed)."}
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": "Reading the clipboard timed out."}


def write_clipboard(text: str) -> dict:
    session = _session_type()
    try:
        if session == "wayland":
            subprocess.run(["wl-copy"], input=text, text=True, timeout=5, check=True)
        else:
            subprocess.run(["xclip", "-selection", "clipboard"], input=text, text=True, timeout=5, check=True)
        log.info("Wrote %d chars to clipboard.", len(text))
        return {"ok": True}
    except FileNotFoundError:
        return {"ok": False, "error": "No clipboard tool available (wl-clipboard/xclip not installed)."}
    except (subprocess.TimeoutExpired, subprocess.CalledProcessError) as exc:
        return {"ok": False, "error": f"Could not write to clipboard: {exc}"}
