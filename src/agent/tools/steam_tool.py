"""
Reads Steam's own local library manifests directly instead of
screenshotting the Steam window and OCRing it. Two reasons this is the
right approach here, not just a workaround:

1. Steam is CEF-based and doesn't expose an AT-SPI accessibility tree at
   all (confirmed by walking the AT-SPI desktop -- it simply isn't in the
   list of accessible applications), so screen-reading tools can't reliably
   see into it regardless of which method (OCR or accessibility API) is
   used.
2. Even where screen-reading works, this is just plain more accurate: these
   files are Steam's own source of truth for what's actually installed, not
   a visual guess from a rendered frame.
"""
import glob
import os
import re

from src.utils.logger import get_logger

log = get_logger("steam_tool")

DEFAULT_STEAM_DIRS = ["~/.local/share/Steam", "~/.steam/steam"]

# Steam's own tooling (compatibility layers, redistributables) lives in the
# same appmanifest_*.acf files as real games -- filter these out by name.
NON_GAME_PATTERNS = [
    r"^Proton\b",
    r"^Steam Linux Runtime\b",
    r"^Steamworks Common Redistributables$",
    r"^SteamVR$",
]


def _library_paths() -> list[str]:
    """Main Steam install dir plus every additional library folder listed
    in its libraryfolders.vdf (e.g. a game installed on a second drive)."""
    for base in DEFAULT_STEAM_DIRS:
        base = os.path.expanduser(base)
        vdf_path = os.path.join(base, "steamapps", "libraryfolders.vdf")
        if not os.path.exists(vdf_path):
            continue
        paths = [base]
        with open(vdf_path, encoding="utf-8", errors="replace") as f:
            content = f.read()
        for match in re.finditer(r'"path"\s+"([^"]+)"', content):
            lib_path = match.group(1).replace("\\\\", "\\")
            if lib_path not in paths:
                paths.append(lib_path)
        return paths
    return []


def list_installed_games() -> dict:
    """Reads every appmanifest_*.acf across all configured Steam library
    folders and returns the real, current list of installed games."""
    libraries = _library_paths()
    if not libraries:
        return {"ok": False, "error": "No Steam library found (checked ~/.local/share/Steam, ~/.steam/steam)."}

    games = []
    for lib in libraries:
        for path in glob.glob(os.path.join(lib, "steamapps", "appmanifest_*.acf")):
            try:
                with open(path, encoding="utf-8", errors="replace") as f:
                    content = f.read()
            except OSError:
                continue
            match = re.search(r'"name"\s+"([^"]+)"', content)
            if not match:
                continue
            name = match.group(1).strip()
            if any(re.search(p, name) for p in NON_GAME_PATTERNS):
                continue
            games.append(name)

    games.sort(key=str.lower)
    log.info(
        "Found %d installed Steam game(s) across %d librar%s",
        len(games), len(libraries), "y" if len(libraries) == 1 else "ies",
    )
    return {"ok": True, "games": games, "count": len(games)}
