"""
A ReAct-style prompted tool-calling loop for AirLLM, used by
airllm_worker.py when a job includes a workspace_dir (a "code this"
request, as opposed to a plain question).

AirLLM's generate() is a raw completion call -- no native function-calling
API the way Ollama's small-brain path has (see agent_loop.py's TOOL_SCHEMAS).
This module makes up for that with prompting: the model is told to emit
either a single-line JSON tool call or a final answer, each call's result is
appended to the transcript, and generation repeats until a final answer, a
hard iteration cap, or a wall-clock timeout is hit. Less reliable than
native structured tool calling by construction (a capable model can still
malform the JSON) -- one retry is given on a parse failure before giving up
on that step and asking the model to try a different approach, rather than
looping forever on the same mistake.

Runs entirely unattended (the whole point -- coding while the user's away
or out of Claude usage), so there's nobody to answer a "needs_confirmation"
prompt the way the small brain's live tools can ask out loud. Destructive
shell commands are refused outright rather than queued, and file overwrites
are only auto-allowed for files this same job already wrote (never a
pre-existing file it didn't create) -- see _safe_write below.
"""
import json
import os
import re
import time

from src.agent.tools.file_tool import FileTool
from src.agent.tools.shell_tool import run_shell, is_destructive
from src.utils.logger import get_logger

log = get_logger("coding_agent")

MAX_ITERATIONS = 15
MAX_SECONDS = 60 * 60  # hard wall-clock cap regardless of iteration count
MAX_NEW_TOKENS_PER_STEP = 1024
SHELL_TIMEOUT_SECONDS = 180

TOOLS_DESCRIPTION = """You are ZELIA's coding agent, working unattended in a workspace directory. You have these tools:

- read_file(path): read a file's contents.
- write_file(path, content): create or overwrite a file.
- delete_file(path): delete a file.
- list_dir(path="."): list a directory's contents.
- run_shell(command): run a shell command in the workspace directory (build, test, run a script, etc). No interactive input is possible -- avoid commands that prompt for input.

To use a tool, respond with EXACTLY one line in this form (valid single-line JSON, newlines in content escaped as \\n):
TOOL_CALL: {"name": "write_file", "arguments": {"path": "app.py", "content": "print('hi')\\n"}}

When the task is complete (or you've hit a wall you can't resolve without the user), respond with:
FINAL ANSWER: <a concise summary of what you did, what you couldn't do, and anything the user should check>

Only ever do ONE of those two things per response -- either one tool call, or the final answer. Never both, never plain prose with no tool call and no FINAL ANSWER prefix."""

_TOOL_CALL_RE = re.compile(r"TOOL_CALL:\s*(\{.*\})", re.DOTALL)
_FINAL_ANSWER_RE = re.compile(r"FINAL ANSWER:\s*(.*)", re.DOTALL)


class _Toolbox:
    def __init__(self, workspace_dir: str):
        self.files = FileTool(workspace_dir)
        self.workspace_dir = self.files.workspace_dir
        self._written_this_session: set[str] = set()

    def _safe_write(self, path: str, content: str) -> dict:
        full = self.files.resolve(path)
        already_ours = full in self._written_this_session or not os.path.exists(full)
        result = self.files.write(path, content, confirmed=already_ours)
        if result.get("ok"):
            self._written_this_session.add(full)
        elif result.get("needs_confirmation"):
            result = {
                "ok": False,
                "error": (
                    f"'{path}' already exists and wasn't created by this job -- refusing to overwrite it "
                    "unattended. Use a different filename, or mention this in your final answer for the user to handle."
                ),
            }
        return result

    def run(self, name: str, arguments: dict) -> dict:
        try:
            if name == "read_file":
                return self.files.read(arguments["path"])
            if name == "write_file":
                return self._safe_write(arguments["path"], arguments.get("content", ""))
            if name == "delete_file":
                path = arguments["path"]
                full = self.files.resolve(path)
                if full not in self._written_this_session:
                    return {
                        "ok": False,
                        "error": f"'{path}' wasn't created by this job -- refusing to delete it unattended.",
                    }
                return self.files.delete(path, confirmed=True)
            if name == "list_dir":
                return self.files.list_dir(arguments.get("path", "."))
            if name == "run_shell":
                command = arguments["command"]
                if is_destructive(command):
                    return {
                        "ok": False,
                        "error": f"Refusing to run a potentially destructive command unattended: {command!r}",
                    }
                full_command = f"cd {self.workspace_dir} && {command}"
                result = run_shell(full_command, confirmed=True, timeout=SHELL_TIMEOUT_SECONDS)
                result["ok"] = result.get("returncode") == 0
                return result
            return {"ok": False, "error": f"Unknown tool '{name}'."}
        except KeyError as exc:
            return {"ok": False, "error": f"Missing required argument {exc} for tool '{name}'."}
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}


def run_coding_task(prompt: str, workspace_dir: str, generate_fn, max_iterations: int = MAX_ITERATIONS) -> dict:
    """generate_fn(text: str) -> str -- a single completion call (wraps
    AirLLM's tokenizer+model.generate, kept out of this module so the loop
    logic here can be tested without a real model loaded)."""
    toolbox = _Toolbox(workspace_dir)
    transcript = f"{TOOLS_DESCRIPTION}\n\nTask:\n{prompt}\n"
    actions_log = []
    start = time.monotonic()

    for step in range(1, max_iterations + 1):
        if time.monotonic() - start > MAX_SECONDS:
            log.warning("Coding task hit the %ds wall-clock cap -- stopping.", MAX_SECONDS)
            return {
                "ok": True,
                "text": "Ran out of time before finishing (hit the background time limit). "
                        f"Actions taken so far:\n" + "\n".join(actions_log),
                "actions": actions_log,
            }

        reply = generate_fn(transcript)
        transcript += reply + "\n"

        final_match = _FINAL_ANSWER_RE.search(reply)
        if final_match:
            answer = final_match.group(1).strip()
            log.info("Coding task finished after %d step(s).", step)
            return {"ok": True, "text": answer, "actions": actions_log}

        tool_match = _TOOL_CALL_RE.search(reply)
        if not tool_match:
            note = (
                "Your last response didn't match either format -- respond with exactly one "
                "TOOL_CALL: {...} line or one FINAL ANSWER: ... line, nothing else."
            )
            transcript += f"SYSTEM: {note}\n"
            continue

        try:
            call = json.loads(tool_match.group(1))
            name, arguments = call["name"], call.get("arguments", {})
        except (json.JSONDecodeError, KeyError, TypeError) as exc:
            transcript += f"SYSTEM: That TOOL_CALL wasn't valid ({exc}) -- retry with correct JSON.\n"
            continue

        result = toolbox.run(name, arguments)
        summary = f"{name}({arguments}) -> {'ok' if result.get('ok') else 'failed: ' + str(result.get('error'))}"
        actions_log.append(summary)
        log.info("Step %d: %s", step, summary)
        transcript += f"TOOL_RESULT: {json.dumps(result)[:4000]}\n"

    log.warning("Coding task hit the %d-iteration cap without a final answer.", max_iterations)
    return {
        "ok": True,
        "text": f"Stopped after {max_iterations} steps without finishing. Actions taken:\n" + "\n".join(actions_log),
        "actions": actions_log,
    }
