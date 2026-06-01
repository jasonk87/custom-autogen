from types import SimpleNamespace
from unittest import mock

import pytest

from app.scenario import _strip_json_fence, generate_agents_from_scenario


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
