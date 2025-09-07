"""
Custom Agent Studio v10 — Manager-Directed + Thinking UI (Single File)
=====================================================================
- Manager selects who goes next (Directed / RoundRobin / Manual).
- Only the Human (Manager proxy) can terminate; non-human TERMINATE is ignored.
- Directed flow enforces Build → Review → UX after an artifact appears.
- Compact “Thinking UI”: client detects <think>…</think>, shows a collapsible dock, and auto-minimizes once normal output starts.
Run:
  pip install flask httpx werkzeug pyautogen
  python main.py
Open:
  http://127.0.0.1:8080
"""

from __future__ import annotations

import base64
import binascii
import importlib.util
import io
import json
import logging
import os
import queue
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
import traceback
from dataclasses import dataclass
from typing import Any, Dict, Generator, List, Optional, Tuple

import httpx
from flask import Flask, Response, jsonify, request, send_from_directory, render_template
from werkzeug.serving import make_server
from werkzeug.utils import secure_filename
from autogen import ConversableAgent, UserProxyAgent, GroupChat, GroupChatManager

# ===================== Directories & Config =====================

SESSIONS_DIR = "autogen_sessions"
WORKSPACE_DIR = "autogen_work_dir"
TOOLS_DIR = "tools"
os.makedirs(SESSIONS_DIR, exist_ok=True)
os.makedirs(WORKSPACE_DIR, exist_ok=True)
os.makedirs(TOOLS_DIR, exist_ok=True)

ALLOWED_UPLOAD_MIMES = {
    "text/plain",
    "application/json",
    "text/markdown",
    "text/x-python",
    "application/octet-stream",
}
MAX_UPLOAD_BYTES = int(os.environ.get("MAX_UPLOAD_BYTES", 10 * 1024 * 1024))  # 10 MB

DEFAULT_MODEL = os.environ.get("DEFAULT_MODEL", "llama3.1:8b")

COLLAB_GUIDANCE = (
    "Collaborate with the team. Do not try to solve the entire task alone. "
    "Never emit TERMINATE. Suggest explicit next-role handoffs. Keep outputs concise."
)

# ===================== Logging =====================

class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "t": int(time.time() * 1000),
            "level": record.levelname,
            "msg": record.getMessage(),
            "name": record.name,
        }
        if record.exc_info:
            payload["exc_info"] = self.formatException(record.exc_info)
        if hasattr(record, "extra") and isinstance(record.extra, dict):
            payload.update(record.extra)  # type: ignore
        return json.dumps(payload, ensure_ascii=False)

log = logging.getLogger("agent_studio")
log.setLevel(logging.INFO)
_handler = logging.StreamHandler(sys.stdout)
_handler.setFormatter(JsonFormatter())
log.addHandler(_handler)

# ===================== HTTPX Client & Ollama =====================

def _make_httpx_client() -> httpx.Client:
    return httpx.Client(timeout=httpx.Timeout(30.0, read=300.0))

_http = _make_httpx_client()

_ollama_lock = threading.Lock()
_ollama_host = os.environ.get("OLLAMA_BASE", "http://192.168.86.30:11434").rstrip("/")

def set_ollama_host(url: str) -> None:
    global _ollama_host
    with _ollama_lock:
        _ollama_host = url.rstrip("/")

def get_ollama_endpoints() -> Tuple[str, str]:
    with _ollama_lock:
        base = _ollama_host
    return f"{base}/api/tags", f"{base}/api/chat"

# TTL cache for models
_models_cache: Dict[str, Any] = {"ts": 0.0, "names": []}
MODELS_TTL_SEC = 20.0

# ===================== Flask App =====================

app = Flask(__name__)

# ===================== State =====================

@dataclass
class AppState:
    thread: Optional[threading.Thread] = None
    stop_event: threading.Event = threading.Event()
    user_input_q: "queue.Queue[Optional[str]]" = queue.Queue()
    manual_next_q: "queue.Queue[Optional[str]]" = queue.Queue()
    is_running: bool = False

    def start(self, target, args) -> None:
        self.stop_event.clear()
        self.user_input_q = queue.Queue()
        self.manual_next_q = queue.Queue()
        self.is_running = True
        self.thread = threading.Thread(target=target, args=args, daemon=True)
        self.thread.start()

    def stop(self) -> None:
        self.is_running = False
        self.stop_event.set()
        for q in (self.user_input_q, self.manual_next_q):
            try:
                q.put_nowait(None)
            except Exception:
                pass

state = AppState()

# ===================== Utilities =====================

def _safe_path(path: str) -> str:
    base = os.path.abspath(WORKSPACE_DIR)
    p = os.path.abspath(os.path.join(base, path))
    if not p.startswith(base + os.sep) and p != base:
        raise ValueError("outside workspace")
    return p

def tree_listing(root: str) -> Dict[str, Any]:
    root_abs = _safe_path(root)
    def walk(p: str) -> Dict[str, Any]:
        node = {"name": os.path.basename(p) or os.path.basename(root_abs), "type": "dir", "children": []}
        try:
            for nm in sorted(os.listdir(p)):
                fp = os.path.join(p, nm)
                if os.path.isdir(fp):
                    node["children"].append(walk(fp))
                else:
                    node["children"].append({"name": nm, "type": "file", "path": os.path.relpath(fp, WORKSPACE_DIR)})
        except Exception as e:
            node["error"] = str(e)
        return node
    return walk(root_abs)

# ===================== Orchestrator =====================

def run_orchestrator(goal: str, model: str, agents_cfg: List[Dict[str, Any]], manager_mode: str, out_q: "queue.Queue[str]", max_turns: int = 60):
    try:
        llm_config = {
            "config_list": [{"model": model, "base_url": _ollama_host, "api_key": "ollama"}],
            "cache_seed": None,
        }

        # Custom get_human_input function
        def custom_get_human_input(prompt: str) -> str:
            out_q.put(json.dumps({"type": "status", "state": "waiting_for_input"}))
            try:
                message = state.user_input_q.get(timeout=3600)
                if message is None or message == "exit":
                    return "exit"
                return message
            except queue.Empty:
                return "exit"

        # Create agents
        autogen_agents: List[ConversableAgent] = []

        # User Proxy Agent
        user_proxy = UserProxyAgent(
            name="Human_Admin",
            system_message="A human admin. Interact with the team to discuss the plan and delegate tasks. You can also execute code.",
            human_input_mode="ALWAYS",
            get_human_input=custom_get_human_input,
            max_consecutive_auto_reply=10,
            is_termination_msg=lambda x: x.get("content", "").rstrip().endswith("TERMINATE"),
            code_execution_config={"work_dir": WORKSPACE_DIR, "use_docker": False},
        )
        autogen_agents.append(user_proxy)

        for agent_cfg in agents_cfg:
            agent = ConversableAgent(
                name=agent_cfg["name"],
                system_message=agent_cfg.get("system") or f"You are a helpful assistant named {agent_cfg['name']}",
                llm_config={**llm_config, "temperature": float(agent_cfg.get("temperature", 0.7))},
                human_input_mode="NEVER",
            )
            autogen_agents.append(agent)

        # Streaming callback
        def on_message_callback(recipient, messages, sender, config):
            last_message = messages[-1]
            sender_name = sender.name

            msg_id = f"m_{time.time()}_{sender_name}"
            out_q.put(json.dumps({"type": "stream_start", "id": msg_id, "sender": sender_name}))
            out_q.put(json.dumps({"type": "token", "id": msg_id, "delta": last_message.get("content", "")}))
            out_q.put(json.dumps({"type": "stream_end", "id": msg_id}))

            return False, None

        for agent in autogen_agents:
            agent.register_reply(
                [ConversableAgent, UserProxyAgent],
                reply_func=on_message_callback,
                trigger="on_receive",
            )

        groupchat = GroupChat(agents=autogen_agents, messages=[], max_round=max_turns)
        manager = GroupChatManager(groupchat=groupchat, llm_config=llm_config)

        out_q.put(json.dumps({"type": "chat", "sender": "System", "message": f"Goal set: {goal}"}))

        user_proxy.initiate_chat(manager, message=goal)

        out_q.put(json.dumps({"type": "status", "state": "idle"}))

    except Exception as e:
        log.error("orchestrator_error", extra={"extra": {"err": str(e), "trace": traceback.format_exc()}})
        out_q.put(json.dumps({"type": "chat", "sender": "Error", "message": f"{type(e).__name__}: {e}"}))
    finally:
        out_q.put("[DONE]")

# ===================== SSE Endpoint =====================

@app.get("/stream")
def stream():
    model = request.args.get("model", DEFAULT_MODEL)
    goal_b64 = request.args.get("goal", "")
    agents_b64 = request.args.get("agents", "")
    manager_mode = request.args.get("manager_mode", "Directed")
    max_turns = int(request.args.get("turns", "60"))

    try:
        goal = base64.b64decode(goal_b64.encode()).decode(errors="ignore") if goal_b64 else ""
    except Exception:
        goal = goal_b64
    try:
        agents_cfg = json.loads(base64.b64decode(agents_b64.encode()).decode(errors="ignore") or "[]")
    except Exception:
        agents_cfg = []

    out_q: "queue.Queue[str]" = queue.Queue()
    state.start(run_orchestrator, (goal, model, agents_cfg, manager_mode, out_q, max_turns))

    def gen():
        last_ping = time.time()
        while True:
            try:
                item = out_q.get(timeout=0.5)
                yield f"data: {item}\n\n"
                if item == "[DONE]":
                    break
            except queue.Empty:
                if time.time() - last_ping > 10:
                    yield ": ping\n\n"
                    last_ping = time.time()

    headers = {
        "Cache-Control": "no-cache, no-transform",
        "Content-Type": "text/event-stream",
        "Connection": "keep-alive",
        "X-Accel-Buffering": "no",
    }
    return Response(gen(), headers=headers)

# ===================== Control APIs =====================

@app.post("/stop")
def stop():
    state.stop()
    return jsonify({"ok": True})

@app.post("/user_input")
def user_input():
    msg = (request.json or {}).get("message")
    try:
        state.user_input_q.put_nowait(msg)
    except Exception:
        pass
    return jsonify({"ok": True})

# ===================== Settings / Models =====================

@app.post("/api/settings/ollama")
def set_ollama():
    data = request.json or {}
    url = (data.get("base") or "").strip()
    if not url:
        return jsonify({"error": "missing_base"}), 400
    set_ollama_host(url)
    return jsonify({"ok": True, "base": url})

@app.get("/api/models")
def api_models():
    global _models_cache
    now = time.time()
    if now - _models_cache["ts"] < MODELS_TTL_SEC and _models_cache["names"]:
        return jsonify(_models_cache["names"])
    tags_url, _ = get_ollama_endpoints()
    try:
        r = _http.get(tags_url)
        r.raise_for_status()
        payload = r.json() or {}
        models = payload.get("models", [])
        names = [m.get("name") for m in models if isinstance(m, dict) and m.get("name")]
        _models_cache = {"ts": now, "names": names}
        return jsonify(names)
    except httpx.HTTPError as e:
        return jsonify({"error": "connect_failed", "message": str(e)}), 503
    except ValueError as e:
        return jsonify({"error": "bad_json", "message": str(e)}), 502

@app.post("/api/generate_description")
def generate_description():
    data = request.json or {}
    title = data.get("title")
    model = data.get("model", DEFAULT_MODEL)

    if not title:
        return jsonify({"error": "missing_title"}), 400

    _, chat_url = get_ollama_endpoints()
    prompt = f"You are an expert at defining roles for AI agents. Create a concise system message for an AI agent named '{title}'. The message should describe its role and responsibilities in a collaborative team. The output should be just the system message itself, without any extra text, quotes, or preamble."

    messages = [{"role": "user", "content": prompt}]

    try:
        r = _http.post(
            chat_url,
            json={
                "model": model,
                "messages": messages,
                "stream": False,
            },
            timeout=60.0,
        )
        r.raise_for_status()
        payload = r.json()
        content = payload.get("message", {}).get("content", "")
        return jsonify({"description": content.strip()})
    except httpx.HTTPError as e:
        log.error("ollama_generate_error", extra={"extra": {"err": str(e)}})
        return jsonify({"error": "ollama_failed", "message": str(e)}), 503
    except Exception as e:
        log.error("generate_desc_error", extra={"extra": {"err": str(e)}})
        return jsonify({"error": "unknown", "message": str(e)}), 500

@app.post("/api/generate_team")
def generate_team():
    data = request.json or {}
    goal = data.get("goal")
    model = data.get("model", DEFAULT_MODEL)

    if not goal:
        return jsonify({"error": "missing_goal"}), 400

    _, chat_url = get_ollama_endpoints()
    prompt = f"""
Given the following goal, please generate a team of up to 4 AI agents to accomplish it.
Return the team as a JSON array of objects, where each object has a "name" and a "system_message".
The "name" should be a short, descriptive title for the agent (e.g., "Code_Reviewer").
The "system_message" should be a concise description of the agent's role and responsibilities.

Example:
Goal: "Build a flask application."
Output:
[
  {{"name": "Programmer", "system_message": "You are a Python programmer who writes Flask applications."}},
  {{"name": "Code_Reviewer", "system_message": "You are a code reviewer who checks for bugs and style issues."}}
]

Goal: "{goal}"
Output:
"""

    messages = [{"role": "user", "content": prompt}]

    try:
        r = _http.post(
            chat_url,
            json={
                "model": model,
                "messages": messages,
                "stream": False,
                "options": {"temperature": 0.2},
                "format": "json"
            },
            timeout=120.0,
        )
        r.raise_for_status()
        payload = r.json()
        content = payload.get("message", {}).get("content", "")
        team_cfg = json.loads(content)
        return jsonify(team_cfg)
    except Exception as e:
        log.error("team_generate_error", extra={"extra": {"err": str(e)}})
        return jsonify({"error": "team_generation_failed", "message": str(e)}), 500

# ===================== Workspace & Sessions =====================

@app.get("/api/workspace/files")
def api_files_flat():
    out = []
    for item in os.listdir(WORKSPACE_DIR):
        p = os.path.join(WORKSPACE_DIR, item)
        if os.path.isfile(p):
            out.append({"name": item, "path": f"/workspace/{item}"})
    return jsonify(out)

@app.get("/api/workspace/tree")
def api_files_tree():
    return jsonify(tree_listing("."))

@app.post("/api/workspace/upload")
def api_upload():
    f = request.files.get("file")
    if not f:
        return jsonify({"error": "missing file"}), 400
    f.stream.seek(0, io.SEEK_END)
    size = f.stream.tell()
    f.stream.seek(0)
    if size > MAX_UPLOAD_BYTES:
        return jsonify({"error": "too_large", "max": MAX_UPLOAD_BYTES}), 413
    mime = f.mimetype or "application/octet-stream"
    if mime not in ALLOWED_UPLOAD_MIMES:
        if not secure_filename(f.filename).endswith((".txt", ".md", ".json", ".py")):
            return jsonify({"error": "mime_blocked", "mime": mime}), 415
    fn = secure_filename(f.filename)
    fp = os.path.join(WORKSPACE_DIR, fn)
    os.makedirs(os.path.dirname(fp), exist_ok=True)
    f.save(fp)
    return jsonify({"ok": True, "file": fn, "bytes": size})

@app.get("/workspace/<path:fn>")
def ws_file(fn: str):
    return send_from_directory(WORKSPACE_DIR, fn)

@app.get("/api/sessions")
def sessions_list():
    return jsonify(sorted([f for f in os.listdir(SESSIONS_DIR) if f.endswith(".json")]))

@app.get("/api/sessions/<path:name>")
def sessions_get(name: str):
    fp = os.path.join(SESSIONS_DIR, name)
    if not os.path.exists(fp):
        return jsonify({"error": "not_found"}), 404
    with open(fp, "r", encoding="utf-8") as f:
        return jsonify(json.load(f))

@app.post("/api/sessions")
def sessions_save():
    data = request.json or {}
    name = data.get("name")
    if not name:
        return jsonify({"error": "missing_name"}), 400
    fp = os.path.join(SESSIONS_DIR, f"{name}.json")
    with open(fp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
    return jsonify({"ok": True, "file": fp}), 201

@app.delete("/api/sessions/<path:name>")
def sessions_delete(name: str):
    fp = os.path.join(SESSIONS_DIR, name)
    if not os.path.exists(fp):
        return jsonify({"error": "not_found"}), 404
    try:
        os.remove(fp)
        return jsonify({"ok": True}), 200
    except Exception as e:
        log.error("session_delete_error", extra={"extra": {"err": str(e)}})
        return jsonify({"error": "delete_failed", "message": str(e)}), 500

@app.post("/api/transcript/md")
def export_md():
    payload = request.json or {}
    transcript = payload.get("transcript") or []
    lines = ["# Transcript", ""]
    for item in transcript:
        ts = item.get("t")
        who = item.get("from")
        text = item.get("text", "")
        lines.append(f"**{who}** — {ts}")
        lines.append("")
        lines.append("```")
        lines.append(text)
        lines.append("```")
        lines.append("")
    md = "\n".join(lines)
    fn = f"transcript_{int(time.time())}.md"
    fp = os.path.join(WORKSPACE_DIR, fn)
    with open(fp, "w", encoding="utf-8") as f:
        f.write(md)
    return jsonify({"ok": True, "file": f"/workspace/{fn}"})

@app.post("/api/transcript/html")
def export_html():
    payload = request.json or {}
    transcript = payload.get("transcript") or []
    parts = [
        "<!doctype html><meta charset='utf-8'><title>Transcript</title>",
        "<style>body{font-family:system-ui,Segoe UI,Inter,Arial;padding:20px;background:#0b1220;color:#e5e7eb} .b{background:#111827;border:1px solid #334155;border-radius:8px;padding:10px;margin:10px 0;white-space:pre-wrap}</style>",
        "<h1>Transcript</h1>",
    ]
    for item in transcript:
        who = item.get("from")
        text = (item.get("text") or "").replace("&","&amp;").replace("<","&lt;").replace(">","&gt;")
        parts.append(f"<div><strong>{who}</strong></div><div class='b'>{text}</div>")
    html = "\n".join(parts)
    fn = f"transcript_{int(time.time())}.html"
    fp = os.path.join(WORKSPACE_DIR, fn)
    with open(fp, "w", encoding="utf-8") as f:
        f.write(html)
    return jsonify({"ok": True, "file": f"/workspace/{fn}"})

# ===================== Routes to serve HTML =====================

@app.get("/")
def index():
    return render_template("index.html")

# ===================== Graceful shutdown =====================

_server: Optional[any] = None

def _shutdown(*_args):
    try:
        state.stop()
        if _server is not None:
            pass
    finally:
        for h in list(log.handlers):
            try:
                h.flush()
            except Exception:
                pass

# ===================== Main =====================

if __name__ == "__main__":
    print("Starting Custom Agent Studio — v10 — http://127.0.0.1:8080")
    signal.signal(signal.SIGINT, _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)
    _server = make_server("0.0.0.0", 8080, app)
    _server.serve_forever()
