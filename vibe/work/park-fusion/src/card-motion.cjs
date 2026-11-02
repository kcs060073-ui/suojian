// Deterministic, media-independent poses shared by preview and export.
const clamp=v=>Math.max(0,Math.min(1,v));
const out=v=>1-Math.pow(1-clamp(v),3);
const EFFECTS=['none','spotlight-hero-card','deck-deal-flyin','card-stack','row-embed','aurora-bloom-bg-flip'];
const aliases={'depth-stack':'card-stack','exploded-view':'card-stack'};
function effectOf(shot){const key=shot.cardEffect??(shot.cardMotion?shot.recipe:'none');return aliases[key]??(EFFECTS.includes(key)?key:'none');}
function pose(shot,frame){
 const effect=effectOf(shot),intensity=Math.max(0,Math.min(1.5,shot.cardIntensity??1));
 const frames=Math.max(6,Math.min((shot.duration??150)-8,shot.cardEnterFrames??36));
 const q=out(frame/frames),miss=(1-q)*intensity;
 let transform='',opacity=1,blur=0,clipPath;
 if(effect==='spotlight-hero-card'){transform=`translateY(${160*miss}px) scale(${1-.26*miss})`;opacity=1-.85*miss;blur=8*miss;}
 if(effect==='deck-deal-flyin'){transform=`perspective(1800px) translate(${1040*miss}px,${190*miss}px) rotate(${-24*miss}deg)`;opacity=1-.4*miss;}
 if(effect==='card-stack'){transform=`translate(${-65*miss}px,${95*miss}px) rotate(${-7*miss}deg) scale(${1-.15*miss})`;opacity=1-.55*miss;}
 if(effect==='row-embed'){transform=`translateY(${35*miss}px)`;opacity=1-.25*miss;}
 const phase=clamp(frame/Math.max(24,Math.min(shot.duration??150,Math.max(72,frames*3))));
 const flip=effect==='aurora-bloom-bg-flip'?out((phase-.57)/.075)*intensity:0;
 if(effect==='aurora-bloom-bg-flip'){transform=`translateY(${18*miss}px)`;blur=7*Math.sin(Math.min(1,flip)*Math.PI)*intensity;}
 return {effect,intensity,progress:q,phase,flip,backgroundGain:effect==='aurora-bloom-bg-flip'?1+.3*intensity-.6*flip:1,transform,opacity:clamp(opacity),blur,clipPath,
  mediaX:effect==='row-embed'?-920*(1-out(frame/frames))*intensity:0,
  diagramX:effect==='row-embed'?920*(1-out((frame-frames*.28)/frames))*intensity:0};
}
module.exports={EFFECTS,effectOf,pose};
