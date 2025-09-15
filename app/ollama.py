import os
import threading
from typing import Tuple

import httpx

from .config import HTTPX_TIMEOUT_CONNECT, HTTPX_TIMEOUT_READ, OLLAMA_BASE_URL

_http = httpx.Client(
    timeout=httpx.Timeout(HTTPX_TIMEOUT_CONNECT, read=HTTPX_TIMEOUT_READ)
)

_ollama_lock = threading.Lock()
_ollama_host = OLLAMA_BASE_URL.rstrip("/")


def set_ollama_host(url: str) -> None:
    global _ollama_host
    with _ollama_lock:
        _ollama_host = url.rstrip("/")


def get_ollama_endpoints() -> Tuple[str, str]:
    with _ollama_lock:
        base = _ollama_host
    return f"{base}/api/tags", f"{base}/api/chat"
