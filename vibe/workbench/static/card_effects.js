'use strict';
(() => {
 const names={'none':'原场景动画','spotlight-hero-card':'聚光浮现','deck-deal-flyin':'发牌飞入','card-stack':'空间堆叠','row-embed':'分行错峰','aurora-bloom-bg-flip':'极光升腾 · 明暗反转'};
 const aliases={'depth-stack':'card-stack','exploded-view':'card-stack'};
 const layouts=['产权之问','搬迁顾虑','直接签约','服务边界','书面承诺','预约到访'];
 let replayTimer;
 function target(){const s=config.shots?.[activeShot];return s?`${config.name||'当前影片'} · 第 ${activeShot+1} 镜「${s.label||s.title||'未命名'}」`:'请先选择一个镜头';}
 function replay(){
  const s=config.shots?.[activeShot];if(!s)return;clearInterval(replayTimer);compare=false;$('#baselineVideo').pause();$('#baselineVideo').hidden=true;$('#player').hidden=false;
  player?.seek(s.from);player?.play();playing=true;$('#playButton').textContent='Ⅱ';
  replayTimer=setInterval(()=>{if(config.shots?.[activeShot]!==s){clearInterval(replayTimer);return;}const at=player?.getCurrentFrame()??0;if(at>=s.from+s.duration-2||(s.from>0&&at<s.from)){player?.pause();player?.seek(s.from+s.duration-1);playing=false;$('#playButton').textContent='▶';clearInterval(replayTimer);}},100);
 }
 window.vibeEditableCardEffects=Object.keys(names);
 window.vibeDemoAction=j=>{
  const id=j.result?.receipt?.id,path=j.result?.previewAsset;if(!path)return '';
  if(id in names)return `<button class="primary" data-apply-effect="${escapeHtml(id)}" data-reference="${escapeHtml(path)}">应用此动效（保留实拍与图解）</button><p class="hint">上方是独立原生参考样片；应用后使用当前影片内容实时演出，不替换为这段视频。</p>`;
  return `<p class="hint">此卡提供独立原生参考；尚未接入实时分层动效。</p><button class="quiet" data-apply-demo="${escapeHtml(path)}">用作独立视频素材</button>`;
 };
 window.applyVibeCardEffect=id=>{
  const effect=aliases[id]||id,s=config.shots?.[activeShot];if(!s)return;
  if(!(effect in names)){toast('这张卡目前提供独立原生参考样片；不会替换你的实拍。可先用已接入的分层动效。');return false;}
  s.cardEffect=effect;s.cardEnterFrames??=36;s.cardIntensity??=1;s.cardMotion=false;
  changed();drawTimeline();showView('edit');replay();toast(`第 ${activeShot+1} 镜 · ${names[effect]}：已从镜头开头重播`);return true;
 };
 const bar=document.createElement('div');bar.className='card-effect-target tool-card';
 bar.innerHTML='<strong>动效将应用到</strong><span id="cardEffectTarget"></span><button id="returnCardTarget" class="quiet">返回这个镜头</button>';
 $('#libraryContent').before(bar);$('#returnCardTarget').onclick=()=>showView('edit');
 function sync(){if($('#cardEffectTarget'))$('#cardEffectTarget').textContent=target();}
 function decorate(){
  const layout=$('#shotInspector [data-shot-field="recipe"]');if(!layout)return;
  layout.parentNode.firstChild.textContent='场景内容布局（与卡片动效独立）';layout.setAttribute('aria-label','场景内容布局');
  for(const option of [...layout.options]){const id=option.value;option.value=id;if(id in names||id in aliases){option.remove();continue;}if(/^jiading-\d$/.test(id))option.textContent='嘉定图解 · '+layouts[Number(id.slice(8))];}
  const s=config.shots[activeShot],current=s.cardEffect||'none';
  const block=document.createElement('div');block.className='card-effect-controls';
  block.innerHTML=`<div class="layer-status"><b>实拍内容层</b><span>${escapeHtml(s.asset||'当前实拍素材')}</span><b>卡片动效层</b><span>实时参数动画</span><b>背景层</b><span>${config.background==='footage'?'跟随本镜实拍 · 虚化':'当前全片背景'}</span></div><label>卡片动效<select id="cardEffectSelect" aria-label="卡片动效">${Object.entries(names).map(([id,name])=>`<option value="${id}" ${current===id?'selected':''}>${name}</option>`).join('')}</select></label><div class="two"><label>入场时长（秒）<input id="cardEnterSeconds" type="number" min=".2" max="4" step=".1" value="${(s.cardEnterFrames??36)/30}"></label><label>动效强度<input id="cardIntensity" type="number" min="0" max="1.5" step=".1" value="${s.cardIntensity??1}"></label></div><div class="actions">${['spotlight-hero-card','deck-deal-flyin','card-stack','row-embed','aurora-bloom-bg-flip'].map(id=>`<button type="button" class="${current===id?'selected':''}" data-card-effect="${id}">${names[id]}</button>`).join('')}</div><div class="actions"><button id="replayCardMotion" type="button" class="primary">▶ 重播本镜动效</button><button id="restoreCardLayout" type="button" class="quiet">恢复原场景动画</button></div><label class="check"><input id="cardBlurBackground" type="checkbox" ${config.background==='footage'?'checked':''}> 使用虚化实景背景</label><div class="two"><label>背景虚化（px）<input id="cardBackgroundBlur" type="number" min="4" max="60" value="${config.backgroundBlur??24}"></label><label>背景亮度（%）<input id="cardBackgroundBrightness" type="number" min="20" max="100" value="${Math.round((config.backgroundBrightness??.64)*100)}"></label></div><p class="hint">切换动效只改变卡片的运动、错峰与光效；实拍、图解、配音和字幕各自保留。动效会从本镜开头重播。</p><button id="browseShotcraft" class="quiet" type="button">浏览 Shotcraft 原生参考镜头库 ↗</button>${s.referencePreview?`<p class="hint"><a href="${escapeHtml(s.referencePreview)}" target="_blank">查看原生参考样片</a> · 参考样片与当前实时动效分开保留</p>`:''}`;
  layout.parentNode.after(block);
  const mediaOffset=document.createElement('label');mediaOffset.innerHTML=`实拍源起始帧<input id="cardFootageOffset" type="number" min="0" value="${s.footageOffset??0}">`;block.querySelector('.layer-status').after(mediaOffset);
  $('#cardFootageOffset').onchange=e=>{s.footageOffset=Math.max(0,Math.round(Number(e.target.value)));changed();};
  block.querySelectorAll('[data-card-effect]').forEach(b=>b.onclick=()=>window.applyVibeCardEffect(b.dataset.cardEffect));
  $('#cardEffectSelect').onchange=e=>window.applyVibeCardEffect(e.target.value);
  $('#cardEnterSeconds').onchange=e=>{s.cardEnterFrames=Math.max(6,Math.min(120,Math.round(Number(e.target.value)*30)));changed();replay();};
  $('#cardIntensity').onchange=e=>{s.cardIntensity=Math.max(0,Math.min(1.5,Number(e.target.value)));changed();replay();};
  $('#cardBlurBackground').onchange=e=>{config.background=e.target.checked?'footage':'flat';drawSettings();changed();drawShot();};
  $('#cardBackgroundBlur').onchange=e=>{config.backgroundBlur=Math.max(4,Math.min(60,Number(e.target.value)));changed();};
  $('#cardBackgroundBrightness').onchange=e=>{config.backgroundBrightness=Math.max(.2,Math.min(1,Number(e.target.value)/100));changed();};
  $('#replayCardMotion').onclick=replay;$('#restoreCardLayout').onclick=()=>window.applyVibeCardEffect('none');
  $('#browseShotcraft').onclick=()=>{sync();showView('library');};sync();
 }
 const originalDrawShot=drawShot;drawShot=function(){originalDrawShot();decorate();};
 function openCards(){if(location.hash!=='#edit-cards'||!config.shots?.length)return;showView('edit');$$('[data-inspect]').forEach(b=>b.classList.toggle('selected',b.dataset.inspect==='shot'));$$('.inspect-panel').forEach(p=>p.classList.toggle('active',p.id==='inspect-shot'));history.replaceState(null,'','#edit-cards');}
 window.addEventListener('vibe-config-ready',()=>{sync();openCards();});window.addEventListener('hashchange',openCards);if(config.shots?.length)drawShot();openCards();
})();
