import binascii
import os
import re
import subprocess
import sys
from typing import Any, Dict

from app.config import WORKSPACE_DIR

# Regex for parsing tool calls and code blocks
TOOL_RE = re.compile(r"```tool:(?P<name>[a-zA-Z0-9_\-]+)\s*\n(?P<body>[\s\S]*?)```", re.M)
CODE_FENCE_RE = re.compile(r"```(python|bash|json|[a-zA-Z0-9_\-]*)[\s\S]*?```", re.M)

# Hints for classifying agent roles based on their names
ROLE_HINTS = {
    "programmer": {"keywords": ["programmer", "developer", "coder", "engineer"]},
    "reviewer": {"keywords": ["critic", "review", "reviewer", "qa", "tester"]},
    "ux": {"keywords": ["ux", "ui", "designer", "design"]},
}

def _safe_path(path: str) -> str:
    """
    Ensures that the given path is within the workspace directory.
    """
    base = os.path.abspath(WORKSPACE_DIR)
    p = os.path.abspath(os.path.join(base, path))
    if not p.startswith(base + os.sep) and p != base:
        raise ValueError("Path is outside the allowed workspace directory.")
    return p

def tree_listing(root: str) -> Dict[str, Any]:
    """
    Generates a tree listing of the workspace directory.
    """
    root_abs = _safe_path(root)
    def walk(p: str) -> Dict[str, Any]:
        rel_path = os.path.relpath(p, WORKSPACE_DIR)
        if rel_path == ".":
            rel_path = ""
        node = {"name": os.path.basename(p) or os.path.basename(root_abs), "type": "dir", "path": rel_path, "children": []}
        try:
            for nm in sorted(os.listdir(p)):
                fp = os.path.join(p, nm)
                if os.path.isdir(fp):
                    node["children"].append(walk(fp))
                else:
                    node["children"].append({"name": nm, "type": "file", "path": os.path.relpath(fp, WORKSPACE_DIR)})
        except Exception as e:
            node["error"] = str(e)
        return node
    return walk(root_abs)

def execute_python(code: str) -> str:
    """
    Executes a string of Python code in a temporary file and returns the output.
    """
    fp = os.path.join(WORKSPACE_DIR, f"tmp_{binascii.b2a_hex(os.urandom(4)).decode()}.py")
    try:
        with open(fp, "w", encoding="utf-8") as f:
            f.write(code)
        p = subprocess.run([sys.executable, fp], capture_output=True, text=True, timeout=60)
        out = p.stdout
        if p.stderr:
            out += "\n--- STDERR ---\n" + p.stderr
        return out
    except Exception as e:
        return f"exec error: {e}"
    finally:
        try:
            os.remove(fp)
        except Exception:
            pass
