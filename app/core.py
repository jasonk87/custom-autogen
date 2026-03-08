import asyncio
import json
import queue
import random
from typing import Any, Dict, List, Optional, Sequence

from autogen_agentchat.agents import AssistantAgent, UserProxyAgent
from autogen_agentchat.messages import (
    BaseAgentEvent,
    BaseChatMessage,
    SelectSpeakerEvent,
    TextMessage,
    ToolCallExecutionEvent,
    ToolCallRequestEvent,
)
from autogen_agentchat.teams import SelectorGroupChat
from autogen_core import CancellationToken
from autogen_ext.models.openai import OpenAIChatCompletionClient

from app import log
from app.config import DEFAULT_MODEL, gemini_model_info, require_gemini_api_key
from app.state import state
from app.tools import TOOLS


class SafeOpenAIChatCompletionClient(OpenAIChatCompletionClient):
    """Wraps model errors with clearer Gemini-specific guidance."""

    async def create(self, messages, **kwargs):
        try:
            return await super().create(messages, **kwargs)
        except asyncio.CancelledError:
            log.info(
                "model_client_cancelled",
                extra={"detail": "Model client creation cancelled (likely session stop)"},
            )
            raise
        except Exception as e:
            error_str = str(e)
            if "'NoneType' object is not subscriptable" in error_str:
                log.error("gemini_api_crash", extra={"reason": "Received empty choices from API"})
                raise RuntimeError(
                    "Gemini API Error: The model returned an empty response. "
                    "This usually indicates a safety filter trigger or invalid API key."
                ) from e

            log.error("model_client_error", extra={"error": error_str, "type": type(e).__name__})
            raise


def _create_graph_payload(
    message: Any,
    groupchat: SelectorGroupChat,
    current_speaker: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """Creates a payload for the frontend, including graph data if applicable."""
    msg_type = type(message).__name__
    log.info("debug_event_inspector", extra={"type": msg_type, "content": str(message)[:100]})

    if "Streaming" in msg_type or "Chunk" in msg_type:
        return None

    payload = None
    if isinstance(message, SelectSpeakerEvent):
        payload = {
            "type": "chat",
            "sender": message.source,
            "message": f"Selected speaker: {message.content[0]}",
            "graph_edge": {
                "from": message.source,
                "to": message.content[0],
            },
        }
    elif isinstance(message, ToolCallRequestEvent):
        tool_names = [call.name for call in message.content]
        payload = {
            "type": "chat",
            "sender": message.source,
            "message": f"Calling tools: {', '.join(tool_names)}",
        }
    elif isinstance(message, ToolCallExecutionEvent):
        results = []
        for res in message.content:
            result_text = str(res.content)
            results.append(result_text[:200] + ("..." if len(result_text) > 200 else ""))
        payload = {
            "type": "chat",
            "sender": message.source,
            "message": f"Tool output: {'; '.join(results)}",
        }
    elif isinstance(message, BaseChatMessage):
        txt = message.to_text().strip()
        if not txt:
            log.warning(
                "filtering_empty_chat_message",
                extra={"sender": message.source, "type": msg_type},
            )
            return None
        payload = {
            "type": "chat",
            "sender": message.source,
            "message": txt,
            "graph_edge": {
                "from": message.source,
                "to": getattr(groupchat, "name", "GroupChat"),
            },
        }
    elif isinstance(message, BaseAgentEvent):
        txt = message.to_text().strip()
        if not txt:
            log.warning(
                "filtering_empty_agent_event",
                extra={"sender": message.source, "type": msg_type},
            )
            return None
        payload = {
            "type": "chat",
            "sender": message.source,
            "message": txt,
        }
    else:
        log.info(
            "ignoring_unknown_message_type",
            extra={"type": msg_type, "source": getattr(message, "source", "unknown")},
        )

    return payload


async def stream_to_queue(stream, out_q, groupchat):
    async for message in stream:
        payload = _create_graph_payload(message, groupchat)
        if payload:
            out_q.put(json.dumps(payload))


async def run_orchestrator(
    goal: str,
    model: str,
    agents_cfg: List[Dict[str, Any]],
    manager_mode: str,
    out_q: "queue.Queue[str]",
    max_turns: int = 60,
    human_proxy: bool = False,
    temperature: float = 0.3,
):
    try:
        model_name = model or DEFAULT_MODEL
        gemini_client = SafeOpenAIChatCompletionClient(
            model=model_name,
            api_key=require_gemini_api_key(),
            base_url="https://generativelanguage.googleapis.com/v1beta/openai/",
            temperature=temperature,
            model_info=gemini_model_info(model_name),
        )

        participants = []
        manager_mode = manager_mode.lower()

        async def input_func(
            prompt: str = "",
            cancellation_token: Optional[CancellationToken] = None,
        ) -> str:
            if manager_mode == "auto" and not human_proxy:
                try:
                    user_input = state.user_input_q.get_nowait()
                    return user_input
                except queue.Empty:
                    await asyncio.sleep(1.0)
                    return "Approved"

            out_q.put(json.dumps({"type": "status", "state": "waiting_for_input"}))
            while True:
                if state.stop_event.is_set():
                    return "exit"
                try:
                    user_input = await asyncio.to_thread(state.user_input_q.get, timeout=1.0)
                    out_q.put(json.dumps({"type": "status", "state": "running"}))
                    return user_input
                except queue.Empty:
                    if cancellation_token and cancellation_token.is_cancelled():
                        return "exit"
                    continue
                except Exception:
                    return "exit"

        include_human_proxy = bool(human_proxy)
        if include_human_proxy:
            user_proxy = UserProxyAgent(name="Human_Admin", input_func=input_func)
            participants.append(user_proxy)

        for agent_cfg in agents_cfg:
            agent = AssistantAgent(
                name=agent_cfg["name"],
                model_client=gemini_client,
                system_message=agent_cfg.get("system", "You are a helpful assistant."),
                model_client_stream=False,
                reflect_on_tool_use=False,
                tools=list(TOOLS.values()),
            )
            participants.append(agent)

        selector_func = None
        if manager_mode in {"round_robin", "roundrobin"}:

            class RoundRobinSelector:
                def __init__(self, participants):
                    self.participants = participants
                    self.index = 0

                def __call__(self, messages: Sequence[BaseAgentEvent | BaseChatMessage]) -> str | None:
                    name = self.participants[self.index % len(self.participants)].name
                    self.index += 1
                    return name

            selector_func = RoundRobinSelector(participants)

        elif manager_mode == "random":

            def random_selector_func(messages: Sequence[BaseAgentEvent | BaseChatMessage]) -> str | None:
                return random.choice(participants).name

            selector_func = random_selector_func

        elif manager_mode == "manual":

            async def manual_selector_func(messages: Sequence[BaseAgentEvent | BaseChatMessage]) -> str | None:
                out_q.put(json.dumps({"type": "status", "state": "waiting_for_manual_selection"}))
                while True:
                    if state.stop_event.is_set():
                        return None
                    try:
                        next_agent_name = await asyncio.to_thread(state.manual_next_q.get, timeout=1.0)
                        out_q.put(json.dumps({"type": "status", "state": "running"}))
                        return next_agent_name
                    except queue.Empty:
                        continue
                    except Exception:
                        return None

            selector_func = manual_selector_func

        groupchat = SelectorGroupChat(
            participants=participants,
            max_turns=max_turns,
            selector_func=selector_func,
            model_client=gemini_client,
            allow_repeated_speaker=True,
        )

        if goal:
            if include_human_proxy:
                task = [TextMessage(content=goal, source="Human_Admin")]
            else:
                task = goal
        else:
            task = []
        cancellation_token = CancellationToken()

        async def check_stop():
            while True:
                if state.stop_event.is_set():
                    cancellation_token.cancel()
                    break
                await asyncio.sleep(0.1)

        stop_monitor = asyncio.create_task(check_stop())

        try:
            current_speaker = None
            async for message in groupchat.run_stream(task=task, cancellation_token=cancellation_token):
                if hasattr(message, "source"):
                    log.info(
                        "message_source_debug",
                        extra={"source": message.source, "type": type(message).__name__},
                    )

                if isinstance(message, SelectSpeakerEvent):
                    log.info("select_speaker_debug", extra={"content": message.content})
                    current_speaker = message.content[0]

                payload = _create_graph_payload(message, groupchat, current_speaker)
                if payload:
                    out_q.put(json.dumps(payload))
        except asyncio.CancelledError:
            out_q.put(json.dumps({"type": "chat", "sender": "System", "message": "Conversation cancelled."}))
        finally:
            stop_monitor.cancel()

        out_q.put(json.dumps({"type": "status", "state": "idle"}))

    except Exception as e:
        out_q.put(json.dumps({"type": "chat", "sender": "Error", "message": f"{type(e).__name__}: {e}"}))
        log.error("orchestrator_error", exc_info=e)
    finally:
        out_q.put("[DONE]")
