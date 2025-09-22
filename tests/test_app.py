import pytest
import pytest_asyncio
from app.server import app

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
