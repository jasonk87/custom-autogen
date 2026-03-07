from app.core import MyUserProxyAgent
from autogen_agentchat.agents import UserProxyAgent

print("Attempting to instantiate MyUserProxyAgent...")
try:
    # Mimic the call in app/core.py (now without legacy args)
    user_proxy = MyUserProxyAgent(
        name="Human_Admin"
    )
    print("Successfully instantiated MyUserProxyAgent!")
    print(f"Name: {user_proxy.name}")
except TypeError as te:
    print(f"TypeError during instantiation: {te}")
except Exception as e:
    print(f"An error occurred: {type(e).__name__}: {e}")
