(() => {
  'use strict';
  const list=document.getElementById('recentProjects'), search=document.getElementById('projectSearch'), more=document.getElementById('moreProjects');
  const names={feed:'信息流视频复刻',presenter:'真人口播复刻',vibe:'VibeCraft'};
  const shapes={feed:'<rect x="3" y="4" width="18" height="16" rx="2"/><path d="M3 9h18M8 4v5m8-5v5m-9 5h5m-5 3h9"/>',presenter:'<circle cx="12" cy="8" r="3"/><path d="M5 20v-3a7 7 0 0 1 14 0v3M3 3h3M3 3v3m18-3h-3m3 0v3"/>',vibe:'<path d="m12 2 2.5 7.5L22 12l-7.5 2.5L12 22l-2.5-7.5L2 12l7.5-2.5Z"/>'};
  let projects=[],filter='all',limit=6;
  const escape=value=>String(value??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  function date(stamp){return new Intl.DateTimeFormat('zh-CN',{month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit',hour12:false}).format(new Date(stamp*1000));}
  function paint(){
    const query=search.value.trim().toLowerCase();
    const selected=projects.filter(p=>(filter==='all'||p.mode===filter)&&`${p.title} ${p.job} ${names[p.mode]}`.toLowerCase().includes(query));
    document.getElementById('projectCount').textContent=`${selected.length}`;
    list.innerHTML=selected.length?selected.slice(0,limit).map(p=>`<a class="recent-row" href="${escape(p.href)}" aria-label="继续${escape(names[p.mode])}：${escape(p.title)}"><span class="project-icon ${p.mode}" aria-hidden="true"><svg viewBox="0 0 24 24">${shapes[p.mode]}</svg></span><span class="project-title"><b>${escape(p.title)}</b><small>${escape(p.job)}</small></span><span class="project-mode">${names[p.mode]}</span><span class="project-status ${/需要|提示|失败/.test(p.status_label)?'attention':/已保存|已导出/.test(p.status_label)?'saved':''}">${escape(p.status_label)}</span><time class="project-time" datetime="${new Date(p.updated_at*1000).toISOString()}">${escape(date(p.updated_at))}</time><span class="project-arrow" aria-hidden="true">↗</span></a>`).join(''):`<div class="home-empty">${query?'没有找到匹配的项目，试试其他名称或编号。':filter==='all'?'还没有制作记录。从上方选择一种方式，开始第一条视频。':'这个方向还没有制作记录。'}</div>`;
    more.hidden=selected.length<=limit;
  }
  document.querySelectorAll('[data-filter]').forEach(button=>button.addEventListener('click',()=>{filter=button.dataset.filter;limit=6;document.querySelectorAll('[data-filter]').forEach(b=>b.setAttribute('aria-pressed',String(b===button)));paint();}));
  search.addEventListener('input',()=>{limit=6;paint();});more.addEventListener('click',()=>{limit+=12;paint();});
  async function load(){
    try{const response=await fetch('/api/studio/projects');if(!response.ok)throw Error();const value=await response.json();projects=(value.projects||[]).filter(p=>names[p.mode]&&/^(?:[0-9a-f]{8}|film-[0-9a-f]{16})$/.test(p.job));paint();document.querySelector('.mode-vibe').href='/motion';}
    catch(_){list.replaceChildren();const message=document.createElement('div');message.className='home-empty';message.textContent='暂时无法读取本机项目。';const retry=document.createElement('button');retry.type='button';retry.textContent='重新读取';retry.onclick=load;message.append(retry);list.append(message);more.hidden=true;}
  }
  load();
})();
