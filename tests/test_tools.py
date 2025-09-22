import os
from app.tools import execute_shell_command
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
    command = f"touch {test_filename}"
    execute_shell_command(command)

    # Check that the file was created in the WORKSPACE_DIR
    assert os.path.exists(os.path.join(WORKSPACE_DIR, test_filename))

    # Clean up the created file
    os.remove(os.path.join(WORKSPACE_DIR, test_filename))

def test_execute_shell_command_error():
    """Test that the shell tool captures and returns errors."""
    command = "ls non_existent_directory_for_sure"
    result = execute_shell_command(command)
    assert "Error executing command" in result
    assert "Stderr:" in result
    assert "No such file or directory" in result
