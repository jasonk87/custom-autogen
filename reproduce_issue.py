import sys
import os
import asyncio
import json

# Add current directory to sys.path
sys.path.append(os.getcwd())

from app.scenario import generate_agents_from_scenario

# The user's prompt
SCENARIO = "two presidents trying to discuss peace. one is the president of the united states and the other is from russia."

from app.config import DEFAULT_MODEL, GEMINI_API_KEY
print(f"DEBUG: DEFAULT_MODEL={DEFAULT_MODEL}")
print(f"DEBUG: API KEY length={len(GEMINI_API_KEY)}")

async def main():
    print(f"Testing scenario generation with prompt: '{SCENARIO}'")
    try:
        # Try gemini-1.5-flash-latest
        agents, goal = await generate_agents_from_scenario(SCENARIO, "gemini-1.5-flash-latest", 3) 
        print("\n--- AGENTS ---")
        print(json.dumps(agents, indent=2))
        print("\n--- GOAL ---")
        print(goal)
    except Exception as e:
        print(f"\nERROR: {e}")
        import traceback
        traceback.print_exc()

if __name__ == "__main__":
    asyncio.run(main())
