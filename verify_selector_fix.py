from autogen_agentchat.teams import SelectorGroupChat
from autogen_agentchat.agents import UserProxyAgent, AssistantAgent
from autogen_ext.models.openai import OpenAIChatCompletionClient
import asyncio

async def test_instantiation():
    print("Attempting to instantiate SelectorGroupChat...")
    try:
        # Mock client (doesn't need real API key for instantiation test)
        gemini_client = OpenAIChatCompletionClient(
            model="gemini-1.5-flash",
            api_key="mock_key",
        )
        
        user_proxy = UserProxyAgent(name="user")
        agent = AssistantAgent(name="agent", model_client=gemini_client)
        
        # Mimic the call in app/core.py (now with model_client)
        groupchat = SelectorGroupChat(
            participants=[user_proxy, agent],
            max_turns=5,
            model_client=gemini_client,
        )
        print("Successfully instantiated SelectorGroupChat!")
        # print(f"Participants: {[p.name for p in groupchat.participants]}")
    except TypeError as te:
        print(f"TypeError during instantiation: {te}")
    except Exception as e:
        print(f"An error occurred: {type(e).__name__}: {e}")

if __name__ == "__main__":
    asyncio.run(test_instantiation())
