"""
Custom Agent Studio v10 — Manager-Directed + Thinking UI (Single File)
=====================================================================
- Manager selects who goes next (Directed / RoundRobin / Manual).
- Only the Human (Manager proxy) can terminate; non-human TERMINATE is ignored.
- Directed flow enforces Build → Review → UX after an artifact appears.
- Compact “Thinking UI”: client detects <think>…</think>, shows a collapsible dock, and auto-minimizes once normal output starts.
Run:
  pip install flask httpx werkzeug
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
from dataclasses import dataclass
from typing import Any, Dict, Generator, List, Optional, Tuple

import httpx
from flask import Flask, Response, jsonify, request, send_from_directory, render_template
from werkzeug.serving import make_server
from werkzeug.utils import secure_filename

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

DEFAULT_MODEL = os.environ.get("DEFAULT_MODEL", "qwen3:8b")

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
    # httpx doesn't expose urllib3.Retry; use sane timeouts & manual backoff in stream loop.
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

TOOL_RE = re.compile(r"```tool:(?P<name>[a-zA-Z0-9_\-]+)\s*\n(?P<body>[\s\S]*?)```", re.M)
CODE_FENCE_RE = re.compile(r"```(python|bash|json|[a-zA-Z0-9_\-]*)[\s\S]*?```", re.M)

ROLE_HINTS = {
    "programmer": {"keywords": ["programmer", "developer", "coder", "engineer"]},
    "reviewer": {"keywords": ["critic", "review", "reviewer", "qa", "tester"]},
    "ux": {"keywords": ["ux", "ui", "designer", "design"]},
}

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

# ===================== Built-in Tools & Plugins =====================

def tool_listdir(path: str = ".") -> Dict[str, Any]:
    p = _safe_path(path)
    entries = []
    for nm in sorted(os.listdir(p)):
        fp = os.path.join(p, nm)
        entries.append({"name": nm, "is_dir": os.path.isdir(fp)})
    return {"cwd": os.path.relpath(p, WORKSPACE_DIR), "entries": entries}

def tool_read_file(path: str) -> str:
    with open(_safe_path(path), "r", encoding="utf-8") as f:
        return f.read()

def tool_write_file(path: str, content: str, overwrite: bool = True) -> str:
    fp = _safe_path(path)
    os.makedirs(os.path.dirname(fp), exist_ok=True)
    if not overwrite and os.path.exists(fp):
        raise FileExistsError("exists")
    with open(fp, "w", encoding="utf-8") as f:
        f.write(content)
    return f"wrote {len(content)} bytes to {path}"

def tool_delete(path: str) -> str:
    fp = _safe_path(path)
    if os.path.isdir(fp):
        shutil.rmtree(fp)
    else:
        os.remove(fp)
    return f"deleted {path}"

TOOLS: Dict[str, Any] = {
    "listdir": tool_listdir,
    "read_file": tool_read_file,
    "write_file": tool_write_file,
    "delete": tool_delete,
}

def load_tool_plugins() -> None:
    for fname in os.listdir(TOOLS_DIR):
        if not fname.endswith(".py"):
            continue
        mod_path = os.path.join(TOOLS_DIR, fname)
        spec = importlib.util.spec_from_file_location(f"tools_{fname[:-3]}", mod_path)
        if not spec or not spec.loader:
            continue
        mod = importlib.util.module_from_spec(spec)
        try:
            spec.loader.exec_module(mod)  # type: ignore
            added = 0
            if hasattr(mod, "register"):
                mod.register(TOOLS)  # type: ignore
                added = -1
            if hasattr(mod, "TOOLS"):
                tools_map = getattr(mod, "TOOLS")
                if isinstance(tools_map, dict):
                    TOOLS.update(tools_map)
                    added = len(tools_map)
            log.info("loaded_tool_plugin", extra={"extra": {"file": fname, "added": added}})
        except Exception as e:
            log.error("tool_plugin_error", extra={"extra": {"file": fname, "error": str(e)}})

load_tool_plugins()

# ===================== Agents and Streaming =====================

class Agent:
    def __init__(self, name: str, system: str, temperature: float = 0.3):
        self.name = name
        self.system = system
        self.temperature = temperature

    def stream(self, history: List[Dict[str, str]], model: str) -> Generator[str, None, None]:
        _, chat_url = get_ollama_endpoints()
        roster = ", ".join(sorted({m.get("name") for m in history if m.get("name")} - {None})) or ""
        sysmsg = self.system + "\n\n" + COLLAB_GUIDANCE + (f"\nTeam: {roster}" if roster else "")
        messages = [{"role": "system", "content": sysmsg}] + history

        attempts = 4
        for attempt in range(attempts):
            try:
                with _http.stream(
                    "POST",
                    chat_url,
                    json={
                        "model": model,
                        "messages": messages,
                        "options": {"temperature": self.temperature},
                        "stream": True,
                    },
                ) as r:
                    r.raise_for_status()
                    for line in r.iter_lines():
                        if not line:
                            continue
                        try:
                            data = json.loads(line)
                        except Exception:
                            continue
                        msg = data.get("message") or {}
                        delta = msg.get("content")
                        if delta:
                            yield delta
                        if data.get("done") is True:
                            return
            except (httpx.ReadTimeout, httpx.ConnectTimeout, httpx.TransportError) as e:
                if attempt == attempts - 1:
                    raise
                sleep_s = 0.5 * (2 ** attempt)
                log.warning("ollama_stream_retry", extra={"extra": {"attempt": attempt + 1, "sleep": sleep_s, "err": str(e)}})
                time.sleep(sleep_s)

class HumanAdmin(Agent):
    def __init__(self):
        super().__init__(
            "Human_Admin",
            "You are the human. Provide brief feedback or code. Use TERMINATE to finish when satisfied.",
        )

    def stream(self, history: List[Dict[str, str]], model: str):  # type: ignore[override]
        msg = state.user_input_q.get()
        if msg is None:
            return
        if "```python" in (msg or ""):
            code = msg.split("```python", 1)[1].split("```", 1)[0]
            out = execute_python(code)
            yield f"Executed code. Output:\n{out}"
        else:
            yield msg

# ===================== Manager =====================

class Manager:
    """
    Central controller that selects next speaker and decides termination.
    Modes:
      - RoundRobin: cycles through agents.
      - Directed: stages: Build → Review → UX → Finalize; considers artifacts.
      - Manual: waits for chooser input via /choose_next.
    """

    def __init__(self, names: List[str], mode: str = "Directed"):
        self.names = names[:]  # includes Human_Admin
        self.mode = mode
        self._rr = 0
        # tracking
        self.last_artifact_turn = -1
        self.has_reviewed_since_artifact = False
        self.has_ux_since_artifact = False
        self.turn = 0
        # classify roles
        self.roles: Dict[str, str] = {}
        for n in self.names:
            nl = n.lower()
            role = "other"
            if any(k in nl for k in ROLE_HINTS["programmer"]["keywords"]):
                role = "programmer"
            elif any(k in nl for k in ROLE_HINTS["reviewer"]["keywords"]):
                role = "reviewer"
            elif any(k in nl for k in ROLE_HINTS["ux"]["keywords"]):
                role = "ux"
            elif n == "Human_Admin":
                role = "human"
            self.roles[n] = role

    def note_message(self, speaker: str, content: str) -> None:
        # artifact signal
        if CODE_FENCE_RE.search(content) or "tool:write_file" in content:
            self.last_artifact_turn = self.turn
            self.has_reviewed_since_artifact = False
            self.has_ux_since_artifact = False
        # stage flags
        role = self.roles.get(speaker, "other")
        if role == "reviewer" and self.last_artifact_turn >= 0:
            self.has_reviewed_since_artifact = True
        if role == "ux" and self.last_artifact_turn >= 0:
            self.has_ux_since_artifact = True
        self.turn += 1

    def choose(self, history: List[Dict[str, str]]) -> Tuple[str, str]:
        if self.mode == "Manual":
            try:
                who = state.manual_next_q.get(timeout=3600)
                if who in self.names:
                    return who, "manual selection"
            except Exception:
                pass
            return "Human_Admin", "manual timeout fallback"

        if self.mode == "RoundRobin":
            pick = self.names[self._rr % len(self.names)]
            self._rr += 1
            return pick, "round-robin"

        # Directed policy
        prog = self._first_by_role("programmer")
        rev = self._first_by_role("reviewer")
        ux = self._first_by_role("ux")
        human = "Human_Admin" if "Human_Admin" in self.names else None

        if self.last_artifact_turn < 0 and prog:
            return prog, "directed: build phase (no artifact yet)"

        if self.last_artifact_turn >= 0:
            if rev and not self.has_reviewed_since_artifact:
                return rev, "directed: post-artifact review"
            if ux and not self.has_ux_since_artifact:
                return ux, "directed: post-artifact ux"

        if prog:
            return prog, "directed: refine"
        if human:
            return human, "directed: human check"

        pick = self.names[self._rr % len(self.names)]
        self._rr += 1
        return pick, "directed: fallback round-robin"

    def should_terminate(self) -> bool:
        if self.mode != "Directed":
            return False
        if self.last_artifact_turn < 0:
            return False
        rev = self._first_by_role("reviewer")
        ux = self._first_by_role("ux")
        if rev and not self.has_reviewed_since_artifact:
            return False
        if ux and not self.has_ux_since_artifact:
            return False
        return True

    def _first_by_role(self, role: str) -> Optional[str]:
        for n in self.names:
            if self.roles.get(n) == role:
                return n
        return None

# ===================== Orchestrator =====================

def default_system(name: str) -> str:
    n = name.lower()
    if any(k in n for k in ["programmer", "developer", "coder", "engineer"]):
        return "You are a Python Developer. Focus on code and artifacts.\n" + COLLAB_GUIDANCE
    if any(k in n for k in ["critic", "review", "reviewer", "qa", "tester"]):
        return "You are a Code Reviewer. Critique and suggest precise fixes.\n" + COLLAB_GUIDANCE
    if any(k in n for k in ["ux", "ui", "designer", "design"]):
        return "You are a UX/UI Designer. Improve affordances, layout, naming, readability.\n" + COLLAB_GUIDANCE
    return f"You are a collaborator named {name}.\n" + COLLAB_GUIDANCE

def execute_python(code: str) -> str:
    fp = os.path.join(WORKSPACE_DIR, f"tmp_{binascii.b2a_hex(os.urandom(4)).decode()}.py")
    try:
        with open(fp, "w", encoding="utf-8") as f:
            f.write(code)
        p = subprocess.run([sys.executable, fp], capture_output=True, text=True, timeout=60)
        out = p.stdout
        if p.stderr:
            out += "\n--- STDERR ---\n" + p.stderr
        return out
    except Exception as e:
        return f"exec error: {e}"
    finally:
        try:
            os.remove(fp)
        except Exception:
            pass

def run_orchestrator(goal: str, model: str, agents_cfg: List[Dict[str, Any]], manager_mode: str, out_q: "queue.Queue[str]", max_turns: int = 60):
    try:
        history: List[Dict[str, str]] = []
        agents: Dict[str, Agent] = {
            cfg["name"]: Agent(
                cfg["name"],
                (cfg.get("system") or default_system(cfg["name"])),
                temperature=float(cfg.get("temperature", 0.3)),
            ) for cfg in agents_cfg
        }
        agents["Human_Admin"] = HumanAdmin()
        names = list(agents.keys())
        mgr = Manager(names, manager_mode)

        # Initial
        history.append({"role": "user", "content": f"Goal: {goal}"})
        out_q.put(json.dumps({"type": "chat", "sender": "System", "message": f"Goal set: {goal}"}))

        for turn in range(max_turns):
            if state.stop_event.is_set():
                break

            next_name, rationale = mgr.choose(history)
            if next_name not in agents:
                out_q.put(json.dumps({"type": "chat", "sender": "Error", "message": f"Manager picked unknown agent: {next_name}"}))
                break

            out_q.put(json.dumps({"type": "chat", "sender": "Manager", "message": f"Next: {next_name}  ·  reason: {rationale}"}))

            speaker = agents[next_name]
            msg_id = f"t{turn}_{next_name}"

            out_q.put(json.dumps({"type": "status", "state": "waiting_for_input" if isinstance(speaker, HumanAdmin) else "running"}))
            out_q.put(json.dumps({"type": "stream_start", "id": msg_id, "sender": next_name}))

            assembled: List[str] = []
            try:
                for delta in speaker.stream(history, model) or []:
                    if state.stop_event.is_set():
                        break
                    assembled.append(delta)
                    out_q.put(json.dumps({"type": "token", "id": msg_id, "delta": delta}))
            except Exception as e:
                out_q.put(json.dumps({"type": "chat", "sender": next_name, "message": f"[stream error] {e}"}))
            finally:
                out_q.put(json.dumps({"type": "stream_end", "id": msg_id}))

            full_msg = ''.join(assembled)

            # Ignore TERMINATE from non-human agents
            if next_name != "Human_Admin" and "TERMINATE" in (full_msg or "").upper():
                log.info("terminate_ignored", extra={"extra": {"by": next_name}})
                full_msg = full_msg.replace("TERMINATE", "")

            history.append({"role": "assistant", "name": next_name, "content": full_msg})
            mgr.note_message(next_name, full_msg)

            # Tool calls embedded in ```tool:name\n{...}```
            for m in TOOL_RE.finditer(full_msg or ""):
                tool_name = m.group("name").strip()
                try:
                    args = json.loads(m.group("body"))
                    fn = TOOLS.get(tool_name)
                    if not fn:
                        out_q.put(json.dumps({"type": "chat", "sender": "tool", "message": f"unknown tool: {tool_name}"}))
                    else:
                        log.info("tool_call", extra={"extra": {"tool": tool_name, "args": args}})
                        res = fn(**args)
                        if not isinstance(res, str):
                            res = json.dumps(res, indent=2)
                        out_q.put(json.dumps({"type": "chat", "sender": "tool", "message": f"{tool_name} ->\n{res}"}))
                except Exception as e:
                    out_q.put(json.dumps({"type": "chat", "sender": "tool", "message": f"{tool_name} error: {e}"}))

            # Manager-driven termination (only in Directed). Human may still type TERMINATE.
            if mgr.should_terminate():
                out_q.put(json.dumps({"type": "chat", "sender": "Manager", "message": "All required roles completed post-artifact. TERMINATE"}))
                break

        out_q.put(json.dumps({"type": "status", "state": "idle"}))

    except Exception as e:
        out_q.put(json.dumps({"type": "chat", "sender": "Error", "message": f"{type(e).__name__}: {e}"}))
    finally:
        out_q.put("[DONE]")

# ===================== SSE Endpoint =====================

@app.get("/stream")
def stream():
    model = request.args.get("model", DEFAULT_MODEL)
    goal_b64 = request.args.get("goal", "")
    agents_b64 = request.args.get("agents", "")
    manager_mode = request.args.get("manager_mode", "Directed")  # Directed | RoundRobin | Manual
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

@app.post("/choose_next")
def choose_next():
    name = (request.json or {}).get("name")
    try:
        state.manual_next_q.put_nowait(name)
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
    # size check
    f.stream.seek(0, io.SEEK_END)
    size = f.stream.tell()
    f.stream.seek(0)
    if size > MAX_UPLOAD_BYTES:
        return jsonify({"error": "too_large", "max": MAX_UPLOAD_BYTES}), 413
    # mime check
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
