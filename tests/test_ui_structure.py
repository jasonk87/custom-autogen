
import pytest
from app.server import app

@pytest.mark.asyncio
async def test_index_html_structure():
    client = app.test_client()
    response = await client.get("/")
    assert response.status_code == 200
    html = await response.get_data(as_text=True)

    # Check for critical sections
    assert 'id="setup"' in html
    assert 'id="chat"' in html
    assert 'id="work"' in html

    # Check for new consolidated sections
    assert '<details' in html
    assert 'Advanced Configuration' in html
    assert 'Add Manual Bot' in html

    # Check that Ollama settings are gone
    assert 'Ollama Base URL' not in html
    assert 'Probe Ollama' not in html
