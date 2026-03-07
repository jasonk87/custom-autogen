let $ = (s) => document.querySelector(s);
let $$ = (s) => Array.from(document.querySelectorAll(s));
let agents = []; let streams = {}; let es = null; let manualNames = [];
const think = { active: false, buffer: '', open: false, minimized: false };
const commands = [];
let commandPaletteOpen = false;

function toast(msg) {
  const t = document.createElement('div');
  t.textContent = msg;
  t.style.cssText = `
    position: fixed; right: 32px; bottom: 32px; background: var(--glass-solid); color: var(--text); border: 1px solid var(--primary); padding: 14px 24px; border-radius: 12px; box-shadow: 0 10px 40px rgba(0,0,0,0.5), 0 0 10px var(--primary-glow); font-size: 14px; z-index: 10000; font-weight: 600; backdrop-filter: blur(12px); animation: messageIn 0.3s cubic-bezier(0.16, 1, 0.3, 1);
  `;
  document.body.appendChild(t);
  setTimeout(() => {
    t.style.opacity = '0'; t.style.transform = 'translateY(10px)'; t.style.transition = 'all 0.4s ease';
    setTimeout(() => t.remove(), 400);
  }, 3000);
}

function setStatus(state) {
  const S = $('#status');
  if (!S) return;
  const map = { idle: 'Status: Idle', running: 'Status: Agents working…', waiting_for_input: 'Status: Waiting for input…' };
  S.textContent = map[state] || 'Status';
  if (state === 'running') S.classList.add('pulse'); else S.classList.remove('pulse');
  const fb = $('#fb'); const send = $('#send');
  if (fb) fb.disabled = state !== 'waiting_for_input';
  if (send) send.disabled = state !== 'waiting_for_input';
  if (state === 'waiting_for_input' && fb) fb.focus();
}

function addMsg(sender, text, mine = false, id = null) {
  window.__transcript = (window.__transcript || []).concat([{ t: Date.now(), from: sender, text }]);
  const chat = $('#chat');
  if (!chat) return;
  const box = document.createElement('div');
  box.className = 'bubble ' + (mine ? 'from-you' : 'from-them');
  const who = document.createElement('div'); who.className = 'who'; who.textContent = mine ? 'YOU' : sender;
  const body = document.createElement('div');
  if (window.marked && text) body.innerHTML = window.marked.parse(text); else body.textContent = text || '';
  box.append(who, body);
  const isAtBottom = chat.scrollHeight - chat.scrollTop - chat.clientHeight < 50;
  chat.append(box);
  if (isAtBottom) chat.scrollTop = chat.scrollHeight;
  if (id) streams[id] = { el: body, thinkPhase: false };
}

function appendToken(id, delta) {
  ensureThinkHandling(id, delta);
  const s = streams[id]; if (!s) { addMsg('assistant', '', false, id); }
  const body = streams[id].el; if (!streams[id].thinkPhase) body.innerHTML += delta.replaceAll('\n', '<br>');
  $('#chat').scrollTop = $('#chat').scrollHeight;
}

function ensureThinkHandling(id, chunk) {
  let c = chunk;
  if (c.includes('<think>') || think.active) {
    if (!think.open) openThinkDock();
    while (c.length) {
      if (!think.active) {
        const i = c.indexOf('<think>'); if (i === -1) break;
        c = c.slice(i + 7); think.active = true;
        if (streams[id]) streams[id].thinkPhase = true;
      } else {
        const j = c.indexOf('</think>');
        if (j === -1) { think.buffer += c; c = ''; break; }
        else {
          think.buffer += c.slice(0, j); c = c.slice(j + 8); think.active = false;
          if (streams[id]) streams[id].thinkPhase = false;
          renderThink();
        }
      }
    }
    renderThink();
  }
}

function openThinkDock() { think.open = true; $('#thinkdock').style.display = 'block'; }
function closeThinkDock() { think.open = false; think.active = false; think.buffer = ''; $('#thinkdock').style.display = 'none'; }
function renderThink() { $('#thinkbody').textContent = think.buffer; $('#thinklen').textContent = think.buffer.length + ' chars'; }

function renderTeam() {
  const container = $('#team'); if (!container) return;
  $('#team-count').textContent = `${agents.length} members`;
  if (!agents.length) {
    container.innerHTML = '<div style="color:var(--muted); padding:16px; text-align:center; font-size:13px; border:1px dashed var(--border); border-radius:6px;">No agents in team.</div>';
    return;
  }
  container.innerHTML = agents.map((a, i) => `
    <div class='team-item'>
      <div class="team-item-header">
        <input data-i='${i}' class='name' value='${a.name}' placeholder='Name' />
        <div class="team-item-actions">
          <button data-i='${i}' class='btn btn-neutral up'>▲</button>
          <button data-i='${i}' class='btn btn-neutral down'>▼</button>
          <button data-i='${i}' class='btn btn-danger rm'>✕</button>
        </div>
      </div>
      <textarea data-i='${i}' class='sys' placeholder='System Message'>${a.system || ""}</textarea>
      <div class="team-item-footer">
        <label>Temp</label>
        <input data-i='${i}' class='num' type='number' step='0.1' min='0' max='2' value='${a.temperature ?? 0.3}' />
      </div>
    </div>`).join('');
  $$('.name').forEach(el => el.onchange = () => { agents[el.dataset.i].name = el.value; saveSession(); });
  $$('.sys').forEach(el => el.onchange = () => { agents[el.dataset.i].system = el.value; saveSession(); });
  $$('.num').forEach(el => el.onchange = () => { agents[el.dataset.i].temperature = parseFloat(el.value); saveSession(); });
  $$('.up').forEach(el => el.onclick = () => { let i = +el.dataset.i; if (i > 0) { [agents[i - 1], agents[i]] = [agents[i], agents[i - 1]]; renderTeam(); saveSession(); } });
  $$('.down').forEach(el => el.onclick = () => { let i = +el.dataset.i; if (i < agents.length - 1) { [agents[i + 1], agents[i]] = [agents[i], agents[i + 1]]; renderTeam(); saveSession(); } });
  $$('.rm').forEach(el => el.onclick = () => { agents.splice(+el.dataset.i, 1); renderTeam(); saveSession(); });
}

function buttons(running) {
  const A = $('#actions'); if (!A) return;
  if (running) {
    A.innerHTML = `<button id='stop' class='btn btn-danger' style="width:100%">Stop Task</button>`;
    $('#stop').onclick = async () => {
      if (es) es.close(); await fetch('/stop', { method: 'POST' });
      setStatus('idle'); buttons(false); closeThinkDock();
    };
  } else {
    A.innerHTML = `<button id='start' class='btn btn-primary' style='width:100%'>Start Task</button>`;
    $('#start').onclick = startRun;
  }
}

async function startRun() {
  if (agents.length < 2) { toast('Add at least two agents.'); return; }
  $('#chat').innerHTML = ''; setStatus('running'); buttons(true);
  const p = new URLSearchParams({ model: $('#model').value, goal: btoa($('#goal').value), agents: btoa(JSON.stringify(agents)), manager_mode: $('#mode').value, turns: $('#turns').value, temperature: $('#temperature').value });
  es = new EventSource('/stream?' + p.toString());
  es.onmessage = ev => {
    if (ev.data === '[DONE]') { es.close(); setStatus('idle'); buttons(false); return; }
    const d = JSON.parse(ev.data);
    if (d.type === 'status') setStatus(d.state);
    else if (d.type === 'chat') addMsg(d.sender, d.message);
    else if (d.type === 'stream_start') addMsg(d.sender, '', false, d.id);
    else if (d.type === 'token') appendToken(d.id, d.delta);
  };
}

const ICONS = {
  file: `<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M13 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V9z"></path><polyline points="13 2 13 9 20 9"></polyline></svg>`,
  folder: `<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M22 19a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h5l2 3h9a2 2 0 0 1 2 2z"></path></svg>`,
  edit: `<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M11 4H4a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h14a2 2 0 0 0 2-2v-7"></path><path d="M18.5 2.5a2.121 2.121 0 0 1 3 3L12 15l-4 1 1-4 9.5-9.5z"></path></svg>`,
  delete: `<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polyline points="3 6 5 6 21 6"></polyline><path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"></path><line x1="10" y1="11" x2="10" y2="17"></line><line x1="14" y1="11" x2="14" y2="17"></line></svg>`
};

function renderTreeNode(n) {
  const icon = n.type === 'file' ? ICONS.file : ICONS.folder;
  const name = n.type === 'file' ? `<a href="/workspace/${n.path}" target="_blank" class="node-name">${n.name}</a>` : `<span class="node-name">${n.name}</span>`;
  const actions = `<div class="node-actions">
    <button class="rename-node" data-path="${n.path}" title="Rename">${ICONS.edit}</button>
    <button class="delete-node" data-path="${n.path}" title="Delete">${ICONS.delete}</button>
  </div>`;
  let children = n.children ? `<ul>${n.children.map(renderTreeNode).join('')}</ul>` : '';
  return `<li><div class="node">${icon}${name}${actions}</div>${children}</li>`;
}

async function refreshTree() {
  const r = await fetch('/api/workspace/tree');
  const d = await r.json();
  $('#tree').innerHTML = `<ul>${renderTreeNode(d)}</ul>`;
  // We'll use event delegation on #tree in DOMContentLoaded
}

async function onTreeClick(e) {
  const deleteBtn = e.target.closest('.delete-node');
  const renameBtn = e.target.closest('.rename-node');
  if (deleteBtn) {
    const p = deleteBtn.dataset.path;
    if (await showConfirm({ title: 'Delete Item', message: `Delete ${p}?`, okText: 'Delete' })) {
      await fetch('/api/workspace/delete', { method: 'DELETE', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ path: p }) });
      refreshTree();
    }
  } else if (renameBtn) handleRename(renameBtn);
}

function handleRename(btn) {
  const node = btn.closest('.node'); const nameEl = node.querySelector('.node-name');
  const oldPath = btn.dataset.path; const oldName = nameEl.textContent;
  const input = document.createElement('input'); input.value = oldName; input.className = 'node-name-input';
  nameEl.replaceWith(input); input.focus(); input.select();
  input.onblur = async () => {
    const newName = input.value.trim();
    if (newName && newName !== oldName) {
      const parts = oldPath.split('/'); const newPath = [...parts.slice(0, -1), newName].join('/');
      await fetch('/api/workspace/rename', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ old_path: oldPath, new_path: newPath }) });
    }
    refreshTree();
  };
  input.onkeydown = e => { if (e.key === 'Enter') input.blur(); if (e.key === 'Escape') refreshTree(); };
}

async function showConfirm({ title, message, okText }) {
  const m = $('#confirm-modal'); $('#confirm-title').textContent = title; $('#confirm-msg').textContent = message; $('#confirm-ok').textContent = okText;
  m.style.display = 'flex';
  return new Promise(resolve => {
    $('#confirm-cancel').onclick = () => { m.style.display = 'none'; resolve(false); };
    $('#confirm-ok').onclick = () => { m.style.display = 'none'; resolve(true); };
  });
}

function saveSession() {
  const s = { goal: $('#goal').value, agents, settings: { model: $('#model').value, mode: $('#mode').value, turns: $('#turns').value, temperature: $('#temperature').value } };
  localStorage.setItem('agentStudioSession', JSON.stringify(s));
}

function loadSession() {
  const saved = localStorage.getItem('agentStudioSession'); if (!saved) return;
  const s = JSON.parse(saved); agents = s.agents || [];
  if (s.goal) $('#goal').value = s.goal; if (s.settings) { $('#mode').value = s.settings.mode; $('#turns').value = s.settings.turns; $('#temperature').value = s.settings.temperature; }
}

async function loadModels() {
  const r = await fetch('/api/models'); const models = await r.json();
  const sel = $('#model'); sel.innerHTML = models.map(m => `<option>${m}</option>`).join('');
  const saved = JSON.parse(localStorage.getItem('agentStudioSession') || '{}');
  if (saved.settings?.model && models.includes(saved.settings.model)) sel.value = saved.settings.model;
}

document.addEventListener('DOMContentLoaded', () => {
  const side = $('.side');
  const overlay = $('#sidebar-overlay');
  const closeSidebar = () => { side.classList.remove('open'); overlay.classList.remove('open'); };

  $('#sidebar-toggle').onclick = () => { side.classList.add('open'); overlay.classList.add('open'); };
  overlay.onclick = closeSidebar;
  $('#sidebar-close-mobile').onclick = closeSidebar;

  $$('.tab').forEach(t => t.onclick = () => {
    $$('.tab').forEach(x => x.classList.remove('active')); t.classList.add('active');
    $$('section').forEach(s => s.classList.add('hidden'));
    const target = t.id.split('-')[1]; $(`#${target}`).classList.remove('hidden');
    const footer = $('.side-footer'); if (footer) footer.style.display = (target === 'setup') ? 'flex' : 'none';
    if (window.innerWidth <= 768) closeSidebar();
    if (target === 'work') refreshTree();
  });

  $('#bot-add').onclick = () => {
    const n = $('#bot-title').value.trim(); if (!n) return;
    agents.push({ name: n.replace(/[^a-zA-Z0-9_]/g, '_'), system: $('#bot-description').value, temperature: 0.3 });
    $('#bot-title').value = ''; $('#bot-description').value = ''; renderTeam(); saveSession();
  };

  $('#generate-agents').onclick = async () => {
    const scenario = $('#scenario').value.trim(); if (!scenario) return;
    const btn = $('#generate-agents'); btn.disabled = true; btn.textContent = 'Generating...';
    try {
      const r = await fetch('/api/scenario/generate', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ scenario, model: $('#model').value, num_agents: $('#num-agents').value }) });
      const d = await r.json(); agents = d.agents; if (d.goal) $('#goal').value = d.goal; renderTeam();
    } finally { btn.disabled = false; btn.textContent = 'Generate Scenario'; }
  };

  $('#fbform').onsubmit = async e => {
    e.preventDefault(); const v = $('#fb').value.trim(); if (!v) return;
    addMsg('You', v, true); await fetch('/user_input', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ message: v }) });
    $('#fb').value = ''; setStatus('running');
  };

  $('#sessions').onclick = async () => {
    $('#sessions-modal').style.display = 'flex';
    const r = await fetch('/api/sessions'); const sessions = await r.json();
    $('#sessions-list').innerHTML = sessions.map(s => `<div style="padding:10px; border-bottom:1px solid var(--border); display:flex; justify-content:space-between;"><span>${s}</span><div><button class="btn btn-neutral session-load" data-file="${s}">Load</button> <button class="btn btn-danger session-delete" data-file="${s}">✕</button></div></div>`).join('');
  };

  $('#sessions-list').onclick = async e => {
    const f = e.target.dataset.file;
    if (e.target.classList.contains('session-load')) {
      const r = await fetch(`/api/sessions/${f}`); const d = await r.json();
      agents = d.agents; $('#goal').value = d.goal; renderTeam(); $('#sessions-modal').style.display = 'none';
    } else if (e.target.classList.contains('session-delete')) {
      if (await showConfirm({ title: 'Delete Session', message: `Delete ${f}?`, okText: 'Delete' })) {
        await fetch(`/api/sessions/${f}`, { method: 'DELETE' }); $('#sessions').click();
      }
    }
  };

  $('#export').onclick = () => { toast('Exporting...'); /* Transcript impl */ };
  $('#tree').onclick = onTreeClick;
  $('#refresh').onclick = refreshTree;
  $('#new-folder').onclick = async () => {
    const p = prompt('Folder name:');
    if (p) {
      await fetch('/api/workspace/new_folder', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ path: p }) });
      refreshTree();
    }
  };

  loadSession(); setStatus('idle'); buttons(false); renderTeam(); loadModels();
});
