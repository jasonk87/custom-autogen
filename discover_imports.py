try:
    from autogen_core import UserMessage
    print("Found in autogen_core")
except ImportError:
    pass

try:
    from autogen_core.models import UserMessage
    print("Found in autogen_core.models")
except ImportError:
    pass

try:
    from autogen_core.components.models import UserMessage
    print("Found in autogen_core.components.models")
except ImportError:
    pass

try:
    from autogen_ext.models.openai import OpenAIChatCompletionClient
    print("Imported Client")
except ImportError:
    print("Failed to import Client")
