import React from 'react';
import {AbsoluteFill} from 'remotion';
import motion from './card-motion.cjs';
import type {Shot} from './Workbench';
export const cardPose=(shot:Shot,frame:number)=>motion.pose(shot,frame);
export function CardLight({shot,frame}:{shot:Shot;frame:number}){
 const p=cardPose(shot,frame);if(p.effect==='none'||p.intensity===0)return null;
 if(p.effect==='aurora-bloom-bg-flip'){
  const flip=Math.max(0,Math.min(1,(p.phase-.57)/.075));
  return <AbsoluteFill style={{pointerEvents:'none',mixBlendMode:'screen',opacity:p.intensity*(.78-.3*flip)}}>
   <div style={{position:'absolute',width:1350,height:1180,left:-190,bottom:-410+p.phase*370,borderRadius:'50%',filter:'blur(90px)',background:'radial-gradient(ellipse,#8764e8aa,transparent 68%)',transform:`scale(${1+p.phase*.25-.08*flip})`}}/>
   <div style={{position:'absolute',width:780,height:900,right:-210+Math.sin(p.phase*6)*80,bottom:40+p.phase*310,borderRadius:'50%',filter:'blur(95px)',background:'radial-gradient(ellipse,#e3aa59bb,transparent 68%)'}}/>
   <AbsoluteFill style={{background:'#111b36',opacity:flip*.4}}/>
  </AbsoluteFill>;
 }
 if(p.effect==='spotlight-hero-card')return <AbsoluteFill style={{pointerEvents:'none',background:`radial-gradient(ellipse ${230+350*p.progress}px ${360+390*p.progress}px at 50% 53%,#e7c88755,transparent 78%)`,opacity:p.intensity}}/>;
 return null;
}
export function CardSheets({shot,frame,top=545,height=946}:{shot:Shot;frame:number;top?:number;height?:number}){
 const p=cardPose(shot,frame);if(!['deck-deal-flyin','card-stack'].includes(p.effect)||p.intensity===0)return null;
 return <>{[2,1].map((n)=>{
  const q=Math.max(0,Math.min(1,(frame-n*5)/Math.max(6,shot.cardEnterFrames??36)));
  const miss=Math.pow(1-q,3)*p.intensity;
  return <div key={n} style={{position:'absolute',top:top+18*n,left:54-10*n,width:972,height:height-12*n,borderRadius:34,border:`2px solid ${n===1?'#e7c88799':'#93b8ac77'}`,background:n===1?'#2b3832':'#20303b',boxShadow:'0 20px 50px #0005',transform:p.effect==='deck-deal-flyin'?`translate(${1050*miss-16*n}px,${145*miss}px) rotate(${-21*miss-2.2*n}deg)`:`translateX(${-n*24-100*miss}px) rotate(${-2*n-3*miss}deg) scale(${1-.018*n})`,opacity:.85*q}}/>;
 })}</>;
}
