"""
Real desktop control -- opens a visible terminal for commands, types/clicks
into whatever window has focus, and works on both Xorg and Wayland.

Xorg: xdotool handles typing, key combos, and clicking directly.
Wayland: no single protocol covers this across compositors, so:
  - typing/key combos/clicking go through ydotool, which operates at the
    kernel uinput level and therefore works regardless of compositor
    (needs ydotoold running and the user in the 'input' group -- install.sh
    sets this up).
  - window focusing is best-effort: tried via wlrctl/swaymsg on
    wlroots-based compositors (Sway, Hyprland); on GNOME/KDE Wayland there's
    no standard way to do this from outside, so it's skipped there and we
    just rely on newly-launched windows already having focus.
"""
import os
import shutil
import subprocess
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
# project actually needs (address bar focus, new tab, enter, tab/escape).
# xdotool takes symbolic names directly and needs none of this.
YDOTOOL_KEYS = {
    "ctrl": "29", "alt": "56", "shift": "42", "super": "125",
    "enter": "28", "tab": "15", "escape": "1", "l": "38", "t": "20",
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

    if command is None:
        subprocess.Popen([terminal], cwd=cwd)
        return {"ok": True, "terminal": terminal, "action": "opened"}

    flags = TERMINAL_RUN_FLAGS.get(terminal, ["-e", "bash", "-c"])
    shell_cmd = command if not keep_open else f"{command}; echo; echo '[done -- press enter to close]'; read"
    subprocess.Popen([terminal, *flags, shell_cmd], cwd=cwd)
    log.info("Ran in visible terminal (%s, cwd=%s): %s", terminal, cwd, command)
    return {"ok": True, "terminal": terminal, "action": "ran", "command": command}


# --------------------------------------------------------------------------
# Typing / key combos
# --------------------------------------------------------------------------
def type_text(text: str) -> dict:
    session = _session_type()
    try:
        if session == "wayland" and _has("ydotool"):
            subprocess.run(["ydotool", "type", text], check=True)
        elif _has("xdotool"):
            subprocess.run(["xdotool", "type", "--clearmodifiers", text], check=True)
        else:
            return {"ok": False, "error": "Neither xdotool nor ydotool is available."}
        return {"ok": True}
    except subprocess.CalledProcessError as exc:
        return {"ok": False, "error": str(exc)}


def press_key(combo: str) -> dict:
    """combo like 'ctrl+l', 'ctrl+t', 'Return', 'Tab', 'Escape'."""
    session = _session_type()
    try:
        if session == "wayland" and _has("ydotool"):
            parts = [p.strip().lower() for p in combo.split("+")]
            codes = [YDOTOOL_KEYS.get(p) for p in parts]
            if any(c is None for c in codes):
                return {"ok": False, "error": f"Key combo '{combo}' isn't in the ydotool keymap yet -- add it to YDOTOOL_KEYS."}
            down = [f"{c}:1" for c in codes]
            up = [f"{c}:0" for c in reversed(codes)]
            subprocess.run(["ydotool", "key", *down, *up], check=True)
        elif _has("xdotool"):
            subprocess.run(["xdotool", "key", combo.replace("+", "+")], check=True)
        else:
            return {"ok": False, "error": "Neither xdotool nor ydotool is available."}
        return {"ok": True}
    except subprocess.CalledProcessError as exc:
        return {"ok": False, "error": str(exc)}


def click_at(x: int, y: int) -> dict:
    session = _session_type()
    try:
        if session == "wayland" and _has("ydotool"):
            subprocess.run(["ydotool", "mousemove", "--absolute", "-x", str(x), "-y", str(y)], check=True)
            subprocess.run(["ydotool", "click", "0xC0"], check=True)  # left click
        elif _has("xdotool"):
            subprocess.run(["xdotool", "mousemove", str(x), str(y), "click", "1"], check=True)
        else:
            return {"ok": False, "error": "Neither xdotool nor ydotool is available."}
        return {"ok": True}
    except subprocess.CalledProcessError as exc:
        return {"ok": False, "error": str(exc)}


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

    if _has("swaymsg"):
        subprocess.run(["swaymsg", f'[title="{name}"] focus'], capture_output=True)
        return {"ok": True, "note": "best-effort on Sway"}
    if _has("wlrctl"):
        subprocess.run(["wlrctl", "window", "focus", name], capture_output=True)
        return {"ok": True, "note": "best-effort via wlrctl"}

    return {"ok": False, "error": "No window-focus mechanism available on this Wayland compositor -- new windows should already be focused on launch."}


# --------------------------------------------------------------------------
# Focus-steal guard -- if the user is actively at the keyboard/mouse right
# now, ZELIA opening/focusing a window for herself shouldn't yank focus away
# from whatever they're doing. Captures whatever was focused before the
# action runs and restores it afterward if the user appears active
# (src/idle_detect.py). Xorg only for now via xdotool -- same "best-effort
# on Wayland" situation as focus_window above, since restoring focus needs
# the same window-activation mechanism that's already Wayland-limited there.
# --------------------------------------------------------------------------
def _get_active_window_id() -> str | None:
    if _session_type() == "wayland" or not _has("xdotool"):
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

    previous = _get_active_window_id()
    user_was_active = idle_detect.is_user_active()
    result = action()
    time.sleep(0.3)  # let the launched/focused window actually grab focus first

    new_window = _get_active_window_id()
    if new_window and new_window != previous:
        window_highlight.highlight_window(new_window)

    if user_was_active and previous:
        subprocess.run(["xdotool", "windowactivate", previous], capture_output=True)
        log.info("Restored focus to previous window (user was active)")
    return result
