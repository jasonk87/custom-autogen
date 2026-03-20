import pytest
import pytest_asyncio
from app.server import app
from app.state import state

@pytest_asyncio.fixture
async def client():
    app.config['TESTING'] = True
    async with app.test_client() as client:
        yield client

async def test_agent_run_smoke(client):
    """
    Keep endpoint tests lightweight: status + basic shape.
    The stream is already patched in conftest.py.
    """
    resp = await client.post(
        "/api/agent/run",
        json={
            "agent": {"name": "Coder", "model": "llama3.1:8b"},
            "message": "hello",
        },
    )
    assert resp.status_code == 200
    data = await resp.get_json()
    assert isinstance(data.get("reply"), str)


async def test_tools_endpoint(client):
    resp = await client.get("/api/tools")
    assert resp.status_code == 200
    data = await resp.get_json()
    assert isinstance(data, list)


async def test_coach_input_enqueues_message(client):
    while not state.coach_input_q.empty():
        state.coach_input_q.get_nowait()

    resp = await client.post("/coach_input", json={"message": "stay on track"})
    assert resp.status_code == 200
    data = await resp.get_json()
    assert data.get("ok") is True
    assert state.coach_input_q.get_nowait() == "stay on track"


async def test_coach_input_rejects_empty_message(client):
    resp = await client.post("/coach_input", json={"message": "   "})
    assert resp.status_code == 400
