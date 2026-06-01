import json
from typing import Any, Dict, List, Optional

import httpx
from autogen_ext.models.ollama import OllamaChatCompletionClient
from autogen_ext.models.ollama._model_info import get_info as get_ollama_model_info
from autogen_ext.models.openai import OpenAIChatCompletionClient

from app.config import (
    DEFAULT_MODEL,
    GEMINI_API_KEY,
    HTTPX_TIMEOUT_CONNECT,
    ROOT_DIR,
    gemini_model_info,
    require_gemini_api_key,
)


SETTINGS_PATH = ROOT_DIR / "user_settings.json"
GEMINI_MODELS = [
    "gemini-2.5-pro",
    "gemini-2.5-flash",
    "gemini-2.5-flash-lite",
    "gemini-2.0-pro-exp-02-05",
    "gemini-2.0-flash",
    "gemini-2.0-flash-lite-preview-02-05",
    "gemini-1.5-pro",
    "gemini-1.5-flash",
    "gemini-1.5-flash-8b",
]


def normalize_model_selection(model: Optional[str]) -> str:
    raw = (model or "").strip()
    if not raw:
        raw = DEFAULT_MODEL
    if "::" in raw:
        return raw
    if raw.lower().startswith("gemini"):
        return f"gemini::{raw}"
    return f"ollama::local::{raw}"


def parse_model_selection(selection: Optional[str]) -> Dict[str, str]:
    normalized = normalize_model_selection(selection)
    parts = normalized.split("::", 2)
    if len(parts) == 2:
        provider, model = parts
        source = "default"
    else:
        provider, source, model = parts
    return {
        "provider": provider,
        "source": source,
        "model": model,
        "selection": normalized,
    }


def _settings_defaults() -> Dict[str, Any]:
    return {
        "selected_model": default_model_selection(),
        "ollama_local_url": "http://127.0.0.1:11434",
        "ollama_remote_url": "http://192.168.86.30:11434",
    }


def load_user_settings() -> Dict[str, Any]:
    defaults = _settings_defaults()
    try:
        with open(SETTINGS_PATH, "r", encoding="utf-8") as f:
            loaded = json.load(f)
        if not isinstance(loaded, dict):
            return defaults
        selected_model = normalize_model_selection(loaded.get("selected_model"))
        return {**defaults, **loaded, "selected_model": selected_model}
    except (FileNotFoundError, json.JSONDecodeError):
        return defaults


def save_user_settings(updates: Dict[str, Any]) -> Dict[str, Any]:
    current = load_user_settings()
    merged = {**current, **(updates or {})}
    merged["selected_model"] = normalize_model_selection(merged.get("selected_model"))
    with open(SETTINGS_PATH, "w", encoding="utf-8") as f:
        json.dump(merged, f, indent=2)
    return merged


def _build_model_entry(model: str, provider: str, source: str, label_prefix: str, icon: str) -> Dict[str, str]:
    selection = normalize_model_selection(f"{provider}::{source}::{model}")
    return {
        "value": selection,
        "model": model,
        "provider": provider,
        "source": source,
        "icon": icon,
        "label": f"{label_prefix} {model}",
    }


async def _fetch_ollama_catalog(base_url: str, source: str, label_prefix: str, icon: str) -> List[Dict[str, str]]:
    timeout = httpx.Timeout(connect=HTTPX_TIMEOUT_CONNECT, read=5.0, write=5.0, pool=5.0)
    async with httpx.AsyncClient(timeout=timeout) as client:
        response = await client.get(f"{base_url.rstrip('/')}/api/tags")
        response.raise_for_status()
        payload = response.json()

    models = payload.get("models") or []
    names = []
    for item in models:
        name = (item or {}).get("name")
        if isinstance(name, str) and name.strip():
            names.append(name.strip())

    return [
        _build_model_entry(model=name, provider="ollama", source=source, label_prefix=label_prefix, icon=icon)
        for name in sorted(set(names), key=str.lower)
    ]


async def get_available_models() -> List[Dict[str, str]]:
    catalog = [
        _build_model_entry(model=name, provider="gemini", source="cloud", label_prefix="$", icon="gemini")
        for name in GEMINI_MODELS
    ]

    settings = load_user_settings()
    local_url = settings.get("ollama_local_url", "http://127.0.0.1:11434").strip()
    remote_url = settings.get("ollama_remote_url", "http://192.168.86.30:11434").strip()

    sources = []
    if local_url:
        sources.append((local_url, "local", "Local"))
    if remote_url:
        sources.append((remote_url, "remote", "Remote"))

    for base_url, source, prefix in sources:
        try:
            catalog.extend(await _fetch_ollama_catalog(base_url, source, prefix, "cloud" if source == "remote" else "server"))
        except Exception:
            continue

    return catalog


def get_model_client(selection: Optional[str], temperature: float = 0.3, response_format: Any = None):
    parsed = parse_model_selection(selection)
    model = parsed["model"]
    provider = parsed["provider"]

    if provider == "gemini":
        kwargs: Dict[str, Any] = {
            "model": model,
            "api_key": require_gemini_api_key(),
            "base_url": "https://generativelanguage.googleapis.com/v1beta/openai/",
            "temperature": temperature,
            "model_info": gemini_model_info(model),
        }
        if model == "gemini-2.5-flash-lite":
            kwargs["reasoning_effort"] = "high"
        if response_format is not None:
            kwargs["response_format"] = response_format
        return OpenAIChatCompletionClient(**kwargs)

    settings = load_user_settings()
    if parsed["source"] == "remote":
        host = settings.get("ollama_remote_url", "http://192.168.86.30:11434").strip()
    else:
        host = settings.get("ollama_local_url", "http://127.0.0.1:11434").strip()

    ollama_model_info = get_model_info(selection)

    kwargs = {
        "model": model,
        "host": host,
        "model_info": ollama_model_info,
        "options": {"temperature": temperature},
    }
    if response_format is not None:
        kwargs["response_format"] = response_format
    return OllamaChatCompletionClient(**kwargs)


def default_model_selection() -> str:
    if GEMINI_API_KEY.strip():
        return normalize_model_selection(DEFAULT_MODEL)
    return normalize_model_selection("ollama::local::llama3.2")


def get_model_info(selection: Optional[str]) -> Dict[str, Any]:
    parsed = parse_model_selection(selection)
    provider = parsed["provider"]
    model = parsed["model"]

    if provider == "gemini":
        return gemini_model_info(model)

    try:
        return get_ollama_model_info(model)
    except KeyError:
        # Be conservative for unknown Ollama models. Structured JSON is commonly fine,
        # but tool/function calling support is model-specific and should not be assumed.
        return {
            "vision": False,
            "function_calling": False,
            "json_output": True,
            "family": "unknown",
            "structured_output": True,
        }


def model_supports_tools(selection: Optional[str]) -> bool:
    info = get_model_info(selection)
    return bool(info.get("function_calling"))
