import React from 'react';
import {AbsoluteFill,Audio,Composition,Easing,interpolate,OffthreadVideo,Sequence,staticFile,useCurrentFrame} from 'remotion';
import voice from './full-narration-voice.json';
import subtitles from './full-narration-subs.json';
import {Presenter} from './Presenter';
import {SHOTS,TOTAL,retimeFrame} from './timing';
import {RealMachine,RealEntrance} from './RealisticAssets';

const INK='#090d14',WHITE='#f2eee5',GOLD='#e8c984',PURPLE='#b798ff',MUTED='#a1a9b6';
const opt={extrapolateLeft:'clamp',extrapolateRight:'clamp'} as const;
const p=(f:number,a:number,b:number)=>interpolate(f,[a,b],[0,1],{...opt,easing:Easing.bezier(.22,1,.36,1)});
const soft=(f:number,a:number,b:number)=>interpolate(f,[a,b],[0,1],{...opt,easing:Easing.inOut(Easing.cubic)});
const font='ParkNoto, Microsoft YaHei, sans-serif';
const AnimationFrame=React.createContext<number|null>(null);
const NarrationFrame=React.createContext(0);
const GuideOpacity=React.createContext(1);
const CenteredGuide=React.createContext(false);
const GroundedGuide=React.createContext(false);
const StillHeadGuide=React.createContext(false);
const FixedMouthGuide=React.createContext(false);
const PresenterVisible=React.createContext(true);
export type TitleOverride={text?:string;fontSize?:number;color?:string;typography?:string};
const TitleSettings=React.createContext<TitleOverride|null>(null);
const useAnimationFrame=()=>{const actual=useCurrentFrame();const visual=React.useContext(AnimationFrame);return visual??actual;};
const SceneClock:React.FC<{duration:number;baseDuration:number;children:React.ReactNode}>=({duration,baseDuration,children})=>{const f=useCurrentFrame();return <AnimationFrame.Provider value={f*baseDuration/duration}>{children}</AnimationFrame.Provider>};
const BackVideo:React.FC<{first:boolean}>=({first})=>{const f=useCurrentFrame();return <AbsoluteFill style={{opacity:first?1:soft(f,0,12)}}><OffthreadVideo src={staticFile('park.mp4')} muted style={{width:'100%',height:'100%',objectFit:'cover',transform:'scale(1.12)',filter:'blur(26px) brightness(0.62) saturate(0.8)'}}/></AbsoluteFill>};

const Back:React.FC<{offset?:number}>=({offset=0})=> <AbsoluteFill style={{background:INK,overflow:'hidden'}}>
 {Array.from({length:Math.ceil(TOTAL/1055)},(_,i)=><Sequence key={i} from={i*1055-offset} durationInFrames={1067}><BackVideo first={i===0}/></Sequence>)}
 <AbsoluteFill style={{background:'linear-gradient(180deg,#090d1444 0%,#090d1414 42%,#090d1466 100%)'}}/>
 <div style={{position:'absolute',inset:38,border:'1px solid #ffffff0d',borderRadius:24}}/>
</AbsoluteFill>;
const Title:React.FC<{kicker:string;line1:string;line2:string;accent?:string}>=({kicker,line1,line2,accent=GOLD})=>{const override=React.useContext(TitleSettings);const f=useCurrentFrame();if(!override)return <div style={{position:'absolute',left:78,top:203,right:66}}>
 <div style={{fontSize:28,letterSpacing:5,color:MUTED,marginBottom:32}}>{kicker}</div>
 <div style={{fontSize:94,lineHeight:1.22,fontWeight:900,letterSpacing:-4}}>{line1}<br/><span style={{color:accent,textShadow:`0 4px 42px ${accent}22`}}>{line2}</span></div>
 </div>;const lines=override.text===undefined?[line1,line2]:override.text.split(/\r?\n/);let cursor=0;return <div style={{position:'absolute',left:78,top:203,right:66}}><div style={{fontSize:28,letterSpacing:5,color:MUTED,marginBottom:32}}>{kicker}</div><div style={{fontSize:override.fontSize??94,lineHeight:1.22,fontWeight:900,letterSpacing:-4}}>{lines.map((line,i)=><React.Fragment key={i}>{i>0?<br/>:null}<span style={{color:override.color??(i>0?accent:WHITE),textShadow:i>0?`0 4px 42px ${override.color??accent}22`:undefined}}>{override.typography==='kinetic'?Array.from(line).map((char,j)=>{const index=cursor++;const q=p(f,index*2,index*2+10);return <span key={j} style={{display:'inline-block',whiteSpace:char===' '?'pre':undefined,opacity:q,transform:`translateY(${14*(1-q)}px)`}}>{char}</span>}):line}</span></React.Fragment>)}</div></div>};
const SceneCamera:React.FC<{mode?:string;duration:number;children:React.ReactNode}>=({mode='static',duration,children})=>{const f=useCurrentFrame();if(mode==='static')return <>{children}</>;const q=Math.min(1,f/Math.max(1,duration-40));const transform=mode==='depth'?`perspective(1800px) rotateY(${3*Math.sin(q*Math.PI)}deg) scale(${1+.025*q})`:`translate(${-15*q}px,${-18*q}px) scale(${1+.035*q})`;return <AbsoluteFill style={{transform,transformOrigin:'50% 50%'}}>{children}</AbsoluteFill>};

// A single footage surface and its annotations share the same perspective.
const FilmCard:React.FC<{x:number;y:number;w:number;h:number;start:number;children?:React.ReactNode;style?:React.CSSProperties;dim?:number;guide?:'left'|'right';guideWidth?:number;centerGuideWidth?:number;guideTop?:number;guideSoftBottom?:boolean}>=({x,y,w,h,start,children,style,dim=.15,guide,guideWidth=300,centerGuideWidth=420,guideTop,guideSoftBottom=false})=>{const centered=React.useContext(CenteredGuide);const grounded=React.useContext(GroundedGuide);const guideNode=guide?<CardPresenter side={centered?'center':guide} width={grounded?(h>900?520:Math.min(centerGuideWidth,(h-6)*1024/1200)):centered?centerGuideWidth:guideWidth} top={centered?undefined:guideTop} softBottom={centered||guideSoftBottom} grounded={grounded}/>:null;return <div style={{position:'absolute',left:x,top:y,width:w,height:h,borderRadius:28,overflow:'hidden',background:'#1e252e',border:'1px solid #d6dbdf77',boxShadow:'0 35px 90px #0009,0 0 40px #b798ff0a',...style}}>
 <OffthreadVideo src={staticFile('park.mp4')} startFrom={start} muted style={{width:'100%',height:'100%',objectFit:'cover'}}/>
 <AbsoluteFill style={{background:`linear-gradient(0deg,#090d14bb,transparent 50%),rgba(5,8,16,${dim})`}}/>
 {grounded?guideNode:null}
 {children}
 {grounded?null:guideNode}
</div>};
const CardPresenter:React.FC<{side:'left'|'right'|'center';width:number;top?:number;softBottom?:boolean;grounded?:boolean}>=props=><div style={{position:'absolute',inset:0,pointerEvents:'none',opacity:React.useContext(PresenterVisible)?React.useContext(GuideOpacity):0}}><Presenter {...props} stillHead={React.useContext(StillHeadGuide)} fixedMouth={React.useContext(FixedMouthGuide)} frame={React.useContext(NarrationFrame)}/></div>;
const SmallTag:React.FC<{text:string;x?:number;y?:number;color?:string}>=({text,x=28,y=25,color=GOLD})=><div style={{position:'absolute',left:x,top:y,fontSize:27,fontWeight:600,color,background:'#0b101ce0',border:`1px solid ${color}66`,borderRadius:100,padding:'8px 20px'}}>{text}</div>;

const Machine:React.FC<{x:number;y:number;s?:number;color?:string}>=({x,y,s=1})=><RealMachine x={x} y={y} s={s}/>;
const Tick:React.FC<{x:number;y:number;a?:number;color?:string}>=({x,y,a=1,color=PURPLE})=><g transform={`translate(${x} ${y})`} opacity={a}><rect width={40} height={40} rx={10} fill={`${color}22`} stroke={color} strokeWidth={2}/><path d="M9 20l9 9 15-19" fill="none" stroke={color} strokeWidth={4}/></g>;

const Hook:React.FC=()=>{const f=useAnimationFrame();const centered=React.useContext(CenteredGuide);const grounded=React.useContext(GroundedGuide);const move=p(f,2,38);const hit=p(f,38,44);return <>
 <Title kicker="定厂房，先别问租金" line1="你的设备" line2="进得来吗？"/>
 <FilmCard x={54} y={530} w={972} h={960} start={480} dim={.22} guide="right" guideWidth={300} guideTop={100} guideSoftBottom style={{transform:`perspective(1600px) rotateX(${5*(1-p(f,0,24))}deg)`}}>
  <SmallTag text="厂房现场"/>
  <svg width={972} height={960} style={{position:'absolute',inset:0}} viewBox="0 0 972 960">
   <g transform={grounded?'translate(200 -140) scale(.6)':centered?'translate(77 286) scale(.78)':undefined}>
    <ellipse cx={190+move*250} cy={792} rx={165} ry={18} fill="#000" opacity={.32}/>
    <RealEntrance x={590} y={460} w={360} h={330}/>
    <text x={770} y={550} textAnchor="middle" fontSize={grounded?40:30} fill="#e5e8ec" stroke="#17212f" strokeWidth={1} paintOrder="stroke">入口净高</text>
    <Machine x={190+move*250} y={771.12} s={1.18}/>
    <path d="M610 470v174M597 470h26M597 644h26" fill="none" stroke={GOLD} strokeWidth={4} opacity={hit}/>
    <g opacity={hit}><circle cx={640} cy={611} r={27} fill="#381d20" stroke="#f09c79" strokeWidth={3}/><path d="M629 600l22 22m0-22l-22 22" stroke="#f09c79" strokeWidth={4}/></g>
   </g>
   <text x={486} y={grounded?110:centered?936:900} textAnchor="middle" fontSize={28} fill={WHITE}>设备与入口关系 · 概念示意</text>
  </svg>
 </FilmCard>
 </>};

const Risks:React.FC=()=>{const f=useAnimationFrame();const grounded=React.useContext(GroundedGuide);const lift=p(f,6,30);const items=[['层高','留出安装与检修空间'],['行车','核对吊装方式与吨位'],['承重','对上设备与楼板条件']];return <>
 <Title kicker="看着宽敞，还不够" line1="面积够大" line2="也可能吊不进"/>
 <FilmCard x={54} y={540} w={972} h={600} start={450} dim={.18} guide="left" guideWidth={290} style={{transform:`perspective(1800px) rotateY(${-9*(1-lift)}deg) translateY(${-20*lift}px) scale(${.96+.04*lift})`}}>
  <SmallTag text="把参数放回现场"/>
  <svg viewBox="0 0 920 530" style={{position:'absolute',inset:0}}><path d="M810 100V438M789 100h42M789 438h42" fill="none" stroke={GOLD} strokeWidth={4} strokeDasharray="420" strokeDashoffset={420*(1-p(f,10,50))}/><path d={grounded?'M140 180H300M750 180H760':'M140 180H760'} fill="none" stroke={PURPLE} strokeWidth={3} strokeDasharray="8 8" opacity={p(f,95,112)}/></svg>
 </FilmCard>
 <div style={{position:'absolute',left:80,top:1130,width:920,perspective:1000}}>{items.map(([name,desc],i)=>{const q=p(f,95+i*16,116+i*16);return <div key={name} style={{height:126,marginBottom:12,display:'flex',alignItems:'center',padding:'0 30px',border:'1px solid #a7b1c52b',background:'#17202cd9',borderRadius:18,opacity:q,transform:`translateY(${-60*(1-q)}px) rotateX(${16*(1-q)}deg)`,boxShadow:`0 6px 0 #0003`}}>
  <div style={{fontSize:50,fontWeight:800,color:i===0?GOLD:WHITE,width:185}}>{name}</div><div style={{fontSize:32,color:'#c1c8d2'}}>{desc}</div><div style={{marginLeft:'auto',color:PURPLE,fontSize:38}}>↗</div>
 </div>})}</div>
 </>};

const Production:React.FC=()=>{const f=useAnimationFrame();const centered=React.useContext(CenteredGuide);const grounded=React.useContext(GroundedGuide);return <>
 <Title kicker="先放生产，再核条件" line1="一楼，还是楼上" line2="让工序来决定" accent={PURPLE}/>
 {[0,1].map(i=>{const q=p(f,i*37+4,i*37+30);const act=p(f,i===0?30:95,i===0?77:132);return <FilmCard key={i} x={54} y={530+i*500} w={972} h={460} start={i===0?480:10} dim={.5} guide={i===1?'right':undefined} guideWidth={270} centerGuideWidth={350} style={{opacity:q,transform:`perspective(1600px) translateX(${(i===0?-180:180)*(1-q)}px) translateY(${-45*Math.sin(q*Math.PI)}px) rotateY(${(i===0?12:-12)*(1-q)}deg)`,borderColor:i===0?'#e8c98499':'#b798ff99'}}>
  <SmallTag text={i===0?'一楼 · 吊装需求':'楼上 · 装配需求'} color={i===0?GOLD:PURPLE}/>
  <svg viewBox="0 0 920 430" style={{position:'absolute',inset:0}}>
   {i===0?<><path d="M350 113H832M372 102v26m434-26v26" stroke={WHITE} strokeWidth={5}/><g transform={`translate(${480+180*act} 112)`}><rect width={90} height={22} x={-45} fill={GOLD}/><path d={`M0 22v${55-45*act}q-20 35 5 35q25 0 18-23`} fill="none" stroke={GOLD} strokeWidth={5}/></g><Machine x={480+180*act} y={385-45*act} s={.62}/></>:<g transform={centered?'translate(455 55) scale(.65)':undefined}><path d="M340 350H650M360 360v25M630 360v25" stroke={WHITE} strokeWidth={5}/><Machine x={500} y={335} s={.62} color={PURPLE}/><path d="M370 295h270" fill="none" stroke={PURPLE} strokeWidth={5} strokeDasharray="270" strokeDashoffset={270*(1-act)}/><g transform={`translate(${370+270*act} 283)`}><rect width={38} height={22} fill={GOLD}/></g></g>}
  </svg>
  <div style={{position:'absolute',left:30,top:155,fontSize:42,lineHeight:1.5,fontWeight:800}}>{i===0?<>吊装怎么走<br/><span style={{color:GOLD}}>先留出路径</span></>:<>装配怎么排<br/><span style={{color:PURPLE}}>先放进工序</span></>}</div>
  <div style={{position:'absolute',left:30,bottom:25,width:grounded&&i===1?290:undefined,fontSize:grounded&&i===1?22:24,lineHeight:grounded&&i===1?1.5:undefined,color:'#d0d2d8'}}>需求图解 · 适用条件按楼栋核验</div>
 </FilmCard>})}
 </>};

const Measure:React.FC=()=>{const f=useAnimationFrame();const centered=React.useContext(CenteredGuide);const grounded=React.useContext(GroundedGuide);return <>
 <Title kicker="让判断落在证据上" line1="能量的，现场量" line2="要查的，对资料"/>
 <FilmCard x={54} y={550} w={972} h={680} start={540} dim={.23} guide="right" guideWidth={300} style={{transform:`scale(${1+.025*p(f,0,120)})`}}>
  <SmallTag text="现场空间 / 效果参考"/>
  <svg viewBox="0 0 920 630" style={{position:'absolute',inset:0}}>
   <path d="M170 155V500M150 155h40M150 500h40" stroke={GOLD} strokeWidth={5} fill="none" strokeDasharray="500" strokeDashoffset={500*(1-p(f,8,45))}/>
   <path d={grounded?'M225 545H365M585 545H725M225 525v40M725 525v40':centered?'M225 545H725M225 525v40M725 525v40':'M225 460H625M225 440v40M625 440v40'} stroke={GOLD} strokeWidth={5} fill="none" strokeDasharray={centered?600:520} strokeDashoffset={(centered?600:520)*(1-p(f,45,85))}/>
   <g opacity={p(f,12,36)}><rect x={205} y={180} width={132} height={66} rx={12} fill="#0b111bef"/><text x={271} y={226} textAnchor="middle" fill={GOLD} fontSize={37}>层高</text></g>
   <g opacity={p(f,50,75)} transform={grounded?'translate(170 82)':centered?'translate(0 82)':undefined}><rect x={423} y={488} width={132} height={66} rx={12} fill="#0b111bef"/><text x={489} y={534} textAnchor="middle" fill={GOLD} fontSize={37}>柱距</text></g>
  </svg>
 </FilmCard>
 <div style={{position:'absolute',left:80,top:1250,width:920,height:260,borderRadius:26,background:'#141e2b',border:'1px solid #b798ff66',padding:'35px 40px',boxSizing:'border-box',opacity:p(f,95,115),transform:`translateY(${40*(1-p(f,95,115))}px)`}}>
  <div style={{fontSize:28,color:MUTED,marginBottom:22}}>资料核对</div><div style={{fontSize:49,fontWeight:800}}>行车条件 <span style={{color:PURPLE}}>＋</span> 楼板承重</div><div style={{fontSize:28,color:MUTED,marginTop:20}}>标线仅为示意 · 不代表实测数值</div>
 </div>
 </>};

const Parameters:React.FC=()=>{const f=useAnimationFrame();const centered=React.useContext(CenteredGuide);const pairs=[['设备重量','承重条件'],['外形尺寸','净空条件'],['吊装方式','行车条件'],['生产工序','楼层适用']];return <>
 <Title kicker="别只带一张面积需求" line1="带上你的设备" line2="逐项对上参数" accent={PURPLE}/>
 <div style={{position:'absolute',left:54,top:530,width:972,height:990,borderRadius:30,background:'linear-gradient(140deg,#253043,#101822)',border:'1px solid #c9d4e766',boxShadow:'0 35px 90px #0008',transform:`perspective(1800px) rotateX(${6*(1-p(f,0,30))}deg)`}}>
  <div style={{position:'absolute',left:40,top:34,fontSize:28,color:MUTED}}>楼栋参数核对单 <span style={{color:PURPLE}}>／ 示意</span></div>
  <FilmCard x={26} y={95} w={920} h={300} start={540} dim={.42} guide="right" guideWidth={250} centerGuideWidth={285}>
   <svg width={310} height={300} style={{position:'absolute',left:12,top:0}}><Machine x={155} y={250} s={.8}/></svg>
   <div style={{position:'absolute',left:centered?650:330,top:85,fontSize:centered?38:44,lineHeight:1.4,fontWeight:800}}>你的设备<br/><span style={{color:GOLD,fontSize:32}}>重量 ＋ 尺寸</span></div>
  </FilmCard>
  <div style={{position:'absolute',left:40,right:40,top:415}}>
   <div style={{display:'flex',fontSize:27,color:MUTED,padding:'0 22px 25px'}}><span style={{width:370}}>生产需求</span><span>按具体楼栋核对</span></div>
   {pairs.map(([a,b],i)=>{const q=p(f,16+i*12,35+i*12);return <div key={a} style={{position:'relative',height:102,marginBottom:12,display:'flex',alignItems:'center',background:'#111a26',border:'1px solid #ffffff19',borderRadius:14,padding:'0 22px',opacity:q,transform:`perspective(900px) translateY(${-45*(1-q)}px) rotateX(${15*(1-q)}deg)`}}><span style={{width:345,fontSize:37}}>{a}</span><span style={{color:PURPLE,fontSize:42,width:70}}>→</span><span style={{fontSize:37,color:GOLD}}>{b}</span></div>})}
  </div>
 </div>
 </>};

const Visit:React.FC=()=>{const f=useAnimationFrame();const grounded=React.useContext(GroundedGuide);const q=p(f,0,20);return <>
 <Title kicker="选厂房，从生产需求出发" line1="先问设备" line2="再问租金"/>
 <FilmCard x={54} y={530} w={972} h={650} start={720} dim={.1} guide="right" guideWidth={320} centerGuideWidth={500} style={{transform:`perspective(1600px) rotateY(${-7*(1-q)}deg) scale(${.96+.04*q})`}}>
  <SmallTag text="把空间带回真实现场"/>
  <div style={{position:'absolute',left:32,bottom:32,width:grounded?310:undefined,fontSize:28,lineHeight:grounded?1.5:undefined,color:'#f3f1e9'}}>用你的生产需求，核对每一项条件。</div>
 </FilmCard>
 <div style={{position:'absolute',top:1230,left:80,width:920,opacity:p(f,12,30),transform:`translateY(${35*(1-p(f,12,30))}px)`}}>
  <div style={{display:'flex',alignItems:'center',justifyContent:'space-between',padding:'32px 42px',background:GOLD,color:'#141721',borderRadius:22,fontSize:62,fontWeight:900,boxShadow:'0 12px 60px #e8c98422'}}>预约到访 <span>↗</span></div>
  <div style={{fontSize:39,marginTop:36,textAlign:'center'}}>带设备重量与尺寸，现场核对。</div>
  <div style={{fontSize:26,color:MUTED,marginTop:24,textAlign:'center'}}>具体条件以实楼栋及正式资料为准</div>
 </div>
 </>};

const SCENES=[Hook,Risks,Production,Measure,Parameters,Visit];
// Reusable original scene artwork. The historical compositions above keep their
// own soundtrack, timing and overlays; the workbench supplies these separately.
export const OriginalSceneArtwork:React.FC<{index:number;duration:number;sourceFrame:number;presenterEnabled?:boolean;centered?:boolean;grounded?:boolean;stillHead?:boolean;fixedMouth?:boolean;titleOverride?:TitleOverride;camera?:string}>=({index,duration,sourceFrame,presenterEnabled=true,centered=false,grounded=false,stillHead=false,fixedMouth=false,titleOverride,camera='static'})=>{const C=SCENES[Math.max(0,Math.min(5,index))];return <CenteredGuide.Provider value={centered}><GroundedGuide.Provider value={grounded}><StillHeadGuide.Provider value={stillHead}><FixedMouthGuide.Provider value={fixedMouth}><NarrationFrame.Provider value={sourceFrame}><GuideOpacity.Provider value={presenterEnabled?1:0}><SceneCamera mode={camera} duration={duration}><TitleSettings.Provider value={titleOverride??null}><SceneClock duration={duration} baseDuration={SHOTS[index]?.baseDuration??90}><C/></SceneClock></TitleSettings.Provider></SceneCamera></GuideOpacity.Provider></NarrationFrame.Provider></FixedMouthGuide.Provider></StillHeadGuide.Provider></GroundedGuide.Provider></CenteredGuide.Provider>};
const Hud:React.FC=()=>{const f=useCurrentFrame();let i=SHOTS.findIndex(s=>f>=s.from&&f<s.from+s.duration);if(i<0)i=5;return <>
 <div style={{position:'absolute',left:78,right:78,top:95,display:'flex',justifyContent:'space-between',alignItems:'center',fontSize:27,letterSpacing:3,color:MUTED}}><span>园区 VIBE <span style={{color:GOLD}}>／</span> 融合预览</span><span style={{color:WHITE,border:'1px solid #ffffff33',borderRadius:100,padding:'9px 19px',letterSpacing:1}}>{SHOTS[i].label}</span></div>
 <div style={{position:'absolute',left:80,top:1775,right:80,display:'flex',gap:8}}>{SHOTS.map((s,j)=><div key={j} style={{height:4,background:'#ffffff25',flex:s.duration,overflow:'hidden',borderRadius:3}}><div style={{height:'100%',width:`${100*Math.min(1,Math.max(0,(f-s.from)/s.duration))}%`,background:j===i?GOLD:'#8e9caf'}}/></div>)}</div>
 <div style={{position:'absolute',left:80,top:1810,right:80,color:'#818d9e',fontSize:25,textAlign:'center'}}>融合样片 · 实拍仅作效果参考</div>
 </>};
const Subtitle:React.FC<{data?:{text:string;from:number;to:number}[];style?:{fontSize?:number;color?:string;background?:boolean}}>=({data=subtitles,style})=>{const f=useCurrentFrame();const s=data.find(s=>f>=s.from&&f<=s.to);if(!s)return null;return <div style={{position:'absolute',left:65,right:65,top:1607,height:130,display:'flex',alignItems:'center',justifyContent:'center'}}><div style={{fontSize:style?.fontSize??(s.text.length>19?42:46),color:style?.color,fontWeight:600,lineHeight:1.6,textAlign:'center',textShadow:'0 3px 18px #000',background:style?.background===false?'transparent':'#090d14db',borderRadius:18,padding:'14px 24px',border:'1px solid #ffffff0b'}}>{s.text}</div></div>};
export const baselineBgm=(f:number)=>{let v=.24;for(const s of voice){const d=Math.min(Math.max(0,(s.from-f)/12),1);const e=Math.min(Math.max(0,(f-s.to)/12),1);v=Math.min(v,.105+.135*Math.max(d,e));}if(f>SHOTS[4].from)v*=.7;return v*p(f,0,12)*(1-soft(f,TOTAL-30,TOTAL));};
export const Main:React.FC<{centered?:boolean;grounded?:boolean;stillHead?:boolean;fixedMouth?:boolean;audioEnabled?:boolean;audioProfile?:string;subtitlesVisible?:boolean;presenterEnabled?:boolean;subtitleData?:{text:string;from:number;to:number}[];subtitleStyle?:{fontSize?:number;color?:string;background?:boolean};transitionFrames?:number;titleOverrides?:(TitleOverride|undefined)[];camera?:string}>=({centered=false,grounded=false,stillHead=false,fixedMouth=false,audioEnabled=true,audioProfile='selected-reference',subtitlesVisible=true,presenterEnabled=true,subtitleData,subtitleStyle,transitionFrames=12,titleOverrides,camera='static'})=> <PresenterVisible.Provider value={presenterEnabled}><CenteredGuide.Provider value={centered}><GroundedGuide.Provider value={grounded}><StillHeadGuide.Provider value={stillHead}><FixedMouthGuide.Provider value={fixedMouth}><AbsoluteFill style={{background:INK,fontFamily:font,color:WHITE}}>
 <style>{`@font-face{font-family:ParkNoto;src:url('${staticFile('fonts/NotoSansSC.ttf')}')} *{box-sizing:border-box}`}</style>
 {SHOTS.map((s,i)=>{const C=SCENES[i];return <Sequence key={s.from} from={s.from} durationInFrames={s.duration+(i<5?transitionFrames:0)}><FadeScene first={i===0} offset={s.from} duration={s.duration} transitionFrames={transitionFrames}><SceneCamera mode={camera} duration={s.duration}><TitleSettings.Provider value={titleOverrides?.[i]??null}><SceneClock duration={s.duration} baseDuration={s.baseDuration}><C/></SceneClock></TitleSettings.Provider></SceneCamera></FadeScene></Sequence>})}
 <Hud/>{subtitlesVisible&&<Subtitle data={subtitleData} style={subtitleStyle}/>}
 {audioEnabled&&audioProfile==='selected-reference'&&<Audio src={staticFile('audio/selected-realistic-halfbody.m4a')}/>} {audioEnabled&&audioProfile!=='selected-reference'&&<><Audio src={staticFile('audio/full-narration.wav')}/><Audio src={staticFile('audio/full-narration-music.wav')} volume={baselineBgm}/></>}
 {audioEnabled&&audioProfile!=='selected-reference'&&[{from:37,src:'transition-snap',v:.18},{from:102,src:'whoosh-big',v:.14},{from:305,src:'whoosh-fast',v:.15},{from:349,src:'transition-soft',v:.18},{from:517,src:'transition-soft',v:.15},{from:693,src:'transition-snap',v:.13},{from:816,src:'transition-soft',v:.17}].map((s,i)=>{const at=retimeFrame(s.from);return <Sequence key={i} from={at} durationInFrames={Math.min(60,TOTAL-at)}><Audio src={staticFile(`audio/${s.src}.mp3`)} volume={s.v}/></Sequence>})}
 </AbsoluteFill></FixedMouthGuide.Provider></StillHeadGuide.Provider></GroundedGuide.Provider></CenteredGuide.Provider></PresenterVisible.Provider>;
const FadeScene:React.FC<{first:boolean;offset:number;duration:number;transitionFrames?:number;children:React.ReactNode}>=({first,offset,duration,children,transitionFrames=12})=>{const f=useCurrentFrame();const guide=transitionFrames===0?1:(first?1:soft(f,transitionFrames*.5,transitionFrames*1.5))*(1-soft(f,duration-Math.min(8,transitionFrames),duration));return <AbsoluteFill style={{opacity:first||transitionFrames===0?1:soft(f,0,transitionFrames)}}><Back offset={offset}/><NarrationFrame.Provider value={f+offset}><GuideOpacity.Provider value={guide}>{children}</GuideOpacity.Provider></NarrationFrame.Provider></AbsoluteFill>};
const StillHeadQA:React.FC<{fixedMouth?:boolean}>=({fixedMouth=false})=> <AbsoluteFill style={{background:'#1e252e'}}><Presenter width={1024} side="left" grounded stillHead fixedMouth={fixedMouth}/></AbsoluteFill>;
export const Root:React.FC=()=> <>
 <Composition id="ParkFusion" component={Main} width={1080} height={1920} fps={30} durationInFrames={TOTAL}/>
 <Composition id="ParkFusionCentered" component={Main} defaultProps={{centered:true,audioProfile:'selected-baseline'}} width={1080} height={1920} fps={30} durationInFrames={TOTAL}/>
 <Composition id="ParkFusionGrounded" component={Main} defaultProps={{centered:true,grounded:true,audioProfile:'selected-baseline'}} width={1080} height={1920} fps={30} durationInFrames={TOTAL}/>
 <Composition id="ParkFusionStillHead" component={Main} defaultProps={{centered:true,grounded:true,stillHead:true,audioProfile:'selected-baseline'}} width={1080} height={1920} fps={30} durationInFrames={TOTAL}/>
 <Composition id="ParkFusionFixedMouth" component={Main} defaultProps={{centered:true,grounded:true,stillHead:true,fixedMouth:true,audioProfile:'selected-baseline'}} width={1080} height={1920} fps={30} durationInFrames={TOTAL}/>
 <Composition id="StillHeadQA" component={StillHeadQA} width={1024} height={1200} fps={30} durationInFrames={TOTAL}/>
 <Composition id="FixedMouthQA" component={StillHeadQA} defaultProps={{fixedMouth:true}} width={1024} height={1200} fps={30} durationInFrames={TOTAL}/>
</>;





