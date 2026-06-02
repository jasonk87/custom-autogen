import pytest
import pytest_asyncio
from unittest import mock
import io
import json
import queue
from pathlib import Path
from types import SimpleNamespace

from app.server import _run_orchestrator_thread, app
from app.state import ReplayEventBuffer, state
from app.core import OrchestratorError, run_orchestrator
from app.config import DEFAULT_MODEL, MAX_UPLOAD_BYTES, ALLOWED_UPLOAD_MIMES, SESSIONS_DIR, WORKSPACE_DIR
import os
import shutil
from werkzeug.datastructures import FileStorage


def upload_file(filename, content, content_type):
    return FileStorage(
        stream=io.BytesIO(content),
        filename=filename,
        content_type=content_type,
    )

@pytest_asyncio.fixture
async def client():
    app.config['TESTING'] = True
    async with app.test_client() as client:
        yield client

@pytest_asyncio.fixture(autouse=True)
def setup_test_environment(tmp_path):
    # Ensure a clean state for each test
    state.active_workspace = str(tmp_path)
    state.active_workspace_session = None
    state.active_workspace_persistent = False
    managed_root = Path(WORKSPACE_DIR) / "sessions"
    managed_root.mkdir(parents=True, exist_ok=True)
    existing_managed_workspaces = {item.name for item in managed_root.iterdir() if item.is_dir()}
    if os.path.exists(SESSIONS_DIR):
        shutil.rmtree(SESSIONS_DIR)
    os.makedirs(SESSIONS_DIR, exist_ok=True)
    yield
    # Clean up after tests
    if os.path.exists(SESSIONS_DIR):
        shutil.rmtree(SESSIONS_DIR)
    for item in managed_root.iterdir():
        if item.is_dir() and item.name not in existing_managed_workspaces:
            shutil.rmtree(item)

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


# --- New tests for /api/scenario/generate ---

@pytest.mark.parametrize("scenario,model,num_agents,expected_status,error_code", [
    (None, "gemini-pro", 3, 400, "invalid_scenario_input"),
    ("", "gemini-pro", 3, 400, "invalid_scenario_input"),
    (123, "gemini-pro", 3, 400, "invalid_scenario_input"),
    ("test scenario", 123, 3, 400, "invalid_model_input"),
    ("test scenario", "gemini-pro", "abc", 400, "invalid_num_agents_input"),
    ("test scenario", "gemini-pro", 0, 400, "invalid_num_agents_input"),
    ("test scenario", "gemini-pro", -1, 400, "invalid_num_agents_input"),
])
async def test_scenario_generate_input_validation(client, scenario, model, num_agents, expected_status, error_code):
    json_data = {"scenario": scenario, "model": model, "num_agents": num_agents}
    resp = await client.post("/api/scenario/generate", json=json_data)
    assert resp.status_code == expected_status
    data = await resp.get_json()
    assert data.get("error_code") == error_code

async def test_scenario_generate_success(client):
    with mock.patch('app.server.generate_agents_from_scenario') as mock_generate:
        mock_generate.return_value = ([{"name": "Agent1"}], "Suggested Goal", "execution")
        resp = await client.post("/api/scenario/generate", json={
            "scenario": "create a website",
            "model": "gemini-pro",
            "num_agents": 2
        })
        assert resp.status_code == 200
        data = await resp.get_json()
        assert data.get("agents") == [{"name": "Agent1"}]
        assert data.get("goal") == "Suggested Goal"
        assert data.get("conversation_mode") == "execution"
        mock_generate.assert_called_once_with("create a website", "gemini-pro", 2, "discussion")


@pytest.mark.parametrize("model", [None, ""])
async def test_scenario_generate_defaults_missing_or_empty_model(client, model):
    with mock.patch("app.server.generate_agents_from_scenario") as mock_generate:
        mock_generate.return_value = ([{"name": "Agent1"}], "Suggested Goal", "discussion")
        resp = await client.post(
            "/api/scenario/generate",
            json={"scenario": "create a website", "model": model, "num_agents": 2},
        )

    assert resp.status_code == 200
    mock_generate.assert_called_once_with("create a website", DEFAULT_MODEL, 2, "discussion")


async def test_scenario_idea_uses_flash_lite_regardless_of_selected_model(client):
    with mock.patch("app.server.generate_scenario_idea") as mock_generate:
        mock_generate.return_value = "A treasure hunt inside a drifting space station."
        resp = await client.post("/api/scenario/idea", json={"model": "gemini::cloud::gemini-2.5-pro"})

    assert resp.status_code == 200
    assert (await resp.get_json())["scenario"] == "A treasure hunt inside a drifting space station."
    mock_generate.assert_awaited_once_with("gemini::cloud::gemini-2.5-flash-lite")


async def test_scenario_idea_ignores_invalid_selected_model(client):
    with mock.patch("app.server.generate_scenario_idea") as mock_generate:
        mock_generate.return_value = "A quick scenario idea."
        resp = await client.post("/api/scenario/idea", json={"model": 123})

    assert resp.status_code == 200
    mock_generate.assert_awaited_once_with("gemini::cloud::gemini-2.5-flash-lite")


async def test_api_models_returns_cloud_catalog_without_waiting_for_ollama(client):
    with (
        mock.patch("app.server.get_cloud_models", return_value=[{"value": "gemini::cloud::fast"}]) as cloud,
        mock.patch("app.server.get_ollama_models") as ollama,
    ):
        resp = await client.get("/api/models")

    assert resp.status_code == 200
    assert await resp.get_json() == [{"value": "gemini::cloud::fast"}]
    cloud.assert_called_once_with()
    ollama.assert_not_called()


async def test_api_ollama_models_returns_background_catalog(client):
    with mock.patch(
        "app.server.get_ollama_models",
        new=mock.AsyncMock(return_value=[{"value": "ollama::local::llama"}]),
    ) as ollama:
        resp = await client.get("/api/models/ollama")

    assert resp.status_code == 200
    assert await resp.get_json() == [{"value": "ollama::local::llama"}]
    ollama.assert_awaited_once_with()


async def test_scenario_generate_retries_transient_provider_error(client):
    with (
        mock.patch('app.server.generate_agents_from_scenario') as mock_generate,
        mock.patch('app.server.asyncio.sleep', new=mock.AsyncMock()) as mock_sleep,
    ):
        mock_generate.side_effect = [
            RuntimeError("503 UNAVAILABLE: model is experiencing high demand"),
            ([{"name": "Agent1"}], "Suggested Goal", "discussion"),
        ]
        resp = await client.post("/api/scenario/generate", json={
            "scenario": "create a website",
            "model": "gemini-pro",
            "num_agents": 1,
        })

    assert resp.status_code == 200
    assert (await resp.get_json())["goal"] == "Suggested Goal"
    assert mock_generate.await_count == 2
    mock_sleep.assert_awaited_once()


async def test_scenario_generate_reports_exhausted_transient_provider_error(client):
    with (
        mock.patch('app.server.generate_agents_from_scenario') as mock_generate,
        mock.patch('app.server.asyncio.sleep', new=mock.AsyncMock()),
    ):
        mock_generate.side_effect = RuntimeError("503 UNAVAILABLE: model is experiencing high demand")
        resp = await client.post("/api/scenario/generate", json={
            "scenario": "create a website",
            "model": "gemini-pro",
            "num_agents": 1,
        })

    assert resp.status_code == 503
    payload = await resp.get_json()
    assert payload["error_code"] == "model_temporarily_unavailable"
    assert mock_generate.await_count == 3


async def test_scenario_generate_orchestrator_error(client):
    with mock.patch('app.server.generate_agents_from_scenario') as mock_generate:
        mock_generate.side_effect = OrchestratorError("User message", "Dev message", "test_error")
        resp = await client.post("/api/scenario/generate", json={
            "scenario": "create a website",
            "model": "gemini-pro",
            "num_agents": 2
        })
        assert resp.status_code == 500
        data = await resp.get_json()
        assert data.get("error_code") == "test_error"
        assert data.get("user_message") == "User message"

async def test_scenario_generate_unexpected_error(client):
    with mock.patch('app.server.generate_agents_from_scenario') as mock_generate:
        mock_generate.side_effect = ValueError("Something went wrong")
        resp = await client.post("/api/scenario/generate", json={
            "scenario": "create a website",
            "model": "gemini-pro",
            "num_agents": 2
        })
        assert resp.status_code == 500
        data = await resp.get_json()
        assert data.get("error_code") == "orchestrator_error"
        assert "unexpected error" in data.get("user_message").lower()

async def test_scenario_generate_reports_expired_gemini_key(client):
    with mock.patch('app.server.generate_agents_from_scenario') as mock_generate:
        mock_generate.side_effect = ValueError("API key expired. Please renew the API key. API_KEY_INVALID")
        resp = await client.post("/api/scenario/generate", json={
            "scenario": "create a website",
            "model": "gemini-pro",
            "num_agents": 2
        })
        assert resp.status_code == 500
        data = await resp.get_json()
        assert data.get("error_code") == "gemini_api_key_expired"
        assert "expired" in data.get("user_message").lower()
        assert "GEMINI_API_KEY" in data.get("user_message")

# --- New tests for /api/agent/run ---

@pytest.mark.parametrize("agent_config,message,model,expected_status,error_code", [
    (None, "hello", "gemini-pro", 400, "invalid_agent_config_input"),
    ({"name": "Coder"}, None, "gemini-pro", 400, "invalid_message_input"),
    ({"name": "Coder"}, "hello", None, 400, "invalid_model_input"),
    ("invalid", "hello", "gemini-pro", 400, "invalid_agent_config_input"),
    ({"name": "Coder"}, 123, "gemini-pro", 400, "invalid_message_input"),
    ({"name": "Coder"}, "hello", 123, 400, "invalid_model_input"),
])
async def test_agent_run_input_validation(client, agent_config, message, model, expected_status, error_code):
    json_data = {"agent": agent_config, "message": message, "model": model}
    resp = await client.post("/api/agent/run", json=json_data)
    assert resp.status_code == expected_status
    data = await resp.get_json()
    assert data.get("error_code") == error_code

async def test_agent_run_orchestrator_error(client):
    with (
        mock.patch.dict(app.config, {'TESTING': False}),
        mock.patch('app.server.get_model_client') as mock_get_model_client,
        mock.patch('app.server.AssistantAgent') as mock_agent,
    ):
        mock_get_model_client.return_value = mock.Mock()
        mock_agent.return_value.run = mock.AsyncMock(
            side_effect=OrchestratorError("Agent run failed", "Dev details", "agent_run_test_error")
        )
        resp = await client.post("/api/agent/run", json={
            "agent": {"name": "Coder", "system_message": "You are a coder.", "tools": []},
            "message": "Write a python script.",
            "model": "gemini-pro"
        })
        assert resp.status_code == 500
        data = await resp.get_json()
        assert data.get("error_code") == "agent_run_test_error"
        assert data.get("user_message") == "Agent run failed"

async def test_agent_run_unexpected_error(client):
    with (
        mock.patch.dict(app.config, {'TESTING': False}),
        mock.patch('app.server.get_model_client') as mock_get_model_client,
        mock.patch('app.server.AssistantAgent') as mock_agent,
    ):
        mock_get_model_client.return_value = mock.Mock()
        mock_agent.return_value.run = mock.AsyncMock(side_effect=ValueError("Internal agent error"))
        resp = await client.post("/api/agent/run", json={
            "agent": {"name": "Coder", "system_message": "You are a coder.", "tools": []},
            "message": "Write a python script.",
            "model": "gemini-pro"
        })
        assert resp.status_code == 500
        data = await resp.get_json()
        assert data.get("error_code") == "orchestrator_error"
        assert "unexpected error" in data.get("user_message").lower()

async def test_agent_run_reflects_after_tool_use(client):
    with (
        mock.patch.dict(app.config, {'TESTING': False}),
        mock.patch('app.server.get_model_client', return_value=mock.Mock()),
        mock.patch('app.server.AssistantAgent') as mock_agent,
    ):
        mock_agent.return_value.run = mock.AsyncMock(
            return_value=SimpleNamespace(messages=[SimpleNamespace(content="I found a useful result.")])
        )
        resp = await client.post("/api/agent/run", json={
            "agent": {"name": "Finder", "system_message": "Search for evidence.", "tools": ["web_search"]},
            "message": "Find a source.",
            "model": "gemini-pro"
        })

    assert resp.status_code == 200
    assert (await resp.get_json())["reply"] == "I found a useful result."
    assert mock_agent.call_args.kwargs["reflect_on_tool_use"] is True
    assert "follow the tool result with a concise message" in mock_agent.call_args.kwargs["system_message"]

# --- New tests for _run_orchestrator_thread error handling ---

async def test_run_orchestrator_thread_orchestrator_error(client):
    q = queue.Queue()
    error = OrchestratorError("Orchestrator thread failed", "Dev details", "thread_test_error")
    with (
        mock.patch('app.server.run_orchestrator', new=mock.Mock(return_value=None)),
        mock.patch('app.server.asyncio.run', side_effect=error),
    ):
        _run_orchestrator_thread("", "", [], "auto", q, 1, "off", "", 0.3, True)

        payload = json.loads(q.get_nowait())
        assert payload.get("error_code") == "thread_test_error"
        assert payload.get("user_message") == "Orchestrator thread failed"
        assert q.get_nowait() == "[DONE]"

async def test_run_orchestrator_thread_unexpected_error(client):
    q = queue.Queue()
    with (
        mock.patch('app.server.run_orchestrator', new=mock.Mock(return_value=None)),
        mock.patch('app.server.asyncio.run', side_effect=ValueError("Unexpected thread issue")),
    ):
        _run_orchestrator_thread("", "", [], "auto", q, 1, "off", "", 0.3, True)

        payload = json.loads(q.get_nowait())
        assert payload.get("error_code") == "orchestrator_error"
        assert "unexpected error" in payload.get("user_message").lower()
        assert q.get_nowait() == "[DONE]"


async def test_run_orchestrator_thread_forwards_conversation_mode(client):
    q = queue.Queue()
    with (
        mock.patch("app.server.run_orchestrator", return_value="orchestrator-call") as mock_run,
        mock.patch("app.server.asyncio.run") as mock_asyncio_run,
    ):
        _run_orchestrator_thread("", "", [], "auto", q, 1, "off", "", 0.3, True, "execution")

    assert mock_run.call_args.args[-1] == "execution"
    mock_asyncio_run.assert_called_once()
    mock_asyncio_run.call_args.args[0].close()


async def test_stream_rejects_second_connection_while_run_is_active(client):
    with mock.patch("app.server.state.start", side_effect=RuntimeError("already active")):
        resp = await client.get("/stream?run_token=reconnect-token")

    assert resp.status_code == 409
    payload = await resp.get_json()
    assert payload["error_code"] == "run_already_active"


async def test_stream_reconnect_replays_buffer_without_starting_new_run(client):
    buffer = ReplayEventBuffer()
    buffer.put(json.dumps({"type": "chat", "sender": "Agent", "message": "missed update"}))
    buffer.put("[DONE]")
    with (
        mock.patch("app.server.state.reconnect_buffer", return_value=buffer),
        mock.patch("app.server.state.start") as mock_start,
    ):
        resp = await client.get("/stream?run_token=active-token")

    assert resp.status_code == 200
    content = (await resp.get_data()).decode("utf-8")
    assert "id: 1" in content
    assert "missed update" in content
    assert "id: 2" in content
    assert "[DONE]" in content
    mock_start.assert_not_called()


async def test_stream_reconnect_resumes_after_last_event_id(client):
    buffer = ReplayEventBuffer()
    buffer.put(json.dumps({"type": "chat", "sender": "Agent", "message": "already seen"}))
    buffer.put(json.dumps({"type": "chat", "sender": "Agent", "message": "missed update"}))
    buffer.put("[DONE]")
    with mock.patch("app.server.state.reconnect_buffer", return_value=buffer):
        resp = await client.get(
            "/stream?run_token=active-token",
            headers={"Last-Event-ID": "1"},
        )

    content = (await resp.get_data()).decode("utf-8")
    assert "already seen" not in content
    assert "id: 2" in content
    assert "missed update" in content
    assert "id: 3" in content
    assert "[DONE]" in content


async def test_stream_resume_only_does_not_start_missing_run(client):
    with mock.patch("app.server.state.start") as mock_start:
        resp = await client.get("/stream?run_token=stale-token&resume_only=true")

    assert resp.status_code == 404
    payload = await resp.get_json()
    assert payload["error_code"] == "run_not_found"
    mock_start.assert_not_called()


async def test_run_status_reports_buffer_availability(client):
    buffer = ReplayEventBuffer()
    with mock.patch("app.server.state.reconnect_buffer", return_value=buffer):
        resp = await client.get("/api/run/status?run_token=active-token")

    assert resp.status_code == 200
    assert (await resp.get_json())["available"] is True


async def test_resume_run_restarts_stopped_simulation_with_transcript(client):
    buffer = ReplayEventBuffer()
    buffer.put(json.dumps({"type": "chat", "sender": "Researcher", "message": "Found the water source."}))
    previous_args = ("Find water.", "model", [], "round_robin", buffer, 5, "off", "", 0.3, False, "simulation")
    with (
        mock.patch(
            "app.server.state.prepare_resume",
            return_value=(mock.sentinel.target, previous_args, buffer, "Researcher: Found the water source."),
        ),
        mock.patch("app.server.state.start") as mock_start,
    ):
        resp = await client.post("/api/run/resume", json={"run_token": "stopped-token"})

    assert resp.status_code == 200
    resumed_args = mock_start.call_args.args[1]
    assert "Resume the existing simulation" in resumed_args[0]
    assert "Researcher: Found the water source." in resumed_args[0]
    assert resumed_args[4] is buffer
    assert any("Simulation resumed." in item for _, item in buffer.read_after(0, timeout=0))


async def test_resume_run_rejects_missing_stopped_simulation(client):
    with mock.patch("app.server.state.prepare_resume", side_effect=RuntimeError("missing")):
        resp = await client.post("/api/run/resume", json={"run_token": "missing-token"})

    assert resp.status_code == 409
    assert (await resp.get_json())["error_code"] == "run_not_resumable"


def test_replay_event_buffer_reads_only_events_after_cursor():
    buffer = ReplayEventBuffer()
    buffer.put("first")
    buffer.put("second")
    assert buffer.read_after(0, timeout=0) == [(1, "first"), (2, "second")]
    assert buffer.read_after(1, timeout=0) == [(2, "second")]


def test_replay_event_buffer_copies_transcript_without_done_marker():
    buffer = ReplayEventBuffer()
    buffer.put(json.dumps({"type": "chat", "sender": "Agent", "message": "Continue from here."}))
    buffer.put("[DONE]")
    copy = buffer.copy_without_done()

    assert copy.read_after(0, timeout=0) == [
        (1, json.dumps({"type": "chat", "sender": "Agent", "message": "Continue from here."}))
    ]
    assert buffer.chat_transcript() == "Agent: Continue from here."


# --- New tests for /api/workspace/upload ---

async def test_api_upload_no_file(client):
    resp = await client.post("/api/workspace/upload")
    assert resp.status_code == 400
    data = await resp.get_json()
    assert data.get("error_code") == "missing_file"

async def test_api_upload_file_too_large(client):
    large_content = b"a" * (MAX_UPLOAD_BYTES + 1)
    resp = await client.post("/api/workspace/upload", files={'file': upload_file('large.txt', large_content, 'text/plain')})
    assert resp.status_code == 413
    data = await resp.get_json()
    assert data.get("error_code") == "too_large"

async def test_api_upload_mime_blocked(client):
    # Assuming .exe is not in ALLOWED_UPLOAD_MIMES and not a whitelisted extension
    resp = await client.post("/api/workspace/upload", files={'file': upload_file('malicious.exe', b'content', 'application/x-msdownload')})
    assert resp.status_code == 415
    data = await resp.get_json()
    assert data.get("error_code") == "mime_blocked"

async def test_api_upload_permission_error(client):
    with mock.patch('app.server._resolve_workspace_path') as mock_resolve_path:
        mock_resolve_path.side_effect = PermissionError("access_denied")
        resp = await client.post("/api/workspace/upload", files={'file': upload_file('test.txt', b'content', 'text/plain')})
        assert resp.status_code == 403
        data = await resp.get_json()
        assert data.get("error_code") == "access_denied"

async def test_api_upload_os_error(client):
    with mock.patch('os.makedirs') as mock_makedirs:
        mock_makedirs.side_effect = OSError("Disk full")
        resp = await client.post("/api/workspace/upload", files={'file': upload_file('test.txt', b'content', 'text/plain')})
        assert resp.status_code == 500
        data = await resp.get_json()
        assert data.get("error_code") == "file_save_error"

async def test_api_upload_success(client):
    test_file_content = b"hello world"
    test_file_name = "upload_test.txt"
    resp = await client.post("/api/workspace/upload", files={'file': upload_file(test_file_name, test_file_content, 'text/plain')})
    assert resp.status_code == 200
    data = await resp.get_json()
    assert data.get("ok") is True
    assert data.get("file") == test_file_name
    assert data.get("bytes") == len(test_file_content)
    # Verify file exists in workspace
    with open(os.path.join(state.active_workspace, test_file_name), "rb") as f:
        assert f.read() == test_file_content

# --- New tests for /api/workspace/delete ---

async def test_api_delete_file_missing_path(client):
    resp = await client.delete("/api/workspace/delete", json={})
    assert resp.status_code == 400
    data = await resp.get_json()
    assert data.get("error_code") == "missing_path"

async def test_api_delete_file_permission_error(client):
    with mock.patch('app.server._resolve_workspace_path') as mock_resolve_path:
        mock_resolve_path.side_effect = PermissionError("access_denied")
        resp = await client.delete("/api/workspace/delete", json={'path': 'secret.txt'})
        assert resp.status_code == 403
        data = await resp.get_json()
        assert data.get("error_code") == "access_denied"

async def test_api_delete_file_not_found(client):
    resp = await client.delete("/api/workspace/delete", json={'path': 'non_existent.txt'})
    assert resp.status_code == 404
    data = await resp.get_json()
    assert data.get("error_code") == "not_found"

async def test_api_delete_file_rejects_workspace_root(client):
    resp = await client.delete("/api/workspace/delete", json={'path': '.'})
    assert resp.status_code == 400
    data = await resp.get_json()
    assert data.get("error_code") == "workspace_root_delete_blocked"

async def test_api_delete_file_os_error(client):
    test_file_name = "delete_me.txt"
    with open(os.path.join(state.active_workspace or app.static_folder, test_file_name), "w") as f:
        f.write("content")
    with mock.patch('os.remove') as mock_remove:
        mock_remove.side_effect = OSError("Disk error")
        resp = await client.delete("/api/workspace/delete", json={'path': test_file_name})
        assert resp.status_code == 500
        data = await resp.get_json()
        assert data.get("error_code") == "file_delete_error"

async def test_api_delete_file_success(client):
    test_file_name = "delete_me_success.txt"
    file_path = os.path.join(state.active_workspace or app.static_folder, test_file_name)
    with open(file_path, "w") as f:
        f.write("content")
    assert os.path.exists(file_path)

    resp = await client.delete("/api/workspace/delete", json={'path': test_file_name})
    assert resp.status_code == 200
    data = await resp.get_json()
    assert data.get("ok") is True
    assert not os.path.exists(file_path)

# --- New tests for /api/workspace/set_directory ---

async def test_api_set_directory_missing_path(client):
    resp = await client.post("/api/workspace/set_directory", json={})
    assert resp.status_code == 400
    data = await resp.get_json()
    assert data.get("error_code") == "missing_path"

async def test_api_set_directory_not_found(client):
    resp = await client.post("/api/workspace/set_directory", json={'path': 'non_existent_dir'})
    assert resp.status_code == 404
    data = await resp.get_json()
    assert data.get("error_code") == "not_found"

async def test_api_set_directory_not_a_directory(client):
    test_file_name = "not_a_dir.txt"
    file_path = os.path.join(state.active_workspace or app.static_folder, test_file_name)
    with open(file_path, "w") as f:
        f.write("content")
    resp = await client.post("/api/workspace/set_directory", json={'path': file_path})
    assert resp.status_code == 400
    data = await resp.get_json()
    assert data.get("error_code") == "not_a_directory"

async def test_api_set_directory_success(client):
    test_dir = os.path.join(SESSIONS_DIR, "new_workspace_dir")
    os.makedirs(test_dir, exist_ok=True)
    resp = await client.post("/api/workspace/set_directory", json={'path': test_dir})
    assert resp.status_code == 200
    data = await resp.get_json()
    assert data.get("ok") is True
    assert data.get("workspace") == test_dir
    assert state.active_workspace == test_dir

# --- New tests for /api/workspace/new_folder ---

async def test_api_new_folder_missing_path(client):
    resp = await client.post("/api/workspace/new_folder", json={})
    assert resp.status_code == 400
    data = await resp.get_json()
    assert data.get("error_code") == "missing_path"

async def test_api_new_folder_permission_error(client):
    with mock.patch('app.server._resolve_workspace_path') as mock_resolve_path:
        mock_resolve_path.side_effect = PermissionError("access_denied")
        resp = await client.post("/api/workspace/new_folder", json={'path': 'forbidden_dir'})
        assert resp.status_code == 403
        data = await resp.get_json()
        assert data.get("error_code") == "access_denied"

async def test_api_new_folder_os_error(client):
    with mock.patch('os.makedirs') as mock_makedirs:
        mock_makedirs.side_effect = OSError("Cannot create directory")
        resp = await client.post("/api/workspace/new_folder", json={'path': 'new_dir'})
        assert resp.status_code == 500
        data = await resp.get_json()
        assert data.get("error_code") == "folder_creation_error"

async def test_api_new_folder_success(client):
    new_folder_name = "my_new_folder"
    new_folder_path = os.path.join(state.active_workspace or app.static_folder, new_folder_name)
    resp = await client.post("/api/workspace/new_folder", json={'path': new_folder_name})
    assert resp.status_code == 200
    data = await resp.get_json()
    assert data.get("ok") is True
    assert os.path.isdir(new_folder_path)

# --- New tests for /api/workspace/file (POST) ---

@pytest.mark.parametrize("path,content,expected_status,error_code", [
    (None, "some content", 400, "missing_path"),
    ("", "some content", 400, "missing_path"),
    (123, "some content", 400, "missing_path"),
    ("test.txt", None, 400, "missing_content"),
    ("test.txt", 123, 400, "missing_content"),
])
async def test_api_save_file_input_validation(client, path, content, expected_status, error_code):
    json_data = {"path": path, "content": content}
    resp = await client.post("/api/workspace/file", json=json_data)
    assert resp.status_code == expected_status
    data = await resp.get_json()
    assert data.get("error_code") == error_code

async def test_api_save_file_permission_error(client):
    with mock.patch('app.server._resolve_workspace_path') as mock_resolve_path:
        mock_resolve_path.side_effect = PermissionError("access_denied")
        resp = await client.post("/api/workspace/file", json={'path': 'forbidden.txt', 'content': 'test'})
        assert resp.status_code == 403
        data = await resp.get_json()
        assert data.get("error_code") == "access_denied"

async def test_api_save_file_os_error(client):
    with mock.patch('builtins.open', side_effect=OSError("Disk full")):
        resp = await client.post("/api/workspace/file", json={'path': 'error.txt', 'content': 'test'})
        assert resp.status_code == 500
        data = await resp.get_json()
        assert data.get("error_code") == "file_save_error"

async def test_api_save_file_success(client):
    file_name = "saved_file.txt"
    file_content = "This is some content."
    file_path = os.path.join(state.active_workspace or app.static_folder, file_name)
    resp = await client.post("/api/workspace/file", json={'path': file_name, 'content': file_content})
    assert resp.status_code == 200
    data = await resp.get_json()
    assert data.get("ok") is True
    with open(file_path, "r", encoding="utf-8") as f:
        assert f.read() == file_content

# --- New tests for /api/workspace/file (GET) ---

@pytest.mark.parametrize("path,expected_status,error_code", [
    (None, 400, "missing_path"),
    ("", 400, "missing_path"),
])
async def test_api_get_file_input_validation(client, path, expected_status, error_code):
    query_string = {} if path is None else {"path": path}
    resp = await client.get("/api/workspace/file", query_string=query_string)
    assert resp.status_code == expected_status
    data = await resp.get_json()
    assert data.get("error_code") == error_code

async def test_api_get_file_permission_error(client):
    with mock.patch('app.server._resolve_workspace_path') as mock_resolve_path:
        mock_resolve_path.side_effect = PermissionError("access_denied")
        resp = await client.get("/api/workspace/file?path=forbidden.txt")
        assert resp.status_code == 403
        data = await resp.get_json()
        assert data.get("error_code") == "access_denied"

async def test_api_get_file_not_found(client):
    resp = await client.get("/api/workspace/file?path=non_existent.txt")
    assert resp.status_code == 404
    data = await resp.get_json()
    assert data.get("error_code") == "not_found"

async def test_api_get_file_os_error(client):
    file_name = "read_error.txt"
    file_path = os.path.join(state.active_workspace or app.static_folder, file_name)
    with open(file_path, "w") as f:
        f.write("content")
    with mock.patch('builtins.open', side_effect=OSError("Read error")):
        resp = await client.get(f"/api/workspace/file?path={file_name}")
        assert resp.status_code == 500
        data = await resp.get_json()
        assert data.get("error_code") == "file_read_error"

async def test_api_get_file_success(client):
    file_name = "get_file_success.txt"
    file_content = "Content to retrieve."
    file_path = os.path.join(state.active_workspace or app.static_folder, file_name)
    with open(file_path, "w", encoding="utf-8") as f:
        f.write(file_content)

    resp = await client.get(f"/api/workspace/file?path={file_name}")
    assert resp.status_code == 200
    assert await resp.get_data(as_text=True) == file_content

# --- New tests for /api/workspace/rename ---

@pytest.mark.parametrize("old_path,new_path,expected_status,error_code", [
    (None, "new.txt", 400, "missing_old_path"),
    ("old.txt", None, 400, "missing_new_path"),
    ("", "new.txt", 400, "missing_old_path"),
    ("old.txt", "", 400, "missing_new_path"),
    (123, "new.txt", 400, "missing_old_path"),
    ("old.txt", 123, 400, "missing_new_path"),
])
async def test_api_rename_file_input_validation(client, old_path, new_path, expected_status, error_code):
    json_data = {"old_path": old_path, "new_path": new_path}
    resp = await client.post("/api/workspace/rename", json=json_data)
    assert resp.status_code == expected_status
    data = await resp.get_json()
    assert data.get("error_code") == error_code

async def test_api_rename_file_permission_error(client):
    with mock.patch('app.server._resolve_workspace_path') as mock_resolve_path:
        mock_resolve_path.side_effect = PermissionError("access_denied")
        resp = await client.post("/api/workspace/rename", json={'old_path': 'old.txt', 'new_path': 'new.txt'})
        assert resp.status_code == 403
        data = await resp.get_json()
        assert data.get("error_code") == "access_denied"

async def test_api_rename_file_not_found(client):
    resp = await client.post("/api/workspace/rename", json={'old_path': 'non_existent_old.txt', 'new_path': 'new.txt'})
    assert resp.status_code == 404
    data = await resp.get_json()
    assert data.get("error_code") == "not_found"

async def test_api_rename_file_os_error(client):
    old_file_name = "rename_old.txt"
    old_file_path = os.path.join(state.active_workspace or app.static_folder, old_file_name)
    with open(old_file_path, "w") as f:
        f.write("content")
    with mock.patch('os.rename') as mock_rename:
        mock_rename.side_effect = OSError("Rename failed")
        resp = await client.post("/api/workspace/rename", json={'old_path': old_file_name, 'new_path': 'rename_new.txt'})
        assert resp.status_code == 500
        data = await resp.get_json()
        assert data.get("error_code") == "file_rename_error"

async def test_api_rename_file_success(client):
    old_file_name = "old_name.txt"
    new_file_name = "new_name.txt"
    old_file_path = os.path.join(state.active_workspace or app.static_folder, old_file_name)
    new_file_path = os.path.join(state.active_workspace or app.static_folder, new_file_name)
    with open(old_file_path, "w") as f:
        f.write("content")
    assert os.path.exists(old_file_path)
    assert not os.path.exists(new_file_path)

    resp = await client.post("/api/workspace/rename", json={'old_path': old_file_name, 'new_path': new_file_name})
    assert resp.status_code == 200
    data = await resp.get_json()
    assert data.get("ok") is True
    assert not os.path.exists(old_file_path)
    assert os.path.exists(new_file_path)

# --- New tests for /api/sessions/activate ---

async def test_sessions_activate_missing_name(client):
    resp = await client.post("/api/sessions/activate", json={})
    assert resp.status_code == 400
    data = await resp.get_json()
    assert data.get("error_code") == "missing_session_name"

async def test_sessions_activate_permission_error(client):
    with mock.patch('app.server.Path') as mock_path:
        mock_path.return_value.resolve.return_value = Path("/forbidden/path")
        resp = await client.post("/api/sessions/activate", json={'name': 'test_session'})
        assert resp.status_code == 403
        data = await resp.get_json()
        assert data.get("error_code") == "access_denied"

async def test_sessions_activate_os_error(client):
    with mock.patch('pathlib.Path.mkdir') as mock_mkdir:
        mock_mkdir.side_effect = OSError("Cannot create session directory")
        resp = await client.post("/api/sessions/activate", json={'name': 'test_session'})
        assert resp.status_code == 500
        data = await resp.get_json()
        assert data.get("error_code") == "session_activation_error"

async def test_sessions_activate_success(client):
    session_name = "my_new_session"
    resp = await client.post("/api/sessions/activate", json={'name': session_name})
    assert resp.status_code == 200
    data = await resp.get_json()
    assert data.get("ok") is True
    assert data.get("session") == session_name
    expected_path = str((Path(WORKSPACE_DIR) / "sessions" / session_name).resolve())
    assert data.get("workspace") == expected_path
    assert state.active_workspace == expected_path


async def test_scenario_workspace_activation_replaces_disposable_workspace(client):
    first = await client.post("/api/workspace/scenario", json={})
    assert first.status_code == 200
    first_data = await first.get_json()
    first_path = Path(first_data["workspace"])
    (first_path / "draft.txt").write_text("temporary", encoding="utf-8")

    second = await client.post("/api/workspace/scenario", json={})
    assert second.status_code == 200
    second_data = await second.get_json()

    assert second_data["workspace_session"] != first_data["workspace_session"]
    assert not first_path.exists()
    assert Path(second_data["workspace"]).exists()


async def test_saved_scenario_retains_active_workspace(client):
    activated = await client.post("/api/workspace/scenario", json={})
    workspace = await activated.get_json()
    resp = await client.post(
        "/api/sessions",
        json={"name": "retained", "artifact_type": "scenario", "agents": [{"name": "A"}]},
    )

    assert resp.status_code == 201
    saved = json.loads((Path(SESSIONS_DIR) / "scenario_retained.json").read_text(encoding="utf-8"))
    assert saved["workspace_session"] == workspace["workspace_session"]
    assert state.active_workspace_persistent is True

# --- New tests for /api/sessions (GET) ---

async def test_sessions_list_not_found(client):
    with mock.patch('os.listdir') as mock_listdir:
        mock_listdir.side_effect = FileNotFoundError
        resp = await client.get("/api/sessions")
        assert resp.status_code == 404
        data = await resp.get_json()
        assert data.get("error_code") == "sessions_dir_not_found"

async def test_sessions_list_os_error(client):
    with mock.patch('os.listdir') as mock_listdir:
        mock_listdir.side_effect = OSError("Disk error")
        resp = await client.get("/api/sessions")
        assert resp.status_code == 500
        data = await resp.get_json()
        assert data.get("error_code") == "sessions_list_error"

async def test_sessions_list_success(client):
    session_files = ["session1.json", "session2.json"]
    for sf in session_files:
        with open(os.path.join(SESSIONS_DIR, sf), "w") as f:
            f.write("{}")
    resp = await client.get("/api/sessions")
    assert resp.status_code == 200
    data = await resp.get_json()
    assert data == sorted(session_files)

# --- New tests for /api/sessions/<path:name> (GET) ---

async def test_sessions_get_not_found(client):
    resp = await client.get("/api/sessions/non_existent.json")
    assert resp.status_code == 404
    data = await resp.get_json()
    assert data.get("error_code") == "session_not_found"

async def test_sessions_get_invalid_json(client):
    session_name = "invalid.json"
    with open(os.path.join(SESSIONS_DIR, session_name), "w") as f:
        f.write("invalid json")
    resp = await client.get(f"/api/sessions/{session_name}")
    assert resp.status_code == 500
    data = await resp.get_json()
    assert data.get("error_code") == "invalid_session_format"

async def test_sessions_get_os_error(client):
    session_name = "error.json"
    with open(os.path.join(SESSIONS_DIR, session_name), "w") as f:
        f.write("{}")
    with mock.patch('builtins.open', side_effect=OSError("Read error")):
        resp = await client.get(f"/api/sessions/{session_name}")
        assert resp.status_code == 500
        data = await resp.get_json()
        assert data.get("error_code") == "session_retrieval_error"

async def test_sessions_get_success(client):
    session_name = "valid.json"
    session_content = {"key": "value"}
    with open(os.path.join(SESSIONS_DIR, session_name), "w") as f:
        json.dump(session_content, f)
    resp = await client.get(f"/api/sessions/{session_name}")
    assert resp.status_code == 200
    data = await resp.get_json()
    assert data == session_content

# --- New tests for /api/sessions (POST) ---

async def test_sessions_save_missing_name(client):
    resp = await client.post("/api/sessions", json={})
    assert resp.status_code == 400
    data = await resp.get_json()
    assert data.get("error_code") == "missing_session_name"

async def test_sessions_save_os_error(client):
    with mock.patch('builtins.open', side_effect=OSError("Write error")):
        resp = await client.post("/api/sessions", json={'name': 'test_session', 'data': {}})
        assert resp.status_code == 500
        data = await resp.get_json()
        assert data.get("error_code") == "session_save_error"

async def test_sessions_save_success(client):
    session_name = "new_session"
    session_data = {"agents": [{"name": "AgentX"}]}
    resp = await client.post("/api/sessions", json={'name': session_name, 'data': session_data})
    assert resp.status_code == 201
    data = await resp.get_json()
    assert data.get("ok") is True
    assert os.path.exists(os.path.join(SESSIONS_DIR, f"{session_name}.json"))

async def test_sessions_save_typed_artifacts_do_not_overwrite_each_other(client):
    shared_name = "support_team"
    scenario = {
        "name": shared_name,
        "artifact_type": "scenario",
        "scenario": "Handle a billing escalation.",
        "goal": "Resolve the billing issue.",
        "agents": [{"name": "BillingLead"}],
    }
    group = {
        "name": shared_name,
        "artifact_type": "group",
        "agents": [{"name": "BillingLead"}],
    }

    scenario_resp = await client.post("/api/sessions", json=scenario)
    group_resp = await client.post("/api/sessions", json=group)

    assert scenario_resp.status_code == 201
    assert group_resp.status_code == 201
    assert os.path.exists(os.path.join(SESSIONS_DIR, "scenario_support_team.json"))
    assert os.path.exists(os.path.join(SESSIONS_DIR, "group_support_team.json"))

async def test_sessions_save_rejects_invalid_artifact_type(client):
    resp = await client.post("/api/sessions", json={"name": "bad", "artifact_type": "unknown"})
    assert resp.status_code == 400
    data = await resp.get_json()
    assert data.get("error_code") == "invalid_artifact_type"

# --- New tests for /api/sessions/<path:name> (DELETE) ---

async def test_sessions_delete_not_found(client):
    resp = await client.delete("/api/sessions/non_existent.json")
    assert resp.status_code == 404
    data = await resp.get_json()
    assert data.get("error_code") == "session_not_found"

async def test_sessions_delete_os_error(client):
    session_name = "delete_error.json"
    with open(os.path.join(SESSIONS_DIR, session_name), "w") as f:
        f.write("{}")
    with mock.patch('os.remove') as mock_remove:
        mock_remove.side_effect = OSError("Delete failed")
        resp = await client.delete(f"/api/sessions/{session_name}")
        assert resp.status_code == 500
        data = await resp.get_json()
        assert data.get("error_code") == "session_delete_error"

async def test_sessions_delete_success(client):
    session_name = "delete_success.json"
    session_path = os.path.join(SESSIONS_DIR, session_name)
    with open(session_path, "w") as f:
        f.write("{}")
    assert os.path.exists(session_path)

    resp = await client.delete(f"/api/sessions/{session_name}")
    assert resp.status_code == 200
    data = await resp.get_json()
    assert data.get("ok") is True
    assert not os.path.exists(session_path)

# --- New tests for /api/transcript/md ---

async def test_export_md_permission_error(client):
    with mock.patch('app.server._resolve_workspace_path') as mock_resolve_path:
        mock_resolve_path.side_effect = PermissionError("access_denied")
        resp = await client.post("/api/transcript/md", json={'transcript': []})
        assert resp.status_code == 403
        data = await resp.get_json()
        assert data.get("error_code") == "access_denied"

async def test_export_md_os_error(client):
    with mock.patch('builtins.open', side_effect=OSError("Write error")):
        resp = await client.post("/api/transcript/md", json={'transcript': []})
        assert resp.status_code == 500
        data = await resp.get_json()
        assert data.get("error_code") == "export_md_error"

async def test_export_md_success(client):
    transcript_data = [
        {"t": "12:00", "from": "User", "text": "Hello"},
        {"t": "12:01", "from": "Agent", "text": "Hi there!"},
    ]
    resp = await client.post("/api/transcript/md", json={'transcript': transcript_data})
    assert resp.status_code == 200
    data = await resp.get_json()
    assert data.get("ok") is True
    assert "/workspace/transcript_" in data.get("file")
    # Verify content (simplified check)
    with open(os.path.join(state.active_workspace or app.static_folder, data["file"].split("/")[-1]), "r", encoding="utf-8") as f:
        content = f.read()
        assert "# Transcript" in content
        assert "**User** - 12:00" in content
        assert "Hello" in content

# --- New tests for /api/transcript/html ---

async def test_export_html_permission_error(client):
    with mock.patch('app.server._resolve_workspace_path') as mock_resolve_path:
        mock_resolve_path.side_effect = PermissionError("access_denied")
        resp = await client.post("/api/transcript/html", json={'transcript': []})
        assert resp.status_code == 403
        data = await resp.get_json()
        assert data.get("error_code") == "access_denied"

async def test_export_html_os_error(client):
    with mock.patch('builtins.open', side_effect=OSError("Write error")):
        resp = await client.post("/api/transcript/html", json={'transcript': []})
        assert resp.status_code == 500
        data = await resp.get_json()
        assert data.get("error_code") == "export_html_error"

async def test_export_html_success(client):
    transcript_data = [
        {"from": "User", "text": "Hello <world>"},
        {"from": "Agent", "text": "Hi there!"},
    ]
    resp = await client.post("/api/transcript/html", json={'transcript': transcript_data})
    assert resp.status_code == 200
    data = await resp.get_json()
    assert data.get("ok") is True
    assert "/workspace/transcript_" in data.get("file")
    # Verify content (simplified check)
    with open(os.path.join(state.active_workspace or app.static_folder, data["file"].split("/")[-1]), "r", encoding="utf-8") as f:
        content = f.read()
        assert "<h1>Transcript</h1>" in content
        assert "<strong>User</strong>" in content
        assert "Hello &lt;world&gt;" in content

# --- New tests for /api/workspace/files and /api/workspace/tree ---

async def test_api_files_flat_success(client):
    # Create some dummy files in the workspace
    with open(os.path.join(state.active_workspace or app.static_folder, "file1.txt"), "w") as f: f.write("1")
    with open(os.path.join(state.active_workspace or app.static_folder, "file2.py"), "w") as f: f.write("2")
    os.makedirs(os.path.join(state.active_workspace or app.static_folder, "subdir"), exist_ok=True)

    resp = await client.get("/api/workspace/files")
    assert resp.status_code == 200
    data = await resp.get_json()
    file_names = [f["name"] for f in data]
    assert "file1.txt" in file_names
    assert "file2.py" in file_names
    assert "subdir" not in file_names # Should only list files, not directories

async def test_api_files_tree_success(client):
    # Create some dummy files and directories
    os.makedirs(os.path.join(state.active_workspace or app.static_folder, "dir1", "subdir1"), exist_ok=True)
    with open(os.path.join(state.active_workspace or app.static_folder, "dir1", "file_in_dir1.txt"), "w") as f: f.write("content")
    with open(os.path.join(state.active_workspace or app.static_folder, "root_file.txt"), "w") as f: f.write("content")

    resp = await client.get("/api/workspace/tree")
    assert resp.status_code == 200
    data = await resp.get_json()
    # Basic check for expected structure
    assert isinstance(data, dict)
    assert data.get("name") == "."
    assert "children" in data
    child_names = [c["name"] for c in data["children"]]
    assert "dir1" in child_names
    assert "root_file.txt" in child_names

# --- New tests for user_input, coach_input, choose_next queue errors ---

async def test_user_input_queue_error(client):
    with mock.patch('app.server.state.user_input_q.put_nowait') as mock_put_nowait:
        mock_put_nowait.side_effect = Exception("Queue full")
        resp = await client.post("/user_input", json={'message': 'test'})
        assert resp.status_code == 200 # Still returns 200 even on internal queue error
        data = await resp.get_json()
        assert data.get("ok") is True

async def test_coach_input_queue_error(client):
    with mock.patch('app.server.state.coach_input_q.put_nowait') as mock_put_nowait:
        mock_put_nowait.side_effect = Exception("Queue full")
        resp = await client.post("/coach_input", json={'message': 'test'})
        assert resp.status_code == 200
        data = await resp.get_json()
        assert data.get("ok") is True

async def test_choose_next_queue_error(client):
    with mock.patch('app.server.state.manual_next_q.put_nowait') as mock_put_nowait:
        mock_put_nowait.side_effect = Exception("Queue full")
        resp = await client.post("/choose_next", json={'name': 'AgentX'})
        assert resp.status_code == 200
        data = await resp.get_json()
        assert data.get("ok") is True
