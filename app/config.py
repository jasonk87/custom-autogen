import json
import os
import sys

try:
    with open("config.json", "r", encoding="utf-8") as f:
        CONFIG = json.load(f)
except (FileNotFoundError, json.JSONDecodeError) as e:
    print(f"FATAL: Could not load config.json: {e}", file=sys.stderr)
    sys.exit(1)

# Define constants from the config file
SESSIONS_DIR = CONFIG["sessions_dir"]
WORKSPACE_DIR = CONFIG["workspace_dir"]
TOOLS_DIR = CONFIG["tools_dir"]
ALLOWED_UPLOAD_MIMES = set(CONFIG["allowed_upload_mimes"])
MAX_UPLOAD_BYTES = int(os.environ.get("MAX_UPLOAD_BYTES", CONFIG["max_upload_bytes"]))
DEFAULT_MODEL = os.environ.get("DEFAULT_MODEL", CONFIG["default_model"])
COLLAB_GUIDANCE = CONFIG["collab_guidance"]
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", CONFIG.get("gemini_api_key", ""))
MODELS_TTL_SEC = CONFIG["models_ttl_sec"]
HTTPX_TIMEOUT_CONNECT = CONFIG["httpx_timeout"]["connect"]
HTTPX_TIMEOUT_READ = CONFIG["httpx_timeout"]["read"]

# Ensure directories exist
os.makedirs(SESSIONS_DIR, exist_ok=True)
os.makedirs(WORKSPACE_DIR, exist_ok=True)
os.makedirs(TOOLS_DIR, exist_ok=True)
