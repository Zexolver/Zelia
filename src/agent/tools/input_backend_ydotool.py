"""
Archived input backend: real keyboard/mouse synthesis via ydotool
(kernel uinput level) and xdotool (Xorg only). This is a FALLBACK, off by
default -- it shares the one real system cursor and keyboard focus with
the user's actual physical devices (confirmed no separate synthetic input
exists on this platform without portal support, see
input_backend_portal.py's docstring for the full story), so anything
ZELIA does here is visible and can interfere with whatever the user is
doing with their own mouse/keyboard at the same time.

Kept available (config.yaml: desktop.input_backend: "ydotool") for
compositors where the portal-based backend isn't usable -- e.g. no
xdg-desktop-portal RemoteDesktop implementation, or a compositor that
doesn't support the transient-seat protocol yet.
"""
import re
import subprocess
import tempfile
import time

from src.utils.logger import get_logger

log = get_logger("input_backend_ydotool")


def _session_type() -> str:
    import os
    return os.environ.get("XDG_SESSION_TYPE", "x11").lower()


def _has(binary: str) -> bool:
    import shutil
    return shutil.which(binary) is not None


# Minimal symbolic-name -> ydotool key sequence map for the combos this
# project actually needs (address bar focus, new tab, enter, tab/escape,
# scrolling a long page). xdotool takes symbolic names directly and needs
# none of this.
YDOTOOL_KEYS = {
    "ctrl": "29", "alt": "56", "shift": "42", "super": "125",
    "enter": "28", "tab": "15", "escape": "1", "l": "38", "t": "20",
    "space": "57", "pagedown": "109", "pageup": "104",
    "down": "108", "up": "103", "end": "107", "home": "102",
}


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


# --------------------------------------------------------------------------
# Mouse positioning (Wayland) -- see click_at()'s docstring for why this
# exists instead of a plain `ydotool mousemove --absolute` call. KDE/KWin
# specific (uses KWin scripting for cursor readback) -- acceptable since
# this whole module is an opt-in fallback, not the generic default.
# --------------------------------------------------------------------------
_KWIN_CURSOR_POS_RE = re.compile(r"KWIN_CURSOR_POS_(?P<marker>[0-9a-f]{8}):(?P<x>-?\d+),(?P<y>-?\d+)")


def _damped_step(delta: int, frac: float = 0.3, max_step: int = 250, min_step: int = 8) -> int:
    """Used by click_at()'s homing loop -- see its comment for why a
    straight proportional move isn't safe here."""
    if delta == 0:
        return 0
    step = int(delta * frac)
    if abs(step) < min_step:
        step = min_step if delta > 0 else -min_step
    if abs(step) > max_step:
        step = max_step if delta > 0 else -max_step
    if abs(step) > abs(delta):
        step = delta
    return step


def _kwin_cursor_pos() -> tuple[int, int] | None:
    """Reads the *real* cursor position straight from the compositor via a
    throwaway KWin script (org.kde.kwin.Scripting -- runs inside KWin's own
    process, so unlike ScreenShot2 it isn't gated behind the same
    unprivileged-client authorization wall). `workspace.cursorPos` is
    read-only in this KWin scripting API (confirmed live: writing to it
    raises 'Cannot assign to read-only property'), so this can't warp the
    cursor directly -- it's only used as ground truth for click_at()'s
    homing loop below. Returns None if anything about this fails (no
    busctl, KWin not running, script errored) so callers can fall back."""
    import os
    if not _has("busctl"):
        return None
    marker = os.urandom(4).hex()
    script = f'print("KWIN_CURSOR_POS_{marker}:" + workspace.cursorPos.x + "," + workspace.cursorPos.y);'
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
        for line in reversed(journal.splitlines()):
            m = _KWIN_CURSOR_POS_RE.search(line)
            if m and m.group("marker") == marker:
                return int(m.group("x")), int(m.group("y"))
        return None
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, FileNotFoundError,
            IndexError, ValueError):
        return None
    finally:
        if path:
            try:
                import os as _os
                _os.remove(path)
            except OSError:
                pass


def _home_cursor_to(x: int, y: int) -> tuple[int, int] | None:
    """Moves the real cursor to (x, y) via closed-loop homing and returns
    where it actually ended up (None if KWin's readback isn't available
    at all -- non-KDE compositor, or busctl/journalctl missing).

    On Wayland, `ydotool mousemove --absolute` does NOT map to real screen
    pixels -- confirmed by inspecting the ydotoold virtual device directly
    via python-evdev: it only advertises EV_KEY and EV_REL capabilities, no
    EV_ABS axis exists at all, so "absolute" is really ydotool internally
    tracking its own assumed position and converting to a relative move
    from *that* guess, with no way to ever resync against where the real
    compositor cursor actually is.

    On KDE/KWin, this uses a proper closed-loop fix instead: read the
    REAL cursor position via a KWin script (_kwin_cursor_pos(), runs
    inside KWin's own process so it's authoritative), send a relative
    ydotool move for the remaining delta (EV_REL is genuinely supported),
    re-measure, and repeat until within a couple pixels of the target or a
    small iteration cap is hit -- self-correcting regardless of any
    pointer-acceleration curve distorting the exact relationship between a
    commanded relative delta and the actual resulting movement (confirmed
    live: a single relative move of (100, 100) actually landed at
    (107, 107), i.e. not 1:1 -- the homing loop absorbs that instead of
    needing an exact acceleration model)."""
    last_pos = None
    for _ in range(35):
        pos = _kwin_cursor_pos()
        if pos is None:
            return last_pos
        last_pos = pos
        cx, cy = pos
        dx, dy = x - cx, y - cy
        if abs(dx) <= 4 and abs(dy) <= 4:
            return pos
        step_x = _damped_step(dx)
        step_y = _damped_step(dy)
        subprocess.run(["ydotool", "mousemove", "--", str(step_x), str(step_y)], check=True)
        time.sleep(0.08)
    return last_pos


def with_real_cursor_restored(action):
    """Runs `action()`, then puts the real cursor back wherever it was
    before -- this backend has no separate synthetic pointer of its own
    (ydotool's virtual device shares the one system cursor with the
    user's actual mouse), so any mouse-based action taken here (click_at)
    visibly moves the same cursor the user might be looking at or using.
    No-op (silently) if KWin's cursor readback isn't available at all."""
    original = _kwin_cursor_pos()
    try:
        return action()
    finally:
        if original is not None:
            _home_cursor_to(*original)


def click_at(x: int, y: int) -> dict:
    """Clicks at real screen pixel coordinates (x, y). See
    _home_cursor_to's docstring for how this reaches the target reliably
    on KDE/KWin; restores the real cursor to wherever it was beforehand
    afterward (see with_real_cursor_restored)."""
    def _do_click():
        session = _session_type()
        try:
            if session == "wayland" and _has("ydotool"):
                final_pos = _home_cursor_to(x, y)
                if final_pos is None:
                    subprocess.run(["ydotool", "mousemove", "--absolute", "-x", str(x), "-y", str(y)], check=True)
                elif abs(final_pos[0] - x) > 4 or abs(final_pos[1] - y) > 4:
                    log.warning("click_at homing didn't fully converge (ended near %s, target %s) -- clicking there anyway.",
                                final_pos, (x, y))
                subprocess.run(["ydotool", "click", "0xC0"], check=True)  # left click
            elif _has("xdotool"):
                subprocess.run(["xdotool", "mousemove", str(x), str(y), "click", "1"], check=True)
            else:
                return {"ok": False, "error": "Neither xdotool nor ydotool is available."}
            return {"ok": True}
        except subprocess.CalledProcessError as exc:
            return {"ok": False, "error": str(exc)}

    return with_real_cursor_restored(_do_click)
