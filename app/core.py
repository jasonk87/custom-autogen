import asyncio
import ast
import json
import queue
import random
import re
import uuid
from typing import Any, Dict, List, Optional, Sequence

from autogen_agentchat.agents import AssistantAgent, UserProxyAgent
from autogen_agentchat.conditions import TextMentionTermination
from autogen_agentchat.messages import (
    BaseAgentEvent,
    BaseChatMessage,
    SelectSpeakerEvent,
    TextMessage,
    ToolCallExecutionEvent,
    ToolCallRequestEvent,
    ToolCallSummaryMessage,
)
from autogen_agentchat.teams import SelectorGroupChat
from autogen_core import CancellationToken
from autogen_core.models import UserMessage

from app import log
from app.config import DEFAULT_MODEL
from app.state import state
from app.tools import TOOLS
from app.model_providers import get_model_client, model_supports_tools

HUMAN_PROXY_MODES = {"off", "consult", "delegate_safe", "autonomous_workspace"}
SMART_SUPERVISOR_MODES = {"auto", "smart_supervisor", "smartsupervisor"}
SMART_SUPERVISOR_TIMEOUT_SECONDS = 12.0
CONVERSATION_MODES = {
    "discussion",
    "debate",
    "brainstorm",
    "execution",
    "simulation",
    "storybook",
}
DELEGATE_SAFE_TOOL_NAMES = {
    "listdir",
    "read_file",
    "write_file",
    "replace_in_file",
    "share_file",
    "execute_python",
}
AUTONOMOUS_WORKSPACE_TOOL_NAMES = DELEGATE_SAFE_TOOL_NAMES | {"delete"}
TOOL_REFLECTION_GUIDANCE = (
    "\n\nWhen you use a tool, follow the tool result with a concise message to the room. "
    "Summarize the useful evidence, explain what it changes, and recommend the next step "
    "so teammates can build on your work instead of repeating the same tool call."
)
TASK_COMPLETE_MARKER = "TASK_COMPLETE"
HUMAN_ADMIN_REQUEST = "@Human_Admin"
SMART_SUPERVISOR_PROMPT = """You are the background discussion supervisor. Select exactly one next speaker from {participants}.

Use the latest conversation context and each participant's role:
- Route work to the specialist whose role is most relevant to the latest message.
- If a specialist is mentioned, questioned, or challenged, prefer that specialist when a response is useful.
- Let a critic or reviewer interrupt when the current plan has an important weakness.
- Let a mediator, facilitator, or lead summarize when discussion is looping or becoming unproductive.
- Prefer forward progress over a fixed speaking order. Do not force every participant to speak.
- A participant may speak again when they remain the best next speaker.

Available roles:
{roles}

Conversation:
{history}

Return only the selected participant name from {participants}.
"""


def _parse_tool_result(content: Any) -> Any:
    if not isinstance(content, str):
        return content
    for parser in (json.loads, ast.literal_eval):
        try:
            parsed = parser(content)
            json.dumps(parsed)
            return parsed
        except (ValueError, SyntaxError, TypeError, json.JSONDecodeError):
            continue
    return content


def _normalize_human_proxy_mode(mode: str | bool | None) -> str:
    if isinstance(mode, bool):
        return "consult" if mode else "off"
    normalized = (mode or "off").strip().lower()
    if normalized not in HUMAN_PROXY_MODES:
        raise OrchestratorError(
            user_message="Invalid human proxy mode.",
            developer_message=f"Unsupported human proxy mode: {mode}",
            error_code="invalid_human_proxy_mode",
        )
    return normalized


def _normalize_conversation_mode(mode: str | None) -> str:
    normalized = (mode or "discussion").strip().lower()
    if normalized not in CONVERSATION_MODES:
        raise OrchestratorError(
            user_message="Invalid conversation mode.",
            developer_message=f"Unsupported conversation mode: {mode}",
            error_code="invalid_conversation_mode",
        )
    return normalized


def _conversation_mode_prompt(mode: str) -> str:
    policies = {
        "discussion": (
            "Discuss naturally. Ask questions, share opinions, collaborate or disagree when appropriate, "
            "and follow your assigned role. Explore the topic without forcing a specialized interaction style."
        ),
        "debate": (
            "Pressure-test ideas. Challenge assumptions, present counterarguments, identify weaknesses, "
            "and defend positions with reasons. Consensus is optional; productive disagreement is valuable."
        ),
        "brainstorm": (
            "Prioritize idea generation. Suggest many useful alternatives, expand on teammates' ideas, "
            "and explore unconventional possibilities. Keep criticism brief and secondary until ideas exist."
        ),
        "execution": (
            "Focus on completing the task and producing concrete results. Create deliverables, documents, "
            "plans, code, reports, or proposals when appropriate. Avoid unnecessary philosophical discussion. "
            "Use available tools when they help, and create files when the work calls for files."
        ),
        "simulation": (
            "Treat the scenario as real and remain immersed. Act according to your role, relationships, "
            "motivations, incentives, and available information. Make decisions and respond to events naturally "
            "as a participant rather than an outside observer."
        ),
        "storybook": (
            "Contribute to an engaging evolving narrative. Advance the plot, develop characters, preserve "
            "continuity, and create dramatic or entertaining interactions. Narrative quality takes priority "
            "over strict realism."
        ),
    }
    return f"\n\nConversation mode: {mode.title()}. {policies[mode]}"


def _human_proxy_prompt(mode: str, preferences: str) -> str:
    authority = {
        "delegate_safe": (
            "You may inspect, create, and edit workspace files and run isolated Python snippets. "
            "You may not delete files, fetch external webpages, execute shell commands, publish, "
            "spend money, or claim the user approved an external action."
        ),
        "autonomous_workspace": (
            "You may inspect, create, edit, share, and delete files inside the active workspace and "
            "run isolated Python snippets. You may not fetch external webpages, execute shell commands, "
            "publish, spend money, or claim the user approved an external action."
        ),
    }[mode]
    preference_text = preferences.strip() or "No additional user preferences were supplied."
    return (
        "You are Human_Admin, a delegated coordinator for the human user. You are not a specialist team "
        "member. The task has already started, so never restart it or repeat the original goal as a new "
        "request. Respond only to the specific coordination request that invited you. Keep the team on "
        "track, answer routine questions, make reversible workspace decisions, and use your available "
        "tools when useful. If the requested objective is fully complete, briefly summarize the outcome "
        f"and end your message with {TASK_COMPLETE_MARKER}. If work remains, state the next concrete "
        "step and hand control back to the specialists. Never invent user approval. If a request is "
        "outside your authority, ambiguous in a consequential way, or requires a real-world commitment, "
        "explicitly ask the team to escalate to Human_Approval. Log delegated decisions briefly with the "
        "reason.\n\n"
        f"Authority boundary: {authority}\n\n"
        f"User preferences:\n{preference_text}"
    )


def _specialist_coordination_prompt(human_proxy_mode: str, conversation_mode: str = "discussion") -> str:
    if human_proxy_mode in {"delegate_safe", "autonomous_workspace"}:
        completion_instruction = (
            f"When the objective appears complete, ask {HUMAN_ADMIN_REQUEST} for a completion review. "
            f"Use {HUMAN_ADMIN_REQUEST} only for a concrete coordination need, blocker, or completion "
            "review. Do not treat Human_Admin as another specialist or ask it to continue the task."
        )
    elif human_proxy_mode == "consult":
        completion_instruction = (
            f"When the objective is fully complete, briefly summarize the outcome and end your message "
            f"with {TASK_COMPLETE_MARKER}. Use {HUMAN_ADMIN_REQUEST} only when real user input, approval, "
            "or clarification is genuinely needed."
        )
    else:
        completion_instruction = (
            f"When the objective is fully complete, briefly summarize the outcome and end your message "
            f"with {TASK_COMPLETE_MARKER}."
        )
    return (
        "\n\nScenario collaboration rules: The initial goal is already active. Continue the existing work "
        "instead of restarting or restating the goal as a new task. Produce concrete progress, build on "
        "prior messages, and avoid repeating completed work. Stay grounded in your assigned role and "
        "contribute primarily through that role's perspective, expertise, personality, and responsibilities. "
        "Do not become a generic assistant or take over another specialist's work when a teammate is better "
        "suited to it. Match the scenario's tone: collaboration may be structured, conversational, creative, "
        "playful, or exploratory. Do not force a work plan, checklist, research step, or formal deliverable "
        "unless the scenario, conversation, or selected conversation mode calls for one. "
        f"{completion_instruction}"
        f"{_conversation_mode_prompt(conversation_mode)}"
    )


def _relationship_prompt(agent_cfg: Dict[str, Any]) -> str:
    relationships = agent_cfg.get("relationships") or []
    lines = []
    for relationship in relationships:
        if not isinstance(relationship, dict):
            continue
        target = str(relationship.get("target") or "").strip()
        relation = str(relationship.get("relation") or "").strip()
        notes = str(relationship.get("notes") or "").strip()
        if not target or not relation or target == agent_cfg.get("name"):
            continue
        line = f"- You are {relation} to {target}."
        if notes:
            line += f" {notes}"
        lines.append(line)
    if not lines:
        return ""
    return (
        "\n\nKnown relationships:\n"
        + "\n".join(lines)
        + "\n\nUse your relationships to guide tone, trust, disagreement, loyalty, and information sharing. "
        "Do not mention relationship metadata directly unless it naturally fits the conversation. "
        "Relationships should influence behavior, not dominate every response."
    )


def _agent_tools_enabled(agent_cfg: Dict[str, Any], global_allow_tools: bool) -> bool:
    return global_allow_tools and bool(agent_cfg.get("tools_enabled", True))


def _message_requests_agent(
    messages: Sequence[BaseAgentEvent | BaseChatMessage],
    request_marker: str,
    *,
    excluded_sources: set[str] | None = None,
) -> bool:
    if not messages:
        return False
    last_message = messages[-1]
    if getattr(last_message, "source", "") in (excluded_sources or set()):
        return False
    return request_marker in str(getattr(last_message, "content", ""))


def _tools_for_human_proxy(mode: str) -> List[Any]:
    tool_names = DELEGATE_SAFE_TOOL_NAMES
    if mode == "autonomous_workspace":
        tool_names = AUTONOMOUS_WORKSPACE_TOOL_NAMES
    return [TOOLS[name] for name in sorted(tool_names)]


class OrchestratorError(Exception):
    def __init__(self, user_message: str, developer_message: str, error_code: str = "orchestrator_error"):
        super().__init__(developer_message)
        self.user_message = user_message
        self.developer_message = developer_message
        self.error_code = error_code

    def to_payload(self) -> Dict[str, Any]:
        return {
            "type": "status",
            "state": "error",
            "error_code": self.error_code,
            "user_message": self.user_message,
            "developer_message": self.developer_message,
        }


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
            "type": "tool_request",
            "sender": message.source,
            "tools": tool_names,
        }
    elif isinstance(message, ToolCallExecutionEvent):
        payload = {
            "type": "tool_result",
            "sender": message.source,
            "results": [_parse_tool_result(res.content) for res in message.content],
            "is_tool_execution": True,
        }
    elif isinstance(message, ToolCallSummaryMessage):
        return None
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
    human_proxy_mode: str | bool = "off",
    human_proxy_preferences: str = "",
    temperature: float = 0.3,
    allow_tools: bool = True,
    conversation_mode: str = "discussion",
):
    run_id = str(uuid.uuid4())
    log.info("orchestrator_start", extra={"run_id": run_id, "goal_len": len(goal), "model": model, "manager_mode": manager_mode, "conversation_mode": conversation_mode})
    try:
        model_name = model or DEFAULT_MODEL
        gemini_client = get_model_client(model_name, temperature=temperature)

        participants = []
        manager_mode = manager_mode.lower()
        human_proxy_mode = _normalize_human_proxy_mode(human_proxy_mode)
        conversation_mode = _normalize_conversation_mode(conversation_mode)
        live_coach_name = "Live_Coach"

        async def input_func(
            prompt: str = "",
            cancellation_token: Optional[CancellationToken] = None,
        ) -> str:
            if manager_mode in SMART_SUPERVISOR_MODES and human_proxy_mode == "off":
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
                except Exception as e:
                    log.error("input_func_error", extra={"run_id": run_id, "error": str(e), "type": type(e).__name__})
                    return "exit"

        async def coach_input_func(
            prompt: str = "",
            cancellation_token: Optional[CancellationToken] = None,
        ) -> str:
            del prompt, cancellation_token
            try:
                coach_msg = state.coach_input_q.get_nowait()
                if isinstance(coach_msg, str) and coach_msg.strip():
                    out_q.put(
                        json.dumps(
                            {
                                "type": "chat",
                                "sender": "System",
                                "message": "Live coach intervention delivered.",
                            }
                        )
                    )
                    return f"--- REAL-TIME USER STEERING ---\n{coach_msg.strip()}\n\nAgents: You MUST address this feedback immediately and adjust your plan if necessary."
            except queue.Empty:
                pass
            except Exception as e:
                log.error("coach_input_func_error", extra={"run_id": run_id, "error": str(e), "type": type(e).__name__})
            return "No real-time intervention. Continue with your current plan."

        live_coach_proxy = UserProxyAgent(
            name=live_coach_name,
            description="System agent for injecting real-time user steering. DO NOT SELECT THIS AGENT manually; it is triggered automatically when the user interrupts.",
            input_func=coach_input_func,
        )
        participants.append(live_coach_proxy)

        supports_tools = model_supports_tools(model_name)
        include_human_proxy = human_proxy_mode != "off"
        specialist_names = []
        if human_proxy_mode == "consult":
            user_proxy = UserProxyAgent(
                name="Human_Admin",
                description="The human user who started the task. You MUST select this agent when you need approval, feedback, clarification, or to present the final outcome.",
                input_func=input_func,
            )
            participants.append(user_proxy)
        elif include_human_proxy:
            human_admin_kwargs = {
                "name": "Human_Admin",
                "description": "Delegated representative for the human user. Select this agent for routine product-owner decisions and workspace actions.",
                "model_client": gemini_client,
                "system_message": _human_proxy_prompt(human_proxy_mode, human_proxy_preferences) + TOOL_REFLECTION_GUIDANCE,
                "model_client_stream": False,
                "reflect_on_tool_use": True,
            }
            if supports_tools and allow_tools:
                human_admin_kwargs["tools"] = _tools_for_human_proxy(human_proxy_mode)
            participants.append(AssistantAgent(**human_admin_kwargs))
            participants.append(
                UserProxyAgent(
                    name="Human_Approval",
                    description="The real human user. Select only when Human_Admin requests escalation for consequential, ambiguous, external, or out-of-policy actions.",
                    input_func=input_func,
                )
            )

        for agent_cfg in agents_cfg:
            # Input validation for agent_cfg
            if not isinstance(agent_cfg, dict):
                raise OrchestratorError(
                    user_message="Invalid agent configuration: each agent must be a dictionary.",
                    developer_message=f"Agent configuration is not a dictionary: {agent_cfg}",
                    error_code="invalid_agent_config"
                )
            if "name" not in agent_cfg or not agent_cfg["name"]:
                raise OrchestratorError(
                    user_message="Invalid agent configuration: 'name' is missing or empty.",
                    developer_message=f"Agent configuration missing 'name': {agent_cfg}",
                    error_code="invalid_agent_config"
                )
            if "system" not in agent_cfg or not agent_cfg["system"]:
                log.warning("agent_config_missing_system_message", extra={"run_id": run_id, "agent_name": agent_cfg["name"]})

            agent_kwargs = {
                "name": agent_cfg["name"],
                "model_client": gemini_client,
                "system_message": (
                    agent_cfg.get("system", "You are a helpful assistant.")
                    + _relationship_prompt(agent_cfg)
                    + _specialist_coordination_prompt(human_proxy_mode, conversation_mode)
                    + TOOL_REFLECTION_GUIDANCE
                ),
                "model_client_stream": False,
                "reflect_on_tool_use": True,
            }
            if supports_tools and _agent_tools_enabled(agent_cfg, allow_tools):
                agent_kwargs["tools"] = list(TOOLS.values())

            agent = AssistantAgent(**agent_kwargs)
            participants.append(agent)
            specialist_names.append(agent.name)

        selector_func = None
        def pick_live_coach_if_waiting() -> str | None:
            if not state.coach_input_q.empty():
                return live_coach_name
            return None

        def pick_human_approval_if_requested(
            messages: Sequence[BaseAgentEvent | BaseChatMessage],
        ) -> str | None:
            if human_proxy_mode not in {"delegate_safe", "autonomous_workspace"} or not messages:
                return None
            last_message = messages[-1]
            source = getattr(last_message, "source", "")
            content = str(getattr(last_message, "content", ""))
            if source == "Human_Admin" and "Human_Approval" in content:
                return "Human_Approval"
            return None

        def pick_human_admin_if_requested(
            messages: Sequence[BaseAgentEvent | BaseChatMessage],
        ) -> str | None:
            if not include_human_proxy:
                return None
            if _message_requests_agent(
                messages,
                HUMAN_ADMIN_REQUEST,
                excluded_sources={"Human_Admin", "Human_Approval"},
            ):
                return "Human_Admin"
            return None

        if manager_mode in {"round_robin", "roundrobin"}:
            rr_participants = [
                p for p in participants if p.name in specialist_names
            ]

            class RoundRobinSelector:
                def __init__(self, participants):
                    self.participants = participants
                    self.index = 0

                def __call__(self, messages: Sequence[BaseAgentEvent | BaseChatMessage]) -> str | None:
                    live = pick_live_coach_if_waiting()
                    if live:
                        return live
                    approval = pick_human_approval_if_requested(messages)
                    if approval:
                        return approval
                    admin = pick_human_admin_if_requested(messages)
                    if admin:
                        return admin
                    name = self.participants[self.index % len(self.participants)].name
                    self.index += 1
                    return name

            selector_func = RoundRobinSelector(rr_participants)

        elif manager_mode == "random":
            random_participants = [
                p for p in participants if p.name in specialist_names
            ]

            def random_selector_func(messages: Sequence[BaseAgentEvent | BaseChatMessage]) -> str | None:
                live = pick_live_coach_if_waiting()
                if live:
                    return live
                approval = pick_human_approval_if_requested(messages)
                if approval:
                    return approval
                admin = pick_human_admin_if_requested(messages)
                if admin:
                    return admin
                return random.choice(random_participants).name

            selector_func = random_selector_func

        elif manager_mode == "manual":

            async def manual_selector_func(messages: Sequence[BaseAgentEvent | BaseChatMessage]) -> str | None:
                approval = pick_human_approval_if_requested(messages)
                if approval:
                    return approval
                admin = pick_human_admin_if_requested(messages)
                if admin:
                    return admin
                out_q.put(json.dumps({"type": "status", "state": "waiting_for_manual_selection"}))
                while True:
                    live = pick_live_coach_if_waiting()
                    if live:
                        out_q.put(json.dumps({"type": "status", "state": "running"}))
                        return live
                    if state.stop_event.is_set():
                        return None
                    try:
                        next_agent_name = await asyncio.to_thread(state.manual_next_q.get, timeout=1.0)
                        out_q.put(json.dumps({"type": "status", "state": "running"}))
                        return next_agent_name
                    except queue.Empty:
                        continue
                    except Exception as e:
                        log.error("manual_selector_error", extra={"run_id": run_id, "error": str(e), "type": type(e).__name__})
                        return None

            selector_func = manual_selector_func
        else:

            def auto_with_live_coach_selector(
                messages: Sequence[BaseAgentEvent | BaseChatMessage],
            ) -> str | None:
                return (
                    pick_live_coach_if_waiting()
                    or pick_human_approval_if_requested(messages)
                    or pick_human_admin_if_requested(messages)
                )

            selector_func = auto_with_live_coach_selector

        def auto_candidate_func(
            messages: Sequence[BaseAgentEvent | BaseChatMessage],
        ) -> List[str]:
            del messages
            return specialist_names

        class SmartSupervisorSelector:
            def __init__(self) -> None:
                self.fallback_index = 0

            def _fallback(self) -> str:
                name = specialist_names[self.fallback_index % len(specialist_names)]
                self.fallback_index += 1
                return name

            async def select(
                self,
                messages: Sequence[BaseAgentEvent | BaseChatMessage],
            ) -> str | None:
                override = (
                    pick_live_coach_if_waiting()
                    or pick_human_approval_if_requested(messages)
                    or pick_human_admin_if_requested(messages)
                )
                if override:
                    return override

                roles = "\n".join(
                    f"- {agent_cfg['name']}: {agent_cfg.get('system', '')[:500]}"
                    for agent_cfg in agents_cfg
                    if agent_cfg.get("name") in specialist_names
                )
                history = "\n".join(
                    f"{getattr(message, 'source', 'unknown')}: {str(getattr(message, 'content', ''))[:1000]}"
                    for message in messages[-8:]
                )
                prompt = SMART_SUPERVISOR_PROMPT.format(
                    participants=", ".join(specialist_names),
                    roles=roles,
                    history=history,
                )
                out_q.put(json.dumps({"type": "status", "state": "supervising"}))
                try:
                    response = await asyncio.wait_for(
                        gemini_client.create(
                            messages=[UserMessage(content=prompt, source="user")],
                        ),
                        timeout=SMART_SUPERVISOR_TIMEOUT_SECONDS,
                    )
                    selected = str(getattr(response, "content", "")).strip()
                    for name in specialist_names:
                        if selected == name or re.search(rf"\b{re.escape(name)}\b", selected):
                            return name
                    log.warning(
                        "smart_supervisor_invalid_selection_fallback",
                        extra={"run_id": run_id, "selection": selected[:200]},
                    )
                except asyncio.TimeoutError:
                    log.warning(
                        "smart_supervisor_timeout_fallback",
                        extra={"run_id": run_id, "timeout_seconds": SMART_SUPERVISOR_TIMEOUT_SECONDS},
                    )
                except Exception as e:
                    log.warning(
                        "smart_supervisor_error_fallback",
                        extra={"run_id": run_id, "error": str(e), "type": type(e).__name__},
                    )
                finally:
                    out_q.put(json.dumps({"type": "status", "state": "running"}))
                return self._fallback()

        if manager_mode in SMART_SUPERVISOR_MODES:
            selector_func = SmartSupervisorSelector().select

        completion_sources = list(specialist_names)
        if human_proxy_mode in {"delegate_safe", "autonomous_workspace"}:
            completion_sources.append("Human_Admin")
        groupchat_kwargs = {
            "participants": participants,
            "max_turns": max_turns,
            "selector_func": selector_func,
            "candidate_func": auto_candidate_func if manager_mode in SMART_SUPERVISOR_MODES else None,
            "model_client": gemini_client,
            "allow_repeated_speaker": True,
            "termination_condition": TextMentionTermination(
                TASK_COMPLETE_MARKER,
                sources=completion_sources,
            ),
        }
        groupchat = SelectorGroupChat(
            **groupchat_kwargs,
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
                        extra={"run_id": run_id, "source": message.source, "type": type(message).__name__},
                    )

                if isinstance(message, SelectSpeakerEvent):
                    log.info("select_speaker_debug", extra={"run_id": run_id, "content": message.content})
                    current_speaker = message.content[0]

                payload = _create_graph_payload(message, groupchat, current_speaker)
                if payload:
                    out_q.put(json.dumps(payload))
        except asyncio.CancelledError:
            out_q.put(json.dumps({"type": "chat", "sender": "System", "message": "Conversation cancelled."}))
        finally:
            stop_monitor.cancel()

        out_q.put(json.dumps({"type": "status", "state": "idle"}))

    except OrchestratorError as e:
        out_q.put(json.dumps(e.to_payload()))
        log.error("orchestrator_known_error", extra={"run_id": run_id, "error_code": e.error_code, "user_message": e.user_message, "developer_message": e.developer_message}, exc_info=e)
    except Exception as e:
        error_str = str(e)
        if state.stop_event.is_set():
            log.info(
                "orchestrator_stopped_without_error",
                extra={"run_id": run_id, "error": error_str, "type": type(e).__name__},
            )
            return
        user_message = "An unexpected error occurred during orchestration. Please check the logs for details."
        developer_message = f"{type(e).__name__}: {error_str}"
        if "'NoneType' object is not subscriptable" in error_str:
            user_message = "API Error: The model returned an empty response. This usually indicates a safety filter trigger or invalid configuration."
            developer_message = "Model returned NoneType object, possibly due to safety filter or invalid config."
        elif any(marker in error_str.lower() for marker in ("503", "unavailable", "high demand")):
            user_message = "The selected AI model is temporarily busy. Please try again shortly."

        out_q.put(json.dumps(OrchestratorError(user_message, developer_message).to_payload()))
        log.error("orchestrator_unexpected_error", extra={"run_id": run_id, "error": error_str, "type": type(e).__name__}, exc_info=e)
    finally:
        out_q.put("[DONE]")
