import os
import shutil
from typing import Any, Dict

from app.utils import _safe_path, get_current_workspace


def tool_listdir(path: str = ".") -> Dict[str, Any]:
    p = _safe_path(path)
    entries = []
    for nm in sorted(os.listdir(p)):
        fp = os.path.join(p, nm)
        entries.append({"name": nm, "is_dir": os.path.isdir(fp)})
    return {"cwd": os.path.relpath(p, get_current_workspace()), "entries": entries}


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


import subprocess

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

    Args:
        command: The shell command to execute.

    Returns:
        A string containing the stdout and stderr of the command.

    Warning:
        This tool allows the execution of arbitrary shell commands.
        Ensure that the agent's instructions are safe and that the
        environment is properly sandboxed if security is a concern.
    """
    try:
        result = subprocess.run(
            command,
            shell=True,
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            cwd=get_current_workspace()
        )
        output = f"Stdout:\n{result.stdout}\n"
        if result.stderr:
            output += f"Stderr:\n{result.stderr}\n"
        
        # Truncate output if too long
        if len(output) > 2000:
            output = output[:2000] + "... (truncated)"
            
        return output
    except subprocess.CalledProcessError as e:
        err_out = f"Error executing command: {e}\nStdout:\n{e.stdout}\nStderr:\n{e.stderr}"
        if len(err_out) > 2000:
            err_out = err_out[:2000] + "... (truncated)"
        return err_out


TOOLS: Dict[str, Any] = {
    "listdir": tool_listdir,
    "read_file": tool_read_file,
    "write_file": tool_write_file,
    "delete": tool_delete,
    "execute_shell_command": execute_shell_command,
}
