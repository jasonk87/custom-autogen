from pathlib import Path
import re


def test_old_api_module_not_used_by_project():
    """
    Guardrail to prevent mixing old AutoGen 0.2 API imports into project code.
    """
    project_root = Path(__file__).resolve().parents[1]
    python_files = [p for p in project_root.rglob("*.py") if ".git" not in p.parts]
    banned_patterns = [
        re.compile(r"^\s*import\s+autogen(\s|$|\.)", re.M),
        re.compile(r"^\s*from\s+autogen(\s|$|\.)", re.M),
        re.compile(r"^\s*import\s+pyautogen(\s|$|\.)", re.M),
        re.compile(r"^\s*from\s+pyautogen(\s|$|\.)", re.M),
    ]

    offenders = []
    for path in python_files:
        text = path.read_text(encoding="utf-8")
        if any(pattern.search(text) for pattern in banned_patterns):
            offenders.append(str(path))

    assert offenders == []
