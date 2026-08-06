"""
Genuinely isolated input backend via libei/EIS (Emulated Input), reached
through the same org.freedesktop.portal.RemoteDesktop portal session as
input_backend_portal.py -- but via that interface's ConnectToEIS() method
instead of its plain Notify* D-Bus calls.

Why this module exists: input_backend_portal.py was proven live (see
CLAUDE.md issue 24) to move the REAL default-seat cursor -- reading
workspace.cursorPos via KWin scripting immediately before/after a
Notify*-based click_at() showed it moving in lockstep with the
"isolated" seat's motion. Root-caused by reading xdg-desktop-portal-kde's
actual source (invent.kde.org, tag v6.7.3): its Notify* handlers go
through WaylandIntegration::FakeInput (the legacy org_kde_kwin_fake_input
protocol), which has always targeted the real default seat -- nothing to
do with transient seats at all. ConnectToEIS is a genuinely different
code path: xdg-desktop-portal-kde just proxies it straight to a private
KWin D-Bus method (org.kde.KWin.EIS.RemoteDesktop.connectToEIS),
implemented by KWin's own src/plugins/eis backend, which is the piece
that actually talks EIS/transient-seat.

First attempt at this hung indefinitely. Root cause (confirmed by
reading KWin's actual eisbackend.cpp, tag v6.7.3): its capability
mapping only ever grants EIS_DEVICE_CAP_KEYBOARD for the portal's
"keyboard" bit -- it never grants EIS_DEVICE_CAP_TEXT. The first version
of this module waited for a CAP_TEXT device before proceeding, which
KWin will simply never hand out -- an infinite wait, not a hang bug or a
missing-ScreenCast requirement (both were considered and ruled out; see
CLAUDE.md issue 24 for the full trail). Fixed by only waiting on
POINTER/BUTTON/SCROLL/KEYBOARD, and typing through
ei_device_keyboard_key() (raw evdev keycodes) instead of
ei_device_text_keysym() -- which needs a real keymap-aware keysym ->
keycode lookup, done here via a small ctypes binding around the system
libxkbcommon (no working Python binding could be installed into the
read-only production venv without an interactive sudo password, and
this is simple enough not to need one).

Session bootstrap deliberately does NOT use liboeffis (see the earlier
version of this module) -- liboeffis's oeffis_create_session() has no
persist_mode/restore_token parameter at all, so every session it creates
needs a fresh consent dialog. Session creation here is the same manual
CreateSession/SelectDevices/Start D-Bus dance as input_backend_portal.py,
restore_token included, so this backend can reuse that *same* saved
grant (~/.zelia/state/portal_restore_token) -- confirmed live: a second
process reusing the token got a ready session in ~2 seconds with no
dialog at all. ConnectToEIS is then just one more call on that session.
"""
import ctypes
import mmap
import os
import select
import threading
import time
import uuid

import gi
gi.require_version("Gio", "2.0")
from gi.repository import Gio, GLib

from src.utils.logger import get_logger

log = get_logger("input_backend_eis")

PORTAL_BUS_NAME = "org.freedesktop.portal.Desktop"
PORTAL_OBJECT_PATH = "/org/freedesktop/portal/desktop"
REMOTE_DESKTOP_IFACE = "org.freedesktop.portal.RemoteDesktop"

DEVICE_KEYBOARD = 1
DEVICE_POINTER = 2
PERSIST_UNTIL_REVOKED = 2

_STATE_DIR = os.path.expanduser("~/.zelia/state")
# Shared with input_backend_portal.py on purpose -- same portal session
# concept (RemoteDesktop, same device types), so the same one-time human
# approval covers both backends.
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


# --- libei: the EIS client protocol ---
_libei = ctypes.CDLL("libei.so.1")
_libei.ei_new_sender.restype = ctypes.c_void_p
_libei.ei_new_sender.argtypes = [ctypes.c_void_p]
_libei.ei_setup_backend_fd.restype = ctypes.c_int
_libei.ei_setup_backend_fd.argtypes = [ctypes.c_void_p, ctypes.c_int]
_libei.ei_get_fd.restype = ctypes.c_int
_libei.ei_get_fd.argtypes = [ctypes.c_void_p]
_libei.ei_dispatch.restype = None
_libei.ei_dispatch.argtypes = [ctypes.c_void_p]
_libei.ei_get_event.restype = ctypes.c_void_p
_libei.ei_get_event.argtypes = [ctypes.c_void_p]
_libei.ei_event_unref.restype = ctypes.c_void_p
_libei.ei_event_unref.argtypes = [ctypes.c_void_p]
_libei.ei_event_get_type.restype = ctypes.c_int
_libei.ei_event_get_type.argtypes = [ctypes.c_void_p]
_libei.ei_event_get_seat.restype = ctypes.c_void_p
_libei.ei_event_get_seat.argtypes = [ctypes.c_void_p]
_libei.ei_event_get_device.restype = ctypes.c_void_p
_libei.ei_event_get_device.argtypes = [ctypes.c_void_p]
_libei.ei_seat_ref.restype = ctypes.c_void_p
_libei.ei_seat_ref.argtypes = [ctypes.c_void_p]
_libei.ei_seat_bind_capabilities.restype = None  # variadic, no argtypes
_libei.ei_device_ref.restype = ctypes.c_void_p
_libei.ei_device_ref.argtypes = [ctypes.c_void_p]
_libei.ei_device_has_capability.restype = ctypes.c_bool
_libei.ei_device_has_capability.argtypes = [ctypes.c_void_p, ctypes.c_int]
_libei.ei_device_start_emulating.restype = None
_libei.ei_device_start_emulating.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
_libei.ei_device_frame.restype = None
_libei.ei_device_frame.argtypes = [ctypes.c_void_p, ctypes.c_uint64]
_libei.ei_device_pointer_motion.restype = None
_libei.ei_device_pointer_motion.argtypes = [ctypes.c_void_p, ctypes.c_double, ctypes.c_double]
_libei.ei_device_button_button.restype = None
_libei.ei_device_button_button.argtypes = [ctypes.c_void_p, ctypes.c_uint32, ctypes.c_bool]
_libei.ei_device_keyboard_key.restype = None
_libei.ei_device_keyboard_key.argtypes = [ctypes.c_void_p, ctypes.c_uint32, ctypes.c_bool]
_libei.ei_device_scroll_discrete.restype = None
_libei.ei_device_scroll_discrete.argtypes = [ctypes.c_void_p, ctypes.c_int32, ctypes.c_int32]
_libei.ei_now.restype = ctypes.c_uint64
_libei.ei_now.argtypes = [ctypes.c_void_p]
_libei.ei_device_keyboard_get_keymap.restype = ctypes.c_void_p
_libei.ei_device_keyboard_get_keymap.argtypes = [ctypes.c_void_p]
_libei.ei_keymap_get_fd.restype = ctypes.c_int
_libei.ei_keymap_get_fd.argtypes = [ctypes.c_void_p]
_libei.ei_keymap_get_size.restype = ctypes.c_size_t
_libei.ei_keymap_get_size.argtypes = [ctypes.c_void_p]

EI_EVENT_DISCONNECT = 2
EI_EVENT_SEAT_ADDED = 3
EI_EVENT_DEVICE_ADDED = 5
EI_EVENT_DEVICE_RESUMED = 8

EI_DEVICE_CAP_POINTER = 1 << 0
EI_DEVICE_CAP_KEYBOARD = 1 << 2
EI_DEVICE_CAP_SCROLL = 1 << 4
EI_DEVICE_CAP_BUTTON = 1 << 5

# NOTE: deliberately no EI_DEVICE_CAP_TEXT -- KWin's eis backend never
# grants it (see module docstring). Binding for it anyway is harmless
# (an unsupported capability is just never fulfilled), but only these
# four are ever actually waited on.
_BOUND_CAPS = (EI_DEVICE_CAP_POINTER, EI_DEVICE_CAP_BUTTON, EI_DEVICE_CAP_SCROLL, EI_DEVICE_CAP_KEYBOARD)
_NEEDED_CAPS = (EI_DEVICE_CAP_POINTER, EI_DEVICE_CAP_BUTTON, EI_DEVICE_CAP_KEYBOARD)

BTN_LEFT = 0x110

# --- libxkbcommon: keysym -> (keycode, level) reverse lookup, needed
# because EIS keyboard devices only take raw evdev keycodes, not keysyms
# (see module docstring) ---
_libxkb = ctypes.CDLL("libxkbcommon.so.0")
_libxkb.xkb_context_new.restype = ctypes.c_void_p
_libxkb.xkb_context_new.argtypes = [ctypes.c_int]
_libxkb.xkb_context_unref.argtypes = [ctypes.c_void_p]
_libxkb.xkb_keymap_new_from_buffer.restype = ctypes.c_void_p
_libxkb.xkb_keymap_new_from_buffer.argtypes = [
    ctypes.c_void_p, ctypes.c_char_p, ctypes.c_size_t, ctypes.c_int, ctypes.c_int,
]
_libxkb.xkb_keymap_unref.argtypes = [ctypes.c_void_p]
_libxkb.xkb_keymap_min_keycode.restype = ctypes.c_uint32
_libxkb.xkb_keymap_min_keycode.argtypes = [ctypes.c_void_p]
_libxkb.xkb_keymap_max_keycode.restype = ctypes.c_uint32
_libxkb.xkb_keymap_max_keycode.argtypes = [ctypes.c_void_p]
_libxkb.xkb_keymap_key_get_syms_by_level.restype = ctypes.c_int
_libxkb.xkb_keymap_key_get_syms_by_level.argtypes = [
    ctypes.c_void_p, ctypes.c_uint32, ctypes.c_uint32, ctypes.c_uint32,
    ctypes.POINTER(ctypes.POINTER(ctypes.c_uint32)),
]

XKB_KEYMAP_FORMAT_TEXT_V1 = 1
# evdev keycodes are XKB keycodes minus 8 (the X11/XKB convention keeps
# the first 8 keycodes reserved) -- this offset is a hard protocol
# constant, not a guess.
_XKB_EVDEV_OFFSET = 8

KEYSYMS = {
    "ctrl": 0xFFE3, "alt": 0xFFE9, "shift": 0xFFE1, "super": 0xFFEB,
    "enter": 0xFF0D, "return": 0xFF0D, "tab": 0xFF09, "escape": 0xFF1B,
    "space": 0x0020, "pagedown": 0xFF56, "pageup": 0xFF55,
    "down": 0xFF54, "up": 0xFF52, "home": 0xFF50, "end": 0xFF57,
}


def _keysym_for_char(ch: str) -> int:
    code = ord(ch)
    if 0x20 <= code <= 0xFF:
        return code
    return 0x01000000 + code


class _Keymap:
    """Builds a keysym -> (evdev keycode, needs_shift) table from the
    XKB keymap EIS hands us, so type_text/press_key can turn characters
    into the raw keycodes ei_device_keyboard_key() actually needs.
    Layout is always index 0 (the keymap's first/only configured
    layout) -- good enough for a single-layout keyboard, which is the
    common case; a multi-layout setup would need live active-layout
    tracking via xkb_state, not attempted here."""

    def __init__(self, keymap_fd: int, size: int):
        with os.fdopen(keymap_fd, "rb") as f:
            data = f.read(size)
        context = _libxkb.xkb_context_new(0)
        if not context:
            raise RuntimeError("xkb_context_new() failed.")
        try:
            keymap = _libxkb.xkb_keymap_new_from_buffer(
                context, data, len(data), XKB_KEYMAP_FORMAT_TEXT_V1, 0,
            )
            if not keymap:
                raise RuntimeError("xkb_keymap_new_from_buffer() failed to parse the EIS keymap.")
            try:
                self._table: dict[int, tuple[int, bool]] = {}
                min_kc = _libxkb.xkb_keymap_min_keycode(keymap)
                max_kc = _libxkb.xkb_keymap_max_keycode(keymap)
                syms_ptr = ctypes.POINTER(ctypes.c_uint32)()
                for keycode in range(min_kc, max_kc + 1):
                    for level, needs_shift in ((0, False), (1, True)):
                        n = _libxkb.xkb_keymap_key_get_syms_by_level(
                            keymap, keycode, 0, level, ctypes.byref(syms_ptr),
                        )
                        for i in range(n):
                            keysym = syms_ptr[i]
                            if keysym not in self._table:
                                self._table[keysym] = (keycode - _XKB_EVDEV_OFFSET, needs_shift)
            finally:
                _libxkb.xkb_keymap_unref(keymap)
        finally:
            _libxkb.xkb_context_unref(context)

    def lookup(self, keysym: int) -> tuple[int, bool] | None:
        return self._table.get(keysym)


class _EisSession:
    """Lazily-created, process-lifetime-persistent EIS connection, built
    on the same manual portal D-Bus session as input_backend_portal.py
    (restore_token included) so the two backends can share one grant."""

    def __init__(self):
        self._lock = threading.Lock()
        self._bus = None
        self._sender_name = None
        self._session_handle = None
        self._ei = None
        self._devices: dict[int, int] = {}  # capability flag -> device handle
        self._resumed: set[int] = set()
        self._seq = 0
        self._keymap: _Keymap | None = None
        self._ready = False

    # --- portal D-Bus session (mirrors input_backend_portal.py) ---

    def _ensure_bus(self):
        if self._bus is not None:
            return
        self._bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
        self._sender_name = self._bus.get_unique_name()[1:].replace(".", "_")
        loop = GLib.MainLoop()
        threading.Thread(target=loop.run, daemon=True).start()

    def _request_call(self, method: str, args: list, arg_types: str, timeout: float = 120.0) -> dict:
        token = "zelia_eis_" + uuid.uuid4().hex[:8]
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

    def _connect_to_eis(self) -> int:
        """Calls RemoteDesktop.ConnectToEIS, which (unlike CreateSession/
        SelectDevices/Start) replies synchronously with a real unix fd --
        needs call_with_unix_fd_list_sync, not plain call_sync, or the
        attached fd never gets delivered."""
        variant = GLib.Variant("(oa{sv})", (self._session_handle, {}))
        result, fd_list = self._bus.call_with_unix_fd_list_sync(
            PORTAL_BUS_NAME, PORTAL_OBJECT_PATH, REMOTE_DESKTOP_IFACE, "ConnectToEIS",
            variant, GLib.VariantType.new("(h)"), Gio.DBusCallFlags.NONE, -1, None, None,
        )
        (handle_index,) = result.unpack()
        return fd_list.get(handle_index)

    # --- EIS event handling ---

    def _wait_ei_until(self, predicate, timeout: float) -> None:
        deadline = time.monotonic() + timeout
        fd = _libei.ei_get_fd(self._ei)
        while not predicate():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("Timed out waiting for EIS seat/device setup.")
            ready, _, _ = select.select([fd], [], [], remaining)
            if not ready:
                continue
            _libei.ei_dispatch(self._ei)
            while True:
                ev = _libei.ei_get_event(self._ei)
                if not ev:
                    break
                self._handle_ei_event(ev)
                _libei.ei_event_unref(ev)

    def _handle_ei_event(self, ev) -> None:
        ev_type = _libei.ei_event_get_type(ev)
        if ev_type == EI_EVENT_SEAT_ADDED:
            seat = _libei.ei_seat_ref(_libei.ei_event_get_seat(ev))
            args = [ctypes.c_void_p(seat)] + [ctypes.c_int(c) for c in _BOUND_CAPS] + [ctypes.c_int(0)]
            _libei.ei_seat_bind_capabilities(*args)
        elif ev_type == EI_EVENT_DEVICE_ADDED:
            device = _libei.ei_device_ref(_libei.ei_event_get_device(ev))
            for cap in _BOUND_CAPS:
                if _libei.ei_device_has_capability(device, cap) and cap not in self._devices:
                    self._devices[cap] = device
        elif ev_type == EI_EVENT_DEVICE_RESUMED:
            self._resumed.add(_libei.ei_event_get_device(ev))
        elif ev_type == EI_EVENT_DISCONNECT:
            raise RuntimeError("EIS implementation disconnected during setup.")

    def ensure_ready(self, timeout: float = 120.0) -> None:
        with self._lock:
            if self._ready:
                return
            self._ensure_bus()

            restore_token = _load_restore_token()
            log.info(
                "No active EIS session yet -- creating one via the RemoteDesktop portal "
                + ("using a saved restore token (should reuse the earlier grant, no new "
                   "consent prompt)." if restore_token else
                   "(this will show a one-time 'control input devices' consent prompt).")
            )

            session_token = "zelia_eis_session_" + uuid.uuid4().hex[:8]
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

            eis_fd = self._connect_to_eis()
            if eis_fd < 0:
                raise RuntimeError("ConnectToEIS returned an invalid fd.")

            self._ei = _libei.ei_new_sender(None)
            if not self._ei:
                raise RuntimeError("ei_new_sender() failed.")
            rc = _libei.ei_setup_backend_fd(self._ei, eis_fd)  # takes ownership of eis_fd
            if rc != 0:
                raise RuntimeError(f"ei_setup_backend_fd() failed (errno {-rc}).")

            self._wait_ei_until(
                lambda: all(
                    cap in self._devices and self._devices[cap] in self._resumed
                    for cap in _NEEDED_CAPS
                ),
                timeout,
            )
            for device in {self._devices[cap] for cap in _NEEDED_CAPS}:
                self._seq += 1
                _libei.ei_device_start_emulating(device, self._seq)

            keyboard_device = self._devices[EI_DEVICE_CAP_KEYBOARD]
            keymap_ptr = _libei.ei_device_keyboard_get_keymap(keyboard_device)
            if keymap_ptr:
                keymap_fd = _libei.ei_keymap_get_fd(keymap_ptr)
                keymap_size = _libei.ei_keymap_get_size(keymap_ptr)
                self._keymap = _Keymap(keymap_fd, keymap_size)
            log.info("EIS session active (capabilities bound: %s).",
                     sorted(k for k in self._devices))
            self._ready = True

    def _frame(self, device: int) -> None:
        _libei.ei_device_frame(device, _libei.ei_now(self._ei))

    def pointer_motion(self, dx: float, dy: float) -> None:
        device = self._devices[EI_DEVICE_CAP_POINTER]
        _libei.ei_device_pointer_motion(device, dx, dy)
        self._frame(device)

    def button(self, button: int, pressed: bool) -> None:
        device = self._devices[EI_DEVICE_CAP_BUTTON]
        _libei.ei_device_button_button(device, button, pressed)
        self._frame(device)

    def key_by_keysym(self, keysym: int, pressed: bool) -> None:
        if self._keymap is None:
            raise RuntimeError("No keymap available from the EIS keyboard device.")
        looked_up = self._keymap.lookup(keysym)
        if looked_up is None:
            raise RuntimeError(f"Keysym 0x{keysym:x} isn't present on the current keyboard layout.")
        keycode, needs_shift = looked_up
        device = self._devices[EI_DEVICE_CAP_KEYBOARD]
        shift_keycode, _ = self._keymap.lookup(KEYSYMS["shift"]) or (None, None)
        if needs_shift and shift_keycode is not None and pressed:
            _libei.ei_device_keyboard_key(device, shift_keycode, True)
        _libei.ei_device_keyboard_key(device, keycode, pressed)
        self._frame(device)
        if needs_shift and shift_keycode is not None and not pressed:
            _libei.ei_device_keyboard_key(device, shift_keycode, False)
            self._frame(device)

    def scroll_discrete(self, x: int, y: int) -> None:
        device = self._devices[EI_DEVICE_CAP_SCROLL]
        _libei.ei_device_scroll_discrete(device, x, y)
        self._frame(device)


_session = _EisSession()

# Same anchor-then-offset strategy as input_backend_portal.py: no
# cursor-position feedback exists for this seat either.
_ANCHOR_OFFSET = 20000


def _press_release_keysym(keysym: int, hold_seconds: float = 0.02) -> None:
    _session.key_by_keysym(keysym, True)
    time.sleep(hold_seconds)
    _session.key_by_keysym(keysym, False)


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
                return {"ok": False, "error": f"Key combo '{combo}' isn't in the EIS backend's keysym map yet -- add it to KEYSYMS."}
        for k in keysyms:
            _session.key_by_keysym(k, True)
            time.sleep(0.01)
        for k in reversed(keysyms):
            _session.key_by_keysym(k, False)
            time.sleep(0.01)
        return {"ok": True}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc)}


def click_at(x: int, y: int) -> dict:
    try:
        _session.ensure_ready()
        _session.pointer_motion(-_ANCHOR_OFFSET, -_ANCHOR_OFFSET)
        time.sleep(0.05)
        _session.pointer_motion(float(x), float(y))
        time.sleep(0.05)
        _session.button(BTN_LEFT, True)
        time.sleep(0.02)
        _session.button(BTN_LEFT, False)
        return {"ok": True}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc)}


def scroll(amount: int) -> dict:
    try:
        _session.ensure_ready()
        _session.scroll_discrete(0, amount * 120)
        return {"ok": True}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc)}
