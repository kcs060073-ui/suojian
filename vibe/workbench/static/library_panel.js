/* No framework required. API returns exact vendor cards, source and playable media. */
(function(){
 'use strict';
 const el=(tag,cls,text)=>{const n=document.createElement(tag);if(cls)n.className=cls;if(text!==undefined)n.textContent=text;return n;};
 const link=(title,url)=>{const a=el('a','',title);a.href=url;a.target='_blank';a.rel='noopener';return a;};
 const button=(title,fn)=>{const b=el('button','',title);b.type='button';b.onclick=fn;return b;};
 window.VibeLibraryPanel={async init(container,options={}){
   if(typeof container==='string')container=document.querySelector(container);
   if(!container)throw new Error('镜头库容器不存在');
   const base=typeof options.api==='string'?options.api:'/api';
   const request=async(route,body)=>{const response=await fetch(base+route,body?{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)}:{});const result=await response.json();if(!response.ok)throw new Error(result.error||'请求失败');return result;};
   container.replaceChildren(el('p','','正在读取本机镜头与声音资产…'));
   let catalog;
   try{catalog=await request('/library');}catch(e){container.replaceChildren(el('p','error',e.message));return;}
   container.replaceChildren();
   const header=el('p','library-counts',`${catalog.stats.cards} 张镜头卡 · ${catalog.stats.styles} 种变体 · 本地动态样片 ${catalog.stats.previews} 个 · ${catalog.stats.bgm} 首音乐 · ${catalog.stats.sfx} 个音效`);
   const controls=el('div','library-controls'), query=el('input');query.type='search';query.placeholder='搜索动作、场景、用途、卡名';query.setAttribute('aria-label','搜索镜头和声音');
   const category=el('select');category.setAttribute('aria-label','镜头分类');category.append(new Option('全部镜头分类',''));
   Object.entries(catalog.categories).forEach(([k,v])=>category.append(new Option(v.zh||k,k)));
   const mode=el('select');mode.setAttribute('aria-label','素材类型');[['镜头配方','recipes'],['背景音乐','bgm'],['音效','sfx'],['原生工具','native'],['流程与许可','references']].forEach(([label,value])=>mode.append(new Option(label,value)));
   const status=el('p','library-status');status.setAttribute('role','status');
   const grid=el('div','library-grid');controls.append(query,category,mode);container.append(header,controls,status,grid);
   let native=null;
   const apply=async(recipe,style)=>{
     const selected={...recipe,styleId:style.id,styleName:style.name,preview:style.preview};
     try{if(!options.onApplyRecipe)throw new Error('尚未连接镜头时间线');const message=await options.onApplyRecipe(selected);status.textContent=message||`已选择 ${style.name}`;}
     catch(e){status.textContent=e.message;}
   };
   async function draw(){
     grid.replaceChildren();category.disabled=mode.value!=='recipes';
     const term=query.value.trim().toLowerCase();
     if(mode.value==='native'){
       try{native=native||await request('/native-tools');}catch(e){status.textContent=e.message;return;}
       for(const tool of native.tools){
         const card=el('article','library-card');card.append(el('h3','',tool.name),el('p','',tool.available?'独立副本已就绪':'依赖未齐备'),el('p','',(tool.notes||[]).join(' ')));
         if(tool.missingDependencies?.length)card.append(el('p','warning','缺少：'+tool.missingDependencies.join('、')));
         if(tool.id==='canvas'){
           (tool.examples||[]).forEach(example=>card.append(link('打开 '+example.name,example.url)));
         }else{
           const launch=button('打开原生工作台',async()=>{try{const result=await request('/native/launch',{id:tool.id});if(result.url)window.open(result.url,'_blank','noopener');status.textContent=result.message||'独立工作台已启动';}catch(e){status.textContent=e.message;}});
           launch.disabled=!tool.available;card.append(launch);
         }
         grid.append(card);
       }
       const scripts=el('article','library-card');scripts.append(el('h3','','Anything 原生制作脚本'));
       for(const script of native.scripts||[])scripts.append(el('p','',`${script.name} · ${script.status==='ready'?'Python入口已接入': '需Windows适配或Bash'} `));
       grid.append(scripts);return;
     }
     if(mode.value==='references'){
       const refs=el('article','library-card');refs.append(el('h3','','完整制作流程与原始规范'));
       catalog.references.filter(r=>!term||(r.name+' '+r.source).toLowerCase().includes(term)).forEach(r=>refs.append(link(r.source+' / '+r.name,r.url)));
       grid.append(refs);
       for(const license of catalog.licenses){const details=el('details','library-card');details.append(el('summary','',license.repo+' / '+license.path),link('打开原始许可',license.url),el('pre','',license.text));grid.append(details);}return;
     }
     if(mode.value!=='recipes'){
       const matches=catalog.audio.filter(a=>a.kind===mode.value&&(!term||`${a.name} ${a.category}`.toLowerCase().includes(term)));
       status.textContent=`找到 ${matches.length} 项声音素材。试听与应用后由时间线控制音量。`;
       for(const item of matches){
         const card=el('article','library-card');card.append(el('h3','',item.name),el('p','',item.category));
         const audio=el('audio');audio.controls=true;audio.preload='none';audio.src=item.preview;audio.onplay=()=>container.querySelectorAll('audio').forEach(a=>{if(a!==audio)a.pause();});
         card.append(audio,el('p',item.licenseStatus==='needs_review'?'warning':'',item.licenseStatus==='needs_review'?'原库授权记录待核对':'原库已登记出处'),link('查看声音出处',item.source),button('应用到时间线',async()=>{try{if(options.onSelectAudio)await options.onSelectAudio(item);else throw new Error('尚未连接声音时间线');status.textContent=`已选用 ${item.name}`;}catch(e){status.textContent=e.message;}}));grid.append(card);
       }return;
     }
     const matches=catalog.recipes.filter(r=>(!category.value||r.category===category.value)&&(!term||`${r.name} ${r.description} ${r.use} ${r.tags.join(' ')}`.toLowerCase().includes(term)));
     status.textContent=`找到 ${matches.length} 张卡。本地封面可立即查看；可按需生成准确原生demo，或在线播放原站样片。`;
     // Only build previews when in view; no 150-media preload storm.
     for(const recipe of matches){
       const card=el('article','library-card');card.append(el('h3','',recipe.name),el('p','',recipe.description),el('small','',`${catalog.categories[recipe.category]?.zh||recipe.category} · ${recipe.duration}`));
       const select=el('select');select.setAttribute('aria-label',recipe.name+' 动态变体');recipe.styles.forEach((s,i)=>select.append(new Option(s.name,i)));
       const video=el('video');video.controls=true;video.loop=true;video.muted=true;video.playsInline=true;video.preload='none';video.style.width='100%';video.poster=recipe.styles[0]?.poster||'';if(recipe.preview)video.src=recipe.preview;video.hidden=!recipe.preview;const poster=el('img');poster.src=recipe.styles[0]?.poster||'';poster.loading='lazy';poster.alt=recipe.name+' 镜头封面';poster.style.width='100%';poster.hidden=!!recipe.preview;
       video.onplay=()=>container.querySelectorAll('video').forEach(v=>{if(v!==video)v.pause();});
       select.onchange=()=>{const style=recipe.styles[+select.value];poster.src=style.poster||'';video.hidden=!style.preview;poster.hidden=!!style.preview;if(style.preview)video.src=style.preview;else video.removeAttribute('src');};
       card.append(poster,video);if(recipe.styles.length>1)card.append(select);
       const generate=button('生成本地原生预览',async()=>{generate.disabled=true;try{status.textContent='正在渲染准确上游源码，完成后可加入时间线…';const result=await request('/tools',{action:'demo',payload:{id:recipe.id,style:recipe.styles[+select.value||0].id}});if(result.preview){video.src=result.preview;video.hidden=false;poster.hidden=true;status.textContent='本地原生预览已生成；复杂动画在媒体内部。';}else{status.textContent='任务已提交，正在等待本地渲染…';const id=result.id;const timer=setInterval(async()=>{try{const jobs=await request('/jobs');const job=jobs.find(j=>j.id===id);if(!job||['queued','running'].includes(job.status))return;clearInterval(timer);if(job.status==='completed'&&job.result?.previewAsset){video.src=job.result.previewAsset;video.hidden=false;poster.hidden=true;status.textContent='本地原生样片已生成；可在制作记录中加入当前镜头。';}else status.textContent=job.error||(job.result?.warnings||[]).join(' ')||'原生渲染未完成，请查看制作记录';}catch(e){clearInterval(timer);status.textContent=e.message;}},2000);}}catch(e){status.textContent=e.message;}finally{generate.disabled=false;}});
       generate.disabled=!recipe.available;
       card.append(generate,button('在线播放原站样片',()=>{video.hidden=false;poster.hidden=true;video.src=recipe.styles[+select.value||0].remotePreview;video.play().catch(()=>{status.textContent='原站样片需要联网；可生成本地预览。';});}));
       const use=button(window.vibeEditableCardEffects?.includes(recipe.id)?'应用可编辑分层动效':'查看接入状态',()=>apply(recipe,recipe.styles[+select.value||0]));
       if(!recipe.sourceAvailable)use.disabled=true;
       card.append(use,link('完整配方与参数',recipe.reference));
       const details=el('details');details.append(el('summary','','源码与渲染依赖'));
       recipe.demo.forEach((url,i)=>details.append(link('准确实现 '+(i+1),url)));
       details.append(el('p',recipe.available?'':'warning',recipe.available?'现有共享依赖可解析此入口；最终以渲染检查为准':'原生源码保留；缺少额外依赖：'+recipe.missingDependencies.join('、')));
       card.append(details);grid.append(card);
     }
   }
   query.oninput=draw;category.onchange=draw;mode.onchange=draw;await draw();
   return {catalog,refresh:draw};
 }};
})();
