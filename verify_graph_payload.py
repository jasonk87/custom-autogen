import sys
import os
import asyncio
from unittest.mock import MagicMock

# Add current directory to sys.path
sys.path.append(os.getcwd())

from autogen_agentchat.messages import ModelClientStreamingChunkEvent
from app.core import _create_graph_payload

def main():
    print("Verifying _create_graph_payload fix...")
    
    # Create a mock event with .content
    # Note: ModelClientStreamingChunkEvent is a dataclass, but we can just mock the object if we want, 
    # or instantiate it if arguments allow. Let's try to instantiate it properly if possible, 
    # or use a mock that behaves like it.
    
    # Looking at the signature earlier: {'content': <class 'str'>, 'source': ..., 'id': ...}
    # Let's try to simulate it with a simple class or mock
    
    class MockEvent:
        def __init__(self):
            self.content = "test_content"
            self.source = "test_source"
            self.id = "test_id"
            
    # We also need to hack the isinstance check in _create_graph_payload
    # Since we can't easily modify the imported class in the module without patching,
    # we can try to actually instantiate ModelClientStreamingChunkEvent if we can.
    
    try:
        event = ModelClientStreamingChunkEvent(content="test_content", source="test_source", model="test_model")
    except TypeError:
        # Fallback if I got the signature wrong
        event = MagicMock(spec=ModelClientStreamingChunkEvent)
        event.content = "test_content"
        event.source = "test_source"
        event.delta = "SHOULD_NOT_BE_USED" 

    # We need to make sure isinstance(event, ModelClientStreamingChunkEvent) is true.
    # If we used the real class, it is.
    
    try:
        payload = _create_graph_payload(event, groupchat=MagicMock())
        print(f"Payload created: {payload}")
        
        if payload['delta'] == "test_content":
            print("SUCCESS: Payload used .content correctly.")
        else:
            print(f"FAILURE: Payload did not use .content correctly. Got: {payload.get('delta')}")
            
    except AttributeError as e:
        print(f"FAILED with AttributeError: {e}")
    except Exception as e:
        print(f"FAILED with Exception: {e}")

if __name__ == "__main__":
    main()
