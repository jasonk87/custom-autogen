from types import SimpleNamespace
from unittest import mock

import pytest

from app.scenario import _normalize_agent_names, _strip_json_fence, generate_agents_from_scenario, generate_scenario_idea


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


@pytest.mark.asyncio
async def test_generate_scenario_idea_uses_one_model_call_and_extracts_json():
    client = mock.AsyncMock()
    client.create.return_value = SimpleNamespace(
        content='{"scenario":"Treasure hunters discover an abandoned observatory beneath a flooded city."}'
    )
    with mock.patch("app.scenario.get_model_client", return_value=client):
        idea = await generate_scenario_idea("gemini-test")

    assert idea == "Treasure hunters discover an abandoned observatory beneath a flooded city."
    client.create.assert_awaited_once()


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
    assert all(agent["relationships"] == [] for agent in agents)
    assert all(agent["tools_enabled"] is True for agent in agents)


def test_normalize_agent_names_filters_relationships_and_renames_valid_targets():
    agents = _normalize_agent_names(
        [
            {
                "name": "Captain Mara",
                "relationships": [
                    {"target": "Engineer Tomas", "relation": "Boss", "notes": "Keep the ship alive."},
                    {"target": "Captain Mara", "relation": "Trusted", "notes": "Invalid self reference."},
                    {"target": "Missing", "relation": "Enemy", "notes": "Invalid missing reference."},
                ],
            },
            {"name": "Engineer Tomas", "tools_enabled": False},
        ]
    )

    assert agents[0]["relationships"] == [
        {"target": "Engineer_Tomas", "relation": "Boss", "notes": "Keep the ship alive."}
    ]
    assert agents[1]["tools_enabled"] is False


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

    assert agents == [
        {
            "name": "Pilot",
            "system": "Lead the crew.",
            "temperature": 0.3,
            "tools_enabled": True,
            "relationships": [],
        }
    ]
    assert goal == "Land safely."


@pytest.mark.asyncio
async def test_generate_agents_keeps_valid_relationships_and_tool_permissions():
    client = mock.AsyncMock()
    client.create.side_effect = [
        SimpleNamespace(
            content=(
                '{"agents":['
                '{"name":"Captain","system":"Lead.","temperature":0.4,"tools_enabled":false,'
                '"relationships":[{"target":"Engineer","relation":"Boss","notes":"Trust their judgment."}]},'
                '{"name":"Engineer","system":"Repair.","temperature":0.2,"tools_enabled":true,'
                '"relationships":[{"target":"Captain","relation":"Employee","notes":""}]}]}'
            )
        ),
        SimpleNamespace(content='{"goal":"Repair the ship."}'),
    ]

    with mock.patch("app.scenario.get_model_client", return_value=client):
        agents, goal = await generate_agents_from_scenario("Damaged starship", "gemini-test", 2)

    assert agents[0]["tools_enabled"] is False
    assert agents[0]["relationships"][0]["target"] == "Engineer"
    assert agents[1]["relationships"][0]["relation"] == "Employee"
    assert goal == "Repair the ship."
