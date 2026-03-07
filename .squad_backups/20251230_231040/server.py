import base64
import io
import json
import os
import queue
import subprocess
import time
import shutil
import asyncio
import struct
from typing import Any, Dict, List, Optional, Tuple

# WINDOWS FIX: Only load these on Linux/Mac
try:
    import pty
    import fcntl
    import termios
except ImportError:
    # On Windows, these don't exist, so we set them to None to prevent crashing.
    # Note: The "Terminal" tab in the app will not work on Windows.
    pty = None
    fcntl = None
    termios = None

import httpx
from quart import Quart, Response, jsonify, request, send_from_directory, render_template, websocket
from werkzeug.utils import secure_filename

from app import log
from app.config import (
    ALLOWED_UPLOAD_MIMES,
    DEFAULT_MODEL,
    MAX_UPLOAD_BYTES,
    MODELS_TTL_SEC,
    SESSIONS_DIR,
    GEMINI_API_KEY,
)
from app.core import run_orchestrator
from .scenario import generate_agents_from_scenario
from app.state import state
from app.tools import TOOLS
from app.utils import tree_listing, get_current_workspace
from autogen_agentchat.agents import AssistantAgent, UserProxyAgent
from autogen_agentchat.messages import TextMessage
from autogen_core.models import UserMessage
from autogen_agentchat.teams import SelectorGroupChat
from autogen_ext.models.openai import OpenAIChatCompletionClient

# To handle templates and static files correctly when run from main.py
app_dir = os.path.dirname(os.path.abspath(__file__))
root_dir = os.path.dirname(app_dir)
app = Quart(__name__,
            template_folder=os.path.join(root_dir, 'templates'),
            static_folder=os.path.join(root_dir, 'static'))

_models_cache: Dict[str, Any] = {"ts": 0.0, "names": []}


def _run_orchestrator_thread(goal, model, agents_cfg, manager_mode, out_q, max_turns, human_proxy, temperature, autonomous_user):
    """Target for the orchestrator thread."""
    try:
        asyncio.run(run_orchestrator(goal, model, agents_cfg, manager_mode, out_q, max_turns, human_proxy, temperature, autonomous_user))
    except Exception as e:
        log.error("orchestrator_thread_error", error=str(e))
        # Ensure [DONE] is sent even if an error occurs
        # Ensure [DONE] is sent even if an error occurs
        out_q.put("[DONE]")

@app.route('/api/sync', methods=['POST'])
async def sync_config():
    data = await request.get_json()
    # Update config without clearing fields not in payload (partial update)
    if data:
        state.config.update(data)
        # Broadcast config update to keep all clients in sync
        state.publish_message(json.dumps({
            "type": "config",
            "data": state.config
        }))
    return jsonify({"status": "ok"})

class BroadcastQueue:
    """Queue-like object that broadcasts to AppState."""
    def put(self, item, block=True, timeout=None):
        state.publish_message(item)
    
    def put_nowait(self, item):
        state.publish_message(item)

@app.route("/stream", methods=["GET", "POST"])
async def stream():
    if request.method == "POST":
        # New Run or Resume
        data = await request.get_json() or {}
        
        # If new_session requested, create fresh workspace/session
        if data.get("new_session"):
            # Stop existing first
            await asyncio.to_thread(state.stop)
            
            import datetime
            ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
            # Sanitize goal for name if possible, else timestamp
            new_name = f"session_{ts}"
            state.current_session_name = new_name
            
            # Broadcast reset signal
            state.publish_message(json.dumps({
                "type": "control",
                "action": "reset",
                "session": new_name
            }))
            
        # Use config if data is empty (resuming or running from synchronized state)
        if not data.get("goal") and not data.get("agents"):
             # Use current state.config content
             print("Using synchronized config for run")
             data = state.config

        model = data.get("model", DEFAULT_MODEL)
        goal = data.get("goal", "")
        agents_cfg = data.get("agents", [])
        manager_mode = data.get("manager_mode", "auto")
        max_turns = int(data.get("turns") or 60)
        human_proxy = bool(data.get("human_proxy", False))
        temperature = float(data.get("temperature") or 0.3)
        autonomous_user = bool(data.get("autonomous_user", False))
        
        # Stop existing if running
        await asyncio.to_thread(state.stop)
        
        # Start new
        broadcast_q = BroadcastQueue()
        thread_args = (goal, model, agents_cfg, manager_mode, broadcast_q, max_turns, human_proxy, temperature, autonomous_user)
        state.start(_run_orchestrator_thread, thread_args)

    # For both GET and POST, we subscribe to the stream
    client_q = queue.Queue()
    state.queues.add(client_q)
    
    def safe_json_dumps(obj, default="{}"):
        try:
            return json.dumps(obj)
        except Exception:
            return default

    async def gen():
        log.info("stream_connected")
        try:
            # 0. Sync Current State (Config & Session)
            # Critical for new clients (e.g. mobile) to get state if history was cleared
            try:
                # Use deepcopy or dict() to avoid modification during iteration if config is being updated
                cfg_copy = dict(state.config)
                yield f"data: {safe_json_dumps({'type': 'config', 'data': cfg_copy})}\n\n"
                yield f"data: {safe_json_dumps({'type': 'session_state', 'data': {'name': state.current_session_name}})}\n\n"
                yield f"data: {safe_json_dumps({'type': 'drafts_sync', 'data': state.drafts})}\n\n"
            except Exception as e:
                log.error("stream_sync_error", error=str(e))


            # 1. Replay History
            for old_msg in list(state.history):
                # Ensure no raw newlines in data payload (SSE requirement)
                safe_msg = old_msg.replace('\n', ' ') if old_msg else ""
                yield f"data: {safe_msg}\n\n"
            
            # 2. Sync Status
            current_status = "running" if state.is_running else "idle"
            yield f"data: {{\"type\": \"status\", \"state\": \"{current_status}\"}}\n\n"

            # 3. Stream New Messages
            last_ping = time.time()
            while True:
                try:
                    item = client_q.get_nowait()
                    try:
                        # Defensive check for logic that puts [DONE]
                        if item == "[DONE]":
                            break
                        
                        # Ensure item is a single line string for SSE
                        safe_item = str(item).replace('\n', ' ')
                        yield f"data: {safe_item}\n\n"
                    except Exception as e:
                        log.error("stream_yield_error", error=str(e))
                        break
                except queue.Empty:
                    await asyncio.sleep(0.1)
                    if time.time() - last_ping > 5:
                        try:
                            yield ": ping\n\n"
                        except Exception as e:
                            log.error("stream_ping_error", error=str(e))
                            break
                        last_ping = time.time()
                except asyncio.CancelledError:
                    break
                except Exception as e:
                     log.error("stream_inner_error", error=str(e))
                     break
        finally:
            state.queues.discard(client_q)
            log.info("stream_disconnected")

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
async def user_input():
    data = await request.get_json()
    msg = (data or {}).get("message")
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
    num_agents = data.get("num_agents", 3)
    if not scenario:
        return jsonify({"error": "missing_scenario"}), 400

    try:
        # Gather file context
        files = []
        try:
            skipped_dirs = {'.git', '__pycache__', 'node_modules', 'venv', '.idea', '.vscode'}
            for root, dirs, filenames in os.walk(get_current_workspace()):
                dirs[:] = [d for d in dirs if d not in skipped_dirs]
                for f in filenames:
                    if f.startswith('.'): continue
                    if f.endswith(('.pyc', '.pyo', '.pyd', '.git')): continue
                    files.append(os.path.relpath(os.path.join(root, f), get_current_workspace()))
                    if len(files) > 50: # sensible limit
                        files.append("...(truncated)")
                        break
                if len(files) > 50:
                    break
        except Exception:
            files = ["(Error listing files)"]
            
        file_context = "\n".join(files)

        agents, suggested_goal = await generate_agents_from_scenario(scenario, model, num_agents, file_context)
        return jsonify({"agents": agents, "goal": suggested_goal})
    except Exception as e:
        log.error("failed_to_generate_agents", exc_info=e)
        return jsonify({"error": str(e)}), 500

@app.post("/choose_next")
async def choose_next():
    data = await request.get_json()
    name = (data or {}).get("name")
    try:
        state.manual_next_q.put_nowait(name)
    except Exception:
        pass
    return jsonify({"ok": True})

from quart import abort

@app.post("/api/settings/ollama")
def set_ollama():
    abort(404)

@app.get("/api/models")
def api_models():
    # Hardcoded list of Gemini models
    return jsonify(["gemini-2.0-flash", "gemini-1.5-flash", "gemini-1.5-pro", "gemini-1.0-pro"])

@app.get("/api/tools")
def api_tools():
    """Returns a list of available tools."""
    tool_list = []
    for name, func in TOOLS.items():
        tool_list.append({
            "name": name,
            "description": func.__doc__
        })
    return jsonify(tool_list)

@app.post("/api/agent/run")
async def api_agent_run():
    data = await request.get_json()
    agent_config = data.get("agent")
    message = data.get("message")
    model = data.get("model", DEFAULT_MODEL)

    if not agent_config or not message:
        return jsonify({"error": "missing_agent_or_message"}), 400

    try:
        # 1. Create model client
        gemini_client = OpenAIChatCompletionClient(
            model=model,
            api_key=GEMINI_API_KEY,
            base_url="https://generativelanguage.googleapis.com/v1beta/openai/"
        )

        # 2. Filter the tools based on the user's selection
        selected_tool_names = agent_config.get("tools", [])
        selected_tools = [TOOLS[name] for name in selected_tool_names if name in TOOLS]

        # 3. Create the agent
        agent = AssistantAgent(
            name=agent_config.get("name", "playground_agent"),
            model_client=gemini_client,
            system_message=agent_config.get("system_message", "You are a helpful assistant."),
            tools=selected_tools,
        )

        # 4. Create a user proxy agent to stand in for the user
        user_proxy = UserProxyAgent(name="user_proxy")

        # 5. Use a simple group chat of two agents to run the conversation
        team = SelectorGroupChat(participants=[user_proxy, agent], max_turns=5, model_client=gemini_client)

        # This can be run directly since we are in an async route.
        final = None
        async for event in team.run_stream(task=message):
            final = event

        reply = final.messages[-1].content if final and final.messages else "No response."

        return jsonify({"reply": reply})
    except Exception as e:
        log.error("agent_run_error: %s", str(e))
        return jsonify({"error": "agent_run_failed", "message": str(e)}), 500

@app.get("/api/workspace/files")
def api_files_flat():
    out = []
    ws = get_current_workspace()
    for item in os.listdir(ws):
        p = os.path.join(ws, item)
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
    fp = os.path.join(get_current_workspace(), fn)
    os.makedirs(os.path.dirname(fp), exist_ok=True)
    f.save(fp)
    return jsonify({"ok": True, "file": fn, "bytes": size})

@app.get("/workspace/<path:fn>")
async def ws_file(fn: str):
    return await send_from_directory(get_current_workspace(), fn)

@app.delete("/api/workspace/delete")
async def api_delete_file():
    data = await request.get_json()
    path = data.get("path")
    if not path:
        return jsonify({"error": "missing_path"}), 400

    # Security: Ensure the path is within the workspace directory
    base_path = os.path.abspath(get_current_workspace())
    target_path = os.path.abspath(os.path.join(base_path, path))

    if not target_path.startswith(base_path):
        return jsonify({"error": "access_denied"}), 403

    try:
        if os.path.isdir(target_path):
            shutil.rmtree(target_path)
        else:
            os.remove(target_path)
        return jsonify({"ok": True})
    except FileNotFoundError:
        return jsonify({"error": "not_found"}), 404
    except OSError as e:
        return jsonify({"error": "os_error", "message": str(e)}), 500

@app.route('/favicon.ico')
async def favicon():
    return Response("", status=204)

@app.post("/api/workspace/new_folder")
async def api_new_folder():
    data = await request.get_json()
    path = data.get("path")
    if not path:
        return jsonify({"error": "missing_path"}), 400

    # Security: Ensure the path is within the workspace directory
    base_path = os.path.abspath(get_current_workspace())
    target_path = os.path.abspath(os.path.join(base_path, path))

    if not target_path.startswith(base_path):
        return jsonify({"error": "access_denied"}), 403

    try:
        os.makedirs(target_path, exist_ok=True)
        return jsonify({"ok": True})
    except OSError as e:
        return jsonify({"error": "os_error", "message": str(e)}), 500

@app.post("/api/workspace/file")
async def api_save_file():
    data = await request.get_json()
    path = data.get("path")
    content = data.get("content")
    if path is None or content is None:
        return jsonify({"error": "missing_path_or_content"}), 400

    # Security: Ensure the path is within the workspace directory
    base_path = os.path.abspath(get_current_workspace())
    target_path = os.path.abspath(os.path.join(base_path, path))

    if not target_path.startswith(base_path):
        return jsonify({"error": "access_denied"}), 403

    try:
        with open(target_path, "w", encoding="utf-8") as f:
            f.write(content)
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"error": "write_error", "message": str(e)}), 500

@app.post("/api/draft")
async def api_draft():
    """Save a temporary draft and broadcast it."""
    data = await request.get_json()
    path = data.get("path")
    content = data.get("content")
    if not path:
        return jsonify({"error": "missing_path"}), 400

    state.drafts[path] = content
    # Broadcast to other clients
    state.publish_message(json.dumps({
        "type": "draft",
        "path": path,
        "content": content
    }))
    return jsonify({"ok": True})

@app.get("/api/workspace/file")
async def api_get_file():
    path = request.args.get("path")
    if not path:
        return jsonify({"error": "missing_path"}), 400

    # Security: Ensure the path is within the workspace directory
    base_path = os.path.abspath(get_current_workspace())
    target_path = os.path.abspath(os.path.join(base_path, path))

    if not target_path.startswith(base_path):
        return jsonify({"error": "access_denied"}), 403

    try:
        with open(target_path, "r", encoding="utf-8") as f:
            content = f.read()
        return Response(content, mimetype="text/plain")
    except FileNotFoundError:
        return jsonify({"error": "not_found"}), 404
    except Exception as e:
        return jsonify({"error": "read_error", "message": str(e)}), 500

@app.post("/api/workspace/rename")
async def api_rename_file():
    data = await request.get_json()
    old_path = data.get("old_path")
    new_path = data.get("new_path")
    if not old_path or not new_path:
        return jsonify({"error": "missing_path"}), 400

    # Security: Ensure the paths are within the workspace directory
    base_path = os.path.abspath(get_current_workspace())

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
    """List sessions with metadata (title, modified time)."""
    sessions = []
    for f in os.listdir(SESSIONS_DIR):
        if not f.endswith(".json"): continue
        path = os.path.join(SESSIONS_DIR, f)
        try:
            stat = os.stat(path)
            # Efficiently read just the title if possible, or load full json
            # Validating JSON is safer.
            with open(path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
                title = data.get("title", f.replace(".json", ""))
                
            sessions.append({
                "id": f.replace(".json", ""),
                "title": title,
                "modified": stat.st_mtime
            })
        except Exception:
            continue
            
    # Sort by modified desc
    sessions.sort(key=lambda x: x["modified"], reverse=True)
    return jsonify(sessions)

@app.post("/api/sessions/rename_ai")
async def sessions_rename_ai():
    data = await request.get_json() or {}
    name = data.get("name") or state.current_session_name
    goal = data.get("goal") or state.config.get("goal")
    
    if not goal:
        return jsonify({"skipped": True})
        
    # Check if we already have a custom title
    fp = os.path.join(SESSIONS_DIR, f"{name}.json")
    if os.path.exists(fp):
         try:
             with open(fp, "r", encoding="utf-8") as f:
                 d = json.load(f)
                 if d.get("title") and d.get("title") != name:
                     # Already has a title
                     return jsonify({"skipped": True}) 
         except: pass

    try:
        # Generate Title
        client = OpenAIChatCompletionClient(
            model="gemini-1.5-flash",
            api_key=GEMINI_API_KEY,
            base_url="https://generativelanguage.googleapis.com/v1beta/openai/"
        )
        prompt = f"Generate a very short, concise title (3-5 words max) for a project with this goal: '{goal}'. Output ONLY the title."
        resp = await client.create(messages=[UserMessage(content=prompt, source="user")])
        title = resp.content.strip().strip('"').strip("'")
        
        # Save title to session file
        if os.path.exists(fp):
            with open(fp, "r+", encoding="utf-8") as f:
                d = json.load(f)
                d["title"] = title
                f.seek(0)
                json.dump(d, f, indent=2)
                f.truncate()
        
        # Update run-time state
        if state.current_session_name == name:
             state.config["title"] = title
             
        return jsonify({"ok": True, "title": title})
    except Exception as e:
        log.error("ai_naming_failed", exc_info=True)
        return jsonify({"error": str(e)}), 500

@app.get("/api/sessions/<path:name>")
def sessions_get(name: str):
    fp = os.path.join(SESSIONS_DIR, name)
    if not os.path.exists(fp):
        return jsonify({"error": "not_found"}), 404
    with open(fp, "r", encoding="utf-8") as f:
        return jsonify(json.load(f))

@app.post("/api/sessions")
async def sessions_save():
    data = await request.get_json() or {}
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

@app.post("/api/sessions/activate")
async def sessions_activate():
    data = await request.get_json() or {}
    name = data.get("name")
    if not name:
        return jsonify({"error": "missing_name"}), 400
    state.current_session_name = name
    # Ensure it exists and return path
    ws = get_current_workspace()
    return jsonify({"ok": True, "workspace": ws})

@app.post("/api/transcript/md")
async def export_md():
    payload = await request.get_json() or {}
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
    fp = os.path.join(get_current_workspace(), fn)
    with open(fp, "w", encoding="utf-8") as f:
        f.write(md)
    return jsonify({"ok": True, "file": f"/workspace/{fn}"})

@app.post("/api/transcript/html")
async def export_html():
    payload = await request.get_json() or {}
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
    fp = os.path.join(get_current_workspace(), fn)
    with open(fp, "w", encoding="utf-8") as f:
        f.write(html)
    return jsonify({"ok": True, "file": f"/workspace/{fn}"})

@app.websocket("/api/workspace/terminal")
async def ws_terminal():
    """Handle pseudo-terminal (Windows compatible)."""
    await websocket.accept()
    
    # Use standard shell
    import sys
    shell = "powershell.exe" if os.name == 'nt' else "/bin/bash"
    
    # Start subprocess
    process = subprocess.Popen(
        [shell],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        bufsize=0,
        cwd=get_current_workspace(),
        shell=False # Important for Popen with pipes on Windows
    )
    
    async def forward_output(stream):
        """Read from stream and forward to websocket."""
        try:
            while True:
                output = await asyncio.to_thread(stream.read, 1024)
                if not output:
                    break
                # Handle decoding, replace errors to avoid crash
                try:
                    text = output.decode('utf-8', errors='replace')
                    # Convert newlines for terminal
                    text = text.replace('\n', '\r\n') if os.name == 'nt' else text
                    await websocket.send(text)
                except Exception:
                   pass
        except Exception:
            pass

    async def forward_input():
        """Read from websocket and forward to process stdin."""
        try:
            while True:
                data = await websocket.receive()
                if data.startswith("resize:"):
                    continue # Ignore resize on Windows (not supported via pipes)
                
                # Write to process
                if process.stdin:
                     await asyncio.to_thread(process.stdin.write, data.encode())
                     await asyncio.to_thread(process.stdin.flush)
        except Exception:
            pass
            
    # Tasks
    out_task = asyncio.create_task(forward_output(process.stdout))
    err_task = asyncio.create_task(forward_output(process.stderr))
    in_task = asyncio.create_task(forward_input())
    
    try:
        await asyncio.gather(in_task, out_task, err_task)
    except Exception:
        pass
    finally:
        process.terminate()

@app.get("/")
async def index():
    html = await render_template("index.html")
    headers = {
        "Cache-Control": "no-cache, no-store, must-revalidate",
        "Pragma": "no-cache",
        "Expires": "0",
    }
    return Response(html, mimetype="text/html", headers=headers)
