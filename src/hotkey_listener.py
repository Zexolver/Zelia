"""
Push-to-talk hotkey -- a second way to activate ZEUS besides the wake word.

Reads raw keyboard events straight from the kernel via evdev (the same
'input' group access install.sh sets up for ydotool), which is why this
works identically on Xorg and Wayland with no compositor-specific code:
it's listening below the display protocol entirely, at the same level
uinput operates on.

Point of this existing at all: wake word *detection* is an audio classifier
tuned for normal speaking volume, and it gets unreliable when you're
speaking quietly (e.g. not wanting to wake someone up). The hotkey sidesteps
that -- press it, then just talk, and only VAD-based recording + Whisper are
involved, which handle quiet/whispered speech much better than a phrase
detector does.
"""
import threading

from src.utils.logger import get_logger

log = get_logger("hotkey_listener")


def _find_keyboards():
    import evdev
    keyboards = []
    for path in evdev.list_devices():
        try:
            dev = evdev.InputDevice(path)
        except OSError:
            continue
        capabilities = dev.capabilities().get(evdev.ecodes.EV_KEY, [])
        # Heuristic: a real keyboard has letter keys, not just a couple of
        # media buttons (avoids grabbing random single-button input devices).
        if evdev.ecodes.KEY_A in capabilities and evdev.ecodes.KEY_Z in capabilities:
            keyboards.append(dev)
    return keyboards


def start_hotkey_listener(key_name: str, on_press):
    """Starts a background thread that calls on_press() every time `key_name`
    (an evdev KEY_* name, e.g. 'KEY_SCROLLLOCK') is pressed on any keyboard."""
    try:
        import evdev
        import select
    except ImportError:
        log.warning("python-evdev not installed -- push-to-talk hotkey is disabled.")
        return None

    try:
        target_code = getattr(evdev.ecodes, key_name)
    except AttributeError:
        log.error("'%s' isn't a valid evdev key name -- hotkey disabled. See README for how to pick one.", key_name)
        return None

    keyboards = _find_keyboards()
    if not keyboards:
        log.warning(
            "No keyboard devices found under /dev/input (or missing 'input' group "
            "permissions) -- push-to-talk hotkey is disabled. Log out/in after "
            "install.sh if you haven't yet; that's what grants this access."
        )
        return None

    def loop():
        devices = {dev.fd: dev for dev in keyboards}
        log.info("Push-to-talk hotkey armed on %s (%d keyboard device(s))", key_name, len(devices))
        while True:
            r, _, _ = select.select(devices, [], [])
            for fd in r:
                dev = devices[fd]
                for event in dev.read():
                    if event.type == evdev.ecodes.EV_KEY and event.code == target_code and event.value == 1:
                        log.info("Push-to-talk hotkey pressed")
                        on_press()

    thread = threading.Thread(target=loop, daemon=True)
    thread.start()
    return thread
