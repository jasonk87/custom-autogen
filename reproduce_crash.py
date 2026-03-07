import asyncio
import json
import logging
from autogen_ext.models.openai import OpenAIChatCompletionClient
from autogen_agentchat.agents import AssistantAgent
from autogen_agentchat.messages import TextMessage
from autogen_core import CancellationToken

# Configure logging
logging.basicConfig(level=logging.INFO)

# Load config to get API key
try:
    with open("config.json", "r", encoding="utf-8") as f:
        CONFIG = json.load(f)
    GEMINI_API_KEY = CONFIG.get("gemini_api_key", "")
except Exception as e:
    print(f"Error loading config: {e}")
    exit(1)

async def reproduce():
    print("Initializing Client...")
    gemini_client = OpenAIChatCompletionClient(
        model="gemini-2.0-flash",
        api_key=GEMINI_API_KEY,
        base_url="https://generativelanguage.googleapis.com/v1beta/openai/",
        temperature=0.3,
    )

    agent = AssistantAgent(
        name="TestAgent",
        model_client=gemini_client,
        system_message="You are a helpful assistant.",
    )
    
    print("Sending request via Agent...")
    try:
        # We need to simulate the agent loop or just call on_messages
        # on_messages takes a sequence of messages
        response = await agent.on_messages(
            [TextMessage(content="Hello, is this working?", source="user")],
            cancellation_token=CancellationToken()
        )
        print("Response received:", response)
        
    except Exception as e:
        print(f"\nCRASH REPRODUCED:\n{e}")
        import traceback
        traceback.print_exc()

if __name__ == "__main__":
    asyncio.run(reproduce())
