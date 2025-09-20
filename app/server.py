import base64
import io
import json
import os
import queue
import time
from typing import Any, Dict, List, Optional, Tuple
from typing import Any, Dict

import httpx
from flask import Flask, Response, jsonify, request, send_from_directory
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
from app.state import state
from app.utils import tree_listing

app = Flask(__name__)

_models_cache: Dict[str, Any] = {"ts": 0.0, "names": []}


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


HTML = r"""
<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>Custom Agent Studio — v10</title>
  <style>
    :root{--bg:#0f172a;--panel:#1f2937;--muted:#9ca3af;--text:#e5e7eb;--border:#374151;--chip:#334155;--blue:#2563eb;--blue2:#1d4ed8;--red:#ef4444;--green:#10b981;--amber:#f59e0b}
    *{box-sizing:border-box} body{margin:0;background:#0b1220;color:var(--text);font:14px/1.4 system-ui,Segoe UI,Inter,Arial}
    .app{display:flex;height:100vh}
    .side{width:34%;background:var(--panel);border-right:1px solid var(--border);display:flex;flex-direction:column;padding:18px}
    header{display:flex;align-items:center;justify-content:space-between;margin-bottom:12px}
    h1{font-size:18px;margin:0}
    .tabs{display:flex;border-bottom:1px solid var(--border);margin-bottom:10px}
    .tab{flex:1;padding:10px 8px;border:none;background:transparent;color:#cbd5e1;font-weight:600;cursor:pointer;transition:150ms}
    .tab.active{background:var(--chip);color:white;border-radius:6px 6px 0 0}
    .group{margin:8px 0}
    label{display:block;margin-bottom:4px;color:#cbd5e1;font-size:12px}
    input,select,textarea{width:100%;background:#111827;color:#e5e7eb;border:1px solid #334155;border-radius:8px;padding:8px}
    textarea{min-height:70px}
    .row{display:grid;grid-template-columns:1fr 1fr;gap:10px}
    .card{background:#0f172a;border:1px solid var(--border);border-radius:10px;padding:10px}
    .btn{display:inline-flex;align-items:center;justify-content:center;border:none;border-radius:10px;padding:10px 12px;font-weight:600;color:white;cursor:pointer;transition:150ms}
    .btn:disabled{opacity:.5;cursor:not-allowed}
    .btn-primary{background:var(--blue)} .btn-primary:hover{background:var(--blue2)}
    .btn-danger{background:var(--red)} .btn-success{background:var(--green)} .btn-neutral{background:#374151}
    .team-item{display:flex;gap:6px;align-items:center;background:#0b1220;border:1px solid #2b3443;border-radius:8px;padding:6px}
    .team-item .name{font-weight:700} .team-item .sys{flex:1} .team-item .num{width:70px}
    .main{flex:1;display:flex;flex-direction:column;background:#0b1220;position:relative}
    #chat{flex:1;overflow:auto;padding:18px}
    .bubble{max-width:90%;white-space:pre-wrap;border-radius:12px;padding:10px;margin:8px 0;box-shadow:0 4px 10px rgba(0,0,0,.15)}
    .from-you{background:#1d4ed8;margin-left:auto} .from-them{background:#1f2937}
    .who{font-size:11px;opacity:.9;margin-bottom:4px}
    #status{padding:10px;text-align:center;border-top:1px solid var(--border);background:var(--panel)}
    #fbform{display:flex;gap:8px;border-top:1px solid var(--border);padding:10px;background:var(--panel)}
    .hidden{display:none}
    .pill{display:inline-block;background:#0b213f;border:1px solid #23406e;color:#cfe4ff;border-radius:999px;padding:4px 8px;font-size:12px}
    .row2{display:grid;grid-template-columns:1fr auto;gap:10px;align-items:end}
    .tree ul{list-style:none;padding-left:14px} .tree li{margin:4px 0}
    #thinkdock{position:absolute;right:16px;bottom:72px;max-width:420px;width:min(42vw,420px);background:#0b213f;border:1px solid #23406e;border-radius:12px;box-shadow:0 10px 25px rgba(0,0,0,.35);display:none}
    #thinkhdr{display:flex;align-items:center;justify-content:space-between;padding:8px 10px;border-bottom:1px solid #1f3a63}
    #thinkhdr .title{font-weight:700;color:#cfe4ff}
    #thinkbody{max-height:40vh;overflow:auto;padding:10px;white-space:pre-wrap;color:#e0edff}
    #thinkhdr .chip{font-size:12px;background:#092245;color:#93c5fd;border:1px solid #1e40af;padding:2px 6px;border-radius:999px}
    #thinkhdr button{background:#0e1a2b;border:1px solid #2a4066;color:#cfe4ff;border-radius:8px;padding:4px 6px;cursor:pointer}
  </style>
</head>
<body>
  <div class="app">
    <aside class="side">
      <header>
        <h1>Custom Agent Studio</h1>
        <div style="display:flex;gap:8px">
          <button id="sessions" class="btn btn-neutral">Sessions</button>
        </div>
      </header>
      <div class="tabs">
        <button id="tab-setup" class="tab active">Setup</button>
        <button id="tab-work" class="tab">Workspace</button>
        <button id="tab-settings" class="tab">Settings</button>
      </div>
      <section id="setup">
        <div class="group">
          <label>Ollama Model <span id="model-count" class="pill"></span></label>
          <div class="row2">
            <select id="model"></select>
            <button id="reloadModels" class="btn btn-neutral">Reload</button>
          </div>
          <div id="model-hint" style="color:#9ca3af;font-size:12px;margin-top:4px"></div>
        </div>
        <div class="row">
          <div class="group">
            <label>Manager Mode</label>
            <select id="mode"><option>Directed</option><option>RoundRobin</option><option>Manual</option></select>
          </div>
          <div class="group">
            <label>Max Turns</label>
            <input id="turns" type="number" min="1" max="200" value="60" />
          </div>
        </div>
        <div class="group">
          <label>Team Goal</label>
          <textarea id="goal" placeholder="e.g. Build a Flask app and write files to workspace"></textarea>
        </div>
        <div class="card">
          <div style="font-weight:700;margin-bottom:6px">Add Agent</div>
          <input id="aname" placeholder="Name (e.g. Programmer)" style="margin-bottom:6px" />
          <textarea id="asys" placeholder="Optional system message" style="margin-bottom:6px"></textarea>
          <div style="display:flex;gap:8px;align-items:center">
            <label style="font-size:12px">Temp</label>
            <input id="atemp" class="num" type="number" min="0" max="2" step="0.1" value="0.3" />
            <button id="add" class="btn btn-primary" style="margin-left:auto">Add</button>
          </div>
        </div>
        <div class="group">
          <div style="font-weight:700;margin-bottom:6px">Team</div>
          <div id="team"></div>
        </div>
        <div class="group" id="actions"></div>
        <div class="group">
          <button id="export" class="btn btn-neutral" style="width:100%">Export Transcript</button>
        </div>
      </section>
      <section id="work" class="hidden">
        <div class="group" style="display:flex;gap:8px;align-items:center">
          <input id="file" type="file" />
          <button id="upload" class="btn btn-neutral">Upload</button>
          <button id="refresh" class="btn btn-neutral">Refresh</button>
        </div>
        <div class="tree" id="tree"></div>
        <div id="files" style="font-size:13px;margin-top:8px"></div>
      </section>
      <section id="settings" class="hidden">
        <div class="group">
          <label>Ollama Base URL</label>
          <div class="row2">
            <input id="ollamaBase" value="http://192.168.86.30:11434" />
            <button id="saveBase" class="btn btn-primary">Save</button>
          </div>
          <div id="baseHint" style="color:#9ca3af;font-size:12px;margin-top:4px">Example: http://192.168.86.30:11434</div>
        </div>
        <div class="group">
          <button id="probe" class="btn btn-neutral">Probe Ollama</button>
          <span id="probeHint" style="margin-left:8px;color:#9ca3af"></span>
        </div>
      </section>
    </aside>
    <main class="main">
      <div id="chat"></div>
      <div id="thinkdock">
        <div id="thinkhdr">
          <span class="title">Thinking</span>
          <div style="display:flex;gap:6px;align-items:center">
            <span id="thinklen" class="chip">0 chars</span>
            <button id="thinkmin">Minimize</button>
            <button id="thinkclose">Close</button>
          </div>
        </div>
        <div id="thinkbody"></div>
      </div>
      <div id="status">Status: Idle</div>
      <form id="fbform">
        <input id="fb" placeholder="Type real-time feedback or ```python code```…" disabled />
        <button id="send" class="btn btn-primary" disabled>Send</button>
      </form>
    </main>
  </div>
<script>
  const $=(s)=>document.querySelector(s); const $$=(s)=>Array.from(document.querySelectorAll(s));
  let agents=[]; let streams={}; let es=null; let manualNames=[]; let lastModels=[];
  const think = { active:false, buffer:'', open:false, minimized:false };
  function toast(msg){ const t=document.createElement('div'); t.textContent=msg; t.style.cssText='position:fixed;right:12px;bottom:12px;background:#0b213f;color:#cfe4ff;border:1px solid #23406e;padding:10px 12px;border-radius:10px;box-shadow:0 10px 25px rgba(0,0,0,.35)'; document.body.appendChild(t); setTimeout(()=>t.remove(),3000); }
  function setStatus(state){ const S=$('#status'); const map={idle:'Status: Idle', running:'Status: Agents working…', waiting_for_input:'Status: Waiting for input…'}; S.textContent = map[state]||'Status'; $('#fb').disabled = state!=='waiting_for_input'; $('#send').disabled = state!=='waiting_for_input'; }
  function addMsg(sender, text, mine=false, id=null){ const box=document.createElement('div'); box.className='bubble '+(mine?'from-you':'from-them'); const who=document.createElement('div'); who.className='who'; who.textContent=mine?'You':sender; const body=document.createElement('div'); body.textContent=text||''; box.append(who,body); $('#chat').append(box); $('#chat').scrollTop=$('#chat').scrollHeight; if(id) streams[id]={el:body, thinkPhase:false}; }
  function appendToken(id, delta){ ensureThinkHandling(id, delta); const s = streams[id]; if(!s){ addMsg('assistant','',false,id); } const body = streams[id].el; if(!streams[id].thinkPhase){ body.textContent += stripThinkTags(delta); } $('#chat').scrollTop=$('#chat').scrollHeight; }
  function ensureThinkHandling(id, chunk){
    let c = chunk;
    if(c.includes('<think>') || think.active){
      if(!think.open){ openThinkDock(); }
      while(c.length){
        if(!think.active){
          const i = c.indexOf('<think>');
          if(i===-1){
            if(think.open && think.buffer.length>0){ collapseThinkDock(); }
            break;
          }
          c = c.slice(i+7);
          think.active = true; streams[id] && (streams[id].thinkPhase=true);
        } else {
          const j = c.indexOf('</think>');
          if(j===-1){ think.buffer += c; c=''; break; }
          else {
            think.buffer += c.slice(0,j);
            c = c.slice(j+8);
            think.active = false; streams[id] && (streams[id].thinkPhase=false);
            renderThink();
            if(c.trim().length>0){ collapseThinkDock(); }
          }
        }
      }
      renderThink();
    }
  }
  function stripThinkTags(s){ return s.replaceAll('<think>','').replaceAll('</think>',''); }
  function openThinkDock(){ if(think.open) return; think.open=true; think.minimized=false; $('#thinkdock').style.display='block'; renderThink(); }
  function collapseThinkDock(){ if(!think.open) return; think.minimized=true; $('#thinkbody').style.display='none'; $('#thinkmin').textContent='Expand'; }
  function expandThinkDock(){ if(!think.open) return; think.minimized=false; $('#thinkbody').style.display='block'; $('#thinkmin').textContent='Minimize'; }
  function closeThinkDock(){ think.open=false; think.active=false; think.buffer=''; $('#thinkdock').style.display='none'; renderThink(); }
  function renderThink(){ if(!think.open){ return; } const body=$('#thinkbody'); body.textContent=think.buffer||''; $('#thinklen').textContent=(think.buffer||'').length+' chars'; if(think.minimized){ body.style.display='none'; } else { body.style.display='block'; } }
  $('#thinkmin').onclick=()=>{ if(think.minimized) expandThinkDock(); else collapseThinkDock(); };
  $('#thinkclose').onclick=()=>{ closeThinkDock(); };
function renderTeam(){ const T=$('#team'); if(!agents.length){T.innerHTML='<div style="color:#9ca3af">Add at least two agents.</div>';return} T.innerHTML=agents.map((a,i)=>`<div class='team-item'> <input data-i='${i}' class='name' value='${a.name}' style='width:120px' /> <input data-i='${i}' class='sys' value='${a.system||""}' placeholder='system message' /> <input data-i='${i}' class='num' type='number' step='0.1' min='0' max='2' value='${a.temperature??0.3}' /> <button data-i='${i}' class='btn btn-neutral up'>▲</button> <button data-i='${i}' class='btn btn-neutral down'>▼</button> <button data-i='${i}' class='btn btn-danger rm'>✕</button> </div>`).join(''); $$('.name').forEach(e=>e.onchange=()=>{agents[e.dataset.i].name=e.value;saveSession();}); $$('.sys').forEach(e=>e.onchange=()=>{agents[e.dataset.i].system=e.value;saveSession();}); $$('.num').forEach(e=>e.onchange=()=>{agents[e.dataset.i].temperature=parseFloat(e.value||'0.3');saveSession();}); $$('.rm').forEach(e=>e.onclick=()=>{agents.splice(+e.dataset.i,1); renderTeam(); saveSession();}); $$('.up').forEach(e=>e.onclick=()=>{const i=+e.dataset.i; if(i>0){[agents[i-1],agents[i]]=[agents[i],agents[i-1]]; renderTeam(); saveSession();}}); $$('.down').forEach(e=>e.onclick=()=>{const i=+e.dataset.i; if(i<agents.length-1){[agents[i+1],agents[i]]=[agents[i],agents[i+1]]; renderTeam(); saveSession();}}); }
  function buttons(running){
    const A=$('#actions');
    if(running){
      let manual = $('#mode').value==='Manual';
      A.innerHTML = `<div style='display:grid;grid-template-columns:${manual?'1fr 1fr':'1fr'};gap:8px'> <button id='stop' class='btn btn-danger'>Stop</button> ${manual?"<div style='display:flex;gap:6px'><select id='manual-chooser' class='sys'></select><button id='step2' class='btn btn-success'>Step</button></div>":""} </div>`;
      $('#stop').onclick=async()=>{ if(es) es.close(); await fetch('/stop',{method:'POST'}); setStatus('idle'); buttons(false); addMsg('System','Task aborted',true); closeThinkDock(); };
      if(manual){
        const sel=$('#manual-chooser'); sel.innerHTML = manualNames.map(n=>`<option>${n}</option>`).join('');
        $('#step2').onclick=()=>fetch('/choose_next',{method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({name: sel.value})});
      }
    } else {
      A.innerHTML = `<button id='start' class='btn btn-primary' style='width:100%'>Start</button>`;
      $('#start').onclick=()=>{ if(agents.length<2){ alert('Add at least two agents'); return; } startRun(); };
    }
  }
  async function startRun(){
    if(es) es.close();
    $('#chat').innerHTML=''; closeThinkDock(); streams={};
    addMsg('System','Task started'); setStatus('running'); buttons(true);
    manualNames = agents.map(a=>a.name).concat(['Human_Admin']);
    const payload = new URLSearchParams({ model: $('#model').value, goal: btoa($('#goal').value||''), agents: btoa(JSON.stringify(agents)), manager_mode: $('#mode').value, turns: ($('#turns').value||'') });
    es = new EventSource('/stream?'+payload.toString());
    es.onmessage = (ev)=>{
      if(ev.data==='[DONE]'){ es.close(); setStatus('idle'); buttons(false); addMsg('System','Task complete'); return; }
      try{
        const d = JSON.parse(ev.data);
        if(d.type==='status'){ setStatus(d.state); }
        else if(d.type==='chat'){ addMsg(d.sender, d.message, false); }
        else if(d.type==='stream_start'){ addMsg(d.sender,'',false,d.id); }
        else if(d.type==='token'){ appendToken(d.id, d.delta); }
        else if(d.type==='stream_end'){ /* no-op */ }
      }catch(e){ /* keepalive ping or partial */ }
    };
    es.onerror = ()=>{ addMsg('Error','Connection lost', true); try{es.close();}catch{} setStatus('idle'); buttons(false); };
  }
  $('#tab-setup').onclick=()=>{ $('#tab-setup').classList.add('active'); $('#tab-work').classList.remove('active'); $('#tab-settings').classList.remove('active'); $('#setup').classList.remove('hidden'); $('#work').classList.add('hidden'); $('#settings').classList.add('hidden'); };
  $('#tab-work').onclick=()=>{ $('#tab-work').classList.add('active'); $('#tab-setup').classList.remove('active'); $('#tab-settings').classList.remove('active'); $('#work').classList.remove('hidden'); $('#setup').classList.add('hidden'); $('#settings').classList.add('hidden'); refreshFiles(); refreshTree(); };
  $('#tab-settings').onclick=()=>{ $('#tab-settings').classList.add('active'); $('#tab-setup').classList.remove('active'); $('#tab-work').classList.remove('active'); $('#settings').classList.remove('hidden'); $('#setup').classList.add('hidden'); $('#work').classList.add('hidden'); };
  $('#add').onclick=()=>{const n=$('#aname').value.trim(); if(!n) return; agents.push({name:n, system:$('#asys').value.trim(), temperature:parseFloat($('#atemp').value)||0.3}); $('#aname').value=''; $('#asys').value=''; renderTeam(); saveSession();};
  $('#fbform').onsubmit=async(e)=>{ e.preventDefault(); const v=$('#fb').value.trim(); if(!v) return; addMsg('You', v, true); await fetch('/user_input',{method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({message:v})}); $('#fb').value=''; setStatus('running'); };
  async function refreshFiles(){ const r=await fetch('/api/workspace/files'); const d=await r.json(); const F=$('#files'); F.innerHTML = d.length? d.map(x=>`<a style='display:block;padding:6px;border:1px solid #2b3443;border-radius:8px;margin:4px 0;background:#111827' target='_blank' href='${x.path}'>${x.name}</a>`).join('') : '<div style="color:#9ca3af">No files yet.</div>'; }
  function renderTreeNode(node){ if(node.type==='file'){ return `<li><a target=\"_blank\" href=\"/workspace/${node.path}\">${node.name}</a></li>`; } let kids=''; if(Array.isArray(node.children)){ kids = '<ul>'+node.children.map(renderTreeNode).join('')+'</ul>'; } return `<li>${node.name}${kids}</li>`; }
  async function refreshTree(){ const r=await fetch('/api/workspace/tree'); const d=await r.json(); $('#tree').innerHTML = '<ul>'+renderTreeNode(d)+'</ul>'; }
  $('#upload').onclick=async()=>{ const f=$('#file').files[0]; if(!f) return; const fd=new FormData(); fd.append('file', f); const r = await fetch('/api/workspace/upload',{method:'POST',body:fd}); if(!r.ok){ const e=await r.json(); toast('Upload failed: '+(e.error||e.message)); } else { toast('Uploaded'); refreshFiles(); refreshTree(); }};
  $('#refresh').onclick=()=>{ refreshFiles(); refreshTree(); };
  $('#sessions').onclick=async()=>{ const name=prompt('Session name to save/load (leave blank to load list)'); if(name){ saveSession(); await fetch('/api/sessions',{method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({name, model:$('#model').value, goal:$('#goal').value, agents})}); toast('Saved.'); } else { const r=await fetch('/api/sessions'); const list=await r.json(); const pick=prompt('Available:\\n'+list.join('\\n')+'\\n\\nType a name to load:'); if(pick){ const d=await (await fetch('/api/sessions/'+pick)).json(); agents=d.agents||[]; $('#goal').value=d.goal||''; await loadModels(); if(d.model){ $('#model').value=d.model; } renderTeam(); }} };
  $('#export').onclick=async()=>{ const tr=(window.__transcript||[]); const r1=await fetch('/api/transcript/md',{method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({transcript: tr})}); const d1=await r1.json(); const r2=await fetch('/api/transcript/html',{method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({transcript: tr})}); const d2=await r2.json(); toast('Exports ready: MD & HTML'); if(d1.file){ window.open(d1.file,'_blank'); } if(d2.file){ window.open(d2.file,'_blank'); } };
  $('#saveBase').onclick=async()=>{ const base=$('#ollamaBase').value.trim(); if(!base) return; const r=await fetch('/api/settings/ollama',{method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({base})}); if(r.ok){ toast('Saved Ollama base'); loadModels(); } else { const e=await r.json(); toast('Save failed: '+(e.message||e.error)); } };
  $('#probe').onclick=async()=>{ const t0=performance.now(); try{ const r=await fetch('/api/models'); const ok=r.ok; const dt=(performance.now()-t0).toFixed(0); const d=await r.json(); if(ok && Array.isArray(d)){ $('#probeHint').textContent=`OK (${d.length} models) in ${dt}ms`; } else { $('#probeHint').textContent=`Error in ${dt}ms: `+(d.message||JSON.stringify(d)); } } catch(e){ $('#probeHint').textContent='Probe failed: '+e.message; } };
  $('#reloadModels').onclick=()=>loadModels();
  async function loadModels(){
    $('#model').innerHTML='<option>Loading…</option>';
    try{
      const r = await fetch('/api/models');
      if (!r.ok) {
        const d = await r.json().catch(() => ({}));
        const msg = d.message || `HTTP ${r.status}`;
        throw new Error(`Failed to connect to Ollama: ${msg}`);
      }
      const models = await r.json();
      const sel=$('#model');
      if(Array.isArray(models)&&models.length){
        sel.innerHTML=models.map(m=>`<option>${m}</option>`).join('');
        const def = models.includes('qwen3:8b')?'qwen3:8b':models[0];
        sel.value=def;
        $('#model-hint').textContent=`Loaded ${models.length} models from Ollama`;
        $('#model-count').textContent=models.length+' found';
        lastModels=models;
      } else {
        sel.innerHTML='';
        $('#model-hint').textContent='No models found, but Ollama is reachable.';
        $('#model-count').textContent='0 found';
      }
    } catch(e){
      $('#model').innerHTML='';
      $('#model-hint').textContent = 'Failed to load models. Is Ollama running?';
      $('#model-count').textContent = 'error';
      toast(e.message);
    }
  }
  const origAddMsg = addMsg; addMsg = function(sender, text, mine=false, id=null){ window.__transcript=(window.__transcript||[]).concat([{t:Date.now(), from:sender, text}]); return origAddMsg(sender,text,mine,id); };
  function saveSession() {
    try {
      const session = { goal: $('#goal').value, agents: agents };
      localStorage.setItem('agentStudioSession', JSON.stringify(session));
    } catch(e) { console.error("Failed to save session", e); }
  }
  function loadSession() {
    const saved = localStorage.getItem('agentStudioSession');
    if (!saved) return;
    try {
      const session = JSON.parse(saved);
      if (session.goal) $('#goal').value = session.goal;
      if (session.agents) agents = session.agents;
    } catch (e) {
      console.error("Failed to load session", e);
      localStorage.removeItem('agentStudioSession');
    }
  }
  $('#goal').oninput = saveSession;
  loadSession();
  setStatus('idle'); buttons(false); renderTeam(); loadModels();
</script>
</body>
</html>
"""
@app.get("/")
def index():
    return Response(HTML, mimetype="text/html")
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
if __name__ == "__main__":
    print("Starting Custom Agent Studio — v10 — http://127.0.0.1:8080")
    signal.signal(signal.SIGINT, _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)
    _server = make_server("0.0.0.0", 8080, app)
    _server.serve_forever()
