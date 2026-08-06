"""
Lets ZELIA read her own source code -- for self-diagnosis ("why did that
happen?", "what tools do I actually have?") instead of guessing from her
system prompt/training alone.

Read-only on purpose. Letting her also *write* her own live source is a
much bigger, separate feature (needs a restart mechanism to actually take
effect, some kind of rollback/safety net if a change breaks something,
and careful thought about what "unsupervised self-modification" should
even mean) -- explicitly not decided on yet, don't wire up write/delete
here without that being a deliberate follow-up choice.

Scoped to src/ specifically, not the whole install directory -- reuses
FileTool's existing path-escape guard rather than duplicating it. Never
exposes config/ (holds the remote_bridge bearer token) or anything
outside src/.
"""
from pathlib import Path

from src.agent.tools.file_tool import FileTool

_SRC_ROOT = str(Path(__file__).resolve().parents[2])
_source = FileTool(_SRC_ROOT)


def read_own_source(path: str) -> dict:
    return _source.read(path)


def list_own_source(path: str = ".") -> dict:
    return _source.list_dir(path)
