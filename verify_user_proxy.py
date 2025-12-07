import sys
import os
import json
import queue
from unittest.mock import MagicMock

# Add current directory to sys.path
sys.path.append(os.getcwd())

from app.core import MyUserProxyAgent, state

def main():
    print("Verifying MyUserProxyAgent fix...")
    
    # Mock out_q
    mock_out_q = queue.Queue()
    
    # Instantiate MyUserProxyAgent
    try:
        agent = MyUserProxyAgent(name="TestUser", out_q=mock_out_q)
        print("Successfully instantiated MyUserProxyAgent with out_q.")
    except TypeError as e:
        print(f"FAILED to instantiate MyUserProxyAgent: {e}")
        return

    # Check if out_q is stored
    if hasattr(agent, 'out_q') and agent.out_q is mock_out_q:
        print("out_q is correctly stored in the agent instance.")
    else:
        print("FAILED: out_q is not stored correctly.")
        return

    # Mock state.user_input_q to return immediately so we don't block forever
    state.user_input_q.put("user_response")
    
    # Test get_human_input
    try:
        response = agent.get_human_input("test prompt")
        print(f"get_human_input returned: {response}")
        
        # Check if status message was put in out_q
        try:
            msg = mock_out_q.get_nowait()
            msg_data = json.loads(msg)
            if msg_data.get("type") == "status" and msg_data.get("state") == "waiting_for_input":
                print("SUCCESS: Status message found in out_q.")
            else:
                print(f"FAILED: Incorrect message in out_q: {msg_data}")
        except queue.Empty:
            print("FAILED: No message in out_q.")
            
    except NameError as e:
        print(f"FAILED with NameError (likely out_q scope issue): {e}")
    except Exception as e:
        print(f"FAILED with Exception: {e}")

if __name__ == "__main__":
    main()
