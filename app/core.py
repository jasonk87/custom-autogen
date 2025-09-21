import json
import queue
import asyncio
import random
from typing import Any, Dict, List, Optional, Sequence

from autogen_agentchat.agents import AssistantAgent, UserProxyAgent
from autogen_agentchat.messages import ModelClientStreamingChunkEvent, TextMessage, BaseChatMessage, BaseAgentEvent
from autogen_agentchat.teams import SelectorGroupChat
from autogen_ext.models.ollama import OllamaChatCompletionClient

from app.config import OLLAMA_BASE_URL, DEFAULT_MODEL
from app.state import state
from app.tools import TOOLS

async def stream_to_queue(stream, out_q):
    async for message in stream:
        if isinstance(message, ModelClientStreamingChunkEvent):
            out_q.put(json.dumps({"type": "token", "id": "stream", "delta": message.delta}))
        else:
            out_q.put(json.dumps({"type": "chat", "sender": "System", "message": str(message)}))

class MyUserProxyAgent(UserProxyAgent):
    def get_human_input(self, prompt: str) -> str:
        out_q.put(json.dumps({"type": "status", "state": "waiting_for_input"}))
        while True:
            try:
                return state.user_input_q.get(timeout=0.1)
            except queue.Empty:
                if state.stop_event.is_set():
                    return "exit"
                continue

async def run_orchestrator(goal: str, model: str, agents_cfg: List[Dict[str, Any]], manager_mode: str, out_q: "queue.Queue[str]", max_turns: int = 60, human_proxy: bool = False):
    try:
        # 1. Create model client
        ollama_client = OllamaChatCompletionClient(
            model=model or DEFAULT_MODEL,
            host=OLLAMA_BASE_URL,
        )

        # 2. Create agents
        user_proxy = MyUserProxyAgent(
            name="Human_Admin",
            human_input_mode="ALWAYS" if human_proxy else "NEVER",
            max_consecutive_auto_reply=10 if not human_proxy else None,
            code_execution_config={"work_dir": "autogen_work_dir", "use_docker": False},
            system_message="You are the human admin. You can execute code and provide feedback.",
        )

        agents = [user_proxy]
        for agent_cfg in agents_cfg:
            agent = AssistantAgent(
                name=agent_cfg["name"],
                model_client=ollama_client,
                system_message=agent_cfg.get("system", "You are a helpful assistant."),
                model_client_stream=True,
                reflect_on_tool_use=True,
                tools=list(TOOLS.values()),
            )
            agents.append(agent)

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
            selector_func = RoundRobinSelector(agents)
        elif manager_mode == "random":
            def random_selector_func(messages: Sequence[BaseAgentEvent | BaseChatMessage]) -> str | None:
                return random.choice(agents).name
            selector_func = random_selector_func

        # 4. Create GroupChat
        groupchat = SelectorGroupChat(
            participants=agents,
            max_turns=max_turns,
            selector_func=selector_func,
        )

        # 5. Start the chat
        messages = [TextMessage(content=goal, source="user")]

        for i in range(max_turns):
            chat_stream = await groupchat.run_stream(
                task=messages,
            )

            stream_task = asyncio.create_task(stream_to_queue(chat_stream, out_q))

            # Reset messages for the next turn
            messages = []

            while not stream_task.done():
                try:
                    user_input = state.user_input_q.get(timeout=0.1)
                    if user_input:
                        messages.append(TextMessage(content=user_input, source="Human_Admin"))
                        stream_task.cancel() # Interrupt the current stream to process the new message
                        break
                except queue.Empty:
                    await asyncio.sleep(0.1)

                if state.stop_event.is_set():
                    stream_task.cancel()
                    break

            if not messages and stream_task.done():
                # The chat has finished and there are no more user messages
                break

        out_q.put(json.dumps({"type": "status", "state": "idle"}))

    except asyncio.CancelledError:
        out_q.put(json.dumps({"type": "status", "state": "idle"}))
    except Exception as e:
        out_q.put(json.dumps({"type": "chat", "sender": "Error", "message": f"{type(e).__name__}: {e}"}))
    finally:
        out_q.put("[DONE]")
