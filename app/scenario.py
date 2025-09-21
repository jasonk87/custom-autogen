import json
import re
from typing import Any, Dict, List

from autogen_ext.models.ollama import OllamaChatCompletionClient

from app.config import OLLAMA_BASE_URL

def _extract_json_from_response(response: str) -> List[Dict[str, Any]]:
    """
    Extracts a JSON array from the LLM's response.
    The response might contain the JSON within a code block or with other text.
    """
    # Find the start of the JSON array
    json_start = response.find('[')
    # Find the end of the JSON array
    json_end = response.rfind(']')
    if json_start == -1 or json_end == -1:
        raise ValueError("No JSON array found in the response")

    json_str = response[json_start:json_end+1]

    try:
        return json.loads(json_str)
    except json.JSONDecodeError:
        # The JSON might be escaped within a string, so we can try to unescape it
        try:
            return json.loads(json.loads(f'"{json_str}"'))
        except (json.JSONDecodeError, TypeError):
            raise ValueError("Failed to decode JSON from the response")

async def generate_agents_from_scenario(scenario: str, model: str) -> List[Dict[str, Any]]:
    """
    Generates a list of agents from a scenario description using an LLM.
    """
    client = OllamaChatCompletionClient(
        model=model,
        host=OLLAMA_BASE_URL,
    )

    prompt = f"""
You are an expert agent creator.
Your task is to generate a list of agents that would be suitable for the following scenario:
"{scenario}"

Please generate a JSON array of agent objects. Each object should have the following properties:
- "name": A short, descriptive name for the agent (e.g., "Programmer", "President_A"). Use only letters, numbers, and underscores.
- "system": A detailed system message that defines the agent's role, personality, and capabilities.

Example output for the scenario "two presidents debating climate change":
[
    {{
        "name": "President_A",
        "system": "You are the president of a developed country. You are concerned about the economic impact of climate change policies."
    }},
    {{
        "name": "President_B",
        "system": "You are the president of a developing country. You are concerned about the impact of climate change on your country's environment and population."
    }}
]

Now, generate the agents for the scenario: "{scenario}"
Please only output the JSON array of agents.
"""

    response = await client.create(
        messages=[{"role": "user", "content": prompt}],
        temperature=0.1,
    )

    response_text = response.choices[0].message.content

    agents = _extract_json_from_response(response_text)

    # Add a default temperature to each agent
    for agent in agents:
        agent["temperature"] = 0.3

    return agents
