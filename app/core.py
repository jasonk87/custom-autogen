import json
import time
from typing import Any, Dict, Generator, List, Optional, Tuple

from app import log
from app.config import COLLAB_GUIDANCE
from app.ollama import get_ollama_endpoints, _http
from app.state import state
from app.tools import TOOLS
from app.utils import (
    CODE_FENCE_RE,
    ROLE_HINTS,
    TOOL_RE,
    execute_python,
)

class Agent:
    def __init__(self, name: str, system: str, temperature: float = 0.3):
        self.name = name
        self.system = system
        self.temperature = temperature

    def stream(self, history: List[Dict[str, str]], model: str, moderator_message: Optional[str] = None) -> Generator[str, None, None]:
        _, chat_url = get_ollama_endpoints()
        roster = ", ".join(sorted({m.get("name") for m in history if m.get("name")} - {None})) or ""
        sysmsg = self.system + "\n\n" + COLLAB_GUIDANCE + (f"\nTeam: {roster}" if roster else "")
        if moderator_message:
            sysmsg = f"Moderator feedback: {moderator_message}\n\n{sysmsg}"
        messages = [{"role": "system", "content": sysmsg}] + history

        attempts = 4
        for attempt in range(attempts):
            try:
                with _http.stream(
                    "POST",
                    chat_url,
                    json={
                        "model": model,
                        "messages": messages,
                        "options": {"temperature": self.temperature},
                        "stream": True,
                    },
                ) as r:
                    r.raise_for_status()
                    for line in r.iter_lines():
                        if not line:
                            continue
                        try:
                            data = json.loads(line)
                        except Exception:
                            continue
                        msg = data.get("message") or {}
                        delta = msg.get("content")
                        if delta:
                            yield delta
                        if data.get("done") is True:
                            return
            except Exception as e:
                if attempt == attempts - 1:
                    raise
                sleep_s = 0.5 * (2**attempt)
                log.warning(
                    "ollama_stream_retry",
                    extra={"extra": {"attempt": attempt + 1, "sleep": sleep_s, "err": str(e)}},
                )
                time.sleep(sleep_s)


class HumanAdmin(Agent):
    def __init__(self):
        super().__init__(
            "Human_Admin",
            "You are the human. Provide brief feedback or code. Use TERMINATE to finish when satisfied.",
        )

    def stream(self, history: List[Dict[str, str]], model: str, moderator_message: Optional[str] = None):  # type: ignore[override]
        msg = state.user_input_q.get()
        if msg is None:
            return
        if "```python" in (msg or ""):
            code = msg.split("```python", 1)[1].split("```", 1)[0]
            out = execute_python(code)
            yield f"Executed code. Output:\n{out}"
        else:
            yield msg


class Manager:
    """
    Central controller that selects next speaker and decides termination.
    """

    def __init__(self, names: List[str], mode: str = "Directed"):
        self.names = names[:]
        self.mode = mode
        self._rr = 0
        self.last_artifact_turn = -1
        self.has_reviewed_since_artifact = False
        self.has_ux_since_artifact = False
        self.turn = 0
        self.roles: Dict[str, str] = {}
        for n in self.names:
            nl = n.lower()
            role = "other"
            if any(k in nl for k in ROLE_HINTS["programmer"]["keywords"]):
                role = "programmer"
            elif any(k in nl for k in ROLE_HINTS["reviewer"]["keywords"]):
                role = "reviewer"
            elif any(k in nl for k in ROLE_HINTS["ux"]["keywords"]):
                role = "ux"
            elif n == "Human_Admin":
                role = "human"
            self.roles[n] = role

    def note_message(self, speaker: str, content: str) -> None:
        if CODE_FENCE_RE.search(content) or "tool:write_file" in content:
            self.last_artifact_turn = self.turn
            self.has_reviewed_since_artifact = False
            self.has_ux_since_artifact = False
        role = self.roles.get(speaker, "other")
        if role == "reviewer" and self.last_artifact_turn >= 0:
            self.has_reviewed_since_artifact = True
        if role == "ux" and self.last_artifact_turn >= 0:
            self.has_ux_since_artifact = True
        self.turn += 1

    def choose(self, history: List[Dict[str, str]]) -> Tuple[str, str]:
        if self.mode == "Manual":
            try:
                who = state.manual_next_q.get(timeout=3600)
                if who in self.names:
                    return who, "manual selection"
            except Exception:
                pass
            return "Human_Admin", "manual timeout fallback"

        if self.mode == "RoundRobin":
            pick = self.names[self._rr % len(self.names)]
            self._rr += 1
            return pick, "round-robin"

        prog = self._first_by_role("programmer")
        rev = self._first_by_role("reviewer")
        ux = self._first_by_role("ux")
        human = "Human_Admin" if "Human_Admin" in self.names else None

        if self.last_artifact_turn < 0 and prog:
            return prog, "directed: build phase (no artifact yet)"
        if self.last_artifact_turn >= 0:
            if rev and not self.has_reviewed_since_artifact:
                return rev, "directed: post-artifact review"
            if ux and not self.has_ux_since_artifact:
                return ux, "directed: post-artifact ux"
        if prog:
            return prog, "directed: refine"
        if human:
            return human, "directed: human check"

        pick = self.names[self._rr % len(self.names)]
        self._rr += 1
        return pick, "directed: fallback round-robin"

    def should_terminate(self) -> bool:
        if self.mode != "Directed":
            return False
        if self.last_artifact_turn < 0:
            return False
        rev = self._first_by_role("reviewer")
        ux = self._first_by_role("ux")
        if rev and not self.has_reviewed_since_artifact:
            return False
        if ux and not self.has_ux_since_artifact:
            return False
        return True

    def _first_by_role(self, role: str) -> Optional[str]:
        for n in self.names:
            if self.roles.get(n) == role:
                return n
        return None


class Moderator:
    """
    A class to moderate the conversation and provide private feedback to agents.
    """

    def moderate(self, history: List[Dict[str, str]], goal: str, model: str) -> Optional[str]:
        """
        Reviews the conversation and returns a private corrective message if needed.
        """
        _, chat_url = get_ollama_endpoints()

        history_str = "\n".join([f"{m.get('name') or m.get('role')}: {m.get('content')}" for m in history])

        system_prompt = f"""You are a conversation moderator for a team of AI agents. Your role is to ensure the team stays on track to achieve the following goal: {goal}.

You will be given the full conversation history. Your task is to determine if the conversation is deviating from the goal.

- If the conversation is on track, or if the agents are having a brief, constructive side-conversation (e.g., complimenting each other), you should not intervene. In this case, your response should be "None".
- If an agent is not following its instructions, or if the conversation is going off-topic, you should provide a brief, private, and corrective message to the next agent. This message should guide the agent back to the task at hand.
- Do not be overly strict. Allow for some natural conversation flow.
- Your feedback should be a single line of text.
"""

        prompt = f"""Conversation History:
{history_str}

Based on the conversation history and the goal, what is your private feedback for the next agent? If no intervention is needed, respond with "None".
"""

        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": prompt}
        ]

        try:
            r = _http.post(
                chat_url,
                json={
                    "model": model,
                    "messages": messages,
                    "options": {"temperature": 0.1},
                    "stream": False,
                },
            )
            r.raise_for_status()
            response_data = r.json()
            feedback = response_data.get("message", {}).get("content", "").strip()

            if feedback.lower() == "none" or not feedback:
                return None

            return feedback
        except Exception as e:
            log.warning("moderator_failed", extra={"extra": {"err": str(e)}})
            return None

def default_system(name: str) -> str:
    n = name.lower()
    if any(k in n for k in ["programmer", "developer", "coder", "engineer"]):
        return "You are a Python Developer. Focus on code and artifacts.\n" + COLLAB_GUIDANCE
    if any(k in n for k in ["critic", "review", "reviewer", "qa", "tester"]):
        return "You are a Code Reviewer. Critique and suggest precise fixes.\n" + COLLAB_GUIDANCE
    if any(k in n for k in ["ux", "ui", "designer", "design"]):
        return "You are a UX/UI Designer. Improve affordances, layout, naming, readability.\n" + COLLAB_GUIDANCE
    return f"You are a collaborator named {name}.\n" + COLLAB_GUIDANCE

def run_orchestrator(goal: str, model: str, agents_cfg: List[Dict[str, Any]], manager_mode: str, out_q: "queue.Queue[str]", max_turns: int = 60):
    try:
        history: List[Dict[str, str]] = []
        agents: Dict[str, Agent] = {
            cfg["name"]: Agent(
                cfg["name"],
                (cfg.get("system") or default_system(cfg["name"])),
                temperature=float(cfg.get("temperature", 0.3)),
            ) for cfg in agents_cfg
        }
        agents["Human_Admin"] = HumanAdmin()
        names = list(agents.keys())
        mgr = Manager(names, manager_mode)
        moderator = Moderator()
        moderator_feedback: Optional[str] = None

        history.append({"role": "user", "content": f"Goal: {goal}"})
        out_q.put(json.dumps({"type": "chat", "sender": "System", "message": f"Goal set: {goal}"}))

        for turn in range(max_turns):
            if state.stop_event.is_set():
                break

            next_name, rationale = mgr.choose(history)
            if next_name not in agents:
                out_q.put(json.dumps({"type": "chat", "sender": "Error", "message": f"Manager picked unknown agent: {next_name}"}))
                break

            out_q.put(json.dumps({"type": "chat", "sender": "Manager", "message": f"Next: {next_name}  ·  reason: {rationale}"}))

            speaker = agents[next_name]
            msg_id = f"t{turn}_{next_name}"

            out_q.put(json.dumps({"type": "status", "state": "waiting_for_input" if isinstance(speaker, HumanAdmin) else "running"}))
            out_q.put(json.dumps({"type": "stream_start", "id": msg_id, "sender": next_name}))

            assembled: List[str] = []
            try:
                for delta in speaker.stream(history, model, moderator_feedback) or []:
                    if state.stop_event.is_set():
                        break
                    assembled.append(delta)
                    out_q.put(json.dumps({"type": "token", "id": msg_id, "delta": delta}))
            except Exception as e:
                out_q.put(json.dumps({"type": "chat", "sender": next_name, "message": f"[stream error] {e}"}))
            finally:
                out_q.put(json.dumps({"type": "stream_end", "id": msg_id}))

            full_msg = ''.join(assembled)

            if next_name != "Human_Admin" and "TERMINATE" in (full_msg or "").upper():
                log.info("terminate_ignored", extra={"extra": {"by": next_name}})
                full_msg = full_msg.replace("TERMINATE", "")

            history.append({"role": "assistant", "name": next_name, "content": full_msg})
            mgr.note_message(next_name, full_msg)

            for m in TOOL_RE.finditer(full_msg or ""):
                tool_name = m.group("name").strip()
                try:
                    args = json.loads(m.group("body"))
                    fn = TOOLS.get(tool_name)
                    if not fn:
                        out_q.put(json.dumps({"type": "chat", "sender": "tool", "message": f"unknown tool: {tool_name}"}))
                    else:
                        log.info("tool_call", extra={"extra": {"tool": tool_name, "args": args}})
                        res = fn(**args)
                        if not isinstance(res, str):
                            res = json.dumps(res, indent=2)
                        out_q.put(json.dumps({"type": "chat", "sender": "tool", "message": f"{tool_name} ->\n{res}"}))
                except Exception as e:
                    out_q.put(json.dumps({"type": "chat", "sender": "tool", "message": f"{tool_name} error: {e}"}))

            if mgr.should_terminate():
                out_q.put(json.dumps({"type": "chat", "sender": "Manager", "message": "All required roles completed post-artifact. TERMINATE"}))
                break

            moderator_feedback = moderator.moderate(history, goal, model)
            if moderator_feedback:
                log.info("moderator_intervention", extra={"extra": {"feedback": moderator_feedback}})

        out_q.put(json.dumps({"type": "status", "state": "idle"}))

    except Exception as e:
        out_q.put(json.dumps({"type": "chat", "sender": "Error", "message": f"{type(e).__name__}: {e}"}))
    finally:
        out_q.put("[DONE]")
