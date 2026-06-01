import pytest
from unittest.mock import Mock
from autogen_agentchat.messages import (
    SelectSpeakerEvent,
    TextMessage,
    ToolCallExecutionEvent,
    ToolCallRequestEvent,
    ToolCallSummaryMessage,
)
from autogen_core import FunctionCall
from autogen_core.models import FunctionExecutionResult

# Import app.core if present; skip the module if not.
core = pytest.importorskip("app.core", reason="app.core not found")

# Find the mapper function; skip file if missing.
_create_graph_payload = getattr(core, "_create_graph_payload", None)
if _create_graph_payload is None:
    pytest.skip("_create_graph_payload not found in app.core", allow_module_level=True)


def test_text_message_maps_to_manager_edge():
    """
    Tests that a TextMessage from an agent correctly generates a graph edge
    to the groupchat manager.
    """
    mock_groupchat = Mock()
    mock_groupchat.name = "test_manager"
    message = TextMessage(source="Agent1", content="Hello, world!")

    payload = _create_graph_payload(message, mock_groupchat)

    assert payload is not None
    assert payload.get("type") == "chat"
    assert payload.get("sender") == "Agent1"
    edge = payload.get("graph_edge")
    assert edge is not None
    assert edge.get("from") == "Agent1"
    assert edge.get("to") == "test_manager"

def test_select_speaker_event_maps_to_agent_edge():
    """
    Tests that a SelectSpeakerEvent from the manager correctly generates a
    graph edge to the selected agent.
    """
    mock_groupchat = Mock()
    mock_groupchat.name = "test_manager"
    message = SelectSpeakerEvent(source="test_manager", content=["Agent2"])

    payload = _create_graph_payload(message, mock_groupchat)

    assert payload is not None
    assert payload.get("type") == "chat"
    assert payload.get("sender") == "test_manager"
    edge = payload.get("graph_edge")
    assert edge is not None
    assert edge.get("from") == "test_manager"
    assert edge.get("to") == "Agent2"


def test_tool_request_maps_to_structured_payload():
    payload = _create_graph_payload(
        ToolCallRequestEvent(
            source="Finder",
            content=[FunctionCall(id="1", name="tool_web_search", arguments='{"query":"rentals"}')],
        ),
        Mock(),
    )

    assert payload == {"type": "tool_request", "sender": "Finder", "tools": ["tool_web_search"]}


def test_tool_result_maps_to_structured_search_payload():
    payload = _create_graph_payload(
        ToolCallExecutionEvent(
            source="Finder",
            content=[
                FunctionExecutionResult(
                    call_id="1",
                    name="tool_web_search",
                    content="{'query': 'rentals', 'results': [{'title': 'Listing', 'link': 'https://example.com', 'snippet': 'Three bedrooms'}]}",
                )
            ],
        ),
        Mock(),
    )

    assert payload["type"] == "tool_result"
    assert payload["results"][0]["query"] == "rentals"
    assert payload["results"][0]["results"][0]["title"] == "Listing"


def test_tool_summary_message_is_filtered_after_structured_result():
    payload = _create_graph_payload(
        ToolCallSummaryMessage(
            source="Finder",
            content="raw duplicate result",
            tool_calls=[FunctionCall(id="1", name="tool_web_search", arguments="{}")],
            results=[FunctionExecutionResult(call_id="1", name="tool_web_search", content="{}")],
        ),
        Mock(),
    )

    assert payload is None
