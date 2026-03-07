
try:
    from autogen_core.models import ModelInfo, ModelFamily
    import inspect
    print(f"ModelFamily members: {[e.name for e in ModelFamily]}")
    print(f"Type: {type(ModelInfo)}")
    if hasattr(ModelInfo, "__annotations__"):
        print("Annotations:")
        for k, v in ModelInfo.__annotations__.items():
            print(f" - {k}: {v}")
    elif hasattr(ModelInfo, "__dataclass_fields__"):
        print("Dataclass fields:")
        print(ModelInfo.__dataclass_fields__.keys())
except ImportError:
    print("Could not import ModelInfo")
except Exception as e:
    print(f"Error: {e}")
