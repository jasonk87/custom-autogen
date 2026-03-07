
import sys
import os
import asyncio
import json

# Add current directory to sys.path
sys.path.append(os.getcwd())

from app.scenario import generate_agents_from_scenario
from app.config import DEFAULT_MODEL

# The user's prompt
SCENARIO = "two presidents trying to discuss peace"

async def main():
    print(f"Testing adapter with model: '{DEFAULT_MODEL}'")
    try:
        # Use None to trigger default model from config (which should be gemini-2.0-flash now)
        agents, goal = await generate_agents_from_scenario(SCENARIO, None, 3) 
        print("\n--- AGENTS ---")
        print(json.dumps(agents, indent=2))
        print("\n--- GOAL ---")
        print(goal)
    except Exception as e:
        print(f"\nERROR TYPE: {type(e)}")
        print(f"ERROR: {e}")
        # import traceback
        # traceback.print_exc()

if __name__ == "__main__":
    asyncio.run(main())
