/* Exact upstream Remotion shotcard adapter. No dependency install and no generic fallback. */
const fs=require('node:fs'),path=require('node:path'),crypto=require('node:crypto');
async function main(){
 const config=JSON.parse(fs.readFileSync(process.argv[2],'utf8'));
 const {bundle}=require(path.join(config.modules,'@remotion/bundler'));
 const {selectComposition,renderMedia,renderStill,openBrowser}=require(path.join(config.modules,'@remotion/renderer'));
 const card=config.card, norm=s=>s.toLowerCase().replace(/[^a-z0-9]/g,'');
 const sourcePaths=card.demo.map(uri=>path.join(config.vendor,decodeURIComponent(uri.replace(/^\/vendor\//,''))));
 const candidates=[];
 for(const file of sourcePaths){const text=fs.readFileSync(file,'utf8');for(const m of text.matchAll(/export const (\w+)\s*:\s*React\.FC(?:<[^>]*>)?\s*=/g))candidates.push({file,name:m[1],text});}
 if(!candidates.length)throw new Error('准确demo中未找到无必填props的React.FC组件；需要专用适配');
 const selected=candidates.find(c=>norm(c.name)===norm(config.style))||candidates.find(c=>norm(c.name)===norm(card.id))||(candidates.length===1?candidates[0]:null);
 if(!selected)throw new Error('此卡有多个原生组件，请选择准确变体：'+candidates.map(c=>c.name).join(', '));
 const folder=path.join(config.root,'work/demo',config.style),pub=path.join(folder,'public');fs.mkdirSync(pub,{recursive:true});
 const textures=path.join(config.vendor,'video-shotcraft-main/demos/_textures');
 for(const dir of ['textures/live','textures'])if(fs.existsSync(textures))fs.cpSync(textures,path.join(pub,dir),{recursive:true});
 const durationExport=selected.text.match(/export const (\w+_DURATION|\w+_DUR)\s*=/)?.[1];
 const parsed=card.duration.match(/(\d{2,4})\s*f\b/)?.[1];
 const duration=parsed?Number(parsed):150;
 const filename=path.join(folder,'index.tsx');
 fs.writeFileSync(filename,`import React from 'react';\nimport {registerRoot,Composition} from 'remotion';\nimport {${selected.name}${durationExport?', '+durationExport:''}} from ${JSON.stringify(selected.file.replace(/\\/g,'/'))};\nconst Root=()=> <Composition id="Shotcard" component={${selected.name}} durationInFrames={${durationExport||duration}} fps={30} width={1920} height={1080}/>;\nregisterRoot(Root);\n`);
 const sourceRoot=path.join(config.modules), serveUrl=await bundle({entryPoint:filename,publicDir:pub,webpackOverride:c=>({...c,resolve:{...c.resolve,modules:[sourceRoot,...(c.resolve?.modules||['node_modules'])]}})});
 const browserExecutable=config.browser||path.join(config.modules,'.remotion/chrome-headless-shell/win64/chrome-headless-shell-win64/chrome-headless-shell.exe');
 if(!fs.existsSync(browserExecutable))throw new Error('已有Remotion浏览器不存在；没有触发下载');
 const browser=await openBrowser('chrome',{browserExecutable,chromeMode:'headless-shell',chromiumOptions:{gl:'angle'}});
 try{
  const composition=await selectComposition({serveUrl,id:'Shotcard',browserExecutable,puppeteerInstance:browser});
  fs.mkdirSync(path.dirname(config.output),{recursive:true});
  const common={composition,serveUrl,browserExecutable,puppeteerInstance:browser};
  if(config.mode==='still')await renderStill({...common,frame:Math.floor(composition.durationInFrames*.55),output:config.output,imageFormat:'png'});
  else await renderMedia({...common,codec:'h264',outputLocation:config.output,concurrency:2,crf:20});
  const data=fs.readFileSync(config.output),receipt={id:card.id,style:config.style,mode:config.mode,output:config.output,source:selected.file,component:selected.name,width:composition.width,height:composition.height,fps:composition.fps,frames:composition.durationInFrames,bytes:data.length,sha256:crypto.createHash('sha256').update(data).digest('hex'),nativeAnimation:false,note:'准确上游镜头代码渲染为媒体；加入剪映后镜头内部动画已烘焙。'};
  fs.writeFileSync(config.output+'.json',JSON.stringify(receipt,null,2));console.log(JSON.stringify(receipt));
 }finally{await browser.close({silent:true});}
}
main().catch(e=>{console.error(e.stack||e);process.exit(1);});
