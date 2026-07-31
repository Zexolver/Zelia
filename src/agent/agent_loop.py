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
from src.agent.tools import screen_tool, app_launcher, desktop_control, browser_control, steam_tool, browser_tabs, page_reader, cdp_reader
from src.router import classify
from src.utils.logger import get_logger

log = get_logger("agent_loop")

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
            "description": "Open a URL in a real, visible browser window. Uses the configured default browser unless `browser` is given.",
            "parameters": {
                "type": "object",
                "properties": {"url": {"type": "string"}, "browser": {"type": "string"}},
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
            "description": "Type text into whatever window currently has keyboard focus (e.g. after opening a browser or app). Works on both Xorg and Wayland.",
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
            "name": "press_key",
            "description": "Send a key combo to the focused window, e.g. 'ctrl+l' (address bar), 'ctrl+t' (new tab), 'Return', 'Tab', 'Escape'.",
            "parameters": {
                "type": "object",
                "properties": {"combo": {"type": "string"}},
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
            "description": "Click at specific screen coordinates. Usually preceded by find_text_on_screen to locate what to click.",
            "parameters": {
                "type": "object",
                "properties": {"x": {"type": "integer"}, "y": {"type": "integer"}},
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
]

MAX_TOOL_ROUNDS = 10

_LEAKED_CALL_RE = re.compile(
    r'\b(' + "|".join(re.escape(t["function"]["name"]) for t in TOOL_SCHEMAS) + r')\s*(\{.*?\})',
    re.DOTALL,
)


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
    return found


SCREEN_VISIBILITY_TOOLS = {
    "read_screen_text", "describe_screen", "find_text_on_screen", "click_at",
    "read_all_browser_tabs", "read_full_page", "read_brave_tab", "list_brave_tabs",
}


class AgentLoop:
    def __init__(self, small_brain, large_brain, second_brain, workspace_dir: str, ask_confirmation,
                 vision_model: str = "moondream", ollama_host: str = "http://127.0.0.1:11434",
                 default_browser: str = "floorp", config_path: str = "", ask_for_password=None):
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

    def _dispatch_tool(self, name: str, args: dict) -> dict:
        if name in SCREEN_VISIBILITY_TOOLS:
            lock_error = self._ensure_unlocked_for_screen_access()
            if lock_error is not None:
                return lock_error
        if name == "run_in_terminal":
            command = args.get("command", "")
            if is_destructive(command) and not args.get("confirmed"):
                result = {"needs_confirmation": True, "command": command}
            else:
                result = desktop_control.preserve_focus_if_user_active(
                    lambda: desktop_control.open_terminal(command=command, cwd=self.files.workspace_dir)
                )
        elif name == "run_shell_quiet":
            args.pop("quiet", None)  # tool name already implies this; small models sometimes add it anyway
            result = run_shell(cwd=self.files.workspace_dir, **args)
        elif name == "read_file":
            result = self.files.read(**args)
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
            result = desktop_control.type_text(**args)
        elif name == "press_key":
            result = desktop_control.press_key(**args)
        elif name == "find_text_on_screen":
            result = desktop_control.find_text_on_screen(**args)
        elif name == "click_at":
            result = desktop_control.click_at(**args)
        elif name == "focus_window":
            result = desktop_control.preserve_focus_if_user_active(lambda: desktop_control.focus_window(**args))
        else:
            return {"ok": False, "error": f"Unknown tool {name}"}

        if result.get("needs_confirmation"):
            question = result.get("reason") or f"About to run: {result.get('command')}. Are you sure?"
            if self.ask_confirmation(question):
                args["confirmed"] = True
                return self._dispatch_tool(name, args)
            return {"ok": False, "error": "User declined."}
        return result

    def handle_request(self, user_text: str, speak, remember_and_reply_when_done):
        """
        speak: fn(text) -> None, spoken immediately (e.g. quick ack / final answer)
        remember_and_reply_when_done: fn(text) -> None, called later for big
            background jobs so a completed project gets announced whenever it finishes
        """
        self.second_brain.remember(user_text, role="user")
        memories = self.second_brain.recall(user_text)

        route = classify(user_text, self.small_brain)
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
            status = self.large_brain.submit_async(prompt, on_done=on_done)
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
            "different browser to use.\n"
            "- When asked to 'show me X', use the show_me tool.\n"
            "- To interact with something only visible on screen (a button in a "
            "browser, a menu in an app like Godot), use find_text_on_screen to "
            "locate it, then click_at, then type_text/press_key as needed.\n"
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
            "- Keep spoken replies concise -- this is a voice conversation. Describe "
            "code, file contents, commands, and errors in plain natural language a "
            "non-technical listener would understand -- never read raw code syntax, "
            "file paths, or stack traces verbatim out loud."
        )
        user_content = user_text
        if memories:
            user_content = "Relevant memories:\n" + "\n".join(f"- {m}" for m in memories) + f"\n\n{user_text}"
        messages = [
            {"role": "system", "content": system_content},
            {"role": "user", "content": user_content},
        ]

        for _ in range(MAX_TOOL_ROUNDS):
            message = self.small_brain.chat(messages, tools=TOOL_SCHEMAS)
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
                    result = self._dispatch_tool(name, args)
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
