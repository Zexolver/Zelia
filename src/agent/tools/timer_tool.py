"""
Simple timers/reminders -- "remind me in 10 minutes to check the oven",
"set a timer for 5 minutes". Not backed by any external scheduler or
persisted to disk -- just a background thread per timer that sleeps then
fires a callback, scoped to this process's lifetime. A ZELIA restart
silently drops any pending timers -- unlike the idle-task queue (which
persists its *queue* to disk specifically so it survives a restart), a
timer is short-lived enough by nature (minutes, not days) that this
wasn't judged worth the same persistence effort; revisit if someone
actually sets an hours-long timer and loses it to a restart.
"""
import threading
import uuid

from src.utils.logger import get_logger

log = get_logger("timer_tool")

_active_timers: dict[str, threading.Timer] = {}
_lock = threading.Lock()


def set_timer(seconds: float, message: str, on_fire) -> dict:
    """on_fire: fn(message: str) -> None, called from a background thread
    when the timer goes off -- the caller decides how to actually surface
    it (agent_loop.py's dispatch wires this to a spoken announcement plus
    a desktop notification)."""
    if seconds <= 0:
        return {"ok": False, "error": "Timer duration must be positive."}

    timer_id = str(uuid.uuid4())[:8]

    def fire():
        with _lock:
            _active_timers.pop(timer_id, None)
        log.info("Timer %s fired: %s", timer_id, message)
        on_fire(message)

    timer = threading.Timer(seconds, fire)
    timer.daemon = True
    with _lock:
        _active_timers[timer_id] = timer
    timer.start()
    log.info("Set timer %s for %.0fs: %s", timer_id, seconds, message)
    return {"ok": True, "timer_id": timer_id, "seconds": seconds}


def cancel_timer(timer_id: str) -> dict:
    with _lock:
        timer = _active_timers.pop(timer_id, None)
    if timer is None:
        return {"ok": False, "error": f"No active timer with id '{timer_id}'."}
    timer.cancel()
    log.info("Cancelled timer %s", timer_id)
    return {"ok": True}


def list_timers() -> dict:
    with _lock:
        return {"ok": True, "active_timer_ids": list(_active_timers.keys())}
