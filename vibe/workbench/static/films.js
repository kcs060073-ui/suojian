'use strict';
(() => {
 const esc=escapeHtml;
 const view=document.createElement('section');view.id='view-films';view.className='view';
 view.innerHTML=`<div class="page-heading"><div><div class="eyebrow">YOUR FILMS</div><h1>做好的影片，都在这里。</h1></div><button id="backToWorkingEdit" class="quiet">返回当前编辑方案</button></div><div class="film-layout"><div><div class="film-player-stage"><video id="filmPlayer" controls playsinline preload="metadata"></video></div><div class="film-player-label"><span id="filmFormat"></span><span id="filmStatus">成片已入库</span></div></div><aside class="tool-card film-detail"><span class="eyebrow">COMPLETED CUT</span><h2 id="filmTitle">正在读取影片…</h2><p id="filmMode"></p><div class="actions" id="filmDownloads"></div><p id="filmDocumentPolicy" class="hint"></p><p class="hint">这是已导出的成片。要修改这一条，可以在当前聊天里说明修改要求；制作工程也可下载留存。</p></aside></div><div class="timeline-heading"><h2>我的影片 <span id="filmCount"></span></h2></div><div id="filmLibrary" class="film-grid"></div>`;
 $('#main').prepend(view);
 const editPicker=document.createElement('div');editPicker.className='editor-film-picker';
 editPicker.innerHTML='<label>编辑影片<select id="editorFilmSelect" aria-label="选择要编辑的影片"></select></label><button id="openEditorFilm" class="quiet">打开所选影片</button><span id="editorFilmStatus" class="hint"></span>';
 $('#view-edit').prepend(editPicker);
 const editButton=document.createElement('button');editButton.id='editSelectedFilm';editButton.className='primary';editButton.textContent='在影片编辑中打开';$('#filmMode').after(editButton);
 $('#filmDocumentPolicy').nextElementSibling.textContent='有编辑工程的影片可以打开六镜时间线，修改标题、字幕、时长和混音；成片与编辑工程分别保留。';
 const nav=document.createElement('button');nav.dataset.view='films';nav.innerHTML='<span>▣</span> 我的影片';$('#navigation').prepend(nav);
 nav.addEventListener('click',()=>{showView('films');refreshFilms();});
 let films=[],selected=null,signature='',loading=false,initialized=false,wasActive=false,savedStatus='已保存到本机';
 const originalShowView=showView;
 const originalExport=$('#exportButton').onclick;
 const launchDirector=$('#directorLaunch')?.onclick;
 function syncPicker(){
  const picker=$('#editorFilmSelect'),before=picker.value;
  picker.innerHTML='<option value="">当前编辑方案</option>'+films.filter(f=>f.editorAvailable).map(f=>`<option value="${esc(f.id)}">${esc(f.title)}</option>`).join('');
  picker.value=config.editFilmId||before||'';
  $('#editorFilmStatus').textContent=config.name?'当前：'+config.name:'';
  const currentFilm=films.find(f=>f.id===config.editFilmId);
  if(currentFilm){const video=$('#baselineVideo');if(video.getAttribute('src')!==currentFilm.video.url){video.src=currentFilm.video.url;if(currentFilm.poster)video.poster=currentFilm.poster.url;else video.removeAttribute('poster');}$('#baselineCompare').textContent=compare?'进入实时编辑':'对照本条成片';}
 }
 function header(){
  const active=view.classList.contains('active');const film=films.find(f=>f.id===selected);
  $('#projectName').readOnly=active;$('#projectName').value=active&&film?film.title:(config.name||'当前编辑方案');
  $('#saveButton').disabled=active;$('#saveButton').textContent=active?'成片已保存':'保存方案';
  if($('#directorLaunch')){$('#directorLaunch').textContent=active&&film?.editorAvailable?'✦ 编辑此影片':'✦ 一句话修改';$('#directorLaunch').onclick=active&&film?.editorAvailable?()=>openEditor(film.id,true):launchDirector;}
  if(active){if(!wasActive)savedStatus=$('#saveState').textContent;$('#exportButton').textContent='下载当前成片 ↗';$('#exportButton').onclick=()=>{if(film)download(film.video);};$('#saveState').textContent='已同步到我的影片';}
  else{$('#exportButton').innerHTML='导出影片 <span>↗</span>';$('#exportButton').onclick=originalExport||(()=>originalShowView('delivery'));if(wasActive)$('#saveState').textContent=savedStatus;}
  wasActive=active;
 }
 showView=function(name){originalShowView(name);if(name==='films'){player?.pause();$('#baselineVideo').pause();}else $('#filmPlayer').pause();if(['edit','films'].includes(name))history.replaceState(null,'','#'+name);header();};
 async function openEditor(id,director=false,automatic=false){
  if(!id)return toast('请先选择一条影片');
  editButton.disabled=true;$('#openEditorFilm').disabled=true;
  try{
   clearTimeout(saving);await api('/config',config);
   const result=await api('/films/open',{id,automatic});
   if(result.opened===false){showView('edit');toast('新成片已完成，已保留你后续的修改；顶部可切换到导出版本。');return;}
   config=result.config;activeShot=0;frame=0;playing=false;compare=false;
   player?.pause();$('#baselineVideo').pause();$('#baselineVideo').hidden=true;$('#player').hidden=false;$('#baselineCompare').textContent='对照已选视频';$('.stage-caption').textContent='LIVE COMPOSITION';
   $('#storyTitle').value=config.scriptDraft?.title||config.name||'';$('#narrationText').value=config.scriptDraft?.narration||'';
   selected=id;localStorage.setItem('vibe.selectedFilm',id);drawAll();player?.seek(0);showView(director?'director':'edit');syncPicker();$('#saveState').textContent=automatic?'生成完成，已自动进入影片编辑':'已打开影片编辑工程';toast((automatic?'生成完成，已打开：':'已打开：')+config.name);
   return true;
  }catch(e){toast(e.message);return false;}finally{editButton.disabled=false;$('#openEditorFilm').disabled=false;}
 }
 function download(file){const a=document.createElement('a');a.href=file.url;a.download=file.name;document.body.append(a);a.click();a.remove();}
 function choose(id){
  const film=films.find(f=>f.id===id);if(!film)return;selected=id;localStorage.setItem('vibe.selectedFilm',id);
  const video=$('#filmPlayer');if(video.dataset.film!==id){video.pause();video.dataset.film=id;video.src=film.video.url;if(film.poster)video.poster=film.poster.url;else video.removeAttribute('poster');}
  $('#filmTitle').textContent=film.title;$('#filmMode').textContent=film.mode;$('#filmDocumentPolicy').textContent=film.documentPolicy||'';
  editButton.hidden=!film.editorAvailable;
  $('#filmDownloads').innerHTML=film.artifacts.map(a=>`<a class="film-download" href="${esc(a.url)}" download="${esc(a.name)}">${esc(a.label)}</a>`).join('');
  const dims=film.resolution?film.resolution.join(' × '):'视频成片';$('#filmFormat').textContent=`${dims}${film.durationSeconds?' · '+Number(film.durationSeconds).toFixed(1)+' 秒':''} · ${film.fps} FPS`;
  $$('#filmLibrary [data-film]').forEach(b=>b.classList.toggle('selected',b.dataset.film===id));header();
 }
 async function refreshFilms(){
  if(loading)return;loading=true;
  try{
   const [all,completion]=await Promise.all([api('/films'),api('/films/latest-completion')]),next=JSON.stringify(all);films=all;
   if(!films.length){$('#filmTitle').textContent='还没有导出的成片';$('#filmLibrary').innerHTML='<p class="hint">完成影片导出后会自动显示在这里。</p>';return;}
   const newest=films[0].id;
   if(!selected)selected=localStorage.getItem('vibe.selectedFilm');
   if(localStorage.getItem('vibe.latestFilm')!==newest||!films.some(f=>f.id===selected)){selected=newest;localStorage.setItem('vibe.latestFilm',newest);}
   if(next!==signature){signature=next;$('#filmCount').textContent=films.length+' 条';$('#filmLibrary').innerHTML=films.map(f=>`<button class="film-tile ${f.id===selected?'selected':''}" data-film="${esc(f.id)}">${f.poster?`<img src="${esc(f.poster.url)}" alt="${esc(f.title)}">`:'<div class="film-cover-placeholder">▶</div>'}<span><strong>${esc(f.title)}</strong><small>${f.durationSeconds?Number(f.durationSeconds).toFixed(1)+' 秒 · ':''}${f.editorAvailable?'可继续编辑':'已完成'}</small></span></button>`).join('');choose(selected);syncPicker();}
   if(!initialized){initialized=true;if(!location.hash&&$('#saveState').textContent!=='有未保存修改')showView(config.editFilmId?'edit':'films');}
   view.dataset.filmsReady='true';
   if(completion&&config.shots?.length&&localStorage.getItem('vibe.autoOpenedCompletion')!==completion.id){
    const focused=document.activeElement;
    if(/有未保存|保存中/.test($('#saveState').textContent)||(focused?.matches('input,textarea,select')&&!focused.readOnly))return;
    const result=await openEditor(completion.filmId,false,true);
    if(result!==false)localStorage.setItem('vibe.autoOpenedCompletion',completion.id);
   }
  }catch(e){$('#filmTitle').textContent=e.message;}finally{loading=false;}
 }
 $('#filmLibrary').onclick=e=>{const button=e.target.closest('[data-film]');if(button)choose(button.dataset.film);};
 $('#backToWorkingEdit').onclick=()=>showView('edit');
 editButton.onclick=()=>openEditor(selected);$('#openEditorFilm').onclick=()=>openEditor($('#editorFilmSelect').value);
 $('#filmPlayer').onloadedmetadata=()=>{const film=films.find(f=>f.id===selected);if(film&&!film.durationSeconds)$('#filmFormat').textContent=`${$('#filmPlayer').videoWidth} × ${$('#filmPlayer').videoHeight} · ${$('#filmPlayer').duration.toFixed(1)} 秒`;};
 function hash(){if(location.hash==='#films'){showView('films');refreshFilms();}else if(location.hash==='#edit')showView('edit');}
 window.addEventListener('hashchange',hash);
 window.addEventListener('vibe-config-ready',()=>{header();syncPicker();refreshFilms();});
 setInterval(refreshFilms,3500);
 if(config.shots?.length)refreshFilms();hash();
})();
