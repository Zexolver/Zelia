"""
Lets the user hand ZELIA low-priority "idle tasks" -- things to work on
only when nothing else needs her attention: no recent keyboard/mouse
activity, no game running, no Gemini/Claude Code CLI process active, and
she's not already mid-request. Explicit user request: give her a to-do
list she works through in the background instead of sitting fully idle,
without ever fighting the user for keyboard/mouse/GUI focus while they're
actually at the machine.

Reuses resource_manager.is_fully_idle() for the "is now a safe time" gate
-- the same bar Turbidle uses for the AirLLM coding worker's resource
budget, checked here both before starting a task and, via
handle_request's should_continue, on every tool round while one is
running -- so a task started during a genuinely idle stretch stops itself
the moment the user comes back, instead of continuing to click/type
against a screen they're now using.

An idle task is a plain natural-language description, run through the
exact same agent.handle_request() pipeline a typed/spoken request goes
through -- an idle task IS a request, just one that originates from a
timer instead of the user's voice/keyboard right now. Deliberately NOT
spoken aloud when it finishes (if the system was genuinely idle, likely
nobody's there to hear it) -- results are only logged and stored to
second_brain (handle_request already does that remember() call itself),
so "what did you do while I was away" stays answerable via recall.

Persisted to a small JSON file rather than kept only in memory, so a
queued task survives a service restart.
"""
import json
import os
import threading
import time

from src.resource_manager import is_fully_idle
from src.utils.logger import get_logger

log = get_logger("idle_tasks")


class IdleTaskRunner:
    def __init__(self, state_path: str, agent, activation_lock: threading.Lock,
                 game_guard=None, poll_seconds: float = 30.0):
        self._state_path = state_path
        self._agent = agent
        self._activation_lock = activation_lock
        self._game_guard = game_guard
        self._poll_seconds = poll_seconds
        self._lock = threading.Lock()
        self._queue: list[str] = self._load()

    def _load(self) -> list[str]:
        try:
            with open(self._state_path) as f:
                return json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            return []

    def _save(self):
        os.makedirs(os.path.dirname(self._state_path), exist_ok=True)
        with open(self._state_path, "w") as f:
            json.dump(self._queue, f)

    def add(self, description: str) -> int:
        with self._lock:
            self._queue.append(description)
            self._save()
            position = len(self._queue)
        log.info("Queued idle task (#%d in queue): %s", position, description)
        return position

    def pending_count(self) -> int:
        with self._lock:
            return len(self._queue)

    def _pop_next(self) -> str | None:
        with self._lock:
            if not self._queue:
                return None
            task = self._queue.pop(0)
            self._save()
            return task

    def _requeue_front(self, task: str):
        with self._lock:
            self._queue.insert(0, task)
            self._save()

    def start(self):
        threading.Thread(target=self._loop, daemon=True).start()

    def _loop(self):
        while True:
            time.sleep(self._poll_seconds)
            if not self.pending_count():
                continue
            if not is_fully_idle(self._game_guard):
                continue
            # Non-blocking: a real voice/text request already holding the
            # lock must never be interrupted or delayed by idle work -- if
            # it's held, just skip this poll cycle and try again later.
            if not self._activation_lock.acquire(blocking=False):
                continue
            try:
                task = self._pop_next()
                if task is None:
                    continue
                log.info("System is idle -- starting idle task: %s", task)

                def should_continue():
                    return is_fully_idle(self._game_guard)

                def on_reply(text):
                    log.info("Idle task reply: %s", text)

                try:
                    status = self._agent.handle_request(
                        task, speak=on_reply, remember_and_reply_when_done=on_reply,
                        should_continue=should_continue,
                    )
                    if status == "aborted":
                        log.info("Idle task interrupted by user activity -- requeued: %s", task)
                        self._requeue_front(task)
                except Exception as exc:  # noqa: BLE001
                    log.error("Idle task failed, dropping it (not requeued): %s", exc)
            finally:
                self._activation_lock.release()
