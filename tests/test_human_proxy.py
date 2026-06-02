import queue
from types import SimpleNamespace
from unittest import mock

import pytest

from app.core import (
    HUMAN_ADMIN_REQUEST,
    SMART_SUPERVISOR_PROMPT,
    TASK_COMPLETE_MARKER,
    OrchestratorError,
    _conversation_mode_prompt,
    _human_proxy_prompt,
    _message_requests_agent,
    _normalize_human_proxy_mode,
    _normalize_conversation_mode,
    _agent_tools_enabled,
    _relationship_prompt,
    _specialist_coordination_prompt,
    _tools_for_human_proxy,
    run_orchestrator,
)
from app.state import state


def _tool_names(mode):
    return {tool.__name__ for tool in _tools_for_human_proxy(mode)}


def test_human_proxy_mode_normalizes_legacy_boolean():
    assert _normalize_human_proxy_mode(True) == "consult"
    assert _normalize_human_proxy_mode(False) == "off"


def test_human_proxy_mode_rejects_unknown_value():
    with pytest.raises(OrchestratorError):
        _normalize_human_proxy_mode("unrestricted")


@pytest.mark.parametrize(
    "mode",
    ["discussion", "debate", "brainstorm", "execution", "simulation", "storybook"],
)
def test_conversation_modes_are_supported(mode):
    assert _normalize_conversation_mode(mode) == mode
    assert f"Conversation mode: {mode.title()}." in _conversation_mode_prompt(mode)


def test_conversation_mode_defaults_to_discussion():
    assert _normalize_conversation_mode(None) == "discussion"


def test_conversation_mode_rejects_unknown_value():
    with pytest.raises(OrchestratorError) as exc:
        _normalize_conversation_mode("round_robin")
    assert exc.value.error_code == "invalid_conversation_mode"


def test_execution_mode_adds_productivity_without_changing_role_rules():
    prompt = _specialist_coordination_prompt("off", "execution")
    assert "Stay grounded in your assigned role" in prompt
    assert "Conversation mode: Execution." in prompt
    assert "Create deliverables" in prompt
    assert "Use available tools" in prompt


def test_simulation_and_storybook_modes_have_distinct_priorities():
    assert "remain immersed" in _conversation_mode_prompt("simulation")
    assert "Narrative quality takes priority" in _conversation_mode_prompt("storybook")


def test_delegate_safe_excludes_destructive_and_external_tools():
    names = _tool_names("delegate_safe")
    assert "tool_write_file" in names
    assert "tool_delete" not in names
    assert "tool_fetch_webpage" not in names
    assert "execute_shell_command" not in names


def test_workspace_autonomy_adds_local_delete_only():
    names = _tool_names("autonomous_workspace")
    assert "tool_delete" in names
    assert "tool_fetch_webpage" not in names
    assert "execute_shell_command" not in names


def test_human_proxy_prompt_includes_preferences_and_escalation():
    prompt = _human_proxy_prompt("delegate_safe", "Prefer focused changes.")
    assert "Human_Approval" in prompt
    assert "Prefer focused changes." in prompt


def test_human_proxy_prompt_is_bounded_coordinator_with_completion_marker():
    prompt = _human_proxy_prompt("delegate_safe", "")
    assert "not a specialist team member" in prompt
    assert "never restart it" in prompt
    assert TASK_COMPLETE_MARKER in prompt


def test_specialist_prompt_requests_admin_only_for_coordination():
    prompt = _specialist_coordination_prompt("delegate_safe")
    assert HUMAN_ADMIN_REQUEST in prompt
    assert "Do not treat Human_Admin as another specialist" in prompt


def test_specialist_prompt_can_finish_without_human_proxy():
    prompt = _specialist_coordination_prompt("off")
    assert TASK_COMPLETE_MARKER in prompt


def test_specialist_prompt_can_finish_without_consulting_user():
    prompt = _specialist_coordination_prompt("consult")
    assert TASK_COMPLETE_MARKER in prompt


def test_specialist_prompt_is_role_bound_but_open_ended():
    prompt = _specialist_coordination_prompt("off")
    assert "Stay grounded in your assigned role" in prompt
    assert "Do not become a generic assistant" in prompt
    assert "creative, playful, or exploratory" in prompt
    assert "Do not force a work plan" in prompt


def test_relationship_prompt_injects_social_context_without_metadata_language():
    prompt = _relationship_prompt(
        {
            "name": "CEO_Carla",
            "relationships": [
                {
                    "target": "Lead_Developer_Dan",
                    "relation": "Boss",
                    "notes": "You trust Dan's technical judgment but expect concise updates.",
                }
            ],
        }
    )
    assert "Known relationships:" in prompt
    assert "You are Boss to Lead_Developer_Dan." in prompt
    assert "guide tone, trust, disagreement, loyalty" in prompt


def test_relationship_prompt_omits_empty_relationships():
    assert _relationship_prompt({"name": "Solo", "relationships": []}) == ""


def test_agent_tool_permission_requires_global_and_agent_switches():
    assert _agent_tools_enabled({}, True) is True
    assert _agent_tools_enabled({"tools_enabled": True}, True) is True
    assert _agent_tools_enabled({"tools_enabled": False}, True) is False
    assert _agent_tools_enabled({"tools_enabled": True}, False) is False


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("global_allow_tools", "agent_tools_enabled", "expects_tools"),
    [(True, True, True), (True, False, False), (False, True, False)],
)
async def test_runtime_respects_agent_and_global_tool_permissions(
    global_allow_tools,
    agent_tools_enabled,
    expects_tools,
):
    created = []

    class FakeAgent:
        def __init__(self, name, **kwargs):
            self.name = name
            created.append((name, kwargs))

    class FakeGroupChat:
        def __init__(self, **kwargs):
            del kwargs

        async def run_stream(self, **kwargs):
            del kwargs
            if False:
                yield None

    with (
        mock.patch("app.core.get_model_client", return_value=mock.Mock()),
        mock.patch("app.core.model_supports_tools", return_value=True),
        mock.patch("app.core.AssistantAgent", FakeAgent),
        mock.patch("app.core.UserProxyAgent", FakeAgent),
        mock.patch("app.core.SelectorGroupChat", FakeGroupChat),
    ):
        await run_orchestrator(
            "Work.",
            "test-model",
            [
                {
                    "name": "Engineer",
                    "system": "Build.",
                    "tools_enabled": agent_tools_enabled,
                    "relationships": [{"target": "Reviewer", "relation": "Coworker", "notes": "Coordinate."}],
                },
                {"name": "Reviewer", "system": "Review.", "tools_enabled": False},
            ],
            "round_robin",
            queue.Queue(),
            allow_tools=global_allow_tools,
        )

    engineer_kwargs = next(kwargs for name, kwargs in created if name == "Engineer")
    assert ("tools" in engineer_kwargs) is expects_tools
    assert "You are Coworker to Reviewer." in engineer_kwargs["system_message"]


def test_message_requests_agent_requires_latest_non_admin_message():
    messages = [SimpleNamespace(source="Researcher", content=f"{HUMAN_ADMIN_REQUEST} completion review")]
    assert _message_requests_agent(messages, HUMAN_ADMIN_REQUEST, excluded_sources={"Human_Admin"})

    messages.append(SimpleNamespace(source="Human_Admin", content=f"Use {HUMAN_ADMIN_REQUEST} later"))
    assert not _message_requests_agent(messages, HUMAN_ADMIN_REQUEST, excluded_sources={"Human_Admin"})


@pytest.mark.asyncio
async def test_smart_supervisor_offers_only_specialists_until_admin_is_requested():
    captured = {}

    class FakeModelClient:
        async def create(self, **kwargs):
            del kwargs
            return SimpleNamespace(content="Engineer")

    class FakeAgent:
        def __init__(self, name, **kwargs):
            del kwargs
            self.name = name

    class FakeGroupChat:
        name = "FakeGroupChat"

        def __init__(self, **kwargs):
            captured.update(kwargs)

        async def run_stream(self, **kwargs):
            del kwargs
            if False:
                yield None

    agents = [
        {"name": "Researcher", "system": "Research."},
        {"name": "Engineer", "system": "Build."},
    ]
    with (
        mock.patch("app.core.get_model_client", return_value=FakeModelClient()),
        mock.patch("app.core.model_supports_tools", return_value=False),
        mock.patch("app.core.AssistantAgent", FakeAgent),
        mock.patch("app.core.UserProxyAgent", FakeAgent),
        mock.patch("app.core.SelectorGroupChat", FakeGroupChat),
    ):
        await run_orchestrator(
            "Complete the project.",
            "test-model",
            agents,
            "smart_supervisor",
            queue.Queue(),
            human_proxy_mode="delegate_safe",
        )

    assert captured["candidate_func"]([]) == ["Researcher", "Engineer"]
    assert "selector_prompt" not in captured
    assert await captured["selector_func"]([]) == "Engineer"
    assert await captured["selector_func"](
        [SimpleNamespace(source="Researcher", content=f"{HUMAN_ADMIN_REQUEST} completion review")]
    ) == "Human_Admin"


@pytest.mark.asyncio
async def test_smart_supervisor_timeout_falls_back_to_next_specialist():
    captured = {}

    class FakeModelClient:
        async def create(self, **kwargs):
            del kwargs
            await __import__("asyncio").sleep(10)

    class FakeAgent:
        def __init__(self, name, **kwargs):
            del kwargs
            self.name = name

    class FakeGroupChat:
        def __init__(self, **kwargs):
            captured.update(kwargs)

        async def run_stream(self, **kwargs):
            del kwargs
            if False:
                yield None

    with (
        mock.patch("app.core.SMART_SUPERVISOR_TIMEOUT_SECONDS", 0.01),
        mock.patch("app.core.get_model_client", return_value=FakeModelClient()),
        mock.patch("app.core.model_supports_tools", return_value=False),
        mock.patch("app.core.AssistantAgent", FakeAgent),
        mock.patch("app.core.UserProxyAgent", FakeAgent),
        mock.patch("app.core.SelectorGroupChat", FakeGroupChat),
    ):
        await run_orchestrator(
            "Keep moving.",
            "test-model",
            [
                {"name": "Researcher", "system": "Research."},
                {"name": "Engineer", "system": "Build."},
            ],
            "smart_supervisor",
            queue.Queue(),
        )
        selector = captured["selector_func"]
        assert await selector([]) == "Researcher"
        assert await selector([]) == "Engineer"


@pytest.mark.asyncio
async def test_stop_requested_runtime_error_does_not_emit_orchestration_error():
    class FakeAgent:
        def __init__(self, name, **kwargs):
            del kwargs
            self.name = name

    class FakeGroupChat:
        def __init__(self, **kwargs):
            del kwargs

        async def run_stream(self, **kwargs):
            del kwargs
            raise RuntimeError("wrapped cancellation")
            yield

    out_q = queue.Queue()
    state.stop_event.set()
    try:
        with (
            mock.patch("app.core.get_model_client", return_value=mock.Mock()),
            mock.patch("app.core.model_supports_tools", return_value=False),
            mock.patch("app.core.AssistantAgent", FakeAgent),
            mock.patch("app.core.UserProxyAgent", FakeAgent),
            mock.patch("app.core.SelectorGroupChat", FakeGroupChat),
        ):
            await run_orchestrator(
                "Stop cleanly.",
                "test-model",
                [{"name": "Researcher", "system": "Research."}],
                "auto",
                out_q,
            )
    finally:
        state.stop_event.clear()

    assert out_q.get_nowait() == "[DONE]"
    assert out_q.empty()
