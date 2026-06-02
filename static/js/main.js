const $ = (s) => document.querySelector(s);
const $$ = (s) => Array.from(document.querySelectorAll(s));

let agents = [];
let streams = {};
let es = null;
let runActive = false;
let runCanResume = false;
let currentStatus = 'idle';
const think = { active: false, buffer: '', open: false, minimized: false };
const playgroundState = { toolNames: [] };
const SESSION_KEY = 'agentStudioSession';
const ACTIVE_RUN_URL_KEY = 'agentStudioActiveRunUrl';
const MODEL_DEFAULT_VERSION_KEY = 'agentStudioModelDefaultVersion';
const MODEL_DEFAULT_VERSION = 'gemini-2.5-flash-lite-max-thinking-v1';
const PREFERRED_MODEL = 'gemini::cloud::gemini-2.5-flash-lite';
const RELATIONSHIP_TYPES = [
  'Boss', 'Employee', 'Coworker', 'Mentor', 'Student', 'Friend', 'Rival', 'Sibling',
  'Parent', 'Child', 'Spouse', 'Partner', 'Ally', 'Enemy', 'Trusted', 'Distrusted',
  'Protector', 'Dependent', 'Advisor', 'Client'
];

function createRunToken() {
  if (typeof globalThis.crypto?.randomUUID === 'function') {
    return globalThis.crypto.randomUUID();
  }
  return `run-${Date.now().toString(36)}-${Math.random().toString(36).slice(2)}`;
}

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

function renderEmptyChat() {
  const chat = $('#chat');
  if (!chat) return;
  chat.innerHTML = `
    <div class="chat-empty">
      <div class="chat-empty-icon" aria-hidden="true">+</div>
      <div class="chat-empty-kicker">Multi-agent workspace</div>
      <h2>Build a team for your next task</h2>
      <p>Describe a scenario, generate specialized agents, and guide their work from one place.</p>
      <button id="empty-open-setup" class="btn btn-primary" type="button">Open setup</button>
    </div>
  `;
  $('#empty-open-setup').onclick = () => {
    $('.side').classList.add('open');
    $('#sidebar-overlay').classList.add('open');
  };
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

function normalizeAgents(rawAgents = []) {
  const roster = (Array.isArray(rawAgents) ? rawAgents : []).map(agent => ({
    ...agent,
    name: String(agent?.name || ''),
    system: String(agent?.system || ''),
    temperature: Number.isFinite(Number(agent?.temperature)) ? Number(agent.temperature) : 0.3,
    tools_enabled: agent?.tools_enabled !== false,
    relationships: Array.isArray(agent?.relationships) ? agent.relationships : []
  }));
  const names = new Set(roster.map(agent => agent.name));
  roster.forEach(agent => {
    agent.relationships = agent.relationships
      .filter(relationship => relationship && typeof relationship === 'object')
      .map(relationship => ({
        target: String(relationship.target || ''),
        relation: String(relationship.relation || '').trim(),
        notes: String(relationship.notes || '')
      }))
      .filter(relationship => relationship.target && relationship.relation && relationship.target !== agent.name && names.has(relationship.target));
  });
  return roster;
}

function relationshipTypeOptions(relation) {
  const custom = relation && !RELATIONSHIP_TYPES.includes(relation);
  return [...RELATIONSHIP_TYPES, 'Custom'].map(type => (
    `<option value='${escapeHtml(type)}' ${type === (custom ? 'Custom' : relation) ? 'selected' : ''}>${escapeHtml(type)}</option>`
  )).join('');
}

function relationshipTargetOptions(agentIndex, target) {
  return agents.map((agent, index) => index === agentIndex ? '' : (
    `<option value='${escapeHtml(agent.name)}' ${agent.name === target ? 'selected' : ''}>${escapeHtml(agent.name)}</option>`
  )).join('');
}

function normalizeManagerMode(mode) {
  const normalized = String(mode || '').toLowerCase().replace(/[\s_-]/g, '');
  if (normalized === 'auto' || normalized === 'smartsupervisor') return 'smart_supervisor';
  if (normalized === 'roundrobin') return 'round_robin';
  if (normalized === 'manual') return 'manual';
  return 'smart_supervisor';
}

function normalizeConversationMode(mode) {
  const normalized = String(mode || 'discussion').toLowerCase();
  return ['discussion', 'debate', 'brainstorm', 'execution', 'simulation', 'storybook'].includes(normalized)
    ? normalized
    : 'discussion';
}

function updateConversationModeHint() {
  const hints = {
    discussion: 'Natural multi-agent conversation for exploring a topic.',
    debate: 'Pressure-test ideas with challenges, counterarguments, and productive disagreement.',
    brainstorm: 'Generate many diverse ideas before spending time on criticism.',
    execution: 'Focus on concrete deliverables, tool use, and completing the work.',
    simulation: 'Stay immersed in the scenario and act as real participants.',
    storybook: 'Advance an engaging narrative with continuity, character, and drama.'
  };
  $('#conversation-mode-hint').textContent = hints[normalizeConversationMode($('#conversation-mode').value)];
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
    paused: 'Status: Simulation paused',
    running: 'Status: Agents working...',
    supervising: 'Status: Supervisor choosing next speaker...',
    waiting_for_input: 'Status: Waiting for input...',
    waiting_for_manual_selection: 'Status: Waiting for speaker selection...'
  };
  status.textContent = map[state] || 'Status';
  status.classList.toggle('pulse', ['running', 'supervising'].includes(state));

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

  agents = normalizeAgents(agents);
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
        <label class='agent-tools-toggle'><input data-i='${i}' class='agent-tools' type='checkbox' ${a.tools_enabled ? 'checked' : ''} /> Allow Tools</label>
        <label>Temp</label>
        <input data-i='${i}' class='num' type='number' min='0' max='2' step='0.1' value='${escapeHtml(a.temperature ?? 0.3)}' />
      </div>
      <div class='relationships-panel'>
        <div class='relationships-header'>
          <span>Relationships</span>
          <button data-i='${i}' class='btn btn-neutral relationship-add' type='button'>+ Add Relationship</button>
        </div>
        <div class='relationships-list'>
          ${(a.relationships || []).map((relationship, relationshipIndex) => {
            const custom = !RELATIONSHIP_TYPES.includes(relationship.relation);
            return `
              <div class='relationship-row'>
                <select data-i='${i}' data-r='${relationshipIndex}' class='relationship-relation' aria-label='Relationship type'>
                  ${relationshipTypeOptions(relationship.relation)}
                </select>
                <select data-i='${i}' data-r='${relationshipIndex}' class='relationship-target' aria-label='Relationship target'>
                  ${relationshipTargetOptions(i, relationship.target)}
                </select>
                <input data-i='${i}' data-r='${relationshipIndex}' class='relationship-custom ${custom ? '' : 'hidden'}' value='${escapeHtml(custom && relationship.relation !== 'Custom' ? relationship.relation : '')}' placeholder='Custom relation...' />
                <textarea data-i='${i}' data-r='${relationshipIndex}' class='relationship-notes' placeholder='Optional relationship notes...'>${escapeHtml(relationship.notes || '')}</textarea>
                <button data-i='${i}' data-r='${relationshipIndex}' class='btn btn-danger relationship-remove' type='button' title='Remove relationship'>x</button>
              </div>
            `;
          }).join('')}
        </div>
      </div>
    </div>
  `).join('');

  $$('.name').forEach(el => {
    el.onchange = () => {
      beginNewSetup();
      const agent = agents[+el.dataset.i];
      const oldName = agent.name;
      agent.name = el.value.trim();
      agents.forEach(item => item.relationships.forEach(relationship => {
        if (relationship.target === oldName) relationship.target = agent.name;
      }));
      renderTeam();
      saveSession();
    };
  });
  $$('.sys').forEach(el => {
    el.onchange = () => { beginNewSetup(); agents[+el.dataset.i].system = el.value; saveSession(); };
  });
  $$('.num').forEach(el => {
    el.onchange = () => { beginNewSetup(); agents[+el.dataset.i].temperature = parseFloat(el.value || '0.3'); saveSession(); };
  });
  $$('.agent-tools').forEach(el => {
    el.onchange = () => { beginNewSetup(); agents[+el.dataset.i].tools_enabled = el.checked; saveSession(); };
  });
  $$('.relationship-add').forEach(el => {
    el.onclick = () => {
      beginNewSetup();
      const i = +el.dataset.i;
      const target = agents.find((_, index) => index !== i)?.name;
      if (!target) {
        toast('Add another agent before creating a relationship.');
        return;
      }
      agents[i].relationships.push({ target, relation: 'Coworker', notes: '' });
      renderTeam();
      saveSession();
    };
  });
  $$('.relationship-remove').forEach(el => {
    el.onclick = () => {
      beginNewSetup();
      agents[+el.dataset.i].relationships.splice(+el.dataset.r, 1);
      renderTeam();
      saveSession();
    };
  });
  $$('.relationship-target').forEach(el => {
    el.onchange = () => { beginNewSetup(); agents[+el.dataset.i].relationships[+el.dataset.r].target = el.value; saveSession(); };
  });
  $$('.relationship-relation').forEach(el => {
    el.onchange = () => {
      beginNewSetup();
      const relationship = agents[+el.dataset.i].relationships[+el.dataset.r];
      relationship.relation = el.value;
      renderTeam();
      saveSession();
    };
  });
  $$('.relationship-custom').forEach(el => {
    el.oninput = () => {
      beginNewSetup();
      agents[+el.dataset.i].relationships[+el.dataset.r].relation = el.value.trim() || 'Custom';
      saveSession();
    };
  });
  $$('.relationship-notes').forEach(el => {
    el.oninput = () => { beginNewSetup(); agents[+el.dataset.i].relationships[+el.dataset.r].notes = el.value; saveSession(); };
  });
  $$('.up').forEach(el => {
    el.onclick = () => {
      beginNewSetup();
      const i = +el.dataset.i;
      if (i <= 0) return;
      [agents[i - 1], agents[i]] = [agents[i], agents[i - 1]];
      renderTeam();
      saveSession();
    };
  });
  $$('.down').forEach(el => {
    el.onclick = () => {
      beginNewSetup();
      const i = +el.dataset.i;
      if (i >= agents.length - 1) return;
      [agents[i + 1], agents[i]] = [agents[i], agents[i + 1]];
      renderTeam();
      saveSession();
    };
  });
  $$('.rm').forEach(el => {
    el.onclick = () => {
      beginNewSetup();
      agents.splice(+el.dataset.i, 1);
      agents = normalizeAgents(agents);
      renderTeam();
      saveSession();
    };
  });
}

function buttons(running, resumable = false) {
  const actions = $('#actions');
  if (!actions) return;

  if (running) {
    actions.innerHTML = "<button id='stop' class='btn btn-danger' style='width:100%'>Stop Task</button>";
    $('#stop').onclick = stopSimulation;
  } else if (resumable) {
    actions.innerHTML = `
      <button id='resume' class='btn btn-success' style='width:100%'>Resume Task</button>
      <button id='new-task' class='btn btn-neutral' style='width:100%'>New Task</button>
    `;
    $('#resume').onclick = resumeSimulation;
    $('#new-task').onclick = startNewTask;
  } else {
    actions.innerHTML = "<button id='start' class='btn btn-primary' style='width:100%'>Start Task</button>";
    $('#start').onclick = startRun;
  }
  $('#stop-run')?.classList.toggle('hidden', !running);
  $('#resume-run')?.classList.toggle('hidden', !resumable || running);
}

function beginNewSetup() {
  if (runActive || !runCanResume) return;
  localStorage.removeItem(ACTIVE_RUN_URL_KEY);
  runCanResume = false;
  window.__transcript = [];
  renderEmptyChat();
  setStatus('idle');
  buttons(false);
}

function clearDraftSetup() {
  localStorage.removeItem(SESSION_KEY);
  agents = [];
  $('#scenario').value = '';
  $('#goal').value = '';
}

function startNewTask() {
  beginNewSetup();
  clearDraftSetup();
  renderTeam();
  renderEmptyChat();
  closeThinkDock();
  toast('Ready for a new task.');
}

async function stopSimulation() {
  if (es) es.close();
  await fetch('/stop', { method: 'POST' });
  runActive = false;
  runCanResume = true;
  setStatus('paused');
  buttons(false, true);
  closeThinkDock();
}

async function resumeSimulation() {
  const url = localStorage.getItem(ACTIVE_RUN_URL_KEY);
  if (!url) {
    runCanResume = false;
    setStatus('idle');
    buttons(false);
    toast('No stopped simulation is available to resume.');
    return;
  }
  const reconnectUrl = new URL(url, window.location.origin);
  const runToken = reconnectUrl.searchParams.get('run_token');
  const response = await fetch('/api/run/resume', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ run_token: runToken })
  });
  if (!response.ok) {
    const payload = await response.json().catch(() => ({}));
    toast(payload.user_message || 'The simulation is still stopping. Try resume again shortly.');
    return;
  }
  $('#chat').innerHTML = '';
  runCanResume = false;
  reconnectUrl.searchParams.set('resume_only', 'true');
  connectRunStream(`${reconnectUrl.pathname}?${reconnectUrl.searchParams.toString()}`);
}

function connectRunStream(url) {
  if (es) es.close();
  runActive = true;
  setStatus('running');
  buttons(true);
  es = new EventSource(url);
  es.onmessage = ev => {
    if (ev.data === '[DONE]') {
      es.close();
      runActive = false;
      if (runCanResume) {
        setStatus('paused');
        buttons(false, true);
      } else {
        localStorage.removeItem(ACTIVE_RUN_URL_KEY);
        setStatus('idle');
        buttons(false);
      }
      return;
    }

    const d = JSON.parse(ev.data);
    if (d.type === 'status') {
      if (d.state === 'error') {
        const errMsg = d.user_message || d.message || "Unknown error";
        toast(`Error: ${errMsg}`);
        addMsg('System', `**Error:** ${errMsg}`);
        es.close();
        localStorage.removeItem(ACTIVE_RUN_URL_KEY);
        runActive = false;
        setStatus('idle');
        buttons(false);
      } else {
        setStatus(d.state);
      }
    }
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
    setStatus('reconnecting');
  };
}

async function startRun() {
  if (agents.length < 2) {
    toast('Add at least two agents.');
    return;
  }

  $('#chat').innerHTML = '';
  runCanResume = false;
  const params = new URLSearchParams({
    model: $('#model').value,
    goal: btoa($('#goal').value || ''),
    agents: btoa(JSON.stringify(agents)),
    manager_mode: $('#mode').value,
    conversation_mode: $('#conversation-mode').value,
    turns: $('#turns').value,
    temperature: $('#temperature').value,
    allow_tools: $('#allow-tools').checked,
    human_proxy_mode: humanProxyMode(),
    human_proxy_preferences: $('#human-proxy-preferences').value,
    run_token: createRunToken()
  });
  const url = `/stream?${params.toString()}`;
  localStorage.setItem(ACTIVE_RUN_URL_KEY, url);
  connectRunStream(url);
}

async function reconnectActiveRun() {
  const url = localStorage.getItem(ACTIVE_RUN_URL_KEY);
  if (!url) return false;
  $('#chat').innerHTML = '';
  const reconnectUrl = new URL(url, window.location.origin);
  const runToken = reconnectUrl.searchParams.get('run_token');
  const statusResponse = await fetch(`/api/run/status?run_token=${encodeURIComponent(runToken || '')}`);
  const status = await statusResponse.json();
  if (!status.available) {
    localStorage.removeItem(ACTIVE_RUN_URL_KEY);
    return false;
  }
  loadSession();
  renderTeam();
  runCanResume = Boolean(status.resumable);
  reconnectUrl.searchParams.set('resume_only', 'true');
  connectRunStream(`${reconnectUrl.pathname}?${reconnectUrl.searchParams.toString()}`);
  return true;
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
    scenario: $('#scenario').value,
    goal: $('#goal').value,
    agents,
    settings: currentRunSettings()
  };
  localStorage.setItem(SESSION_KEY, JSON.stringify(s));
}

function currentRunSettings() {
  return {
    model: $('#model').value,
    mode: $('#mode').value,
    conversation_mode: $('#conversation-mode').value,
    turns: $('#turns').value,
    temperature: $('#temperature').value,
    allow_tools: $('#allow-tools').checked,
    human_proxy_mode: humanProxyMode(),
    human_proxy_preferences: $('#human-proxy-preferences').value
  };
}

function applyRunSettings(settings = {}) {
  if (settings.model) $('#model').value = settings.model;
  if (settings.mode) $('#mode').value = normalizeManagerMode(settings.mode);
  $('#conversation-mode').value = normalizeConversationMode(settings.conversation_mode);
  if (settings.turns) $('#turns').value = settings.turns;
  if (settings.temperature) $('#temperature').value = settings.temperature;
  if (settings.allow_tools !== undefined) $('#allow-tools').checked = settings.allow_tools;
  setHumanProxyMode(settings.human_proxy_mode);
  updateConversationModeHint();
  if (settings.human_proxy_preferences !== undefined) {
    $('#human-proxy-preferences').value = settings.human_proxy_preferences;
  }
}

function loadSession() {
  const raw = localStorage.getItem(SESSION_KEY);
  if (!raw) return;
  let s;
  try {
    s = JSON.parse(raw);
  } catch {
    return;
  }
  agents = normalizeAgents(s.agents);
  $('#scenario').value = s.scenario || '';
  $('#goal').value = s.goal || '';
  applyRunSettings(s.settings);
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

function renderModelOptions(models, { preserveSelection = false } = {}) {
  const sel = $('#model');
  const previousSelection = preserveSelection ? sel.value : '';
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
  if (previousSelection && models.some(model => model.value === previousSelection)) {
    sel.value = previousSelection;
  }
  return sel;
}

async function loadOllamaModels(cloudModels, selectedModel) {
  $('#model-hint').textContent = 'Google models ready. Checking Ollama servers in the background...';
  try {
    const r = await fetch('/api/models/ollama');
    const ollamaModels = await r.json();
    const models = [...cloudModels, ...(Array.isArray(ollamaModels) ? ollamaModels : [])];
    const sel = renderModelOptions(models, { preserveSelection: true });
    if (selectedModel && models.some(model => model.value === selectedModel)) sel.value = selectedModel;
    $('#model-count').textContent = `${models.length}`;
    $('#model-hint').textContent = ollamaModels.length
      ? `Loaded ${models.length} model options, including Ollama.`
      : `Loaded ${models.length} Google model options. No Ollama servers responded.`;
  } catch {
    $('#model-hint').textContent = `Loaded ${cloudModels.length} Google model options. Ollama discovery failed.`;
  }
}

async function loadModels() {
  const generateButton = $('#generate-agents');
  if (generateButton) {
    generateButton.disabled = true;
    generateButton.textContent = 'Loading Models...';
  }
  $('#model-hint').textContent = 'Loading Google models...';
  const r = await fetch('/api/models');
  const models = await r.json();
  const sel = renderModelOptions(models);
  $('#model-count').textContent = `${models.length}`;
  $('#model-hint').textContent = models.length ? `Loaded ${models.length} Google model options.` : 'No Google models loaded.';

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
  if (generateButton) {
    generateButton.disabled = !sel.value;
    generateButton.textContent = 'Generate Scenario';
  }
  void loadOllamaModels(models, sel.value);
}

async function loadOllamaSettings() {
  const r = await fetch('/api/settings/ollama');
  if (r.ok) {
    const d = await r.json();
    if ($('#ollama-local-url')) $('#ollama-local-url').value = d.local_url || '';
    if ($('#ollama-remote-url')) $('#ollama-remote-url').value = d.remote_url || '';
  }
}

async function rollScenarioIdea() {
  const button = $('#random-scenario');
  const status = $('#scenario-idea-status');
  if (!button || button.disabled) return;
  button.disabled = true;
  button.classList.add('rolling');
  if (status) {
    status.textContent = 'Generating a fresh idea...';
    status.classList.add('generating');
  }
  try {
    const r = await fetch('/api/scenario/idea', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ model: $('#model').value })
    });
    const d = await r.json();
    if (!r.ok) throw new Error(d.user_message || d.error || 'Idea generation failed.');

    beginNewSetup();
    agents = [];
    $('#scenario').value = d.scenario || '';
    $('#goal').value = '';
    renderTeam();
    saveSession();
    if (status) status.textContent = 'New idea ready. Tap the dice again to explore another.';
  } catch (e) {
    toast(e.message || 'Failed to generate a scenario idea.');
    if (status) status.textContent = 'Idea generation failed. Tap the dice to try again.';
  } finally {
    button.disabled = false;
    button.classList.remove('rolling');
    if (status) status.classList.remove('generating');
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

async function saveLibraryItem(artifactType) {
  const name = $('#session-name').value.trim();
  if (!name) {
    toast('Enter a name for this saved item.');
    return;
  }
  if (!agents.length) {
    toast('Add at least one agent before saving.');
    return;
  }

  const payload = {
    name,
    artifact_type: artifactType,
    agents,
  };
  if (artifactType === 'scenario') {
    payload.scenario = $('#scenario').value;
    payload.goal = $('#goal').value;
    payload.settings = currentRunSettings();
  }

  const r = await fetch('/api/sessions', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload)
  });

  if (r.ok) {
    toast(artifactType === 'group' ? 'Agent group saved.' : 'Scenario saved.');
    $('#session-name').value = '';
    await openSessionsModal();
  } else {
    const d = await r.json().catch(() => ({}));
    toast(d.user_message || 'Failed to save item.');
  }
}

function normalizeSavedArtifact(file, payload) {
  const value = payload?.data && !payload.agents ? payload.data : payload;
  const artifactType = value?.artifact_type === 'group' ? 'group' : 'scenario';
  return {
    file,
    artifactType,
    name: value?.name || file.replace(/\.json$/i, ''),
    payload: value || {}
  };
}

function renderSavedLibrary(items) {
  const list = $('#sessions-list');
  if (!items.length) {
    list.innerHTML = "<div class='library-empty'>No saved scenarios or groups yet.</div>";
    return;
  }
  list.innerHTML = items.map(item => {
    const action = item.artifactType === 'group' ? 'Use Group' : 'Load Scenario';
    return `
      <div class='library-item'>
        <div>
          <div class='library-item-kind'>${escapeHtml(item.artifactType)}</div>
          <div class='library-item-name'>${escapeHtml(item.name)}</div>
        </div>
        <div class='library-item-actions'>
          <button class='btn btn-neutral session-load' data-file='${escapeHtml(item.file)}'>${action}</button>
          <button class='btn btn-danger session-delete' data-file='${escapeHtml(item.file)}'>Delete</button>
        </div>
      </div>
    `;
  }).join('');
}

async function openSessionsModal() {
  $('#sessions-modal').style.display = 'flex';
  const r = await fetch('/api/sessions');
  const sessions = await r.json();
  if (!r.ok || !Array.isArray(sessions)) {
    renderSavedLibrary([]);
    toast(sessions.user_message || 'Failed to load saved library.');
    return;
  }
  const items = await Promise.all(sessions.map(async file => {
    const response = await fetch(`/api/sessions/${encodeURIComponent(file)}`);
    const payload = await response.json();
    return normalizeSavedArtifact(file, payload);
  }));
  renderSavedLibrary(items);
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

    beginNewSetup();
    agents.push({
      name: name.replace(/[^a-zA-Z0-9_]/g, '_'),
      system: $('#bot-description').value,
      temperature: 0.3,
      tools_enabled: true,
      relationships: []
    });

    $('#bot-title').value = '';
    $('#bot-description').value = '';
    renderTeam();
    saveSession();
  };

  $('#random-scenario').onclick = rollScenarioIdea;

  $('#generate-agents').onclick = async () => {
    const scenario = $('#scenario').value.trim();
    if (!scenario) return;
    const selectedModel = $('#model').value;
    if (!selectedModel) {
      toast('Models are still loading. Try again in a moment.');
      return;
    }

    beginNewSetup();
    const btn = $('#generate-agents');
    btn.disabled = true;
    btn.textContent = 'Generating...';

    try {
      const r = await fetch('/api/scenario/generate', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          scenario,
          model: selectedModel,
          num_agents: parseInt($('#num-agents').value, 10),
          conversation_mode: $('#conversation-mode').value
        })
      });
      const d = await r.json();
      if (!r.ok) throw new Error(d.user_message || d.error || 'Generation failed');

      agents = normalizeAgents(d.agents);
      if (d.goal) $('#goal').value = d.goal;
      renderTeam();
      saveSession();
    } catch (e) {
      toast(e.message || 'Failed to generate agents.');
    } finally {
      btn.disabled = !$('#model').value;
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
  $('#scenario-save').onclick = () => saveLibraryItem('scenario');
  $('#group-save').onclick = () => saveLibraryItem('group');
  $('#sessions-list').onclick = async (e) => {
    const file = e.target.dataset.file;
    if (!file) return;

    if (e.target.classList.contains('session-load')) {
      const r = await fetch(`/api/sessions/${encodeURIComponent(file)}`);
      const raw = await r.json();
      if (!r.ok) {
        toast(raw.user_message || 'Failed to load saved item.');
        return;
      }
      const item = normalizeSavedArtifact(file, raw);
      const d = item.payload;
      beginNewSetup();
      agents = normalizeAgents(d.agents);
      if (item.artifactType === 'scenario') {
        $('#scenario').value = d.scenario || '';
        $('#goal').value = d.goal || '';
        applyRunSettings(d.settings);
      }
      renderTeam();
      saveSession();
      $('#sessions-modal').style.display = 'none';
      toast(item.artifactType === 'group' ? 'Agent group loaded. Current task preserved.' : 'Scenario loaded.');
    }

    if (e.target.classList.contains('session-delete')) {
      if (await showConfirm({ title: 'Delete Session', message: `Delete ${file}?`, okText: 'Delete' })) {
        await fetch(`/api/sessions/${encodeURIComponent(file)}`, { method: 'DELETE' });
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
  $('#resume-run').onclick = resumeSimulation;
  $('#jump-latest').onclick = () => scrollChatToLatest($('#chat'));

  $('#scenario').addEventListener('input', beginNewSetup);
  $('#scenario').addEventListener('change', saveSession);
  $('#goal').addEventListener('input', beginNewSetup);
  $('#goal').addEventListener('change', saveSession);
  $('#mode').addEventListener('change', saveSession);
  $('#conversation-mode').addEventListener('change', () => {
    updateConversationModeHint();
    saveSession();
  });
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

  await loadOllamaSettings();
  if (!await reconnectActiveRun()) {
    clearDraftSetup();
    renderTeam();
    renderEmptyChat();
    setStatus('idle');
    buttons(false);
  }
  $('#random-scenario').disabled = false;
  await loadModels();
  renderTeam();
  updateHumanProxyPreferencesVisibility();
  updateConversationModeHint();
});
