import pytest
import os
import shutil
from app.server import app
from app.state import state
from app.config import WORKSPACE_DIR

@pytest.fixture
def client():
    app.config['TESTING'] = True
    return app.test_client()

@pytest.fixture(autouse=True)
def cleanup_sessions():
    yield
    # Cleanup created sessions
    sessions_path = os.path.join(WORKSPACE_DIR, "sessions")
    if os.path.exists(sessions_path):
        for session in ["test_session_a", "test_session_b"]:
            p = os.path.join(sessions_path, session)
            if os.path.exists(p):
                shutil.rmtree(p)

@pytest.mark.asyncio
async def test_session_isolation(client):
    # 1. Activate Session A
    data = {"name": "test_session_a"}
    resp = await client.post("/api/sessions/activate", json=data)
    assert resp.status_code == 200
    
    # 2. Create file in Session A
    resp = await client.post("/api/workspace/file", json={"path": "file_a.txt", "content": "Hello A"})
    assert resp.status_code == 200
    
    # Verify file exists via API
    resp = await client.get("/workspace/file_a.txt")
    assert resp.status_code == 200
    assert (await resp.data) == b"Hello A"

    # 3. Activate Session B
    data = {"name": "test_session_b"}
    resp = await client.post("/api/sessions/activate", json=data)
    assert resp.status_code == 200
    
    # 4. Verify file_a.txt is NOT accessible
    resp = await client.get("/workspace/file_a.txt")
    assert resp.status_code == 404
    
    # 5. Create file in Session B
    resp = await client.post("/api/workspace/file", json={"path": "file_b.txt", "content": "Hello B"})
    assert resp.status_code == 200
    
    # 6. Activate Session A again
    data = {"name": "test_session_a"}
    resp = await client.post("/api/sessions/activate", json=data)
    assert resp.status_code == 200
    
    # 7. Verify file_a.txt IS accessible again
    resp = await client.get("/workspace/file_a.txt")
    assert resp.status_code == 200
    assert (await resp.data) == b"Hello A"
    
    # 8. Verify file_b.txt is NOT accessible from A
    resp = await client.get("/workspace/file_b.txt")
    assert resp.status_code == 404
