import os
import shutil
from typing import Any, Dict

from app.config import WORKSPACE_DIR
from app.utils import _safe_path


def tool_listdir(path: str = ".") -> Dict[str, Any]:
    p = _safe_path(path)
    entries = []
    for nm in sorted(os.listdir(p)):
        fp = os.path.join(p, nm)
        entries.append({"name": nm, "is_dir": os.path.isdir(fp)})
    return {"cwd": os.path.relpath(p, WORKSPACE_DIR), "entries": entries}


def tool_read_file(path: str) -> str:
    with open(_safe_path(path), "r", encoding="utf-8") as f:
        return f.read()


def tool_write_file(path: str, content: str, overwrite: bool = True) -> str:
    fp = _safe_path(path)
    os.makedirs(os.path.dirname(fp), exist_ok=True)
    if not overwrite and os.path.exists(fp):
        raise FileExistsError("exists")
    with open(fp, "w", encoding="utf-8") as f:
        f.write(content)
    return f"wrote {len(content)} bytes to {path}"


def tool_delete(path: str) -> str:
    fp = _safe_path(path)
    if os.path.isdir(fp):
        shutil.rmtree(fp)
    else:
        os.remove(fp)
    return f"deleted {path}"


TOOLS: Dict[str, Any] = {
    "listdir": tool_listdir,
    "read_file": tool_read_file,
    "write_file": tool_write_file,
    "delete": tool_delete,
}
