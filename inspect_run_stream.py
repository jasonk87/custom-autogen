import inspect
import asyncio
from autogen_agentchat.teams import SelectorGroupChat

print(f"Type of run_stream: {SelectorGroupChat.run_stream}")
print(f"Is async generator function: {inspect.isasyncgenfunction(SelectorGroupChat.run_stream)}")

async def test():
    try:
        # We can't easily instantiate SelectorGroupChat without mocks, 
        # but checking isasyncgenfunction should be enough.
        pass
    except Exception as e:
        print(f"Error: {e}")

asyncio.run(test())
