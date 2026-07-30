"""
Detects gaming and lets the rest of the app defer to it.

Two signals, either one is enough to count as "gaming":
  1. A running process matches a known game/launcher pattern (configurable).
  2. A non-ZEUS process is hogging a big share of the GPU (via nvidia-smi).

Nothing here kills or pauses the game -- it just tells the rest of ZEUS
("agent_loop", "large_brain", "main") to back off: don't start new AirLLM
jobs, lower ZEUS's own process priority. Wake word + quick commands still
work the whole time.
"""
import re
import subprocess
import threading
import time

import psutil

from src.utils.logger import get_logger

log = get_logger("game_guard")

DEFAULT_PATTERNS = [
    r"\bsteam(app|linuxruntime)?\b", r"\bproton\b", r"\blutris\b", r"\bheroic\b",
    r"\bwine(64)?\b", r"\bretroarch\b", r"\bgamescope\b", r"\bminecraft\b",
    r"\.exe$",  # most Windows games run under Proton/Wine as an .exe process name
]

GPU_HOG_THRESHOLD_PCT = 25  # a non-ZEUS process using >25% of one GPU counts as "gaming/heavy load"


class GameGuard:
    def __init__(self, extra_patterns=None, poll_interval_seconds: float = 5.0, own_pid: int | None = None):
        patterns = DEFAULT_PATTERNS + list(extra_patterns or [])
        self._patterns = [re.compile(p, re.IGNORECASE) for p in patterns]
        self._poll_interval = poll_interval_seconds
        self._own_pid = own_pid or psutil.Process().pid
        self._is_gaming = False
        self._lock = threading.Lock()

    # -- detection -----------------------------------------------------
    def _process_match(self) -> bool:
        for proc in psutil.process_iter(["pid", "name", "exe"]):
            if proc.info["pid"] == self._own_pid:
                continue
            name = (proc.info.get("name") or "")
            for pattern in self._patterns:
                if pattern.search(name):
                    return True
        return False

    def _gpu_hog_match(self) -> bool:
        # Try NVIDIA first, then AMD (rocm-smi is often not installed unless
        # the full ROCm stack is set up -- that's fine, this signal is a
        # bonus on top of process-pattern matching, not the only one).
        pid = self._nvidia_compute_pid()
        if pid is not None:
            return pid != self._own_pid

        pid = self._amd_compute_pid()
        if pid is not None:
            return pid != self._own_pid

        return False

    @staticmethod
    def _nvidia_compute_pid():
        try:
            out = subprocess.check_output(
                ["nvidia-smi", "--query-compute-apps=pid,used_memory", "--format=csv,noheader,nounits"],
                text=True, timeout=3,
            )
        except Exception:
            return None
        for line in out.strip().splitlines():
            parts = [p.strip() for p in line.split(",")]
            if len(parts) == 2:
                try:
                    return int(parts[0])
                except ValueError:
                    continue
        return None

    @staticmethod
    def _amd_compute_pid():
        try:
            out = subprocess.check_output(["rocm-smi", "--showpids"], text=True, timeout=3)
        except Exception:
            return None
        for line in out.strip().splitlines():
            parts = line.split()
            if parts and parts[0].isdigit():
                return int(parts[0])
        return None

    def _check_once(self) -> bool:
        return self._process_match() or self._gpu_hog_match()

    # -- public API ------------------------------------------------------
    def is_gaming(self) -> bool:
        with self._lock:
            return self._is_gaming

    def start_background_poll(self):
        def loop():
            while True:
                gaming_now = self._check_once()
                with self._lock:
                    changed = gaming_now != self._is_gaming
                    self._is_gaming = gaming_now
                if changed:
                    log.info("Gaming state changed -> %s", "GAMING (yielding priority)" if gaming_now else "not gaming")
                time.sleep(self._poll_interval)

        thread = threading.Thread(target=loop, daemon=True)
        thread.start()
        return thread
