"""
Screen brightness control -- "dim the screen", "brightness up". No
dedicated CLI tool (brightnessctl, light) is installed on the reference
machine, so this reads/writes the kernel backlight interface directly
under /sys/class/backlight/ -- the same thing those tools do internally.

Worth knowing before assuming this "isn't working": the reference machine
is a desktop with external monitors, not a laptop, so it may well have NO
/sys/class/backlight entries at all (desktop monitors are typically
controlled over DDC/CI by the monitor's own firmware, not the kernel
backlight API that's really meant for laptop panels/eDP) -- that's a
real hardware fact to detect and report honestly, not a bug to route
around. On hardware that does have one, writing to it commonly needs
root or a udev rule granting the logged-in user write access (a `video`
group ACL is the usual fix) -- if that's not already set up,
set_brightness fails with a clear permission error rather than silently
doing nothing.
"""
from pathlib import Path

from src.utils.logger import get_logger

log = get_logger("brightness_tool")

_BACKLIGHT_ROOT = Path("/sys/class/backlight")


def _find_backlight() -> Path | None:
    if not _BACKLIGHT_ROOT.is_dir():
        return None
    devices = [d for d in _BACKLIGHT_ROOT.iterdir() if d.is_dir()]
    return devices[0] if devices else None


_NO_DEVICE_ERROR = (
    "No backlight device found -- this machine may not have a kernel-controllable display "
    "backlight at all (common on desktops with external monitors, which are usually adjusted "
    "via the monitor's own controls/DDC-CI instead)."
)


def get_brightness() -> dict:
    device = _find_backlight()
    if device is None:
        # Confirmed live (2026-08-06): this path had no logging at all,
        # which left it genuinely ambiguous whether a "no backlight
        # device" reply meant the tool was actually called, or the model
        # just paraphrased this same error text straight from its own
        # tool-schema description without calling anything. Every other
        # branch in this module already logs -- this was the one gap.
        log.info("get_brightness: no backlight device found.")
        return {"ok": False, "error": _NO_DEVICE_ERROR}
    try:
        current = int((device / "brightness").read_text().strip())
        maximum = int((device / "max_brightness").read_text().strip())
        percent = round(current / maximum * 100) if maximum else 0
        log.info("get_brightness: %d%% (device=%s)", percent, device.name)
        return {"ok": True, "brightness_percent": percent}
    except (OSError, ValueError) as exc:
        return {"ok": False, "error": f"Could not read brightness: {exc}"}


def set_brightness(percent: int) -> dict:
    device = _find_backlight()
    if device is None:
        return {"ok": False, "error": _NO_DEVICE_ERROR}
    percent = max(0, min(100, percent))
    try:
        maximum = int((device / "max_brightness").read_text().strip())
        target = round(maximum * percent / 100)
        (device / "brightness").write_text(str(target))
        log.info("Set brightness to %d%% (device=%s)", percent, device.name)
        return {"ok": True, "brightness_percent": percent}
    except PermissionError:
        return {
            "ok": False,
            "error": (
                "No permission to change brightness directly -- this usually needs a udev rule "
                "granting the 'video' group write access to /sys/class/backlight, or installing "
                "brightnessctl (which sets one up automatically)."
            ),
        }
    except (OSError, ValueError) as exc:
        return {"ok": False, "error": f"Could not set brightness: {exc}"}
