
import pytest
from pathlib import Path
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
    assert 'class="chat-empty"' in html
    assert 'id="empty-open-setup"' in html
    assert 'id="stop-run"' in html
    assert 'id="resume-run"' in html
    assert 'id="jump-latest"' in html
    assert 'id="work"' in html

    # Check for new consolidated sections
    assert '<details' in html
    assert 'Advanced Configuration' in html
    assert 'Add Manual Bot' in html
    assert 'Human Proxy' in html
    assert 'Consult me' in html
    assert 'Delegate routine' in html
    assert 'Workspace autonomy' in html
    assert '<option value="smart_supervisor">Smart Supervisor</option>' in html
    assert '<option value="round_robin">Round Robin</option>' in html
    assert 'Conversation Mode' in html
    assert 'Conversation Orchestration' in html
    assert '<option value="discussion">Discussion</option>' in html
    assert '<option value="debate">Debate</option>' in html
    assert '<option value="brainstorm">Brainstorm</option>' in html
    assert '<option value="execution">Execution</option>' in html
    assert '<option value="simulation">Simulation</option>' in html
    assert '<option value="storybook">Storybook</option>' in html
    assert 'id="sessions-modal"' in html
    assert 'Scenarios and Groups' in html
    assert 'id="scenario-save"' in html
    assert 'id="group-save"' in html
    assert 'id="generate-agents" class="btn btn-primary" style="width:100%" disabled>Loading Models...</button>' in html
    assert 'id="random-scenario"' in html
    assert 'aria-label="Generate a random scenario idea"' in html
    assert 'aria-label="Generate a random scenario idea" disabled>' in html

    # Check that Ollama settings are gone
    assert 'Ollama Base URL' not in html
    assert 'Probe Ollama' not in html


def test_stream_errors_reconnect_to_active_run():
    script = Path("static/js/main.js").read_text(encoding="utf-8")
    assert "function createRunToken()" in script
    assert "typeof globalThis.crypto?.randomUUID === 'function'" in script
    assert "run_token: createRunToken()" in script
    assert "localStorage.setItem(ACTIVE_RUN_URL_KEY, url)" in script
    assert "async function reconnectActiveRun()" in script
    assert "async function resumeSimulation()" in script
    assert "function startNewTask()" in script
    assert "<button id='new-task' class='btn btn-neutral' style='width:100%'>New Task</button>" in script
    assert "/api/run/resume" in script
    assert "Simulation paused" in script
    assert "function beginNewSetup()" in script
    assert "function clearDraftSetup()" in script
    assert "$('#scenario').addEventListener('input', beginNewSetup)" in script
    assert "$('#goal').addEventListener('input', beginNewSetup)" in script
    assert "clearDraftSetup();\n    renderTeam();" in script
    assert "/api/run/status?run_token=" in script
    assert "localStorage.removeItem(ACTIVE_RUN_URL_KEY)" in script
    assert "es.close();\n        localStorage.removeItem(ACTIVE_RUN_URL_KEY);" in script
    assert "reconnectUrl.searchParams.set('resume_only', 'true')" in script
    assert "setStatus('reconnecting')" in script
    assert "Supervisor choosing next speaker..." in script
    assert "function normalizeManagerMode(mode)" in script
    assert "normalized === 'auto' || normalized === 'smartsupervisor'" in script
    assert "async function saveLibraryItem(artifactType)" in script
    assert "function normalizeSavedArtifact(file, payload)" in script
    assert "item.artifactType === 'scenario'" in script
    assert "Agent group loaded. Current task preserved." in script
    assert "$('#scenario').addEventListener('change', saveSession)" in script
    assert "loadSession();" in script
    assert "conversation_mode: $('#conversation-mode').value" in script
    assert "generateButton.textContent = 'Loading Models...'" in script
    assert "Models are still loading. Try again in a moment." in script
    assert "if (window.innerWidth <= 768) closeSidebar();" not in script
    assert "async function rollScenarioIdea()" in script
    assert "fetch('/api/scenario/idea'" in script
    assert "$('#random-scenario').onclick = rollScenarioIdea" in script
    assert "$('#random-scenario').disabled = false;" in script
    assert "function normalizeConversationMode(mode)" in script
    assert "$('#conversation-mode').addEventListener('change'" in script
    assert "const RELATIONSHIP_TYPES = [" in script
    assert "function normalizeAgents(rawAgents = [])" in script
    assert "tools_enabled: agent?.tools_enabled !== false" in script
    assert "class='relationships-panel'" in script
    assert "class='btn btn-neutral relationship-add'" in script
    assert "class='relationship-custom ${custom ? '' : 'hidden'}'" in script
    assert "class='agent-tools'" in script
    assert "relationships.splice(+el.dataset.r, 1)" in script
    assert "$$('.relationship-custom').forEach(el => {" in script
    assert "el.oninput = () => {" in script
    assert "conversation_mode: $('#conversation-mode').value" in script
