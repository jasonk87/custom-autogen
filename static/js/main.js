const $=(s)=>document.querySelector(s); const $$=(s)=>Array.from(document.querySelectorAll(s));
  let agents=[]; let streams={}; let es=null; let manualNames=[]; let lastModels=[];

  // Thinking state
  const think = { active:false, buffer:'', open:false, minimized:false };

  function toast(msg){ const t=document.createElement('div'); t.textContent=msg; t.style.cssText='position:fixed;right:12px;bottom:12px;background:#0b213f;color:#cfe4ff;border:1px solid #23406e;padding:10px 12px;border-radius:10px;box-shadow:0 10px 25px rgba(0,0,0,.35)'; document.body.appendChild(t); setTimeout(()=>t.remove(),3000); }

  function setStatus(state){ const S=$('#status'); const map={idle:'Status: Idle', running:'Status: Agents working…', waiting_for_input:'Status: Waiting for input…'}; S.textContent = map[state]||'Status'; $('#fb').disabled = state!=='waiting_for_input'; $('#send').disabled = state!=='waiting_for_input'; }
  function addMsg(sender, text, mine=false, id=null){ const box=document.createElement('div'); box.className='bubble '+(mine?'from-you':'from-them'); const who=document.createElement('div'); who.className='who'; who.textContent=mine?'You':sender; const body=document.createElement('div'); body.textContent=text||''; box.append(who,body); $('#chat').append(box); $('#chat').scrollTop=$('#chat').scrollHeight; if(id) streams[id]={el:body, thinkPhase:false}; }
  function appendToken(id, delta){ ensureThinkHandling(id, delta); const s = streams[id]; if(!s){ addMsg('assistant','',false,id); } const body = streams[id].el; if(!streams[id].thinkPhase){ body.textContent += stripThinkTags(delta); } $('#chat').scrollTop=$('#chat').scrollHeight; }

  // THINKING UI HANDLING
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

  function renderTeam(){ const T=$('#team'); if(!agents.length){T.innerHTML='<div style="color:#9ca3af">Add at least two agents.</div>';return} T.innerHTML=agents.map((a,i)=>`<div class='team-item'> <input data-i='${i}' class='name' value='${a.name}' style='width:120px' /> <input data-i='${i}' class='sys' value='${a.system||""}' placeholder='system message' /> <div style="margin-left:auto; display:flex; gap:4px;"><button data-i='${i}' class='btn btn-neutral up'>▲</button> <button data-i='${i}' class='btn btn-neutral down'>▼</button> <button data-i='${i}' class='btn btn-danger rm'>✕</button></div> </div>`).join(''); $$('.name').forEach(e=>e.onchange=()=>{agents[e.dataset.i].name=e.value;}); $$('.sys').forEach(e=>e.onchange=()=>{agents[e.dataset.i].system=e.value;}); $$('.rm').forEach(e=>e.onclick=()=>{agents.splice(+e.dataset.i,1); renderTeam();}); $$('.up').forEach(e=>e.onclick=()=>{const i=+e.dataset.i; if(i>0){[agents[i-1],agents[i]]=[agents[i],agents[i-1]]; renderTeam();}}); $$('.down').forEach(e=>e.onclick=()=>{const i=+e.dataset.i; if(i<agents.length-1){[agents[i+1],agents[i]]=[agents[i],agents[i+1]]; renderTeam();}}); }

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

  // Tabs
  $('#tab-setup').onclick=()=>{ $('#tab-setup').classList.add('active'); $('#tab-work').classList.remove('active'); $('#tab-settings').classList.remove('active'); $('#setup').classList.remove('hidden'); $('#work').classList.add('hidden'); $('#settings').classList.add('hidden'); };
  $('#tab-work').onclick=()=>{ $('#tab-work').classList.add('active'); $('#tab-setup').classList.remove('active'); $('#tab-settings').classList.remove('active'); $('#work').classList.remove('hidden'); $('#setup').classList.add('hidden'); $('#settings').classList.add('hidden'); refreshFiles(); refreshTree(); };
  $('#tab-settings').onclick=()=>{ $('#tab-settings').classList.add('active'); $('#tab-setup').classList.remove('active'); $('#tab-work').classList.remove('active'); $('#settings').classList.remove('hidden'); $('#setup').classList.add('hidden'); $('#work').classList.add('hidden'); };

  // Add agent & feedback
  $('#bot-add').onclick=()=>{
    const n=$('#bot-title').value.trim(); if(!n) return;
    const temp = parseFloat($('#default-temp').value) || 0.3;
    agents.push({name:n, system:$('#bot-description').value.trim(), temperature:temp });
    $('#bot-title').value=''; $('#bot-description').value=''; renderTeam();
  };

  $('#bot-generate-desc').onclick=async()=>{
    const title = $('#bot-title').value.trim();
    if (!title) { toast('Please enter a bot title first.'); return; }
    const btn = $('#bot-generate-desc');
    btn.disabled = true; btn.textContent = '...';
    try {
      const r = await fetch('/api/generate_description', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({ title: title, model: $('#model').value })
      });
      if (!r.ok) { const err = await r.json(); throw new Error(err.message || 'Failed to generate'); }
      const data = await r.json();
      $('#bot-description').value = data.description;
    } catch (e) {
      toast('Error: ' + e.message);
    } finally {
      btn.disabled = false; btn.textContent = 'Generate';
    }
  };
  $('#fbform').onsubmit=async(e)=>{ e.preventDefault(); const v=$('#fb').value.trim(); if(!v) return; addMsg('You', v, true); await fetch('/user_input',{method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({message:v})}); $('#fb').value=''; setStatus('running'); };

  // Workspace
  async function refreshFiles(){ const r=await fetch('/api/workspace/files'); const d=await r.json(); const F=$('#files'); F.innerHTML = d.length? d.map(x=>`<a style='display:block;padding:6px;border:1px solid #2b3443;border-radius:8px;margin:4px 0;background:#111827' target='_blank' href='${x.path}'>${x.name}</a>`).join('') : '<div style="color:#9ca3af">No files yet.</div>'; }
  function renderTreeNode(node){ if(node.type==='file'){ return `<li><a target=\"_blank\" href=\"/workspace/${node.path}\">${node.name}</a></li>`; } let kids=''; if(Array.isArray(node.children)){ kids = '<ul>'+node.children.map(renderTreeNode).join('')+'</ul>'; } return `<li>${node.name}${kids}</li>`; }
  async function refreshTree(){ const r=await fetch('/api/workspace/tree'); const d=await r.json(); $('#tree').innerHTML = '<ul>'+renderTreeNode(d)+'</ul>'; }
  $('#upload').onclick=async()=>{ const f=$('#file').files[0]; if(!f) return; const fd=new FormData(); fd.append('file', f); const r = await fetch('/api/workspace/upload',{method:'POST',body:fd}); if(!r.ok){ const e=await r.json(); toast('Upload failed: '+(e.error||e.message)); } else { toast('Uploaded'); refreshFiles(); refreshTree(); }};
  $('#refresh').onclick=()=>{ refreshFiles(); refreshTree(); };

  // Sessions quick save/load
  $('#sessions').onclick=async()=>{ const name=prompt('Session name to save/load (leave blank to load list)'); if(name){ await fetch('/api/sessions',{method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({name, model:$('#model').value, goal:$('#goal').value, agents})}); toast('Saved.'); } else { const r=await fetch('/api/sessions'); const list=await r.json(); const pick=prompt('Available:\\n'+list.join('\\n')+'\\n\\nType a name to load:'); if(pick){ const d=await (await fetch('/api/sessions/'+pick)).json(); agents=d.agents||[]; $('#goal').value=d.goal||''; await loadModels(); if(d.model){ $('#model').value=d.model; } renderTeam(); }} };

  // Export transcript (MD/HTML)
  $('#export').onclick=async()=>{ const tr=(window.__transcript||[]); const r1=await fetch('/api/transcript/md',{method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({transcript: tr})}); const d1=await r1.json(); const r2=await fetch('/api/transcript/html',{method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({transcript: tr})}); const d2=await r2.json(); toast('Exports ready: MD & HTML'); if(d1.file){ window.open(d1.file,'_blank'); } if(d2.file){ window.open(d2.file,'_blank'); } };

  // Settings
  $('#saveBase').onclick=async()=>{ const base=$('#ollamaBase').value.trim(); if(!base) return; const r=await fetch('/api/settings/ollama',{method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({base})}); if(r.ok){ toast('Saved Ollama base'); loadModels(); } else { const e=await r.json(); toast('Save failed: '+(e.message||e.error)); } };
  $('#probe').onclick=async()=>{ const t0=performance.now(); try{ const r=await fetch('/api/models'); const ok=r.ok; const dt=(performance.now()-t0).toFixed(0); const d=await r.json(); if(ok && Array.isArray(d)){ $('#probeHint').textContent=`OK (${d.length} models) in ${dt}ms`; } else { $('#probeHint').textContent=`Error in ${dt}ms: `+(d.message||JSON.stringify(d)); } } catch(e){ $('#probeHint').textContent='Probe failed: '+e.message; } };

  // Models
  $('#reloadModels').onclick=()=>loadModels();
  async function loadModels(){ $('#model').innerHTML='<option>Loading…</option>'; try{ const r=await fetch('/api/models'); const models=await r.json(); const sel=$('#model'); if(Array.isArray(models)&&models.length){ sel.innerHTML=models.map(m=>`<option>${m}</option>`).join(''); const def = models.includes('qwen3:8b')?'qwen3:8b':models[0]; sel.value=def; $('#model-hint').textContent=`Loaded ${models.length} models from Ollama`; $('#model-count').textContent=models.length+' found'; lastModels=models; } else { sel.innerHTML=''; $('#model-hint').textContent='No models found'; $('#model-count').textContent='0 found'; } } catch(e){ $('#model').innerHTML=''; $('#model-hint').textContent='Failed to load models: '+e.message; $('#model-count').textContent='error'; } }

  // Transcript capture
  const origAddMsg = addMsg; addMsg = function(sender, text, mine=false, id=null){ window.__transcript=(window.__transcript||[]).concat([{t:Date.now(), from:sender, text}]); return origAddMsg(sender,text,mine,id); };

  // Init
  setStatus('idle'); buttons(false); renderTeam(); loadModels();
