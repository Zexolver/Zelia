"""Lets the agent run shell commands. Anything that looks destructive is
flagged so agent_loop can ask for a spoken "yes" before it actually runs."""
import re
import subprocess

from src.utils.logger import get_logger

log = get_logger("shell_tool")

# Deliberately broad/paranoid -- false positives just mean an extra "are you sure?",
# false negatives mean data loss, so this errs toward asking.
DESTRUCTIVE_PATTERNS = [
    r"\brm\s+-rf\b", r"\brm\s+", r"\bmkfs\b", r"\bdd\s+if=", r"\bshutdown\b",
    r"\breboot\b", r"\bpoweroff\b", r"\bsystemctl\s+(stop|disable|mask)\b",
    r"\b>\s*/dev/sd", r"\bchmod\s+-R\b", r"\bchown\s+-R\b", r"\bkill\s+-9\b",
    r"\bpacman\s+-R", r"\bgit\s+push\s+.*--force\b", r"\bgit\s+reset\s+--hard\b",
    r"\btruncate\b", r"\b:(){ :\|:& };:",
]


def is_destructive(command: str) -> bool:
    return any(re.search(p, command) for p in DESTRUCTIVE_PATTERNS)


def run_shell(command: str, confirmed: bool = False) -> dict:
    if is_destructive(command) and not confirmed:
        return {"needs_confirmation": True, "command": command}

    log.info("Running shell command: %s", command)
    try:
        proc = subprocess.run(
            command, shell=True, capture_output=True, text=True, timeout=120
        )
        return {
            "needs_confirmation": False,
            "returncode": proc.returncode,
            "stdout": proc.stdout[-4000:],
            "stderr": proc.stderr[-4000:],
        }
    except subprocess.TimeoutExpired:
        return {"needs_confirmation": False, "returncode": -1, "stdout": "", "stderr": "Command timed out."}
