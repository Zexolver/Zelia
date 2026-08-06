"""
Default input backend: keyboard/mouse via org.freedesktop.portal.RemoteDesktop,
a standard XDG Desktop Portal interface backed by the compositor's own
transient-seat mechanism (ext-transient-seat-v1) where implemented.

Explicit user requirement this exists for: ZELIA's mouse/keyboard actions
must not interfere with the user's real devices while they're using the
computer (gaming, Blender, etc). ydotool's virtual input device (see
input_backend_ydotool.py) shares the ONE system cursor and keyboard focus
with the user's real hardware -- confirmed by inspecting its uinput
capabilities directly (EV_KEY/EV_REL only), and by reading the raw
Wayland protocol model (wl_seat is fundamentally single-pointer,
single-keyboard-focus; there's no compositor-agnostic way around that).

This backend uses a genuinely separate input path instead: the portal
creates an isolated seat for this session (that's what the compositor's
transient-seat support is for), so key/pointer events injected here don't
touch the real seat at all. Confirmed live: typed text into a focused
window via NotifyKeyboardKeysym while the real keyboard was left alone,
and it correctly followed whatever window the compositor considers
focused -- no extra focus-tracking code needed here, focus_window() in
desktop_control.py already handles that the same way regardless of which
input backend is active.

NOT KDE-specific on purpose (explicit user requirement) -- this only
calls the standard org.freedesktop.portal.Desktop D-Bus interface, never
anything KWin-specific. Whichever portal backend + compositor combination
the user's desktop environment ships is what actually provides the
isolated seat; per wayland.app's compositor compatibility matrix this
includes KWin 6.6+, Mutter 49.2+, and wlroots 0.18+-based compositors
(Sway, Hyprland, etc), among others.

One-time setup, persisted across restarts: the first-ever input action
triggers a real consent dialog ("<app> is asking for special privileges:
Control input devices") that a human must approve -- same principle as
every other permission prompt in this project, not something to be
auto-clicked (confirmed live: even if it could be scripted, a synthetic
click made by ZELIA's own current input path approving her own privilege
escalation request would defeat the point of asking at all -- this is
the same reasoning as declining the screen-unlock-bypass request
earlier).

That approval is remembered from then on using the portal's own
persist_mode/restore_token mechanism (RemoteDesktop interface version 2,
confirmed present here via `busctl --user introspect
org.freedesktop.portal.Desktop /org/freedesktop/portal/desktop
org.freedesktop.portal.RemoteDesktop` -> `.version 2`) -- this is the
same officially-supported "remember this grant" feature screen-sharing
apps use so you're not asked every single call, not a workaround or a
bypass of the prompt. SelectDevices is called with persist_mode=2
("until explicitly revoked") and, once granted, Start()'s results
include a restore_token that's saved to disk (~/.zelia/state/
portal_restore_token) and replayed on every future SelectDevices call so
the portal recognizes the grant and skips the dialog. Delete that file
(or revoke the grant via the desktop's own permission settings) to force
a fresh prompt.

Known limitation: click_at's absolute positioning isn't backed by real
cursor-position feedback the way the ydotool backend's KWin-scripting
readback was -- the isolated seat has no compositor-scripting equivalent
exposed here (and building one would be compositor-specific, which this
module deliberately avoids). Real pixel-accurate absolute positioning
would need a paired ScreenCast session (NotifyPointerMotionAbsolute takes
a stream ID tied to one) -- not built yet, follow-up work. For now,
click_at anchors to a known origin by moving a relative distance large
enough to hit a screen edge (compositors reliably clamp cursor motion at
monitor boundaries), then moves the exact target offset from that known
point in one single relative move.
"""
import os
import threading
import time
import uuid

import gi
gi.require_version("Gio", "2.0")
from gi.repository import Gio, GLib

from src.utils.logger import get_logger

log = get_logger("input_backend_portal")

PORTAL_BUS_NAME = "org.freedesktop.portal.Desktop"
PORTAL_OBJECT_PATH = "/org/freedesktop/portal/desktop"
REMOTE_DESKTOP_IFACE = "org.freedesktop.portal.RemoteDesktop"

DEVICE_KEYBOARD = 1
DEVICE_POINTER = 2

# persist_mode: 2 = "persist until explicitly revoked" (vs 0 = don't
# persist, 1 = only while the app is running) -- see this module's
# docstring for why this is the portal's own supported mechanism, not a
# workaround.
PERSIST_UNTIL_REVOKED = 2

_STATE_DIR = os.path.expanduser("~/.zelia/state")
_RESTORE_TOKEN_PATH = os.path.join(_STATE_DIR, "portal_restore_token")


def _load_restore_token() -> str | None:
    try:
        with open(_RESTORE_TOKEN_PATH) as f:
            token = f.read().strip()
            return token or None
    except OSError:
        return None


def _save_restore_token(token: str) -> None:
    os.makedirs(_STATE_DIR, exist_ok=True)
    with open(_RESTORE_TOKEN_PATH, "w") as f:
        f.write(token)

# A large-enough relative move to hit any real monitor edge from anywhere
# on this project's reference 3-monitor, 4280x1920 virtual desktop (see
# CLAUDE.md's click_at history) -- comfortably oversized for smaller
# setups too.
_ANCHOR_OFFSET = 20000

# Symbolic-name -> X11 keysym map for the combos this project actually
# needs. Lowercase ASCII letters/digits use their own keysym value
# (matches ord()), so only the special/modifier keys need an entry here.
KEYSYMS = {
    "ctrl": 0xFFE3, "alt": 0xFFE9, "shift": 0xFFE1, "super": 0xFFEB,
    "enter": 0xFF0D, "return": 0xFF0D, "tab": 0xFF09, "escape": 0xFF1B,
    "space": 0x0020, "pagedown": 0xFF56, "pageup": 0xFF55,
    "down": 0xFF54, "up": 0xFF52, "home": 0xFF50, "end": 0xFF57,
}


def _keysym_for_char(ch: str) -> int:
    """X11 keysym convention: Latin-1 range (0x20-0xff) keysyms equal the
    Unicode code point directly; anything else uses the "Unicode keysym"
    convention (0x01000000 + code point), which portal implementations
    based on libei/xkbcommon understand."""
    code = ord(ch)
    if 0x20 <= code <= 0xFF:
        return code
    return 0x01000000 + code


class _PortalSession:
    """Lazily-created, process-lifetime-persistent RemoteDesktop session.
    Created on first use (one consent prompt), reused for every
    subsequent input action -- re-creating a session per call would mean
    a consent prompt every single time, which isn't just annoying, it's
    the kind of thing that trains a user to reflexively click through
    security prompts without reading them."""

    def __init__(self):
        self._lock = threading.Lock()
        self._bus = None
        self._session_handle = None
        self._sender_name = None
        self._ready = False

    def _ensure_bus(self):
        if self._bus is not None:
            return
        self._bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
        self._sender_name = self._bus.get_unique_name()[1:].replace(".", "_")
        loop = GLib.MainLoop()
        threading.Thread(target=loop.run, daemon=True).start()

    def _request_call(self, method: str, args: list, arg_types: str, timeout: float = 120.0) -> dict:
        """Calls a portal method that returns a Request object path, and
        blocks until that request's Response signal fires (this is the
        standard XDG portal async pattern -- CreateSession/SelectDevices/
        Start all work this way)."""
        token = "zelia_" + uuid.uuid4().hex[:8]
        request_path = f"/org/freedesktop/portal/desktop/request/{self._sender_name}/{token}"

        result = {}
        event = threading.Event()

        def on_response(connection, sender, path, iface, signal, params, *_):
            code, results = params.unpack()
            result["code"] = code
            result["results"] = results
            event.set()

        sub_id = self._bus.signal_subscribe(
            PORTAL_BUS_NAME, "org.freedesktop.portal.Request", "Response",
            request_path, None, Gio.DBusSignalFlags.NONE, on_response,
        )
        try:
            options = dict(args[-1])
            options["handle_token"] = GLib.Variant("s", token)
            full_args = tuple(args[:-1]) + (options,)
            variant = GLib.Variant(f"({arg_types})", full_args)
            reply = self._bus.call_sync(
                PORTAL_BUS_NAME, PORTAL_OBJECT_PATH, REMOTE_DESKTOP_IFACE, method,
                variant, GLib.VariantType.new("(o)"), Gio.DBusCallFlags.NONE, -1, None,
            )
            actual_path = reply.unpack()[0]
            if actual_path != request_path:
                # Compositor/portal gave us a different request object than
                # we predicted the token would produce -- resubscribe to the
                # real path rather than silently hanging forever.
                self._bus.signal_unsubscribe(sub_id)
                sub_id = self._bus.signal_subscribe(
                    PORTAL_BUS_NAME, "org.freedesktop.portal.Request", "Response",
                    actual_path, None, Gio.DBusSignalFlags.NONE, on_response,
                )
            if not event.wait(timeout=timeout):
                raise TimeoutError(f"Portal method '{method}' timed out waiting for a response.")
        finally:
            self._bus.signal_unsubscribe(sub_id)

        if result["code"] != 0:
            raise RuntimeError(f"Portal method '{method}' was denied or cancelled (code {result['code']}).")
        return result["results"]

    def ensure_ready(self) -> None:
        with self._lock:
            if self._ready:
                return
            self._ensure_bus()

            restore_token = _load_restore_token()
            if restore_token:
                log.info("No active RemoteDesktop portal session yet -- creating one "
                         "using a saved restore token (should reuse the earlier grant, "
                         "no new consent prompt).")
            else:
                log.info("No active RemoteDesktop portal session yet -- creating one "
                         "(this will show a one-time 'control input devices' consent "
                         "prompt; approving it is remembered for next time).")

            session_token = "zelia_session_" + uuid.uuid4().hex[:8]
            results = self._request_call(
                "CreateSession", [{"session_handle_token": GLib.Variant("s", session_token)}], "a{sv}",
            )
            self._session_handle = results["session_handle"]

            select_options = {
                "types": GLib.Variant("u", DEVICE_KEYBOARD | DEVICE_POINTER),
                "persist_mode": GLib.Variant("u", PERSIST_UNTIL_REVOKED),
            }
            if restore_token:
                select_options["restore_token"] = GLib.Variant("s", restore_token)
            self._request_call("SelectDevices", [self._session_handle, select_options], "oa{sv}")

            results = self._request_call("Start", [self._session_handle, "", {}], "osa{sv}")
            new_token = results.get("restore_token")
            if new_token:
                _save_restore_token(new_token)
            log.info("RemoteDesktop portal session active (devices granted: %s).", results.get("devices"))
            self._ready = True

    def _call_notify(self, method: str, args: tuple, arg_types: str) -> None:
        variant = GLib.Variant(f"({arg_types})", args)
        self._bus.call_sync(
            PORTAL_BUS_NAME, PORTAL_OBJECT_PATH, REMOTE_DESKTOP_IFACE, method,
            variant, None, Gio.DBusCallFlags.NONE, -1, None,
        )

    def notify_keyboard_keysym(self, keysym: int, pressed: bool) -> None:
        self._call_notify(
            "NotifyKeyboardKeysym",
            (self._session_handle, {}, keysym, 1 if pressed else 0),
            "oa{sv}iu",
        )

    def notify_pointer_motion(self, dx: float, dy: float) -> None:
        self._call_notify(
            "NotifyPointerMotion", (self._session_handle, {}, dx, dy), "oa{sv}dd",
        )

    def notify_pointer_button(self, button: int, pressed: bool) -> None:
        self._call_notify(
            "NotifyPointerButton",
            (self._session_handle, {}, button, 1 if pressed else 0),
            "oa{sv}iu",
        )

    def notify_pointer_axis_discrete(self, axis: int, steps: int) -> None:
        self._call_notify(
            "NotifyPointerAxisDiscrete",
            (self._session_handle, {}, axis, steps),
            "oa{sv}ui",
        )


_session = _PortalSession()


def _press_release_keysym(keysym: int, hold_seconds: float = 0.02) -> None:
    _session.notify_keyboard_keysym(keysym, True)
    time.sleep(hold_seconds)
    _session.notify_keyboard_keysym(keysym, False)


def type_text(text: str) -> dict:
    try:
        _session.ensure_ready()
        for ch in text:
            _press_release_keysym(_keysym_for_char(ch))
            time.sleep(0.01)
        return {"ok": True}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc)}


def press_key(combo: str) -> dict:
    """combo like 'ctrl+l', 'ctrl+t', 'enter', 'tab', 'escape', 'pagedown'."""
    try:
        _session.ensure_ready()
        parts = [p.strip().lower() for p in combo.split("+")]
        keysyms = []
        for p in parts:
            if p in KEYSYMS:
                keysyms.append(KEYSYMS[p])
            elif len(p) == 1:
                keysyms.append(_keysym_for_char(p))
            else:
                return {"ok": False, "error": f"Key combo '{combo}' isn't in the portal backend's keysym map yet -- add it to KEYSYMS."}

        for k in keysyms:
            _session.notify_keyboard_keysym(k, True)
            time.sleep(0.01)
        for k in reversed(keysyms):
            _session.notify_keyboard_keysym(k, False)
            time.sleep(0.01)
        return {"ok": True}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc)}


def click_at(x: int, y: int) -> dict:
    """Clicks at real screen pixel coordinates (x, y). See this module's
    docstring for the anchor-then-offset positioning strategy this uses
    in the absence of cursor-position feedback for the isolated seat."""
    try:
        _session.ensure_ready()
        # Pin the cursor at the top-left-most reachable point on the
        # virtual desktop -- compositors clamp relative motion at
        # monitor edges, so this lands at a known, deterministic origin
        # regardless of where the cursor started.
        _session.notify_pointer_motion(-_ANCHOR_OFFSET, -_ANCHOR_OFFSET)
        time.sleep(0.05)
        _session.notify_pointer_motion(float(x), float(y))
        time.sleep(0.05)
        _session.notify_pointer_button(0x110, True)  # BTN_LEFT
        time.sleep(0.02)
        _session.notify_pointer_button(0x110, False)
        return {"ok": True}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc)}


def scroll(amount: int) -> dict:
    """Positive amount scrolls down, negative scrolls up. One 'step' is
    one notch of a physical mouse wheel (matches NotifyPointerAxisDiscrete's
    own unit)."""
    try:
        _session.ensure_ready()
        _session.notify_pointer_axis_discrete(0, amount)  # axis 0 = vertical
        return {"ok": True}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc)}
