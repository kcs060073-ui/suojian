(() => {
  'use strict';
  const style=document.createElement('style');
  style.textContent=`
    .timeline-tools{display:flex;align-items:center;justify-content:space-between;gap:12px;margin-top:18px;flex-wrap:wrap}
    .timeline-tools>div{display:flex;align-items:center;gap:8px;min-width:0}
    .timeline-tools button{min-width:36px;padding:6px 10px!important;min-height:34px!important}
    .timeline-tools input{width:140px;max-width:30vw;padding:0!important}
    .timeline-tools output{font-size:12px;min-width:38px;text-align:right;color:#566174}
    .sj-studio .timeline-wrap{margin-top:10px;touch-action:pan-x pan-y}
    .sj-studio .timeline-wrap>.track,.sj-studio .timeline-wrap>.time-ruler{width:var(--timeline-content-width)!important;min-width:0!important;grid-template-columns:86px minmax(0,1fr)}
    .sj-studio .timeline-wrap .track-label{position:sticky;left:0;z-index:6;background:#f0f2f6;border-right:1px solid #dce1e8}
    .timeline-overview{position:relative;height:28px;margin-top:10px;background:#e8edf5;border:1px solid #d5dce7;border-radius:7px;overflow:hidden;cursor:grab;touch-action:none}
    .timeline-overview:focus-visible{outline:2px solid #145fc3;outline-offset:3px}
    .overview-shot{position:absolute;top:7px;bottom:7px;background:#7f98bd;border-right:1px solid #e8edf5;pointer-events:none}
    .overview-window{position:absolute;top:1px;bottom:1px;border:2px solid #145fc3;border-radius:5px;background:#ffffff5c;pointer-events:none}
    .sj-tooltip{width:min(390px,calc(100vw - 24px))!important;max-height:calc(100vh - 32px);overflow:hidden;background:#fffffff5!important;border-color:#d1d9e5!important;color:#27313f!important;box-shadow:0 10px 40px #27385430!important;backdrop-filter:blur(16px)}
    .sj-tooltip .shot-methods{border-top:1px solid #dbe1ea;margin-top:12px;padding-top:8px}.shot-methods h4{margin:4px 0;font-size:14px}.shot-methods ul{padding-left:18px;margin:8px 0}.shot-methods li{font-size:12px;line-height:1.7;margin:4px 0}.sj-tooltip h3,.sj-tooltip p{color:#27313f!important}
  `;
  document.head.appendChild(style);
  function mount(){
    const timeline=document.querySelector('#studio-reference .timeline-wrap');
    if(!timeline||timeline.dataset.navigationAttached)return;
    timeline.dataset.navigationAttached='true';
    const player=document.querySelector('#sjPlayer');
    const tools=document.createElement('div');tools.className='timeline-tools';
    tools.innerHTML='<b>时间线</b><div><button type="button" data-fit>适应全片</button><button type="button" data-zoom-out aria-label="缩小时间线">−</button><input type="range" min="1" max="16" step="0.25" value="1" aria-label="时间线缩放"><button type="button" data-zoom-in aria-label="放大时间线">＋</button><output>1×</output></div>';
    timeline.before(tools);
    const overview=document.createElement('div');overview.className='timeline-overview';overview.tabIndex=0;overview.setAttribute('role','slider');overview.setAttribute('aria-label','时间线横向导览');overview.setAttribute('aria-valuemin','0');overview.setAttribute('aria-valuemax','100');
    const segments=timeline.querySelectorAll('[data-kind="shot"]');
    segments.forEach(segment=>{const mini=document.createElement('span');mini.className='overview-shot';mini.style.left=segment.style.left;mini.style.width=segment.style.width;overview.append(mini)});
    const windowBox=document.createElement('span');windowBox.className='overview-window';overview.append(windowBox);timeline.after(overview);
    const slider=tools.querySelector('input'),output=tools.querySelector('output');
    const rows=timeline.querySelectorAll(':scope > .track,:scope > .time-ruler');
    let zoom=1,drag=null;
    const duration=()=>Number.isFinite(player?.duration)&&player.duration>0?player.duration:Math.max(1,Number(timeline.dataset.duration)||1);
    function navigate(){
      const total=Math.max(1,timeline.scrollWidth),view=timeline.clientWidth;
      windowBox.style.left=`${timeline.scrollLeft/total*100}%`;
      windowBox.style.width=`${Math.min(100,view/total*100)}%`;
      overview.setAttribute('aria-valuenow',String(Math.round(100*timeline.scrollLeft/Math.max(1,total-view))));
    }
    function ticks(){
      const lane=timeline.querySelector('.time-ruler .lane');if(!lane)return;
      const seconds=duration(),pixels=Math.max(1,timeline.scrollWidth-86)/seconds;
      const step=[1,2,5,10,15,30,60,120,300,600].find(n=>n*pixels>=64)||600;
      lane.replaceChildren();
      for(let time=0;time<=seconds;time+=step){const tick=document.createElement('span');tick.className='tick';tick.style.left=`${100*time/seconds}%`;tick.textContent=`${String(Math.floor(time/60)).padStart(2,'0')}:${String(time%60).padStart(2,'0')}`;lane.append(tick)}
    }
    function resize(next=zoom,fit=false){
      const seconds=duration(),oldLane=Math.max(1,timeline.scrollWidth-86),view=timeline.clientWidth;
      const playX=86+oldLane*(player?.currentTime||0)/seconds-timeline.scrollLeft;
      const anchor=playX>=86&&playX<=view?playX:view/2;
      const anchorRatio=(timeline.scrollLeft+anchor-86)/oldLane;
      zoom=Math.min(16,Math.max(1,Number(next)));
      timeline.style.setProperty('--timeline-content-width',`${86+Math.max(1,view-86)*zoom}px`);
      rows.forEach(row=>row.style.width='var(--timeline-content-width)');
      slider.value=String(zoom);output.value=`${Number(zoom.toFixed(2))}×`;
      timeline.scrollLeft=fit?0:86+(timeline.scrollWidth-86)*anchorRatio-anchor;
      ticks();navigate();
    }
    slider.oninput=()=>resize(slider.value);
    tools.querySelector('[data-fit]').onclick=()=>resize(1,true);
    tools.querySelector('[data-zoom-out]').onclick=()=>resize(zoom-0.5);
    tools.querySelector('[data-zoom-in]').onclick=()=>resize(zoom+0.5);
    timeline.addEventListener('wheel',event=>{if(event.ctrlKey){event.preventDefault();resize(zoom+(event.deltaY<0?0.5:-0.5))}},{passive:false});
    timeline.addEventListener('scroll',navigate,{passive:true});
    overview.addEventListener('pointerdown',event=>{
      const rect=overview.getBoundingClientRect(),x=event.clientX-rect.left;
      const left=timeline.scrollLeft/timeline.scrollWidth*rect.width,width=timeline.clientWidth/timeline.scrollWidth*rect.width;
      if(x<left||x>left+width)timeline.scrollLeft=x/rect.width*timeline.scrollWidth-timeline.clientWidth/2;
      drag={x:event.clientX,scroll:timeline.scrollLeft};overview.setPointerCapture(event.pointerId);navigate();
    });
    overview.addEventListener('pointermove',event=>{if(drag){timeline.scrollLeft=drag.scroll+(event.clientX-drag.x)*timeline.scrollWidth/overview.clientWidth;navigate()}});
    const release=()=>{drag=null};overview.addEventListener('pointerup',release);overview.addEventListener('pointercancel',release);overview.addEventListener('lostpointercapture',release);
    overview.addEventListener('keydown',event=>{if(['ArrowLeft','ArrowRight','Home','End'].includes(event.key)){event.preventDefault();if(event.key==='Home')timeline.scrollLeft=0;else if(event.key==='End')timeline.scrollLeft=timeline.scrollWidth;else timeline.scrollLeft+=(event.key==='ArrowLeft'?-1:1)*timeline.clientWidth*.2;navigate()}});
    const observer=new ResizeObserver(()=>{if(timeline.isConnected)resize();else observer.disconnect()});observer.observe(timeline);
    player?.addEventListener('loadedmetadata',()=>resize());
    resize(1,true);
  }
  new MutationObserver(mount).observe(document.querySelector('#workbench')||document.body,{childList:true,subtree:true});
  mount();
})();
