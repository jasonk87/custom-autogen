import json
import queue
import asyncio
import random
from typing import Any, Dict, List, Optional, Sequence

from autogen_agentchat.agents import AssistantAgent, UserProxyAgent
from autogen_agentchat.messages import ModelClientStreamingChunkEvent, TextMessage, BaseChatMessage, BaseAgentEvent, SelectSpeakerEvent
from autogen_agentchat.teams import SelectorGroupChat
from autogen_ext.models.openai import OpenAIChatCompletionClient

from app.config import GEMINI_API_KEY, DEFAULT_MODEL, WORKSPACE_DIR
from app.state import state
from app.tools import TOOLS

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
        # Ignore tool call messages to prevent empty black boxes
        if type(message).__name__ in ["ToolCallRequestEvent", "ToolCallExecutionEvent", "ToolCallSummaryMessage"]:
            return None
        
        text = message.to_text()
        # Filter out tool code blocks if they slip through as TextMessage
        if "```tool_" in text or "``` tool_" in text:
            return None

        payload = {
            "type": "chat",
            "sender": message.source,
            "message": text,
            "graph_edge": {
                "from": message.source,
                "to": groupchat.name
            }
        }
    elif isinstance(message, BaseAgentEvent):
        if type(message).__name__ in ["ToolCallRequestEvent", "ToolCallExecutionEvent"]:
            return None
        
        text = message.to_text()
        if "```tool_" in text or "``` tool_" in text:
            return None

        payload = {
            "type": "chat",
            "sender": message.source,
            "message": text,
        }
    return payload

async def stream_to_queue(stream, out_q, groupchat):
    try:
        async for message in stream:
            try:
                payload = _create_graph_payload(message, groupchat)
                if payload:
                    out_q.put(json.dumps(payload))
            except Exception as e:
                print(f"Error processing message: {e}")
    except Exception as e:
        print(f"Stream error: {e}")
        out_q.put(json.dumps({"type": "chat", "sender": "System", "message": f"Stream error: {e}"}))

class MyUserProxyAgent(UserProxyAgent):
    def __init__(self, name: str, out_q: "queue.Queue[str]"):
        super().__init__(name=name)
        self.out_q = out_q

    def get_human_input(self, prompt: str) -> str:
        self.out_q.put(json.dumps({"type": "status", "state": "waiting_for_input"}))
        while True:
            try:
                return state.user_input_q.get(timeout=0.1)
            except queue.Empty:
                if state.stop_event.is_set():
                    return "exit"
                continue

async def run_orchestrator(goal: str, model: str, agents_cfg: List[Dict[str, Any]], manager_mode: str, out_q: "queue.Queue[str]", max_turns: int = 60, human_proxy: bool = False, temperature: float = 0.3, autonomous_user: bool = False):
    try:
        # 1. Create model client
        model_name = model or DEFAULT_MODEL
        gemini_client = OpenAIChatCompletionClient(
            model=model_name,
            api_key=GEMINI_API_KEY,
            base_url="https://generativelanguage.googleapis.com/v1beta/openai/",
        )

        # 2. Create agents
        # 2. Create agents
        if autonomous_user:
            # Context Awareness
            context_str = ""
            try:
                files = []
                skipped_dirs = {'.git', '__pycache__', 'node_modules', 'venv', '.idea', '.vscode'}
                for root, dirs, filenames in os.walk(WORKSPACE_DIR):
                    dirs[:] = [d for d in dirs if d not in skipped_dirs]
                    for f in filenames:
                        if f.startswith('.'): continue
                        if f.endswith(('.pyc', '.pyo', '.pyd', '.git')): continue
                        files.append(os.path.relpath(os.path.join(root, f), WORKSPACE_DIR))
                        if len(files) > 50:
                            files.append("...(truncated)")
                            break
                    if len(files) > 50:
                        break
                context_str = f"\nCurrent Workspace Files: {', '.join(files)}"
            except Exception:
                pass

            sys_msg = (
                "You are the **Project Manager** and Technical Lead. "
                "Your goal is to ensure the team successfully completes the user's objective. "
                "You do NOT write code or execute tools yourself. "
                "Instead, you must:\n"
                "1. **Read the Workspace**: Review existing files to understand the current state. "
                "2. **Guide the Team**: Provide high-level direction, point out missing components, and correct architectural mistakes. "
                "3. **Review Progress**: If the team says they are done, verify their work (by asking them to run tests or checking files). "
                "4. **Approve/Reject**: If the goal is met, say 'APPROVED'. If not, explain what is missing.\n"
                f"{context_str}"
            )
            user_proxy = AssistantAgent(
                name="Human_Admin",
                model_client=gemini_client,
                system_message=sys_msg,
                model_client_stream=True,
            )
        else:
            user_proxy = MyUserProxyAgent(
                name="Human_Admin",
                out_q=out_q,
            )

        agents = [user_proxy]
        for agent_cfg in agents_cfg:
            agent = AssistantAgent(
                name=agent_cfg["name"],
                model_client=gemini_client,
                system_message=agent_cfg.get("system", "You are a helpful assistant."),
                model_client_stream=True,
                reflect_on_tool_use=False,
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
            model_client=gemini_client,
        )

        # 5. Start the chat
        messages = [TextMessage(content=goal, source="user")]

        for i in range(max_turns):
            chat_stream = groupchat.run_stream(
                task=messages,
            )

            stream_task = asyncio.create_task(stream_to_queue(chat_stream, out_q, groupchat))

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
