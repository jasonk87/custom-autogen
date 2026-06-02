import json
import keyword
import re
from typing import Any, Dict, List
from pydantic import BaseModel, Field
from autogen_core.models import UserMessage
from app.config import DEFAULT_MODEL
from app.model_providers import get_model_client

# Pydantic models for structured output
class AgentConfig(BaseModel):
    name: str = Field(..., description="A short, descriptive name for the agent (e.g., 'Programmer', 'President_A'). Use only letters, numbers, and underscores.")
    system: str = Field(..., description="A detailed system message that defines the agent's role, personality, and capabilities.")
    temperature: float = Field(default=0.3, description="The temperature setting for the agent's responses.")
    tools_enabled: bool = Field(default=True, description="Whether this agent needs access to external tools.")
    relationships: List[Dict[str, str]] = Field(default_factory=list, description="Social relationships to other generated agents.")

class AgentList(BaseModel):
    agents: List[AgentConfig]


def _strip_json_fence(content: str) -> str:
    text = (content or "").strip()
    if text.startswith("```"):
        first_newline = text.find("\n")
        if first_newline != -1:
            text = text[first_newline + 1:]
        if text.endswith("```"):
            text = text[:-3]
    return text.strip()


def _normalize_agent_names(agents: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    used_names = set()
    normalized_agents = []
    original_to_normalized = {}
    for index, agent in enumerate(agents, start=1):
        original_name = str(agent.get("name", ""))
        name = re.sub(r"\W+", "_", original_name, flags=re.ASCII).strip("_")
        if not name or name[0].isdigit() or keyword.iskeyword(name):
            name = f"Agent_{index}_{name}" if name else f"Agent_{index}"
        candidate = name
        suffix = 2
        while candidate in used_names:
            candidate = f"{name}_{suffix}"
            suffix += 1
        used_names.add(candidate)
        original_to_normalized[original_name] = candidate
        normalized_agents.append(
            {
                **agent,
                "name": candidate,
                "tools_enabled": bool(agent.get("tools_enabled", True)),
                "relationships": list(agent.get("relationships") or []),
            }
        )
    valid_names = {agent["name"] for agent in normalized_agents}
    for agent in normalized_agents:
        relationships = []
        for relationship in agent["relationships"]:
            if not isinstance(relationship, dict):
                continue
            target = original_to_normalized.get(str(relationship.get("target", "")), str(relationship.get("target", "")))
            relation = str(relationship.get("relation", "")).strip()
            notes = str(relationship.get("notes", "")).strip()
            if not target or target == agent["name"] or target not in valid_names or not relation:
                continue
            relationships.append({"target": target, "relation": relation, "notes": notes})
        agent["relationships"] = relationships
    return normalized_agents


async def generate_agents_from_scenario(
    scenario: str,
    model: str,
    num_agents: int = 3,
    conversation_mode: str = "discussion",
) -> List[Dict[str, Any]]:
    """
    Generates a list of agents from a scenario description using an LLM
    with structured output (JSON schema).
    """
    # Use get_model_client to get either Gemini or Ollama client
    model_name = model or DEFAULT_MODEL
    client = get_model_client(
        model_name, 
        temperature=0.1
    )

    prompt = f"""
You are an expert agent creator.
Your task is to generate a list of {num_agents} agents that would be suitable for the following scenario:
"{scenario}"

The selected conversation mode is: "{conversation_mode}".

The agents should have diverse roles and capabilities to effectively collaborate on the scenario.
For each agent, decide whether tools should be enabled. In simulation or storybook scenarios, default
tools_enabled to false unless the agent specifically needs tools. In execution or research-heavy scenarios,
enable tools only for agents whose role requires research, file writing, coding, analysis, or concrete output
generation. Relationships must only reference agents in this generated roster, must not target the same agent,
and should be asymmetric where appropriate. Do not force every agent to have relationships.
You MUST output ONLY a valid JSON object representing the agents. Follow this exact structure:
{{
  "agents": [
    {{
      "name": "Programmer_Bob",
      "system": "You are a skilled programmer...",
      "temperature": 0.3,
      "tools_enabled": true,
      "relationships": [
        {{
          "target": "Designer_Ava",
          "relation": "Coworker",
          "notes": "You value Ava's product judgment."
        }}
      ]
    }}
  ]
}}
Do NOT output the JSON schema. Output the actual instantiated agents!
"""

    # We need to manually enforce JSON output since OpenAIChatCompletionClient might not support `format='json'` directly in the same way as Ollama client or it might differ.
    # However, Gemini supports response_format={"type": "json_object"} if we were using the google client directly, but through OpenAI compat layer it should also work if supported.
    # But for safety, let's ask for JSON in the prompt and parse it.

    # Actually, AutoGen's client `create` method takes `response_format`.
    # But `OpenAIChatCompletionClient` is a `ChatCompletionClient`.
    # Let's use `create` if available or `create_stream`.

    # Wait, `client.create` returns a `CreateResult`.

    response = await client.create(
        messages=[UserMessage(content=prompt, source="user")]
    )

    response_text = _strip_json_fence(response.content)
    agent_list_obj = AgentList.model_validate_json(response_text)


    # Convert the Pydantic models back to dictionaries for the rest of the app
    agents = _normalize_agent_names([agent.model_dump() for agent in agent_list_obj.agents])

    # Now, make a second call to generate a team goal
    goal_prompt = f"""
Based on the following scenario:
"{scenario}"

And the following team of agents that has been created:
{json.dumps(agents, indent=2)}

Please generate a single, concise, and actionable "Team Goal" for this team to accomplish.
The goal should be a clear instruction that can be given to the team to start their work.
Please only output the goal as a single string in the following JSON format: {{"goal": "the goal"}}
"""

    # For the goal, we still expect JSON because we set response_format={"type": "json_object"} on the client.
    goal_response = await client.create(
        messages=[UserMessage(content=goal_prompt, source="user")]
    )

    try:
        goal_data = json.loads(_strip_json_fence(goal_response.content))
        suggested_goal = goal_data.get("goal", "")
    except Exception:
        suggested_goal = goal_response.content.strip()

    return agents, suggested_goal
