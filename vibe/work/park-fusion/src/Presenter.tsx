import React from 'react';
import {staticFile,useCurrentFrame} from 'remotion';
import energy from './full-narration-energy.json';
import speech from './full-narration-voice.json';
import {retimeFrame} from './timing';

const clamp=(v:number)=>Math.min(1,Math.max(0,v));
const ease=(x:number)=>{x=clamp(x);return x*x*(3-2*x)};
const cues=[{f:0,open:false},{f:90,open:true},{f:300,open:false},{f:387,open:true},{f:690,open:false},{f:810,open:true},{f:870,open:false}].map(c=>({...c,f:retimeFrame(c.f)}));

// Whole silhouettes preserve the original suit and hair without shoulder seams.
// Facial patches follow the audio envelope; gestures alternate between the
// supplied poses, with restrained conversational leaning.
export const Presenter:React.FC<{energyOverride?:number;assetBase?:string;frame?:number;width?:number;side?:'left'|'right'|'center';top?:number;softBottom?:boolean;grounded?:boolean;stillHead?:boolean;fixedMouth?:boolean}>=({energyOverride,assetBase,frame,width=300,side='right',top,softBottom=false,grounded=false,stillHead=false,fixedMouth=false})=>{
 const localFrame=useCurrentFrame();const f=frame??localFrame;
 const src=assetBase?`${assetBase}/presenter/introduce.png`:staticFile('presenter/introduce.png');
 const wait=assetBase?`${assetBase}/presenter/wait.png`:staticFile('presenter/wait.png');
 let i=0;for(let j=0;j<cues.length;j++)if(f>=cues[j].f)i=j;
 const c=cues[i];const q=i===0?1:ease((f-c.f)/12);
 const wOpen=(c.open?q:0)+(i>0&&cues[i-1].open?1-q:0);
 const amp=clamp(energyOverride??((energy[f]??0)*.65+(energy[Math.max(0,f-1)]??0)*.35));
 const sent=speech.find(s=>f>=s.from&&f<=s.to);
 const emphasis=sent?Math.exp(-Math.pow((f-sent.from-12)/10,2)):0;
 const lean=stillHead?0:.22*amp*Math.sin(f*.09)+.36*emphasis;
 const blinkCenters=[65,174,290,409,536,649,762,865].map(retimeFrame);
 let blink=0;for(const b of blinkCenters)blink=Math.max(blink,1-Math.abs(f-b)/3.5);blink=ease(blink);
 const mouth=1+.48*amp;
 const eyeY=stillHead?329:329+4*(1-wOpen);const eyeXs=stillHead?[330,455]:[330+8*(1-wOpen),455+3*(1-wOpen)];
 const enter=ease(f/15);
 const cropHeight=grounded?1200:930;
 const height=width*cropHeight/1024;
 const centered=side==='center';
 const headShape='M0 0H680V500L650 550L625 585L570 610L540 580L510 545L470 525V590H355V525L315 535L280 575L245 605L160 630L80 615L0 570Z';
 // Align the face/torso axis, rather than the transparent PNG's outer box.
 return <div style={{position:'absolute',left:centered?`calc(50% - ${width*395/1024}px)`:side==='left'?14:undefined,right:side==='right'?14:undefined,top:grounded?undefined:centered?'50%':top,bottom:grounded||(!centered&&top===undefined)?0:undefined,width,height,overflow:'hidden',pointerEvents:'none',opacity:enter,transform:`translateY(${centered&&!grounded?`calc(-50% + ${stillHead?0:8*(1-enter)}px)`:`${stillHead?0:8*(1-enter)}px`})`,filter:'drop-shadow(0 5px 5px #0005)',maskImage:softBottom&&!grounded?'linear-gradient(to bottom,#000 0%,#000 86%,transparent 100%)':undefined}}>
 <svg width={width} height={height} viewBox={`0 0 1024 ${cropHeight}`} style={{overflow:'hidden',transform:side==='right'?'scaleX(-1)':undefined}}>
  <defs>
   <filter id="p-head-seam" x="-10%" y="-10%" width="120%" height="120%"><feGaussianBlur stdDeviation={6}/></filter>
   <mask id="p-fixed-head" maskUnits="userSpaceOnUse" x={0} y={0} width={1024} height={1536}><path d={headShape} fill="white" filter="url(#p-head-seam)"/><rect width={680} height={500} fill="white"/></mask>
   <mask id="p-moving-body" maskUnits="userSpaceOnUse" x={0} y={0} width={1024} height={1536}><rect width={1024} height={1536} fill="white"/><path d={headShape} fill="black" filter="url(#p-head-seam)"/><rect width={680} height={500} fill="black"/></mask>
   <clipPath id="p-mouth"><ellipse cx={395} cy={418} rx={65} ry={31}/></clipPath>
   <linearGradient id="p-skin" x1="0" x2="0" y1="0" y2="1"><stop stopColor="#f8b591"/><stop offset="1" stopColor="#ffd3b9"/></linearGradient>
   {eyeXs.map((x,j)=><clipPath key={j} id={`p-eye-${j}`}><ellipse cx={x} cy={eyeY} rx={54} ry={48}/></clipPath>)}
  </defs>
  <g transform={`rotate(${lean} 435 1410)`}>
   <g mask={stillHead?'url(#p-moving-body)':undefined}>
    <image href={src} width={1024} height={1536} opacity={wOpen}/>
    <image href={wait} width={1024} height={1536} transform="translate(-59 28) scale(.96)" opacity={1-wOpen}/>
   </g>
   {stillHead?<image href={src} width={1024} height={1536} mask="url(#p-fixed-head)"/>:null}
   {fixedMouth?null:<g transform={`translate(395 418) scale(1 ${mouth}) translate(-395 -418)`} clipPath="url(#p-mouth)">
    <image href={src} width={1024} height={1536} opacity={stillHead?1:wOpen}/>
    {stillHead?null:<image href={wait} width={1024} height={1536} transform="translate(-59 28) scale(.96)" opacity={1-wOpen}/>}
   </g>}
   {eyeXs.map((x,j)=><g key={j} clipPath={`url(#p-eye-${j})`} opacity={blink>0?1:0}>
    <rect x={x-56} y={eyeY-50} width={112} height={50*blink} fill="url(#p-skin)"/>
    <rect x={x-56} y={eyeY+50-50*blink} width={112} height={50*blink} fill="url(#p-skin)"/>
    <path d={`M${x-49} ${eyeY}q49 21 98 0`} stroke="#542a21" strokeWidth={5} fill="none" opacity={blink}/>
   </g>)}
  </g>
 </svg>
 </div>;
};
