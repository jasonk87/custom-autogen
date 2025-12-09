from autogen_agentchat.messages import ModelClientStreamingChunkEvent
import inspect

print("ModelClientStreamingChunkEvent attributes:")
try:
    # It's a dataclass, so we can check __annotations__ or plain dir()
    print(ModelClientStreamingChunkEvent.__annotations__)
except Exception:
    pass

print("\ndir(ModelClientStreamingChunkEvent):")
print([d for d in dir(ModelClientStreamingChunkEvent) if not d.startswith('_')])
