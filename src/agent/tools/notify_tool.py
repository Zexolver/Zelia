"""
Sends a real desktop notification (the same kind any other app uses, via
libnotify's notify-send) -- for things worth surfacing visually without
ZELIA necessarily interrupting out loud, and the always-available half of
a fired timer/reminder (see timer_tool.py) alongside a spoken announcement.
"""
import subprocess

from src.utils.logger import get_logger

log = get_logger("notify_tool")


def send_notification(title: str, message: str = "") -> dict:
    try:
        subprocess.run(["notify-send", title, message], check=True, timeout=5)
        log.info("Sent notification: %s -- %s", title, message)
        return {"ok": True}
    except FileNotFoundError:
        return {"ok": False, "error": "notify-send isn't installed -- can't send a desktop notification."}
    except (subprocess.TimeoutExpired, subprocess.CalledProcessError) as exc:
        return {"ok": False, "error": f"Could not send notification: {exc}"}
