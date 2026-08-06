"""
Decides which brain handles a request.

Keyword heuristics only -- no model call. Used to route a genuinely
different, model-based classification call to the small brain for
ambiguous cases (a short, tools-free system prompt asking for one word),
but that was found live to cause a real, continuous efficiency problem:
Ollama only keeps ONE cached prompt-prefix per loaded model, so that
extra call -- with its own completely different, shorter system prompt --
evicted whatever was cached from agent_loop.py's much larger system+
TOOL_SCHEMAS prefix, forcing the REAL tool-calling call right after it to
cold-prefill from scratch every single time. Since the ambiguous-
classification fallback fired for nearly every ordinary request (only
requests matching BIG_PROJECT_HINTS skip it), this meant almost every
request was paying two cold prefills back to back instead of one warm
one. Confirmed via the same reproduction method that caught the
num_ctx/context-overflow bug earlier this session (see CLAUDE.md) --
removing the extra call and keeping the tool-schema prefix stable across
every request is what actually lets Ollama's cache do its job.

Given up on purpose: the model-based fallback's own stated bias was
"when genuinely unsure, prefer small" anyway, and 'small' already has
full tool access (safe default -- see Known Issue #19, misrouting to
'large' is the worse failure mode since it has zero tools at all), so a
keyword-only router loses very little real routing accuracy for a
continuous, meaningful latency win on every request.
"""
BIG_PROJECT_HINTS = [
    "build a full", "build an entire", "write a complete", "refactor the whole",
    "create a full app", "create an entire", "take your time", "big project",
    "this is a big one", "use the big brain", "quality matters here",
]


def classify(user_text: str) -> str:
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
    return "small"
