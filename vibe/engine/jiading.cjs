const path=require('path');
const clamp=n=>Math.max(0,Math.min(1,n));
const scenes=require('../work/park-fusion/src/script07-data.json').scenes;
const index=s=>s.audioScene??scenes.find(b=>(s.sourceOffset??0)>=b.from&&(s.sourceOffset??0)<b.from+b.duration)?.index??Math.max(0,Math.min(5,Number(s.recipe?.slice(8))||0));
function manifest(c,project){
 const duration=c.shots.reduce((n,s)=>Math.max(n,s.from+s.duration),30);
 const media=(value,fallback)=>{value=value??fallback;if(value?.startsWith('/media/'))return path.join(project,'public',decodeURIComponent(value.slice(7)));if(value?.startsWith('__STATIC__'))return path.join(project,'public',value.slice(10));return value;};
 const audioEvents=[],subs=[];
 if(!c.plate){
  const source=media(c.narrationFile,path.join(project,'public/script07/voice.mp3'));
  if(c.narrationMode==='existing')for(const s of c.shots)audioEvents.push({track:'voice',source,from:s.from,duration:s.duration,sourceFrom:s.sourceOffset??0,sourceDuration:s.duration*(s.speed??1),playbackRate:s.speed??1,gain:c.voiceGain??1});
  if(c.narrationMode==='custom'&&c.narrationFile)audioEvents.push({track:'voice',source,from:0,duration,sourceFrom:0,playbackRate:1,gain:c.voiceGain??1});
  if(c.musicGain>0)audioEvents.push({track:'music',source:media(c.musicFile,path.join(project,'public/audio/full-narration-music.wav')),from:0,duration,sourceFrom:0,loop:true,playbackRate:1,gain:c.musicGain??1,envelope:{type:'frame-values',fps:30,interpolation:'hold-per-frame',values:Array.from({length:duration},(_,f)=>{const s=c.shots.find(s=>f>=s.from&&f<s.from+s.duration);return ([.48,.3,.42,.018,.04,.34][s?index(s):0]??.2)*Math.min(clamp(f/18),clamp((duration-f)/30));})}});
  if(c.sfxEnabled)for(const s of c.shots.filter(s=>s.from>0&&![3,4].includes(index(s))))audioEvents.push({track:'sfx',source:path.join(project,index(s)===2?'public/audio/transition-snap.mp3':'public/audio/transition-soft.mp3'),from:s.from,duration:Math.min(40,s.duration),sourceFrom:0,playbackRate:1,gain:index(s)===2?.23:.14});
  if(c.sfxEnabled)for(const s of c.sfxEvents??[])if(s.from<duration)audioEvents.push({track:'sfx',source:media(s.source),from:s.from,duration:Math.min(s.duration??50,duration-s.from),sourceFrom:0,playbackRate:1,gain:s.gain??.2});
  if(c.subtitleMode!=='off'){
   if(c.subtitlesFollowShots===false)subs.push(...c.subtitles);
   else for(const shot of c.shots){const start=shot.sourceOffset??shot.from,rate=shot.speed??1,end=start+(shot.duration-1)*rate;for(const s of c.subtitles??[]){const lo=Math.max(start,s.from),hi=Math.min(end,s.to);if(hi>=lo)subs.push({text:s.text,from:shot.from+Math.ceil((lo-start)/rate),to:Math.min(shot.from+shot.duration-1,shot.from+Math.floor((hi-start)/rate))});}}
  }
 }
 return {ok:true,schemaVersion:1,composition:'ParkVibeWorkbench',scenePack:'jiading-script07',fps:30,durationInFrames:duration,width:c.format==='landscape'?1920:1080,height:c.format==='landscape'?1080:1920,shots:c.shots,subtitles:subs,audioEvents,audioProfile:'jiading-script07',audioPolicy:'Jiading Xiaoxiao speech retimed per shot; independent voice, music envelope and SFX. Export mix is not the loudness-mastered delivery bitstream.',subtitleStyle:{renderer:'Workbench.Subtitle',fontFamily:'ParkNoto',fontFile:path.join(project,'public/fonts/NotoSansSC.ttf'),fontSize:c.subtitleStyle?.fontSize??44,color:c.subtitleStyle?.color??'#f3f1e9',backgroundEnabled:c.subtitleStyle?.background!==false,x:65,y:1545,width:950,height:108,lineHeight:1.55,textAlign:'center',background:'#090d14de',radius:18},plate:c.plate===true,platePolicy:'Scene artwork retained; omit subtitle and all audio',nativeEditable:['shot cuts','single language subtitle overlay','voice segments and speed','music envelope','sfx'],baked:['scene internal graphics','presenter','camera'],voiceRegenerated:false};
}
module.exports={manifest};
