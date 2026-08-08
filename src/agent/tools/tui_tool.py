"""
Drives interactive terminal (TUI) tools -- htop, vim, a database/language
REPL, an installer's ncurses menu, anything that takes over the terminal
instead of just printing output and exiting -- which run_in_terminal/
run_shell_quiet can't do anything useful with beyond "launch it and hope"
(no way to send further keystrokes once it's running, no way to read its
current screen state back).

Built on tmux: a detached tmux session gives a real pty (so curses/
ncurses apps render correctly) plus a scriptable interface for exactly
the two things needed here -- send-keys (type into it) and capture-pane
(read its current screen). Same "use the standard, purpose-built system
tool instead of hand-rolling pty handling" principle the rest of this
project already follows (xdotool/ydotool for input, tesseract for OCR,
wpctl for volume, etc). Requires tmux to be installed -- NOT confirmed
installed on the reference machine as of this writing, add it to
system dependencies (PKGBUILD/install.sh) if it isn't already there.

The tmux session itself is independent of how (or whether) anyone is
currently viewing it -- start_tui's `location` just controls what, if
any, VIEWER gets attached alongside it:
  - "desktop" (default): opens a real, visible terminal window attached
    to the session, matching this project's "nothing hidden by default"
    philosophy (see CLAUDE.md's Philosophy section) -- the user can watch
    it happen just like run_in_terminal.
  - "vtty": attaches on the dedicated virtual terminal instead (see
    desktop_control.open_vtty_viewer) -- invisible on the real desktop,
    only visible if the user manually switches VTs (Ctrl+Alt+F9). This is
    the explicit "run it in the background" fallback -- doesn't compete
    for the user's screen/focus while they're gaming or otherwise busy,
    same motivation as run_in_vtty.
  - "background": no viewer attached at all -- ZELIA is the only thing
    "seeing" it, via read_tui_screen. Lightest-weight option when nobody
    needs to watch it live, just check on it periodically.
Regardless of `location`, send_keys/read_tui_screen/stop_tui always work
the same way, talking to the tmux session directly.
"""
import shutil
import subprocess
import uuid

from src.agent.tools import desktop_control
from src.utils.logger import get_logger

log = get_logger("tui_tool")

_SESSION_PREFIX = "zelia-tui-"
_NOT_INSTALLED_ERROR = "tmux isn't installed -- needed to drive interactive terminal tools."


def _has_tmux() -> bool:
    return shutil.which("tmux") is not None


def _session_name(session_id: str) -> str:
    return f"{_SESSION_PREFIX}{session_id}"


_VTTY_DISABLED_ERROR = (
    "location='vtty' is disabled -- a real incident (2026-08-07) confirmed the sudoers rule "
    "doesn't actually cover this invocation, and the unsafe retry/prompt fallback that "
    "exposed temporarily locked the user's own account via pam_faillock. Use "
    "location='background' instead (no viewer, but no risk), or 'desktop' if a visible "
    "window is acceptable. See desktop_control.open_vtty_viewer's docstring before re-enabling."
)


def start_tui(command: str, location: str = "desktop", cwd: str | None = None) -> dict:
    if not _has_tmux():
        return {"ok": False, "error": _NOT_INSTALLED_ERROR}
    if location == "vtty":
        return {"ok": False, "error": _VTTY_DISABLED_ERROR}
    if location not in ("desktop", "background"):
        return {"ok": False, "error": f"Unknown location '{location}' -- must be 'desktop' or 'background'."}

    session_id = str(uuid.uuid4())[:8]
    session = _session_name(session_id)
    cmd = ["tmux", "new-session", "-d", "-s", session]
    if cwd:
        cmd += ["-c", cwd]
    cmd += [command]
    try:
        subprocess.run(cmd, check=True, capture_output=True, text=True, timeout=10)
    except subprocess.CalledProcessError as exc:
        return {"ok": False, "error": f"Could not start '{command}' in tmux: {exc.stderr.strip()}"}
    except subprocess.TimeoutExpired as exc:
        return {"ok": False, "error": str(exc)}

    log.info("Started TUI session %s (location=%s): %s", session, location, command)

    if location == "desktop":
        viewer = desktop_control.open_terminal(command=f"tmux attach -t {session}", keep_open=False)
        if not viewer.get("ok"):
            return {"ok": True, "session_id": session_id, "location": location, "command": command,
                     "warning": f"Session started but couldn't open a visible window: {viewer.get('error')}"}

    return {"ok": True, "session_id": session_id, "location": location, "command": command}


def send_keys(session_id: str, keys: str, enter: bool = True) -> dict:
    # Every function here talks to tmux directly -- if it's not installed,
    # subprocess.run can't even exec it and raises FileNotFoundError, which
    # (confirmed live) was NOT being caught here, crashing this call with
    # a raw traceback-shaped error instead of a clean message. start_tui/
    # list_tui_sessions already guarded against this; the rest didn't.
    if not _has_tmux():
        return {"ok": False, "error": _NOT_INSTALLED_ERROR}
    session = _session_name(session_id)
    cmd = ["tmux", "send-keys", "-t", session, keys]
    if enter:
        cmd.append("Enter")
    try:
        subprocess.run(cmd, check=True, capture_output=True, text=True, timeout=5)
        return {"ok": True}
    except subprocess.CalledProcessError as exc:
        return {"ok": False, "error": f"No active TUI session '{session_id}' (or tmux error): {exc.stderr.strip()}"}
    except subprocess.TimeoutExpired as exc:
        return {"ok": False, "error": str(exc)}


def read_tui_screen(session_id: str) -> dict:
    if not _has_tmux():
        return {"ok": False, "error": _NOT_INSTALLED_ERROR}
    session = _session_name(session_id)
    try:
        result = subprocess.run(
            ["tmux", "capture-pane", "-t", session, "-p"],
            check=True, capture_output=True, text=True, timeout=5,
        )
        return {"ok": True, "text": result.stdout}
    except subprocess.CalledProcessError as exc:
        return {"ok": False, "error": f"No active TUI session '{session_id}' (or tmux error): {exc.stderr.strip()}"}
    except subprocess.TimeoutExpired as exc:
        return {"ok": False, "error": str(exc)}


def stop_tui(session_id: str) -> dict:
    if not _has_tmux():
        return {"ok": False, "error": _NOT_INSTALLED_ERROR}
    session = _session_name(session_id)
    try:
        subprocess.run(["tmux", "kill-session", "-t", session], check=True, capture_output=True, text=True, timeout=5)
        log.info("Stopped TUI session %s", session)
        return {"ok": True}
    except subprocess.CalledProcessError as exc:
        return {"ok": False, "error": f"No active TUI session '{session_id}' (or tmux error): {exc.stderr.strip()}"}
    except subprocess.TimeoutExpired as exc:
        return {"ok": False, "error": str(exc)}


def list_tui_sessions() -> dict:
    if not _has_tmux():
        return {"ok": False, "error": _NOT_INSTALLED_ERROR}
    try:
        result = subprocess.run(["tmux", "list-sessions"], capture_output=True, text=True, timeout=5)
        if result.returncode != 0:
            return {"ok": True, "session_ids": []}  # tmux exits non-zero when there are no sessions at all
        session_ids = [
            line.split(":")[0][len(_SESSION_PREFIX):]
            for line in result.stdout.splitlines()
            if line.startswith(_SESSION_PREFIX)
        ]
        return {"ok": True, "session_ids": session_ids}
    except subprocess.TimeoutExpired as exc:
        return {"ok": False, "error": str(exc)}
