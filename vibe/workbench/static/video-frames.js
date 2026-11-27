// Extract reference stills locally; no video generation service is involved.
window.extractSceneFrames = async (file, count) => {
 const video = document.createElement('video');
 const url = URL.createObjectURL(file);
 const waitFor = (event, action) => new Promise((resolve, reject) => {
  const timer = setTimeout(() => finish(new Error('视频读取超时，请换用浏览器支持的视频格式')), 30000);
  function finish(error) { clearTimeout(timer); video.removeEventListener(event, onReady); video.removeEventListener('error', onError); error ? reject(error) : resolve(); }
  function onReady() { finish(); }
  function onError() { finish(new Error('无法解码视频，请尝试 MP4（H.264）或 WebM')); }
  video.addEventListener(event, onReady, {once:true});
  video.addEventListener('error', onError, {once:true});
  if (action) action();
 });
 try {
  video.preload = 'auto'; video.muted = true;
  await waitFor('loadedmetadata', () => { video.src = url; });
  if (!Number.isFinite(video.duration) || video.duration <= 0 || !video.videoWidth || !video.videoHeight) throw Error('视频没有可用画面');
  const canvas = document.createElement('canvas');
  canvas.width = video.videoWidth; canvas.height = video.videoHeight;
  const context = canvas.getContext('2d');
  const frames = [];
  for (let i = 0; i < count; i++) {
   const seconds = video.duration * (i + 0.5) / count;
   await waitFor('seeked', () => { video.currentTime = seconds; });
   context.drawImage(video, 0, 0);
   const blob = await new Promise(resolve => canvas.toBlob(resolve, 'image/jpeg', 0.9));
   if (!blob) throw Error('视频画面转换失败');
   frames.push({seconds: Math.round(seconds * 1000) / 1000, blob});
  }
  return {duration: video.duration, frames};
 } finally { video.removeAttribute('src'); video.load(); URL.revokeObjectURL(url); }
};
