import os
import shutil
import subprocess
import ast
import httpx
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
    if fp == _safe_path("."):
        return "Error: Refusing to delete the workspace root."
    if os.path.isdir(fp):
        shutil.rmtree(fp)
    else:
        os.remove(fp)
    return f"deleted {path}"


def tool_share_file(path: str) -> str:
    """
    Returns the Markdown snippet to display or link to a file in the chat UI.
    Use this to visually share an image or provide a download link for a file
    that you have created or edited in the workspace.
    """
    fp = _safe_path(path)
    if not os.path.exists(fp):
        return f"Error: File '{path}' does not exist."
    if os.path.isdir(fp):
        return f"Error: '{path}' is a directory. Please provide a file."

    # Extract the relative path from the workspace root to format the URL correctly
    rel_path = os.path.relpath(fp, _safe_path("."))
    url = f"/workspace/{rel_path}"

    ext = os.path.splitext(fp)[1].lower()
    if ext in {".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg"}:
        return f"To share this image, output this exact markdown in your next message:\n![{os.path.basename(fp)}]({url})"
    elif ext in {".mp4", ".webm"}:
        return f"To share this video, output this exact HTML in your next message:\n<video controls src='{url}' width='100%'></video>"
    else:
        return f"To share this file, output this exact markdown in your next message:\n[{os.path.basename(fp)}]({url})"


def tool_fetch_webpage(url: str) -> str:
    """
    Fetches the text content of a webpage via HTTP GET.
    Useful for reading external documentation, APIs, or website content.
    """
    try:
        with httpx.Client(timeout=10.0, follow_redirects=True) as client:
            response = client.get(url)
            response.raise_for_status()

            # Return text directly; if it's HTML, the LLM is usually smart enough to read the raw source.
            # Truncate at 20000 chars to avoid blowing up the context window.
            text = response.text
            return text[:20000] + ("\n...[content truncated]..." if len(text) > 20000 else "")
    except httpx.HTTPError as exc:
        return f"Error fetching {url}: {exc}"
    except Exception as e:
        return f"Unexpected error fetching {url}: {e}"


def tool_web_search(query: str) -> Dict[str, Any]:
    """
    Searches the web through Google Programmable Search Engine.
    Returns compact source metadata so agents can inspect relevant pages as needed.
    """
    search_query = (query or "").strip()
    if not search_query:
        return {"error": "Search query is required."}

    api_key = os.environ.get("GOOGLE_API_KEY", "").strip()
    cse_id = os.environ.get("GOOGLE_CSE_ID", "").strip()
    if not api_key or not cse_id:
        return {
            "error": (
                "Google search is not configured. Set GOOGLE_API_KEY and "
                "GOOGLE_CSE_ID in .env, then restart the server."
            )
        }

    try:
        with httpx.Client(timeout=10.0) as client:
            response = client.get(
                "https://www.googleapis.com/customsearch/v1",
                params={"key": api_key, "cx": cse_id, "q": search_query, "num": 5},
            )
            response.raise_for_status()
            payload = response.json()
    except httpx.HTTPStatusError as exc:
        return {"error": f"Google search request failed with HTTP {exc.response.status_code}."}
    except httpx.HTTPError as exc:
        return {"error": f"Google search request failed: {exc}"}
    except ValueError:
        return {"error": "Google search returned an invalid JSON response."}

    results = [
        {
            "title": item.get("title", ""),
            "link": item.get("link", ""),
            "snippet": item.get("snippet", ""),
        }
        for item in payload.get("items", [])
    ]
    return {"query": search_query, "results": results}


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
    "share_file": tool_share_file,
    "web_search": tool_web_search,
    "fetch_webpage": tool_fetch_webpage,
    "execute_shell_command": execute_shell_command,
    "execute_python": execute_python,
}
