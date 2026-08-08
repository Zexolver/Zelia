"""
Tracks whether the user is actively at the keyboard/mouse right now, so
ZELIA can avoid stealing window focus while someone's clearly in the
middle of using their own machine (see desktop_control.py's focus-steal
guard and app_launcher.py).

Reads raw input events straight from the kernel via evdev -- same
mechanism/permissions as hotkey_listener.py and ydotool -- so this works
identically on Xorg and Wayland with no compositor-specific code. Listens
to every keyboard AND pointer device (not just keyboards, unlike
hotkey_listener) since mouse movement alone should still count as "the
user is here."
"""
import threading
import time

from src.utils.logger import get_logger

log = get_logger("idle_detect")

_last_activity = time.time()
_lock = threading.Lock()
_tracking = False

# ydotool's own uinput-created virtual device (confirmed via
# /proc/bus/input/devices: "N: Name=ydotoold virtual device") -- found
# live 2026-08-07 as a real, previously-unnoticed bug: this module was
# watching it too, meaning ZELIA's own synthetic type_text/press_key/
# click_at input (injected through it) incorrectly counted as "the user
# is here," resetting the idle clock right after her own actions. That's
# self-defeating for the busy-gate this feeds (agent_loop._user_busy) --
# it should reflect whether a REAL person is at the keyboard, not whether
# ZELIA herself just moved the mouse. Same exclusion input_lock.py
# already uses for the same device, same reason.
_EXCLUDED_DEVICE_NAMES = {"ydotoold virtual device"}


def _find_input_devices():
    import evdev
    devices = []
    for path in evdev.list_devices():
        try:
            dev = evdev.InputDevice(path)
        except OSError:
            continue
        if dev.name in _EXCLUDED_DEVICE_NAMES:
            continue
        caps = dev.capabilities()
        has_keys = evdev.ecodes.EV_KEY in caps
        has_pointer_motion = evdev.ecodes.EV_REL in caps or evdev.ecodes.EV_ABS in caps
        if has_keys or has_pointer_motion:
            devices.append(dev)
    return devices


def start() -> None:
    """Starts a background thread that updates the last-activity timestamp
    on every keypress/click/mouse-move. Safe to call once at startup;
    is_user_active() defaults to "not active" if this was never started or
    no input devices could be opened."""
    try:
        import evdev
        import select
    except ImportError:
        log.warning("python-evdev not installed -- can't detect user activity for the focus-steal guard.")
        return

    devices = _find_input_devices()
    if not devices:
        log.warning(
            "No input devices found under /dev/input (or missing 'input' group "
            "permissions) -- can't detect user activity for the focus-steal guard."
        )
        return

    def loop():
        global _last_activity, _tracking
        fd_map = {dev.fd: dev for dev in devices}
        log.info("Watching %d input device(s) for user-activity detection", len(fd_map))
        with _lock:
            _tracking = True
        while True:
            r, _, _ = select.select(fd_map, [], [])
            for fd in r:
                try:
                    for _event in fd_map[fd].read():
                        with _lock:
                            _last_activity = time.time()
                except OSError:
                    pass  # device unplugged mid-read etc. -- just keep going with the rest

    threading.Thread(target=loop, daemon=True).start()


def seconds_since_last_activity() -> float:
    with _lock:
        return time.time() - _last_activity


def is_user_active(threshold_seconds: float = 10.0) -> bool:
    """True if there's been keyboard/mouse input within the last
    `threshold_seconds`. Defaults to "active" (the safer assumption -- don't
    steal focus) if tracking never actually started, e.g. no python-evdev,
    no input devices found, or start() was never called."""
    with _lock:
        if not _tracking:
            return True
        idle_for = time.time() - _last_activity
    return idle_for < threshold_seconds
