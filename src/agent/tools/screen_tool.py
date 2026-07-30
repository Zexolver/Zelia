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


def take_screenshot() -> str:
    """Returns a path to a freshly captured PNG screenshot."""
    path = tempfile.mktemp(prefix="zelia_screenshot_", suffix=".png")
    session = _session_type()

    if session == "wayland" and _has("grim"):
        subprocess.run(["grim", path], check=True)
    elif _has("maim"):
        subprocess.run(["maim", path], check=True)
    elif _has("scrot"):
        subprocess.run(["scrot", path], check=True)
    elif _has("import"):  # ImageMagick fallback
        subprocess.run(["import", "-window", "root", path], check=True)
    else:
        raise RuntimeError(
            "No screenshot tool found. Install one of: maim, scrot, grim (Wayland), or imagemagick."
        )
    return path


def _has(binary: str) -> bool:
    return subprocess.run(["which", binary], capture_output=True).returncode == 0


def read_screen_text() -> dict:
    """OCR everything currently visible on screen."""
    path = take_screenshot()
    try:
        text = pytesseract.image_to_string(Image.open(path))
        return {"ok": True, "text": text.strip()}
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
