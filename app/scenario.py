import json
from typing import Any, Dict, List
from pydantic import BaseModel, Field
from autogen_ext.models.openai import OpenAIChatCompletionClient
from autogen_agentchat.messages import TextMessage

from app.config import GEMINI_API_KEY, DEFAULT_MODEL

# Pydantic models for structured output
class AgentConfig(BaseModel):
    name: str = Field(..., description="A short, descriptive name for the agent (e.g., 'Programmer', 'President_A'). Use only letters, numbers, and underscores.")
    system: str = Field(..., description="A detailed system message that defines the agent's role, personality, and capabilities.")
    temperature: float = Field(default=0.3, description="The temperature setting for the agent's responses.")

class AgentList(BaseModel):
    agents: List[AgentConfig]

async def generate_agents_from_scenario(scenario: str, model: str, num_agents: int = 3) -> List[Dict[str, Any]]:
    """
    Generates a list of agents from a scenario description using an LLM
    with structured output (JSON schema).
    """
    # Use OpenAIChatCompletionClient for Gemini
    client = OpenAIChatCompletionClient(
        model=model or DEFAULT_MODEL,
        api_key=GEMINI_API_KEY,
        base_url="https://generativelanguage.googleapis.com/v1beta/openai/",
        temperature=0.1
    )

    prompt = f"""
You are an expert agent creator.
Your task is to generate a list of {num_agents} agents that would be suitable for the following scenario:
"{scenario}"

The agents should have diverse roles and capabilities to effectively collaborate on the scenario.
Please generate a JSON object that conforms to the provided schema.
"""

    # We need to manually enforce JSON output since OpenAIChatCompletionClient might not support `format='json'` directly in the same way as Ollama client or it might differ.
    # However, Gemini supports response_format={"type": "json_object"} if we were using the google client directly, but through OpenAI compat layer it should also work if supported.
    # But for safety, let's ask for JSON in the prompt and parse it.

    # Actually, AutoGen's client `create` method takes `response_format`.
    # But `OpenAIChatCompletionClient` is a `ChatCompletionClient`.
    # Let's use `create` if available or `create_stream`.

    # Wait, `client.create` returns a `CreateResult`.

    response = await client.create(
        messages=[TextMessage(content=prompt, source="user")],
        response_format={"type": "json_object"}
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
Based on the following scenario:
"{scenario}"

And the following team of agents that has been created:
{json.dumps(agents, indent=2)}

Please generate a single, concise, and actionable "Team Goal" for this team to accomplish.
The goal should be a clear instruction that can be given to the team to start their work.
Please only output the goal as a single string.
"""

    goal_response = await client.create(
        messages=[TextMessage(content=goal_prompt, source="user")],
        temperature=0.5
    )
    suggested_goal = goal_response.content.strip()

    return agents, suggested_goal
