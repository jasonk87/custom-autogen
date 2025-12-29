import pytest
from autogen_agentchat.messages import TextMessage

@pytest.fixture
def example_message():
    return TextMessage(content="Hello", source="User")
