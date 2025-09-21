import json
import queue
import asyncio
from typing import Any, Dict, List, Optional

from autogen_agentchat.agents import AssistantAgent, UserProxyAgent
from autogen_agentchat.messages import ModelClientStreamingChunkEvent
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

async def run_orchestrator(goal: str, model: str, agents_cfg: List[Dict[str, Any]], manager_mode: str, out_q: "queue.Queue[str]", max_turns: int = 60):
    try:
        # 1. Create model client
        ollama_client = OllamaChatCompletionClient(
            model=model or DEFAULT_MODEL,
            host=OLLAMA_BASE_URL,
        )

        # 2. Create agents
        user_proxy = UserProxyAgent(
            name="Human_Admin",
            human_input_mode="ALWAYS", # Ask for input every time
            max_consecutive_auto_reply=None, # No auto-reply
            code_execution_config={"work_dir": "autogen_work_dir", "use_docker": False},
            system_message="You are the human admin. You can execute code and provide feedback.",
        )

        # It's better to get user input from the queue than to block the main thread
        def get_user_input_from_queue(prompt: str) -> str:
            out_q.put(json.dumps({"type": "status", "state": "waiting_for_input"}))
            while True:
                try:
                    return state.user_input_q.get(timeout=0.1)
                except queue.Empty:
                    if state.stop_event.is_set():
                        return "TERMINATE"
                    continue

        user_proxy.register_reply([agent_name for agent_name in agents_cfg], get_user_input_from_queue)


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

        # 3. Create GroupChat
        groupchat = SelectorGroupChat(
            participants=agents,
            model_client=ollama_client,
            max_turns=max_turns,
        )

        # 4. Start the chat stream
        chat_stream = await groupchat.run_stream(
            task=goal,
        )

        # 5. Stream the chat to the queue
        await stream_to_queue(chat_stream, out_q)

        out_q.put(json.dumps({"type": "status", "state": "idle"}))

    except asyncio.CancelledError:
        out_q.put(json.dumps({"type": "status", "state": "idle"}))
    except Exception as e:
        out_q.put(json.dumps({"type": "chat", "sender": "Error", "message": f"{type(e).__name__}: {e}"}))
    finally:
        out_q.put("[DONE]")
