"""
Lets the agent see the screen.

Two levels:
  - read_screen_text(): fast, cheap OCR (Tesseract) for "what does this say" /
    reading text off the screen. No GPU involved.
  - describe_screen(): optional, uses a small local vision-language model
    (served by Ollama, e.g. "moondream") for "what's on my screen" /
    "what am I looking at" -- loaded on demand, not resident, so it doesn't
    eat into the GPU budget the rest of the time.

Screenshotting shells out to whichever tool matches the session type
(X11 vs Wayland) rather than depending on a Python screenshot library, since
those are unreliable across compositors.
"""
import os
import subprocess
import tempfile

import pytesseract
from PIL import Image

from src.utils.logger import get_logger

log = get_logger("screen_tool")


def _session_type() -> str:
    return os.environ.get("XDG_SESSION_TYPE", "x11").lower()


def take_screenshot(active_window_only: bool = True) -> str:
    """Returns a path to a freshly captured PNG screenshot.

    Tries each candidate tool in order and actually runs it rather than
    stopping at the first one found installed -- grim needs the
    wlr-screencopy protocol, which only wlroots compositors (Sway,
    Hyprland) implement. It's simply *absent* on KWin (KDE) and Mutter
    (GNOME) Wayland, where grim exists on disk (if installed at all) but
    fails at runtime with "compositor doesn't support the screen capture
    protocol" -- found by actually testing screen reading live on a KDE
    Wayland session, not by reading the code. spectacle is KDE's own
    non-interactive screenshot tool and works there instead.

    active_window_only defaults to True: spectacle's -a captures just the
    focused window instead of the whole desktop (-f) -- on this project's
    reference machine (a 3-monitor, 4280x1920 virtual desktop), OCR'ing a
    full-desktop capture instead of the one relevant window measured at
    ~4.7s per read_screen_text() call, which multiplies badly for
    anything that reads the screen repeatedly (page_reader.py's
    scroll-and-read loop, browser_tabs.py's tab-cycling). grim has no
    per-window capture option, so this only narrows spectacle's scope --
    acceptable since grim already doesn't work on this project's actual
    reference compositor (KWin) anyway, per the note above."""
    path = tempfile.mktemp(prefix="zelia_screenshot_", suffix=".png")
    session = _session_type()

    if session == "wayland":
        spectacle_scope = "-a" if active_window_only else "-f"
        candidates = [["grim", path], ["spectacle", "-b", "-n", spectacle_scope, "-o", path]]
    else:
        candidates = [["maim", path], ["scrot", path], ["import", "-window", "root", path]]

    errors = []
    for cmd in candidates:
        if not _has(cmd[0]):
            continue
        try:
            subprocess.run(cmd, check=True, capture_output=True, timeout=10)
            if os.path.getsize(path) > 0:
                return path
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired, FileNotFoundError, OSError) as exc:
            errors.append(f"{cmd[0]}: {exc}")

    raise RuntimeError(
        "No working screenshot tool for this session. Tried: "
        + ("; ".join(errors) if errors else "nothing installed")
        + ". On KDE Wayland, install spectacle; on GNOME Wayland, gnome-screenshot "
          "isn't wired up here yet -- see CLAUDE.md."
    )


def _has(binary: str) -> bool:
    return subprocess.run(["which", binary], capture_output=True).returncode == 0


def read_screen_text() -> dict:
    """Reads whatever text is currently visible. Prefers AT-SPI -- the
    focused app's actual live accessibility tree, real text content rather
    than an image-to-text guess, and the same channel real screen-reader
    tech uses (not a screenshot, not bypassing the app). Falls back to OCR
    only for apps that don't expose one at all (most Electron/CEF apps,
    e.g. Steam -- confirmed absent from the AT-SPI desktop tree)."""
    from src.agent.tools import atspi_tool
    atspi_result = atspi_tool.read_focused_app()
    if atspi_result.get("ok"):
        return {"ok": True, "text": atspi_result["text"], "source": "atspi", "app": atspi_result.get("app")}

    path = take_screenshot()
    try:
        text = pytesseract.image_to_string(Image.open(path))
        return {"ok": True, "text": text.strip(), "source": "ocr"}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc)}
    finally:
        os.remove(path)


def describe_screen(question: str, vision_model: str, ollama_host: str) -> dict:
    """Ask a small local vision model to describe/answer about the screen."""
    import base64
    import ollama

    path = take_screenshot()
    try:
        with open(path, "rb") as f:
            image_b64 = base64.b64encode(f.read()).decode()
        client = ollama.Client(host=ollama_host)
        response = client.chat(
            model=vision_model,
            messages=[{
                "role": "user",
                "content": question or "Briefly describe what's on this screen.",
                "images": [image_b64],
            }],
        )
        return {"ok": True, "description": response["message"]["content"]}
    except Exception as exc:  # noqa: BLE001
        log.warning("Vision model call failed: %s", exc)
        return {"ok": False, "error": str(exc)}
    finally:
        os.remove(path)
