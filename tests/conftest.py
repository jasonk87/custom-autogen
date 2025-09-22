import pytest
from autogen_agentchat.messages import TextMessage


@pytest.fixture(autouse=True)
def patch_selector_group_chat(monkeypatch):
    """
    Patch SelectorGroupChat.run_stream everywhere your app might have imported it.
    Provides a small, realistic async stream and a final TaskResult-like object.
    """

    async def fake_run_stream(self, *, task=None, cancellation_token=None,
                              output_task_messages=True, **_):
        # Minimal realistic stream: two agent messages…
        yield TextMessage(source="Planner", content="Plan: do X")
        yield TextMessage(source="Coder",   content="Code: implemented X")

        # …and a final TaskResult-like object
        class _TaskResult:
            def __init__(self, messages):
                self.messages = messages
                self.stop_reason = "max_turns"

        yield _TaskResult([TextMessage(source="Coder", content="Done")])

    # Patch public API path
    try:
        import autogen_agentchat.teams as teams
        monkeypatch.setattr(teams.SelectorGroupChat, "run_stream", fake_run_stream, raising=False)
    except Exception:
        pass

    # Patch private/internal path (in case your code imported internals)
    monkeypatch.setattr(
        "autogen_agentchat.teams._group_chat._selector_group_chat.SelectorGroupChat.run_stream",
        fake_run_stream,
        raising=False,
    )

    # Patch local modules if they did `from ... import SelectorGroupChat`
    for path in (
        "app.core.SelectorGroupChat.run_stream",
        "app.server.SelectorGroupChat.run_stream",
    ):
        try:
            monkeypatch.setattr(path, fake_run_stream, raising=False)
        except Exception:
            pass
