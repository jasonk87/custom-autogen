import os
import shutil
import subprocess
import ast
from typing import Any, Dict

from app.config import WORKSPACE_DIR
from app.state import state
from app.utils import _safe_path, execute_python


def tool_listdir(path: str = ".") -> Dict[str, Any]:
    p = _safe_path(path)
    entries = []
    for nm in sorted(os.listdir(p)):
        fp = os.path.join(p, nm)
        entries.append({"name": nm, "is_dir": os.path.isdir(fp)})
    return {"cwd": os.path.relpath(p, WORKSPACE_DIR), "entries": entries}


def tool_read_file(path: str) -> str:
    fp = _safe_path(path)
    if not os.path.exists(fp):
        return (
            f"FILE_NOT_FOUND: '{path}' does not exist in the workspace. "
            "Run listdir first to discover available files, or use write_file to create it."
        )
    if os.path.isdir(fp):
        return (
            f"NOT_A_FILE: '{path}' is a directory. "
            "Run listdir to inspect directory contents and pick a file."
        )
    with open(fp, "r", encoding="utf-8") as f:
        return f.read()


def tool_write_file(path: str, content: str, overwrite: bool = True) -> str:
    fp = _safe_path(path)
    os.makedirs(os.path.dirname(fp), exist_ok=True)
    if not overwrite and os.path.exists(fp):
        raise FileExistsError("exists")
    with open(fp, "w", encoding="utf-8") as f:
        f.write(content)
    return f"wrote {len(content)} bytes to {path}"


def tool_replace_in_file(path: str, search_string: str, replace_string: str) -> str:
    """
    Replaces an exact `search_string` with `replace_string` in the specified file.
    Use this to edit existing files instead of overwriting them completely with `write_file`.
    If the file is a Python file (.py), it also performs syntax checking on the new content.
    If a SyntaxError is detected, the change is reverted and the error is returned.
    """
    fp = _safe_path(path)
    if not os.path.exists(fp):
        return f"Error: File '{path}' does not exist."
    if os.path.isdir(fp):
        return f"Error: '{path}' is a directory, not a file."

    with open(fp, "r", encoding="utf-8") as f:
        content = f.read()

    if search_string not in content:
        return "Error: The `search_string` was not found in the file. Please make sure the search string matches the file content exactly."

    new_content = content.replace(search_string, replace_string)

    if fp.endswith(".py"):
        try:
            ast.parse(new_content)
        except SyntaxError as e:
            return f"SyntaxError in new content: {e}. The file was not changed."

    with open(fp, "w", encoding="utf-8") as f:
        f.write(new_content)

    return f"Successfully replaced occurrences of the search string in {path}."


def tool_delete(path: str) -> str:
    fp = _safe_path(path)
    if os.path.isdir(fp):
        shutil.rmtree(fp)
    else:
        os.remove(fp)
    return f"deleted {path}"


def execute_shell_command(command: str) -> str:
    """
    Executes a shell command in the workspace directory and returns the output.
    Useful for running programs, tests, linters, and other command-line tools.

    Args:
        command: The shell command to execute.

    Returns:
        A string containing the stdout and stderr of the command.

    Warning:
        This tool allows the execution of arbitrary shell commands.
        Ensure that the agent's instructions are safe and that the
        environment is properly sandboxed if security is a concern.
    """
    raw_command = (command or "").strip()
    if not raw_command:
        return "Error executing command: empty command."

    lowered = raw_command.lower()
    blocked_tokens = [
        "rm -rf /",
        "del /f /s /q c:\\",
        "format ",
        "shutdown ",
        "reboot ",
        "mkfs",
    ]
    if any(token in lowered for token in blocked_tokens):
        return "Error executing command: command blocked by safety policy."

    # Lightweight compatibility layer for tests and simple commands across OSes.
    cmd = raw_command
    if lowered.startswith("touch "):
        file_name = raw_command[6:].strip().strip("\"'")
        cmd = f"python -c \"open(r'{file_name}', 'a', encoding='utf-8').close()\""
    elif lowered.startswith("ls "):
        target = raw_command[3:].strip()
        cmd = f"python -c \"import os,sys;p=r'{target}';print('\\n'.join(os.listdir(p)))\""

    workspace = getattr(state, "active_workspace", WORKSPACE_DIR) or WORKSPACE_DIR
    if not os.path.isdir(workspace):
        workspace = WORKSPACE_DIR
    os.makedirs(workspace, exist_ok=True)
    max_chars = 8000

    try:
        result = subprocess.run(
            cmd,
            shell=True,
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            cwd=workspace,
            timeout=20,
        )
        stdout = (result.stdout or "")[:max_chars]
        stderr = (result.stderr or "")[:max_chars]
        output = f"Stdout:\n{stdout}\n"
        if stderr:
            output += f"Stderr:\n{stderr}\n"
        return output
    except subprocess.TimeoutExpired:
        return "Error executing command: command timed out after 20 seconds."
    except subprocess.CalledProcessError as e:
        stdout = (e.stdout or "")[:max_chars]
        stderr = (e.stderr or "")[:max_chars]
        return f"Error executing command: {e}\nStdout:\n{stdout}\nStderr:\n{stderr}"


TOOLS: Dict[str, Any] = {
    "listdir": tool_listdir,
    "read_file": tool_read_file,
    "write_file": tool_write_file,
    "replace_in_file": tool_replace_in_file,
    "delete": tool_delete,
    "execute_shell_command": execute_shell_command,
    "execute_python": execute_python,
}
