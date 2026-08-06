"""
System volume control -- "turn the volume up/down", "mute", "what's the
volume at". Uses wpctl (WirePlumber's own CLI -- confirmed this machine's
actual audio stack, PipeWire) as the primary path, falling back to pactl
(PulseAudio-compat, also present) if wpctl isn't installed -- same
"detect what's actually there" pattern as everywhere else in this project
(gpu_detect.py, desktop_control._find_terminal, etc), not hardcoded to
one specific audio server/desktop.
"""
import re
import shutil
import subprocess

from src.utils.logger import get_logger

log = get_logger("volume_tool")

_WPCTL_SINK = "@DEFAULT_AUDIO_SINK@"
_PACTL_SINK = "@DEFAULT_SINK@"
_NO_TOOL_ERROR = "No volume control tool available (wpctl/pactl not installed)."


def _has(binary: str) -> bool:
    return shutil.which(binary) is not None


def get_volume() -> dict:
    if _has("wpctl"):
        try:
            result = subprocess.run(["wpctl", "get-volume", _WPCTL_SINK], capture_output=True, text=True, timeout=5)
            match = re.search(r"([\d.]+)", result.stdout)
            if match:
                percent = round(float(match.group(1)) * 100)
                return {"ok": True, "volume_percent": percent, "muted": "MUTED" in result.stdout}
        except subprocess.TimeoutExpired as exc:
            return {"ok": False, "error": str(exc)}
    if _has("pactl"):
        try:
            result = subprocess.run(["pactl", "get-sink-volume", _PACTL_SINK], capture_output=True, text=True, timeout=5)
            match = re.search(r"(\d+)%", result.stdout)
            if match:
                return {"ok": True, "volume_percent": int(match.group(1))}
        except subprocess.TimeoutExpired as exc:
            return {"ok": False, "error": str(exc)}
    return {"ok": False, "error": _NO_TOOL_ERROR}


def set_volume(percent: int) -> dict:
    # wpctl will happily accept over 100% (amplified boost) -- cap to
    # something sane so a misheard/misparsed number can't blast the audio.
    percent = max(0, min(150, percent))
    if _has("wpctl"):
        try:
            subprocess.run(["wpctl", "set-volume", _WPCTL_SINK, f"{percent}%"], check=True, timeout=5)
            log.info("Set volume to %d%%", percent)
            return {"ok": True, "volume_percent": percent}
        except (subprocess.TimeoutExpired, subprocess.CalledProcessError) as exc:
            return {"ok": False, "error": str(exc)}
    if _has("pactl"):
        try:
            subprocess.run(["pactl", "set-sink-volume", _PACTL_SINK, f"{percent}%"], check=True, timeout=5)
            log.info("Set volume to %d%% (via pactl)", percent)
            return {"ok": True, "volume_percent": percent}
        except (subprocess.TimeoutExpired, subprocess.CalledProcessError) as exc:
            return {"ok": False, "error": str(exc)}
    return {"ok": False, "error": _NO_TOOL_ERROR}


def set_mute(muted: bool) -> dict:
    value = "1" if muted else "0"
    if _has("wpctl"):
        try:
            subprocess.run(["wpctl", "set-mute", _WPCTL_SINK, value], check=True, timeout=5)
            log.info("Set mute=%s", muted)
            return {"ok": True, "muted": muted}
        except (subprocess.TimeoutExpired, subprocess.CalledProcessError) as exc:
            return {"ok": False, "error": str(exc)}
    if _has("pactl"):
        try:
            subprocess.run(["pactl", "set-sink-mute", _PACTL_SINK, value], check=True, timeout=5)
            log.info("Set mute=%s (via pactl)", muted)
            return {"ok": True, "muted": muted}
        except (subprocess.TimeoutExpired, subprocess.CalledProcessError) as exc:
            return {"ok": False, "error": str(exc)}
    return {"ok": False, "error": _NO_TOOL_ERROR}
