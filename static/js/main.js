const $ = (s) => document.querySelector(s);
const $$ = (s) => Array.from(document.querySelectorAll(s));

let agents = [];
let streams = {};
let es = null;
let runActive = false;
let currentStatus = 'idle';
const think = { active: false, buffer: '', open: false, minimized: false };
const playgroundState = { toolNames: [] };
const SESSION_KEY = 'agentStudioSession';

function toast(msg) {
  const t = document.createElement('div');
  t.textContent = msg;
  t.style.cssText = [
    'position:fixed',
    'right:24px',
    'bottom:24px',
    'background:var(--glass-solid)',
    'color:var(--text)',
    'border:1px solid var(--primary)',
    'padding:12px 16px',
    'border-radius:10px',
    'z-index:10000',
    'font-size:13px'
  ].join(';');
  document.body.appendChild(t);
  setTimeout(() => t.remove(), 2200);
}

function setStatus(state) {
  currentStatus = state;
  const status = $('#status');
  if (!status) return;
  const map = {
    idle: 'Status: Idle',
    running: 'Status: Agents working...',
    waiting_for_input: 'Status: Waiting for input...',
    waiting_for_manual_selection: 'Status: Waiting for speaker selection...'
  };
  status.textContent = map[state] || 'Status';
  status.classList.toggle('pulse', state === 'running');

  const fb = $('#fb');
  const send = $('#send');
  const enabled = runActive;
  if (fb) fb.disabled = !enabled;
  if (send) send.disabled = !enabled;
  if (state === 'waiting_for_input' && fb) fb.focus();
  if (fb) {
    fb.placeholder = state === 'waiting_for_input'
      ? 'Type required input...'
      : 'Type message (live coaching while agents run)...';
  }
}

function addMsg(sender, text, mine = false, id = null, target = '#chat') {
  const chat = $(target);
  if (!chat) return;

  window.__transcript = (window.__transcript || []).concat([{ t: Date.now(), from: sender, text }]);

  const box = document.createElement('div');
  box.className = `bubble ${mine ? 'from-you' : 'from-them'}`;
  const who = document.createElement('div');
  who.className = 'who';
  who.textContent = mine ? 'YOU' : sender;

  const body = document.createElement('div');
  if (window.marked && text) body.innerHTML = window.marked.parse(text);
  else body.textContent = text || '';

  box.append(who, body);
  chat.append(box);
  chat.scrollTop = chat.scrollHeight;

  if (id) streams[id] = { el: body, thinkPhase: false };
}

function ensureThinkHandling(id, chunk) {
  let c = chunk;
  if (!c.includes('<think>') && !think.active) return;

  if (!think.open) openThinkDock();
  while (c.length) {
    if (!think.active) {
      const i = c.indexOf('<think>');
      if (i === -1) break;
      c = c.slice(i + 7);
      think.active = true;
      if (streams[id]) streams[id].thinkPhase = true;
    } else {
      const j = c.indexOf('</think>');
      if (j === -1) {
        think.buffer += c;
        c = '';
      } else {
        think.buffer += c.slice(0, j);
        c = c.slice(j + 8);
        think.active = false;
        if (streams[id]) streams[id].thinkPhase = false;
      }
    }
  }

  renderThink();
}

function appendToken(id, delta) {
  ensureThinkHandling(id, delta);
  if (!streams[id]) addMsg('assistant', '', false, id);
  const stream = streams[id];
  if (!stream.thinkPhase) {
    stream.el.innerHTML += (delta || '').replaceAll('\n', '<br>');
  }
  $('#chat').scrollTop = $('#chat').scrollHeight;
}

function openThinkDock() {
  think.open = true;
  think.minimized = false;
  const dock = $('#thinkdock');
  const body = $('#thinkbody');
  if (dock) dock.style.display = 'block';
  if (body) body.style.display = 'block';
}

function closeThinkDock() {
  think.open = false;
  think.active = false;
  think.buffer = '';
  think.minimized = false;
  const dock = $('#thinkdock');
  const body = $('#thinkbody');
  if (dock) dock.style.display = 'none';
  if (body) body.style.display = 'block';
}

function toggleThinkMinimize() {
  const body = $('#thinkbody');
  if (!body) return;
  think.minimized = !think.minimized;
  body.style.display = think.minimized ? 'none' : 'block';
  $('#thinkmin').textContent = think.minimized ? 'Expand' : 'Minimize';
}

function renderThink() {
  const body = $('#thinkbody');
  const len = $('#thinklen');
  if (body) body.textContent = think.buffer;
  if (len) len.textContent = `${think.buffer.length} chars`;
}

function renderTeam() {
  const container = $('#team');
  if (!container) return;

  $('#team-count').textContent = `${agents.length} members`;
  if (!agents.length) {
    container.innerHTML = "<div style='color:var(--muted); padding:12px; border:1px dashed var(--border); border-radius:8px;'>No agents in team.</div>";
    return;
  }

  container.innerHTML = agents.map((a, i) => `
    <div class='team-item'>
      <div class='team-item-header'>
        <input data-i='${i}' class='name' value='${a.name || ''}' placeholder='Name' />
        <div class='team-item-actions'>
          <button data-i='${i}' class='btn btn-neutral up' title='Move Up'>^</button>
          <button data-i='${i}' class='btn btn-neutral down' title='Move Down'>v</button>
          <button data-i='${i}' class='btn btn-danger rm' title='Remove'>x</button>
        </div>
      </div>
      <textarea data-i='${i}' class='sys' placeholder='System Message'>${a.system || ''}</textarea>
      <div class='team-item-footer'>
        <label>Temp</label>
        <input data-i='${i}' class='num' type='number' min='0' max='2' step='0.1' value='${a.temperature ?? 0.3}' />
      </div>
    </div>
  `).join('');

  $$('.name').forEach(el => {
    el.onchange = () => { agents[+el.dataset.i].name = el.value.trim(); saveSession(); };
  });
  $$('.sys').forEach(el => {
    el.onchange = () => { agents[+el.dataset.i].system = el.value; saveSession(); };
  });
  $$('.num').forEach(el => {
    el.onchange = () => { agents[+el.dataset.i].temperature = parseFloat(el.value || '0.3'); saveSession(); };
  });
  $$('.up').forEach(el => {
    el.onclick = () => {
      const i = +el.dataset.i;
      if (i <= 0) return;
      [agents[i - 1], agents[i]] = [agents[i], agents[i - 1]];
      renderTeam();
      saveSession();
    };
  });
  $$('.down').forEach(el => {
    el.onclick = () => {
      const i = +el.dataset.i;
      if (i >= agents.length - 1) return;
      [agents[i + 1], agents[i]] = [agents[i], agents[i + 1]];
      renderTeam();
      saveSession();
    };
  });
  $$('.rm').forEach(el => {
    el.onclick = () => {
      agents.splice(+el.dataset.i, 1);
      renderTeam();
      saveSession();
    };
  });
}

function buttons(running) {
  const actions = $('#actions');
  if (!actions) return;

  if (running) {
    actions.innerHTML = "<button id='stop' class='btn btn-danger' style='width:100%'>Stop Task</button>";
    $('#stop').onclick = async () => {
      if (es) es.close();
      await fetch('/stop', { method: 'POST' });
      runActive = false;
      setStatus('idle');
      buttons(false);
      closeThinkDock();
    };
  } else {
    actions.innerHTML = "<button id='start' class='btn btn-primary' style='width:100%'>Start Task</button>";
    $('#start').onclick = startRun;
  }
}

async function startRun() {
  if (agents.length < 2) {
    toast('Add at least two agents.');
    return;
  }

  $('#chat').innerHTML = '';
  runActive = true;
  setStatus('running');
  buttons(true);

  const params = new URLSearchParams({
    model: $('#model').value,
    goal: btoa($('#goal').value || ''),
    agents: btoa(JSON.stringify(agents)),
    manager_mode: $('#mode').value,
    turns: $('#turns').value,
    temperature: $('#temperature').value
  });

  es = new EventSource(`/stream?${params.toString()}`);
  es.onmessage = ev => {
    if (ev.data === '[DONE]') {
      es.close();
      runActive = false;
      setStatus('idle');
      buttons(false);
      return;
    }

    const d = JSON.parse(ev.data);
    if (d.type === 'status') setStatus(d.state);
    else if (d.type === 'chat') {
        addMsg(d.sender, d.message);
        if (d.is_tool_execution) refreshTree();
    }
    else if (d.type === 'stream_start') addMsg(d.sender, '', false, d.id);
    else if (d.type === 'token') appendToken(d.id, d.delta);
  };
  es.onerror = () => {
    if (!runActive) return;
    if (!es || es.readyState === EventSource.CLOSED) {
      runActive = false;
      setStatus('idle');
      buttons(false);
      toast('Stream closed.');
      return;
    }
    // Ignore transient EventSource reconnect events while run is still active.
    setStatus('running');
  };
}

async function showConfirm({ title, message, okText }) {
  const modal = $('#confirm-modal');
  $('#confirm-title').textContent = title;
  $('#confirm-msg').textContent = message;
  $('#confirm-ok').textContent = okText;
  modal.style.display = 'flex';

  return new Promise(resolve => {
    $('#confirm-cancel').onclick = () => { modal.style.display = 'none'; resolve(false); };
    $('#confirm-ok').onclick = () => { modal.style.display = 'none'; resolve(true); };
  });
}

function saveSession() {
  const s = {
    goal: $('#goal').value,
    agents,
    settings: {
      model: $('#model').value,
      mode: $('#mode').value,
      turns: $('#turns').value,
      temperature: $('#temperature').value
    }
  };
  localStorage.setItem(SESSION_KEY, JSON.stringify(s));
}

function loadSession() {
  const raw = localStorage.getItem(SESSION_KEY);
  if (!raw) return;
  const s = JSON.parse(raw);
  agents = s.agents || [];
  if (s.goal) $('#goal').value = s.goal;
  if (s.settings) {
    if (s.settings.mode) $('#mode').value = s.settings.mode;
    if (s.settings.turns) $('#turns').value = s.settings.turns;
    if (s.settings.temperature) $('#temperature').value = s.settings.temperature;
  }
}

function getSavedSettings() {
  const raw = localStorage.getItem(SESSION_KEY);
  if (!raw) return {};
  try {
    const parsed = JSON.parse(raw);
    return parsed?.settings || {};
  } catch {
    return {};
  }
}

function applySavedSettings() {
  const settings = getSavedSettings();
  if (settings.mode) $('#mode').value = settings.mode;
  if (settings.turns) $('#turns').value = settings.turns;
  if (settings.temperature) $('#temperature').value = settings.temperature;
}

function clearDraftKeepSettings() {
  const settings = getSavedSettings();
  localStorage.setItem(SESSION_KEY, JSON.stringify({ goal: '', agents: [], settings }));
}

async function loadModels() {
  const r = await fetch('/api/models');
  const models = await r.json();
  const sel = $('#model');
  sel.innerHTML = models.map(m => `<option>${m}</option>`).join('');
  $('#model-count').textContent = `${models.length}`;
  $('#model-hint').textContent = models.length ? `Loaded ${models.length} model options.` : 'No models loaded.';

  const savedSettings = getSavedSettings();
  if (savedSettings.model && models.includes(savedSettings.model)) {
    sel.value = savedSettings.model;
  } else if (models.includes('gemini-2.0-flash')) {
    sel.value = 'gemini-2.0-flash';
  } else if (models.length > 0) {
    sel.value = models[0];
  }
}

async function refreshTree() {
  const r = await fetch('/api/workspace/tree');
  const d = await r.json();
  $('#tree').innerHTML = `<ul>${renderTreeNode(d)}</ul>`;
}

const ICONS = {
  file: `<svg width='18' height='18' viewBox='0 0 24 24' fill='none' stroke='currentColor' stroke-width='2'><path d='M13 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V9z'></path><polyline points='13 2 13 9 20 9'></polyline></svg>`,
  folder: `<svg width='18' height='18' viewBox='0 0 24 24' fill='none' stroke='currentColor' stroke-width='2'><path d='M22 19a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h5l2 3h9a2 2 0 0 1 2 2z'></path></svg>`,
  edit: `<svg width='14' height='14' viewBox='0 0 24 24' fill='none' stroke='currentColor' stroke-width='2'><path d='M11 4H4a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h14a2 2 0 0 0 2-2v-7'></path><path d='M18.5 2.5a2.121 2.121 0 0 1 3 3L12 15l-4 1 1-4 9.5-9.5z'></path></svg>`,
  delete: `<svg width='14' height='14' viewBox='0 0 24 24' fill='none' stroke='currentColor' stroke-width='2'><polyline points='3 6 5 6 21 6'></polyline><path d='M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2'></path><line x1='10' y1='11' x2='10' y2='17'></line><line x1='14' y1='11' x2='14' y2='17'></line></svg>`
};

function renderTreeNode(n) {
  const icon = n.type === 'file' ? ICONS.file : ICONS.folder;
  const name = n.type === 'file'
    ? `<a href='/workspace/${n.path}' target='_blank' class='node-name'>${n.name}</a>`
    : `<span class='node-name'>${n.name}</span>`;
  const actions = `<div class='node-actions'>
    <button class='rename-node' data-path='${n.path}' title='Rename'>${ICONS.edit}</button>
    <button class='delete-node' data-path='${n.path}' title='Delete'>${ICONS.delete}</button>
  </div>`;
  const children = n.children ? `<ul>${n.children.map(renderTreeNode).join('')}</ul>` : '';
  return `<li><div class='node'>${icon}${name}${actions}</div>${children}</li>`;
}

function handleRename(btn) {
  const node = btn.closest('.node');
  const nameEl = node.querySelector('.node-name');
  const oldPath = btn.dataset.path;
  const oldName = nameEl.textContent;

  const input = document.createElement('input');
  input.value = oldName;
  input.className = 'node-name-input';
  nameEl.replaceWith(input);
  input.focus();
  input.select();

  input.onblur = async () => {
    const newName = input.value.trim();
    if (newName && newName !== oldName) {
      const parts = oldPath.split('/');
      const newPath = [...parts.slice(0, -1), newName].join('/');
      await fetch('/api/workspace/rename', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ old_path: oldPath, new_path: newPath })
      });
    }
    refreshTree();
  };
  input.onkeydown = e => {
    if (e.key === 'Enter') input.blur();
    if (e.key === 'Escape') refreshTree();
  };
}

async function onTreeClick(e) {
  const deleteBtn = e.target.closest('.delete-node');
  const renameBtn = e.target.closest('.rename-node');

  if (deleteBtn) {
    const p = deleteBtn.dataset.path;
    if (await showConfirm({ title: 'Delete Item', message: `Delete ${p}?`, okText: 'Delete' })) {
      await fetch('/api/workspace/delete', {
        method: 'DELETE',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ path: p })
      });
      refreshTree();
    }
  } else if (renameBtn) {
    handleRename(renameBtn);
  }
}

async function saveCurrentSessionToServer() {
  const name = $('#session-name').value.trim();
  if (!name) {
    toast('Enter a session name.');
    return;
  }

  const payload = {
    name,
    goal: $('#goal').value,
    agents,
    settings: {
      model: $('#model').value,
      mode: $('#mode').value,
      turns: $('#turns').value,
      temperature: $('#temperature').value
    }
  };

  const r = await fetch('/api/sessions', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload)
  });

  if (r.ok) {
    toast('Session saved.');
    $('#session-name').value = '';
    await openSessionsModal();
  } else {
    toast('Failed to save session.');
  }
}

async function openSessionsModal() {
  $('#sessions-modal').style.display = 'flex';
  const r = await fetch('/api/sessions');
  const sessions = await r.json();
  $('#sessions-list').innerHTML = sessions.map(s => `
    <div style='padding:10px; border-bottom:1px solid var(--border); display:flex; justify-content:space-between;'>
      <span>${s}</span>
      <div>
        <button class='btn btn-neutral session-load' data-file='${s}'>Load</button>
        <button class='btn btn-danger session-delete' data-file='${s}'>x</button>
      </div>
    </div>
  `).join('');
}

async function loadToolsForPlayground() {
  const r = await fetch('/api/tools');
  const tools = await r.json();
  playgroundState.toolNames = tools.map(t => t.name);
  const container = $('#playground-tools');
  if (!container) return;
  container.innerHTML = tools.map(t => `
    <label class='playground-tool-checkbox' style='display:flex; gap:6px; align-items:center; font-size:12px;'>
      <input type='checkbox' value='${t.name}' />
      <span>${t.name}</span>
    </label>
  `).join('');
}

function selectedPlaygroundTools() {
  return $$('#playground-tools input[type="checkbox"]:checked').map(el => el.value);
}

async function runPlaygroundMessage() {
  const input = $('#playground-input');
  const text = input.value.trim();
  if (!text) return;

  addMsg('YOU', text, true, null, '#playground-chat-area');
  input.value = '';

  const payload = {
    message: text,
    model: $('#model').value,
    agent: {
      name: $('#playground-agent-name').value.trim() || 'MyAgent',
      system_message: $('#playground-system-message').value || 'You are a helpful assistant.',
      tools: selectedPlaygroundTools()
    }
  };

  const r = await fetch('/api/agent/run', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload)
  });
  const d = await r.json();
  if (!r.ok) {
    addMsg('System', d.message || d.error || 'Playground request failed.', false, null, '#playground-chat-area');
    return;
  }

  addMsg(payload.agent.name, d.reply || 'No response.', false, null, '#playground-chat-area');
}

function bindTabs() {
  const side = $('.side');
  const overlay = $('#sidebar-overlay');
  const closeSidebar = () => {
    side.classList.remove('open');
    overlay.classList.remove('open');
  };

  $('#sidebar-toggle').onclick = () => {
    side.classList.add('open');
    overlay.classList.add('open');
  };
  overlay.onclick = closeSidebar;
  $('#sidebar-close-mobile').onclick = closeSidebar;

  $$('.tab').forEach(t => {
    t.onclick = async () => {
      $$('.tab').forEach(x => x.classList.remove('active'));
      t.classList.add('active');
      $$('section').forEach(s => s.classList.add('hidden'));

      const target = t.id.split('-')[1];
      $(`#${target}`).classList.remove('hidden');
      const footer = $('.side-footer');
      if (footer) footer.style.display = target === 'setup' ? 'flex' : 'none';
      if (window.innerWidth <= 768) closeSidebar();

      if (target === 'work') await refreshTree();
      if (target === 'playground') await loadToolsForPlayground();
    };
  });
}

async function exportTranscript() {
  const transcript = window.__transcript || [];
  if (!transcript.length) {
    toast('No transcript to export.');
    return;
  }

  const r = await fetch('/api/transcript/md', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ transcript })
  });
  const d = await r.json();
  if (r.ok) {
    toast('Transcript exported.');
    window.open(d.file, '_blank');
  } else {
    toast('Export failed.');
  }
}

document.addEventListener('DOMContentLoaded', async () => {
  bindTabs();

  $('#bot-add').onclick = () => {
    const name = $('#bot-title').value.trim();
    if (!name) return;

    agents.push({
      name: name.replace(/[^a-zA-Z0-9_]/g, '_'),
      system: $('#bot-description').value,
      temperature: 0.3
    });

    $('#bot-title').value = '';
    $('#bot-description').value = '';
    renderTeam();
    saveSession();
  };

  $('#generate-agents').onclick = async () => {
    const scenario = $('#scenario').value.trim();
    if (!scenario) return;

    const btn = $('#generate-agents');
    btn.disabled = true;
    btn.textContent = 'Generating...';

    try {
      const r = await fetch('/api/scenario/generate', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          scenario,
          model: $('#model').value,
          num_agents: $('#num-agents').value
        })
      });
      const d = await r.json();
      if (!r.ok) throw new Error(d.error || 'Generation failed');

      agents = d.agents || [];
      if (d.goal) $('#goal').value = d.goal;
      renderTeam();
      saveSession();
    } catch (e) {
      toast(e.message || 'Failed to generate agents.');
    } finally {
      btn.disabled = false;
      btn.textContent = 'Generate Scenario';
    }
  };

  $('#fbform').onsubmit = async (e) => {
    e.preventDefault();
    if (!runActive) return;
    const value = $('#fb').value.trim();
    if (!value) return;

    addMsg('You', value, true);
    const endpoint = currentStatus === 'waiting_for_input' ? '/user_input' : '/coach_input';
    await fetch(endpoint, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ message: value })
    });
    $('#fb').value = '';
    setStatus('running');
  };

  $('#sessions').onclick = openSessionsModal;
  $('#sessions-close').onclick = () => { $('#sessions-modal').style.display = 'none'; };
  $('#session-save').onclick = saveCurrentSessionToServer;
  $('#sessions-list').onclick = async (e) => {
    const file = e.target.dataset.file;
    if (!file) return;

    if (e.target.classList.contains('session-load')) {
      const r = await fetch(`/api/sessions/${file}`);
      const d = await r.json();
      agents = d.agents || [];
      $('#goal').value = d.goal || '';
      if (d.settings) {
        if (d.settings.model) $('#model').value = d.settings.model;
        if (d.settings.mode) $('#mode').value = d.settings.mode;
        if (d.settings.turns) $('#turns').value = d.settings.turns;
        if (d.settings.temperature) $('#temperature').value = d.settings.temperature;
      }
      renderTeam();
      saveSession();
      $('#sessions-modal').style.display = 'none';
    }

    if (e.target.classList.contains('session-delete')) {
      if (await showConfirm({ title: 'Delete Session', message: `Delete ${file}?`, okText: 'Delete' })) {
        await fetch(`/api/sessions/${file}`, { method: 'DELETE' });
        await openSessionsModal();
      }
    }
  };

  $('#export').onclick = exportTranscript;
  $('#tree').onclick = onTreeClick;
  $('#refresh').onclick = refreshTree;

  $('#mount-workspace').onclick = async () => {
    const path = $('#workspace-mount-path').value.trim();
    if (!path) return;

    const r = await fetch('/api/workspace/set_directory', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ path })
    });

    const d = await r.json();
    if (r.ok) {
      toast('Workspace mounted successfully.');
      await refreshTree();
    } else {
      toast(d.message || d.error || 'Failed to mount workspace.');
    }
  };

  $('#reloadModels').onclick = async () => {
    await loadModels();
    saveSession();
    toast('Models refreshed.');
  };

  $('#new-file').onclick = async () => {
    const p = prompt('File name:');
    if (!p) return;
    await fetch('/api/workspace/file', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ path: p, content: '' })
    });
    await refreshTree();
  };

  $('#new-folder').onclick = async () => {
    const p = prompt('Folder name:');
    if (!p) return;
    await fetch('/api/workspace/new_folder', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ path: p })
    });
    await refreshTree();
  };

  $('#file').onchange = async (e) => {
    const file = e.target.files?.[0];
    if (!file) return;
    const fd = new FormData();
    fd.append('file', file);
    const r = await fetch('/api/workspace/upload', { method: 'POST', body: fd });
    if (r.ok) {
      toast('File uploaded.');
      await refreshTree();
    } else {
      const d = await r.json().catch(() => ({}));
      toast(d.error || 'Upload failed.');
    }
    $('#file').value = '';
  };

  $('#upload').onclick = async () => {
    const input = $('#file');
    if (!input.files?.length) {
      toast('Choose a file first.');
      return;
    }
  };

  $('#playground-form').onsubmit = async (e) => {
    e.preventDefault();
    await runPlaygroundMessage();
  };

  $('#thinkmin').onclick = toggleThinkMinimize;
  $('#thinkclose').onclick = closeThinkDock;

  $('#goal').addEventListener('change', saveSession);
  $('#mode').addEventListener('change', saveSession);
  $('#turns').addEventListener('change', saveSession);
  $('#temperature').addEventListener('change', saveSession);
  $('#model').addEventListener('change', saveSession);

  applySavedSettings();
  clearDraftKeepSettings();
  await loadModels();
  renderTeam();
  setStatus('idle');
  buttons(false);
});
