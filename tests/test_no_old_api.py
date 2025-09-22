import importlib.util

def test_pyautogen_not_installed():
    """
    Guardrail to prevent mixing old AutoGen 0.2 API with AgentChat/Teams.
    """
    assert importlib.util.find_spec("pyautogen") is None
