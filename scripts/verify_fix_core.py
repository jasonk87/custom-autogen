import sys
import os
import asyncio
import queue
from unittest.mock import MagicMock, AsyncMock

# Add current directory to sys.path
sys.path.append(os.getcwd())

# Mock dependencies before importing app.core
import autogen_agentchat.teams
import autogen_ext.models.openai

# Create a dummy async generator for run_stream
async def dummy_run_stream(*args, **kwargs):
    yield MagicMock()

# Mock SelectorGroupChat
MockSelectorGroupChat = MagicMock()
MockSelectorGroupChat.return_value.run_stream = dummy_run_stream
autogen_agentchat.teams.SelectorGroupChat = MockSelectorGroupChat

# Mock OpenAIChatCompletionClient to avoid API calls
autogen_ext.models.openai.OpenAIChatCompletionClient = MagicMock()

from app.core import run_orchestrator

async def main():
    print("Verifying run_orchestrator fix...")
    out_q = queue.Queue()
    
    # We expect this to run without "TypeError: object async_generator can't be used in 'await' expression"
    # It might fail later due to other mocks, but if it passes the run_stream call, we are good.
    
    agents_cfg = [{"name": "TestAgent", "system": "You are a test agent."}]
    
    try:
        # We set max_turns=1 to keep it short
        await run_orchestrator(
            goal="Test Goal",
            model="test-model",
            agents_cfg=agents_cfg,
            manager_mode="auto",
            out_q=out_q,
            max_turns=1,
            human_proxy=False,
            temperature=0.0
        )
        print("run_orchestrator executed successfully (or at least didn't crash on await).")
    except TypeError as e:
        if "async_generator" in str(e):
            print(f"FAILED: Still getting TypeError: {e}")
        else:
            print(f"Passed usage check, but got other TypeError: {e}")
    except Exception as e:
        # If we get here, it means we got past the await call (likely) or failed before.
        # But if the await was still there, it would definitely raise TypeError immediately upon calling run_stream.
        print(f"Execution finished with exception (expected due to mocks): {e}")

if __name__ == "__main__":
    asyncio.run(main())
