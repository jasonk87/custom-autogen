import json
import os
from pathlib import Path

from dotenv import load_dotenv
from autogen_core.models import ModelFamily

ROOT_DIR = Path(__file__).resolve().parents[1]
CONFIG_PATH = ROOT_DIR / "config.json"

load_dotenv()

try:
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        CONFIG = json.load(f)
except (FileNotFoundError, json.JSONDecodeError) as e:
    raise RuntimeError(f"Could not load config at {CONFIG_PATH}: {e}") from e

SESSIONS_DIR = str((ROOT_DIR / CONFIG["sessions_dir"]).resolve())
WORKSPACE_DIR = str((ROOT_DIR / CONFIG["workspace_dir"]).resolve())
TOOLS_DIR = str((ROOT_DIR / CONFIG["tools_dir"]).resolve())
ALLOWED_UPLOAD_MIMES = set(CONFIG["allowed_upload_mimes"])
MAX_UPLOAD_BYTES = int(os.environ.get("MAX_UPLOAD_BYTES", CONFIG["max_upload_bytes"]))
DEFAULT_MODEL = os.environ.get("DEFAULT_MODEL", CONFIG["default_model"])
COLLAB_GUIDANCE = CONFIG["collab_guidance"]
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
MODELS_TTL_SEC = CONFIG["models_ttl_sec"]
HTTPX_TIMEOUT_CONNECT = CONFIG["httpx_timeout"]["connect"]
HTTPX_TIMEOUT_READ = CONFIG["httpx_timeout"]["read"]


def require_gemini_api_key() -> str:
    key = (GEMINI_API_KEY or "").strip()
    if key:
        return key
    raise RuntimeError("GEMINI_API_KEY is required and must be set in the environment.")


def gemini_model_info(model_name: str) -> dict:
    model_key = (model_name or "").lower()
    family = ModelFamily.UNKNOWN
    if "2.5-pro" in model_key:
        family = ModelFamily.GEMINI_2_5_PRO
    elif "2.5-flash" in model_key:
        family = ModelFamily.GEMINI_2_5_FLASH
    elif "2.0-flash" in model_key:
        family = ModelFamily.GEMINI_2_0_FLASH
    elif "1.5-pro" in model_key:
        family = ModelFamily.GEMINI_1_5_PRO
    elif "1.5-flash" in model_key:
        family = ModelFamily.GEMINI_1_5_FLASH

    return {
        "vision": True,
        "function_calling": True,
        "json_output": True,
        "structured_output": True,
        "multiple_system_messages": True,
        "family": family,
    }


os.makedirs(SESSIONS_DIR, exist_ok=True)
os.makedirs(WORKSPACE_DIR, exist_ok=True)
os.makedirs(TOOLS_DIR, exist_ok=True)
