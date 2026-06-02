from types import SimpleNamespace
from unittest import mock

import pytest

from app.scenario import _normalize_agent_names, _strip_json_fence, generate_agents_from_scenario


@pytest.mark.parametrize(
    ("content", "expected"),
    [
        ('{"goal": "Land safely."}', '{"goal": "Land safely."}'),
        ('```json\n{"goal": "Land safely."}\n```', '{"goal": "Land safely."}'),
        ('```\n{"goal": "Land safely."}\n```', '{"goal": "Land safely."}'),
    ],
)
def test_strip_json_fence(content, expected):
    assert _strip_json_fence(content) == expected


def test_normalize_agent_names_produces_unique_python_identifiers():
    agents = _normalize_agent_names(
        [
            {"name": "Alex the Analyst"},
            {"name": "Alex the Analyst"},
            {"name": "123 Critic!"},
        ]
    )
    assert [agent["name"] for agent in agents] == [
        "Alex_the_Analyst",
        "Alex_the_Analyst_2",
        "Agent_3_123_Critic",
    ]
    assert all(agent["name"].isidentifier() for agent in agents)


@pytest.mark.asyncio
async def test_generate_agents_extracts_goal_from_fenced_json():
    client = mock.AsyncMock()
    client.create.side_effect = [
        SimpleNamespace(
            content='{"agents":[{"name":"Pilot","system":"Lead the crew.","temperature":0.3}]}'
        ),
        SimpleNamespace(content='```json\n{"goal":"Land safely."}\n```'),
    ]

    with mock.patch("app.scenario.get_model_client", return_value=client):
        agents, goal = await generate_agents_from_scenario("Emergency landing", "gemini-test", 1)

    assert agents == [{"name": "Pilot", "system": "Lead the crew.", "temperature": 0.3}]
    assert goal == "Land safely."
