import json
from typing import Any, Dict, List
from pydantic import BaseModel, Field
import ollama

from app.config import OLLAMA_BASE_URL

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
    client = ollama.AsyncClient(host=OLLAMA_BASE_URL)

    prompt = f"""
You are an expert agent creator.
Your task is to generate a list of {num_agents} agents that would be suitable for the following scenario:
"{scenario}"

The agents should have diverse roles and capabilities to effectively collaborate on the scenario.
Please generate a JSON object that conforms to the provided schema.
"""

    response = await client.chat(
        model=model,
        messages=[{"role": "user", "content": prompt}],
        format='json',
        options={"temperature": 0.1}
    )

    # The response content is a JSON string, which we can validate with our Pydantic model
    response_text = response['message']['content']
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

    goal_response = await client.chat(
        model=model,
        messages=[{"role": "user", "content": goal_prompt}],
        options={"temperature": 0.5}
    )
    suggested_goal = goal_response['message']['content'].strip()

    return agents, suggested_goal
