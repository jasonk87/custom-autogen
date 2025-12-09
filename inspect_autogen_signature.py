from autogen_agentchat.agents import UserProxyAgent
import inspect

print("UserProxyAgent signature:")
try:
    print(inspect.signature(UserProxyAgent.__init__))
except Exception as e:
    print(f"Error getting signature: {e}")

print("\nUserProxyAgent help:")
help(UserProxyAgent)
