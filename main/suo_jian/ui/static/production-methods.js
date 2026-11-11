(() => {
  'use strict';
  const escape = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  function mount() {
    const reference = document.querySelector('#studio-reference');
    const job = new URLSearchParams(location.search).get('job');
    if (!reference || !/^[a-f0-9]{8}$/.test(job || '') || reference.dataset.methodsAttached) return;
    reference.dataset.methodsAttached='true';
    document.querySelector('#studio-methods')?.remove();
    document.querySelector('.studio-nav a[href="#studio-methods"]')?.remove();
    const panel=document.createElement('span');panel.className='summary-pill';
    panel.innerHTML='<span data-methods-content role="status">正在读取制作方式…</span><button data-methods-retry hidden>重试</button>';
    reference.querySelector('.summary-strip').append(panel);
    const content=panel.querySelector('[data-methods-content]');
    const retry=panel.querySelector('[data-methods-retry]');
    let shots=[];
    window.suojianProductionMethodsFor=(start,end)=>shots.filter(shot=>Number(shot.start)<Number(end)-0.001&&Number(shot.end)>Number(start)+0.001);
    window.suojianMethodsStatus='正在读取制作方式…';
    let timer;
    let apiBase = '';
    async function load(start = false) {
      clearTimeout(timer);
      retry.hidden = true;
      try {
        const response = await fetch(`${apiBase}/api/analyze/${job}/methods`, start ? {method:'POST'} : {});
        if (response.status === 404 && !apiBase) {
          apiBase = 'http://127.0.0.1:8790';
          return load(start);
        }
        const data = await response.json();
        if (!panel.isConnected) return;
        if (!response.ok) throw Error(data.error || '制作方式暂时无法读取');
        if (data.status === 'idle') return load(true);
        if (data.status === 'completed' && data.shots) {
          shots=data.shots;
          window.suojianMethodsStatus='制作方式已就绪';
          panel.hidden=true;
          return;
        }
        if (data.status !== 'running') throw Error(data.error || '制作方式分析需要重新运行');
        content.textContent = window.suojianMethodsStatus = '正在分析制作方式…';
        timer = setTimeout(() => {if (panel.isConnected) load();}, 3000);
      } catch (error) {
        content.textContent = window.suojianMethodsStatus = error.message;
        retry.hidden = false;
      }
    }
    retry.onclick = () => load(true);
    load();
  }
  new MutationObserver(mount).observe(document.querySelector('#workbench') || document.body, {childList:true,subtree:true});
  mount();
})();
