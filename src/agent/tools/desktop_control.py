"""
Real desktop control -- opens a visible terminal for commands, types/clicks
into whatever window has focus, and works on both Xorg and Wayland.

type_text/press_key/click_at delegate to a swappable input backend (see
the "Keyboard / mouse input backend selection" section below) -- by
default that's org.freedesktop.portal.RemoteDesktop, a genuinely isolated
input path that doesn't touch the user's real mouse/keyboard
(input_backend_portal.py). input_backend_ydotool.py (real input
synthesis, shares the user's actual devices) is kept as an opt-in
fallback for compositors/portal backends where that isn't available yet.

Window focusing is best-effort: tried via wlrctl/swaymsg on wlroots-based
compositors (Sway, Hyprland), and via KWin's own D-Bus window matcher on
KDE (see _focus_window_kwin below) -- on other Wayland compositors there's
no standard way to do this from outside, so it's skipped there and we
just rely on newly-launched windows already having focus.
"""
import json
import os
import shutil
import subprocess
import tempfile
import time

import pytesseract
from PIL import Image

from src.agent.tools.screen_tool import take_screenshot
from src.utils.logger import get_logger

log = get_logger("desktop_control")

TERMINAL_CANDIDATES = ["konsole", "gnome-terminal", "xfce4-terminal", "alacritty", "kitty", "foot", "xterm"]

# command, run-flag -- how each terminal takes "run this command and stay open"
TERMINAL_RUN_FLAGS = {
    "konsole": ["--hold", "-e", "bash", "-c"],
    "gnome-terminal": ["--", "bash", "-c"],
    "xfce4-terminal": ["--hold", "-x", "bash", "-c"],
    "alacritty": ["--hold", "-e", "bash", "-c"],
    "kitty": ["--hold", "bash", "-c"],
    "foot": ["bash", "-c"],
    "xterm": ["-hold", "-e", "bash", "-c"],
}

# Minimal symbolic-name -> ydotool key sequence map for the combos this
# project actually needs (address bar focus, new tab, enter, tab/escape,
# scrolling a long page -- see page_reader.py). xdotool takes symbolic
# names directly and needs none of this.
YDOTOOL_KEYS = {
    "ctrl": "29", "alt": "56", "shift": "42", "super": "125",
    "enter": "28", "tab": "15", "escape": "1", "l": "38", "t": "20",
    "space": "57", "pagedown": "109", "pageup": "104",
    "down": "108", "up": "103", "end": "107", "home": "102",
}


def _session_type() -> str:
    return os.environ.get("XDG_SESSION_TYPE", "x11").lower()


def _has(binary: str) -> bool:
    return shutil.which(binary) is not None


# --------------------------------------------------------------------------
# Terminal
# --------------------------------------------------------------------------
def _find_terminal() -> str | None:
    preferred = os.environ.get("TERMINAL")
    if preferred and _has(preferred):
        return preferred
    for candidate in TERMINAL_CANDIDATES:
        if _has(candidate):
            return candidate
    return None


def open_terminal(command: str | None = None, keep_open: bool = True, cwd: str | None = None) -> dict:
    """Opens a new, visible terminal window. If `command` is given, runs it
    there (visibly) instead of a hidden background subprocess. Defaults to
    starting in `cwd` (the agent's workspace) so commands like "run
    hello.py" don't silently fail from wherever ZELIA's own process happens
    to be running -- pass an explicit `cd` in `command` to override."""
    terminal = _find_terminal()
    if not terminal:
        return {"ok": False, "error": "No terminal emulator found. Install one (konsole, alacritty, kitty, etc.)."}

    if not command:
        subprocess.Popen([terminal], cwd=cwd)
        return {"ok": True, "terminal": terminal, "action": "opened"}

    flags = TERMINAL_RUN_FLAGS.get(terminal, ["-e", "bash", "-c"])
    shell_cmd = command if not keep_open else f"{command}; echo; echo '[done -- press enter to close]'; read"
    subprocess.Popen([terminal, *flags, shell_cmd], cwd=cwd)
    log.info("Ran in visible terminal (%s, cwd=%s): %s", terminal, cwd, command)
    return {"ok": True, "terminal": terminal, "action": "ran", "command": command}


# --------------------------------------------------------------------------
# Keyboard / mouse input backend selection
#
# ZELIA has no separate synthetic input device of her own on this platform
# by default -- ydotool's virtual device shares the one real system cursor
# and keyboard focus with the user's actual physical devices (confirmed:
# no separate visible/independent pointer exists in the standard Wayland
# single-seat model). Explicit user requirement: her actions must not
# interfere with the user's own mouse/keyboard while they're using the
# computer (e.g. gaming, using Blender) at the same time.
#
# The fix is org.freedesktop.portal.RemoteDesktop -- a standard XDG
# Desktop Portal interface, backed by the compositor's transient-seat
# protocol (ext-transient-seat-v1) where implemented, that creates a
# genuinely isolated input seat instead of injecting into the real one.
# This is NOT KDE-specific: it's a standard portal interface, implemented
# by multiple desktop environments' own portal backends (confirmed via
# wayland.app's compositor compatibility matrix: KWin 6.6+, Mutter 49.2+,
# wlroots-based compositors via wlroots 0.18+, and others) -- whichever
# portal backend + compositor combination the user's desktop environment
# ships handles the actual seat isolation, this code just talks to the
# standard portal API. See input_backend_portal.py's module docstring for
# the full story (including the one-time consent dialog this requires).
#
# input_backend_ydotool.py is kept as an opt-in fallback (config.yaml:
# desktop.input_backend: "ydotool") for compositors/portal backends where
# the portal approach isn't available yet -- shares the real cursor and
# keyboard, same caveat as always with that approach.
# --------------------------------------------------------------------------
_input_backend_module = None


def _input_backend():
    global _input_backend_module
    if _input_backend_module is None:
        backend_name = "portal"
        try:
            from src.config import load_config
            backend_name = load_config().get("desktop", {}).get("input_backend", "portal")
        except Exception as exc:  # noqa: BLE001
            log.warning("Could not read desktop.input_backend from config (%s) -- defaulting to 'portal'.", exc)

        if backend_name == "ydotool":
            from src.agent.tools import input_backend_ydotool as backend
        else:
            from src.agent.tools import input_backend_portal as backend
        _input_backend_module = backend
    return _input_backend_module


def type_text(text: str) -> dict:
    return _input_backend().type_text(text)


def press_key(combo: str) -> dict:
    """combo like 'ctrl+l', 'ctrl+t', 'Return', 'Tab', 'Escape'."""
    return _input_backend().press_key(combo)


def click_at(x: int, y: int) -> dict:
    """Clicks at real screen pixel coordinates (x, y)."""
    return _input_backend().click_at(x, y)



def find_text_on_screen(text: str) -> dict:
    """OCRs the screen and returns click-able coordinates for the first
    match of `text`, so the agent can click things it can only see, not
    reach programmatically (e.g. a button inside a browser or Godot)."""
    path = take_screenshot()
    try:
        data = pytesseract.image_to_data(Image.open(path), output_type=pytesseract.Output.DICT)
        lowered = text.lower()
        for i, word in enumerate(data["text"]):
            if word.strip().lower() == lowered or lowered in word.strip().lower():
                x = data["left"][i] + data["width"][i] // 2
                y = data["top"][i] + data["height"][i] // 2
                return {"ok": True, "x": x, "y": y}
        return {"ok": False, "error": f"'{text}' not found on screen."}
    finally:
        os.remove(path)


# --------------------------------------------------------------------------
# Window focus (best-effort on Wayland)
# --------------------------------------------------------------------------
def _focus_window_kwin(name: str) -> dict | None:
    """KDE/KWin-specific focus, via the same D-Bus interface KRunner's
    built-in "Windows" plugin uses (org.kde.krunner1 on /WindowsRunner --
    Match to search by title/app, Run to activate). This is a real, working
    mechanism unlike the generic wlrctl/swaymsg paths below, which don't
    apply to KWin -- confirmed live: activated a specific Brave window by
    title out of ~20 other open windows (many Konsole/Claude Code sessions)
    on a KDE Plasma 6 Wayland session, verified via screenshot. Returns None
    (not a dict) if org.kde.KWin isn't on the session bus at all, so callers
    can fall through to other compositor paths; returns an {"ok": ...} dict
    once it's established KWin is present, since a no-match at that point is
    a real failure, not "wrong compositor"."""
    if not _has("busctl"):
        return None
    try:
        out = subprocess.check_output(
            ["busctl", "--user", "--json=short", "call", "org.kde.KWin", "/WindowsRunner",
             "org.kde.krunner1", "Match", "s", name],
            text=True, stderr=subprocess.DEVNULL, timeout=5,
        )
    except (subprocess.CalledProcessError, FileNotFoundError, subprocess.TimeoutExpired):
        return None  # org.kde.KWin likely isn't on the bus -- not KWin, or KWin too old

    try:
        matches = json.loads(out)["data"][0]
    except (json.JSONDecodeError, KeyError, IndexError):
        return {"ok": False, "error": "Unexpected response from KWin's window matcher."}
    if not matches:
        return {"ok": False, "error": f"No window matching '{name}' (via KWin)."}

    match_id = matches[0][0]  # matches are pre-sorted by relevance
    subprocess.run(
        ["busctl", "--user", "call", "org.kde.KWin", "/WindowsRunner",
         "org.kde.krunner1", "Run", "ss", match_id, ""],
        capture_output=True, timeout=5,
    )
    return {"ok": True, "note": "focused via KWin's window matcher", "matched_title": matches[0][1]}


def focus_window(name: str) -> dict:
    session = _session_type()
    if session != "wayland" and _has("wmctrl"):
        try:
            out = subprocess.check_output(["wmctrl", "-l"], text=True)
            for line in out.splitlines():
                if name.lower() in line.lower():
                    subprocess.run(["wmctrl", "-i", "-a", line.split()[0]])
                    return {"ok": True}
        except subprocess.CalledProcessError:
            pass
        return {"ok": False, "error": f"No window matching '{name}'."}

    kwin_result = _focus_window_kwin(name)
    if kwin_result is not None:
        return kwin_result

    if _has("swaymsg"):
        subprocess.run(["swaymsg", f'[title="{name}"] focus'], capture_output=True)
        return {"ok": True, "note": "best-effort on Sway"}
    if _has("wlrctl"):
        subprocess.run(["wlrctl", "window", "focus", name], capture_output=True)
        return {"ok": True, "note": "best-effort via wlrctl"}

    return {"ok": False, "error": "No window-focus mechanism available on this Wayland compositor -- new windows should already be focused on launch."}


def _kwin_active_window_title() -> str | None:
    """What window is focused *right now*, via the same KWin-scripting
    mechanism as _kwin_cursor_pos() -- used so preserve_focus_if_user_active()
    can remember the user's window before ZELIA focuses something else of
    her own, and hand that title back to focus_window() afterward to
    restore it. Returns "" (not None) if nothing is focused, None if the
    query itself failed (no busctl, KWin not running, etc)."""
    if not _has("busctl"):
        return None
    marker = os.urandom(4).hex()
    script = (
        f'var w = workspace.activeWindow;\n'
        f'print("KWIN_ACTIVE_{marker}:" + (w ? w.caption : ""));'
    )
    path = None
    try:
        with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False) as f:
            f.write(script)
            path = f.name
        out = subprocess.check_output(
            ["busctl", "--user", "call", "org.kde.KWin", "/Scripting",
             "org.kde.kwin.Scripting", "loadScript", "s", path],
            text=True, stderr=subprocess.DEVNULL, timeout=5,
        )
        script_id = out.split()[1]
        subprocess.run(
            ["busctl", "--user", "call", "org.kde.KWin", f"/Scripting/Script{script_id}",
             "org.kde.kwin.Script", "run"],
            capture_output=True, timeout=5,
        )
        subprocess.run(
            ["busctl", "--user", "call", "org.kde.KWin", "/Scripting",
             "org.kde.kwin.Scripting", "unloadScript", "s", path],
            capture_output=True, timeout=5,
        )
        journal = subprocess.check_output(
            ["journalctl", "--user", "_COMM=kwin_wayland", "--since=-3s", "--no-pager"],
            text=True, stderr=subprocess.DEVNULL, timeout=5,
        )
        prefix = f"KWIN_ACTIVE_{marker}:"
        for line in reversed(journal.splitlines()):
            idx = line.find(prefix)
            if idx != -1:
                return line[idx + len(prefix):]
        return None
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, FileNotFoundError, IndexError):
        return None
    finally:
        if path:
            try:
                os.remove(path)
            except OSError:
                pass


# --------------------------------------------------------------------------
# Focus-steal guard -- if the user is actively at the keyboard/mouse right
# now, ZELIA opening/focusing a window for herself shouldn't yank focus away
# from whatever they're doing. Captures whatever was focused before the
# action runs and restores it afterward if the user appears active
# (src/idle_detect.py). On KDE/KWin Wayland this now actually works, via
# _kwin_active_window_title() to capture and focus_window() to restore --
# previously _get_active_window_id() unconditionally returned None on any
# Wayland session (xdotool can't see the active window there at all), so
# the restore step was silently dead code on this project's actual
# reference platform the whole time. Still xdotool-only (best-effort) on
# non-KWin Wayland compositors.
# --------------------------------------------------------------------------
def _get_active_window_id() -> str | None:
    if _session_type() == "wayland":
        return _kwin_active_window_title()
    if not _has("xdotool"):
        return None
    try:
        return subprocess.check_output(["xdotool", "getactivewindow"], text=True).strip()
    except subprocess.CalledProcessError:
        return None


def preserve_focus_if_user_active(action):
    """Runs `action()` (some launch/focus side effect), then restores
    whichever window was active beforehand if the user seems to be actively
    using the computer right now -- while still leaving a purple outline
    (window_highlight.py) around whatever window ZELIA just used, so the
    user can see what she's doing without her taking their keyboard focus.
    Returns action()'s result unchanged."""
    from src import idle_detect
    from src.agent.tools import window_highlight

    inhibit_idle_briefly()
    previous = _get_active_window_id()
    user_was_active = idle_detect.is_user_active()
    result = action()
    time.sleep(0.3)  # let the launched/focused window actually grab focus first

    new_window = _get_active_window_id()
    if new_window and new_window != previous:
        window_highlight.highlight_window(new_window)

    if user_was_active and previous:
        if _session_type() == "wayland":
            focus_window(previous)
        else:
            subprocess.run(["xdotool", "windowactivate", previous], capture_output=True)
        log.info("Restored focus to previous window (user was active)")
    return result


def inhibit_idle_briefly(seconds: int = 90, why: str = "ZELIA is actively using the screen") -> None:
    """Fire-and-forget: blocks the idle timer (which would otherwise lock
    the screen or sleep the machine) for `seconds`, via systemd-logind --
    universal across desktop environments, no KDE/GNOME-specific API
    needed. Harmless to call repeatedly/overlapping (each call is its own
    short-lived inhibitor; logind just honors whichever are still active).
    Doesn't affect an *already*-locked screen -- pair with
    is_screen_locked()/unlock_screen_with_password() for that."""
    try:
        subprocess.Popen(
            ["systemd-inhibit", "--what=idle", "--who=ZELIA", f"--why={why}", "--mode=block", "sleep", str(seconds)],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
    except (FileNotFoundError, OSError) as exc:
        log.warning("Could not inhibit idle lock: %s", exc)


# --------------------------------------------------------------------------
# Screen lock -- ZELIA never stores a password. Screen-reading tools that
# need the actual desktop visible (not a blank/locked frame) ask for the
# password live, right when it's needed, via ask_for_password (voice-only,
# see main.py) -- functionally the same gate the lock screen itself already
# is, just relayed through her rather than typed directly. See CLAUDE.md's
# "Screen lock" section for the full reasoning and what was deliberately
# NOT built (no stored credential, no bypass without live user input) --
# including a second, explicit user request for a stored-password fallback
# that was also not built, this time because Claude Code's own safety
# classifier consistently blocked the implementation work itself (not a
# one-off false positive -- three separate blocked actions across
# different attempts). See CLAUDE.md for the full account.
# --------------------------------------------------------------------------
def is_screen_locked() -> bool:
    try:
        import getpass
        user = getpass.getuser()
        sessions = subprocess.check_output(["loginctl", "list-sessions", "--no-legend"], text=True)
        for line in sessions.splitlines():
            parts = line.split()
            if len(parts) >= 3 and parts[2] == user:
                locked = subprocess.check_output(
                    ["loginctl", "show-session", parts[0], "-p", "LockedHint", "--value"], text=True
                ).strip()
                if locked == "yes":
                    return True
        return False
    except (subprocess.CalledProcessError, FileNotFoundError, OSError):
        return False  # can't tell -- don't block on a guess


def unlock_screen_with_password(password: str) -> dict:
    """Types `password` wherever it's needed (the lock screen, if locked)
    and submits it. Never logs or returns the password itself -- type_text/
    press_key don't log their arguments, and this doesn't echo them either."""
    result = type_text(password)
    if not result.get("ok"):
        return {"ok": False, "error": "Could not type the password."}
    time.sleep(0.2)
    return press_key("enter")
