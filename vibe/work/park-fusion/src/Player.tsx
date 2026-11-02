import React from 'react';
import {createRoot} from 'react-dom/client';
import {Player,PlayerRef} from '@remotion/player';
import {Workbench,Config,normalize,durationOf} from './Workbench';
declare global{interface Window{mountVibePlayer:(element:HTMLElement,config:Config,callback?:(e:any)=>void)=>any}}
window.mountVibePlayer=(element,initial,callback)=>{const root=createRoot(element);const ref=React.createRef<PlayerRef>();let config=normalize(initial);const draw=()=>{if(config.assetBase)(window as any).remotion_staticBase=config.assetBase;root.render(<Player ref={ref} component={Workbench} inputProps={{config}} durationInFrames={durationOf(config)} compositionWidth={config.format==='landscape'?1920:1080} compositionHeight={config.format==='landscape'?1080:1920} fps={30} controls loop style={{width:'100%',height:'100%',maxHeight:'100%'}} clickToPlay doubleClickToFullscreen acknowledgeRemotionLicense/>)};draw();let last=-1;const tick=setInterval(()=>{const frame=ref.current?.getCurrentFrame();if(frame!==undefined&&frame!==last){last=frame;callback?.({type:'frame',frame});element.dispatchEvent(new CustomEvent('vibe-frame',{detail:{frame}}))}},100);const api={update:(next:Config)=>{config=normalize(next);draw();callback?.({type:'update',duration:durationOf(config)})},seek:(frame:number)=>ref.current?.seekTo(Math.max(0,Math.min(durationOf(config)-1,frame))),play:()=>ref.current?.play(),pause:()=>ref.current?.pause(),getCurrentFrame:()=>ref.current?.getCurrentFrame()??0,destroy:()=>{clearInterval(tick);root.unmount()}};return api};


