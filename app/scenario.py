import json
from typing import Any, Dict, List
from pydantic import BaseModel, Field
from autogen_ext.models.openai import OpenAIChatCompletionClient
from autogen_agentchat.messages import TextMessage
from autogen_core.models import SystemMessage, UserMessage, ModelFamily

from app.config import GEMINI_API_KEY, DEFAULT_MODEL

# Pydantic models for structured output
class AgentConfig(BaseModel):
    name: str = Field(..., description="A short, descriptive name for the agent (e.g., 'Programmer', 'President_A'). Use only letters, numbers, and underscores.")
    system: str = Field(..., description="A detailed system message that defines the agent's role, personality, and capabilities.")
    temperature: float = Field(default=0.3, description="The temperature setting for the agent's responses.")

class AgentList(BaseModel):
    agents: List[AgentConfig]

async def generate_agents_from_scenario(scenario: str, model: str, num_agents: int = 3, file_context: str = "") -> List[Dict[str, Any]]:
    """
    Generates a list of agents from a scenario description using an LLM
    with structured output (JSON schema).
    """
    # Use OpenAIChatCompletionClient for Gemini
    client = OpenAIChatCompletionClient(
        model=model or DEFAULT_MODEL,
        api_key=GEMINI_API_KEY,
        base_url="https://generativelanguage.googleapis.com/v1beta/openai/"
    )

    prompt = f"""
You are an expert technical lead and agent architect.
Your task is to analyze the following User Scenario and the current Project Context (existing files).
Then, generate a list of {num_agents} specialized AI agents that can collaborate to fulfill the scenario.

User Scenario:
"{scenario}"

Project Context (Existing Files):
{file_context}

Instructions:
1. Analyze the existing files to understand the current state of the project.
2. Design agents that complement specific roles needed to extend the current codebase. Avoid generic roles if specific verification or refactoring is needed.
3. The agents should have diverse capabilities (coding, reviewing, testing, planning).

Please generate a JSON object that conforms to the following schema:
{AgentList.model_json_schema()}
"""

    response = await client.create(
        messages=[UserMessage(content=prompt, source="user")]
    )

    response_text = response.content

    # The response content is a JSON string, which we can validate with our Pydantic model
    # It might be wrapped in ```json ... ``` so we clean it.
    if "```json" in response_text:
        response_text = response_text.split("```json")[1].split("```")[0].strip()
    elif "```" in response_text:
        response_text = response_text.split("```")[1].split("```")[0].strip()

    try:
        agent_list_obj = AgentList.model_validate_json(response_text)
    except Exception:
        # Fallback if parsing fails, maybe just return empty or retry (for now, empty/error)
        # Try to clean harder or just fail
        agent_list_obj = AgentList.model_validate_json(response_text)


    # Convert the Pydantic models back to dictionaries for the rest of the app
    agents = [agent.model_dump() for agent in agent_list_obj.agents]

    # Now, make a second call to generate a team goal
    goal_prompt = f"""
You are an expert Technical Project Manager.
Context:
- Scenario: "{scenario}"
- Current Files: {file_context}
- Team: {", ".join(a['name'] for a in agents)}

Task:
Write a DETAILED, STEP-BY-STEP Implementation Plan (Goal) for this team.
The goal should be a single comprehensive string (can use markdown).
It must:
1. Reference specific existing files if they need modification.
2. Break down the scenario into technical steps (e.g., "Step 1: Create x", "Step 2: Modify y").
3. Assign rough responsibilities to the agents implicitly by the nature of the steps.
4. Be actionable and rigorous.

Output only the plan text.
"""

    goal_response = await client.create(
        messages=[UserMessage(content=goal_prompt, source="user")]
    )
    suggested_goal = goal_response.content.strip()

    return agents, suggested_goal


