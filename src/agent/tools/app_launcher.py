"""
Everything needed for "show me X": find an installed app / file / URL that
matches what was asked for, bring it to the front if it's already running,
or launch it fresh if not. All native desktop tools (.desktop files,
gtk-launch, wmctrl, xdg-open) -- no MCP servers involved.
"""
import difflib
import glob
import os
import re
import subprocess
import configparser

from src.utils.logger import get_logger

log = get_logger("app_launcher")

DESKTOP_DIRS = [
    "/usr/share/applications",
    "/usr/local/share/applications",
    os.path.expanduser("~/.local/share/applications"),
    # Flatpak apps' .desktop files live here, not in the dirs above --
    # found via a real bug: Brave (Flatpak-only on this machine, no direct
    # PATH executable at all) was invisible to show_me/open_browser
    # entirely until this was added, not just the launch-mechanism issue
    # fixed alongside this in browser_control.py.
    "/var/lib/flatpak/exports/share/applications",
    os.path.expanduser("~/.local/share/flatpak/exports/share/applications"),
]


def _list_desktop_entries() -> dict:
    """Returns {display_name: desktop_id} for every installed app."""
    entries = {}
    for directory in DESKTOP_DIRS:
        for path in glob.glob(os.path.join(directory, "*.desktop")):
            parser = configparser.ConfigParser(interpolation=None, strict=False)
            try:
                parser.read(path, encoding="utf-8")
                section = parser["Desktop Entry"]
                if section.get("NoDisplay", "false").lower() == "true":
                    continue
                name = section.get("Name")
                if name:
                    entries[name] = os.path.splitext(os.path.basename(path))[0]
            except Exception:  # noqa: BLE001
                continue
    return entries


def list_apps() -> dict:
    return {"ok": True, "apps": sorted(_list_desktop_entries().keys())}


def _best_app_match(query: str):
    entries = _list_desktop_entries()
    if not entries:
        return None
    names = list(entries.keys())
    lowered_query = query.lower()

    # An app name appearing as a whole word inside the query is a much
    # stronger signal than difflib's whole-string similarity ratio, and
    # must be checked first -- confirmed live as a real bug: asking for
    # "the Kate text editor application" scored closer (by
    # get_close_matches' ratio, which penalizes length mismatch) to "KDE
    # Connect Indicator" than to "Kate" itself, launching the wrong app
    # entirely. Prefer the longest such match (e.g. "VS Code" over "Code"
    # if both were somehow candidates).
    word_matches = [
        name for name in names
        if re.search(rf"\b{re.escape(name.lower())}\b", lowered_query)
    ]
    if word_matches:
        best = max(word_matches, key=len)
        return best, entries[best]

    matches = difflib.get_close_matches(query, names, n=1, cutoff=0.4)
    if matches:
        best = matches[0]
        return best, entries[best]

    # try substring match as a looser fallback (either direction: a short
    # query inside a longer app name, or vice versa)
    for name in names:
        if lowered_query in name.lower() or name.lower() in lowered_query:
            return name, entries[name]
    return None


def _focus_existing_window(name: str) -> bool:
    if subprocess.run(["which", "wmctrl"], capture_output=True).returncode != 0:
        return False
    try:
        out = subprocess.check_output(["wmctrl", "-l"], text=True)
    except subprocess.CalledProcessError:
        return False
    for line in out.splitlines():
        if name.lower() in line.lower():
            window_id = line.split()[0]
            subprocess.run(["wmctrl", "-i", "-a", window_id])
            return True
    return False


def launch_or_focus_app(query: str) -> dict:
    match = _best_app_match(query)
    if not match:
        return {"ok": False, "error": f"No installed app matches '{query}'."}
    display_name, desktop_id = match

    if _focus_existing_window(display_name):
        log.info("Focused existing window for %s", display_name)
        return {"ok": True, "action": "focused", "app": display_name}

    log.info("Launching %s (%s)", display_name, desktop_id)
    if subprocess.run(["which", "gtk-launch"], capture_output=True).returncode == 0:
        subprocess.Popen(["gtk-launch", desktop_id])
    else:
        subprocess.Popen(["gio", "launch", f"/usr/share/applications/{desktop_id}.desktop"])
    return {"ok": True, "action": "launched", "app": display_name}


def open_path_or_url(target: str) -> dict:
    """Opens a file, folder, or URL with the system default handler."""
    try:
        subprocess.Popen(["xdg-open", target])
        return {"ok": True, "action": "opened", "target": target}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc)}


def show_me(target: str) -> dict:
    """
    High-level "show me X" handler:
      1. Try it as an installed application.
      2. Try it as a file/folder path.
      3. Fall back to treating it as a URL/web search.
    """
    app_result = launch_or_focus_app(target)
    if app_result.get("ok"):
        return app_result

    expanded = os.path.expanduser(target)
    if os.path.exists(expanded):
        return open_path_or_url(expanded)

    if target.startswith("http://") or target.startswith("https://") or "." in target.split()[0]:
        url = target if target.startswith("http") else f"https://{target}"
        return open_path_or_url(url)

    # Last resort: search the web for it.
    query = target.replace(" ", "+")
    return open_path_or_url(f"https://duckduckgo.com/?q={query}")
