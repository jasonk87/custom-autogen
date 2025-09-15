import base64
import json
import os
import pytest
from app.config import WORKSPACE_DIR
from app.server import app

# Ensure the workspace directory exists for the test
os.makedirs(WORKSPACE_DIR, exist_ok=True)

@pytest.fixture
def client():
    app.config['TESTING'] = True
    with app.test_client() as client:
        yield client

def test_e2e_mocked(client, monkeypatch):
    """
    Tests the full end-to-end flow of the application using a mocked agent stream.
    This test verifies that the main orchestration loop, tool execution, and
    file system interaction work correctly.
    """
    # 1. Define the mock stream function that simulates agent responses
    def mock_stream(self, history, model):
        # The 'self' argument is the instance of the Agent class
        if "programmer" in self.name.lower():
            yield 'I will create the `hello.py` file as requested.\n```tool:write_file\n{\n    "path": "hello.py",\n    "content": "print(\'hello world\')"\n}\n```'
        elif "reviewer" in self.name.lower():
            yield "The code looks good to me. Great job!"
        else:
            yield "This is a mock response."
        return

    # 2. Apply the mock to the Agent.stream method for the duration of this test
    monkeypatch.setattr('app.core.Agent.stream', mock_stream)

    # 3. Prepare the request payload for the /stream endpoint
    goal = "create a Python file named hello.py in the workspace with the content print('hello world')"
    agents = [{'name': 'Programmer'}, {'name': 'Reviewer'}]
    goal_b64 = base64.b64encode(goal.encode()).decode()
    agents_b64 = base64.b64encode(json.dumps(agents).encode()).decode()

    url = f"/stream?model=mock-model&manager_mode=Directed&turns=5&goal={goal_b64}&agents={agents_b64}"

    # 4. Make the request that starts the agent workflow
    response = client.get(url)
    assert response.status_code == 200

    # The agent orchestrator runs in a background thread. We need to wait for it
    # to finish its work. A simple sleep is the easiest way to do this in this case.
    import time
    time.sleep(3)

    # 5. Verify that the file was created with the correct content
    file_path = os.path.join(WORKSPACE_DIR, 'hello.py')
    assert os.path.exists(file_path), "The 'hello.py' file was not created in the workspace."

    with open(file_path, 'r') as f:
        content = f.read()
        assert content == "print('hello world')", f"File content was '{content}' instead of 'print(\\'hello world\\')'"

    # 6. Clean up the test file
    os.remove(file_path)
