"""
Dispatches big/quality-sensitive project work to AirLLM without blocking the
rest of ZELIA.

Each job spawns a separate subprocess (src/brains/airllm_worker.py) with its
own capped slice of VRAM (see gpu_manager). submit_async() returns
immediately; the small brain keeps handling normal conversation and simple
commands the whole time.

If a game is running (game_guard), new jobs are queued instead of started --
AirLLM is the one part of ZELIA that's genuinely GPU-heavy, so it's the part
that waits its turn. A background thread drains the queue as soon as gaming
stops. When a job finishes, on_done(result) fires from a background thread.
"""
import json
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from collections import deque

from src.utils.logger import get_logger

log = get_logger("large_brain")


class LargeBrain:
    def __init__(self, game_guard=None, queue_poll_seconds: float = 5.0):
        self._active_jobs = {}
        self._queue = deque()
        self._queue_lock = threading.Lock()
        self.game_guard = game_guard
        self._queue_poll_seconds = queue_poll_seconds
        if self.game_guard is not None:
            self._start_queue_drainer()

    def submit_async(self, prompt: str, on_done, model: str | None = None, compression: str | None = None):
        job = {"prompt": prompt, "on_done": on_done, "model": model, "compression": compression}

        if self.game_guard is not None and self.game_guard.is_gaming():
            log.info("Game detected -- queuing big-brain job until it's over.")
            with self._queue_lock:
                self._queue.append(job)
            return "queued"

        return self._run_job(job)

    def _run_job(self, job: dict) -> str:
        job_id = str(uuid.uuid4())[:8]
        job_path = tempfile.mktemp(prefix=f"zelia_job_{job_id}_", suffix=".json")
        result_path = tempfile.mktemp(prefix=f"zelia_result_{job_id}_", suffix=".json")

        with open(job_path, "w") as f:
            json.dump({"prompt": job["prompt"], "model": job["model"], "compression": job["compression"]}, f)

        log.info("Submitting big-brain job %s in the background...", job_id)

        def run():
            proc = subprocess.run(
                [sys.executable, "-m", "src.brains.airllm_worker", job_path, result_path],
            )
            try:
                with open(result_path) as f:
                    result = json.load(f)
            except FileNotFoundError:
                result = {"ok": False, "error": f"worker exited with code {proc.returncode}, no result written"}
            self._active_jobs.pop(job_id, None)
            job["on_done"](result)

        thread = threading.Thread(target=run, daemon=True)
        self._active_jobs[job_id] = thread
        thread.start()
        return job_id

    def _start_queue_drainer(self):
        def loop():
            while True:
                time.sleep(self._queue_poll_seconds)
                if self.game_guard.is_gaming():
                    continue
                with self._queue_lock:
                    if not self._queue:
                        continue
                    job = self._queue.popleft()
                log.info("Game over (or none running) -- starting queued big-brain job.")
                self._run_job(job)

        threading.Thread(target=loop, daemon=True).start()

    def has_active_jobs(self) -> bool:
        return len(self._active_jobs) > 0

    def queued_count(self) -> int:
        with self._queue_lock:
            return len(self._queue)
