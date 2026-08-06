"""
Screen lock and system power actions -- "lock the screen", "shut down the
computer", "restart", "put it to sleep". Fills a real, previously-noted
gap (CLAUDE.md's Jarvis-feature-comparison writeup explicitly called out
"no direct lock tool exists").

Lock uses loginctl (systemd-logind's own session-lock mechanism) rather
than anything screensaver/compositor-specific -- desktop-environment-
agnostic, the standard systemd-native way to do this, not a KDE-only
dbus call, so it should keep working even if this project's reference
desktop ever changes.

Power actions are real and hard to reverse (shutdown/restart end the
current session outright; suspend interrupts whatever's running) -- this
module doesn't gate on confirmation itself, it just runs what it's told.
The caller (agent_loop.py's _dispatch_tool) is responsible for routing
these through the same needs_confirmation flow already used for
destructive shell commands, exactly like that existing pattern, not a
new mechanism invented here. lock_screen deliberately is NOT gated the
same way -- it's easily reversible (unlock again) and low-risk, so it
should feel snappy ("hey lock my screen" -> immediate), unlike the
power actions above it.
"""
import subprocess

from src.utils.logger import get_logger

log = get_logger("power_tool")


def lock_screen() -> dict:
    try:
        subprocess.run(["loginctl", "lock-session"], check=True, timeout=5)
        log.info("Locked the screen.")
        return {"ok": True}
    except FileNotFoundError:
        return {"ok": False, "error": "loginctl isn't available -- can't lock the screen this way."}
    except (subprocess.TimeoutExpired, subprocess.CalledProcessError) as exc:
        return {"ok": False, "error": f"Could not lock the screen: {exc}"}


_POWER_ACTIONS = {
    "shutdown": ["systemctl", "poweroff"],
    "restart": ["systemctl", "reboot"],
    "suspend": ["systemctl", "suspend"],
}


def power_action(action: str) -> dict:
    cmd = _POWER_ACTIONS.get(action)
    if cmd is None:
        return {"ok": False, "error": f"Unknown power action '{action}' -- must be one of {list(_POWER_ACTIONS)}."}
    try:
        log.info("Running power action: %s", action)
        subprocess.run(cmd, check=True, timeout=5)
        return {"ok": True, "action": action}
    except FileNotFoundError:
        return {"ok": False, "error": "systemctl isn't available -- can't perform power actions this way."}
    except (subprocess.TimeoutExpired, subprocess.CalledProcessError) as exc:
        return {"ok": False, "error": f"Could not {action}: {exc}"}
