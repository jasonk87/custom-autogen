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
const MODEL_DEFAULT_VERSION_KEY = 'agentStudioModelDefaultVersion';
const MODEL_DEFAULT_VERSION = 'gemini-2.5-flash-lite-max-thinking-v1';
const PREFERRED_MODEL = 'gemini::cloud::gemini-2.5-flash-lite';

function escapeHtml(value) {
  return String(value ?? '')
    .replaceAll('&', '&amp;')
    .replaceAll('<', '&lt;')
    .replaceAll('>', '&gt;')
    .replaceAll('"', '&quot;')
    .replaceAll("'", '&#39;');
}

function inlineMarkdown(value) {
  return escapeHtml(value)
    .replace(/`([^`]+)`/g, '<code>$1</code>')
    .replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>');
}

function safeUrl(value, { allowWorkspace = false } = {}) {
  const raw = String(value ?? '').trim();
  if (allowWorkspace && raw.startsWith('/workspace/')) return raw;
  try {
    const url = new URL(raw, window.location.origin);
    return ['http:', 'https:'].includes(url.protocol) ? url.href : '';
  } catch {
    return '';
  }
}

function renderMarkdown(value) {
  const lines = String(value ?? '').replace(/\r\n?/g, '\n').split('\n');
  const blocks = [];
  let paragraph = [];
  let listType = null;
  let listItems = [];
  let codeLines = null;

  const flushParagraph = () => {
    if (!paragraph.length) return;
    blocks.push(`<p>${paragraph.map(inlineMarkdown).join('<br>')}</p>`);
    paragraph = [];
  };
  const flushList = () => {
    if (!listItems.length) return;
    blocks.push(`<${listType}>${listItems.map(item => `<li>${inlineMarkdown(item)}</li>`).join('')}</${listType}>`);
    listType = null;
    listItems = [];
  };

  lines.forEach(line => {
    if (line.trim().startsWith('```')) {
      flushParagraph();
      flushList();
      if (codeLines === null) {
        codeLines = [];
      } else {
        blocks.push(`<pre><code>${escapeHtml(codeLines.join('\n'))}</code></pre>`);
        codeLines = null;
      }
      return;
    }
    if (codeLines !== null) {
      codeLines.push(line);
      return;
    }

    const heading = line.match(/^(#{1,3})\s+(.+)$/);
    const image = line.match(/^!\[([^\]]*)\]\(([^)]+)\)$/);
    const unordered = line.match(/^\s*[-*]\s+(.+)$/);
    const ordered = line.match(/^\s*\d+\.\s+(.+)$/);
    if (image) {
      flushParagraph();
      flushList();
      const src = safeUrl(image[2], { allowWorkspace: true });
      if (src) {
        blocks.push(`<figure class="chat-image"><img src="${escapeHtml(src)}" alt="${escapeHtml(image[1] || 'Shared image')}" loading="lazy"><figcaption>${escapeHtml(image[1] || 'Shared image')}</figcaption></figure>`);
      }
    } else if (heading) {
      flushParagraph();
      flushList();
      const level = heading[1].length + 2;
      blocks.push(`<h${level}>${inlineMarkdown(heading[2])}</h${level}>`);
    } else if (unordered || ordered) {
      flushParagraph();
      const nextType = unordered ? 'ul' : 'ol';
      if (listType && listType !== nextType) flushList();
      listType = nextType;
      listItems.push((unordered || ordered)[1]);
    } else if (!line.trim()) {
      flushParagraph();
      flushList();
    } else {
      flushList();
      paragraph.push(line);
    }
  });
  if (codeLines !== null) blocks.push(`<pre><code>${escapeHtml(codeLines.join('\n'))}</code></pre>`);
  flushParagraph();
  flushList();
  return blocks.join('');
}

function appendChatBox(sender, className = '') {
  const chat = $('#chat');
  if (!chat) return null;
  const follow = shouldFollowChat(chat);
  const box = document.createElement('div');
  box.className = `bubble from-them ${className}`.trim();
  const who = document.createElement('div');
  who.className = 'who';
  who.textContent = sender;
  box.append(who);
  chat.append(box);
  if (follow) scrollChatToLatest(chat);
  else revealLatestButton();
  return { chat, box };
}

function addToolRequest(sender, tools) {
  const entry = appendChatBox(sender, 'tool-card tool-request');
  if (!entry) return;
  const content = document.createElement('div');
  content.className = 'tool-card-row';
  const badge = document.createElement('span');
  badge.className = 'tool-badge';
  badge.textContent = 'Tool call';
  const text = document.createElement('span');
  text.textContent = (tools || []).join(', ') || 'Preparing tool';
  content.append(badge, text);
  entry.box.append(content);
}

function addToolResult(sender, results) {
  const entry = appendChatBox(sender, 'tool-card tool-result');
  if (!entry) return;
  const heading = document.createElement('div');
  heading.className = 'tool-card-row';
  const badge = document.createElement('span');
  badge.className = 'tool-badge tool-badge-success';
  badge.textContent = 'Tool result';
  heading.append(badge, document.createTextNode(' Completed'));
  entry.box.append(heading);

  (results || []).forEach(result => {
    if (result && typeof result === 'object' && Array.isArray(result.results)) {
      const meta = document.createElement('div');
      meta.className = 'tool-query';
      meta.textContent = result.query ? `Query: ${result.query}` : 'Sources found';
      entry.box.append(meta);
      const list = document.createElement('div');
      list.className = 'search-results';
      result.results.forEach(item => {
        const link = safeUrl(item.link);
        const card = document.createElement(link ? 'a' : 'div');
        card.className = 'search-result';
        if (link) {
          card.href = link;
          card.target = '_blank';
          card.rel = 'noopener noreferrer';
        }
        const title = document.createElement('strong');
        title.textContent = item.title || link || 'Search result';
        const snippet = document.createElement('span');
        snippet.textContent = item.snippet || '';
        card.append(title, snippet);
        list.append(card);
      });
      entry.box.append(list);
      return;
    }
    const details = document.createElement('details');
    details.className = 'tool-details';
    const summary = document.createElement('summary');
    summary.textContent = 'View output';
    const body = document.createElement('pre');
    body.textContent = typeof result === 'string' ? result : JSON.stringify(result, null, 2);
    details.append(summary, body);
    entry.box.append(details);
  });
  refreshTree();
}

function shouldFollowChat(chat) {
  return chat.scrollHeight - chat.scrollTop - chat.clientHeight < 96;
}

function scrollChatToLatest(chat) {
  chat.scrollTop = chat.scrollHeight;
  $('#jump-latest')?.classList.add('hidden');
}

function revealLatestButton() {
  $('#jump-latest')?.classList.remove('hidden');
}

function workspaceUrl(path) {
  return String(path ?? '')
    .replaceAll('\\', '/')
    .split('/')
    .map(encodeURIComponent)
    .join('/');
}

function humanProxyMode() {
  return $('input[name="human-proxy-mode"]:checked')?.value || 'consult';
}

function setHumanProxyMode(mode) {
  const input = $(`input[name="human-proxy-mode"][value="${mode || 'consult'}"]`);
  if (input) input.checked = true;
  updateHumanProxyPreferencesVisibility();
}

function updateHumanProxyPreferencesVisibility() {
  const group = $('#human-proxy-preferences-group');
  if (!group) return;
  group.classList.toggle('hidden', !['delegate_safe', 'autonomous_workspace'].includes(humanProxyMode()));
}

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
    'font-size:13px',
    'line-height:1.5',
    'max-width:min(420px, calc(100vw - 32px))',
    'box-shadow:0 12px 30px rgba(0,0,0,0.35)'
  ].join(';');
  document.body.appendChild(t);
  setTimeout(() => t.remove(), msg.length > 72 ? 8000 : 2200);
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
  const follow = shouldFollowChat(chat);

  window.__transcript = (window.__transcript || []).concat([{ t: Date.now(), from: sender, text }]);

  const box = document.createElement('div');
  box.className = `bubble ${mine ? 'from-you' : 'from-them'}`;
  const who = document.createElement('div');
  who.className = 'who';
  who.textContent = mine ? 'YOU' : sender;

  const body = document.createElement('div');
  body.className = 'message-body';
  body.innerHTML = renderMarkdown(text);

  box.append(who, body);
  chat.append(box);
  if (follow) scrollChatToLatest(chat);
  else if (target === '#chat') revealLatestButton();

  if (id) streams[id] = { el: body, thinkPhase: false, rawText: text || '' };
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
  const chat = $('#chat');
  const follow = shouldFollowChat(chat);
  if (!stream.thinkPhase) {
    stream.rawText += delta || '';
    stream.el.innerHTML = renderMarkdown(stream.rawText);
  }
  if (follow) scrollChatToLatest(chat);
  else revealLatestButton();
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
        <input data-i='${i}' class='name' value='${escapeHtml(a.name)}' placeholder='Name' />
        <div class='team-item-actions'>
          <button data-i='${i}' class='btn btn-neutral up' title='Move Up'>^</button>
          <button data-i='${i}' class='btn btn-neutral down' title='Move Down'>v</button>
          <button data-i='${i}' class='btn btn-danger rm' title='Remove'>x</button>
        </div>
      </div>
      <textarea data-i='${i}' class='sys' placeholder='System Message'>${escapeHtml(a.system)}</textarea>
      <div class='team-item-footer'>
        <label>Temp</label>
        <input data-i='${i}' class='num' type='number' min='0' max='2' step='0.1' value='${escapeHtml(a.temperature ?? 0.3)}' />
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
    $('#stop').onclick = stopSimulation;
  } else {
    actions.innerHTML = "<button id='start' class='btn btn-primary' style='width:100%'>Start Task</button>";
    $('#start').onclick = startRun;
  }
  $('#stop-run')?.classList.toggle('hidden', !running);
}

async function stopSimulation() {
  if (es) es.close();
  await fetch('/stop', { method: 'POST' });
  runActive = false;
  setStatus('idle');
  buttons(false);
  closeThinkDock();
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
    temperature: $('#temperature').value,
    allow_tools: $('#allow-tools').checked,
    human_proxy_mode: humanProxyMode(),
    human_proxy_preferences: $('#human-proxy-preferences').value
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
    else if (d.type === 'tool_request') addToolRequest(d.sender, d.tools);
    else if (d.type === 'tool_result') addToolResult(d.sender, d.results);
    else if (d.type === 'chat') {
        addMsg(d.sender, d.message);
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
      temperature: $('#temperature').value,
      allow_tools: $('#allow-tools').checked,
      human_proxy_mode: humanProxyMode(),
      human_proxy_preferences: $('#human-proxy-preferences').value
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
    if (s.settings.allow_tools !== undefined) $('#allow-tools').checked = s.settings.allow_tools;
    setHumanProxyMode(s.settings.human_proxy_mode);
    if (s.settings.human_proxy_preferences !== undefined) $('#human-proxy-preferences').value = s.settings.human_proxy_preferences;
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
  if (settings.allow_tools !== undefined) $('#allow-tools').checked = settings.allow_tools;
  setHumanProxyMode(settings.human_proxy_mode);
  if (settings.human_proxy_preferences !== undefined) $('#human-proxy-preferences').value = settings.human_proxy_preferences;
}

function clearDraftKeepSettings() {
  const settings = getSavedSettings();
  localStorage.setItem(SESSION_KEY, JSON.stringify({ goal: '', agents: [], settings }));
}

async function loadModels() {
  const r = await fetch('/api/models');
  const models = await r.json();
  const sel = $('#model');
  
  const groups = {
    'Google': models.filter(m => m.provider === 'gemini'),
    'Local Ollama': models.filter(m => m.source === 'local'),
    'Remote Ollama': models.filter(m => m.source === 'remote'),
  };

  let html = '';
  for (const [groupName, groupModels] of Object.entries(groups)) {
    if (groupModels.length > 0) {
      html += `<optgroup label="${escapeHtml(groupName)}">` + groupModels.map(m => `<option value="${escapeHtml(m.value)}">${escapeHtml(m.label)}</option>`).join('') + `</optgroup>`;
    }
  }

  sel.innerHTML = html;
  $('#model-count').textContent = `${models.length}`;
  $('#model-hint').textContent = models.length ? `Loaded ${models.length} model options.` : 'No models loaded.';

  const savedSettings = getSavedSettings();
  const values = models.map(m => m.value);
  if (localStorage.getItem(MODEL_DEFAULT_VERSION_KEY) !== MODEL_DEFAULT_VERSION && values.includes(PREFERRED_MODEL)) {
    sel.value = PREFERRED_MODEL;
    localStorage.setItem(MODEL_DEFAULT_VERSION_KEY, MODEL_DEFAULT_VERSION);
  } else if (savedSettings.model && values.includes(savedSettings.model)) {
    sel.value = savedSettings.model;
  } else if (values.find(v => v.includes('gemini-2.5-flash-lite'))) {
    sel.value = values.find(v => v.includes('gemini-2.5-flash-lite'));
  } else if (values.length > 0) {
    sel.value = values[0];
  }
}

async function loadOllamaSettings() {
  const r = await fetch('/api/settings/ollama');
  if (r.ok) {
    const d = await r.json();
    if ($('#ollama-local-url')) $('#ollama-local-url').value = d.local_url || '';
    if ($('#ollama-remote-url')) $('#ollama-remote-url').value = d.remote_url || '';
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
    ? `<a href='/workspace/${workspaceUrl(n.path)}' target='_blank' class='node-name'>${escapeHtml(n.name)}</a>`
    : `<span class='node-name'>${escapeHtml(n.name)}</span>`;
  const actions = `<div class='node-actions'>
    <button class='rename-node' data-path='${escapeHtml(n.path)}' title='Rename'>${ICONS.edit}</button>
    <button class='delete-node' data-path='${escapeHtml(n.path)}' title='Delete'>${ICONS.delete}</button>
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
      temperature: $('#temperature').value,
      allow_tools: $('#allow-tools').checked,
      human_proxy_mode: humanProxyMode(),
      human_proxy_preferences: $('#human-proxy-preferences').value
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
      <span>${escapeHtml(s)}</span>
      <div>
        <button class='btn btn-neutral session-load' data-file='${escapeHtml(s)}'>Load</button>
        <button class='btn btn-danger session-delete' data-file='${escapeHtml(s)}'>x</button>
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
      <input type='checkbox' value='${escapeHtml(t.name)}' />
      <span>${escapeHtml(t.name)}</span>
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
  const openSidebar = () => {
    side.classList.add('open');
    overlay.classList.add('open');
  };

  $('#sidebar-toggle').onclick = openSidebar;
  const emptyOpenSetup = $('#empty-open-setup');
  if (emptyOpenSetup) emptyOpenSetup.onclick = openSidebar;
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
          num_agents: parseInt($('#num-agents').value, 10)
        })
      });
      const d = await r.json();
      if (!r.ok) throw new Error(d.user_message || d.error || 'Generation failed');

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
        if (d.settings.allow_tools !== undefined) $('#allow-tools').checked = d.settings.allow_tools;
        setHumanProxyMode(d.settings.human_proxy_mode);
        if (d.settings.human_proxy_preferences !== undefined) $('#human-proxy-preferences').value = d.settings.human_proxy_preferences;
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
  $('#stop-run').onclick = stopSimulation;
  $('#jump-latest').onclick = () => scrollChatToLatest($('#chat'));

  $('#goal').addEventListener('change', saveSession);
  $('#mode').addEventListener('change', saveSession);
  $('#turns').addEventListener('change', saveSession);
  $('#temperature').addEventListener('change', saveSession);
  $('#model').addEventListener('change', saveSession);
  $$('input[name="human-proxy-mode"]').forEach(input => {
    input.addEventListener('change', () => {
      updateHumanProxyPreferencesVisibility();
      saveSession();
    });
  });
  $('#human-proxy-preferences').addEventListener('change', saveSession);

  const saveOllamaBtn = $('#save-ollama-config');
  if (saveOllamaBtn) {
    saveOllamaBtn.onclick = async () => {
      const local_url = $('#ollama-local-url').value.trim();
      const remote_url = $('#ollama-remote-url').value.trim();
      const r = await fetch('/api/settings/ollama', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ local_url, remote_url })
      });
      if (r.ok) {
        toast('Ollama settings saved.');
        await loadModels();
      } else {
        toast('Failed to save Ollama settings.');
      }
    };
  }

  applySavedSettings();
  clearDraftKeepSettings();
  await loadOllamaSettings();
  await loadModels();
  renderTeam();
  updateHumanProxyPreferencesVisibility();
  setStatus('idle');
  buttons(false);
});
