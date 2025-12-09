from autogen_agentchat.teams import SelectorGroupChat
import inspect

print("SelectorGroupChat signature:")
try:
    print(inspect.signature(SelectorGroupChat.__init__))
except Exception as e:
    print(f"Error getting signature: {e}")
