"""File operations for the agent, scoped to the configured workspace directory."""
import os

from src.utils.logger import get_logger

log = get_logger("file_tool")


class FileTool:
    def __init__(self, workspace_dir: str):
        self.workspace_dir = os.path.abspath(workspace_dir)
        os.makedirs(self.workspace_dir, exist_ok=True)

    def _resolve(self, path: str) -> str:
        full = os.path.abspath(os.path.join(self.workspace_dir, path))
        if not full.startswith(self.workspace_dir):
            raise ValueError("Path escapes the workspace directory -- refusing.")
        return full

    def read(self, path: str) -> dict:
        full = self._resolve(path)
        if not os.path.exists(full):
            return {"ok": False, "error": "File does not exist."}
        with open(full, "r", errors="replace") as f:
            return {"ok": True, "content": f.read()}

    def write(self, path: str, content: str, confirmed: bool = False) -> dict:
        full = self._resolve(path)
        exists = os.path.exists(full)
        if exists and not confirmed:
            return {"needs_confirmation": True, "reason": f"'{path}' already exists and would be overwritten."}
        os.makedirs(os.path.dirname(full), exist_ok=True)
        with open(full, "w") as f:
            f.write(content)
        log.info("Wrote %s (%d bytes)", full, len(content))
        return {"ok": True}

    def delete(self, path: str, confirmed: bool = False) -> dict:
        full = self._resolve(path)
        if not confirmed:
            return {"needs_confirmation": True, "reason": f"About to permanently delete '{path}'."}
        if os.path.exists(full):
            os.remove(full)
            log.info("Deleted %s", full)
        return {"ok": True}

    def list_dir(self, path: str = ".") -> dict:
        full = self._resolve(path)
        if not os.path.isdir(full):
            return {"ok": False, "error": "Not a directory."}
        return {"ok": True, "entries": sorted(os.listdir(full))}
