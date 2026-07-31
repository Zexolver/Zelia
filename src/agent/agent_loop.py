"""
Ties everything together: takes a transcribed user request, routes it to the
right brain, runs the tool-calling loop, handles "are you sure?" confirmations
out loud, and hands the final answer back to be spoken + remembered.
"""
import json

from src.agent.tools.shell_tool import run_shell, is_destructive
from src.agent.tools.file_tool import FileTool
from src.agent.tools.browser_tool import fetch_url
from src.agent.tools.code_tool import CodeTool
from src.agent.tools import screen_tool, app_launcher, desktop_control, browser_control, steam_tool
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


SCREEN_VISIBILITY_TOOLS = {"read_screen_text", "describe_screen", "find_text_on_screen", "click_at"}


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
        messages = [
            {
                "role": "system",
                "content": (
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
                    "- Keep spoken replies concise -- this is a voice conversation. Describe "
                    "code, file contents, commands, and errors in plain natural language a "
                    "non-technical listener would understand -- never read raw code syntax, "
                    "file paths, or stack traces verbatim out loud.\n\n"
                    "Relevant memories:\n" + "\n".join(f"- {m}" for m in memories)
                ),
            },
            {"role": "user", "content": user_text},
        ]

        for _ in range(MAX_TOOL_ROUNDS):
            message = self.small_brain.chat(messages, tools=TOOL_SCHEMAS)
            tool_calls = message.get("tool_calls")
            if not tool_calls:
                reply = message.get("content", "").strip()
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
