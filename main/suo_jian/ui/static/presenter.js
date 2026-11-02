(async function(){
  'use strict';
  const $=id=>document.getElementById(id),esc=x=>String(x??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  if(!$('adaptation')){const banner=document.createElement('div');banner.id='adaptation';banner.hidden=true;$('matches').before(banner);}
  let job=new URLSearchParams(location.search).get('job'),state={},group=null,loaded=false,busy=false,timer=null;
  let sentenceAssets=[],selected=[],sentenceJob=null,stopAt=null,selectionDirty=false;
  const buttons=['scan','match','render','draft'];
  async function api(path,data){const response=await fetch(`/api/presenter/${job}/${path}`,data===undefined?{}:{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(data)});const value=await response.json();if(!response.ok)throw Error(value.error||'请求失败');return value;}
  function enabled(){const working=busy||state.state?.status==='running';buttons.forEach(id=>$(id).disabled=working);$('match').disabled||=!group;$('render').disabled||=selectionDirty||!state.plan?.selected?.length||Boolean(state.plan?.missing.length&&!$('partial').checked)||state.plan?.person_group!==group||state.plan?.script!==$('script').value;$('draft').disabled||=selectionDirty||!state.render;$('buildSentencePlan').disabled=working||!group||!selected.length;}
  function draw(value){
    state=value;$('status').textContent=value.state.error||value.state.phase;
    $('progress').hidden=value.state.status!=='running'||!value.state.total;
    $('progress').max=value.state.total||1;$('progress').value=value.state.completed||0;
    if(!loaded&&(value.plan?.script||value.reference?.text)){$('script').value=value.plan?.script||value.reference.text;loaded=true;}
    if(value.library){
      const library=value.library;if(!$('folder').value)$('folder').value=library.folder;
      const unknown=library.assets.filter(a=>!a.person_group);
      $('inventory').textContent=`${library.file_count} 个文件 · ${library.groups.length} 组人物 · ${unknown.length} 条待核对 · ${library.errors.length} 条不可用`;
      if(!group&&value.plan)group=value.plan.person_group;
      $('groups').innerHTML=library.groups.map((g,i)=>`<button class="person ${group===g.id?'selected':''}" data-group="${esc(g.id)}" aria-pressed="${group===g.id}"><img src="/api/presenter/${job}/thumb/${g.asset_ids[0]}" alt="人物组 ${i+1} 代表画面"><strong>人物 ${i+1}</strong><small>${g.count} 条已归组素材</small></button>`).join('');
      $('groups').querySelectorAll('button').forEach(b=>b.onclick=()=>{if(value.state.status==='running')return;if(group!==b.dataset.group){selected=[];selectionDirty=true}group=b.dataset.group;draw(state);drawSentences();});
      $('errorList').innerHTML=[...unknown.map(a=>`${esc(a.name)}：人物不确定`),...library.errors.map(e=>`${esc(e.name)}：${esc(e.error)}`),...(value.speech_errors||[]).map(e=>`${esc(e.name)}：${esc(e.error)}`)].join('<br>');
    }
    const plan=value.plan;
    $('adaptation').hidden=!plan?.adapted;
    if(plan?.adapted)$('adaptation').innerHTML=`<b class="matched">同义重组 · 已锁定人物 ${esc(plan.person_group.replace('person-',''))}</b><p class="hint">以下字幕沿用实际口播。参考片中未覆盖的事实：${(plan.reference_gaps||[]).map(esc).join('；')||'无'}。</p>${(plan.changes||[]).length?`<p class="hint">措辞说明：${plan.changes.map(esc).join('；')}</p>`:''}`;
    if(plan){$('coverage').textContent=`${plan.matched_clause_count??plan.rows.filter(r=>r.selected).length} / ${plan.rows.length} 句匹配 · ${plan.duration.toFixed(1)} 秒`;$('matches').innerHTML=plan.rows.map((r,i)=>`<div class="match-row"><span>${String(i+1).padStart(2,'0')}</span><p>${esc(r.text)}</p><div>${r.selected?`<b class="matched">${plan.adapted?'已按实际口播选段':`已定位 · ${(r.selected.score*100).toFixed(0)}% 文字相似`}</b><small>${esc(r.selected.name)} · ${r.selected.source_start.toFixed(2)}–${r.selected.source_end.toFixed(2)}s</small><small>实际转写：${esc(r.selected.spoken_text)}</small>`:`<b class="missing">未找到足够可靠的原句</b><small>请补充素材或修正文案。以下仅为信息点候选，需确认意思一致。</small>${(r.suggestions||[]).map((s,j)=>`<button class="suggestion secondary" data-row="${i}" data-suggestion="${j}">改用实际口播：${esc(s.text)}</button>`).join('')}`}</div></div>`).join('');
      $('matches').querySelectorAll('[data-suggestion]').forEach(b=>b.onclick=()=>{const rows=plan.rows.map(r=>r.text);rows[Number(b.dataset.row)]=plan.rows[Number(b.dataset.row)].suggestions[Number(b.dataset.suggestion)].text;$('script').value=rows.join('，');action('match',{script:$('script').value,person_group:group});});
    }
    if(value.render){const r=value.render,url=`/api/presenter/${job}/media/${r.id}/preview`;
      if($('preview').getAttribute('src')!==url)$('preview').src=url;
      $('preview').hidden=false;$('emptyPreview').hidden=true;$('renderNote').textContent=(r.adapted?'这是同义重组版本。':'')+(r.partial?'这是缺句小样，尚非完整复刻。':'')+r.note+((r.reference_gaps||[]).length?' 未覆盖的原片事实：'+r.reference_gaps.join('；')+'。':'');
      $('download').hidden=false;$('download').href=url;$('download').download='presenter-preview.mp4';
      $('archive').hidden=!value.draft;$('draft').textContent=value.draft?.registration?.registered?'检查剪映草稿登记':'保存到剪映草稿列表';if(value.draft){$('archive').href=`/api/presenter/${job}/media/${r.id}/draft`;$('draftNote').textContent='草稿名称：'+(value.draft.display_name||value.draft.name)+'。'+value.draft.note+' 本机目录：'+value.draft.folder;}
    }else{$('preview').hidden=true;$('emptyPreview').hidden=false;$('download').hidden=true;$('archive').hidden=true;$('draftNote').textContent='';$('renderNote').textContent='';}
    enabled();
  }
  function drawSentences(){
    const assets=sentenceAssets.filter(a=>a.person_group===group);
    $('sentenceList').innerHTML=assets.length?assets.map(a=>`<div class="sentence-asset"><strong>${esc(a.name)}</strong>${a.sentences.map(s=>{const key=`${a.asset_id}:${s.first}:${s.last}`;return `<div class="sentence-row"><span>${esc(s.text)}<small>${s.start.toFixed(2)}–${s.end.toFixed(2)} 秒</small></span><button class="secondary" data-play="${key}">试听</button><button class="secondary" data-add="${key}" ${selected.some(x=>x.key===key)?'disabled':''}>加入</button></div>`}).join('')||'<p class="hint">未识别到可用句子</p>'}</div>`).join(''):'<p class="hint">选择人物后显示该人物的口播。</p>';
    const find=key=>{for(const a of assets)for(const s of a.sentences)if(`${a.asset_id}:${s.first}:${s.last}`===key)return {...s,asset_id:a.asset_id,name:a.name,key};};
    $('sentenceList').querySelectorAll('[data-add]').forEach(b=>b.onclick=()=>{selected.push(find(b.dataset.add));selectionDirty=true;drawSentences();enabled()});
    $('sentenceList').querySelectorAll('[data-play]').forEach(b=>b.onclick=()=>{const s=find(b.dataset.play),v=$('sourcePreview');stopAt=s.end+.2;v.hidden=false;const url=`/api/presenter/${job}/source/${s.asset_id}`;const play=()=>{v.currentTime=Math.max(0,s.start-.15);v.play().catch(()=>{})};if(v.getAttribute('src')!==url){v.src=url;v.addEventListener('loadedmetadata',play,{once:true})}else play()});
    $('selectedSentences').innerHTML=selected.length?selected.map((s,i)=>`<div class="sentence-row"><span>${i+1}. ${esc(s.text)}<small>${esc(s.name)} · ${s.start.toFixed(2)}–${s.end.toFixed(2)} 秒</small></span><button class="secondary" data-up="${i}" ${i===0?'disabled':''}>↑</button><button class="secondary" data-down="${i}" ${i===selected.length-1?'disabled':''}>↓</button><button class="secondary" data-remove="${i}">移除</button></div>`).join(''):'<p class="hint">还没有选择句子。</p>';
    $('selectedSentences').querySelectorAll('[data-up],[data-down],[data-remove]').forEach(b=>b.onclick=()=>{const i=Number(b.dataset.up??b.dataset.down??b.dataset.remove);if(b.dataset.remove!==undefined)selected.splice(i,1);else{const j=i+(b.dataset.up!==undefined?-1:1);[selected[i],selected[j]]=[selected[j],selected[i]]}selectionDirty=true;drawSentences();enabled()});
    $('buildSentencePlan').disabled=!group||!selected.length||busy||state.state?.status==='running';
  }
  $('sourcePreview').ontimeupdate=()=>{if(stopAt!==null&&$('sourcePreview').currentTime>=stopAt){$('sourcePreview').pause();stopAt=null}};
  async function refresh(){try{const value=await api('state');draw(value);if(value.library&&value.state.status!=='running'&&sentenceJob!==job){sentenceAssets=(await api('sentences')).assets;sentenceJob=job;if(!selected.length&&value.plan?.selection_mode==='sentences'){selected=value.plan.rows.map(r=>{const c=r.selected,a=sentenceAssets.find(x=>x.asset_id===c.asset_id),s=a?.sentences.find(x=>x.first===c.word_first&&x.last===c.word_last);return s?{...s,asset_id:a.asset_id,name:a.name,key:`${a.asset_id}:${s.first}:${s.last}`}:null}).filter(Boolean)}drawSentences();}}catch(e){$('status').textContent=e.message;}}
  async function action(path,data){busy=true;enabled();try{await api(path,data);await refresh();}catch(e){$('status').textContent=e.message;}finally{busy=false;enabled();}}
  try{
    if(job&&!/^(?:[0-9a-f]{8}|p-[0-9a-f]{12})$/.test(job))throw Error('任务编号无效');
    if(job){history.replaceState(null,'',`?job=${job}`);if(!job.startsWith('p-'))$('referenceLink').href=`/?job=${job}`;}
    $('scan').onclick=async()=>{if(!$('folder').value.trim()){$('status').textContent='请输入素材文件夹路径';return}if(!job||(job.startsWith('p-')&&state.library)){const r=await fetch('/api/presenter/new',{method:'POST',headers:{'Content-Type':'application/json'},body:'{}'});const value=await r.json();if(!r.ok){$('status').textContent=value.error;return}job=value.job;state={};group=null;loaded=false;history.replaceState(null,'',`?job=${job}`)}sentenceJob=null;selected=[];selectionDirty=false;await action('scan',{folder:$('folder').value.trim()})};
    $('buildSentencePlan').onclick=async()=>{busy=true;enabled();try{const result=await api('sentence-plan',{person_group:group,selections:selected.map(({asset_id,first,last})=>({asset_id,first,last}))});$('script').value=result.plan.script;loaded=true;selectionDirty=false;await refresh()}catch(e){$('status').textContent=e.message}finally{busy=false;enabled();drawSentences()}};
    $('match').onclick=()=>action('match',{script:$('script').value,person_group:group});
    $('partial').onchange=enabled;
    $('script').oninput=enabled;
    $('render').onclick=()=>action('render',{partial:$('partial').checked});
    $('draft').onclick=()=>action('draft',{});
    if(job)await refresh();else{$('status').textContent='填写素材文件夹后开始扫描';enabled();drawSentences()}timer=setInterval(()=>{if(job)refresh()},4000);
    window.addEventListener('pagehide',()=>clearInterval(timer));
  }catch(e){$('status').textContent=e.message;buttons.forEach(id=>$(id).disabled=true);}
})();
