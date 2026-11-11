/* Keep media compatibility failures visible without altering source assets or jobs. */
(() => {
 'use strict';
 document.addEventListener('error',event=>{
  const media=event.target;
  if(!(media instanceof HTMLVideoElement)||!media.closest('#player'))return;
  const stage=document.querySelector('#stage');
  if(!stage)return;
  let notice=stage.querySelector('.media-compatibility');
  if(!notice){
   notice=document.createElement('div');notice.className='media-compatibility';notice.setAttribute('role','status');
   const title=document.createElement('strong');title.textContent='当前浏览器无法播放这段素材';
   const text=document.createElement('p');text.textContent='可能是视频编码不兼容。可下载原片检查，或导入浏览器支持的 H.264 视频副本。';
   const link=document.createElement('a');link.textContent='下载原始片段';link.download='';
   notice.append(title,text,link);stage.append(notice);
  }
  const src=media.currentSrc||media.src;
  const link=notice.querySelector('a');
  try{const url=new URL(src,location.href);link.hidden=url.origin!==location.origin;link.href=url.href}catch{link.hidden=true}
 },true);
 document.addEventListener('loadeddata',event=>{
  if(event.target instanceof HTMLVideoElement&&event.target.closest('#player'))document.querySelector('#stage .media-compatibility')?.remove();
 },true);
})();
