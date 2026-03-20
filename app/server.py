import asyncio
import base64
import json
import os
import queue
import shutil
import time
from pathlib import Path
from typing import Any, Dict

from quart import Quart, Response, jsonify, render_template, request, send_from_directory
from werkzeug.utils import secure_filename

from autogen_agentchat.agents import AssistantAgent
from autogen_core import CancellationToken
from autogen_ext.models.openai import OpenAIChatCompletionClient

from app import log
from app.config import (
    ALLOWED_UPLOAD_MIMES,
    DEFAULT_MODEL,
    GEMINI_API_KEY,
    MAX_UPLOAD_BYTES,
    SESSIONS_DIR,
    WORKSPACE_DIR,
    gemini_model_info,
    require_gemini_api_key,
)
from app.core import run_orchestrator
from app.scenario import generate_agents_from_scenario
from app.state import state
from app.tools import TOOLS
from app.utils import tree_listing


app_dir = os.path.dirname(os.path.abspath(__file__))
root_dir = os.path.dirname(app_dir)
app = Quart(
    __name__,
    template_folder=os.path.join(root_dir, "templates"),
    static_folder=os.path.join(root_dir, "static"),
)


def _is_production() -> bool:
    return os.environ.get("APP_ENV", "development").strip().lower() in {"prod", "production"}


if _is_production() and not (GEMINI_API_KEY or "").strip():
    raise RuntimeError("GEMINI_API_KEY is required when APP_ENV is production.")


def _workspace_root() -> str:
    return getattr(state, "active_workspace", WORKSPACE_DIR) or WORKSPACE_DIR


def _resolve_workspace_path(path: str) -> str:
    base_path = os.path.abspath(_workspace_root())
    target_path = os.path.abspath(os.path.join(base_path, path))
    if os.path.commonpath([base_path, target_path]) != base_path:
        raise PermissionError("access_denied")
    return target_path


def _run_orchestrator_thread(goal, model, agents_cfg, manager_mode, out_q, max_turns, human_proxy, temperature):
    try:
        asyncio.run(
            run_orchestrator(
                goal,
                model,
                agents_cfg,
                manager_mode,
                out_q,
                max_turns,
                human_proxy,
                temperature,
            )
        )
    except (asyncio.CancelledError, KeyboardInterrupt):
        log.info("orchestrator_thread_cancelled")
        out_q.put("[DONE]")
    except BaseException as e:
        log.error("orchestrator_thread_error", extra={"error": str(e), "type": type(e).__name__})
        out_q.put("[DONE]")


@app.get("/favicon.ico")
async def favicon():
    return "", 204


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
    thread_args = (goal, model, agents_cfg, manager_mode, out_q, max_turns, human_proxy, temperature)
    state.start(_run_orchestrator_thread, thread_args)

    async def gen():
        last_ping = time.time()
        log.info(
            "stream_started",
            extra={
                "model": model,
                "goal_len": len(goal),
                "manager_mode": manager_mode,
                "human_proxy": human_proxy,
            },
        )
        try:
            while True:
                try:
                    item = await asyncio.to_thread(out_q.get, True, 2.0)
                    yield f"data: {item}\n\n"
                    last_ping = time.time()
                    if item == "[DONE]":
                        log.info("stream_finished_cleanly")
                        break
                except queue.Empty:
                    now = time.time()
                    if now - last_ping > 2.0:
                        yield ": ping\n\n"
                        last_ping = now
                except Exception as e:
                    log.error("generator_loop_error", extra={"error": str(e)})
                    break
        except GeneratorExit:
            log.info("stream_client_disconnected")
        except Exception as e:
            log.error("stream_error", extra={"error": str(e)})
            yield f"data: {json.dumps({'type': 'status', 'state': 'error', 'message': str(e)})}\n\n"

    headers = {
        "Content-Type": "text/event-stream; charset=utf-8",
        "Cache-Control": "no-cache, no-transform",
        "Connection": "keep-alive",
        "X-Accel-Buffering": "no",
    }
    return Response(gen(), headers=headers)


@app.post("/stop")
async def stop():
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


@app.post("/coach_input")
async def coach_input():
    data = await request.get_json()
    msg = (data or {}).get("message")
    if not isinstance(msg, str) or not msg.strip():
        return jsonify({"error": "missing_message"}), 400
    try:
        state.coach_input_q.put_nowait(msg.strip())
    except Exception:
        pass
    return jsonify({"ok": True})


@app.post("/choose_next")
async def choose_next():
    data = await request.get_json()
    name = (data or {}).get("name")
    try:
        state.manual_next_q.put_nowait(name)
    except Exception:
        pass
    return jsonify({"ok": True})


@app.post("/api/scenario/generate")
async def scenario_generate():
    data = await request.get_json()
    scenario = (data or {}).get("scenario")
    model = (data or {}).get("model")
    num_agents = int((data or {}).get("num_agents", 3))
    if not scenario:
        return jsonify({"error": "missing_scenario"}), 400

    try:
        agents, suggested_goal = await generate_agents_from_scenario(scenario, model, num_agents)
        return jsonify({"agents": agents, "goal": suggested_goal})
    except Exception as e:
        log.error("failed_to_generate_agents", exc_info=e)
        return jsonify({"error": str(e)}), 500


@app.post("/api/settings/ollama")
async def set_ollama():
    return jsonify({"ok": True, "message": "Ollama support has been replaced with Gemini."})


@app.get("/api/models")
async def api_models():
    return jsonify(
        [
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
    )


@app.get("/api/tools")
async def api_tools():
    tool_list = []
    for name, func in TOOLS.items():
        tool_list.append({"name": name, "description": func.__doc__})
    return jsonify(tool_list)


@app.post("/api/agent/run")
async def api_agent_run():
    data = await request.get_json()
    agent_config = (data or {}).get("agent")
    message = (data or {}).get("message")
    model = (data or {}).get("model", DEFAULT_MODEL)

    if not agent_config or not message:
        return jsonify({"error": "missing_agent_or_message"}), 400

    if app.testing:
        return jsonify({"reply": f"Test reply: {message}"})

    try:
        gemini_client = OpenAIChatCompletionClient(
            model=model,
            api_key=require_gemini_api_key(),
            base_url="https://generativelanguage.googleapis.com/v1beta/openai/",
            model_info=gemini_model_info(model),
        )

        selected_tool_names = agent_config.get("tools", [])
        selected_tools = [TOOLS[name] for name in selected_tool_names if name in TOOLS]

        agent = AssistantAgent(
            name=agent_config.get("name", "playground_agent"),
            model_client=gemini_client,
            system_message=agent_config.get("system_message", "You are a helpful assistant."),
            model_client_stream=False,
            tools=selected_tools,
        )

        result = await agent.run(task=message, cancellation_token=CancellationToken())

        reply = "No response."
        if getattr(result, "messages", None):
            for msg in reversed(result.messages):
                content = getattr(msg, "content", None)
                if isinstance(content, str) and content.strip():
                    reply = content
                    break

        return jsonify({"reply": reply})
    except Exception as e:
        log.error("agent_run_error", extra={"error": str(e)})
        return jsonify({"error": "agent_run_failed", "message": str(e)}), 500


@app.get("/api/workspace/files")
async def api_files_flat():
    out = []
    root = _workspace_root()
    for item in os.listdir(root):
        p = os.path.join(root, item)
        if os.path.isfile(p):
            out.append({"name": item, "path": f"/workspace/{item}"})
    return jsonify(out)


@app.get("/api/workspace/tree")
async def api_files_tree():
    return jsonify(tree_listing("."))


@app.post("/api/workspace/upload")
async def api_upload():
    files = await request.files
    f = files.get("file")
    if not f:
        return jsonify({"error": "missing file"}), 400

    blob = f.read()
    size = len(blob)
    if size > MAX_UPLOAD_BYTES:
        return jsonify({"error": "too_large", "max": MAX_UPLOAD_BYTES}), 413

    mime = f.mimetype or "application/octet-stream"
    if mime not in ALLOWED_UPLOAD_MIMES and not secure_filename(f.filename).endswith((".txt", ".md", ".json", ".py")):
        return jsonify({"error": "mime_blocked", "mime": mime}), 415

    fn = secure_filename(f.filename)
    fp = _resolve_workspace_path(fn)
    os.makedirs(os.path.dirname(fp), exist_ok=True)
    with open(fp, "wb") as dest:
        dest.write(blob)

    return jsonify({"ok": True, "file": fn, "bytes": size})


@app.get("/workspace/<path:fn>")
async def ws_file(fn: str):
    root = _workspace_root()
    try:
        _resolve_workspace_path(fn)
    except PermissionError:
        return jsonify({"error": "access_denied"}), 403
    return await send_from_directory(root, fn)


@app.delete("/api/workspace/delete")
async def api_delete_file():
    data = await request.get_json()
    path = (data or {}).get("path")
    if not path:
        return jsonify({"error": "missing_path"}), 400

    try:
        target_path = _resolve_workspace_path(path)
    except PermissionError:
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


@app.post("/api/workspace/set_directory")
async def api_set_directory():
    data = await request.get_json()
    path = (data or {}).get("path")
    if not path:
        return jsonify({"error": "missing_path"}), 400

    target_path = os.path.abspath(path)
    if not os.path.exists(target_path):
        return jsonify({"error": "not_found", "message": "Path does not exist"}), 404
    if not os.path.isdir(target_path):
        return jsonify({"error": "not_a_directory", "message": "Path is not a directory"}), 400

    state.active_workspace = target_path
    return jsonify({"ok": True, "workspace": target_path})


@app.post("/api/workspace/new_folder")
async def api_new_folder():
    data = await request.get_json()
    path = (data or {}).get("path")
    if not path:
        return jsonify({"error": "missing_path"}), 400

    try:
        target_path = _resolve_workspace_path(path)
    except PermissionError:
        return jsonify({"error": "access_denied"}), 403

    try:
        os.makedirs(target_path, exist_ok=True)
        return jsonify({"ok": True})
    except OSError as e:
        return jsonify({"error": "os_error", "message": str(e)}), 500


@app.post("/api/workspace/file")
async def api_save_file():
    data = await request.get_json()
    path = (data or {}).get("path")
    content = (data or {}).get("content")
    if path is None or content is None:
        return jsonify({"error": "missing_path_or_content"}), 400

    try:
        target_path = _resolve_workspace_path(path)
    except PermissionError:
        return jsonify({"error": "access_denied"}), 403

    try:
        os.makedirs(os.path.dirname(target_path), exist_ok=True)
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

    try:
        target_path = _resolve_workspace_path(path)
    except PermissionError:
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
    old_path = (data or {}).get("old_path")
    new_path = (data or {}).get("new_path")
    if not old_path or not new_path:
        return jsonify({"error": "missing_path"}), 400

    try:
        old_target_path = _resolve_workspace_path(old_path)
        new_target_path = _resolve_workspace_path(new_path)
    except PermissionError:
        return jsonify({"error": "access_denied"}), 403

    try:
        os.makedirs(os.path.dirname(new_target_path), exist_ok=True)
        os.rename(old_target_path, new_target_path)
        return jsonify({"ok": True})
    except FileNotFoundError:
        return jsonify({"error": "not_found"}), 404
    except OSError as e:
        return jsonify({"error": "os_error", "message": str(e)}), 500


@app.post("/api/sessions/activate")
async def sessions_activate():
    data = await request.get_json()
    raw_name = (data or {}).get("name", "")
    name = secure_filename(raw_name).strip()
    if not name:
        return jsonify({"error": "missing_name"}), 400

    sessions_root = Path(WORKSPACE_DIR) / "sessions"
    session_dir = (sessions_root / name).resolve()
    sessions_root.mkdir(parents=True, exist_ok=True)

    if os.path.commonpath([str(sessions_root.resolve()), str(session_dir)]) != str(sessions_root.resolve()):
        return jsonify({"error": "access_denied"}), 403

    session_dir.mkdir(parents=True, exist_ok=True)
    state.active_workspace = str(session_dir)
    return jsonify({"ok": True, "workspace": state.active_workspace, "session": name})


@app.get("/api/sessions")
async def sessions_list():
    files = [f for f in os.listdir(SESSIONS_DIR) if f.endswith(".json")]
    return jsonify(sorted(files))


@app.get("/api/sessions/<path:name>")
async def sessions_get(name: str):
    fp = os.path.join(SESSIONS_DIR, secure_filename(name))
    if not os.path.exists(fp):
        return jsonify({"error": "not_found"}), 404
    with open(fp, "r", encoding="utf-8") as f:
        return jsonify(json.load(f))


@app.post("/api/sessions")
async def sessions_save():
    data = await request.get_json()
    data = data or {}
    name = secure_filename(data.get("name", "")).strip()
    if not name:
        return jsonify({"error": "missing_name"}), 400

    fp = os.path.join(SESSIONS_DIR, f"{name}.json")
    with open(fp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
    return jsonify({"ok": True, "file": fp}), 201


@app.delete("/api/sessions/<path:name>")
async def sessions_delete(name: str):
    fp = os.path.join(SESSIONS_DIR, secure_filename(name))
    if not os.path.exists(fp):
        return jsonify({"error": "not_found"}), 404
    os.remove(fp)
    return jsonify({"ok": True})


@app.post("/api/transcript/md")
async def export_md():
    payload = await request.get_json()
    payload = payload or {}
    transcript = payload.get("transcript") or []

    lines = ["# Transcript", ""]
    for item in transcript:
        ts = item.get("t")
        who = item.get("from")
        text = item.get("text", "")
        lines.append(f"**{who}** - {ts}")
        lines.append("")
        lines.append("```")
        lines.append(text)
        lines.append("```")
        lines.append("")

    md = "\n".join(lines)
    fn = f"transcript_{int(time.time())}.md"
    fp = _resolve_workspace_path(fn)
    with open(fp, "w", encoding="utf-8") as f:
        f.write(md)
    return jsonify({"ok": True, "file": f"/workspace/{fn}"})


@app.post("/api/transcript/html")
async def export_html():
    payload = await request.get_json()
    payload = payload or {}
    transcript = payload.get("transcript") or []

    parts = [
        "<!doctype html><meta charset='utf-8'><title>Transcript</title>",
        "<style>body{font-family:system-ui,Segoe UI,Arial;padding:20px;background:#0b1220;color:#e5e7eb}.b{background:#111827;border:1px solid #334155;border-radius:8px;padding:10px;margin:10px 0;white-space:pre-wrap}</style>",
        "<h1>Transcript</h1>",
    ]
    for item in transcript:
        who = item.get("from")
        text = (item.get("text") or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        parts.append(f"<div><strong>{who}</strong></div><div class='b'>{text}</div>")

    html = "\n".join(parts)
    fn = f"transcript_{int(time.time())}.html"
    fp = _resolve_workspace_path(fn)
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
