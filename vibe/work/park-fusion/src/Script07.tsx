import React,{useEffect,useState} from 'react';
import {AbsoluteFill,Audio,Img,Loop,OffthreadVideo,Sequence,staticFile,useCurrentFrame,delayRender,continueRender,cancelRender} from 'remotion';
import {Presenter} from './Presenter';
import {CardSheets,cardPose} from './CardMotion';
import type {Shot} from './Workbench';
import data from './script07-data.json';

const WHITE='#f3f1e9',GOLD='#e7c887',MUTED='#9ba9b4',GREEN='#8bbda8',INK='#111923';
const clamp=(v:number)=>Math.max(0,Math.min(1,v));
const ease=(v:number)=>1-Math.pow(1-clamp(v),3);
const font='ParkNoto, Microsoft YaHei, sans-serif';
function Font(){const [handle]=useState(()=>delayRender('载入中文字体'));useEffect(()=>{const face=new FontFace('ParkNoto',`url(${staticFile('fonts/NotoSansSC.ttf')})`);face.load().then(f=>{document.fonts.add(f);continueRender(handle);}).catch(cancelRender);},[handle]);return null;}

function Paper({children,title,tag,style={}}:{children:React.ReactNode;title:string;tag?:string;style?:React.CSSProperties}){
 return <div style={{background:'#eee9dc',color:'#24323d',padding:'30px 36px',borderRadius:18,boxShadow:'0 20px 45px #0005',...style}}><div style={{display:'flex',alignItems:'center',justifyContent:'space-between',borderBottom:'2px solid #c5c2b6',paddingBottom:18,marginBottom:24}}><strong style={{fontSize:35,letterSpacing:2}}>{title}</strong><span style={{fontSize:19,color:'#737f7e'}}>{tag||'图解示意'}</span></div>{children}</div>;
}

function Timeline({f}:{f:number}){
 const rows=[['装修调整',.78],['生产停机',.59],['员工返岗',.91]] as const;
 return <Paper title="搬迁时间表" tag="企业顾虑示意" style={{position:'absolute',left:42,right:42,top:483,height:396,transform:`perspective(1600px) rotateX(${3*(1-ease(f/20))}deg)`}}>
 {rows.map(([label,extent],i)=>{const q=ease((f-12-i*13)/21);return <div key={label} style={{display:'flex',alignItems:'center',gap:28,margin:'23px 0',opacity:.4+.6*q}}><span style={{width:156,fontSize:31}}>{label}</span><div style={{height:22,background:'#d7d9cf',borderRadius:20,flex:1,overflow:'hidden'}}><div style={{width:`${extent*q*100}%`,height:'100%',background:i===1?'#ac795f':'#788c77',borderRadius:20}}/></div><span style={{fontSize:24,color:'#8b7361'}}>时间</span></div>})}
 <div style={{fontSize:24,color:'#786d5b',marginTop:20}}>租金以外，还要核算搬迁成本。</div></Paper>;
}

function Ownership({f}:{f:number}){
 const q=ease((f-10)/28);
 return <div style={{position:'absolute',left:40,right:40,top:500,height:370,padding:34,border:'1px solid #647765',borderRadius:23,background:'linear-gradient(135deg,#1c2c2a,#111923)'}}>
  <div style={{color:GOLD,fontSize:31,letterSpacing:4}}>只有一个业主</div><div style={{fontSize:21,color:MUTED,marginTop:10}}>产权关系图解 · 自持范围到访核验</div>
  <div style={{display:'flex',alignItems:'center',gap:25,marginTop:40}}>
   <div style={{width:250,border:'2px solid '+GOLD,borderRadius:16,padding:'22px 16px',textAlign:'center',fontSize:43,color:WHITE}}>产权方</div>
   <div style={{flex:1,textAlign:'center',opacity:q}}><div style={{fontSize:24,color:GOLD,marginBottom:14}}>直接签约</div><svg width="260" height="35" viewBox="0 0 260 35"><path d={`M8 18H${12+q*230}`} stroke={GOLD} strokeWidth="4"/><path d="M231 7L247 18L231 29" stroke={GOLD} strokeWidth="4" fill="none" opacity={q}/></svg></div>
   <div style={{width:250,border:'1px solid #6c8a83',borderRadius:16,padding:'22px 16px',textAlign:'center',fontSize:43,color:WHITE}}>企业</div>
  </div><div style={{fontSize:25,color:'#b9c4bd',marginTop:34,textAlign:'center'}}>只租不售　／　减少转包环节</div>
 </div>;
}

function Service({f}:{f:number}){
 const rows=[['房屋与设施','物业职责'],['经营活动','企业自主'],['检查流程','以官方通知为准']];
 return <Paper title="服务边界清单" tag="剧本要点示意" style={{position:'absolute',left:42,right:42,top:482,height:439}}>
 {rows.map(([a,b],i)=>{const q=ease((f-10-i*15)/18);return <div key={a} style={{display:'flex',alignItems:'center',fontSize:30,padding:'17px 0',borderBottom:'1px solid #d4d3c7',opacity:.3+.7*q}}><span style={{fontSize:31,color:'#627d64',width:48}}>{q>.6?'✓':'·'}</span><span>{a}</span><span style={{marginLeft:'auto',fontSize:28,color:'#5a6f63'}}>{b}</span></div>})}
 <div style={{fontSize:22,color:'#857561',marginTop:22}}>实际服务边界须有对应书面文件。</div></Paper>;
}

function Contract({f}:{f:number}){
 const q=ease((f-8)/28);
 return <div style={{position:'absolute',left:36,right:36,top:490,height:410}}>
 {['租赁合同','服务边界附件'].map((title,i)=><Paper key={title} title={title} tag="条款示意" style={{position:'absolute',width:430,height:320,left:i?455:0,top:i?18:0,transform:`perspective(1800px) rotateY(${(i?-1:1)*(3+9*(1-q))}deg) translateY(${(1-q)*30}px)`,padding:'26px 26px'}}>
  {[85,70,92,60].map((w,j)=><div key={j} style={{width:w+'%',height:6,background:j===2?'#c2ac77':'#c7cbc3',margin:'22px 0'}}/>)}
  <div style={{fontSize:25,color:'#6b8067',marginTop:23}}>{i?'把服务边界写清楚':'把约定写成条款'}</div>
 </Paper>)}
 <div style={{position:'absolute',bottom:0,left:15,right:15,textAlign:'center',fontSize:26,color:GOLD}}>口头承诺　→　书面条款</div></div>;
}

function Appointment({f}:{f:number}){
 const q=ease((f-14)/22);
 return <><div style={{position:'absolute',left:542,top:208,width:345,height:566,background:'#f0eee4',border:'10px solid #333e46',borderRadius:42,transform:`perspective(1500px) rotateY(${-8+5*q}deg) translateY(${(1-q)*45}px)`,boxShadow:'0 30px 50px #0007',padding:'45px 22px',color:'#30423a'}}>
  <div style={{position:'absolute',top:13,left:118,width:88,height:10,borderRadius:8,background:'#303b40'}}/>
  <div style={{fontSize:23,color:'#8c9988',marginTop:14}}>预约流程示意</div><div style={{fontSize:37,fontWeight:700,marginTop:24,lineHeight:1.4}}>预约到访<br/>嘉定项目</div><div style={{fontSize:24,marginTop:44}}>选择到访时间段</div>
  <div style={{display:'flex',gap:10,marginTop:18}}>{['上午','下午'].map((t,i)=><span key={t} style={{padding:'16px 22px',fontSize:26,borderRadius:10,border:'1px solid #a8b09c',background:i===0&&q>.8?'#dbe5c8':'#f5f3eb'}}>{t}</span>)}</div>
  <div style={{fontSize:21,marginTop:28,color:'#8c9988'}}>到访时核对产权<br/>与服务边界文件</div>
 </div><div style={{position:'absolute',left:52,top:625,fontSize:40,color:WHITE,lineHeight:1.6}}>把问题<br/><span style={{color:GOLD}}>留到现场核验。</span></div>
 <div style={{position:'absolute',left:52,right:52,bottom:34,background:GOLD,color:'#202b2a',borderRadius:18,padding:'22px 20px',textAlign:'center',fontSize:38,fontWeight:700,opacity:.45+.55*q}}>预约看房　↗</div></>;
}

export type ArtworkOptions={motionShot?:Shot;source?:string;sourceType?:string;sourceStart?:number;sourceFrames?:number;speed?:number;duration?:number;presenter?:{enabled?:boolean;placement?:string;grounded?:boolean;fixedHead?:boolean;fixedMouth?:boolean};assetBase?:string};
export function Artwork({index,globalFrame,options={}}:{index:number;globalFrame:number;options?:ArtworkOptions}){
 const f=useCurrentFrame();const scene=data.scenes[index];const q=ease(f/17);const hold=clamp(f/(options.duration??scene.duration));
 const motionShot=options.motionShot??({duration:scene.duration,cardEffect:'none'} as Shot);const motion=cardPose(motionShot,f);
 const source=options.source??staticFile(scene.footage);const mediaStyle:React.CSSProperties={width:'100%',height:'100%',objectFit:'cover',transform:`scale(${1+.018*hold})`};
 return <><CardSheets shot={motionShot} frame={f}/><div style={{position:'absolute',top:545,left:54,width:972,height:946,border:'1px solid #8b98a155',borderRadius:34,overflow:'hidden',background:INK,boxShadow:'0 30px 80px #0005',opacity:(.7+.3*q)*motion.opacity,filter:`blur(${motion.blur}px)`,transform:`translateY(${(1-q)*20}px) ${motion.transform}`}}>
  <div style={{height:548,overflow:'hidden',position:'relative',transform:`translateX(${motion.mediaX}px)`}}>{options.sourceType==='image'?<Img src={source} style={mediaStyle}/>:<Loop durationInFrames={Math.max(1,Math.floor(((options.sourceFrames??Math.max(scene.duration,300))-(options.sourceStart??0))/(options.speed??1)))}><OffthreadVideo src={source} startFrom={options.sourceStart??0} playbackRate={options.speed??1} muted style={mediaStyle}/></Loop>}<div style={{position:'absolute',inset:0,background:'linear-gradient(transparent 20%,#11192366 75%,#111923)'}}/>
   <span style={{position:'absolute',top:28,left:30,color:GOLD,fontSize:25,padding:'10px 20px',background:'#101923d9',borderRadius:30}}>嘉定项目 · 现场实拍</span>
  </div>
  {options.presenter?.enabled!==false&&<div style={{position:'absolute',left:options.presenter?.placement==='center'?340:index===5?45:undefined,right:options.presenter?.placement==='center'?undefined:index===5?undefined:10,top:index===5?360:291,width:index===5?255:238,height:240}}><Presenter frame={globalFrame%900} energyOverride={data.energy[Math.round(globalFrame)]??0} width={index===5?255:238} side="right" top={0} grounded={options.presenter?.grounded} stillHead={options.presenter?.fixedHead} fixedMouth={options.presenter?.fixedMouth} assetBase={options.assetBase}/></div>}
  <AbsoluteFill style={{transform:`translateX(${motion.diagramX}px)`}}>{index===0&&<><div style={{position:'absolute',top:88,left:338,fontSize:320,lineHeight:1,color:GOLD,textShadow:'0 10px 40px #0008',opacity:clamp((64-f)/12)*q,transform:`scale(${.85+.15*q})`}}>?</div><div style={{position:'absolute',left:50,right:50,top:629,color:WHITE,fontSize:42,lineHeight:1.7}}>看得见的厂房，<br/><span style={{color:GOLD,fontSize:50}}>先问清楚产权方。</span></div><div style={{position:'absolute',left:50,top:826,color:MUTED,fontSize:25}}>从“这栋楼是谁的”开始。</div></>}
  {index===1&&<Timeline f={f}/>} {index===2&&<Ownership f={f}/>} {index===3&&<Service f={f}/>} {index===4&&<Contract f={f}/>} {index===5&&<Appointment f={f}/>}</AbsoluteFill>
 </div></>;
}

export const Script07:React.FC<{plate?:boolean}> = ({plate=false})=>{
 const f=useCurrentFrame();const scene=data.scenes.find(s=>f>=s.from&&f<s.from+s.duration)??data.scenes[5];const local=f-scene.from;const q=ease(local/16);
 const sub=data.subtitles.find(s=>f>=s.from&&f<=s.to);const endFade=clamp((data.totalFrames-f)/14);
 const music=(frame:number)=>{const phase=data.scenes.findIndex(s=>frame>=s.from&&frame<s.from+s.duration);const levels=[.48,.3,.42,.018,.04,.34];return (levels[phase]??.2)*Math.min(clamp(frame/18),clamp((data.totalFrames-frame)/30));};
 return <AbsoluteFill style={{background:'#0e151e',fontFamily:font,color:WHITE}}><Font/>
  <Sequence from={scene.from} durationInFrames={scene.duration}><AbsoluteFill style={{opacity:.29,filter:'blur(36px)',transform:'scale(1.15)'}}><OffthreadVideo src={staticFile(scene.footage)} muted style={{width:'100%',height:'100%',objectFit:'cover'}}/></AbsoluteFill></Sequence>
  <AbsoluteFill style={{background:'linear-gradient(180deg,#0c141dc9,#0e1722b0 38%,#0c141ef8)'}}/>
  <div style={{position:'absolute',inset:38,border:'1px solid #76899324',borderRadius:36}}/>
  <div style={{position:'absolute',top:91,left:78,right:78,display:'flex',justifyContent:'space-between',alignItems:'center',fontSize:27,letterSpacing:3,color:MUTED}}><span>谷川高科 <span style={{color:GOLD}}>／</span> 嘉定</span><span style={{border:'1px solid #b7c4c43b',borderRadius:30,padding:'10px 21px',fontSize:24,letterSpacing:1}}>{String(scene.index+1).padStart(2,'0')} · {scene.label}</span></div>
  <div style={{position:'absolute',top:212,left:78,right:65,transform:`translateY(${(1-q)*10}px)`,opacity:.55+.45*q}}>{scene.title.map((line,i)=><div key={i} style={{fontSize:scene.index===5?77:86,fontWeight:800,lineHeight:1.26,letterSpacing:1,color:i?GOLD:WHITE}}>{line}</div>)}</div>
  <div style={{position:'absolute',left:80,top:467,width:76+110*clamp(local/(scene.duration-20)),height:4,background:GOLD,opacity:.8}}/>
  {data.scenes.map(s=><Sequence key={s.index} from={s.from} durationInFrames={s.duration}><Artwork index={s.index} globalFrame={f}/></Sequence>)}
  {!plate&&sub&&<div style={{position:'absolute',left:68,right:68,top:1545,minHeight:108,padding:'18px 28px',borderRadius:18,background:'#080e15dd',display:'flex',alignItems:'center',justifyContent:'center',textAlign:'center',fontSize:44,lineHeight:1.5,color:WHITE}}>{sub.text}</div>}
  <div style={{position:'absolute',top:1720,left:80,right:80,display:'flex',gap:12}}>{data.scenes.map(s=><div key={s.index} style={{height:4,flex:1,background:'#67717b66',borderRadius:5,overflow:'hidden'}}><div style={{height:'100%',width:clamp((f-s.from)/s.duration)*100+'%',background:GOLD}}/></div>)}</div>
  <div style={{position:'absolute',top:1777,left:75,right:75,textAlign:'center',fontSize:21,color:'#8c9aa7',lineHeight:1.8}}>剧本演绎 · 产权与自持范围到访核验<br/>承诺内容以书面文件及实际合同为准</div>
  <div style={{position:'absolute',top:1848,left:80,right:80,textAlign:'center',fontSize:18,color:'#677684',letterSpacing:3}}>JIADING　／　ONE OWNER, DIRECT LEASE</div>
  {!plate&&<><Audio src={staticFile(data.voice)} volume={1}/><Audio src={staticFile('audio/full-narration-music.wav')} volume={music} loop loopVolumeCurveBehavior="extend"/>{data.scenes.slice(1).filter(s=>s.index!==3&&s.index!==4).map(s=><Sequence key={s.index} from={s.from} durationInFrames={Math.min(40,s.duration)}><Audio src={staticFile(s.index===2?'audio/transition-snap.mp3':'audio/transition-soft.mp3')} volume={s.index===2?.23:.14}/></Sequence>)}</>}
 </AbsoluteFill>;
};
