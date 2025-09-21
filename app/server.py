import base64
import io
import json
import os
import queue
import time
from typing import Any, Dict, List, Optional, Tuple
from typing import Any, Dict

import httpx
from quart import Quart, Response, jsonify, request, send_from_directory, render_template
from werkzeug.utils import secure_filename

from app import log
from app.config import (
    ALLOWED_UPLOAD_MIMES,
    DEFAULT_MODEL,
    MAX_UPLOAD_BYTES,
    MODELS_TTL_SEC,
    SESSIONS_DIR,
    WORKSPACE_DIR,
)
from app.core import run_orchestrator
from app.ollama import _http, get_ollama_endpoints, set_ollama_host
from .scenario import generate_agents_from_scenario
from app.state import state
from app.utils import tree_listing

# To handle templates and static files correctly when run from main.py
app_dir = os.path.dirname(os.path.abspath(__file__))
root_dir = os.path.dirname(app_dir)
app = Quart(__name__,
            template_folder=os.path.join(root_dir, 'templates'),
            static_folder=os.path.join(root_dir, 'static'))

_models_cache: Dict[str, Any] = {"ts": 0.0, "names": []}


import asyncio

@app.get("/stream")
async def stream():
    model = request.args.get("model", DEFAULT_MODEL)
    goal_b64 = request.args.get("goal", "")
    agents_b64 = request.args.get("agents", "")
    manager_mode = request.args.get("manager_mode", "auto")
    max_turns = int(request.args.get("turns", "60"))
    human_proxy = request.args.get("human_proxy", "false").lower() == "true"
    try:
        goal = base64.b64decode(goal_b64.encode()).decode(errors="ignore") if goal_b64 else ""
    except Exception:
        goal = goal_b64
    try:
        agents_cfg = json.loads(base64.b64decode(agents_b64.encode()).decode(errors="ignore") or "[]")
    except Exception:
        agents_cfg = []
    out_q: "queue.Queue[str]" = queue.Queue()

    # We need to run the orchestrator in a separate thread with its own event loop
    def run_async_orchestrator():
        asyncio.run(run_orchestrator(goal, model, agents_cfg, manager_mode, out_q, max_turns, human_proxy))

    state.start(run_async_orchestrator, ())

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

@app.post("/api/scenario/generate")
async def scenario_generate():
    data = await request.get_json()
    scenario = data.get("scenario")
    model = data.get("model")
    if not scenario:
        return jsonify({"error": "missing_scenario"}), 400

    try:
        agents = await generate_agents_from_scenario(scenario, model)
        return jsonify(agents)
    except Exception as e:
        log.error("failed_to_generate_agents", error=e)
        return jsonify({"error": str(e)}), 500

@app.post("/choose_next")
def choose_next():
    name = (request.json or {}).get("name")
    try:
        state.manual_next_q.put_nowait(name)
    except Exception:
        pass
    return jsonify({"ok": True})

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

@app.delete("/api/workspace/delete")
async def api_delete_file():
    data = await request.get_json()
    path = data.get("path")
    if not path:
        return jsonify({"error": "missing_path"}), 400

    # Security: Ensure the path is within the workspace directory
    base_path = os.path.abspath(WORKSPACE_DIR)
    target_path = os.path.abspath(os.path.join(base_path, path))

    if not target_path.startswith(base_path):
        return jsonify({"error": "access_denied"}), 403

    try:
        if os.path.isdir(target_path):
            # This will only remove empty directories, which is a safe default
            os.rmdir(target_path)
        else:
            os.remove(target_path)
        return jsonify({"ok": True})
    except FileNotFoundError:
        return jsonify({"error": "not_found"}), 404
    except OSError as e:
        return jsonify({"error": "os_error", "message": str(e)}), 500

@app.post("/api/workspace/new_folder")
async def api_new_folder():
    data = await request.get_json()
    path = data.get("path")
    if not path:
        return jsonify({"error": "missing_path"}), 400

    # Security: Ensure the path is within the workspace directory
    base_path = os.path.abspath(WORKSPACE_DIR)
    target_path = os.path.abspath(os.path.join(base_path, path))

    if not target_path.startswith(base_path):
        return jsonify({"error": "access_denied"}), 403

    try:
        os.makedirs(target_path, exist_ok=True)
        return jsonify({"ok": True})
    except OSError as e:
        return jsonify({"error": "os_error", "message": str(e)}), 500

@app.post("/api/workspace/rename")
async def api_rename_file():
    data = await request.get_json()
    old_path = data.get("old_path")
    new_path = data.get("new_path")
    if not old_path or not new_path:
        return jsonify({"error": "missing_path"}), 400

    # Security: Ensure the paths are within the workspace directory
    base_path = os.path.abspath(WORKSPACE_DIR)

    old_target_path = os.path.abspath(os.path.join(base_path, old_path))
    # For the new path, we need to resolve the directory part and then join the new name
    new_path_dir = os.path.dirname(os.path.join(base_path, new_path))
    new_target_path = os.path.abspath(os.path.join(new_path_dir, os.path.basename(new_path)))

    if not old_target_path.startswith(base_path) or not new_target_path.startswith(base_path):
        return jsonify({"error": "access_denied"}), 403

    try:
        os.rename(old_target_path, new_target_path)
        return jsonify({"ok": True})
    except FileNotFoundError:
        return jsonify({"error": "not_found"}), 404
    except OSError as e:
        return jsonify({"error": "os_error", "message": str(e)}), 500

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
    os.remove(fp)
    return jsonify({"ok": True})

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

@app.get("/")
async def index():
    html = await render_template("index.html")
    headers = {
        "Cache-Control": "no-cache, no-store, must-revalidate",
        "Pragma": "no-cache",
        "Expires": "0",
    }
    return Response(html, mimetype="text/html", headers=headers)
