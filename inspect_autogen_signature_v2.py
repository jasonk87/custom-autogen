from autogen_agentchat.agents import UserProxyAgent, AssistantAgent
import inspect

def inspect_cls(cls):
    print(f"\n--- {cls.__name__} ---")
    try:
        sig = inspect.signature(cls.__init__)
        print(f"Signature: {sig}")
    except Exception as e:
        print(f"Error getting signature: {e}")
    
    print("Methods:")
    for name in dir(cls):
        if not name.startswith("__"):
            print(f" - {name}")

inspect_cls(UserProxyAgent)
inspect_cls(AssistantAgent)
