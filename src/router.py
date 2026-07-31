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

    # No word-count heuristic here on purpose: the large-brain path has zero
    # tool access (see agent_loop.py's "large" branch -- it's a single raw
    # text completion, no browser/file/desktop tools at all), so length alone
    # is a bad signal -- a long but ordinary multi-step tool-use request is
    # exactly the kind of thing that reads as "substantial" by word count
    # while still needing tools "large" doesn't have.

    # Ambiguous -- let the small brain make a quick call rather than guessing wrong.
    try:
        result = small_brain.chat([
            {
                "role": "system",
                "content": (
                    "Reply with exactly one word: 'small' or 'large'.\n\n"
                    "'large' is ONLY for self-contained generation work that needs no "
                    "tools -- e.g. writing a substantial chunk of code or a document "
                    "from scratch, in one shot, where nothing needs to be opened, "
                    "clicked, browsed, or read from the live system first.\n\n"
                    "'small' is for everything else, including multi-step requests, "
                    "as long as any step needs to interact with the actual computer: "
                    "opening or reading apps/websites/browser tabs, clicking, taking "
                    "screenshots, reading or writing files, running commands. The "
                    "large option has NO ability to do any of that -- if you pick "
                    "'large' for a task that needs it, the task will simply fail. "
                    "When genuinely unsure, prefer 'small'.\n\n"
                    "No other text in your reply, just the one word."
                ),
            },
            {"role": "user", "content": user_text},
        ])
        decision = result["content"].strip().lower()
        return "large" if "large" in decision else "small"
    except Exception as exc:  # noqa: BLE001
        log.warning("Router classification failed (%s); defaulting to small.", exc)
        return "small"
