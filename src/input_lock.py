"""
Physical keyboard/mouse input lock -- a toggle hotkey combo that, once
pressed, makes ALL keyboard and mouse input get silently discarded
(kernel-level, via evdev's exclusive grab -- EVIOCGRAB) until the same
combo is pressed again. For "walk away without anyone bumping the
keyboard/mouse and doing something by accident" -- explicitly NOT a
security/authentication feature: it doesn't touch the session lock at
all, anyone could restart zelia.service to release it, and remote input
(e.g. RustDesk) injects on a separate synthetic path this never grabs, so
it isn't affected either way. Pure accidental-input guard.

Auto-releases after MAX_LOCK_SECONDS regardless of whether the unlock
combo is ever seen again, specifically so a bug here can never
permanently strand the user's own physical keyboard/mouse -- and even
that safety net aside, a plain `systemctl --user restart zelia` always
releases the grab immediately (closing the file descriptors releases
EVIOCGRAB), so recovery never depends on this code being bug-free.
"""
import threading
import time

from src.utils.logger import get_logger

log = get_logger("input_lock")

MAX_LOCK_SECONDS = 3600

_lock_active = threading.Event()


def is_locked() -> bool:
    return _lock_active.is_set()


# ydotool's own uinput-created virtual device (confirmed via
# /proc/bus/input/devices: "N: Name=ydotoold virtual device") -- must
# never be grabbed, or ZELIA's own type_text/press_key/click_at (which
# inject through it) would stop working the moment physical input gets
# locked, which is exactly backwards from what this feature is for.
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
        if evdev.ecodes.EV_KEY in caps or evdev.ecodes.EV_REL in caps or evdev.ecodes.EV_ABS in caps:
            devices.append(dev)
    return devices


def _lock_loop(combo_codes: set) -> None:
    import evdev
    import select

    devices = _find_input_devices()
    grabbed = []
    for dev in devices:
        try:
            dev.grab()
            grabbed.append(dev)
        except OSError as exc:
            log.warning("Could not grab %s: %s", dev.path, exc)

    if not grabbed:
        log.warning("Could not grab any input device -- input lock not engaged.")
        return

    log.info("Input locked (%d device(s)) -- press the same combo again to unlock.", len(grabbed))
    _lock_active.set()
    try:
        held = set()
        deadline = time.time() + MAX_LOCK_SECONDS
        fd_map = {dev.fd: dev for dev in grabbed}
        while time.time() < deadline:
            r, _, _ = select.select(fd_map, [], [], 1.0)
            for fd in r:
                try:
                    for event in fd_map[fd].read():
                        if event.type != evdev.ecodes.EV_KEY:
                            continue
                        if event.value == 1:
                            held.add(event.code)
                        elif event.value == 0:
                            held.discard(event.code)
                        if combo_codes <= held:
                            return
                except OSError:
                    continue
        log.warning("Input lock auto-released after %ds safety timeout.", MAX_LOCK_SECONDS)
    finally:
        for dev in grabbed:
            try:
                dev.ungrab()
            except OSError:
                pass
        _lock_active.clear()
        log.info("Input unlocked.")


def start_toggle_listener(combo: list) -> None:
    """Starts a background thread watching (non-exclusively) for `combo`
    (evdev KEY_* names, e.g. ["KEY_LEFTMETA", "KEY_LEFTCTRL", "KEY_L"]) to
    be held together. Each time it's seen while unlocked, engages the
    input lock (grabs every keyboard/mouse device, discards their events
    in a separate loop -- see _lock_loop, which watches for this same
    combo to release)."""
    try:
        import evdev
        import select
    except ImportError:
        log.warning("python-evdev not installed -- input-lock hotkey is disabled.")
        return

    try:
        combo_codes = {getattr(evdev.ecodes, name) for name in combo}
    except AttributeError as exc:
        log.error("Invalid key name in input_lock combo (%s) -- disabled.", exc)
        return

    devices = _find_input_devices()
    if not devices:
        log.warning("No input devices found -- input-lock hotkey is disabled.")
        return

    def loop():
        held = set()
        fd_map = {dev.fd: dev for dev in devices}
        log.info("Input-lock toggle armed on %s", "+".join(combo))
        while True:
            if _lock_active.is_set():
                time.sleep(0.5)  # _lock_loop owns the devices exclusively while active
                continue
            r, _, _ = select.select(fd_map, [], [], 1.0)
            for fd in r:
                try:
                    for event in fd_map[fd].read():
                        if event.type != evdev.ecodes.EV_KEY:
                            continue
                        if event.value == 1:
                            held.add(event.code)
                        elif event.value == 0:
                            held.discard(event.code)
                        if combo_codes <= held and not _lock_active.is_set():
                            held.clear()
                            threading.Thread(target=_lock_loop, args=(combo_codes,), daemon=True).start()
                except OSError:
                    continue

    threading.Thread(target=loop, daemon=True).start()
