import os
from unittest import mock

import httpx

from app.tools import execute_shell_command, tool_delete, tool_web_search
from app.config import WORKSPACE_DIR

def test_execute_shell_command_echo():
    """Test that the shell tool can execute a simple echo command."""
    command = "echo 'hello from test'"
    result = execute_shell_command(command)
    assert "hello from test" in result
    assert "Stdout:" in result

def test_execute_shell_command_cwd():
    """Test that the shell tool executes in the correct workspace directory."""
    # Create a unique filename for this test
    test_filename = "test_cwd_file.txt"
    # Ensure the file doesn't exist before the test
    if os.path.exists(os.path.join(WORKSPACE_DIR, test_filename)):
        os.remove(os.path.join(WORKSPACE_DIR, test_filename))

    # Use the tool to create a file in the workspace
    command = f"echo ok > {test_filename}"
    execute_shell_command(command)

    # Check that the file was created in the WORKSPACE_DIR
    assert os.path.exists(os.path.join(WORKSPACE_DIR, test_filename))

    # Clean up the created file
    os.remove(os.path.join(WORKSPACE_DIR, test_filename))

def test_execute_shell_command_error():
    """Test that the shell tool captures and returns errors."""
    command = "cd non_existent_directory_for_sure"
    result = execute_shell_command(command)
    assert "Error executing command" in result
    assert "Stderr:" in result
    assert "non_existent_directory_for_sure" in result

def test_tool_delete_rejects_workspace_root():
    assert tool_delete(".") == "Error: Refusing to delete the workspace root."


def test_tool_web_search_requires_configuration(monkeypatch):
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    monkeypatch.delenv("GOOGLE_CSE_ID", raising=False)
    result = tool_web_search("test query")
    assert "not configured" in result["error"]


def test_tool_web_search_returns_compact_results(monkeypatch):
    monkeypatch.setenv("GOOGLE_API_KEY", "test-key")
    monkeypatch.setenv("GOOGLE_CSE_ID", "test-cse")
    response = mock.Mock()
    response.raise_for_status.return_value = None
    response.json.return_value = {
        "items": [{"title": "Example", "link": "https://example.com", "snippet": "Example result"}]
    }
    with mock.patch.object(httpx.Client, "get", return_value=response):
        result = tool_web_search("test query")
    assert result == {
        "query": "test query",
        "results": [{"title": "Example", "link": "https://example.com", "snippet": "Example result"}],
    }
