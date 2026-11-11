(() => {
  'use strict';
  const shell = document.querySelector('#uploadShell');
  const drop = document.querySelector('#drop');
  const fileInput = document.querySelector('#file');
  const start = document.querySelector('#start');
  const film = document.querySelector('#filmProgress');
  if (!shell || !drop || !fileInput || !start || !film || document.querySelector('#analysisPreview')) return;

  const style = document.createElement('style');
  style.textContent = `
    .page-feed .analysis-preview {grid-column:1/-1;min-width:0;border:1px solid #ffffffd9;border-radius:16px;overflow:hidden;background:#17191d;box-shadow:0 8px 30px -20px #27385460}
    .page-feed .analysis-preview[hidden] {display:none!important}
    .page-feed .analysis-preview video {display:block;width:100%;height:min(52vh,520px);min-height:220px;object-fit:contain;background:#17191d}
    .page-feed .analysis-preview video[hidden] {display:none!important}
    .page-feed .analysis-preview .preview-wait {display:grid;place-items:center;min-height:230px;margin:0;color:#c9cdd5;font-size:14px}
    .page-feed .analysis-preview .preview-wait[hidden] {display:none}
    .page-feed .analysis-previewing :is(.upload-card,.video-link-import,.upload-start,.hero-copy) {display:none!important}
    .page-feed .analysis-previewing #filmProgress {margin:0;grid-column:1/-1}
    .page-feed .preview-error {margin:0;padding:12px 18px;color:#e1e4e9;background:#24272e;font-size:12px}
    .page-feed .preview-error[hidden] {display:none}
    @media(max-width:600px){.page-feed .analysis-preview video{height:38vh;min-height:190px}}
  `;
  document.head.appendChild(style);
  const panel = document.createElement('section');
  panel.id = 'analysisPreview';
  panel.className = 'analysis-preview';
  panel.setAttribute('aria-label', '解析中的参考视频');
  panel.hidden = true;
  panel.innerHTML = '<p class="preview-wait" role="status">正在下载视频，完成后即可播放</p><video controls playsinline preload="metadata" aria-label="播放参考视频" hidden></video><p class="preview-error" role="status" hidden>浏览器暂时无法播放该视频，可继续等待分析结果。</p>';
  drop.before(panel);
  const player = panel.querySelector('video');
  const waiting = panel.querySelector('.preview-wait');
  const error = panel.querySelector('.preview-error');
  const container = shell.querySelector('.upload-shell');
  let selectedFile = null;
  let objectUrl = null;
  let source = null;
  let timer = null;
  let generation = 0;

  function release() {
    if (objectUrl) URL.revokeObjectURL(objectUrl);
    objectUrl = null;
  }
  function show() {
    container.classList.add('analysis-previewing');
    panel.hidden = false;
  }
  function setSource(url) {
    if (source === url) return;
    source = url;
    player.src = url;
    player.hidden = false;
    waiting.hidden = true;
    error.hidden = true;
  }
  function stop() {
    clearTimeout(timer);
    timer = null;
    generation++;
  }
  async function findVideo(job, token) {
    if (token !== generation || shell.classList.contains('hidden')) return;
    try {
      const response = await fetch(`/api/analyze/${job}/progress`);
      const state = await response.json();
      if (token !== generation) return;
      let url = response.ok && state.video_url;
      if (!url) {
        // Link downloads are atomically published under this existing server name.
        const candidate = `/api/video/${job}_linked_video.mp4`;
        const ready = await fetch(candidate, {method: 'HEAD'});
        if (ready.ok) url = candidate;
      }
      if (token !== generation || shell.classList.contains('hidden')) return;
      if (url) {
        setSource(url);
        return;
      }
      if (!response.ok || ['failed', 'interrupted'].includes(state.status)) {
        panel.hidden = true;
        container.classList.remove('analysis-previewing');
        return;
      }
    } catch (_) {
      // The analysis poll owns network/error reporting; retry source discovery only.
    }
    if (token === generation) timer = setTimeout(() => findVideo(job, token), 2000);
  }
  function begin(newTask = false) {
    const previousJob = new URLSearchParams(location.search).get('job');
    stop();
    release();
    source = null;
    player.pause();
    player.removeAttribute('src');
    player.load();
    player.hidden = true;
    waiting.hidden = false;
    error.hidden = true;
    show();
    if (selectedFile && !document.querySelector('#videoLink').value.trim()) {
      objectUrl = URL.createObjectURL(selectedFile);
      setSource(objectUrl);
      return;
    }
    const token = generation;
    const waitForJob = () => {
      if (token !== generation) return;
      const job = new URLSearchParams(location.search).get('job');
      if (/^[a-f0-9]{8}$/.test(job || '') && (!newTask || job !== previousJob)) findVideo(job, token);
      else timer = setTimeout(waitForJob, 500);
    };
    waitForJob();
  }
  player.addEventListener('error', () => {if (source) error.hidden = false;});
  fileInput.addEventListener('change', () => {selectedFile = fileInput.files[0] || null;});
  document.addEventListener('drop', event => {
    if (!fileInput.disabled && event.dataTransfer?.files[0]) selectedFile = event.dataTransfer.files[0];
  }, true);
  start.addEventListener('click', () => begin(true), true);
  new MutationObserver(() => {
    if (shell.classList.contains('hidden')) {
      stop();player.pause();release();
    } else if (film.classList.contains('is-error') && !source) {
      stop();panel.hidden = true;container.classList.remove('analysis-previewing');
    }
  }).observe(shell, {attributes: true, attributeFilter: ['class'], subtree: true});
  window.addEventListener('pagehide', () => {stop();release();});
  if (/^[a-f0-9]{8}$/.test(new URLSearchParams(location.search).get('job') || '')) begin();
})();
