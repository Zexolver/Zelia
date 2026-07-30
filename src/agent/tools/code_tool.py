"""Write and execute code inside the workspace. Runs visibly in a terminal
window by default (matches how shell commands behave) -- pass quiet=True
only when explicitly asked to run something in the background."""
from src.agent.tools.file_tool import FileTool
from src.agent.tools.desktop_control import open_terminal
from src.agent.tools.shell_tool import run_shell, is_destructive


class CodeTool:
    def __init__(self, workspace_dir: str):
        self.files = FileTool(workspace_dir)

    def write_code(self, path: str, content: str, confirmed: bool = False) -> dict:
        return self.files.write(path, content, confirmed=confirmed)

    def run(self, command: str, confirmed: bool = False, quiet: bool = False) -> dict:
        """e.g. command='python3 script.py' -- runs inside the workspace dir."""
        full_command = f"cd {self.files.workspace_dir} && {command}"
        if quiet:
            return run_shell(full_command, confirmed=confirmed)
        if is_destructive(full_command) and not confirmed:
            return {"needs_confirmation": True, "command": full_command}
        return open_terminal(command=full_command)
