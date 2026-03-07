
import inspect
from autogen_agentchat.agents import AssistantAgent
from autogen_agentchat.teams import SelectorGroupChat
from autogen_ext.models.openai import OpenAIChatCompletionClient

with open("signatures.txt", "w") as f:
    f.write("AssistantAgent:\n")
    f.write(str(inspect.signature(AssistantAgent.__init__)))
    f.write("\n\n")
    
    f.write("SelectorGroupChat:\n")
    f.write(str(inspect.signature(SelectorGroupChat.__init__)))
    f.write("\n\n")

    f.write("OpenAIChatCompletionClient:\n")
    f.write(str(inspect.signature(OpenAIChatCompletionClient.__init__)))
    f.write("\n\n")
