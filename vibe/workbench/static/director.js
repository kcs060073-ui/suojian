'use strict';
// Shares the existing editor state, undo service and local job API.
(() => {
  const h=escapeHtml;
  const nav=document.createElement('button');nav.dataset.view='director';nav.innerHTML='<span>✦</span> 一句话导演';
  $('#navigation').prepend(nav);nav.addEventListener('click',()=>{showView('director');refreshDirector();});
  const launch=document.createElement('button');launch.id='directorLaunch';launch.className='quiet';launch.textContent='✦ 一句话修改';
  $('.top-actions').prepend(launch);launch.onclick=()=>nav.click();
  const page=document.createElement('section');page.id='view-director';page.className='view';
  page.innerHTML=`<div class="page-heading"><div><div class="eyebrow">DIRECT YOUR STORY</div><h1>说出想法，继续拍下去。</h1></div><button id="openApiSettings" class="quiet">API 与调度设置</button></div>
  <div class="director-intro"><p>给一句修改意见，或贴进一份剧本。先看改动，也可以选择理解后直接应用。</p><span id="directorReady" class="pill">读取接口状态…</span></div>
  <div class="director-grid"><div class="tool-card director-command"><div class="two"><label>这次做什么<select id="directorRole"><option value="edit">修改当前影片</option><option value="script">剧本 → 分镜工作稿</option><option value="review">审查文案与结构</option></select></label><label>使用哪个接口<select id="directorProfile"><option value="">按任务调度</option></select></label></div>
  <label>一句话，或一份剧本<textarea id="directorPrompt" rows="7" placeholder="例如：第二镜缩短到5秒，主标题改为「设备先进来」，颜色改成青绿色，保留其他镜头。"></textarea></label>
  <div class="director-examples"><button data-example="把第二镜的主标题改成「先量入口，再选厂房」，字号改为86，颜色改为青绿色，保留其他内容。">改一镜</button><button data-example="为全片加入轻微连续推近和动态花字，其他内容不改。">调风格</button><button data-example="做一条30秒的项目介绍，围绕设备能否进场、现场核对和预约到访展开。不要编造实测参数，沿用现有画面风格。" data-role="script">写新剧本</button></div>
  <div class="actions"><label class="director-file">导入 TXT / Markdown<input id="directorFile" type="file" accept=".txt,.md,.markdown"></label><label class="check"><input id="directorAuto" type="checkbox">理解后直接应用，可撤销</label></div>
  <div class="actions"><button id="directorRun" class="primary">理解并生成改动</button><button id="directorFromScript" class="quiet">带入“脚本与分镜”文字</button></div><p id="directorMessage" class="hint" role="status">未配置 API 时，也可以在当前聊天里让我操作；两种入口共用修改与撤销记录。</p>
  <p class="hint">发给模型的是本次文字、镜头配置和字幕；不会自动上传视频。新剧本先生成可编辑工作稿，配音、素材制作和成片导出仍使用现有制作工具。</p></div>
  <div class="tool-card"><h2>这次准备怎么改</h2><div id="directorPlan"><p class="hint">理解完成后，这里显示修改范围。</p></div></div></div>
  <details id="directorSettings" class="director-settings"><summary>API 连接与任务调度</summary><div class="notice">密钥只保存在本机，Windows 使用当前账号保护；不会写进工程包。保存配置不会发起模型请求。连接测试会发出一次小请求。</div>
  <div class="actions"><button id="addOpenAI" class="quiet">＋ OpenAI</button><button id="addDeepSeek" class="quiet">＋ DeepSeek</button><button id="addCustomAI" class="quiet">＋ 兼容接口 / 本机模型</button></div><div id="directorProfiles"></div>
  <div class="tool-card"><h2>按任务调度</h2><p class="hint">每种任务可选首选和备用接口。备用仅在开启切换，且首选出现限流或连接故障时使用；不会重复应用修改。</p><div id="directorRoutes" class="director-routes"></div>
  <div class="two"><label>同时请求数<select id="directorConcurrency"><option>1</option><option>2</option><option>3</option><option>4</option></select></label><label>请求超时（秒）<input id="directorTimeout" type="number" min="15" max="180" value="90"></label></div>
  <label>单次输出 Token 上限<input id="directorTokens" type="number" min="512" max="16000" value="6000"></label><label class="check"><input id="directorFallback" type="checkbox">首选临时不可用时，允许切换到所选备用接口</label><button id="saveDirectorSettings" class="primary">保存 API 与调度</button><span id="directorSettingsMessage" class="hint" role="status"></span></div></details>
  <div class="tool-grid director-history"><div class="tool-card"><h2>API 调度记录</h2><div id="directorJobs"></div></div><div class="tool-card"><h2>修改与撤销记录</h2><div id="directorHistory"></div></div></div>`;
  $('#main').append(page);
  let settings={routes:{edit:[],script:[],review:[]}}, profiles=[], plans=[], selectedPlan=null, autoJob=null, polling=false, loaded=false;
  const roles={edit:'剪辑修改',script:'剧本与分镜',review:'文案与结构审查'};
  const state={};
  const message=(value)=>{$('#directorMessage').textContent=value;};
  const profileChoices=(value='',blank='未分配')=>`<option value="">${blank}</option>`+profiles.map(p=>`<option value="${h(p.id)}" ${p.id===value?'selected':''}>${h(p.name)}${p.keyConfigured?'':' · 待填密钥'}</option>`).join('');
  function renderProfiles(){
    $('#directorProfiles').innerHTML=profiles.map(p=>`<article class="tool-card director-profile" data-profile="${h(p.id)}"><div class="actions"><strong>${h(p.name)}</strong><span class="pill">${p.keyConfigured?'密钥已保存':'待填写密钥'}</span><button class="quiet danger" data-remove-profile="${h(p.id)}">移除配置</button></div><div class="two"><label>名称<input data-p="name" value="${h(p.name)}"></label><label>API 协议<select data-p="protocol"><option value="responses" ${p.protocol==='responses'?'selected':''}>OpenAI Responses</option><option value="chat" ${p.protocol==='chat'?'selected':''}>Chat Completions 兼容</option></select></label></div><label>Base URL<input data-p="base_url" value="${h(p.base_url)}" placeholder="https://api.example.com/v1"></label><div class="two"><label>模型名称<input data-p="model" value="${h(p.model)}" placeholder="填写账号可用的模型 ID"></label><label>API Key<input data-p="apiKey" type="password" autocomplete="new-password" placeholder="${p.keyConfigured?'留空保留已有密钥':'在此填写，不发到聊天'}"></label></div><label class="check"><input data-p="enabled" type="checkbox" ${p.enabled!==false?'checked':''}>启用此接口</label><label class="check"><input data-p="clearKey" type="checkbox">清除此接口已保存的密钥</label><div class="actions"><button class="quiet" data-test-profile="${h(p.id)}">测试连接</button><button class="quiet" data-models-profile="${h(p.id)}">读取可用模型</button></div><p class="hint" data-profile-result="${h(p.id)}"></p><div data-model-list="${h(p.id)}"></div></article>`).join('')||'<p class="hint">先添加一个接口。支持 OpenAI、DeepSeek，以及提供兼容协议的其他服务。</p>';
    $('#directorRoutes').innerHTML=Object.entries(roles).map(([role,label])=>`<div><h3>${label}</h3><label>首选<select data-route="${role}-0">${profileChoices(settings?.routes?.[role]?.[0])}</select></label><label>备用<select data-route="${role}-1">${profileChoices(settings?.routes?.[role]?.[1])}</select></label></div>`).join('');
    updateProfileSelect();
  }
  function readProfileEdits(){
    for(const el of $$('[data-profile]')){
      const p=profiles.find(p=>p.id===el.dataset.profile);
      for(const input of el.querySelectorAll('[data-p]')){if(input.dataset.p==='apiKey'&&!input.value.trim())continue;p[input.dataset.p]=input.type==='checkbox'?input.checked:input.value;}
    }
    if(settings)for(const role of Object.keys(roles))settings.routes[role]=[0,1].map(i=>$(`[data-route="${role}-${i}"]`)?.value).filter(Boolean);
  }
  function updateProfileSelect(){const prior=$('#directorProfile').value;$('#directorProfile').innerHTML=profileChoices(prior,'按任务调度');}
  function loadSettings(data){settings=data;profiles=structuredClone(data.profiles);renderProfiles();$('#directorConcurrency').value=data.maxConcurrency;$('#directorTimeout').value=data.timeoutSeconds;$('#directorTokens').value=data.maxOutputTokens;$('#directorFallback').checked=data.allowFallback;$('#directorReady').textContent=profiles.some(p=>p.ready)?'● API 已配置':'○ 等你填写 API';loaded=true;}
  async function saveSettings(){
    readProfileEdits();const button=$('#saveDirectorSettings');button.disabled=true;
    try{const data=await api('/director/settings',{profiles,routes:settings.routes,allowFallback:$('#directorFallback').checked,maxConcurrency:Number($('#directorConcurrency').value),timeoutSeconds:Number($('#directorTimeout').value),maxOutputTokens:Number($('#directorTokens').value)});loadSettings(data);$('#directorSettingsMessage').textContent=' 已保存，密钥不再回显。';return true;}catch(e){$('#directorSettingsMessage').textContent=' '+e.message;toast(e.message);return false;}finally{button.disabled=false;}
  }
  function addProfile(provider){
    readProfileEdits();const n=profiles.length+1;
    const p={id:provider+'-'+Date.now(),name:(provider==='openai'?'OpenAI':provider==='deepseek'?'DeepSeek':'兼容接口')+' '+n,provider,protocol:provider==='openai'?'responses':'chat',base_url:provider==='openai'?'https://api.openai.com/v1':provider==='deepseek'?'https://api.deepseek.com':'http://127.0.0.1:11434/v1',model:'',enabled:true,keyConfigured:false};
    profiles.push(p);for(const role of Object.keys(roles))if(!settings.routes[role]?.length)settings.routes[role]=[p.id];renderProfiles();$('#directorSettings').open=true;
  }
  function summarizeValue(value){if(value===null||value===undefined)return '—';const s=typeof value==='string'?value:JSON.stringify(value);return s.length>220?s.slice(0,220)+'…':s;}
  function renderPlan(p){
    if(!p)return;selectedPlan=p.id;const signature=JSON.stringify(p);if(state.planSignature===signature)return;state.planSignature=signature;
    $('#directorPlan').innerHTML=`<div class="eyebrow">${h(p.source)} · ${h({pending:'待应用',applied:'已应用',undone:'已撤销',advice:'建议'}[p.status]||p.status)}</div><h3>${h(p.summary)}</h3>${p.changes.length?`<div class="director-diffs">${p.changes.map(c=>`<div><strong>${h(c.field)}</strong><span>${h(summarizeValue(c.before))}</span><b>→</b><span>${h(summarizeValue(c.after))}</span></div>`).join('')}</div>`:''}${p.warnings.map(w=>`<p class="notice">${h(w)}</p>`).join('')}<div class="actions">${p.status==='pending'?`<button class="primary" data-apply-plan="${p.id}">应用这些改动</button>`:''}${p.status==='applied'?`<button class="quiet" data-undo-plan="${p.id}">撤销这次修改</button><button class="quiet" data-view-edit>去看实时画面</button><button class="quiet" data-preview-plan>试渲前5秒</button>`:''}</div>`;
  }
  function renderHistory(){
    const historyHTML=plans.map(p=>`<div class="director-record"><button class="quiet" data-open-plan="${p.id}">${h(p.summary||p.instruction.slice(0,40))}</button><small>${new Date(p.created*1000).toLocaleString()} · ${h({pending:'待应用',applied:'已应用',undone:'已撤销',advice:'建议'}[p.status]||p.status)} · ${h(p.source)}</small></div>`).join('')||'<p class="hint">应用后可以从这里撤销；聊天助手提交的修改也会显示在这里。</p>';if(state.historyHTML!==historyHTML){$('#directorHistory').innerHTML=historyHTML;state.historyHTML=historyHTML;}
  }
  async function applyPlan(id,undo=false){
    try{if(/有未保存|保存中/.test($('#saveState').textContent))throw Error('编辑器正在保存新的修改，请稍后重新理解指令，避免覆盖。');clearTimeout(saving);const r=await api('/director/'+(undo?'undo':'apply'),{id});config=r.config;drawAll();if(!undo){compare=false;$('#baselineVideo').pause();$('#baselineVideo').hidden=true;$('#player').hidden=false;$('#baselineCompare').textContent='对照已选视频';$('.stage-caption').textContent='LIVE COMPOSITION';}else if(config.showSelectedVideo!==false&&config.selectedBaseline==='realistic-halfbody'){compare=true;$('#player').hidden=true;$('#baselineVideo').hidden=false;$('#baselineCompare').textContent='进入实时编辑';$('.stage-caption').textContent='FINAL SELECTED CUT';}$('#saveState').textContent=undo?'已撤销修改':'AI 修改已保存';renderPlan(r.plan);state.lastRevision=null;message(undo?'已撤销，恢复到应用前的工程。':'已应用。可以预览，或试渲一个短段。');await refreshDirector();return true;}catch(e){message(e.message);toast(e.message);return false;}
  }
  async function refreshDirector(){
    if(polling)return;polling=true;
    try{
      if(!loaded)loadSettings(await api('/director/settings'));
      const [allPlans, jobs]=await Promise.all([api('/director/plans'),api('/director/jobs')]);plans=allPlans;renderHistory();
      const revision=plans.filter(p=>['applied','undone'].includes(p.status)).map(p=>p.id+':'+p.status).join('|');
      if(revision!==state.lastRevision&&!/有未保存|保存中/.test($('#saveState').textContent)){config=await api('/config');drawAll();state.lastRevision=revision;if(config.showSelectedVideo===false){compare=false;$('#baselineVideo').pause();$('#baselineVideo').hidden=true;$('#player').hidden=false;$('#baselineCompare').textContent='对照已选视频';$('.stage-caption').textContent='LIVE COMPOSITION';}else if(config.selectedBaseline==='realistic-halfbody'){compare=true;$('#player').hidden=true;$('#baselineVideo').hidden=false;$('#baselineCompare').textContent='进入实时编辑';$('.stage-caption').textContent='FINAL SELECTED CUT';}}
      const latest=plans.find(p=>p.id===selectedPlan)||plans[0];if(latest)renderPlan(latest);
      page.dataset.directorReady='true';
      const jobsHTML=jobs.map(j=>`<div class="director-record"><strong>${h({queued:'排队中',running:'请求中',completed:'完成',failed:'失败',cancelled:'已取消',interrupted:'已中断'}[j.status]||j.status)} · ${h(roles[j.role])}</strong><p>${h(j.instruction.slice(0,100))}</p>${j.error?`<p class="danger">${h(j.error)}</p>`:''}${j.attempts.map(a=>`<small>${h(a.profile)} / ${h(a.model)} · ${h(a.status||'请求中')}${a.seconds!==undefined?' · '+a.seconds+'s':''}${a.usage?' · '+h(JSON.stringify(a.usage)):''}${a.error?' · '+h(a.error):''}</small>`).join('')}${j.planId?`<button class="quiet" data-open-plan="${j.planId}">查看结果</button>`:''}${j.status==='queued'?`<button class="quiet" data-cancel-job="${j.id}">取消排队</button>`:''}</div>`).join('')||'<p class="hint">这里记录实际接口、耗时、Token 用量与失败原因。</p>';if(state.jobsHTML!==jobsHTML){$('#directorJobs').innerHTML=jobsHTML;state.jobsHTML=jobsHTML;}
      if(autoJob){const job=jobs.find(j=>j.id===autoJob);if(job?.status==='completed'){autoJob=null;if($('#directorAuto').checked&&plans.find(p=>p.id===job.planId)?.status==='pending')await applyPlan(job.planId);else{renderPlan(plans.find(p=>p.id===job.planId));message('理解完成，查看改动后可应用。');}}else if(job&&['failed','interrupted','cancelled'].includes(job.status)){autoJob=null;message(job.error||'任务已取消。');}}
    }catch(e){message(e.message);}finally{polling=false;}
  }
  $('#openApiSettings').onclick=()=>{$('#directorSettings').open=true;$('#directorSettings').scrollIntoView({behavior:'smooth'});};
  $('#addOpenAI').onclick=()=>addProfile('openai');$('#addDeepSeek').onclick=()=>addProfile('deepseek');$('#addCustomAI').onclick=()=>addProfile('custom');$('#saveDirectorSettings').onclick=saveSettings;
  $$('.director-examples button').forEach(b=>b.onclick=()=>{$('#directorPrompt').value=b.dataset.example;if(b.dataset.role)$('#directorRole').value=b.dataset.role;});
  $('#directorFile').onchange=async e=>{const file=e.target.files[0];if(!file)return;if(file.size>200000)return toast('剧本文本请控制在200KB以内');$('#directorPrompt').value=await file.text();$('#directorRole').value='script';};
  $('#directorFromScript').onclick=()=>{$('#directorPrompt').value=($('#storyTitle').value+'\n\n'+$('#narrationText').value).trim();$('#directorRole').value='script';};
  $('#directorRun').onclick=async()=>{const b=$('#directorRun');b.disabled=true;try{await save();const job=await api('/director/run',{instruction:$('#directorPrompt').value,role:$('#directorRole').value,profile_id:$('#directorProfile').value||undefined});autoJob=job.id;message('已进入API调度队列，工程尚未修改。');await refreshDirector();}catch(e){message(e.message);if(/API|密钥|模型|接口/.test(e.message))$('#directorSettings').open=true;}finally{b.disabled=false;}};
  page.onclick=async e=>{
    const b=e.target.closest('button');if(!b)return;
    if(b.dataset.applyPlan)await applyPlan(b.dataset.applyPlan);
    if(b.dataset.undoPlan)await applyPlan(b.dataset.undoPlan,true);
    if(b.dataset.openPlan){renderPlan(plans.find(p=>p.id===b.dataset.openPlan));$('#directorPlan').scrollIntoView({behavior:'smooth'});}
    if(b.hasAttribute('data-view-edit'))showView('edit');
    if(b.hasAttribute('data-preview-plan'))startTool('pilot',{from:0,to:Math.min(totalFrames()-1,149)});
    if(b.dataset.cancelJob){try{await api('/director/cancel',{id:b.dataset.cancelJob});refreshDirector();}catch(e){toast(e.message);}}
    if(b.dataset.removeProfile){readProfileEdits();profiles=profiles.filter(p=>p.id!==b.dataset.removeProfile);for(const role of Object.keys(roles))settings.routes[role]=settings.routes[role].filter(id=>id!==b.dataset.removeProfile);renderProfiles();$('#directorSettingsMessage').textContent='移除尚未保存，请点击保存 API 与调度。';}
    const pid=b.dataset.testProfile||b.dataset.modelsProfile;
    if(pid){b.disabled=true;try{if(!await saveSettings())return;const r=await api('/director/'+(b.dataset.modelsProfile?'models':'test'),{profile_id:pid});$(`[data-profile-result="${pid}"]`).textContent=r.models?'可用模型如下；点击填入后保存。':r.message+' · '+r.latencySeconds+'秒';if(r.models)$(`[data-model-list="${pid}"]`).innerHTML=r.models.map(m=>`<button class="quiet" data-pick-model="${h(m)}" data-for-profile="${pid}">${h(m)}</button>`).join('');}catch(e){$(`[data-profile-result="${pid}"]`).textContent=e.message;}finally{b.disabled=false;}}
    if(b.dataset.pickModel){$(`[data-profile="${b.dataset.forProfile}"] [data-p="model"]`).value=b.dataset.pickModel;}
  };
  window.addEventListener('vibe-config-ready',()=>{if(config.scriptDraft){$('#storyTitle').value=config.scriptDraft.title||'';$('#narrationText').value=config.scriptDraft.narration||'';}});
  setInterval(()=>{if(page.classList.contains('active'))refreshDirector();},2500);
  function openDirectorHash(){if(location.hash.startsWith('#director')){nav.click();if(location.hash==='#director-settings')$('#directorSettings').open=true;}}
  window.addEventListener('hashchange',openDirectorHash);openDirectorHash();
})();
