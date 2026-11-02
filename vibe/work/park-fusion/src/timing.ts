import timing from './full-narration-timing.json';
export const SHOTS=timing.shots;
export const TOTAL=timing.totalFrames;
export const retimeFrame=(frame:number)=>{
 const s=SHOTS.find(s=>frame>=s.baseFrom&&frame<s.baseFrom+s.baseDuration)??SHOTS[SHOTS.length-1];
 return Math.round(s.from+(frame-s.baseFrom)*s.duration/s.baseDuration);
};
