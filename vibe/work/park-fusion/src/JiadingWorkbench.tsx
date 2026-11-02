import React from 'react';
import {AbsoluteFill,Audio,Img,Loop,OffthreadVideo,Sequence,staticFile,useCurrentFrame,delayRender,continueRender,cancelRender} from 'remotion';
import {getVideoMetadata} from '@remotion/media-utils';
import {Artwork} from './Script07';
import {CardLight,cardPose} from './CardMotion';
import data from './script07-data.json';
import type {Config,Shot} from './Workbench';

const clamp=(v:number)=>Math.max(0,Math.min(1,v));
const ease=(v:number)=>1-Math.pow(1-clamp(v),3);
const GOLD='#e7c887',WHITE='#f3f1e9';
const asset=(c:Config,p:string)=>c.assetBase?`${c.assetBase}/${p}`:staticFile(p);
const src=(c:Config,p:string)=>p.startsWith('__STATIC__')?asset(c,p.slice(10)):p;
export const jiadingIndex=(shot:Shot)=>Math.max(0,Math.min(5,Number(shot.recipe?.slice(8))||0));
export const jiadingAudioIndex=(shot:Shot)=>shot.audioScene??data.scenes.find(s=>(shot.sourceOffset??0)>=s.from&&(shot.sourceOffset??0)<s.from+s.duration)?.index??jiadingIndex(shot);
export function JiadingScene({c,shot}:{c:Config;shot:Shot}){
 const f=useCurrentFrame(),index=jiadingIndex(shot),base=data.scenes[index],q=ease(f/16);
 const source=shot.source?src(c,shot.source):asset(c,base.footage);
 const [frames,setFrames]=React.useState(shot.sourceDuration??300);
 const handle=React.useMemo(()=>shot.sourceType==='image'?null:delayRender('读取嘉定镜头素材'),[source,shot.sourceType]);
 React.useEffect(()=>{if(handle===null)return;getVideoMetadata(source).then(meta=>{setFrames(Math.max(1,Math.floor(meta.durationInSeconds*30)));continueRender(handle);}).catch(cancelRender);},[source,handle]);
 const offset=(shot.footageOffset??0)%frames,rate=shot.speed??1;
 const global=(shot.sourceOffset??base.from)+f*rate;const motion=cardPose(shot,f);
 const camera=c.camera==='continuous'?`scale(${1+.025*clamp(f/shot.duration)})`:c.camera==='depth'?`perspective(1800px) rotateY(${2*Math.sin(f/shot.duration*Math.PI)}deg)`:undefined;
 const media={width:'100%',height:'100%',objectFit:'cover'} as React.CSSProperties;
 return <AbsoluteFill style={{background:'#0e151e',color:WHITE,transform:camera}}>
  <AbsoluteFill style={{opacity:c.background==='footage'?1:0,filter:`blur(${c.backgroundBlur??24}px) brightness(${(c.backgroundBrightness??.64)*motion.backgroundGain})`,transform:'scale(1.15)'}}>{shot.sourceType==='image'?<Img src={source} style={media}/>:<Loop durationInFrames={Math.max(1,Math.floor((frames-offset)/rate))}><OffthreadVideo src={source} startFrom={offset} playbackRate={rate} muted style={media}/></Loop>}</AbsoluteFill>
  <AbsoluteFill style={{background:'linear-gradient(180deg,#0c141d44,#0e172218 40%,#0c141e8a)'}}/>
  <CardLight shot={shot} frame={f}/>
  <div style={{position:'absolute',inset:38,border:'1px solid #76899324',borderRadius:36}}/>
  <div style={{position:'absolute',top:91,left:78,right:78,display:'flex',justifyContent:'space-between',alignItems:'center',fontSize:27,letterSpacing:3,color:'#9ba9b4'}}><span>谷川高科 <span style={{color:GOLD}}>／</span> 嘉定</span><span style={{border:'1px solid #b7c4c43b',borderRadius:30,padding:'10px 21px',fontSize:24,letterSpacing:1}}>{String(c.shots.indexOf(shot)+1).padStart(2,'0')} · {shot.label}</span></div>
  <div style={{position:'absolute',top:212,left:78,right:65,transform:`translateY(${(1-q)*10}px)`,opacity:.55+.45*q}}>{(shot.title??base.title.join('\n')).split('\n').map((line,i)=><div key={i} style={{fontSize:shot.fontSize??(index===5?77:86),fontWeight:800,lineHeight:1.26,letterSpacing:1,color:shot.textColor??(i?GOLD:WHITE)}}>{line}</div>)}</div>
  <div style={{position:'absolute',left:80,top:467,width:76+110*clamp(f/Math.max(1,shot.duration-20)),height:4,background:GOLD,opacity:.8}}/>
  <Artwork index={index} globalFrame={global} options={{motionShot:shot,source,sourceType:shot.sourceType,sourceStart:offset,sourceFrames:frames,speed:rate,duration:shot.duration,presenter:c.presenter,assetBase:c.assetBase}}/>
  <div style={{position:'absolute',top:1777,left:75,right:75,textAlign:'center',fontSize:21,color:'#8c9aa7',lineHeight:1.8}}>剧本演绎 · 产权与自持范围到访核验<br/>承诺内容以书面文件及实际合同为准</div>
  <div style={{position:'absolute',top:1848,left:80,right:80,textAlign:'center',fontSize:18,color:'#677684',letterSpacing:3}}>JIADING　／　ONE OWNER, DIRECT LEASE</div>
 </AbsoluteFill>;
}
export function JiadingAudioMix({c}:{c:Config}){
 if(c.plate)return null;
 const duration=c.shots.reduce((n,s)=>Math.max(n,s.from+s.duration),30);
 const voice=c.narrationFile?src(c,c.narrationFile):asset(c,data.voice);
 return <>
 {c.narrationMode==='existing'&&c.shots.map(s=><Sequence key={s.id} from={s.from} durationInFrames={s.duration}><Audio src={voice} startFrom={Math.round(s.sourceOffset??0)} playbackRate={s.speed??1} volume={c.voiceGain??1} allowAmplificationDuringRender useWebAudioApi/></Sequence>)}
 {c.narrationMode==='custom'&&c.narrationFile&&<Audio src={voice} volume={c.voiceGain??1} allowAmplificationDuringRender useWebAudioApi/>}
 {(c.musicGain??1)>0&&<Audio src={c.musicFile?src(c,c.musicFile):asset(c,'audio/full-narration-music.wav')} loop loopVolumeCurveBehavior="extend" volume={f=>{const s=c.shots.find(s=>f>=s.from&&f<s.from+s.duration);return (c.musicGain??1)*([.48,.3,.42,.018,.04,.34][s?jiadingAudioIndex(s):0]??.2)*Math.min(clamp(f/18),clamp((duration-f)/30));}} allowAmplificationDuringRender useWebAudioApi/>}
 {c.sfxEnabled&&c.shots.filter(s=>s.from>0&&![3,4].includes(jiadingAudioIndex(s))).map(s=><Sequence key={s.id} from={s.from} durationInFrames={Math.min(40,s.duration)}><Audio src={asset(c,jiadingAudioIndex(s)===2?'audio/transition-snap.mp3':'audio/transition-soft.mp3')} volume={jiadingAudioIndex(s)===2?.23:.14}/></Sequence>)}
 {c.sfxEnabled&&(c.sfxEvents??[]).filter(s=>s.from<duration).map((s,i)=><Sequence key={i} from={s.from} durationInFrames={Math.min(s.duration??50,duration-s.from)}><Audio src={src(c,s.source)} volume={s.gain??.2}/></Sequence>)}
 </>;
}
