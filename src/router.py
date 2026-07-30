"""
Decides which brain handles a request.

Fast path: obvious keyword heuristics (cheap, no model call).
Fallback: ask the small brain itself for a one-word classification -- still
fast, since it's the always-loaded model.
"""
from src.utils.logger import get_logger

log = get_logger("router")

BIG_PROJECT_HINTS = [
    "build a full", "build an entire", "write a complete", "refactor the whole",
    "create a full app", "create an entire", "take your time", "big project",
    "this is a big one", "use the big brain", "quality matters here",
]


def classify(user_text: str, small_brain) -> str:
    """Returns 'small' or 'large'."""
    lowered = user_text.lower()
    if any(hint in lowered for hint in BIG_PROJECT_HINTS):
        return "large"

    # Cheap length heuristic: very long, multi-requirement asks are usually projects.
    if len(user_text.split()) > 60:
        return "large"

    # Ambiguous -- let the small brain make a quick call rather than guessing wrong.
    try:
        result = small_brain.chat([
            {
                "role": "system",
                "content": (
                    "Reply with exactly one word: 'small' if this request is a quick "
                    "question or simple command, or 'large' if it's a substantial "
                    "project (e.g. building a full application) that deserves more "
                    "time and a higher-quality model. No other text."
                ),
            },
            {"role": "user", "content": user_text},
        ])
        decision = result["content"].strip().lower()
        return "large" if "large" in decision else "small"
    except Exception as exc:  # noqa: BLE001
        log.warning("Router classification failed (%s); defaulting to small.", exc)
        return "small"
