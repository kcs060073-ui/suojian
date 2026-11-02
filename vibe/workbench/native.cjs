/* Native Canvas seek/render bridge and cross-platform Anything Remotion rendering.
   An ephemeral HTTP server lives only for this command. No shared services. */
const fs=require('node:fs'),path=require('node:path'),http=require('node:http'),crypto=require('node:crypto');
const {spawn}=require('node:child_process');
const c=JSON.parse(fs.readFileSync(process.argv[2],'utf8'));
const sha=b=>crypto.createHash('sha256').update(b).digest('hex');
const check=(condition,message)=>{if(!condition)throw new Error(message);};
async function main(){
 const {openBrowser,selectComposition,renderMedia,renderStill}=require(path.join(c.modules,'@remotion/renderer'));
 const executable=c.browser||path.join(c.modules,'.remotion/chrome-headless-shell/win64/chrome-headless-shell-win64/chrome-headless-shell.exe');
 check(fs.existsSync(executable),'已有浏览器不存在，不会自动下载');
 const browser=await openBrowser('chrome',{browserExecutable:executable,chromeMode:'headless-shell',chromiumOptions:{gl:'angle'}});
 let server;
 const artifacts=[];
 const artifact=(file,kind)=>{const b=fs.readFileSync(file);artifacts.push({path:file,kind,bytes:b.length,sha256:sha(b)});};
 try{
 if(c.tool==='explainer'||c.tool==='inkpress'){
   const {bundle}=require(path.join(c.modules,'@remotion/bundler'));
   const serveUrl=await bundle({entryPoint:path.join(c.project,'src/index.ts'),publicDir:path.join(c.project,'public'),webpackOverride:x=>({...x,resolve:{...x.resolve,modules:[c.modules,...(x.resolve?.modules||['node_modules'])]}})});
   const composition=await selectComposition({serveUrl,id:c.payload.composition||(c.tool==='inkpress'?'AiflPromo':'Video'),browserExecutable:executable,puppeteerInstance:browser});
   const common={serveUrl,composition,browserExecutable:executable,puppeteerInstance:browser};
   const file=path.join(c.output,c.action==='still'?'still.png':c.action+'.mp4');
   if(c.action==='still')await renderStill({...common,frame:Math.min(composition.durationInFrames-1,Number(c.payload.frame||40)),output:file,imageFormat:'png'});
   else{const frameRange=c.action==='preview'?[Math.max(0,Math.round((c.payload.start||0)*composition.fps)),Math.min(composition.durationInFrames-1,Math.round(((c.payload.start||0)+(c.payload.seconds||30))*composition.fps)-1)]:undefined;
     await renderMedia({...common,outputLocation:file,codec:'h264',crf:18,concurrency:2,...(frameRange?{frameRange}:{})});}
   artifact(file,c.action==='still'?'image':'video');
   return {status:'done',artifacts,composition:{width:composition.width,height:composition.height,fps:composition.fps,frames:composition.durationInFrames}};
 }
 const engine=path.join(c.root,'work/native/canvas/engine');
 server=http.createServer((req,res)=>{try{
   const pathname=decodeURIComponent(new URL(req.url,'http://localhost').pathname);
   const base=pathname.startsWith('/engine/')?engine:c.project;
   const rel=pathname.startsWith('/engine/')?pathname.slice(8):pathname==='/'?'index.html':pathname.slice(1);
   const file=path.resolve(base,rel);check(file.startsWith(path.resolve(base)+path.sep),'Invalid path');
   const mime={'.html':'text/html; charset=utf-8','.js':'text/javascript; charset=utf-8','.ttf':'font/ttf','.png':'image/png','.jpg':'image/jpeg'};
   res.setHeader('Content-Type',mime[path.extname(file)]||'application/octet-stream');fs.createReadStream(file).on('error',()=>{res.statusCode=404;res.end();}).pipe(res);
 }catch{res.statusCode=400;res.end();}});
 await new Promise(r=>server.listen(0,'127.0.0.1',r));
 const page=await browser.newPage({context:()=>null,logLevel:'error',indent:false,pageIndex:0,onBrowserLog:null,onLog:()=>{}});
 await page.setViewport({width:1920,height:1080,deviceScaleFactor:1});
 await page.goto({url:'http://127.0.0.1:'+server.address().port+'/',timeout:30000});
 await page.evaluate(async()=>{await document.fonts.ready;for(let i=0;i<300;i++){if(window.READY===true)return;await new Promise(r=>setTimeout(r,100));}throw new Error('Canvas电影未READY，检查film.js');});
 const meta=await page.evaluate(()=>({end:END,fps:FPS,meta:META}));
 const capture=async t=>{await page.evaluate(t=>{seek(t);},t);const shot=await page._client().send('Page.captureScreenshot',{format:'png',captureBeyondViewport:true});return Buffer.from((shot.value||shot).data,'base64');};
 if(c.action==='events'){
   const events=await page.evaluate(()=>events());const file=path.join(c.project,'audio/events.json');fs.mkdirSync(path.dirname(file),{recursive:true});fs.writeFileSync(file,JSON.stringify({...meta,events},null,2));artifact(file,'json');return{status:'done',artifacts,events:events.length};
 }
 if(c.action==='determinism'){
   const times=Array.from({length:12},(_,i)=>(i+.37)*meta.end/12),first={};for(const t of times)first[t]=sha(await capture(t));
   const mismatch=[];for(const t of [...times].reverse())if(sha(await capture(t))!==first[t])mismatch.push(t);
   const file=path.join(c.output,'determinism.json');fs.writeFileSync(file,JSON.stringify({times,hashes:first,mismatch,deterministic:!mismatch.length},null,2));artifact(file,'json');return{status:mismatch.length?'failed':'done',artifacts,deterministic:!mismatch.length};
 }
 if(['stills','sheet','motion'].includes(c.action)){
   const count=Math.min(36,Math.max(1,Number(c.payload.count||12)));
   const times=c.action==='motion'?Array.from({length:count},(_,i)=>Number(c.payload.start||0)+(Number(c.payload.end||Math.min(meta.end,3))-Number(c.payload.start||0))*i/Math.max(1,count-1)):
    Array.isArray(c.payload.times)&&c.payload.times.length?c.payload.times.map(Number):Array.from({length:c.action==='stills'?1:count},(_,i)=>(i+.5)*meta.end/(c.action==='stills'?1:count));
   const images=[];for(let i=0;i<times.length;i++){const b=await capture(times[i]);images.push(b.toString('base64'));if(c.action==='stills'){const file=path.join(c.output,'still-'+i+'.png');fs.writeFileSync(file,b);artifact(file,'image');}}
   if(c.action!=='stills'){
    const data=await page.evaluate(async(images,times)=>{const cols=images.length>6?4:images.length>1?2:1,w=480,h=270;const out=document.createElement('canvas');out.width=cols*w;out.height=Math.ceil(images.length/cols)*h;const ctx=out.getContext('2d');for(let i=0;i<images.length;i++){const im=new Image();im.src='data:image/png;base64,'+images[i];await im.decode();const x=(i%cols)*w,y=Math.floor(i/cols)*h;ctx.drawImage(im,x,y,w,h);ctx.fillStyle='black';ctx.fillRect(x,y+h-26,120,26);ctx.fillStyle='#ffee00';ctx.font='18px monospace';ctx.fillText(times[i].toFixed(2)+'s',x+6,y+h-7);}return out.toDataURL('image/png').split(',')[1];},images,times);
    const file=path.join(c.output,c.action+'.png');fs.writeFileSync(file,Buffer.from(data,'base64'));artifact(file,'image');
   }return{status:'done',artifacts};
 }
 check(['clip','render'].includes(c.action),'未知Canvas动作');
 const start=c.action==='clip'?Math.max(0,Number(c.payload.start||0)):0,end=c.action==='clip'?Math.min(meta.end,Number(c.payload.end||Math.min(meta.end,10))):meta.end;
 check(Number.isFinite(start)&&Number.isFinite(end)&&end>start,'片段时间范围无效');
 const file=path.join(c.output,c.action+'.mp4');
 const ff=spawn(c.ffmpeg,['-v','error','-y','-f','image2pipe','-framerate',String(meta.fps),'-i','-','-c:v','libx264','-pix_fmt','yuv420p','-crf','18','-movflags','+faststart',file],{windowsHide:true,stdio:['pipe','ignore','pipe']});
 let stderr='';ff.stderr.on('data',x=>stderr+=x.toString());
 const completed=new Promise((resolve,reject)=>{ff.on('error',reject);ff.on('exit',code=>code===0?resolve():reject(new Error(stderr||'FFmpeg失败')));});
 try{for(let i=Math.round(start*meta.fps);i<Math.round(end*meta.fps);i++){const b=await capture(i/meta.fps);if(!ff.stdin.write(b))await new Promise(r=>ff.stdin.once('drain',r));}ff.stdin.end();await completed;}catch(e){ff.kill();throw e;}
 artifact(file,'video');return{status:'done',artifacts,seconds:end-start,audio:false,note:'Canvas画面已生成；声音事件与合成配乐可在“声音”动作单独合成。'};
 }finally{await browser.close({silent:true});if(server)await new Promise(r=>server.close(r));}
}
main().then(x=>{fs.writeFileSync(path.join(c.output,'receipt.json'),JSON.stringify(x,null,2));console.log(JSON.stringify(x));}).catch(e=>{console.error(e.stack||e);process.exit(1);});
