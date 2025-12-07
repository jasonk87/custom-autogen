
import sys
import os
import asyncio
import json

# Add current directory to sys.path
sys.path.append(os.getcwd())

from app.scenario import generate_agents_from_scenario
from app.config import DEFAULT_MODEL

SCENARIO = "two presidents trying to discuss peace"

async def main():
    print(f"Testing generation with model: '{DEFAULT_MODEL}'")
    try:
        # Use None to trigger default model from config
        agents, goal = await generate_agents_from_scenario(SCENARIO, None, 3) 
        print("\n--- AGENTS ---")
        print(json.dumps(agents, indent=2))
        
        # Validation check
        for agent in agents:
            if "system" not in agent:
                print(f"FAILED: Agent {agent.get('name')} missing 'system' field")
                sys.exit(1)
        
        print("\n--- GOAL ---")
        print(goal)
        print("\nSUCCESS: All agents have system field.")
    except Exception as e:
        print(f"\nERROR: {e}")
        import traceback
        traceback.print_exc()

if __name__ == "__main__":
    asyncio.run(main())
