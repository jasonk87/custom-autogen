import asyncio
import base64
import html
import json
import os
import shutil
import time
import uuid
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
from app.core import TOOL_REFLECTION_GUIDANCE, run_orchestrator, OrchestratorError
from app.scenario import generate_agents_from_scenario, generate_scenario_idea
from app.state import ReplayEventBuffer, state
from app.tools import TOOLS
from app.utils import tree_listing
from app.model_providers import get_cloud_models, get_model_client, get_ollama_models, load_user_settings, save_user_settings


app_dir = os.path.dirname(os.path.abspath(__file__))
root_dir = os.path.dirname(app_dir)
app = Quart(
    __name__,
    template_folder=os.path.join(root_dir, "templates"),
    static_folder=os.path.join(root_dir, "static"),
)
SCENARIO_GENERATION_ATTEMPTS = 3
SCENARIO_GENERATION_RETRY_DELAY_SECONDS = 0.75
SCENARIO_IDEA_MODEL = "gemini::cloud::gemini-2.5-flash-lite"


def _is_production() -> bool:
    return os.environ.get("APP_ENV", "development").strip().lower() in {"prod", "production"}


if _is_production() and not (GEMINI_API_KEY or "").strip():
    raise RuntimeError("GEMINI_API_KEY is required when APP_ENV is production.")


def _workspace_root() -> str:
    return getattr(state, "active_workspace", WORKSPACE_DIR) or WORKSPACE_DIR


def _is_transient_provider_error(error: BaseException | str) -> bool:
    message = str(error).lower()
    return any(
        marker in message
        for marker in (
            "503",
            "unavailable",
            "high demand",
            "temporarily unavailable",
            "rate limit",
            "429",
        )
    )


def _resolve_workspace_path(path: str) -> str:
    base_path = os.path.realpath(_workspace_root())
    target_path = os.path.realpath(os.path.join(base_path, path))
    if os.path.commonpath([base_path, target_path]) != base_path:
        raise PermissionError("access_denied")
    return target_path


def _run_orchestrator_thread(goal, model, agents_cfg, manager_mode, out_q, max_turns, human_proxy_mode, human_proxy_preferences, temperature, allow_tools, conversation_mode="discussion"):
    try:
        asyncio.run(
            run_orchestrator(
                goal,
                model,
                agents_cfg,
                manager_mode,
                out_q,
                max_turns,
                human_proxy_mode,
                human_proxy_preferences,
                temperature,
                allow_tools,
                conversation_mode,
            )
        )
    except OrchestratorError as e:
        log.error("orchestrator_thread_known_error", extra={"error_code": e.error_code, "user_message": e.user_message, "developer_message": e.developer_message}, exc_info=e)
        out_q.put(json.dumps(e.to_payload()))
        out_q.put("[DONE]")
    except (asyncio.CancelledError, KeyboardInterrupt):
        log.info("orchestrator_thread_cancelled")
        out_q.put("[DONE]")
    except BaseException as e:
        log.error("orchestrator_thread_unexpected_error", extra={"error": str(e), "type": type(e).__name__}, exc_info=e)
        out_q.put(json.dumps(OrchestratorError("An unexpected error occurred in the orchestrator thread.", str(e)).to_payload()))
        out_q.put("[DONE]")
    finally:
        state.finish()


@app.get("/favicon.ico")
async def favicon():
    return "", 204


@app.get("/stream")
async def stream():
    model = request.args.get("model", DEFAULT_MODEL)
    goal_b64 = request.args.get("goal", "")
    agents_b64 = request.args.get("agents", "")
    manager_mode = request.args.get("manager_mode", "auto")
    conversation_mode = request.args.get("conversation_mode", "discussion")
    max_turns = int(request.args.get("turns", "60"))
    legacy_human_proxy = request.args.get("human_proxy", "false").lower() == "true"
    human_proxy_mode = request.args.get("human_proxy_mode", "consult" if legacy_human_proxy else "off")
    human_proxy_preferences = request.args.get("human_proxy_preferences", "")
    temperature = float(request.args.get("temperature", "0.3"))
    allow_tools = request.args.get("allow_tools", "true").lower() == "true"
    client_run_token = request.args.get("run_token", "").strip() or uuid.uuid4().hex
    resume_only = request.args.get("resume_only", "false").lower() == "true"

    try:
        goal = base64.b64decode(goal_b64.encode()).decode(errors="ignore") if goal_b64 else ""
    except Exception:
        goal = goal_b64

    try:
        agents_cfg = json.loads(base64.b64decode(agents_b64.encode()).decode(errors="ignore") or "[]")
    except Exception:
        agents_cfg = []

    out_q = state.reconnect_buffer(client_run_token)
    reconnecting = out_q is not None
    if out_q is None:
        if resume_only:
            payload = OrchestratorError(
                "The saved scenario is no longer available. Start a new task.",
                f"No buffered run exists for token {client_run_token}.",
                "run_not_found",
            ).to_payload()
            return jsonify(payload), 404
        out_q = ReplayEventBuffer()
        thread_args = (goal, model, agents_cfg, manager_mode, out_q, max_turns, human_proxy_mode, human_proxy_preferences, temperature, allow_tools, conversation_mode)
        try:
            state.start(_run_orchestrator_thread, thread_args, client_run_token, out_q)
        except RuntimeError as e:
            log.warning(
                "stream_start_rejected_active_run",
                extra={
                    "run_id": state.current_run_id,
                    "client_run_token": client_run_token,
                    "matches_active_token": client_run_token == state.current_client_run_token,
                },
            )
            payload = OrchestratorError(
                "A scenario is already running. Reconnect to it or stop it before starting another.",
                str(e),
                "run_already_active",
            ).to_payload()
            return jsonify(payload), 409

    try:
        last_event_id = int(request.headers.get("Last-Event-ID", "0") or "0")
    except ValueError:
        last_event_id = 0

    async def gen():
        cursor = max(last_event_id, 0)
        last_ping = time.time()
        log.info(
            "stream_reattached" if reconnecting else "stream_started",
            extra={
                "run_id": state.current_run_id,
                "client_run_token": client_run_token,
                "model": model,
                "goal_len": len(goal),
                "manager_mode": manager_mode,
                "conversation_mode": conversation_mode,
                "human_proxy_mode": human_proxy_mode,
            },
        )
        try:
            while True:
                try:
                    events = await asyncio.to_thread(out_q.read_after, cursor, 2.0)
                    if events:
                        for event_id, item in events:
                            yield f"id: {event_id}\ndata: {item}\n\n"
                            cursor = event_id
                            last_ping = time.time()
                            if item == "[DONE]":
                                log.info("stream_finished_cleanly", extra={"run_id": state.current_run_id})
                                return
                    now = time.time()
                    if now - last_ping > 2.0:
                        yield ": ping\n\n"
                        last_ping = now
                except Exception as e:
                    log.error("generator_loop_error", extra={"run_id": state.current_run_id, "error": str(e)})
                    break
        except GeneratorExit:
            log.info("stream_client_disconnected", extra={"run_id": state.current_run_id})
        except Exception as e:
            log.error("stream_error", extra={"run_id": state.current_run_id, "error": str(e)})
            yield f"data: {json.dumps({'type': 'status', 'state': 'error', 'user_message': 'Stream encountered an unexpected error.', 'message': str(e)})}\n\n"

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


@app.post("/api/run/resume")
async def resume_run():
    data = await request.get_json() or {}
    client_run_token = str(data.get("run_token") or "").strip()
    try:
        target, previous_args, out_q, transcript = state.prepare_resume(client_run_token)
    except RuntimeError as e:
        return jsonify(
            OrchestratorError(
                "The stopped simulation is not ready to resume. Try again shortly.",
                str(e),
                "run_not_resumable",
            ).to_payload()
        ), 409

    previous_goal = previous_args[0]
    continuation_goal = (
        f"{previous_goal}\n\n"
        "Resume the existing simulation from the transcript below. Continue the work naturally from "
        "the latest point. Do not restart the scenario, repeat introductions, or restate the original "
        "task as a new assignment.\n\n"
        f"Transcript before pause:\n{transcript or '(No prior chat messages were captured.)'}"
    )
    thread_args = (continuation_goal, *previous_args[1:4], out_q, *previous_args[5:])
    state.start(target, thread_args, client_run_token, out_q)
    out_q.put(json.dumps({"type": "chat", "sender": "System", "message": "Simulation resumed."}))
    return jsonify({"ok": True})


@app.get("/api/run/status")
async def run_status():
    client_run_token = request.args.get("run_token", "").strip() or None
    return jsonify(
        {
            "available": state.reconnect_buffer(client_run_token) is not None,
            "running": state.is_running,
            "resumable": state.resumable(client_run_token),
        }
    )


@app.post("/user_input")
async def user_input():
    data = await request.get_json()
    msg = (data or {}).get("message")
    try:
        state.user_input_q.put_nowait(msg)
    except Exception as e:
        log.error("user_input_queue_error", extra={"error": str(e), "type": type(e).__name__})
    return jsonify({"ok": True})


@app.post("/coach_input")
async def coach_input():
    data = await request.get_json()
    msg = (data or {}).get("message")
    if not isinstance(msg, str) or not msg.strip():
        return jsonify({"error": "missing_message"}), 400
    try:
        state.coach_input_q.put_nowait(msg.strip())
    except Exception as e:
        log.error("coach_input_queue_error", extra={"error": str(e), "type": type(e).__name__})
    return jsonify({"ok": True})


@app.post("/choose_next")
async def choose_next():
    data = await request.get_json()
    name = (data or {}).get("name")
    try:
        state.manual_next_q.put_nowait(name)
    except Exception as e:
        log.error("choose_next_queue_error", extra={"error": str(e), "type": type(e).__name__})
    return jsonify({"ok": True})


@app.post("/api/scenario/generate")
async def scenario_generate():
    data = await request.get_json()
    scenario = (data or {}).get("scenario")
    model = (data or {}).get("model") or DEFAULT_MODEL
    num_agents = (data or {}).get("num_agents", 3)
    conversation_mode = (data or {}).get("conversation_mode", "discussion")

    if not scenario or not isinstance(scenario, str):
        return jsonify(OrchestratorError("Missing or invalid 'scenario' in request.", "Scenario is missing or not a string.", "invalid_scenario_input").to_payload()), 400
    if not isinstance(model, str):
        return jsonify(OrchestratorError("Missing or invalid 'model' in request.", "Model is missing or not a string.", "invalid_model_input").to_payload()), 400
    if not isinstance(num_agents, int) or num_agents <= 0:
        return jsonify(OrchestratorError("Invalid 'num_agents' in request.", "num_agents must be a positive integer.", "invalid_num_agents_input").to_payload()), 400

    try:
        for attempt in range(1, SCENARIO_GENERATION_ATTEMPTS + 1):
            try:
                agents, suggested_goal, suggested_mode = await generate_agents_from_scenario(
                    scenario,
                    model,
                    num_agents,
                    conversation_mode,
                )
                return jsonify({"agents": agents, "goal": suggested_goal, "conversation_mode": suggested_mode})
            except Exception as e:
                if not _is_transient_provider_error(e) or attempt == SCENARIO_GENERATION_ATTEMPTS:
                    raise
                log.warning(
                    "scenario_generation_transient_error_retrying",
                    extra={"attempt": attempt, "max_attempts": SCENARIO_GENERATION_ATTEMPTS, "error": str(e)},
                )
                await asyncio.sleep(SCENARIO_GENERATION_RETRY_DELAY_SECONDS)
    except OrchestratorError as e:
        log.error("failed_to_generate_agents_known_error", extra={"error_code": e.error_code, "user_message": e.user_message, "developer_message": e.developer_message}, exc_info=e)
        return jsonify(e.to_payload()), 500
    except Exception as e:
        log.error("failed_to_generate_agents_unexpected_error", exc_info=e)
        error_message = str(e)
        if "API key expired" in error_message:
            return jsonify(
                OrchestratorError(
                    "Your Gemini API key has expired. Replace GEMINI_API_KEY in .env and restart the server.",
                    error_message,
                    "gemini_api_key_expired",
                ).to_payload()
            ), 500
        if "API_KEY_INVALID" in error_message:
            return jsonify(
                OrchestratorError(
                    "Your Gemini API key is invalid. Replace GEMINI_API_KEY in .env and restart the server.",
                    error_message,
                    "gemini_api_key_invalid",
                ).to_payload()
            ), 500
        if _is_transient_provider_error(e):
            return jsonify(
                OrchestratorError(
                    "The selected AI model is temporarily busy. Please try generating the scenario again shortly.",
                    error_message,
                    "model_temporarily_unavailable",
                ).to_payload()
            ), 503
        return jsonify(OrchestratorError("Failed to generate agents due to an unexpected error.", error_message).to_payload()), 500


@app.post("/api/scenario/idea")
async def scenario_idea():
    try:
        idea = await generate_scenario_idea(SCENARIO_IDEA_MODEL)
        return jsonify({"scenario": idea})
    except Exception as e:
        log.error("failed_to_generate_scenario_idea", extra={"error": str(e)}, exc_info=e)
        if _is_transient_provider_error(e):
            return jsonify(
                OrchestratorError(
                    "The idea generator is temporarily busy. Try the dice again shortly.",
                    str(e),
                    "model_temporarily_unavailable",
                ).to_payload()
            ), 503
        return jsonify(OrchestratorError("Failed to generate a scenario idea.", str(e)).to_payload()), 500


@app.route("/api/settings/ollama", methods=["GET", "POST"])
async def set_ollama():
    if request.method == "GET":
        settings = load_user_settings()
        return jsonify({
            "local_url": settings.get("ollama_local_url", "http://127.0.0.1:11434"),
            "remote_url": settings.get("ollama_remote_url", "http://192.168.86.30:11434")
        })

    data = await request.get_json() or {}
    local_url = data.get("local_url", "").strip()
    remote_url = data.get("remote_url", "").strip()
    
    updates = {}
    if local_url:
        updates["ollama_local_url"] = local_url
    if remote_url:
        updates["ollama_remote_url"] = remote_url
        
    save_user_settings(updates)
    return jsonify({"ok": True, "message": "Ollama settings saved."})


@app.get("/api/models")
async def api_models():
    return jsonify(get_cloud_models())


@app.get("/api/models/ollama")
async def api_ollama_models():
    return jsonify(await get_ollama_models())


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

    if not agent_config or not isinstance(agent_config, dict):
        return jsonify(OrchestratorError("Missing or invalid 'agent' configuration.", "Agent configuration is missing or not a dictionary.", "invalid_agent_config_input").to_payload()), 400
    if not message or not isinstance(message, str):
        return jsonify(OrchestratorError("Missing or invalid 'message' for agent run.", "Message is missing or not a string.", "invalid_message_input").to_payload()), 400
    if not model or not isinstance(model, str):
        return jsonify(OrchestratorError("Missing or invalid 'model' for agent run.", "Model is missing or not a string.", "invalid_model_input").to_payload()), 400

    if app.testing:
        return jsonify({"reply": f"Test reply: {message}"})

    try:
        model_client = get_model_client(model, temperature=0.3)

        selected_tool_names = agent_config.get("tools", [])
        selected_tools = [TOOLS[name] for name in selected_tool_names if name in TOOLS]

        agent = AssistantAgent(
            name=agent_config.get("name", "playground_agent"),
            model_client=model_client,
            system_message=agent_config.get("system_message", "You are a helpful assistant.") + TOOL_REFLECTION_GUIDANCE,
            model_client_stream=False,
            tools=selected_tools,
            reflect_on_tool_use=True,
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
    except OrchestratorError as e:
        log.error("agent_run_known_error", extra={"error_code": e.error_code, "user_message": e.user_message, "developer_message": e.developer_message}, exc_info=e)
        return jsonify(e.to_payload()), 500
    except Exception as e:
        log.error("agent_run_unexpected_error", extra={"error": str(e)}, exc_info=e)
        return jsonify(OrchestratorError("Failed to run agent due to an unexpected error.", str(e)).to_payload()), 500


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
        return jsonify(OrchestratorError("No file provided for upload.", "File object is missing from request.", "missing_file").to_payload()), 400

    blob = f.read()
    size = len(blob)
    if size > MAX_UPLOAD_BYTES:
        return jsonify(OrchestratorError(f"File too large. Maximum allowed is {MAX_UPLOAD_BYTES} bytes.", f"Uploaded file size {size} exceeds max {MAX_UPLOAD_BYTES}.", "too_large").to_payload()), 413

    mime = f.mimetype or "application/octet-stream"
    if mime not in ALLOWED_UPLOAD_MIMES and not secure_filename(f.filename).endswith((".txt", ".md", ".json", ".py")):
        return jsonify(OrchestratorError(f"File type '{mime}' is not allowed.", f"MIME type {mime} is not in ALLOWED_UPLOAD_MIMES and filename extension is not allowed.", "mime_blocked").to_payload()), 415

    fn = secure_filename(f.filename)
    if not fn:
        return jsonify(OrchestratorError("Uploaded file must have a valid filename.", "Filename is empty after sanitization.", "invalid_filename").to_payload()), 400
    try:
        fp = _resolve_workspace_path(fn)
    except PermissionError:
        return jsonify(OrchestratorError("Access denied to workspace path.", f"Attempted to resolve path {fn} outside of workspace root.", "access_denied").to_payload()), 403

    try:
        os.makedirs(os.path.dirname(fp), exist_ok=True)
        with open(fp, "wb") as dest:
            dest.write(blob)
    except OSError as e:
        log.error("file_upload_os_error", extra={"upload_name": fn, "error": str(e)}, exc_info=e)
        return jsonify(OrchestratorError("Failed to save file due to a server error.", str(e), "file_save_error").to_payload()), 500

    return jsonify({"ok": True, "file": fn, "bytes": size})


@app.get("/workspace/<path:fn>")
async def ws_file(fn: str):
    root = _workspace_root()
    try:
        _resolve_workspace_path(fn)
    except PermissionError:
        return jsonify(OrchestratorError("Access denied to workspace path.", f"Attempted to access path {fn} outside of workspace root.", "access_denied").to_payload()), 403
    return await send_from_directory(root, fn)


@app.delete("/api/workspace/delete")
async def api_delete_file():
    data = await request.get_json()
    path = (data or {}).get("path")
    if not path:
        return jsonify(OrchestratorError("Missing 'path' for file deletion.", "Path is missing from request.", "missing_path").to_payload()), 400

    try:
        target_path = _resolve_workspace_path(path)
    except PermissionError:
        return jsonify(OrchestratorError("Access denied to workspace path.", f"Attempted to delete path {path} outside of workspace root.", "access_denied").to_payload()), 403
    if target_path == os.path.realpath(_workspace_root()):
        return jsonify(OrchestratorError("Refusing to delete the workspace root.", f"Attempted to delete workspace root {target_path}.", "workspace_root_delete_blocked").to_payload()), 400

    try:
        if os.path.isdir(target_path):
            shutil.rmtree(target_path)
        else:
            os.remove(target_path)
        return jsonify({"ok": True})
    except FileNotFoundError:
        return jsonify(OrchestratorError(f"File or directory at '{path}' not found.", f"Path {target_path} not found.", "not_found").to_payload()), 404
    except OSError as e:
        log.error("file_delete_os_error", extra={"path": path, "error": str(e)}, exc_info=e)
        return jsonify(OrchestratorError("Failed to delete file or directory due to a server error.", str(e), "file_delete_error").to_payload()), 500


@app.post("/api/workspace/set_directory")
async def api_set_directory():
    data = await request.get_json()
    path = (data or {}).get("path")
    if not path:
        return jsonify(OrchestratorError("Missing 'path' for setting workspace directory.", "Path is missing from request.", "missing_path").to_payload()), 400

    target_path = os.path.abspath(path)
    if not os.path.exists(target_path):
        return jsonify(OrchestratorError(f"Path '{path}' does not exist.", f"Path {target_path} does not exist.", "not_found").to_payload()), 404
    if not os.path.isdir(target_path):
        return jsonify(OrchestratorError(f"Path '{path}' is not a directory.", f"Path {target_path} is not a directory.", "not_a_directory").to_payload()), 400

    state.active_workspace = target_path
    return jsonify({"ok": True, "workspace": target_path})


@app.post("/api/workspace/new_folder")
async def api_new_folder():
    data = await request.get_json()
    path = (data or {}).get("path")
    if not path:
        return jsonify(OrchestratorError("Missing 'path' for new folder creation.", "Path is missing from request.", "missing_path").to_payload()), 400

    try:
        target_path = _resolve_workspace_path(path)
    except PermissionError:
        return jsonify(OrchestratorError("Access denied to workspace path.", f"Attempted to create folder at {path} outside of workspace root.", "access_denied").to_payload()), 403

    try:
        os.makedirs(target_path, exist_ok=True)
        return jsonify({"ok": True})
    except OSError as e:
        log.error("new_folder_os_error", extra={"path": path, "error": str(e)}, exc_info=e)
        return jsonify(OrchestratorError("Failed to create folder due to a server error.", str(e), "folder_creation_error").to_payload()), 500


@app.post("/api/workspace/file")
async def api_save_file():
    data = await request.get_json()
    path = (data or {}).get("path")
    content = (data or {}).get("content")
    if path is None or not isinstance(path, str) or not path.strip():
        return jsonify(OrchestratorError("Missing or invalid 'path' for file save.", "Path is missing or not a string.", "missing_path").to_payload()), 400
    if content is None or not isinstance(content, str):
        return jsonify(OrchestratorError("Missing or invalid 'content' for file save.", "Content is missing or not a string.", "missing_content").to_payload()), 400

    try:
        target_path = _resolve_workspace_path(path)
    except PermissionError:
        return jsonify(OrchestratorError("Access denied to workspace path.", f"Attempted to save file at {path} outside of workspace root.", "access_denied").to_payload()), 403

    try:
        os.makedirs(os.path.dirname(target_path), exist_ok=True)
        with open(target_path, "w", encoding="utf-8") as f:
            f.write(content)
        return jsonify({"ok": True})
    except Exception as e:
        log.error("file_save_error", extra={"path": path, "error": str(e)}, exc_info=e)
        return jsonify(OrchestratorError("Failed to save file due to a server error.", str(e), "file_save_error").to_payload()), 500


@app.get("/api/workspace/file")
async def api_get_file():
    path = request.args.get("path")
    if not path or not isinstance(path, str) or not path.strip():
        return jsonify(OrchestratorError("Missing or invalid 'path' for file retrieval.", "Path is missing or not a string.", "missing_path").to_payload()), 400

    try:
        target_path = _resolve_workspace_path(path)
    except PermissionError:
        return jsonify(OrchestratorError("Access denied to workspace path.", f"Attempted to read file at {path} outside of workspace root.", "access_denied").to_payload()), 403

    try:
        with open(target_path, "r", encoding="utf-8") as f:
            content = f.read()
        return Response(content, mimetype="text/plain")
    except FileNotFoundError:
        return jsonify(OrchestratorError(f"File at '{path}' not found.", f"Path {target_path} not found.", "not_found").to_payload()), 404
    except Exception as e:
        log.error("file_read_error", extra={"path": path, "error": str(e)}, exc_info=e)
        return jsonify(OrchestratorError("Failed to read file due to a server error.", str(e), "file_read_error").to_payload()), 500


@app.post("/api/workspace/rename")
async def api_rename_file():
    data = await request.get_json()
    old_path = (data or {}).get("old_path")
    new_path = (data or {}).get("new_path")
    if not old_path or not isinstance(old_path, str) or not old_path.strip():
        return jsonify(OrchestratorError("Missing or invalid 'old_path' for file rename.", "old_path is missing or not a string.", "missing_old_path").to_payload()), 400
    if not new_path or not isinstance(new_path, str) or not new_path.strip():
        return jsonify(OrchestratorError("Missing or invalid 'new_path' for file rename.", "new_path is missing or not a string.", "missing_new_path").to_payload()), 400

    try:
        old_target_path = _resolve_workspace_path(old_path)
        new_target_path = _resolve_workspace_path(new_path)
    except PermissionError:
        return jsonify(OrchestratorError("Access denied to workspace path.", f"Attempted to rename file from {old_path} to {new_path} outside of workspace root.", "access_denied").to_payload()), 403

    try:
        os.makedirs(os.path.dirname(new_target_path), exist_ok=True)
        os.rename(old_target_path, new_target_path)
        return jsonify({"ok": True})
    except FileNotFoundError:
        return jsonify(OrchestratorError(f"File or directory at '{old_path}' not found.", f"Path {old_target_path} not found.", "not_found").to_payload()), 404
    except OSError as e:
        log.error("file_rename_os_error", extra={"old_path": old_path, "new_path": new_path, "error": str(e)}, exc_info=e)
        return jsonify(OrchestratorError("Failed to rename file or directory due to a server error.", str(e), "file_rename_error").to_payload()), 500


@app.post("/api/sessions/activate")
async def sessions_activate():
    data = await request.get_json()
    raw_name = (data or {}).get("name", "")
    name = secure_filename(raw_name).strip()
    if not name:
        return jsonify(OrchestratorError("Missing 'name' for session activation.", "Session name is missing or empty.", "missing_session_name").to_payload()), 400

    sessions_root = Path(WORKSPACE_DIR) / "sessions"
    session_dir = (sessions_root / name).resolve()
    if os.path.commonpath([str(sessions_root.resolve()), str(session_dir)]) != str(sessions_root.resolve()):
        return jsonify(OrchestratorError("Access denied to session path.", f"Attempted to activate session at {session_dir} outside of sessions root.", "access_denied").to_payload()), 403

    try:
        sessions_root.mkdir(parents=True, exist_ok=True)
        session_dir.mkdir(parents=True, exist_ok=True)
        state.active_workspace = str(session_dir)
        return jsonify({"ok": True, "workspace": state.active_workspace, "session": name})
    except OSError as e:
        log.error("session_activate_os_error", extra={"session_name": name, "error": str(e)}, exc_info=e)
        return jsonify(OrchestratorError("Failed to activate session due to a server error.", str(e), "session_activation_error").to_payload()), 500


@app.get("/api/sessions")
async def sessions_list():
    try:
        files = [f for f in os.listdir(SESSIONS_DIR) if f.endswith(".json")]
        return jsonify(sorted(files))
    except FileNotFoundError:
        return jsonify(OrchestratorError("Sessions directory not found.", f"Sessions directory {SESSIONS_DIR} does not exist.", "sessions_dir_not_found").to_payload()), 404
    except Exception as e:
        log.error("sessions_list_error", extra={"error": str(e)}, exc_info=e)
        return jsonify(OrchestratorError("Failed to list sessions due to a server error.", str(e), "sessions_list_error").to_payload()), 500


@app.get("/api/sessions/<path:name>")
async def sessions_get(name: str):
    fp = os.path.join(SESSIONS_DIR, secure_filename(name))
    if not os.path.exists(fp):
        return jsonify(OrchestratorError(f"Session '{name}' not found.", f"Session file {fp} not found.", "session_not_found").to_payload()), 404
    try:
        with open(fp, "r", encoding="utf-8") as f:
            return jsonify(json.load(f))
    except json.JSONDecodeError as e:
        log.error("session_get_json_error", extra={"session_name": name, "error": str(e)}, exc_info=e)
        return jsonify(OrchestratorError(f"Failed to read session '{name}': invalid format.", str(e), "invalid_session_format").to_payload()), 500
    except Exception as e:
        log.error("session_get_error", extra={"session_name": name, "error": str(e)}, exc_info=e)
        return jsonify(OrchestratorError(f"Failed to retrieve session '{name}' due to a server error.", str(e), "session_retrieval_error").to_payload()), 500


@app.post("/api/sessions")
async def sessions_save():
    data = await request.get_json()
    data = data or {}
    name = secure_filename(data.get("name", "")).strip()
    if not name:
        return jsonify(OrchestratorError("Missing 'name' for session save.", "Session name is missing or empty.", "missing_session_name").to_payload()), 400

    artifact_type = data.get("artifact_type")
    if artifact_type not in {None, "scenario", "group"}:
        return jsonify(OrchestratorError("Invalid saved item type.", f"Unsupported artifact_type: {artifact_type}", "invalid_artifact_type").to_payload()), 400

    filename = f"{artifact_type}_{name}.json" if artifact_type else f"{name}.json"
    fp = os.path.join(SESSIONS_DIR, filename)
    try:
        os.makedirs(SESSIONS_DIR, exist_ok=True)
        with open(fp, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        return jsonify({"ok": True, "file": fp}), 201
    except Exception as e:
        log.error("session_save_error", extra={"session_name": name, "error": str(e)}, exc_info=e)
        return jsonify(OrchestratorError(f"Failed to save session '{name}' due to a server error.", str(e), "session_save_error").to_payload()), 500


@app.delete("/api/sessions/<path:name>")
async def sessions_delete(name: str):
    fp = os.path.join(SESSIONS_DIR, secure_filename(name))
    if not os.path.exists(fp):
        return jsonify(OrchestratorError(f"Session '{name}' not found.", f"Session file {fp} not found.", "session_not_found").to_payload()), 404
    try:
        os.remove(fp)
        return jsonify({"ok": True})
    except Exception as e:
        log.error("session_delete_error", extra={"session_name": name, "error": str(e)}, exc_info=e)
        return jsonify(OrchestratorError(f"Failed to delete session '{name}' due to a server error.", str(e), "session_delete_error").to_payload()), 500


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
    try:
        fp = _resolve_workspace_path(fn)
    except PermissionError:
        return jsonify(OrchestratorError("Access denied to workspace path for transcript export.", f"Attempted to export transcript to {fn} outside of workspace root.", "access_denied").to_payload()), 403

    try:
        with open(fp, "w", encoding="utf-8") as f:
            f.write(md)
        return jsonify({"ok": True, "file": f"/workspace/{fn}"})
    except Exception as e:
        log.error("export_md_error", extra={"export_name": fn, "error": str(e)}, exc_info=e)
        return jsonify(OrchestratorError("Failed to export transcript as Markdown due to a server error.", str(e), "export_md_error").to_payload()), 500


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
        who = html.escape(str(item.get("from") or ""))
        text = (item.get("text") or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        parts.append(f"<div><strong>{who}</strong></div><div class='b'>{text}</div>")

    html_content = "\n".join(parts)
    fn = f"transcript_{int(time.time())}.html"
    try:
        fp = _resolve_workspace_path(fn)
    except PermissionError:
        return jsonify(OrchestratorError("Access denied to workspace path for transcript export.", f"Attempted to export transcript to {fn} outside of workspace root.", "access_denied").to_payload()), 403

    try:
        with open(fp, "w", encoding="utf-8") as f:
            f.write(html_content)
        return jsonify({"ok": True, "file": f"/workspace/{fn}"})
    except Exception as e:
        log.error("export_html_error", extra={"export_name": fn, "error": str(e)}, exc_info=e)
        return jsonify(OrchestratorError("Failed to export transcript as HTML due to a server error.", str(e), "export_html_error").to_payload()), 500


@app.get("/")
async def index():
    html = await render_template("index.html")
    headers = {
        "Cache-Control": "no-cache, no-store, must-revalidate",
        "Pragma": "no-cache",
        "Expires": "0",
    }
    return Response(html, mimetype="text/html", headers=headers)
