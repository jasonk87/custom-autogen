import pytest

from app.core import (
    OrchestratorError,
    _human_proxy_prompt,
    _normalize_human_proxy_mode,
    _tools_for_human_proxy,
)


def _tool_names(mode):
    return {tool.__name__ for tool in _tools_for_human_proxy(mode)}


def test_human_proxy_mode_normalizes_legacy_boolean():
    assert _normalize_human_proxy_mode(True) == "consult"
    assert _normalize_human_proxy_mode(False) == "off"


def test_human_proxy_mode_rejects_unknown_value():
    with pytest.raises(OrchestratorError):
        _normalize_human_proxy_mode("unrestricted")


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
