import React from 'react';
import {staticFile} from 'remotion';
import metadata from '../public/realistic/metadata.json';

const view=(a:typeof metadata.machine)=>`${a.box[0]} ${a.box[1]} ${a.box[2]-a.box[0]} ${a.box[3]-a.box[1]}`;
export const RealMachine:React.FC<{x:number;y:number;s?:number}>=({x,y,s=1})=><svg x={x-150*s} y={y-264*s} width={300*s} height={280*s} viewBox={view(metadata.machine)} preserveAspectRatio="xMidYMax meet" overflow="hidden">
 <image href={staticFile('realistic/machine.png')} width={metadata.machine.width} height={metadata.machine.height}/>
</svg>;
export const RealEntrance:React.FC<{x:number;y:number;w:number;h:number}>=({x,y,w,h})=><svg x={x} y={y} width={w} height={h} viewBox={view(metadata.entrance)} preserveAspectRatio="xMidYMax meet" overflow="hidden">
 <image href={staticFile('realistic/entrance.png')} width={metadata.entrance.width} height={metadata.entrance.height}/>
</svg>;
