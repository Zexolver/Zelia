"""
Ties everything together: takes a transcribed user request, routes it to the
right brain, runs the tool-calling loop, handles "are you sure?" confirmations
out loud, and hands the final answer back to be spoken + remembered.
"""
import json
import re

from src.agent.tools.shell_tool import run_shell, is_destructive
from src.agent.tools.file_tool import FileTool
from src.agent.tools.browser_tool import fetch_url
from src.agent.tools.code_tool import CodeTool
from src.agent.tools import screen_tool, app_launcher, desktop_control, browser_control, steam_tool, browser_tabs, page_reader, cdp_reader, self_source_tool
from src.agent.tools import clipboard_tool, volume_tool, brightness_tool, notify_tool, power_tool, timer_tool
from src.agent.tools import man_tool, tui_tool, atspi_tool
from src.router import classify
from src import idle_detect
from src.utils.logger import get_logger

log = get_logger("agent_loop")

# Tools that actually inject synthetic input into whatever's currently
# focused on the real, visible desktop -- these are the ones that can
# compete with the user for control of their own screen while they're
# actively using it (gaming, typing, etc). Opening/focusing an app
# (show_me, open_browser, focus_window) is deliberately NOT in this set
# -- those already go through preserve_focus_if_user_active, which is a
# much lighter touch (restores the user's focus right after), not a
# sustained interaction the way a click+type sequence is.
INPUT_INJECTING_TOOLS = {"click_at", "type_text", "press_key", "atspi_click"}

TOOL_SCHEMAS = [
    {
        "type": "function",
        "function": {
            "name": "run_in_terminal",
            "description": "Run a shell command (git, cd, builds, etc.) in a NEW VISIBLE terminal window so the user can watch it happen in real time. This is the default way to run commands -- use this unless the user specifically asked for something quiet/background.",
            "parameters": {
                "type": "object",
                "properties": {"command": {"type": "string"}},
                "required": ["command"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "run_in_vtty",
            "description": "Run a shell command on a completely separate real Linux virtual terminal (not the graphical desktop at all) -- for when the user is actively using the computer (gaming, etc.) and offered this as an alternative to a visible terminal window so ZELIA doesn't compete for their screen/focus. Only offer/use this after the user has been asked and picked it over waiting. Not visible unless the user manually switches virtual terminals themselves; report the result back from what this returns.",
            "parameters": {
                "type": "object",
                "properties": {"command": {"type": "string"}},
                "required": ["command"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "run_shell_quiet",
            "description": "Run a shell command hidden in the background, with no visible terminal. ONLY use this when the user has explicitly said to run something quietly/in the background -- otherwise use run_in_terminal.",
            "parameters": {
                "type": "object",
                "properties": {"command": {"type": "string"}},
                "required": ["command"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "Read a file inside ZELIA's workspace.",
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string"}},
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_own_source",
            "description": "List files/directories in ZELIA's own source code (the src/ directory of the code actually running right now), for self-diagnosis -- e.g. 'why can't you do X', 'what tools do you actually have'. Read-only, and separate from the user's own project files (list_dir/read_file, which are scoped to the workspace instead).",
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string", "description": "Path relative to src/, default '.' for the top level."}},
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_own_source",
            "description": "Read one of ZELIA's own source files (path relative to src/, e.g. 'agent/agent_loop.py') to understand her own actual behavior/capabilities. Read-only -- she cannot modify her own running code this way.",
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string"}},
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "write_file",
            "description": "Create or overwrite a file inside ZELIA's workspace.",
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string"}, "content": {"type": "string"}},
                "required": ["path", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "delete_file",
            "description": "Delete a file inside ZELIA's workspace.",
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string"}},
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "fetch_url",
            "description": "Fetch the readable text content of a web page (silent HTTP request, used for reading documentation/content -- not a visible action, pair with open_browser if the user should see the page too).",
            "parameters": {
                "type": "object",
                "properties": {"url": {"type": "string"}},
                "required": ["url"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "run_code",
            "description": "Run a command (e.g. 'python3 script.py') inside ZELIA's workspace directory. Runs in a visible terminal by default; pass quiet=true only if asked to run it in the background.",
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {"type": "string"},
                    "quiet": {"type": "boolean"},
                },
                "required": ["command"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_screen_text",
            "description": "Read all text currently visible on the user's screen via OCR. Use for 'what does this say', 'read this', reading errors/labels/etc.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_all_browser_tabs",
            "description": "Reads every open tab in a browser window, by actually cycling through them with Ctrl+Tab and reading each one (stops automatically when it cycles back to the start). Use this specifically for 'what tabs do I have open', 'read all my tabs', or checking content across multiple open tabs -- for a single visible tab/page, use read_screen_text instead, it's faster. Pass 'browser' (e.g. 'brave', 'floorp') so it can focus the right window itself first -- don't rely on it already being focused.",
            "parameters": {
                "type": "object",
                "properties": {"browser": {"type": "string", "description": "Which browser's window to focus first, e.g. 'brave' or 'floorp'."}},
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_full_page",
            "description": "Reads an ENTIRE long page or conversation by scrolling down through it (Page Down) and reading each screen's worth of text, stopping automatically once scrolling no longer reveals new content. Use this whenever asked to read/summarize/describe the FULL content of something that could extend beyond one screen -- a chat conversation (Gemini, Claude.ai), a long article, a scrollable document. read_screen_text only sees whatever's currently visible; for anything that might be longer than one screen, use this instead or you'll only see -- and summarize -- the first screenful. Pass 'browser' so it can focus the right window itself first. NOTE: for a tab open in Brave specifically, prefer read_brave_tab instead -- it's more accurate (reads the page's real content directly, not a screenshot guess) and doesn't need to move the mouse/keyboard at all.",
            "parameters": {
                "type": "object",
                "properties": {"browser": {"type": "string", "description": "Which browser's window to focus first, e.g. 'brave' or 'floorp'."}},
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_brave_tab",
            "description": "Reads a Brave tab's ENTIRE real content directly (not a screenshot/OCR guess, and not limited to what's currently scrolled into view) by talking to Brave's own remote-debugging protocol. Use this instead of read_full_page/read_screen_text whenever the tab you need to read is open in Brave specifically (Gemini chats, Claude.ai chats, articles, anything) -- it's more accurate and doesn't need to move the mouse/keyboard at all. Only works if Brave was launched with remote debugging enabled (open_browser does this automatically for Brave now) -- if it wasn't (e.g. Brave was already running from before), this will fail with a clear error explaining that Brave needs a full restart; fall back to read_full_page for that one request rather than guessing.",
            "parameters": {
                "type": "object",
                "properties": {"hint": {"type": "string", "description": "Text to match against the tab's title or URL, e.g. 'gemini', 'claude.ai'."}},
                "required": ["hint"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_brave_tabs",
            "description": "Lists titles/URLs of every tab currently open in Brave, via its remote-debugging protocol -- lighter-weight than read_brave_tab/read_all_browser_tabs since it doesn't read each tab's full content, just what's open. Use for 'what tabs do I have open' when Brave is the browser in question.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "click_brave_element",
            "description": "Clicks whatever on a Brave tab's page contains the given text -- e.g. selecting one specific past conversation out of a sidebar list of chats (Gemini, Claude.ai). Use this whenever the user asks to open/pick/switch to a SPECIFIC existing chat/item by name/topic, instead of guessing with click_at or just opening the site fresh (which lands on a blank new chat, not the one they meant). No synthetic mouse input -- talks to Brave's remote-debugging protocol directly, same as read_brave_tab. Only works if Brave has remote debugging enabled (see read_brave_tab's notes) and the target text is actually somewhere on the currently loaded page (e.g. visible in an open chat-history sidebar) -- if you don't know the exact title of the chat the user means, read_brave_tab first to find it, then click it by that exact (or a substring of that) title.",
            "parameters": {
                "type": "object",
                "properties": {
                    "hint": {"type": "string", "description": "Text to match against the tab's title or URL, e.g. 'gemini', 'claude.ai'."},
                    "text": {"type": "string", "description": "Text to find and click on the page, e.g. the title of a specific past chat."},
                },
                "required": ["hint", "text"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "describe_screen",
            "description": "Look at the user's screen with a vision model and answer a question about what's visible (layout, images, what an app looks like). Slower than read_screen_text -- use that instead for pure text reading.",
            "parameters": {
                "type": "object",
                "properties": {"question": {"type": "string"}},
                "required": ["question"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "show_me",
            "description": "Handles 'show me X' / 'open X' / 'pull up X' requests: launches or focuses an installed application (including Godot), opens a file/folder, or opens a URL/web search -- whichever matches best.",
            "parameters": {
                "type": "object",
                "properties": {"target": {"type": "string"}},
                "required": ["target"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_apps",
            "description": "List installed applications on the user's machine.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_installed_steam_games",
            "description": "Cross-check/fallback source for the user's installed Steam games, read directly from Steam's local library files. Steam's library list is long and scrollable and doesn't expose an accessibility tree, so OCR alone can miss entries or misread names -- look at the actual Steam window first (open_browser/show_me + read_screen_text/describe_screen, scrolling as needed) like you normally would, then use this to verify or fill in what you saw, especially for an exact count or an exact game name. Don't use this as a substitute for looking when the user just wants you to check something visible on screen in general -- it's specific to Steam's install list being unusually unreliable to fully OCR.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "open_browser",
            "description": "Open a URL in a real, visible browser window. Uses the configured default browser unless `browser` is given. If the browser is already running, this opens a new TAB in the existing window by default -- pass new_window=true if the user specifically asked for a new WINDOW. Never use press_key/ctrl+tab to try to open a new window, that cycles between existing tabs instead. NEVER invent or guess a URL you don't actually know, e.g. a specific chat/conversation's exact address -- only pass a URL you have real reason to believe is correct (a well-known site's homepage/root URL, a URL the user gave you directly, or one you actually read from a tool result). If asked to open a SPECIFIC existing item (a particular past chat, a particular page in an app) that you don't have a real URL for, that's not this tool's job -- open the site's plain root URL here if needed, then use read_brave_tab to see what's actually there and click_brave_element to select the specific thing, instead of fabricating a URL.",
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {"type": "string"}, "browser": {"type": "string"},
                    "new_window": {"type": "boolean", "description": "True only if the user specifically asked for a new window, not just a new tab/page."},
                },
                "required": ["url"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "set_browser_for_now",
            "description": "Use a different browser than the default, for this session only (e.g. user says 'use Chromium for this').",
            "parameters": {
                "type": "object",
                "properties": {"browser": {"type": "string"}},
                "required": ["browser"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "set_default_browser",
            "description": "Permanently change the default browser (e.g. user says 'always use Firefox from now on'). Persists to config.",
            "parameters": {
                "type": "object",
                "properties": {"browser": {"type": "string"}},
                "required": ["browser"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "type_text",
            "description": "Type text into whatever window currently has keyboard focus (e.g. after opening a browser or app). Works on both Xorg and Wayland. If the user is actively using the computer, this is blocked unless 'confirmed' is set (after they've said to go ahead anyway).",
            "parameters": {
                "type": "object",
                "properties": {
                    "text": {"type": "string"},
                    "confirmed": {"type": "boolean", "description": "Set true only after the user was asked about interrupting their active use and said to proceed."},
                },
                "required": ["text"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "press_key",
            "description": "Send a key combo to the focused window, e.g. 'ctrl+l' (address bar), 'ctrl+t' (new tab), 'Return', 'Tab', 'Escape'. If the user is actively using the computer, this is blocked unless 'confirmed' is set (after they've said to go ahead anyway).",
            "parameters": {
                "type": "object",
                "properties": {
                    "combo": {"type": "string"},
                    "confirmed": {"type": "boolean", "description": "Set true only after the user was asked about interrupting their active use and said to proceed."},
                },
                "required": ["combo"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "find_text_on_screen",
            "description": "OCRs the screen to find the (x, y) location of visible text, so you can click on it. Use before click_at when you need to click something by its on-screen label (a button, a link, a menu item).",
            "parameters": {
                "type": "object",
                "properties": {"text": {"type": "string"}},
                "required": ["text"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "click_at",
            "description": "Click at specific screen coordinates. Usually preceded by find_text_on_screen to locate what to click. If the user is actively using the computer, this is blocked unless 'confirmed' is set (after they've said to go ahead anyway).",
            "parameters": {
                "type": "object",
                "properties": {
                    "x": {"type": "integer"}, "y": {"type": "integer"},
                    "confirmed": {"type": "boolean", "description": "Set true only after the user was asked about interrupting their active use and said to proceed."},
                },
                "required": ["x", "y"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "focus_window",
            "description": "Bring a window matching this name to the front. Best-effort on Wayland depending on the desktop compositor.",
            "parameters": {
                "type": "object",
                "properties": {"name": {"type": "string"}},
                "required": ["name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "clipboard",
            "description": "Reads or writes the desktop clipboard. action='read' for 'what's on my clipboard'; action='write' with text for 'copy this for me'.",
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {"type": "string", "enum": ["read", "write"]},
                    "text": {"type": "string", "description": "Required for action='write'."},
                },
                "required": ["action"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "volume",
            "description": "Gets or sets system volume/mute. action='get' reports level+muted state. action='set' needs percent (0-100, or slightly over for boosted volume) -- for a relative change ('turn it up a bit'), get first, then set with the adjusted number. action='mute'/'unmute' toggles audio.",
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {"type": "string", "enum": ["get", "set", "mute", "unmute"]},
                    "percent": {"type": "integer", "description": "Required for action='set'."},
                },
                "required": ["action"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "brightness",
            "description": "Gets or sets screen brightness (0-100), if this machine has a controllable display backlight -- may not be available on a desktop with external monitors, report the error plainly if so.",
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {"type": "string", "enum": ["get", "set"]},
                    "percent": {"type": "integer", "description": "Required for action='set'."},
                },
                "required": ["action"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "send_notification",
            "description": "Sends a real desktop notification popup with a title and message. Use when something is worth surfacing visually without necessarily interrupting the user out loud.",
            "parameters": {
                "type": "object",
                "properties": {"title": {"type": "string"}, "message": {"type": "string"}},
                "required": ["title"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "power",
            "description": "Locks the screen, or shuts down/restarts/suspends the computer. action='lock' is reversible/low-risk -- just do it, no confirmation needed. action='shutdown'/'restart'/'suspend' ends the session or interrupts whatever's running -- always confirm with the user first unless they've already explicitly confirmed in this same request.",
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {"type": "string", "enum": ["lock", "shutdown", "restart", "suspend"]},
                    "confirmed": {"type": "boolean", "description": "Set true only after the user has explicitly confirmed a shutdown/restart/suspend."},
                },
                "required": ["action"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "timer",
            "description": "Sets, cancels, or lists timers/reminders. action='set' needs seconds (convert whatever duration the user said into seconds yourself) and message (what to announce when it fires, spoken + a desktop notification). action='cancel' needs timer_id (from a previous 'set'). action='list' reports active timer ids.",
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {"type": "string", "enum": ["set", "cancel", "list"]},
                    "seconds": {"type": "number", "description": "Required for action='set'."},
                    "message": {"type": "string", "description": "Required for action='set'."},
                    "timer_id": {"type": "string", "description": "Required for action='cancel'."},
                },
                "required": ["action"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_man_page",
            "description": "Reads the local man page (or --help output as a fallback) for an installed CLI command -- use this to check a command's REAL, actual flags/usage on THIS machine before running an unfamiliar or complex command, instead of guessing from what you already know (which may be outdated or wrong for the version actually installed here).",
            "parameters": {
                "type": "object",
                "properties": {"command": {"type": "string"}},
                "required": ["command"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "atspi_click",
            "description": "Clicks a button/control in the currently-focused app by its accessible name, via the accessibility tree directly -- no screenshot, no OCR, no coordinate guessing. Try this FIRST for clicking something in a native Qt/GTK app (most system apps, e.g. Dolphin, Kate, system settings) -- it's far more precise than find_text_on_screen + click_at. Falls back automatically with a clear error if the focused app doesn't expose AT-SPI at all (common for Electron/CEF apps like Steam/Discord/Brave) or no matching control is found -- use find_text_on_screen + click_at for those instead.",
            "parameters": {
                "type": "object",
                "properties": {"name": {"type": "string", "description": "The control's visible label/name, e.g. 'Save', 'OK', 'Settings'."}},
                "required": ["name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "tui_session",
            "description": "Drives an interactive terminal (TUI) tool -- htop, vim, a REPL, an ncurses installer menu, anything that takes over the terminal and needs ongoing keystrokes rather than just printing output and exiting. action='start' needs command (and optionally location: 'desktop' opens a real visible terminal window (default); 'background' attaches no viewer at all -- use this when asked to run something without disturbing the user, e.g. while they're gaming) -- returns a session_id. action='send_keys' needs session_id+keys (set enter=false for single keypresses like 'q' or arrow keys). action='read_screen' needs session_id -- check this before deciding what to send next. action='stop' needs session_id. action='list' shows all running session ids.",
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {"type": "string", "enum": ["start", "send_keys", "read_screen", "stop", "list"]},
                    "command": {"type": "string", "description": "Required for action='start', e.g. 'htop' or 'vim notes.txt'."},
                    "location": {"type": "string", "enum": ["desktop", "background"], "description": "Optional, for action='start'."},
                    "session_id": {"type": "string", "description": "Required for action='send_keys'/'read_screen'/'stop'."},
                    "keys": {"type": "string", "description": "Required for action='send_keys'."},
                    "enter": {"type": "boolean", "description": "For action='send_keys' -- default true."},
                },
                "required": ["action"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "add_idle_task",
            "description": "Queue a low-priority background task to work on later, only once the system is genuinely idle (no keyboard/mouse activity, no game running, no Gemini/Claude Code CLI active, and no active request already in progress). Use this when the user says something like 'when you have nothing else to do', 'in the background', 'whenever I'm not using the computer', or explicitly asks you to remember a task for idle time -- not for anything they want done now. The task description should be a clear, self-contained instruction, since it'll be run on its own later with no other context from this conversation.",
            "parameters": {
                "type": "object",
                "properties": {"description": {"type": "string", "description": "Clear, self-contained instruction for the idle task."}},
                "required": ["description"],
            },
        },
    },
]

MAX_TOOL_ROUNDS = 10

# Tools whose result depends only on their arguments (reading/checking
# state, never causing an action) -- safe to short-circuit on an exact
# repeat instead of re-running. Confirmed live: the small brain called
# list_installed_steam_games with identical (empty) arguments 10 times in
# a row, burning through the entire tool-round budget instead of just
# answering from the first result. Deliberately excludes anything with a
# side effect (run_shell, write_file, click_at, open_browser, ...) --
# repeating one of those might be exactly what's intended (e.g. "check
# again"), so only genuinely read-only tools get deduplicated.
IDEMPOTENT_TOOLS = {
    "read_file", "list_own_source", "read_own_source", "list_apps",
    "list_installed_steam_games", "read_all_browser_tabs", "read_full_page",
    "read_brave_tab", "list_brave_tabs", "read_screen_text", "describe_screen",
    "find_text_on_screen", "fetch_url", "read_man_page",
    # clipboard/volume/brightness/timer/tui_session are single tool names
    # covering both read and write actions -- deliberately NOT idempotent-
    # cached (the cache key is (name, args), so a repeated action='set'
    # call would wrongly be treated as a no-op repeat of a real command).
    # These are all cheap/fast operations anyway, so losing the repeat-
    # dedup benefit here doesn't reintroduce the original runaway-expensive-
    # rescan problem this mechanism exists for.
}

_LEAKED_CALL_RE = re.compile(
    r'\b(' + "|".join(re.escape(t["function"]["name"]) for t in TOOL_SCHEMAS) + r')\s*(\{.*?\})',
    re.DOTALL,
)

# Second, distinct leaked-call shape found live 2026-08-06: instead of
# `toolname {"arg": "val"}` (what _LEAKED_CALL_RE above catches), the model
# sometimes emits a fenced ```json { "name": "...", "arguments": {...} } ```
# block -- an OpenAI-function-call-style JSON *object* with "name" as a key
# rather than a bare prefix, which the first regex never matches at all (no
# "toolname{" substring exists in this shape). Parsed by trying json.loads()
# on each fenced block rather than a single regex, since matching balanced
# nested braces with regex is fragile -- this only needs "does this parse,
# and does it look like a tool call," not general JSON extraction.
_FENCED_JSON_RE = re.compile(r'```(?:json)?\s*(\{.*?\})\s*```', re.DOTALL)


def _find_leaked_tool_calls(content: str) -> list[tuple[str, dict]]:
    """Small-model replies sometimes contain what looks like a tool call as
    plain text instead of a real structured tool_calls response (e.g.
    `run_in_terminal {"command": "..."}` appearing in .content) -- found
    repeatedly in live testing, including one case where the leaked call
    was the ONLY record of an action the model described taking, so the
    actual file write never happened even though the reply confidently
    said it did. Extracts any of these so the caller can actually execute
    them instead of just displaying/speaking the raw syntax as if it were
    the final answer."""
    found = []
    for match in _LEAKED_CALL_RE.finditer(content):
        name, raw_args = match.group(1), match.group(2)
        try:
            args = json.loads(raw_args)
        except json.JSONDecodeError:
            continue
        found.append((name, args))

    known_names = {t["function"]["name"] for t in TOOL_SCHEMAS}
    for match in _FENCED_JSON_RE.finditer(content):
        try:
            obj = json.loads(match.group(1))
        except json.JSONDecodeError:
            continue
        name, args = obj.get("name"), obj.get("arguments")
        if name in known_names and isinstance(args, dict):
            found.append((name, args))
    return found


SCREEN_VISIBILITY_TOOLS = {
    "read_screen_text", "describe_screen", "find_text_on_screen", "click_at",
    "read_all_browser_tabs", "read_full_page", "read_brave_tab", "list_brave_tabs",
    "click_brave_element", "atspi_click",
}


class AgentLoop:
    def __init__(self, small_brain, large_brain, second_brain, workspace_dir: str, ask_confirmation,
                 vision_model: str = "moondream", ollama_host: str = "http://127.0.0.1:11434",
                 default_browser: str = "floorp", config_path: str = "", ask_for_password=None,
                 game_guard=None, idle_task_runner=None, announce=None):
        self.small_brain = small_brain
        self.large_brain = large_brain
        self.second_brain = second_brain
        self.files = FileTool(workspace_dir)
        self.code = CodeTool(workspace_dir)
        self.ask_confirmation = ask_confirmation  # fn(question: str) -> bool, spoken yes/no
        self.ask_for_password = ask_for_password  # fn(question: str) -> str, spoken, never logged; None if unsupported (e.g. text-only session)
        self.vision_model = vision_model
        self.ollama_host = ollama_host
        self.default_browser = default_browser
        self.config_path = config_path
        self.game_guard = game_guard  # src.game_guard.GameGuard instance; None disables the gaming half of the busy-check
        self.idle_task_runner = idle_task_runner  # src.idle_tasks.IdleTaskRunner instance; None means add_idle_task reports unavailable
        self.announce = announce  # fn(text) -> None, speaks unprompted (e.g. main.py's safe_speak) -- used by set_timer to announce when a timer fires, independent of any request's original speak() callback (which is long gone by the time a timer actually goes off)

    def _ensure_unlocked_for_screen_access(self) -> dict | None:
        """Called before any tool that needs the real screen visible (not a
        locked/blank frame). Returns None if the screen's already unlocked
        (or lock state can't be determined) -- proceed as normal. Returns an
        error dict if it's locked and couldn't be unlocked, in which case the
        caller should use that as the tool result instead of running the
        real tool against a blank/locked screen."""
        desktop_control.inhibit_idle_briefly()
        if not desktop_control.is_screen_locked():
            return None
        if self.ask_for_password is None:
            return {"ok": False, "error": "Screen is locked and no way to ask for the password is available right now (text-only session)."}
        password = self.ask_for_password("The screen is locked -- what's the password?")
        if not password:
            return {"ok": False, "error": "Screen is locked and no password was given."}
        result = desktop_control.unlock_screen_with_password(password)
        if not result.get("ok"):
            return {"ok": False, "error": "Screen is locked and the unlock attempt failed."}
        return None

    def _user_busy(self) -> bool:
        """True if the user appears to be actively at the keyboard/mouse,
        or a game/heavy foreground app is running (src/idle_detect.py,
        src/game_guard.py) -- the signal for whether an input-injecting
        tool would be fighting the user for control of their own screen
        right now."""
        if idle_detect.is_user_active():
            return True
        if self.game_guard is not None and self.game_guard.is_gaming():
            return True
        return False

    def _busy_gate(self, name: str, args: dict) -> dict | None:
        """Returns a blocking result if `name` needs the real desktop and
        the user is busy and hasn't already said to go ahead anyway
        (args['confirmed']); None if it's fine to proceed. Only gates
        tools that either sustain visible interaction (click/type) or pop
        a persistent window (a visible terminal) -- see
        INPUT_INJECTING_TOOLS's comment for why opening/focusing an app
        isn't included."""
        if args.get("confirmed"):
            return None
        if name in INPUT_INJECTING_TOOLS:
            if self._user_busy():
                return {
                    "ok": False,
                    "user_currently_active": True,
                    "note": (
                        "The user appears to be actively using the computer (typing/clicking, "
                        "or a game running) right now. Don't retry this automatically -- ask them "
                        "out loud whether you should wait until they're idle, or go ahead anyway. "
                        "Only retry this exact tool call with confirmed=true if they say to proceed."
                    ),
                }
        elif name == "run_in_terminal":
            if self._user_busy():
                return {
                    "ok": False,
                    "user_currently_active": True,
                    "note": (
                        "The user appears to be actively using the computer (typing/clicking, or a "
                        "game running) right now, and a visible terminal window would compete for "
                        "their screen. Ask them out loud: wait until they're idle, run it via "
                        "run_in_vtty instead (a separate real virtual terminal, invisible unless "
                        "they switch to it, doesn't touch the graphical session at all), or go "
                        "ahead anyway with a visible terminal. Only retry run_in_terminal itself "
                        "with confirmed=true if they specifically choose the visible-terminal option."
                    ),
                }
        return None

    def _dispatch_tool(self, name: str, args: dict) -> dict:
        if name in SCREEN_VISIBILITY_TOOLS:
            lock_error = self._ensure_unlocked_for_screen_access()
            if lock_error is not None:
                return lock_error
        busy_block = self._busy_gate(name, args)
        if busy_block is not None:
            return busy_block
        if name == "run_in_terminal":
            command = args.get("command", "")
            if is_destructive(command) and not args.get("confirmed"):
                result = {"needs_confirmation": True, "command": command}
            else:
                result = desktop_control.preserve_focus_if_user_active(
                    lambda: desktop_control.open_terminal(command=command, cwd=self.files.workspace_dir)
                )
        elif name == "run_in_vtty":
            result = desktop_control.run_in_vtty(command=args.get("command", ""), cwd=self.files.workspace_dir)
        elif name == "run_shell_quiet":
            args.pop("quiet", None)  # tool name already implies this; small models sometimes add it anyway
            result = run_shell(cwd=self.files.workspace_dir, **args)
        elif name == "read_file":
            result = self.files.read(**args)
        elif name == "list_own_source":
            result = self_source_tool.list_own_source(**args)
        elif name == "read_own_source":
            result = self_source_tool.read_own_source(**args)
        elif name == "write_file":
            result = self.files.write(**args)
        elif name == "delete_file":
            result = self.files.delete(**args)
        elif name == "fetch_url":
            result = fetch_url(**args)
        elif name == "run_code":
            result = self.code.run(**args)
        elif name == "read_screen_text":
            result = screen_tool.read_screen_text()
        elif name == "read_all_browser_tabs":
            result = browser_tabs.read_all_tabs(browser=args.get("browser", ""))
        elif name == "read_full_page":
            result = page_reader.read_full_page(browser=args.get("browser", ""))
        elif name == "read_brave_tab":
            result = cdp_reader.read_tab(args.get("hint", ""))
        elif name == "list_brave_tabs":
            result = cdp_reader.list_tab_titles()
        elif name == "click_brave_element":
            result = cdp_reader.click_text(args.get("hint", ""), args.get("text", ""))
        elif name == "describe_screen":
            result = screen_tool.describe_screen(args.get("question", ""), self.vision_model, self.ollama_host)
        elif name == "show_me":
            result = desktop_control.preserve_focus_if_user_active(lambda: app_launcher.show_me(**args))
        elif name == "list_apps":
            result = app_launcher.list_apps()
        elif name == "list_installed_steam_games":
            result = steam_tool.list_installed_games()
        elif name == "open_browser":
            result = desktop_control.preserve_focus_if_user_active(
                lambda: browser_control.open_browser(default_browser=self.default_browser, **args)
            )
        elif name == "set_browser_for_now":
            result = browser_control.set_browser_for_now(**args)
        elif name == "set_default_browser":
            result = browser_control.set_default_browser(config_path=self.config_path, **args)
        elif name == "type_text":
            args.pop("confirmed", None)
            result = desktop_control.type_text(**args)
        elif name == "press_key":
            args.pop("confirmed", None)
            result = desktop_control.press_key(**args)
        elif name == "find_text_on_screen":
            result = desktop_control.find_text_on_screen(**args)
        elif name == "click_at":
            args.pop("confirmed", None)
            result = desktop_control.click_at(**args)
        elif name == "focus_window":
            result = desktop_control.preserve_focus_if_user_active(lambda: desktop_control.focus_window(**args))
        elif name == "add_idle_task":
            if self.idle_task_runner is None:
                result = {"ok": False, "error": "Idle task queue isn't available right now."}
            else:
                position = self.idle_task_runner.add(args.get("description", ""))
                result = {"ok": True, "queued_position": position}
        elif name == "clipboard":
            action = args.get("action", "")
            if action == "write":
                result = clipboard_tool.write_clipboard(args.get("text", ""))
            else:
                result = clipboard_tool.read_clipboard()
        elif name == "volume":
            action = args.get("action", "")
            if action == "set":
                result = volume_tool.set_volume(args.get("percent", 50))
            elif action == "mute":
                result = volume_tool.set_mute(True)
            elif action == "unmute":
                result = volume_tool.set_mute(False)
            else:
                result = volume_tool.get_volume()
        elif name == "brightness":
            if args.get("action") == "set":
                result = brightness_tool.set_brightness(args.get("percent", 50))
            else:
                result = brightness_tool.get_brightness()
        elif name == "send_notification":
            result = notify_tool.send_notification(args.get("title", "ZELIA"), args.get("message", ""))
        elif name == "power":
            action = args.get("action", "")
            if action == "lock":
                result = power_tool.lock_screen()
            elif not args.get("confirmed"):
                result = {"needs_confirmation": True, "reason": f"About to {action} the computer. Are you sure?"}
            else:
                result = power_tool.power_action(action)
        elif name == "timer":
            action = args.get("action", "")
            if action == "cancel":
                result = timer_tool.cancel_timer(args.get("timer_id", ""))
            elif action == "list":
                result = timer_tool.list_timers()
            else:
                def _on_fire(message: str) -> None:
                    notify_tool.send_notification("ZELIA timer", message)
                    if self.announce is not None:
                        self.announce(f"Timer's up: {message}")
                result = timer_tool.set_timer(args.get("seconds", 0), args.get("message", ""), _on_fire)
        elif name == "read_man_page":
            result = man_tool.read_man_page(args.get("command", ""))
        elif name == "atspi_click":
            result = atspi_tool.invoke_action(args.get("name", ""))
        elif name == "tui_session":
            action = args.get("action", "")
            if action == "send_keys":
                result = tui_tool.send_keys(args.get("session_id", ""), args.get("keys", ""), args.get("enter", True))
            elif action == "read_screen":
                result = tui_tool.read_tui_screen(args.get("session_id", ""))
            elif action == "stop":
                result = tui_tool.stop_tui(args.get("session_id", ""))
            elif action == "list":
                result = tui_tool.list_tui_sessions()
            else:
                result = tui_tool.start_tui(
                    args.get("command", ""), args.get("location", "desktop"), cwd=self.files.workspace_dir,
                )
        else:
            return {"ok": False, "error": f"Unknown tool {name}"}

        if result.get("needs_confirmation"):
            question = result.get("reason") or f"About to run: {result.get('command')}. Are you sure?"
            if self.ask_confirmation(question):
                args["confirmed"] = True
                return self._dispatch_tool(name, args)
            return {"ok": False, "error": "User declined."}
        return result

    def handle_request(self, user_text: str, speak, remember_and_reply_when_done, should_continue=None):
        """
        speak: fn(text) -> None, spoken immediately (e.g. quick ack / final answer)
        remember_and_reply_when_done: fn(text) -> None, called later for big
            background jobs so a completed project gets announced whenever it finishes
        should_continue: optional fn() -> bool, checked at the top of each tool
            round; returning False aborts the request early instead of
            finishing it (returns "aborted" instead of the usual None). Used
            by src/idle_tasks.py so a low-priority background task stops the
            moment the user becomes active again, rather than fighting them
            for the keyboard/mouse/GUI focus it may be mid-use of. Voice/text
            requests don't pass this -- None means "never abort," the
            existing behavior.
        """
        self.second_brain.remember(user_text, role="user")
        memories = self.second_brain.recall(user_text)

        route = classify(user_text)
        log.info("Routed to %s brain", route)

        if route == "large":
            speak("Got it, that's a bigger one -- I'll work on it in the background and let you know.")

            def on_done(result):
                if result.get("ok"):
                    text = result["text"]
                else:
                    text = f"I ran into a problem on that project: {result.get('error')}"
                self.second_brain.remember(text, role="assistant")
                remember_and_reply_when_done(text)

            context = "\n".join(f"- {m}" for m in memories)
            prompt = f"Relevant context from past conversations:\n{context}\n\nTask:\n{user_text}"
            status = self.large_brain.submit_async(prompt, on_done=on_done, workspace_dir=self.files.workspace_dir)
            if status == "queued":
                speak("Actually, looks like you're gaming right now -- I'll hold off and start that as soon as you're done.")
            return

        # Small/fast path with tool calling.
        #
        # The system message content below MUST stay byte-identical across
        # every call -- Ollama caches the KV-computation for a matching
        # prompt prefix, and reuses it on the next call instead of
        # reprocessing from scratch. Confirmed live this matters a lot:
        # with the ~1900-token TOOL_SCHEMAS payload included (as every
        # call here does), a stable system+tools prefix took ~18s to
        # process cold but only ~1.2s on a cache hit -- a >15x difference.
        # This used to have "Relevant memories" interpolated directly into
        # this system string, which varies per request (different
        # memories recalled for different questions) -- since that
        # content preceded the point where tools get serialized into the
        # actual model prompt, ANY difference there invalidated the cache
        # for the entire rest of the prompt, including the large static
        # tool-schema block after it. Every single request was paying the
        # full cold-prefill cost as a result. Moving the varying memories
        # text into the user message instead (which comes after
        # everything that needs to stay cacheable) fixed this -- verified
        # live: repeated calls with a static system message and a
        # different memories+question each time stayed fast (~1.2s) after
        # the first.
        system_content = (
            "You are ZELIA, a helpful voice-controlled assistant running entirely "
            "locally on the user's Linux machine -- no cloud services, no MCP "
            "servers, just direct access to the machine you're running on.\n\n"
            "Important behavior rules:\n"
            "- By default, run shell/git/build commands with run_in_terminal so "
            "the user can watch them happen in a real terminal window. Only use "
            "run_shell_quiet if the user explicitly asked for something quiet or "
            "run in the background.\n"
            "- run_in_terminal, run_shell_quiet, and run_code all already start "
            "inside your workspace directory -- use relative paths like `hello.py`, "
            "never guess an absolute path like `~/workspace/hello.py` (that's the "
            "user's actual home directory, not your workspace, and won't exist).\n"
            "- To create or write a file's contents (a script, code, config, etc.), "
            "always use write_file -- never shell tricks like echo/printf piped into "
            "a file. echo doesn't interpret \\n as a real newline without -e, so "
            "multi-line content written that way comes out broken (literal backslash-n "
            "characters instead of line breaks). write_file has no such problem.\n"
            "- For anything web-related, use open_browser (a real visible browser "
            "window, default is Floorp) rather than just fetching text, unless the "
            "user only asked you to look something up for yourself. Use "
            "set_browser_for_now / set_default_browser if the user names a "
            "different browser to use. 'open a new window' means open_browser with "
            "new_window=true -- read_all_browser_tabs' Ctrl+Tab cycling is only for "
            "reading existing tabs' content, never a way to open or manage windows.\n"
            "- When asked to 'show me X', use the show_me tool.\n"
            "- If asked to explain/diagnose your own behavior or capabilities (why "
            "something happened, what tools you have, how a feature works), use "
            "list_own_source/read_own_source to actually check your real code rather "
            "than guessing -- don't use these for the user's own project files, "
            "that's read_file/list_dir.\n"
            "- show_me/launch_or_focus_app work for ANY installed app, not just ones "
            "you've been told about by name -- use list_apps if unsure what's installed. "
            "To click something in a native app's window (a button, a menu item), try "
            "atspi_click FIRST -- it's exact and doesn't need OCR/coordinates at all. It "
            "only works for apps that expose accessibility info though (most Qt/GTK apps; "
            "NOT Electron/CEF apps like Steam/Discord/Brave) -- if it reports no match/not "
            "available, fall back to find_text_on_screen to locate the thing, then "
            "click_at, then type_text/press_key as needed.\n"
            "- For a command-line tool you're not fully sure how to use correctly -- "
            "unfamiliar flags, an unusual command, anything where a wrong guess could "
            "matter -- use read_man_page to check its REAL usage on this machine first, "
            "rather than guessing from what you already know (which may not match the "
            "actual installed version).\n"
            "- For anything interactive in a terminal that isn't just 'run a command and "
            "read its output' -- htop, vim, a REPL, an ncurses menu -- use tui_session "
            "(action='start', then 'send_keys'/'read_screen'/'stop') instead of "
            "run_in_terminal/run_shell_quiet, which can't send further input once launched. "
            "Use location='background' specifically when asked to do this without disturbing "
            "the user (e.g. while they're gaming) instead of a visible window.\n"
            "- Opening or focusing an app (show_me, launch_or_focus_app) does not "
            "type or click anything by itself -- if the task also involves typing "
            "text, pressing keys, or clicking something, you must still call "
            "type_text/press_key/click_at yourself as separate tool calls after "
            "the app is open, in the same turn. Never describe text as 'typed' or "
            "an action as 'done' in your reply unless you actually called the tool "
            "that does it -- narrating an action instead of calling its tool is a "
            "real bug, not a shortcut.\n"
            "- click_at/type_text/press_key/run_in_terminal can come back with "
            "'user_currently_active': true instead of running -- this means the user "
            "is actively at the keyboard/mouse or a game is running, and doing this "
            "now would fight them for control of their own screen. Don't retry "
            "automatically. Ask them out loud what they'd like: wait until they're "
            "idle, or go ahead anyway (for run_in_terminal specifically, also offer "
            "run_in_vtty -- a separate real virtual terminal that doesn't touch their "
            "graphical session at all, invisible unless they switch to it themselves). "
            "Only retry the exact same tool call with confirmed=true once they've told "
            "you which they want, and only if they chose to proceed rather than wait.\n"
            "- Never answer a question about specific on-screen content (what's in a "
            "list, a library, a file, a window) without actually looking first via "
            "read_screen_text/describe_screen/find_text_on_screen. Opening or focusing "
            "an app is not the same as having seen what's inside it -- if the app needs "
            "navigating (e.g. clicking a tab) to reach the content asked about, do that "
            "before answering, don't guess or assume what you'd probably see.\n"
            "- read_screen_text/describe_screen only see whatever is currently visible "
            "on screen -- if asked to read/summarize/describe the FULL content of "
            "something that could be longer than one screen (a chat conversation, a "
            "long article, a scrollable document), use read_full_page instead, or "
            "you'll only see and summarize the first screenful and miss the rest. If "
            "the tab in question is open in Brave specifically, use read_brave_tab "
            "instead of read_full_page -- it reads the page's actual content directly "
            "(more accurate, and doesn't need to move the mouse/keyboard at all).\n"
            "- 'Gemini' here always means Google's Gemini AI chat, at https://gemini.google.com "
            "-- NOT the unrelated 'gemini://' network protocol (a completely different, much "
            "older internet protocol that happens to share the name). Never construct a "
            "'gemini://...' URL for anything -- that is never correct here, regardless of what "
            "it might otherwise look plausible for.\n"
            "- If asked to open/pick/switch to a SPECIFIC existing chat/conversation by "
            "name or topic (e.g. Gemini, Claude.ai chat history): NEVER pass a made-up URL "
            "to open_browser for this -- you do not know the real address of a specific "
            "past chat, and guessing one (e.g. something like 'gemini.google.com/chat' or "
            "similar) produces a broken page, not the chat meant. If Gemini/Claude.ai is "
            "already open in Brave, skip open_browser entirely and go straight to "
            "read_brave_tab with hint='gemini' (or 'claude') -- NOT the chat's topic/name, "
            "hint matches the TAB's title/URL, not page content -- to see what chats are "
            "listed. If no chat list/sidebar shows up in that read, the history sidebar is "
            "probably just collapsed (confirmed on Gemini specifically) -- use "
            "click_brave_element to click a likely sidebar/menu/history toggle button "
            "(match on something like 'menu', 'sidebar', or 'history'; icon-only buttons "
            "match by their aria-label even with no visible text), then read_brave_tab "
            "again (same hint='gemini') to see the now-visible chat titles. Once you can "
            "see the chat meant, "
            "use click_brave_element with that chat's exact title to select it -- never "
            "click_at/OCR-guess for any of this, click_brave_element is exact and doesn't "
            "touch the real mouse.\n"
            "- If the user asks you to do something later/in the background/whenever "
            "you're not busy, or specifically 'when idle', use add_idle_task instead of "
            "doing it now -- it queues the task to run automatically once the system is "
            "genuinely idle (no one at the keyboard, no game running). Don't use this for "
            "anything the user wants done right now.\n"
            "- power_action (shutdown/restart/suspend) ends the session or interrupts "
            "whatever's running -- always ask the user to confirm first, same as a "
            "destructive shell command, unless they already explicitly confirmed in this "
            "same request. lock_screen is different -- it's easily reversible, just do it, "
            "no confirmation needed.\n"
            "- For set_timer, convert whatever duration the user said (minutes, hours, "
            "'in half an hour', etc.) into seconds yourself before calling it.\n"
            "- Keep spoken replies concise -- this is a voice conversation. Describe "
            "code, file contents, commands, and errors in plain natural language a "
            "non-technical listener would understand -- never read raw code syntax, "
            "file paths, or stack traces verbatim out loud."
        )
        user_content = user_text
        if memories:
            # Explicit, forceful framing added after a live, confirmed
            # failure: asked a brand-new question about screen brightness,
            # the model answered with fabricated screen CONTENT lifted from
            # an unrelated earlier "what's on my screen" memory, presented
            # as if it were something just observed right now -- no tool
            # call happened at all. The bare "Relevant memories:\n- ..."
            # framing gave no signal that this text is OLD and describes a
            # DIFFERENT past moment, not the current state of anything.
            user_content = (
                "Relevant memories (OLD -- things said/observed in PAST separate "
                "conversations, NOT current facts, may be about a completely different "
                "topic or moment in time than this question -- never answer a question "
                "about CURRENT state, e.g. what's on screen right now, current settings, "
                "current file contents, from these alone; use the actual tool to check "
                "instead):\n"
                + "\n".join(f"- {m}" for m in memories)
                + f"\n\nCurrent question:\n{user_text}"
            )
        messages = [
            {"role": "system", "content": system_content},
            {"role": "user", "content": user_content},
        ]

        # A single short-circuited repeat (see IDEMPOTENT_TOOLS below) plus a
        # note wasn't always enough on its own -- confirmed live: the model
        # kept calling the same already-answered tool 8 more times after the
        # first short-circuit, still burning the whole round budget with no
        # final answer. REPEAT_LIMIT is the harder backstop: once a single
        # call has repeated past it, tools get withheld entirely on the next
        # round, forcing a plain-text completion the model has to answer
        # from what's already in the conversation instead of calling anything.
        REPEAT_LIMIT = 2
        seen_calls: dict[tuple, dict] = {}
        repeat_counts: dict[tuple, int] = {}
        force_final_answer = False
        for _ in range(MAX_TOOL_ROUNDS):
            if should_continue is not None and not should_continue():
                log.info("Aborting mid-request -- should_continue() returned False (system is no longer idle).")
                return "aborted"
            message = self.small_brain.chat(messages, tools=None if force_final_answer else TOOL_SCHEMAS)
            tool_calls = message.get("tool_calls")
            if not tool_calls:
                content = message.get("content", "").strip()
                leaked = _find_leaked_tool_calls(content)
                if leaked:
                    log.warning(
                        "Recovering %d leaked tool call(s) from reply text instead of executing them: %s",
                        len(leaked), [name for name, _ in leaked],
                    )
                    outcomes = []
                    for name, args in leaked:
                        try:
                            result = self._dispatch_tool(name, args)
                        except Exception as exc:  # noqa: BLE001
                            log.error("Recovered leaked tool call %s failed: %s", name, exc)
                            result = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
                        outcomes.append("done" if result.get("ok") else f"failed ({result.get('error', 'unknown error')})")
                    prefix = content.split(leaked[0][0], 1)[0].strip()
                    reply = (prefix + " " if prefix else "") + "; ".join(outcomes)
                else:
                    reply = content
                self.second_brain.remember(reply, role="assistant")
                speak(reply)
                return

            messages.append(message)
            for call in tool_calls:
                name = call["function"]["name"]
                args = call["function"]["arguments"]
                try:
                    if isinstance(args, str):
                        args = json.loads(args)
                    call_key = (name, json.dumps(args, sort_keys=True))
                    if name in IDEMPOTENT_TOOLS and call_key in seen_calls:
                        repeat_counts[call_key] = repeat_counts.get(call_key, 1) + 1
                        result = dict(seen_calls[call_key])
                        if repeat_counts[call_key] > REPEAT_LIMIT:
                            force_final_answer = True
                            result["_note"] = (
                                "You have called this the same way several times now. Tools are "
                                "disabled for your next reply -- answer the user's question directly "
                                "using the result above, in plain text."
                            )
                        else:
                            result["_note"] = (
                                "You already called this with these exact arguments earlier in this "
                                "turn -- this is the same result, not a fresh check. Answer from it "
                                "instead of calling this again."
                            )
                        log.warning(
                            "Short-circuited a repeated call to %s%s (repeat #%d)",
                            name, args, repeat_counts[call_key],
                        )
                    else:
                        result = self._dispatch_tool(name, args)
                        if name in IDEMPOTENT_TOOLS:
                            seen_calls[call_key] = result
                            repeat_counts[call_key] = 1
                except Exception as exc:  # noqa: BLE001
                    # A single bad tool call (malformed args, a hallucinated
                    # extra kwarg, a permission error, etc.) must not take
                    # down the whole request -- feed the error back as a
                    # tool result so the model can retry/adapt, same as any
                    # other tool outcome. This matters most for unsupervised
                    # overnight runs with nobody watching to notice a crash.
                    log.error("Tool %s failed: %s", name, exc)
                    result = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
                messages.append({"role": "tool", "content": json.dumps(result)})

        speak("I'm having trouble finishing that one, sorry -- want me to try a different approach?")
