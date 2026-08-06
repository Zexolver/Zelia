"""
Reads local man pages (or --help output as a fallback for tools that
don't ship one) so ZELIA can look up a CLI command's REAL, actual usage
on this machine instead of guessing at flags from training data -- which
can be outdated or simply wrong for whatever version is actually
installed locally.
"""
import os
import shutil
import subprocess

from src.utils.logger import get_logger

log = get_logger("man_tool")

MAX_CHARS = 8000  # keep a very long man page from swamping the small brain's context


def read_man_page(command: str) -> dict:
    if not shutil.which(command):
        return {"ok": False, "error": f"'{command}' isn't installed (not found on PATH)."}

    text = ""
    source = ""
    try:
        env = {**os.environ, "MANWIDTH": "100"}
        result = subprocess.run(["man", command], capture_output=True, text=True, timeout=10, env=env)
        if result.returncode == 0 and result.stdout.strip():
            # man's output has backspace-encoded bold/underline sequences
            # (e.g. "b\bbo\bol\bld\bd") meant for a real terminal to render
            # -- `col -bx` is the standard tool for stripping that down to
            # plain readable text.
            stripped = subprocess.run(["col", "-bx"], input=result.stdout, capture_output=True, text=True, timeout=5)
            text = stripped.stdout
            source = "man page"
    except (subprocess.TimeoutExpired, FileNotFoundError):
        pass

    if not text.strip():
        # No man page installed for this command -- common for compiled
        # Rust/Go/single-binary tools that only document themselves via
        # --help. Try that before giving up.
        try:
            result = subprocess.run([command, "--help"], capture_output=True, text=True, timeout=10)
            text = (result.stdout or result.stderr).strip()
            source = "--help output"
        except (subprocess.TimeoutExpired, OSError) as exc:
            return {"ok": False, "error": f"No man page installed, and --help failed: {exc}"}

    if not text.strip():
        return {"ok": False, "error": f"No man page or --help output found for '{command}'."}

    truncated = len(text) > MAX_CHARS
    if truncated:
        text = text[:MAX_CHARS] + "\n\n...(truncated, the full page is longer than this)"

    log.info("Read %s for '%s' (%d chars%s).", source, command, len(text), ", truncated" if truncated else "")
    return {"ok": True, "text": text, "source": source, "truncated": truncated}
