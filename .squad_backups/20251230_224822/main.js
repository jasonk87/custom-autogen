const $ = (s) => document.querySelector(s); const $$ = (s) => Array.from(document.querySelectorAll(s));
function debounce(func, wait) { let timeout; return function (...args) { clearTimeout(timeout); timeout = setTimeout(() => func.apply(this, args), wait); }; }
let remoteDrafts = {};
let agents = []; let streams = {}; let es = null; let manualNames = []; let lastModels = []; let term = null; let ws = null;
let currentSessionFile = "default";
let terminalLocked = false;
const think = { active: false, buffer: '', open: false, minimized: false };
let graph = null;
const commands = [];
let commandPaletteOpen = false;

function toast(msg) { const t = document.createElement('div'); t.textContent = msg; t.style.cssText = 'position:fixed;right:12px;bottom:12px;background:#0b213f;color:#cfe4ff;border:1px solid #23406e;padding:10px 12px;border-radius:10px;box-shadow:0 10px 25px rgba(0,0,0,.35)'; document.body.appendChild(t); setTimeout(() => t.remove(), 3000); }
function setStatus(state) { const S = $('#status'); const map = { idle: 'Status: Idle', running: 'Status: Agents working…', waiting_for_input: 'Status: Waiting for input…' }; S.textContent = map[state] || 'Status'; $('#fb').disabled = state !== 'waiting_for_input'; $('#send').disabled = state !== 'waiting_for_input'; }
function addMsg(sender, text, mine = false, id = null) {
  const box = document.createElement('div');
  box.className = 'bubble ' + (mine ? 'from-you' : 'from-them');
  const who = document.createElement('div');
  who.className = 'who';
  who.textContent = mine ? 'You' : sender;
  const body = document.createElement('div');

  if (text) {
    body.innerHTML = marked.parse(text);
    body.querySelectorAll('pre code').forEach((block) => {
      hljs.highlightElement(block);
    });
  }

  box.append(who, body);
  const chat = $('#chat');
  const isAtBottom = chat.scrollHeight - chat.scrollTop - chat.clientHeight < 50;
  chat.append(box);
  if (isAtBottom) chat.scrollTop = chat.scrollHeight;
  if (id) streams[id] = { el: body, thinkPhase: false };
}
function appendToken(id, delta, sender) {
  ensureThinkHandling(id, delta);
  const s = streams[id];
  if (!s) { addMsg(sender || 'assistant', '', false, id); }
  const body = streams[id].el;
  if (!streams[id].thinkPhase) { body.textContent += stripThinkTags(delta); }
  const chat = $('#chat');
  const isAtBottom = chat.scrollHeight - chat.scrollTop - chat.clientHeight < 50;
  if (isAtBottom) chat.scrollTop = chat.scrollHeight;
}
function ensureThinkHandling(id, chunk) { let c = chunk; if (c.includes('<think>') || think.active) { if (!think.open) { openThinkDock() } while (c.length) { if (!think.active) { const i = c.indexOf('<think>'); if (i === -1) { if (think.open && think.buffer.length > 0) { closeThinkDock() } break } c = c.slice(i + 7); think.active = true; streams[id] && (streams[id].thinkPhase = true) } else { const j = c.indexOf('</think>'); if (j === -1) { think.buffer += c; c = ''; break } else { think.buffer += c.slice(0, j); c = c.slice(j + 8); think.active = false; streams[id] && (streams[id].thinkPhase = false); renderThink(); if (c.trim().length > 0) { closeThinkDock() } } } } renderThink() } }
function stripThinkTags(s) { return s.replaceAll('<think>', '').replaceAll('</think>', ''); }
function openThinkDock() { if (think.open) return; think.open = true; think.minimized = false; $('#thinkdock').style.display = 'block'; renderThink(); }
function collapseThinkDock() { if (!think.open) return; think.minimized = true; $('#thinkbody').style.display = 'none'; $('#thinkmin').textContent = 'Expand'; }
function expandThinkDock() { if (!think.open) return; think.minimized = false; $('#thinkbody').style.display = 'block'; $('#thinkmin').textContent = 'Minimize'; }
function closeThinkDock() { think.open = false; think.active = false; think.buffer = ''; $('#thinkdock').style.display = 'none'; renderThink(); }
function renderThink() { if (!think.open) { return } const body = $('#thinkbody'); body.textContent = think.buffer || ''; $('#thinklen').textContent = (think.buffer || '').length + ' chars'; if (think.minimized) { body.style.display = 'none' } else { body.style.display = 'block' } }
function renderTeam() {
  const teamContainer = $('#team'); if (!agents.length) { teamContainer.innerHTML = '<div style="color:#9ca3af">Add at least two agents.</div>'; return } teamContainer.innerHTML = agents.map((agent, i) => `
      <div class='team-item'>
        <input data-i='${i}' class='name' value='${agent.name}' style='width:120px' />
        <input data-i='${i}' class='sys' value='${agent.system || ""}' placeholder='system message' />
        <input data-i='${i}' class='num' type='number' step='0.1' min='0' max='2' value='${agent.temperature ?? 0.3}' />
        <button data-i='${i}' class='btn btn-neutral up'>▲</button>
        <button data-i='${i}' class='btn btn-neutral down'>▼</button>
        <button data-i='${i}' class='btn btn-danger rm'>✕</button>
      </div>`).join('');
  $$('.name').forEach(el => { el.onchange = () => { agents[el.dataset.i].name = el.value; syncConfig() } });
  $$('.sys').forEach(el => { el.onchange = () => { agents[el.dataset.i].system = el.value; syncConfig() } });
  $$('.num').forEach(el => { el.onchange = () => { agents[el.dataset.i].temperature = parseFloat(el.value || '0.3'); syncConfig() } });
  $$('.rm').forEach(el => { el.onclick = () => { agents.splice(+el.dataset.i, 1); renderTeam(); syncConfig() } });
  $$('.up').forEach(el => { el.onclick = () => { const i = +el.dataset.i; if (i > 0) { [agents[i - 1], agents[i]] = [agents[i], agents[i - 1]]; renderTeam(); syncConfig() } } });
  $$('.down').forEach(el => { el.onclick = () => { const i = +el.dataset.i; if (i < agents.length - 1) { [agents[i + 1], agents[i]] = [agents[i], agents[i + 1]]; renderTeam(); syncConfig() } } })
}

let syncTimeout;
function syncConfig() {
  clearTimeout(syncTimeout);
  syncTimeout = setTimeout(() => {
    const payload = {
      model: $('#model').value,
      goal: $('#goal').value,
      agents: agents,
      manager_mode: $('#mode').value,
      turns: parseInt($('#turns').value) || 60,
      temperature: parseFloat($('#temperature').value) || 0.3,
      autonomous_user: $('#autonomous-user').checked,
      current_tab: $('.tab.active')?.id || 'tab-setup'
    };
    fetch('/api/sync', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload)
    }).catch(e => console.error("Sync failed", e));
  }, 500);
}
// Sessions Logic
async function renderSessionsList() {
  try {
    const r = await fetch('/api/sessions');
    const list = await r.json();
    const container = $('#session-list');
    if (!container) return;

    container.innerHTML = list.map(s => `
          <div class="session-item ${currentSessionFile === s.id ? 'active' : ''}" onclick="activateSession('${s.id}')">
            <div>
              <div style="font-weight:600;color:white">${s.title}</div>
              <div class="session-meta">Modified: ${new Date(s.modified * 1000).toLocaleString()}</div>
            </div>
            <button onclick="event.stopPropagation(); deleteSession('${s.id}')" class="btn btn-danger" style="padding:2px 6px;font-size:10px">✕</button>
          </div>
        `).join('');
  } catch (e) { console.error("Failed to load sessions", e) }
}

async function deleteSession(name) {
  if (!confirm("Start fresh? Delete " + name)) return;
  await fetch('/api/sessions/' + name, { method: 'DELETE' });
  renderSessionsList();
}

function handleNavToggle() {
  const side = $('.side');
  const backdrop = $('#backdrop');
  side.classList.toggle('open');
  if (backdrop) backdrop.classList.toggle('hidden');
}

document.addEventListener('DOMContentLoaded', () => {
  // Mobile Nav
  const navToggle = $('#nav-toggle');
  if (navToggle) navToggle.onclick = handleNavToggle;

  const backdrop = $('#backdrop');
  if (backdrop) backdrop.onclick = () => {
    $('.side').classList.remove('open');
    backdrop.classList.add('hidden');
  };

  // Switch tabs logic update
  $$('.tab').forEach(t => {
    t.onclick = () => {
      $$('.tab').forEach(x => x.classList.remove('active'));
      t.classList.add('active');

      // Hide all views first
      ['setup', 'work', 'terminal', 'graph', 'playground', 'settings', 'sessions-view'].forEach(id => {
        const el = $('#' + (id === 'sessions-view' ? id : id));
        if (el) el.classList.add('hidden');
      });

      // Simple view toggling:
      $('#setup').classList.add('hidden');
      $('#tree-container') && $('#tree-container').classList.add('hidden');
      $('#terminal-container') && $('#terminal-container').classList.add('hidden');
      $('#graph-container') && $('#graph-container').classList.add('hidden');
      $('#playground').classList.add('hidden');
      $('#settings').classList.add('hidden');
      const sess = $('#sessions-view'); if (sess) sess.classList.add('hidden');

      const map = {
        'tab-setup': 'setup',
        'tab-work': 'tree-container',
        'tab-terminal': 'terminal-container',
        'tab-graph': 'graph-container',
        'tab-playground': 'playground',
        'tab-settings': 'settings',
        'tab-sessions': 'sessions-view'
      };
      const target = $('#' + map[t.id]);
      if (target) target.classList.remove('hidden');

      // Mobile close on tab switch
      if (window.innerWidth <= 768) {
        $('.side').classList.remove('open');
        const backdrop = $('#backdrop');
        if (backdrop) backdrop.classList.add('hidden');
      }

      if (t.id === 'tab-sessions') renderSessionsList();
    }
  });

  // New Chat Button
  const newChat = $('#new-chat');
  if (newChat) newChat.onclick = () => {
    if (confirm("Start new session?")) startRun();
  };

  // Initialize other listeners (moved from bottom if needed)
  $('#goal').oninput = syncConfig;
  $('#model').onchange = syncConfig;
  $('#mode').onchange = e => { syncConfig(); buttons(false) };
  $('#turns').oninput = syncConfig;
  $('#temperature').oninput = syncConfig;
  $('#autonomous-user').onchange = syncConfig;

  loadModels();
  renderTeam();
  joinSession();
});

function buttons(running) { const A = $('#actions'); if (running) { let manual = $('#mode').value === 'Manual'; A.innerHTML = `<div style='display:grid;grid-template-columns:${manual ? '1fr 1fr' : '1fr'};gap:8px'> <button id='stop' class='btn btn-danger'>Stop</button> ${manual ? "<div style='display:flex;gap:6px'><select id='manual-chooser' class='sys'></select><button id='step2' class='btn btn-success'>Step</button></div>" : ""} </div>`; $('#stop').onclick = async () => { if (es) es.close(); await fetch('/stop', { method: 'POST' }); setStatus('idle'); buttons(false); addMsg('System', 'Task aborted', true); closeThinkDock() }; if (manual) { const sel = $('#manual-chooser'); sel.innerHTML = manualNames.map(n => `<option>${n}</option>`).join(''); $('#step2').onclick = () => fetch('/choose_next', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ name: sel.value }) }) } } else { A.innerHTML = `<button id='start' class='btn btn-primary' style='width:100%'>Start</button>`; $('#start').onclick = startRun } }

async function processStream(response) {
  // Clear UI for new/reconnected stream (server replays history)
  if (graph) { graph.clear(); graph.paint() };
  $('#chat').innerHTML = ''; streams = {}; closeThinkDock();

  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = '';

  while (true) {
    const { done, value } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    const parts = buffer.split('\n\n');
    buffer = parts.pop();

    for (const part of parts) {
      if (part.startsWith('data: ')) {
        const dataStr = part.slice(6);
        if (dataStr === '[DONE]') {
          setStatus('idle'); buttons(false); addMsg('System', 'Task complete');
          return 'DONE';
        }
        try {
          const d = JSON.parse(dataStr);
          if (d.type === 'status') {
            setStatus(d.state);
            buttons(d.state === 'running' || d.state === 'waiting_for_input');
          }
          else if (d.type === 'chat') { addMsg(d.sender, d.message, false); if (d.graph_edge) { updateGraph(d.graph_edge) } }
          else if (d.type === 'token') { appendToken(d.id, d.delta, d.sender) }
          else if (d.type === 'config') {
            // Update UI from server config
            const c = d.data;
            if (c.goal !== undefined && $('#goal').value !== c.goal) $('#goal').value = c.goal;
            if (c.model !== undefined && $('#model').value !== c.model) $('#model').value = c.model;
            if (c.manager_mode !== undefined && $('#mode').value !== c.manager_mode) $('#mode').value = c.manager_mode;
            if (c.turns !== undefined) $('#turns').value = c.turns;
            if (c.temperature !== undefined) $('#temperature').value = c.temperature;
            if (c.autonomous_user !== undefined) $('#autonomous-user').checked = c.autonomous_user;
            if (c.agents) {
              // Only redraw if different to avoid focus loss
              if (JSON.stringify(agents) !== JSON.stringify(c.agents)) {
                agents = c.agents;
                renderTeam();
              }
            }
            if (c.current_tab && $('.tab.active')?.id !== c.current_tab) {
              const t = $('#' + c.current_tab);
              if (t) t.onclick();
            }
          }
          else if (d.type === 'draft') {
            remoteDrafts[d.path] = d.content;
            if (currentFilePath === d.path && editor && editor.getValue() !== d.content) {
              window.updatingFromRemote = true;
              const cursor = editor.getCursor();
              editor.setValue(d.content);
              editor.setCursor(cursor);
              window.updatingFromRemote = false;
            }
          }
          else if (d.type === 'drafts_sync') {
            remoteDrafts = d.data || {};
          }
          else if (d.type === 'session_state') {
            if (d.data && d.data.name) {
              currentSessionFile = d.data.name;
              // If sessions list is visible, refresh it to show active state
              if (!$('#sessions-view').classList.contains('hidden')) renderSessionsList();
            }
          }
          else if (d.type === 'control' && d.action === 'reset') {
            // Server started new session, clear client state
            window.location.reload(); // Simplest way to get clean state? Or just clean UI?
            // Reload might be best to re-fetch file tree etc.
          }
        } catch (e) { }
      }
    }
  }
  // Stream ended without [DONE]
  return 'RETRY';
}

async function connectStream(url, initialMethod, initialBody) {
  let method = initialMethod;
  let body = initialBody;
  let retryDelay = 1000;

  if (es) es.close();

  const controller = new AbortController();
  es = { close: () => controller.abort() };

  while (true) {
    try {
      const response = await fetch(url, {
        method: method,
        headers: method === 'POST' ? { 'Content-Type': 'application/json' } : undefined,
        body: body,
        signal: controller.signal
      });

      if (!response.ok) throw new Error('Network response was not ok');

      // On success, reset retry delay and ensure next attempt is a join (GET)
      retryDelay = 1000;
      if (method === 'POST') {
        method = 'GET';
        body = undefined;
      }

      const result = await processStream(response);
      if (result === 'DONE') return;

      // If we are here, stream disconnected prematurely. Retry.
      console.log("Stream disconnected, retrying...");

    } catch (e) {
      if (e.name === 'AbortError') return;
      console.error("Connection error:", e);
    }

    // Backoff and retry
    setStatus('Connection lost. Retrying...');
    await new Promise(r => setTimeout(r, retryDelay));
    retryDelay = Math.min(retryDelay * 1.5, 10000);
  }
}

async function startRun() {
  if (agents.length < 2) { toast('Add at least two agents to start.'); return }

  const payload = {
    new_session: true, // ALWAYS start fresh on explicit start button click per user request
    model: $('#model').value,
    goal: $('#goal').value || '',
    agents: agents,
    manager_mode: $('#mode').value,
    turns: $('#turns').value || '',
    temperature: $('#temperature').value || '0.3',
    autonomous_user: $('#autonomous-user').checked
  };

  await connectStream('/stream', 'POST', JSON.stringify(payload));

  // Trigger AI renaming in background
  setTimeout(triggerAIRenaming, 5000);
}

async function triggerAIRenaming() {
  try {
    await fetch('/api/sessions/rename_ai', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ goal: $('#goal').value })
    });
    // Refresh list if visible
    if (!$('#sessions-view').classList.contains('hidden')) renderSessionsList();
  } catch (e) { }
}

async function activateSession(name) {
  if (!name) return;
  try {
    // 1. Tell server to switch state
    const r = await fetch('/api/sessions/activate', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ name })
    });
    if (!r.ok) throw new Error('Activation failed');

    const d = await r.json();
    currentSessionFile = name;

    // 2. Refresh UI (file tree, session list highlight)
    refreshTree();
    renderSessionsList();

    // 3. Connect to stream to get history/state
    joinSession();

    // Switch to Workspace tab automatically? Or stay on Sessions?
    // Maybe stay on sessions but show toast
    toast(`Switched to session: ${name}`);

  } catch (e) {
    toast("Session activation failed: " + e.message);
  }
}

async function joinSession() {
  await connectStream('/stream', 'GET', undefined);
}
function renderTreeNode(node) {
  const isDir = node.type === 'dir' || node.children;
  const icon = isDir ? '📁' : '📄';
  const toggleClass = isDir ? 'tree-toggle' : 'tree-toggle invisible';
  // Default collapsed (except root or if we want logic) - let's default expanded for now or collapsed?
  // Let's go with collapsed by default for subfolders to save space, but maybe top level expanding is better.
  // Ideally we preserve state, but for now simple toggle.

  const actions = `<div class="node-actions" style="margin-left:auto;display:flex;gap:4px">
    <button class="rename-node" data-path="${node.path}" title="Rename" style="border:none;background:none;cursor:pointer;opacity:0.5;font-size:12px">✏️</button>
    <button class="delete-node" data-path="${node.path}" title="Delete" style="border:none;background:none;cursor:pointer;opacity:0.5;font-size:12px">✕</button>
  </div>`;

  const nameEl = node.type === 'file'
    ? `<span class="node-name file-link" data-path="${node.path}" style="color:#93c5fd">${node.name}</span>`
    : `<span class="node-name dir-link" data-path="${node.path}">${node.name}</span>`;

  let childrenHtml = '';
  if (isDir && node.children) {
    childrenHtml = `<ul class="tree-children">${node.children.map(renderTreeNode).join('')}</ul>`;
  }

  return `
    <li>
      <div class="node" data-path="${node.path}">
        <span class="${toggleClass}">▼</span>
        <span class="node-icon">${icon}</span>
        ${nameEl}
        ${actions}
      </div>
      ${childrenHtml}
    </li>`;
}

async function refreshTree() {
  try {
    const r = await fetch('/api/workspace/tree');
    const d = await r.json();
    // Wrap root in UL
    const html = `<ul class="tree-root">${renderTreeNode(d)}</ul>`;
    $('#tree').innerHTML = html;

    // Add Event Delegation
    $('#tree').onclick = (e) => {
      // Toggle handling
      if (e.target.classList.contains('tree-toggle') || e.target.classList.contains('dir-link')) {
        const nodeDiv = e.target.closest('.node');
        const li = nodeDiv.closest('li');
        const ul = li.querySelector('ul.tree-children');
        const toggle = nodeDiv.querySelector('.tree-toggle');
        if (ul) {
          ul.classList.toggle('hidden');
          toggle.classList.toggle('collapsed');
        }
        return; // Stop further handling
      }
      onTreeClick(e);
    };
  } catch (e) { console.error("Error in refreshTree:", e) }
}
async function onTreeClick(e) {
  if (e.target.classList.contains('delete-node')) {
    const path = e.target.dataset.path; if (confirm(`Are you sure you want to delete '${path}'?`)) { try { const r = await fetch('/api/workspace/delete', { method: 'DELETE', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ path }) }); if (!r.ok) { const err = await r.json().catch(() => ({ error: 'Request failed' })); throw new Error(err.message || err.error) } toast(`Deleted ${path}`); refreshTree() } catch (err) { toast(`Error deleting ${path}: ${err.message}`) } }
  } else if (e.target.classList.contains('rename-node')) {
    e.preventDefault(); e.stopPropagation(); handleRename(e.target)
  } else if (e.target.classList.contains('file-link')) {
    loadFile(e.target.dataset.path);
  }
}
function handleRename(renameButton) { const nodeDiv = renameButton.closest('.node'); const nameEl = nodeDiv.querySelector('.node-name'); const oldPath = renameButton.dataset.path; const oldName = nameEl.textContent; const input = document.createElement('input'); input.type = 'text'; input.value = oldName; input.className = 'node-name-input'; nameEl.replaceWith(input); input.focus(); input.select(); const finishRename = async () => { const newName = input.value.trim(); if (newName && newName !== oldName) { const oldPathParts = oldPath.split('/'); const newPath = [...oldPathParts.slice(0, -1), newName].join('/'); try { const r = await fetch('/api/workspace/rename', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ old_path: oldPath, new_path: newPath }) }); if (!r.ok) { const err = await r.json().catch(() => ({ error: 'Request failed' })); throw new Error(err.message || err.error) } toast(`Renamed to ${newName}`) } catch (err) { toast(`Error renaming: ${err.message}`) } } refreshTree() }; input.onblur = finishRename; input.onkeydown = e => { if (e.key === 'Enter') { input.blur() } else if (e.key === 'Escape') { input.onblur = null; refreshTree() } } }
async function loadModels() { $('#model').innerHTML = '<option>Loading…</option>'; try { const r = await fetch('/api/models'); const models = await r.json(); const sel = $('#model'); if (Array.isArray(models) && models.length) { sel.innerHTML = models.map(m => `<option>${m}</option>`).join(''); const def = models.includes('qwen3:8b') ? 'qwen3:8b' : models[0]; sel.value = def; $('#model-hint').textContent = `Loaded ${models.length} models from Ollama`; $('#model-count').textContent = models.length + ' found'; lastModels = models } else { sel.innerHTML = ''; $('#model-hint').textContent = 'No models found'; $('#model-count').textContent = '0 found' } } catch (e) { $('#model').innerHTML = ''; $('#model-hint').textContent = 'Failed to load models: ' + e.message; $('#model-count').textContent = 'error' } }
const origAddMsg = addMsg; addMsg = function (sender, text, mine = false, id = null) { window.__transcript = (window.__transcript || []).concat([{ t: Date.now(), from: sender, text }]); return origAddMsg(sender, text, mine, id) };
function saveSession() { try { const session = { goal: $('#goal').value, agents: agents, name: currentSessionFile }; localStorage.setItem('agentStudioSession', JSON.stringify(session)) } catch (e) { console.error("Failed to save session", e) } }
function loadSession() { const saved = localStorage.getItem('agentStudioSession'); if (!saved) return; try { const session = JSON.parse(saved); if (session.goal) $('#goal').value = session.goal; if (session.agents) agents = session.agents; if (session.name) { currentSessionFile = session.name; activateSession(session.name); } } catch (e) { console.error("Failed to load session", e); localStorage.removeItem('agentStudioSession') } }
async function activateSession(name) {
  if (!name) return;
  try {
    const r = await fetch('/api/sessions/activate', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ name })
    });
    if (!r.ok) throw new Error('Activation failed');
    refreshTree();
  } catch (e) {
    toast("Session activation failed: " + e.message);
  }
}
function initTerminal() {
  if (term) return; // Already initialized
  const terminalContainer = document.getElementById('terminal-container');
  term = new Terminal({
    cursorBlink: true,
    theme: {
      background: '#0b1220',
      foreground: '#e5e7eb',
      cursor: '#e5e7eb',
    }
  });
  const fitAddon = new FitAddon.FitAddon();
  term.loadAddon(fitAddon);
  term.open(terminalContainer);
  fitAddon.fit();

  const wsProtocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
  const wsUrl = `${wsProtocol}//${window.location.host}/api/workspace/terminal`;
  ws = new WebSocket(wsUrl);

  ws.onopen = () => {
    // Send initial size
    const initialSize = `resize:${term.rows}:${term.cols}`;
    ws.send(initialSize);
  };

  ws.onmessage = (event) => {
    term.write(event.data);
    if (terminalLocked) {
      setTimeout(() => {
        const buf = term.buffer.active;
        const line = buf.getLine(buf.baseY + buf.cursorY);
        if (line) {
          const str = line.translateToString().trimEnd();
          if (/[>$%#?:\]]\s*$/.test(str)) {
            terminalLocked = false;
          }
        }
      }, 10);
    }
  };

  ws.onclose = () => {
    term.write('\\r\\n\\n[Connection Closed]');
    terminalLocked = false;
  };

  term.onData((data) => {
    if (terminalLocked && data !== '\x03') {
      toast("Terminal busy (Ctrl+C to force)");
      return;
    }
    if (data.includes('\r')) terminalLocked = true;
    ws.send(data);
  });

  window.addEventListener('resize', () => {
    fitAddon.fit();
    const size = `resize:${term.rows}:${term.cols}`;
    if (ws.readyState === WebSocket.OPEN) {
      ws.send(size);
    }
  });
}
function initGraph() { const container = $('#graph-container'); const width = container.scrollWidth; const height = container.scrollHeight || 500; graph = new G6.Graph({ container, width, height, fitView: true, layout: { type: 'force', preventOverlap: true, nodeSize: 30 }, defaultNode: { size: 30, style: { fill: '#2563eb', stroke: '#1d4ed8', lineWidth: 2, }, labelCfg: { style: { fill: '#e5e7eb' } } }, defaultEdge: { style: { stroke: '#9ca3af', lineWidth: 1.5, endArrow: { path: G6.Arrow.triangle(), d: 5, } } } }); graph.data({ nodes: [], edges: [] }); graph.render() }
function updateGraph(edge) { const fromNode = graph.findById(edge.from); if (!fromNode) { graph.addItem('node', { id: edge.from, label: edge.from }) } const toNode = graph.findById(edge.to); if (!toNode) { graph.addItem('node', { id: edge.to, label: edge.to }) } graph.addItem('edge', { source: edge.from, target: edge.to }); }
async function loadTools() {
  try {
    const r = await fetch('/api/tools'); const tools = await r.json(); const container = $('#playground-tools'); container.innerHTML = tools.map(t => `
      <label title="${t.description}" style="display:flex;align-items:center;gap:4px;background:var(--chip);padding:4px 8px;border-radius:6px;">
        <input type="checkbox" class="playground-tool-checkbox" value="${t.name}" />
        <span>${t.name}</span>
      </label>`).join('')
  } catch (e) { console.error("Failed to load tools", e) }
}
function setupTabs() {
  const tabs = $$('.tab');
  const sections = $$('section');
  tabs.forEach(tab => {
    tab.onclick = () => {
      tabs.forEach(t => t.classList.remove('active'));
      sections.forEach(s => s.classList.add('hidden'));
      tab.classList.add('active');
      const target = tab.id.replace('tab-', '');
      $(`#${target}`).classList.remove('hidden');

      // Wide Mode logic
      if (['work', 'terminal', 'graph'].includes(target)) {
        $('.app').classList.add('wide-sidebar');
      } else {
        $('.app').classList.remove('wide-sidebar');
      }

      if (target === 'work') {
        refreshTree();
        setTimeout(() => typeof initEditor === 'function' && initEditor(), 100);
      }
      if (target === 'terminal') initTerminal();
      if (target === 'graph' && graph) graph.fitView();
      if (target === 'playground') loadTools();
      syncConfig();
    }
  })
}
function registerCommand(id, description, action) {
  commands.push({ id, description, action });
}

function renderCommands(filter = '') {
  const list = $('#command-list');
  list.innerHTML = '';
  const filteredCommands = commands.filter(c => c.description.toLowerCase().includes(filter.toLowerCase()));

  filteredCommands.forEach(command => {
    const item = document.createElement('li');
    item.className = 'command-item';
    item.textContent = command.description;
    item.onclick = () => {
      command.action();
      toggleCommandPalette(false);
    };
    list.appendChild(item);
  });
}

function toggleCommandPalette(show) {
  const palette = $('#command-palette');
  if (show) {
    renderCommands();
    palette.style.display = 'block';
    $('#command-input').value = '';
    $('#command-input').focus();
    commandPaletteOpen = true;
  } else {
    palette.style.display = 'none';
    commandPaletteOpen = false;
  }
}

document.addEventListener('DOMContentLoaded', () => {
  initGraph(); setupTabs();

  // EDITOR LOGIC
  let editor = null;
  let currentFilePath = null;

  window.runFile = async () => {
    if (!currentFilePath) return;
    // 1. Save
    await $('#save-file').click();

    // 2. Switch to Terminal
    $('#tab-terminal').click();

    // 3. Send command
    // Wait a bit for terminal to be ready if it wasn't
    setTimeout(() => {
      if (ws && ws.readyState === WebSocket.OPEN) {
        // Check if python
        let cmd = '';
        if (currentFilePath.endsWith('.py')) {
          cmd = `python "${currentFilePath}"\r`;
        } else {
          cmd = `echo "Cannot run ${currentFilePath}"\r`;
        }
        ws.send(cmd);
        terminalLocked = true;
      } else {
        toast("Terminal not ready");
      }
    }, 500);
  };

  const saveDraft = debounce(async (path, content) => {
    await fetch('/api/draft', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ path, content })
    }).catch(console.error);
  }, 1000);

  function initEditor() {
    if (editor) return;
    const el = document.getElementById('editor-container');
    if (!el) return;
    editor = CodeMirror(el, {
      mode: 'python',
      theme: 'dracula',
      lineNumbers: true,
      tabSize: 4,
      indentUnit: 4,
      lineWrapping: true
    });
    editor.setSize('100%', '100%');
    editor.on('change', () => {
      if (!window.updatingFromRemote && currentFilePath) {
        saveDraft(currentFilePath, editor.getValue());
      }
    });
  }

  window.loadFile = async (path) => {
    try {
      let content = '';
      if (remoteDrafts[path]) {
        content = remoteDrafts[path];
      } else {
        const r = await fetch(`/api/workspace/file?path=${encodeURIComponent(path)}`);
        if (!r.ok) throw new Error('Failed to load file');
        content = await r.text();
      }

      if (!editor) initEditor();

      // Determine mode
      let mode = 'python';
      if (path.endsWith('.js')) mode = 'javascript';
      else if (path.endsWith('.html')) mode = 'htmlmixed';
      else if (path.endsWith('.css')) mode = 'css';
      else if (path.endsWith('.json')) mode = 'javascript'; // json mode often falls under js

      editor.setOption('mode', mode);
      editor.setValue(content);
      currentFilePath = path;
      $('#current-file').textContent = path;
      $('#save-file').disabled = false;
      $('#run-file').disabled = !path.endsWith('.py');
    } catch (e) {
      toast('Error loading file: ' + e.message);
    }
  };

  $('#save-file').onclick = async () => {
    if (!currentFilePath || !editor) return;
    const btn = $('#save-file');
    btn.disabled = true;
    btn.textContent = 'Saving...';
    try {
      const content = editor.getValue();
      const r = await fetch('/api/workspace/file', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ path: currentFilePath, content })
      });
      if (!r.ok) throw new Error('Save failed');
      toast('File saved');
    } catch (e) {
      toast(e.message);
    } finally {
      btn.disabled = false;
      btn.textContent = 'Save';
    }
  };

  $('#run-file').onclick = window.runFile;



  // Autonomous User Logic
  const updateAutonomousState = () => {
    const mode = $('#mode').value;
    const container = $('#autonomous-user-container');
    const input = $('#autonomous-user');
    if (!container || !input) return;

    if (mode === 'Auto') {
      input.checked = true;
      container.style.display = 'none';
    } else if (mode === 'RoundRobin') {
      container.style.display = 'flex';
    } else {
      input.checked = false;
      container.style.display = 'none';
    }
  };
  $('#mode').onchange = updateAutonomousState;
  updateAutonomousState(); // Init

  // Custom Mode Toggle
  $('#btn-mode-scenario').onclick = () => {
    $('#btn-mode-scenario').classList.add('active');
    $('#btn-mode-manual').classList.remove('active');
    $('#mode-scenario-container').classList.remove('hidden');
    $('#mode-manual-container').classList.add('hidden');
  };
  $('#btn-mode-manual').onclick = () => {
    $('#btn-mode-scenario').classList.remove('active');
    $('#btn-mode-manual').classList.add('active');
    $('#mode-scenario-container').classList.add('hidden');
    $('#mode-manual-container').classList.remove('hidden');
  };


  registerCommand('view.setup', 'View: Show Setup', () => $('#tab-setup').click());
  registerCommand('view.workspace', 'View: Show Workspace', () => $('#tab-work').click());
  registerCommand('view.terminal', 'View: Show Terminal', () => $('#tab-terminal').click());
  registerCommand('view.graph', 'View: Show Graph', () => $('#tab-graph').click());
  registerCommand('view.playground', 'View: Show Playground', () => $('#tab-playground').click());
  registerCommand('view.settings', 'View: Show Settings', () => $('#tab-settings').click());

  // Think Dock
  $('#thinkmin').onclick = () => { if (think.minimized) expandThinkDock(); else collapseThinkDock() };
  $('#thinkclose').onclick = () => { closeThinkDock() };

  // Bot Add
  $('#bot-add').onclick = () => {
    const n = $('#bot-title').value.trim();
    if (!n) return;
    const sanitizedName = n.replace(/[^a-zA-Z0-9_]/g, '_');
    agents.push({ name: sanitizedName, system: $('#bot-description').value.trim(), temperature: 0.3 });
    $('#bot-title').value = '';
    $('#bot-description').value = '';
    renderTeam();
    saveSession()
  };

  // Generate Agents
  $('#generate-agents').onclick = async () => {
    const scenario = $('#scenario').value.trim();
    if (!scenario) { toast('Please enter a scenario description.'); return }
    const btn = $('#generate-agents');
    btn.disabled = true;
    btn.textContent = 'Generating...';
    try {
      const r = await fetch('/api/scenario/generate', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ scenario, model: $('#model').value, num_agents: $('#num-agents').value }) });
      if (!r.ok) { const err = await r.json().catch(() => ({ error: 'Request failed' })); throw new Error(err.error) }
      const data = await r.json();
      agents = data.agents;
      $('#goal').value = data.goal || '';
      renderTeam();
      toast('Agents and goal generated successfully.')
    } catch (e) { toast('Failed to generate agents: ' + e.message) }
    finally { btn.disabled = false; btn.textContent = 'Generate Team' }
  };

  // Chat Input
  $('#fbform').onsubmit = async e => {
    e.preventDefault();
    const v = $('#fb').value.trim();
    if (!v) return;
    addMsg('You', v, true);
    await fetch('/user_input', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ message: v }) });
    $('#fb').value = '';
    setStatus('running')
  };

  // New File Explorer Logic
  window.handleUpload = async (input) => {
    const file = input.files[0];
    if (!file) return;
    if (file.size > 10 * 1024 * 1024) { toast('File too large (max 10MB)'); input.value = ''; return; }
    const fd = new FormData();
    fd.append('file', file);
    try {
      const r = await fetch('/api/workspace/upload', { method: 'POST', body: fd });
      if (!r.ok) throw new Error('Upload failed');
      toast('Uploaded ' + file.name);
      refreshTree();
    } catch (e) { toast(e.message); }
    input.value = '';
  };

  window.showCreateInput = (type) => {
    const tree = $('#tree');
    let ul = tree.querySelector('ul');
    if (!ul) { ul = document.createElement('ul'); tree.appendChild(ul); }

    const li = document.createElement('li');
    li.className = 'node';
    li.style.paddingLeft = '14px';

    const icon = type === 'folder' ? '📁' : '📄';
    li.innerHTML = `<span class="node-icon">${icon}</span> <input class="tree-input" placeholder="Name..." />`;
    ul.prepend(li);

    const input = li.querySelector('input');
    input.focus();

    const commit = async () => {
      const name = input.value.trim();
      if (!name) { li.remove(); return; }
      try {
        if (type === 'folder') {
          const r = await fetch('/api/workspace/new_folder', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ path: name }) });
          if (!r.ok) throw new Error('Failed to create folder');
        } else {
          const r = await fetch('/api/workspace/file', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ path: name, content: '' }) });
          if (!r.ok) throw new Error('Failed to create file');
        }
        refreshTree();
      } catch (e) { toast(e.message); li.remove(); }
    };

    input.onblur = commit;
    input.onkeydown = (e) => {
      if (e.key === 'Enter') { input.onblur = null; commit(); }
      else if (e.key === 'Escape') { input.onblur = null; li.remove(); }
    };
  };

  document.addEventListener('keydown', (e) => {
    if ((e.ctrlKey || e.metaKey) && e.shiftKey && e.key === 'P') {
      e.preventDefault();
      toggleCommandPalette(!commandPaletteOpen);
    }

    if (!commandPaletteOpen) return;

    if (e.key === 'Escape') {
      toggleCommandPalette(false);
    }

    const list = $('#command-list');
    const items = Array.from(list.children);
    const selected = list.querySelector('.selected');
    let currentIndex = items.indexOf(selected);

    if (e.key === 'ArrowDown') {
      e.preventDefault();
      if (selected) selected.classList.remove('selected');
      currentIndex = (currentIndex + 1) % items.length;
      if (items[currentIndex]) {
        items[currentIndex].classList.add('selected');
        items[currentIndex].scrollIntoView({ block: 'nearest' });
      }
    } else if (e.key === 'ArrowUp') {
      e.preventDefault();
      if (selected) selected.classList.remove('selected');
      currentIndex = (currentIndex - 1 + items.length) % items.length;
      if (items[currentIndex]) {
        items[currentIndex].classList.add('selected');
        items[currentIndex].scrollIntoView({ block: 'nearest' });
      }
    } else if (e.key === 'Enter') {
      e.preventDefault();
      const target = selected || items[0];
      if (target) target.click();
    }
  });

  $('#command-input').addEventListener('input', (e) => {
    renderCommands(e.target.value);
    const firstItem = $('#command-list').children[0];
    if (firstItem) {
      firstItem.classList.add('selected');
    }
  });

  document.addEventListener('click', (e) => {
    const palette = $('#command-palette');
    if (commandPaletteOpen && !palette.contains(e.target)) {
      toggleCommandPalette(false);
    }
  });
  const sessionsModal = $('#sessions-modal');
  const sessionsList = $('#sessions-list');

  async function renderSessions() {
    const r = await fetch('/api/sessions');
    const files = await r.json();
    sessionsList.innerHTML = files.map(f => `
      <div style="display:flex; justify-content:space-between; align-items:center; padding:5px; border-bottom:1px solid var(--border);">
        <span>${f}</span>
        <div>
          <button class="btn btn-success session-load" data-file="${f}">Load</button>
          <button class="btn btn-danger session-delete" data-file="${f}">Delete</button>
        </div>
      </div>`).join('');
  }

  // Removed obsolete sessions modal triggers
  // $('#sessions').onclick = async () => { await renderSessions(); sessionsModal.style.display = 'flex' };
  // $('#sessions-close').onclick = () => { sessionsModal.style.display = 'none' };

  $('#session-save').onclick = async () => {
    const name = $('#session-name').value.trim();
    if (!name) { toast('Please enter a session name.'); return }
    await fetch('/api/sessions', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ name, model: $('#model').value, goal: $('#goal').value, agents }) });
    toast('Session saved.');
    currentSessionFile = name;
    activateSession(name);
    $('#session-name').value = '';
    await renderSessions();
  };

  sessionsList.addEventListener('click', async e => {
    const target = e.target;
    const file = target.dataset.file;
    if (target.classList.contains('session-load')) {
      const r = await fetch(`/api/sessions/${file}`);
      const data = await r.json();
      agents = data.agents || [];
      $('#goal').value = data.goal || '';
      await loadModels();
      if (data.model) { $('#model').value = data.model }
      renderTeam();
      currentSessionFile = file.replace('.json', '');
      activateSession(currentSessionFile);
      toast(`Session ${file} loaded.`);
      sessionsModal.style.display = 'none';
    }
    if (target.classList.contains('session-delete')) {
      if (confirm(`Are you sure you want to delete ${file}?`)) {
        await fetch(`/api/sessions/${file}`, { method: 'DELETE' });
        toast(`Session ${file} deleted.`);
        await renderSessions();
      }
    }
  });

  $('#export').onclick = async () => {
    const tr = (window.__transcript || []);
    const r1 = await fetch('/api/transcript/md', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ transcript: tr }) });
    const d1 = await r1.json();
    const r2 = await fetch('/api/transcript/html', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ transcript: tr }) });
    const d2 = await r2.json();
    toast('Exports ready: MD & HTML');
    if (d1.file) { window.open(d1.file, '_blank') }
    if (d2.file) { window.open(d2.file, '_blank') }
  };

  $('#saveBase').onclick = async () => {
    const base = $('#ollamaBase').value.trim();
    if (!base) return;
    const r = await fetch('/api/settings/ollama', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ base }) });
    if (r.ok) { toast('Saved Ollama base'); loadModels() }
    else { const e = await r.json(); toast('Save failed: ' + (e.message || e.error)) }
  };

  $('#probe').onclick = async () => {
    const t0 = performance.now();
    try {
      const r = await fetch('/api/models');
      const ok = r.ok;
      const dt = (performance.now() - t0).toFixed(0);
      const d = await r.json();
      if (ok && Array.isArray(d)) { $('#probeHint').textContent = `OK (${d.length} models) in ${dt}ms` }
      else { $('#probeHint').textContent = `Error in ${dt}ms: ` + (d.message || JSON.stringify(d)) }
    } catch (e) { $('#probeHint').textContent = 'Probe failed: ' + e.message }
  };

  $('#reloadModels').onclick = () => loadModels();

  $('#playground-form').onsubmit = async e => {
    e.preventDefault();
    const input = $('#playground-input');
    const msg = input.value.trim();
    if (!msg) return;
    const agentName = $('#playground-agent-name').value;
    const systemMessage = $('#playground-system-message').value;
    const selectedTools = $$('.playground-tool-checkbox:checked').map(el => el.value);
    const chatArea = $('#playground-chat-area');
    const userMsgEl = document.createElement('div');
    userMsgEl.innerHTML = `<b>You:</b><p>${msg}</p>`;
    chatArea.append(userMsgEl);
    input.value = '';
    input.disabled = true;
    $('#playground-send').disabled = true;
    try {
      const r = await fetch('/api/agent/run', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ agent: { name: agentName, system_message: systemMessage, tools: selectedTools }, message: msg, model: $('#model').value }) });
      const res = await r.json();
      const agentMsgEl = document.createElement('div');
      if (r.ok) { agentMsgEl.innerHTML = `<b>${agentName}:</b><p>${res.reply}</p>` }
      else { agentMsgEl.innerHTML = `<b>Error:</b><p style="color:var(--red)">${res.error || res.message}</p>` }
      chatArea.append(agentMsgEl)
    } catch (err) {
      const errorEl = document.createElement('div');
      errorEl.innerHTML = `<b>Error:</b><p style="color:var(--red)">${err.message}</p>`;
      chatArea.append(errorEl)
    } finally {
      input.disabled = false;
      $('#playground-send').disabled = false;
      input.focus()
    }
  };

  $('#goal').oninput = syncConfig;
  $('#model').onchange = syncConfig;
  $('#mode').onchange = e => { syncConfig(); buttons(false) }; // buttons(false) to update manual/auto UI
  $('#turns').oninput = syncConfig;
  $('#temperature').oninput = syncConfig;
  $('#autonomous-user').onchange = syncConfig;

  // loadSession(); // Disable old session loading in favor of sync
  setStatus('idle');
  buttons(false);
  renderTeam();
  loadModels();



  // Auto-Join Session
  joinSession();
});
