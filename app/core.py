import json
import queue
import asyncio
import random
from typing import Any, Dict, List, Optional, Sequence

from autogen_agentchat.agents import AssistantAgent, UserProxyAgent
from autogen_agentchat.messages import ModelClientStreamingChunkEvent, TextMessage, BaseChatMessage, BaseAgentEvent, SelectSpeakerEvent
from autogen_agentchat.teams import SelectorGroupChat
from autogen_ext.models.openai import OpenAIChatCompletionClient
from autogen_core import CancellationToken

from app.config import GEMINI_API_KEY, DEFAULT_MODEL
from app.state import state
from app.tools import TOOLS
from app import log

def _create_graph_payload(message: Any, groupchat: SelectorGroupChat) -> Optional[Dict[str, Any]]:
    """Creates a payload for the frontend, including graph data if applicable."""
    payload = None
    if isinstance(message, ModelClientStreamingChunkEvent):
        payload = {"type": "token", "id": "stream", "delta": message.content, "sender": message.source}
    elif isinstance(message, SelectSpeakerEvent):
        payload = {
            "type": "chat",
            "sender": message.source,
            "message": f"Selected speaker: {message.content[0]}",
            "graph_edge": {
                "from": message.source,
                "to": message.content[0]
            }
        }
    elif isinstance(message, BaseChatMessage):
        payload = {
            "type": "chat",
            "sender": message.source,
            "message": message.to_text(),
            "graph_edge": {
                "from": message.source,
                "to": groupchat.name
            }
        }
    elif isinstance(message, BaseAgentEvent):
        payload = {
            "type": "chat",
            "sender": message.source,
            "message": message.to_text(),
        }
    return payload

async def stream_to_queue(stream, out_q, groupchat):
    async for message in stream:
        payload = _create_graph_payload(message, groupchat)
        if payload:
            out_q.put(json.dumps(payload))

async def run_orchestrator(goal: str, model: str, agents_cfg: List[Dict[str, Any]], manager_mode: str, out_q: "queue.Queue[str]", max_turns: int = 60, human_proxy: bool = False, temperature: float = 0.3):
    try:
        # 1. Create model client
        model_name = model or DEFAULT_MODEL
        gemini_client = OpenAIChatCompletionClient(
            model=model_name,
            api_key=GEMINI_API_KEY,
            base_url="https://generativelanguage.googleapis.com/v1beta/openai/",
            temperature=temperature,
        )

        participants = []

        # 2. Define Input Function for UserProxy
        async def input_func(prompt: str = "", cancellation_token: Optional[CancellationToken] = None) -> str:
            out_q.put(json.dumps({"type": "status", "state": "waiting_for_input"}))
            while True:
                if state.stop_event.is_set():
                    return "exit"
                try:
                    # Use to_thread to avoid blocking the event loop
                    user_input = await asyncio.to_thread(state.user_input_q.get, timeout=1.0)
                    out_q.put(json.dumps({"type": "status", "state": "running"}))
                    return user_input
                except queue.Empty:
                    if cancellation_token and cancellation_token.is_cancelled():
                        return "exit"
                    continue
                except Exception:
                    return "exit"

        # Add UserProxy
        user_proxy = UserProxyAgent(
            name="Human_Admin",
            input_func=input_func,
        )
        participants.append(user_proxy)

        # Add Assistant Agents
        for agent_cfg in agents_cfg:
            agent = AssistantAgent(
                name=agent_cfg["name"],
                model_client=gemini_client,
                system_message=agent_cfg.get("system", "You are a helpful assistant."),
                model_client_stream=True,
                reflect_on_tool_use=False,
                tools=list(TOOLS.values()),
            )
            participants.append(agent)

        # 3. Create selector function
        selector_func = None
        if manager_mode == "round_robin":
            class RoundRobinSelector:
                def __init__(self, participants):
                    self.participants = participants
                    self.index = 0
                def __call__(self, messages: Sequence[BaseAgentEvent | BaseChatMessage]) -> str | None:
                    name = self.participants[self.index % len(self.participants)].name
                    self.index += 1
                    return name

            selector_instance = RoundRobinSelector(participants)
            selector_func = selector_instance

        elif manager_mode == "random":
             def random_selector_func(messages: Sequence[BaseAgentEvent | BaseChatMessage]) -> str | None:
                return random.choice(participants).name
             selector_func = random_selector_func

        # 4. Create GroupChat
        groupchat = SelectorGroupChat(
            participants=participants,
            max_turns=max_turns,
            selector_func=selector_func,
            model_client=gemini_client,
            allow_repeated_speaker=True,
        )

        # 5. Run the chat
        task = [TextMessage(content=goal, source="user")] if goal else []

        cancellation_token = CancellationToken()

        # Monitoring stop event to cancel
        async def check_stop():
            while True:
                if state.stop_event.is_set():
                    cancellation_token.cancel()
                    break
                await asyncio.sleep(1)

        stop_monitor = asyncio.create_task(check_stop())

        try:
             async for message in groupchat.run_stream(task=task, cancellation_token=cancellation_token):
                payload = _create_graph_payload(message, groupchat)
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
