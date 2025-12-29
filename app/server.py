import base64
import io
import json
import os
import queue
import subprocess
import time
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
    WORKSPACE_DIR,
    GEMINI_API_KEY,
)
from app.core import run_orchestrator
from .scenario import generate_agents_from_scenario
from app.state import state
from app.tools import TOOLS
from app.utils import tree_listing
from autogen_agentchat.agents import AssistantAgent, UserProxyAgent
from autogen_agentchat.messages import TextMessage
from autogen_agentchat.teams import SelectorGroupChat
from autogen_ext.models.openai import OpenAIChatCompletionClient

# To handle templates and static files correctly when run from main.py
app_dir = os.path.dirname(os.path.abspath(__file__))
root_dir = os.path.dirname(app_dir)
app = Quart(__name__,
            template_folder=os.path.join(root_dir, 'templates'),
            static_folder=os.path.join(root_dir, 'static'))

_models_cache: Dict[str, Any] = {"ts": 0.0, "names": []}


import asyncio

def _run_orchestrator_thread(goal, model, agents_cfg, manager_mode, out_q, max_turns, human_proxy, temperature):
    """Target for the orchestrator thread."""
    try:
        asyncio.run(run_orchestrator(goal, model, agents_cfg, manager_mode, out_q, max_turns, human_proxy, temperature))
    except Exception as e:
        log.error("orchestrator_thread_error", error=str(e))
        # Ensure [DONE] is sent even if an error occurs
        out_q.put("[DONE]")

@app.get("/stream")
async def stream():
    model = request.args.get("model", DEFAULT_MODEL)
    goal_b64 = request.args.get("goal", "")
    agents_b64 = request.args.get("agents", "")
    manager_mode = request.args.get("manager_mode", "auto")
    max_turns = int(request.args.get("turns", "60"))
    human_proxy = request.args.get("human_proxy", "false").lower() == "true"
    temperature = float(request.args.get("temperature", "0.3"))
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
    thread_args = (goal, model, agents_cfg, manager_mode, out_q, max_turns, human_proxy, temperature)
    state.start(_run_orchestrator_thread, thread_args)

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
    num_agents = data.get("num_agents", 3)
    if not scenario:
        return jsonify({"error": "missing_scenario"}), 400

    try:
        agents, suggested_goal = await generate_agents_from_scenario(scenario, model, num_agents)
        return jsonify({"agents": agents, "goal": suggested_goal})
    except Exception as e:
        log.error("failed_to_generate_agents", exc_info=e)
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
    # Deprecated/Removed for Gemini switch, but keeping a stub to avoid frontend breaking if it calls it
    return jsonify({"ok": True, "message": "Ollama support has been replaced with Gemini."})

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

        # We need to extract the response. The final event is likely the last message?
        # But run_stream yields events.
        # final might be the last event.
        reply = "No response."
        if final and hasattr(final, "messages") and final.messages:
             reply = final.messages[-1].content
        elif final and hasattr(final, "content"):
             reply = final.content

        return jsonify({"reply": reply})
    except Exception as e:
        log.error("agent_run_error: %s", str(e))
        return jsonify({"error": "agent_run_failed", "message": str(e)}), 500

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
async def ws_file(fn: str):
    return await send_from_directory(WORKSPACE_DIR, fn)

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

@app.post("/api/workspace/file")
async def api_save_file():
    data = await request.get_json()
    path = data.get("path")
    content = data.get("content")
    if path is None or content is None:
        return jsonify({"error": "missing_path_or_content"}), 400

    # Security: Ensure the path is within the workspace directory
    base_path = os.path.abspath(WORKSPACE_DIR)
    target_path = os.path.abspath(os.path.join(base_path, path))

    if not target_path.startswith(base_path):
        return jsonify({"error": "access_denied"}), 403

    try:
        with open(target_path, "w", encoding="utf-8") as f:
            f.write(content)
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"error": "write_error", "message": str(e)}), 500

@app.get("/api/workspace/file")
async def api_get_file():
    path = request.args.get("path")
    if not path:
        return jsonify({"error": "missing_path"}), 400

    # Security: Ensure the path is within the workspace directory
    base_path = os.path.abspath(WORKSPACE_DIR)
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

@app.websocket("/api/workspace/terminal")
async def ws_terminal():
    """Handle the lifecycle of a pseudo-terminal for the workspace."""
    await websocket.accept()

    # Check if pty is available
    if not pty:
        await websocket.send("Error: pty not available on this system.")
        return

    # Create a child process attached to a pseudo-terminal
    pid, master_fd = pty.fork()
    if pid == 0:  # Child process
        # Start a shell
        os.execv("/bin/bash", ["/bin/bash"])

    # --- Parent Process ---
    # Set the initial window size
    try:
        # Set the window size of the pty
        fcntl.ioctl(master_fd, termios.TIOCSWINSZ, struct.pack("HHHH", 24, 80, 0, 0))
    except Exception as e:
        log.error("failed_to_set_winsize", error=str(e))

    async def forward_pty_output():
        """Read from pty and forward to websocket."""
        try:
            while True:
                # This is a blocking read, so run it in an executor
                data = await asyncio.to_thread(os.read, master_fd, 1024)
                if not data:
                    break
                await websocket.send(data.decode(errors="ignore"))
        except (asyncio.CancelledError, OSError):
            pass

    async def forward_websocket_input():
        """Read from websocket and forward to pty."""
        try:
            while True:
                data = await websocket.receive()
                # Handle resize messages from the frontend
                if data.startswith("resize:"):
                    try:
                        _, rows, cols = data.split(":")
                        winsize = struct.pack("HHHH", int(rows), int(cols), 0, 0)
                        fcntl.ioctl(master_fd, termios.TIOCSWINSZ, winsize)
                    except Exception as e:
                        log.error("failed_to_resize_pty", error=str(e))
                else:
                    os.write(master_fd, data.encode())
        except asyncio.CancelledError:
            pass

    # Create and run the two forwarding tasks
    pty_task = asyncio.create_task(forward_pty_output())
    ws_task = asyncio.create_task(forward_websocket_input())

    try:
        await asyncio.gather(pty_task, ws_task)
    except Exception as e:
        log.error("terminal_session_error", error=str(e))
    finally:
        # Ensure the child process is killed when the connection is closed
        try:
            os.kill(pid, 9)
            os.waitpid(pid, 0)
        except OSError:
            pass # Process may have already exited
        log.info("terminal_session_closed", extra={"pid": pid})

@app.get("/")
async def index():
    html = await render_template("index.html")
    headers = {
        "Cache-Control": "no-cache, no-store, must-revalidate",
        "Pragma": "no-cache",
        "Expires": "0",
    }
    return Response(html, mimetype="text/html", headers=headers)
