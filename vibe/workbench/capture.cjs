/* Screenshot a real HTTP page with the already installed Remotion browser.
   node capture.cjs --url http://127.0.0.1:18991/ --output work/capture.png [--width 1080 --height 1920 --selector main]
   No browser download, dependency installation, shared service start or credentials. */
const fs = require('node:fs');
const path = require('node:path');
const crypto = require('node:crypto');
const args = process.argv.slice(2);
const opt = (k, fallback) => {const i=args.indexOf('--'+k); return i>=0?args[i+1]:fallback;};
async function capture(options) {
  const url = new URL(options.url);
  if (!['http:', 'https:'].includes(url.protocol) || url.username || url.password) throw new Error('URL必须是无嵌入凭据的HTTP(S)地址');
  const modules = options.modules || path.resolve(__dirname,'../engine/node_modules');
  const fallback = 'C:/Users/Tanikawa/Documents/Codex/2026-10-08/ban/motion-park/node_modules';
  const moduleRoot = fs.existsSync(path.join(modules,'@remotion/renderer'))?modules:fallback;
  const {openBrowser}=require(path.join(moduleRoot,'@remotion/renderer'));
  const executable=options.browser || path.join(moduleRoot,'.remotion/chrome-headless-shell/win64/chrome-headless-shell-win64/chrome-headless-shell.exe');
  if(!fs.existsSync(executable)) throw new Error('缺少已有浏览器；未下载新浏览器');
  const width=Number(options.width||1080), height=Number(options.height||1920);
  if(!Number.isInteger(width)||!Number.isInteger(height)||width<100||height<100||width>7680||height>7680) throw new Error('画布尺寸应为100至7680的整数');
  const browser=await openBrowser('chrome',{browserExecutable:executable,chromeMode:'headless-shell',chromiumOptions:{gl:'angle'}});
  try {
    const page=await browser.newPage({context:()=>null,logLevel:'error',indent:false,pageIndex:0,onBrowserLog:null,onLog:()=>{}});
    await page.setViewport({width,height,deviceScaleFactor:1});
    const response=await page.goto({url:url.href,timeout:30000});
    if(response && response.status()>=400) throw new Error('网页返回HTTP '+response.status());
    if(options.waitSelector) await page.evaluate(async(selector)=>{
      await new Promise((resolve,reject)=>{const deadline=Date.now()+15000;const check=()=>{if(document.querySelector(selector))return resolve();if(Date.now()>deadline)return reject(new Error('动态页面等待超时'));requestAnimationFrame(check);};check();});
    },options.waitSelector);
    await page.evaluate(async()=>{await document.fonts.ready; await Promise.all([...document.images].map(i=>i.complete?Promise.resolve():new Promise(r=>{i.onload=i.onerror=r;})));});
    if(options.seek!==undefined) await page.evaluate((t)=>{if(typeof window.seek==='function')window.seek(t);},Number(options.seek));
    const layout=await page.evaluate((selector)=>{
      const target=selector?document.querySelector(selector):document.documentElement;
      if(!target)throw new Error('截图选择器没有匹配元素');
      const r=target.getBoundingClientRect();
      const s=getComputedStyle(target);
      return {x:Math.max(0,r.x+scrollX),y:Math.max(0,r.y+scrollY),width:Math.ceil(r.width),height:Math.ceil(r.height),title:document.title,
              fontFamily:s.fontFamily,fontSize:s.fontSize,color:s.color,background:s.backgroundColor,url:location.href};
    },options.selector||null);
    const client=page._client();
    const clip=options.selector?{x:layout.x,y:layout.y,width:Math.min(7680,layout.width),height:Math.min(16384,layout.height),scale:1}:null;
    const shot=await client.send('Page.captureScreenshot',{format:'png',captureBeyondViewport:!!clip,...(clip?{clip}:{})});
    const data=Buffer.from((shot.value||shot).data,'base64');
    const output=path.resolve(options.output);
    fs.mkdirSync(path.dirname(output),{recursive:true});fs.writeFileSync(output,data);
    const receipt={url:url.href,output,width:clip?clip.width:width,height:clip?clip.height:height,sha256:crypto.createHash('sha256').update(data).digest('hex'),bytes:data.length,layout};
    fs.writeFileSync(output+'.json',JSON.stringify(receipt,null,2));return receipt;
  } finally {await browser.close({silent:true});}
}
module.exports={capture};
if(require.main===module)capture({url:opt('url'),output:opt('output'),width:opt('width',1080),height:opt('height',1920),selector:opt('selector'),waitSelector:opt('wait-selector'),seek:opt('seek'),modules:opt('modules'),browser:opt('browser')}).then(r=>console.log(JSON.stringify(r))).catch(e=>{console.error(e.message);process.exit(1);});
