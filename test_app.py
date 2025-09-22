import asyncio
import base64
import json
import os
import queue
import pytest
import pytest_asyncio
from app.config import WORKSPACE_DIR
from app.server import app

# Ensure the workspace directory exists for the test
os.makedirs(WORKSPACE_DIR, exist_ok=True)

@pytest_asyncio.fixture
async def client():
    app.config['TESTING'] = True
    async with app.test_client() as client:
        yield client

async def test_e2e_mocked(client, monkeypatch):
    """
    Tests the full end-to-end flow of the application by mocking the orchestrator thread target.
    """
    # 1. Define the mock orchestrator function
    def mock_orchestrator_thread(goal, model, agents_cfg, manager_mode, out_q, max_turns, human_proxy):
        # This function is now synchronous, as it's the target of a thread
        # It puts messages into the queue, which is thread-safe
        out_q.put(json.dumps({"type": "chat", "sender": "Programmer", "message": "I will create the file."}))

        file_path = os.path.join(WORKSPACE_DIR, 'hello.py')
        with open(file_path, 'w') as f:
            f.write("print('hello world')")

        out_q.put(json.dumps({"type": "chat", "sender": "Programmer", "message": "I have created the file."}))
        out_q.put(json.dumps({"type": "chat", "sender": "Reviewer", "message": "The code looks good."}))
        out_q.put("[DONE]")

    # 2. Apply the mock
    monkeypatch.setattr('app.server._run_orchestrator_thread', mock_orchestrator_thread)

    # 3. Prepare and make the request
    goal = "create a Python file named hello.py"
    agents = [{'name': 'Programmer'}, {'name': 'Reviewer'}]
    goal_b64 = base64.b64encode(goal.encode()).decode()
    agents_b64 = base64.b64encode(json.dumps(agents).encode()).decode()
    url = f"/stream?model=mock-model&manager_mode=auto&turns=5&goal={goal_b64}&agents={agents_b64}"

    response = await client.get(url)
    assert response.status_code == 200

    # 4. Consume the stream to ensure the background task has finished
    async for data in response.response:
        if b"[DONE]" in data:
            break

    # 5. Verify the outcome
    file_path = os.path.join(WORKSPACE_DIR, 'hello.py')
    assert os.path.exists(file_path)
    with open(file_path, 'r') as f:
        content = f.read()
        assert content == "print('hello world')"

    # 6. Clean up
    os.remove(file_path)
