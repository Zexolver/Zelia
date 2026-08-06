"""
Keeps the AirLLM coding worker (src/brains/airllm_worker.py) from starving
the rest of the machine -- explicit user requirement: it needs to be able to
run unattended (e.g. while the user is out of Claude usage for the week)
without freezing the PC or starving Gemini CLI of CPU for smaller work
happening at the same time.

This is about system RAM/CPU, not VRAM (see gpu_manager.py for that side --
AirLLM runs on CPU on this machine's AMD GPU regardless). The mechanism is a
systemd --user scope wrapped around the worker subprocess, not an in-process
limit (e.g. resource.setrlimit(RLIMIT_AS, ...)) -- confirmed live that
RLIMIT_AS-style caps are the wrong tool here: PyTorch/numpy routinely reserve
large virtual address ranges well beyond what they actually touch, so a
virtual-address-space limit would fail on legitimate allocations long before
real memory pressure exists. A cgroup memory.max limit constrains real
(resident) usage instead, which is what actually matters.

Also confirmed live: MemoryMax alone does NOT prevent a runaway process from
swapping instead of dying -- with 15GB of free swap on this machine, a
process was able to blow straight through a 100MB MemoryMax by paging to
swap, which is arguably worse for "does the PC freeze" than a clean kill
would be (heavy swapping is what actually makes a Linux desktop feel
unresponsive, more so than a contained OOM-kill of one background process).
Pairing MemoryMax with MemorySwapMax=0 forces a hard kill of just this
cgroup instead -- verified: the same test that quietly succeeded via swap
got SIGKILL'd once MemorySwapMax=0 was added.

--- Turbidle ---

"Turbo while idle": explicit user request for a mode that gives the AirLLM
coding worker a much bigger resource budget, but only while the system is
genuinely doing nothing else -- no user input activity, no game running, and
no Gemini/Claude Code CLI process active either.

Named/requested as "splitting layers across CPU and GPU for faster speed" --
that framing doesn't actually hold on this machine: AirLLM has no usable
CUDA/ROCm path on this AMD card (see gpu_manager.py, confirmed repeatedly
this session) so it's CPU-only regardless, and the small brain is already
100% GPU-resident whenever nothing else is competing for VRAM (confirmed
live via `ollama ps`'s size_vram field -- it only partially fell back to
CPU when an actual game was contending for the same 8GB). The real lever
that delivers genuine, safe speed here is the inverse of the plain caps
above: they exist specifically to protect the rest of the machine from the
coding worker; Turbidle lifts that protection when there's nothing left to
protect, so the worker gets to use most of the machine instead of a
deliberately small slice of it.
"""
import re
from dataclasses import dataclass

import psutil

from src import idle_detect
from src.utils.logger import get_logger

log = get_logger("resource_manager")

# Same spirit as game_guard.py's process-pattern matching, reused here for
# a different question ("is a coding CLI actively using the machine") --
# deliberately broad/best-effort, like that module: a coding-agent CLI's
# real process name can vary (node wrapper, the tool's own binary name,
# etc), so this errs toward treating more things as "active" rather than
# risking Turbidle kicking in while Gemini CLI or Claude Code is genuinely
# working.
CODING_CLI_PATTERNS = [re.compile(p, re.IGNORECASE) for p in (r"\bclaude\b", r"\bgemini\b")]


def _coding_cli_active(own_pid: int | None = None) -> bool:
    own_pid = own_pid or psutil.Process().pid
    for proc in psutil.process_iter(["pid", "name"]):
        if proc.info["pid"] == own_pid:
            continue
        name = proc.info.get("name") or ""
        if any(p.search(name) for p in CODING_CLI_PATTERNS):
            return True
    return False


def is_fully_idle(game_guard=None) -> bool:
    """True only if there's no recent keyboard/mouse activity, no game
    running, and no Gemini/Claude Code CLI process active -- the bar for
    Turbidle specifically, deliberately stricter than the "user busy"
    check agent_loop.py's busy-gate uses for click_at/type_text/etc."""
    if idle_detect.is_user_active():
        return False
    if game_guard is not None and game_guard.is_gaming():
        return False
    if _coding_cli_active():
        return False
    return True

# Conservative defaults, not tuned to any specific machine's total RAM/core
# count -- config.yaml overrides these per-install. Deliberately low: this
# is explicitly meant to be slow-but-safe background work, not a race to
# finish quickly.
DEFAULT_MAX_RAM_MB = 6144
DEFAULT_CPU_QUOTA_PERCENT = 400  # e.g. 4 of 12 cores' worth of wall-clock CPU time
# cgroup-relative scheduling priority under contention (default weight for
# any unit is 100) -- NOT the same as classic process nice, which is an
# exec-context property systemd only applies when it forks the process
# itself for a real service; confirmed live that `-p Nice=15` on a
# `--scope` unit fails outright ("Unknown assignment: Nice=15") since a
# scope wraps an already-running process rather than owning its exec
# context. CPUWeight is the correct cgroup-native equivalent and does
# apply to scopes.
DEFAULT_CPU_WEIGHT = 25


@dataclass
class ResourceBudget:
    max_ram_mb: int
    cpu_quota_percent: int
    cpu_weight: int

    def systemd_run_args(self) -> list[str]:
        return [
            "systemd-run", "--user", "--scope", "--collect",
            "-p", f"MemoryMax={self.max_ram_mb}M",
            "-p", "MemorySwapMax=0",  # forces a hard kill instead of swap-thrashing, see module docstring
            "-p", f"CPUQuota={self.cpu_quota_percent}%",
            "-p", f"CPUWeight={self.cpu_weight}",
            "--",
        ]


# Turbidle defaults aren't fixed numbers like the plain caps above -- "how
# much RAM/CPU is safe to hand over when nothing else needs it" depends on
# the actual machine, so these are computed from what's really there
# (psutil) rather than guessed. Still leaves a margin rather than handing
# over 100%: the small brain (Ollama) and the rest of the desktop
# environment stay running even during Turbidle, they're just idle, not
# gone -- and this whole feature is meant to activate/deactivate live
# without a restart, so a job started under Turbidle needs to not
# immediately OOM the moment the user comes back and Ollama reloads.
_TURBIDLE_RAM_MARGIN_MB = 4096
_TURBIDLE_CPU_CORE_MARGIN = 1


def _turbidle_defaults() -> tuple[int, int]:
    total_ram_mb = int(psutil.virtual_memory().total / (1024 * 1024))
    max_ram_mb = max(total_ram_mb - _TURBIDLE_RAM_MARGIN_MB, DEFAULT_MAX_RAM_MB)
    core_count = psutil.cpu_count() or 4
    usable_cores = max(core_count - _TURBIDLE_CPU_CORE_MARGIN, 1)
    cpu_quota_percent = usable_cores * 100
    return max_ram_mb, cpu_quota_percent


def get_budget(cfg, turbidle: bool = False) -> ResourceBudget:
    large_cfg = cfg.brains.large

    if turbidle:
        default_ram_mb, default_cpu_quota = _turbidle_defaults()
        budget = ResourceBudget(
            max_ram_mb=large_cfg.get("turbidle_max_ram_mb", default_ram_mb),
            cpu_quota_percent=large_cfg.get("turbidle_cpu_quota_percent", default_cpu_quota),
            # Not deprioritized -- normal cgroup weight (100), since nothing
            # else is actually contending for the CPU right now.
            cpu_weight=large_cfg.get("turbidle_cpu_weight", 100),
        )
        log.info(
            "Turbidle active (system genuinely idle) -- AirLLM coding worker resource budget "
            "raised to %sMB RAM, %s%% CPU quota, CPU weight %s.",
            budget.max_ram_mb, budget.cpu_quota_percent, budget.cpu_weight,
        )
        return budget

    budget = ResourceBudget(
        max_ram_mb=large_cfg.get("max_ram_mb", DEFAULT_MAX_RAM_MB),
        cpu_quota_percent=large_cfg.get("cpu_quota_percent", DEFAULT_CPU_QUOTA_PERCENT),
        cpu_weight=large_cfg.get("cpu_weight", DEFAULT_CPU_WEIGHT),
    )
    log.info(
        "AirLLM coding worker resource budget: %sMB RAM (hard cap, no swap overflow), "
        "%s%% CPU quota, CPU weight %s (out of the usual 100).",
        budget.max_ram_mb, budget.cpu_quota_percent, budget.cpu_weight,
    )
    return budget
