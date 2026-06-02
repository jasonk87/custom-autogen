from types import SimpleNamespace
from unittest import mock

import pytest

from app.scenario import _infer_conversation_mode, _normalize_agent_names, _normalize_conversation_mode, _normalize_scenario_agent_count, _strip_json_fence, generate_agents_from_scenario, generate_scenario_idea


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
        content='{"scenario":"Treasure hunters discover an abandoned observatory beneath a flooded city.","num_agents":4}'
    )
    with mock.patch("app.scenario.get_model_client", return_value=client):
        idea, num_agents = await generate_scenario_idea("gemini-test")

    assert idea == "Treasure hunters discover an abandoned observatory beneath a flooded city."
    assert num_agents == 4
    client.create.assert_awaited_once()


@pytest.mark.parametrize(("value", "expected"), [(1, 2), (6, 6), (99, 10), ("4", 4), ("bad", 3), (None, 3)])
def test_normalize_scenario_agent_count_clamps_to_supported_range(value, expected):
    assert _normalize_scenario_agent_count(value) == expected


def test_normalize_conversation_mode_defaults_invalid_values_to_discussion():
    assert _normalize_conversation_mode("DEBATE") == "debate"
    assert _normalize_conversation_mode("unknown") == "discussion"
    assert _normalize_conversation_mode(None) == "discussion"


@pytest.mark.parametrize(
    ("scenario", "suggested_mode", "expected"),
    [
        ("A stranded rescue team must survive a blizzard.", "discussion", "simulation"),
        ("Two experts debate opposing positions.", "discussion", "debate"),
        ("Brainstorm creative possibilities for a new product.", "discussion", "brainstorm"),
        ("Implement a working API and write code.", "discussion", "execution"),
        ("Tell a story and advance the plot.", "discussion", "storybook"),
        ("Discuss the meaning of friendship.", "discussion", "discussion"),
        ("A stranded team talks.", "debate", "debate"),
    ],
)
def test_infer_conversation_mode_uses_specialized_fallbacks(scenario, suggested_mode, expected):
    assert _infer_conversation_mode(scenario, suggested_mode) == expected


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
        SimpleNamespace(content='```json\n{"goal":"Land safely.","conversation_mode":"simulation"}\n```'),
    ]

    with mock.patch("app.scenario.get_model_client", return_value=client):
        agents, goal, mode = await generate_agents_from_scenario("Emergency landing", "gemini-test", 1)

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
    assert mode == "simulation"


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
        SimpleNamespace(content='{"goal":"Repair the ship.","conversation_mode":"unsupported"}'),
    ]

    with mock.patch("app.scenario.get_model_client", return_value=client):
        agents, goal, mode = await generate_agents_from_scenario("Damaged starship", "gemini-test", 2)

    assert agents[0]["tools_enabled"] is False
    assert agents[0]["relationships"][0]["target"] == "Engineer"
    assert agents[1]["relationships"][0]["relation"] == "Employee"
    assert goal == "Repair the ship."
    assert mode == "simulation"
