import binascii
import os
import re
import subprocess
import sys
from typing import Any, Dict

from app.config import WORKSPACE_DIR
from app.state import state

# Regex for parsing tool calls and code blocks
TOOL_RE = re.compile(r"```tool:(?P<name>[a-zA-Z0-9_\-]+)\s*\n(?P<body>[\s\S]*?)```", re.M)
CODE_FENCE_RE = re.compile(r"```(python|bash|json|[a-zA-Z0-9_\-]*)[\s\S]*?```", re.M)

# Hints for classifying agent roles based on their names
ROLE_HINTS = {
    "programmer": {"keywords": ["programmer", "developer", "coder", "engineer"]},
    "reviewer": {"keywords": ["critic", "review", "reviewer", "qa", "tester"]},
    "ux": {"keywords": ["ux", "ui", "designer", "design"]},
}

def get_current_workspace() -> str:
    """
    Returns the workspace path for the current session.
    """
    session = state.current_session_name
    # secure filename to prevent path traversal
    session = "".join([c for c in session if c.isalnum() or c in (' ', '.', '_', '-')]).strip()
    if not session:
        session = "default"
    
    # Store sessions in a 'sessions' subdirectory to keep root clean
    ws = os.path.abspath(os.path.join(WORKSPACE_DIR, "sessions", session))
    os.makedirs(ws, exist_ok=True)
    return ws

def _safe_path(path: str) -> str:
    """
    Ensures that the given path is within the workspace directory.
    """
    base = get_current_workspace()
    # Resolve 'path' relative to the base
    # If path is absolute and starts with base, allowed.
    # If path is relative, join with base.

    if os.path.isabs(path):
        p = os.path.abspath(path)
    else:
        p = os.path.abspath(os.path.join(base, path))

    # Normalize for case-insensitive systems (Windows)
    base_norm = os.path.normcase(base)
    p_norm = os.path.normcase(p)

    # Resolve any symlinks in the path
    try:
        p = os.path.realpath(p)
        base = os.path.realpath(base)
        base_norm = os.path.normcase(base)
        p_norm = os.path.normcase(p)
    except OSError:
        # Handle cases where the file or directory doesn't exist
        pass

    if not p_norm.startswith(base_norm) and p_norm != base_norm:
        # Allow base directory itself
        raise ValueError(f"Path is outside the allowed workspace directory: {p} (base: {base})")
    return p

def tree_listing(root: str) -> Dict[str, Any]:
    """
    Generates a tree listing of the workspace directory.
    """
    # Root is typically "."
    cwd = get_current_workspace()
    
    # If root is ".", we look at cwd. 
    # If root is a specific path, safely resolve it.
    if root == ".":
        root_abs = cwd
    else:
        root_abs = _safe_path(root)

    def walk(p: str) -> Dict[str, Any]:
        rel_path = os.path.relpath(p, cwd)
        if rel_path == ".":
            rel_path = ""
        node = {"name": os.path.basename(p) or os.path.basename(root_abs), "type": "dir", "path": rel_path, "children": []}
        try:
            for nm in sorted(os.listdir(p)):
                fp = os.path.join(p, nm)
                if os.path.isdir(fp):
                    node["children"].append(walk(fp))
                else:
                    node["children"].append({"name": nm, "type": "file", "path": os.path.relpath(fp, cwd)})
        except Exception as e:
            node["error"] = str(e)
        return node
    return walk(root_abs)

import tempfile

def execute_python(code: str) -> str:
    """
    Executes a string of Python code in a temporary file and returns the output.
    """
    ws = get_current_workspace()
    try:
        with tempfile.NamedTemporaryFile(suffix=".py", delete=False, mode="w", encoding="utf-8", dir=ws) as tmp_file:
            tmp_file.write(code)
            tmp_file_path = tmp_file.name

        # Run in the workspace dir
        p = subprocess.run([sys.executable, tmp_file_path], capture_output=True, text=True, timeout=60, cwd=ws)
        out = p.stdout
        if p.stderr:
            out += "\n--- STDERR ---\n" + p.stderr
        return out
    except Exception as e:
        return f"exec error: {e}"
    finally:
        try:
            os.remove(tmp_file_path)
        except Exception:
            pass
