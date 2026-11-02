import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import {
  existsSync,
  mkdtempSync,
  readFileSync,
  rmSync as rawRmSync,
  writeFileSync,
} from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import test from "node:test";
import { pathToFileURL } from "node:url";

test("创建页和任务页提供清晰的 Agent 规划模式标识", () => {
  const html = readFileSync(
    new URL("../web-ui/public/index.html", import.meta.url),
    "utf8",
  );
  assert.match(html, /id="orchestration-mode"/);
  assert.match(html, />CUTROOM 自己规划</);
  assert.match(html, />当前 Agent 规划</);
  assert.match(html, /data-planning-profile/);
  assert.match(html, /id="task-orchestration-mode"/);
});

const BROWSER = [
  "C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe",
  "C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe",
].find(existsSync);

const rmSync = (path, options = {}) => {
  try {
    rawRmSync(path, {
      maxRetries: 10,
      retryDelay: 200,
      ...options,
    });
  } catch (error) {
    // Chromium can keep its isolated profile open briefly after --dump-dom exits.
    // A stale test-only temp directory must not mask completed UI assertions.
    if (error?.code !== "EPERM") throw error;
  }
};

test("粗剪真实画面、时间线和候选卡执行预览后确认替换", { skip: !BROWSER }, () => {
  const temp = mkdtempSync(join(tmpdir(), "cutroom-rough-media-"));
  try {
    const app = readFileSync(new URL("../web-ui/public/app.mjs", import.meta.url), "utf8");
    const block = (start, end) => app.slice(app.indexOf(start), app.indexOf(end, app.indexOf(start)));
    const sceneModel = readFileSync(
      new URL("../web-ui/public/scene-editor.mjs", import.meta.url),
      "utf8",
    ).replace(/^import .*$/gm, "").replaceAll("export ", "");
    const previewModel = readFileSync(
      new URL("../web-ui/public/rough-preview.mjs", import.meta.url),
      "utf8",
    ).replaceAll("export ", "");
    const source = [
      "const roughCutDescription = () => '';",
      block("function element(", "function statusBadge("),
      block("function button(", "function renderAiDisclosure("),
      block("function roughMediaError(", "function renderPlanInspector("),
      previewModel,
      sceneModel,
    ].join("\n");
    const fixture = join(temp, "rough-media.html");
    writeFileSync(fixture, `<!doctype html><meta charset="utf-8">
      <main id="cutroom-workbench">
        <section id="primary"></section><aside id="inspector"></aside>
        <div id="impact-modal" hidden><div id="impact-details"></div></div>
      </main><pre id="probe"></pre><script>
      ${source}
      addEventListener('error',event=>{document.querySelector('#probe').textContent=JSON.stringify({browserError:String(event.error||event.message)});});
      addEventListener('unhandledrejection',event=>{document.querySelector('#probe').textContent=JSON.stringify({browserError:String(event.reason)});});
      const root=document.querySelector('#cutroom-workbench');
      const state={selectedTaskId:'task-a',selectedSceneId:null,pendingImpact:null,workspaceOverride:null,playhead:0,previewCandidateId:null};
      const apiCalls=[];
      const api=async(path,options)=>{apiCalls.push({path,body:JSON.parse(options.body)});return {affected_card_ids:['scene-image'],invalidated_stages:[],locked:false};};
      const jsonRequest=(method,body)=>({method,body:JSON.stringify(body)});
      const workflowLabel=value=>value;
      const showToast=()=>{};
      const showActionError=error=>{throw error;};
      const taskPost=async()=>{};
      const renderTaskWorkspace=()=>{};
      const renderAiDisclosure=()=>document.createElement('aside');
      const detail={sceneCards:[
        {card_id:'scene-image',idx:0,narrative_text:'园区全景',narrative_start:0,narrative_end:3,asset_id:'image-a',mix_form:'broll全屏',relative_path:'本次/园区.jpg',media_kind:'image',preview_available:true,preview_url:'/api/tasks/task-a/rough-media/image-a/preview',thumbnail_url:'/api/tasks/task-a/rough-media/image-a/thumbnail'},
        {card_id:'scene-video',idx:1,narrative_text:'生产线',narrative_start:3,narrative_end:7,asset_id:'video-a',mix_form:'broll全屏',relative_path:'本次/生产线.mp4',media_kind:'video',preview_available:true,preview_url:'/api/tasks/task-a/rough-media/video-a/preview',thumbnail_url:'/api/tasks/task-a/rough-media/video-a/thumbnail'},
      ],eligibleAssets:[
        {asset_id:'candidate-a',media_kind:'image',relative_path:'候选/园区全景.jpg',source_mode:'primary',preview_available:true,preview_url:'/api/tasks/task-a/rough-media/candidate-a/preview',thumbnail_url:'/api/tasks/task-a/rough-media/candidate-a/thumbnail'},
        {asset_id:'candidate-b',media_kind:'video',relative_path:'候选/园区航拍.mp4',source_mode:'shared',preview_available:true,preview_url:'/api/tasks/task-a/rough-media/candidate-b/preview',thumbnail_url:'/api/tasks/task-a/rough-media/candidate-b/thumbnail'},
      ]};
      renderRoughCut(detail,document.querySelector('#primary'),document.querySelector('#inspector'),{readOnly:false});
      const stageImageSrc=document.querySelector('.video-stage .rough-media')?.getAttribute('src')||null;
      const timelineThumbnailCount=document.querySelectorAll('.scene-timeline .rough-thumbnail').length;
      const candidateThumbnailCount=document.querySelectorAll('.candidate-grid .rough-thumbnail').length;
      const selectCount=document.querySelectorAll('.replace-panel select').length;
      const trimControlCount=document.querySelectorAll('.trim-controls').length;
      document.querySelector('.candidate-card')?.click();
      const callsAfterPreview=apiCalls.length;
      const activeCandidateLabel=document.querySelector('.candidate-card[aria-pressed="true"]')?.textContent.trim()||null;
      document.querySelector('.replace-panel .btn.primary')?.click();
      setTimeout(()=>{
        document.querySelector('#probe').textContent=JSON.stringify({
          stageImageSrc,timelineThumbnailCount,candidateThumbnailCount,selectCount,
          trimControlCount,
          callsAfterPreview,replacementCalls:apiCalls.length,activeCandidateLabel,
          replacementEdit:apiCalls[0]?.body?.edit||null,
        });
      },0);
    </script>`, "utf8");
    const result = spawnSync(BROWSER, [
      "--headless=new", "--disable-gpu", "--no-first-run", "--allow-file-access-from-files",
      `--user-data-dir=${join(temp, "profile")}`, "--dump-dom", pathToFileURL(fixture).href,
    ], { encoding: "utf8", timeout: 30_000 });
    assert.equal(result.status, 0, result.stderr);
    const match = result.stdout.match(/<pre id="probe">([^<]+)<\/pre>/);
    assert.ok(match, result.stderr + result.stdout.slice(-2000));
    const actual = JSON.parse(match[1].replaceAll("&quot;", '"'));
    assert.equal(actual.stageImageSrc, "/api/tasks/task-a/rough-media/image-a/preview");
    assert.equal(actual.timelineThumbnailCount, 2);
    assert.equal(actual.candidateThumbnailCount, 2);
    assert.equal(actual.selectCount, 0);
    assert.equal(actual.trimControlCount, 0);
    assert.equal(actual.callsAfterPreview, 0);
    assert.equal(actual.replacementCalls, 1);
    assert.equal(actual.activeCandidateLabel, "候选/园区全景.jpg本次主素材");
    assert.deepEqual(actual.replacementEdit, {
      action: "replace", card_id: "scene-image", asset_id: "candidate-a",
      segment_id: null, mix_form: "broll全屏",
    });
  } finally {
    rmSync(temp, { recursive: true, force: true });
  }
});

test("粗剪排序裁剪真实播放头拆分与离线锁定门禁使用精确载荷", { skip: !BROWSER }, () => {
  const temp = mkdtempSync(join(tmpdir(), "cutroom-rough-controls-"));
  try {
    const app = readFileSync(new URL("../web-ui/public/app.mjs", import.meta.url), "utf8");
    const block = (start, end) => app.slice(app.indexOf(start), app.indexOf(end, app.indexOf(start)));
    const sceneModel = readFileSync(new URL("../web-ui/public/scene-editor.mjs", import.meta.url), "utf8")
      .replace(/^import .*$/gm, "").replaceAll("export ", "");
    const previewModel = readFileSync(new URL("../web-ui/public/rough-preview.mjs", import.meta.url), "utf8")
      .replaceAll("export ", "");
    const source = [
      "const roughCutDescription = () => '';",
      block("function element(", "function statusBadge("),
      block("function button(", "function renderAiDisclosure("),
      block("function roughMediaError(", "function renderPlanInspector("),
      previewModel,
      sceneModel,
    ].join("\n");
    const fixture = join(temp, "rough-controls.html");
    writeFileSync(fixture, `<!doctype html><meta charset="utf-8"><main id="cutroom-workbench">
      <section id="primary"></section><aside id="inspector"></aside>
      <div id="impact-modal" hidden><div id="impact-details"></div></div>
      </main><pre id="probe"></pre><script>
      ${source}
      const root=document.querySelector('#cutroom-workbench');
      const state={selectedTaskId:'task-a',selectedSceneId:'c',pendingImpact:null,workspaceOverride:null,roughPlayhead:8.5,roughPlaying:false,roughAnimationFrame:null,previewCandidateId:null};
      addEventListener('error',event=>{document.querySelector('#probe').textContent=JSON.stringify({browserError:String(event.error||event.message)});});
      addEventListener('unhandledrejection',event=>{document.querySelector('#probe').textContent=JSON.stringify({browserError:String(event.reason)});});
      const edits=[];
      const api=async(path,options)=>{const body=JSON.parse(options.body);edits.push(body.edit);return {affected_card_ids:[body.edit.card_id],invalidated_stages:[],locked:false};};
      const jsonRequest=(method,body)=>({method,body:JSON.stringify(body)});
      const workflowLabel=value=>value; const showToast=()=>{}; const showActionError=error=>{throw error;};
      const taskPost=async()=>{}; const renderTaskWorkspace=()=>{}; const renderAiDisclosure=()=>document.createElement('aside');
      const cards=[
        {card_id:'a',idx:0,narrative_text:'A',narrative_start:0,narrative_end:4,enter_tc:10,exit_tc:16,asset_id:'a-img',segment_id:'seg-a',mix_form:'信息卡',relative_path:'a.jpg',media_kind:'image',preview_available:true,preview_url:'/a',thumbnail_url:'/at'},
        {card_id:'c',idx:1,narrative_text:'C',narrative_start:4,narrative_end:8,enter_tc:20,exit_tc:26,asset_id:'c-video',mix_form:'broll全屏',relative_path:'c.mp4',media_kind:'video',preview_available:false,preview_problem:'素材离线'},
        {card_id:'b',idx:2,narrative_text:'B',narrative_start:8,narrative_end:12,asset_id:'b-img',mix_form:'信息卡',relative_path:'b.jpg',media_kind:'image',preview_available:true,preview_url:'/b',thumbnail_url:'/bt'},
      ];
      const detail={sceneCards:cards,eligibleAssets:[]};
      const primary=document.querySelector('#primary'); const inspector=document.querySelector('#inspector');
      const draw=()=>{primary.replaceChildren();inspector.replaceChildren();renderRoughCut(detail,primary,inspector,{readOnly:false});};
      {
        draw();
        const transfer={value:'',effectAllowed:'',setData(type,value){this.value=value;},getData(){return this.value;}};
        const dragStart=new Event('dragstart',{bubbles:true});Object.defineProperty(dragStart,'dataTransfer',{value:transfer});
        const drop=new Event('drop',{bubbles:true});Object.defineProperty(drop,'dataTransfer',{value:transfer});
        document.querySelector('[data-scene-id="c"]').dispatchEvent(dragStart);
        document.querySelector('[data-scene-id="a"]').dispatchEvent(drop);
        const dragReorderEdit=edits.find(item=>item?.action==='reorder')||null;
        edits.length=0;
        document.querySelector('[data-move-direction="left"]')?.click();
        const keyboardReorderEdit=edits.find(item=>item?.action==='reorder')||null;
        edits.length=0;
        state.selectedSceneId='a'; state.roughPlayhead=2.75; draw();
        const inputs=document.querySelectorAll('.trim-controls input');
        if(inputs[0]) inputs[0].value='11'; if(inputs[1]) inputs[1].value='15';
        document.querySelector('[data-edit-action="trim"]')?.click();
        document.querySelector('[data-edit-action="split"]')?.click();
        const trimEdit=edits.find(item=>item?.action==='trim')||null;
        const splitEdit=edits.find(item=>item?.action==='split')||null;
        const lock=document.querySelector('[data-picture-lock]');
        state.selectedSceneId='voice';
        const voice={card_id:'voice',idx:0,narrative_text:'口播',narrative_start:0,narrative_end:4,asset_id:'voice-video',mix_form:'数字人全屏',fixed_anchor:true,media_kind:'video',preview_available:true,preview_url:'/voice',thumbnail_url:'/voice-thumb'};
        detail.sceneCards=[voice]; draw();
        document.querySelector('#probe').textContent=JSON.stringify({
          dragReorderEdit,keyboardReorderEdit,trimEdit,splitEdit,
          lockDisabled:lock?.disabled??null,lockReason:lock?.title||'',
          voiceDestructiveControls:document.querySelectorAll('[data-edit-action="delete"],[data-edit-action="split"],[data-move-direction]').length,
        });
      }
      </script>`, "utf8");
    const result = spawnSync(BROWSER, [
      "--headless=new", "--disable-gpu", "--no-first-run", "--allow-file-access-from-files",
      `--user-data-dir=${join(temp, "profile")}`, "--dump-dom", pathToFileURL(fixture).href,
    ], { encoding: "utf8", timeout: 30_000 });
    assert.equal(result.status, 0, result.stderr);
    const match = result.stdout.match(/<pre id="probe">([^<]+)<\/pre>/);
    assert.ok(match, result.stderr + result.stdout.slice(-2000));
    const actual = JSON.parse(match[1].replaceAll("&quot;", '"'));
    assert.equal(actual.browserError, undefined, actual.browserError || JSON.stringify(actual));
    assert.deepEqual(actual.dragReorderEdit, { action: "reorder", card_ids: ["c", "a", "b"] });
    assert.deepEqual(actual.keyboardReorderEdit, actual.dragReorderEdit);
    assert.deepEqual(actual.trimEdit, { action: "trim", card_id: "a", enter_tc: 11, exit_tc: 15 });
    assert.deepEqual(actual.splitEdit, { action: "split", card_id: "a", playhead: 2.75 });
    assert.equal(actual.lockDisabled, true);
    assert.match(actual.lockReason, /第 2 镜头.*素材离线/);
    assert.equal(actual.voiceDestructiveControls, 0);
  } finally {
    rmSync(temp, { recursive: true, force: true });
  }
});

test("粗剪跨镜头重绑编辑控件并在媒体失败时停止锁定", { skip: !BROWSER }, () => {
  const temp = mkdtempSync(join(tmpdir(), "cutroom-rough-runtime-"));
  try {
    const app = readFileSync(new URL("../web-ui/public/app.mjs", import.meta.url), "utf8");
    const block = (start, end) => app.slice(app.indexOf(start), app.indexOf(end, app.indexOf(start)));
    const sceneModel = readFileSync(new URL("../web-ui/public/scene-editor.mjs", import.meta.url), "utf8")
      .replace(/^import .*$/gm, "").replaceAll("export ", "");
    const previewModel = readFileSync(new URL("../web-ui/public/rough-preview.mjs", import.meta.url), "utf8")
      .replaceAll("export ", "");
    const source = [
      "const roughCutDescription = () => '';",
      block("function element(", "function statusBadge("),
      block("function button(", "function renderAiDisclosure("),
      block("function roughMediaError(", "function renderPlanInspector("),
      previewModel,
      sceneModel,
    ].join("\n");
    const fixture = join(temp, "rough-runtime.html");
    const pixel = "data:image/gif;base64,R0lGODlhAQABAIAAAAAAAP///ywAAAAAAQABAAACAUwAOw==";
    writeFileSync(fixture, `<!doctype html><meta charset="utf-8"><main id="cutroom-workbench">
      <section id="primary"></section><aside id="inspector"></aside>
      <div id="impact-modal" hidden><div id="impact-details"></div></div>
      </main><pre id="probe"></pre><script>
      ${source}
      const root=document.querySelector('#cutroom-workbench');
      const state={selectedTaskId:'task-a',selectedSceneId:'a',pendingImpact:null,workspaceOverride:null,roughPlayhead:0.5,roughPlaying:false,roughClockAt:0,roughAnimationFrame:null,roughMediaFailures:new Set(),previewCandidateId:null};
      addEventListener('error',event=>{document.querySelector('#probe').textContent=JSON.stringify({browserError:String(event.error||event.message)});});
      addEventListener('unhandledrejection',event=>{document.querySelector('#probe').textContent=JSON.stringify({browserError:String(event.reason)});});
      const edits=[]; const posts=[];
      const api=async(path,options)=>{const body=JSON.parse(options.body);edits.push(body.edit);return {affected_card_ids:[body.edit.card_id],invalidated_stages:[],locked:false};};
      const jsonRequest=(method,body)=>({method,body:JSON.stringify(body)});
      const workflowLabel=value=>value; const showToast=()=>{}; const showActionError=error=>{throw error;};
      const taskPost=async(path,body)=>{posts.push({path,body});}; const renderTaskWorkspace=()=>{}; const renderAiDisclosure=()=>document.createElement('aside');
      const cards=[
        {card_id:'a',idx:0,narrative_text:'A',narrative_start:0,narrative_end:3,asset_id:'shared',mix_form:'broll全屏',relative_path:'shared.jpg',media_kind:'image',preview_available:true,preview_version:'v-a',preview_url:'${pixel}',thumbnail_url:'${pixel}'},
        {card_id:'b',idx:1,narrative_text:'B',narrative_start:3,narrative_end:6,asset_id:'shared',mix_form:'broll全屏',relative_path:'shared.jpg',media_kind:'image',preview_available:true,preview_version:'v-b',preview_url:'${pixel}',thumbnail_url:'${pixel}'},
      ];
      const detail={sceneCards:cards,eligibleAssets:[{asset_id:'candidate-video',media_kind:'video',relative_path:'candidate.mp4',source_mode:'primary',preview_available:true,preview_version:'candidate-v',preview_url:'data:video/mp4;base64,AAAA',thumbnail_url:'data:video/mp4;base64,AAAA'}]};
      const primary=document.querySelector('#primary'); const inspector=document.querySelector('#inspector');
      renderRoughCut(detail,primary,inspector,{readOnly:false});
      const scrubber=document.querySelector('[aria-label="粗剪全局播放头"]');
      scrubber.value='3.5'; scrubber.dispatchEvent(new Event('input',{bubbles:true}));
      const captionAfterSwitch=document.querySelector('.preview-caption')?.textContent;
      document.querySelector('[data-edit-action="delete"]')?.click();
      document.querySelector('[data-picture-lock]')?.click();
      document.querySelector('[data-asset-id="candidate-video"]')?.click();
      const candidateControls=document.querySelector('.video-stage video')?.controls??false;
      scrubber.value='3.5'; scrubber.dispatchEvent(new Event('input',{bubbles:true}));
      document.querySelector('.rough-play-toggle')?.click();
      document.querySelector('.video-stage .rough-media')?.dispatchEvent(new Event('error'));
      setTimeout(()=>{
        const lock=document.querySelector('[data-picture-lock]');
        document.querySelector('#probe').textContent=JSON.stringify({
          captionAfterSwitch,
          deleteCardId:edits.find(item=>item?.action==='delete')?.card_id||null,
          lockBody:posts.find(item=>item.path==='/picture/lock')?.body||null,
          candidateControls,
          playingAfterError:state.roughPlaying,
          lockDisabledAfterError:lock?.disabled??null,
          lockReasonAfterError:lock?.title||'',
        });
      },0);
      </script>`, "utf8");
    const result = spawnSync(BROWSER, [
      "--headless=new", "--disable-gpu", "--no-first-run", "--allow-file-access-from-files",
      `--user-data-dir=${join(temp, "profile")}`, "--dump-dom", pathToFileURL(fixture).href,
    ], { encoding: "utf8", timeout: 30_000 });
    assert.equal(result.status, 0, result.stderr);
    const match = result.stdout.match(/<pre id="probe">([^<]+)<\/pre>/);
    assert.ok(match, result.stderr + result.stdout.slice(-2000));
    const actual = JSON.parse(match[1].replaceAll("&quot;", '"'));
    assert.equal(actual.browserError, undefined, actual.browserError || JSON.stringify(actual));
    assert.equal(actual.captionAfterSwitch, "B");
    assert.equal(actual.deleteCardId, "b");
    assert.deepEqual(actual.lockBody, { mediaVersions: { a: "v-a", b: "v-b" } });
    assert.equal(actual.candidateControls, true);
    assert.equal(actual.playingAfterError, false);
    assert.equal(actual.lockDisabledAfterError, true);
    assert.match(actual.lockReasonAfterError, /第 2 镜头.*浏览器无法加载/);
  } finally {
    rmSync(temp, { recursive: true, force: true });
  }
});

test("真实目录引用可提交且发布包七文件正常显示", { skip: !BROWSER }, () => {
  const temp = mkdtempSync(join(tmpdir(), "cutroom-packaging-contract-"));
  try {
    const app = readFileSync(new URL("../web-ui/public/app.mjs", import.meta.url), "utf8");
    const block = (start, end) => app.slice(app.indexOf(start), app.indexOf(end, app.indexOf(start)));
    const source = [block("function element(", "function statusBadge("),
      block("function button(", "function renderAiDisclosure("),
      block("function renderPlanInspector(", "function renderPackaging("),
      block("function displayCandidateLabel(", "function renderReview("),
      block("function renderPublish(", "function renderRecovery("),
      readFileSync(new URL("../web-ui/public/packaging-preview.mjs", import.meta.url), "utf8").replaceAll("export ", ""),
      readFileSync(new URL("../web-ui/public/publish-controls.mjs", import.meta.url), "utf8").replaceAll("export ", ""),
      readFileSync(new URL("../web-ui/public/review-closure.mjs", import.meta.url), "utf8").replaceAll("export ", "")].join("\n");
    const python = join(process.cwd(), ".venv", "Scripts", "python.exe");
    const catalog = spawnSync(python, ["-c", "import json; from workbench.api import _packaging_catalog; from pathlib import Path; print(json.dumps(_packaging_catalog(project_root=Path.cwd())))"], {encoding:"utf8"});
    assert.equal(catalog.status, 0, catalog.stderr);
    const expected = JSON.parse(catalog.stdout).find(item => item.key === "subtitle").assets.find(item => item.authorizationStatus === "approved").assetRef;
    const fixture = join(temp, "contract.html");
    writeFileSync(fixture, `<!doctype html><meta charset="utf-8"><div id="inspector"></div><div id="publish"></div><div id="side"></div><pre id="probe"></pre><script>
      ${source}
      const state={
        selectedTaskId:'task',selectedPlanId:'p',taskDetail:null,
        bootstrap:{packagingCatalog:${catalog.stdout}},
        packagingPreviews:{'task:p':{status:'not_requested'}},
        loadingPackagingPreviews:new Set(),packagingViewed:new Set(),packagingPreviewPolls:new Map(),
      }; let sent;
      const packagingPreviewKey=(taskId,planId)=>taskId+':'+planId;
      const taskPost=async(path,body)=>{sent={path,body};};
      const showToast=()=>{}; const showActionError=e=>{throw e;}; const gateSummary=()=>'';
      const renderAiDisclosure=()=>document.createElement('aside');
      renderPlanInspector({task:{task_id:'task',task_type:'digital_human'}},document.querySelector('#inspector'),{plan_id:'p',plan_name:'我的自选组合',items:[]});
      document.querySelector('#inspector .replace-panel button').click();
      renderPublish({workflow:{gates:{}},publishPackages:[{record_id:'pub-1',package_dir:'fixture/output',package_complete:true,files:['video-final.mp4','cover-01.jpg','cover-02.jpg','cover-03.jpg','publish-copy.md','publish-check.json','qa-report.md']}]},document.querySelector('#publish'),document.querySelector('#side'),{readOnly:true});
      document.querySelector('#probe').textContent=JSON.stringify({sent,header:document.querySelector('.package-card-head').textContent,files:[...document.querySelector('.package-files').children].map(n=>n.textContent)});
    </script>`, "utf8");
    const result=spawnSync(BROWSER,["--headless=new","--disable-gpu","--no-first-run",`--user-data-dir=${join(temp,"profile")}`,"--dump-dom",pathToFileURL(fixture).href],{encoding:"utf8",timeout:30000});
    assert.equal(result.status,0,result.stderr);
    const match=result.stdout.match(/<pre id="probe">([^<]+)<\/pre>/);
    assert.ok(match,result.stdout.slice(-2000));
    const actual=JSON.parse(match[1].replaceAll("&quot;",'"'));
    assert.equal(actual.sent.body.assetRef,expected);
    assert.match(actual.header,/发布包 1/);
    assert.doesNotMatch(actual.header,/\[object /);
    assert.deepEqual(actual.files,['video-final.mp4','cover-01.jpg','cover-02.jpg','cover-03.jpg','publish-copy.md','publish-check.json','qa-report.md']);
  } finally { rmSync(temp,{recursive:true,force:true}); }
});

test("真实包装预览在窄屏可达且观看后开放确认", { skip: !BROWSER }, () => {
  const temp = mkdtempSync(join(tmpdir(), "cutroom-packaging-preview-"));
  try {
    const app = readFileSync(new URL("../web-ui/public/app.mjs", import.meta.url), "utf8");
    const styles = readFileSync(new URL("../web-ui/public/styles.css", import.meta.url), "utf8");
    const block = (start, end) => app.slice(app.indexOf(start), app.indexOf(end, app.indexOf(start)));
    const source = [
      block("function element(", "function statusBadge("),
      block("function inlineFeedback(", "function setButtonBusy("),
      block("function button(", "function renderAiDisclosure("),
      block("function renderPlanInspector(", "function renderPackaging("),
      readFileSync(new URL("../web-ui/public/packaging-preview.mjs", import.meta.url), "utf8").replaceAll("export ", ""),
    ].join("\n");
    const fixture = join(temp, "preview.html");
    writeFileSync(fixture, `<!doctype html><meta charset="utf-8"><style>${styles}body{margin:0}#inspector{width:390px;max-width:100%;padding:12px;box-sizing:border-box}</style>
      <div id="inspector"></div><pre id="probe"></pre><script>
      ${source}
      const task={task:{task_id:'task',task_type:'digital_human'}};
      const plan={plan_id:'plan',plan_name:'我的自选组合',items:[
        {slot:'subtitle',asset_ref:'sub'}, {slot:'sfx',asset_ref:'hit'}
      ]};
      const ready={previewId:'pv-1',status:'ready',planDigest:'plan-digest',pictureLockDigest:'lock-digest',viewedAt:null,mediaUrl:'/api/tasks/task/packaging-previews/pv-1/media'};
      const state={selectedTaskId:'task',selectedPlanId:'plan',taskDetail:task,bootstrap:{packagingCatalog:[
        {key:'subtitle',assets:[{assetRef:'sub',name:'字幕',authorizationStatus:'approved'}]},
        {key:'sfx',assets:[{assetRef:'hit',name:'提示音',authorizationStatus:'approved',previewKind:'audio',previewUrl:'data:audio/wav;base64,UklGRg=='}]},
      ]},packagingPreviews:{'task:plan':ready},loadingPackagingPreviews:new Set(),packagingViewed:new Set(),packagingPreviewPolls:new Map()};
      const packagingPreviewKey=(taskId,planId)=>taskId+':'+planId;
      const showToast=()=>{}; const showActionError=e=>{throw e;}; const taskPost=async()=>{};
      const api=async(path)=>{window.lastPath=path;return {...ready,viewedAt:'now'};};
      const jsonRequest=(method,body)=>({method,body:JSON.stringify(body)});
      const rerenderSelectedPackaging=()=>renderPlanInspector(task,document.querySelector('#inspector'),plan);
      renderPlanInspector(task,document.querySelector('#inspector'),plan);
      const beforeDisabled=document.querySelector('[data-action="confirm-packaging"]').disabled;
      const slot=document.querySelector('.replace-panel select'); slot.value='sfx'; slot.dispatchEvent(new Event('change'));
      const disableReachable=!document.querySelector('[data-action="disable-packaging-slot"]').hidden;
      const video=document.querySelector('[data-packaging-preview]');
      Object.defineProperty(video,'duration',{value:10}); video.currentTime=9; video.dispatchEvent(new Event('timeupdate'));
      setTimeout(()=>{const currentVideo=document.querySelector('[data-packaging-preview]');document.querySelector('#probe').textContent=JSON.stringify({
        beforeDisabled,afterDisabled:document.querySelector('[data-action="confirm-packaging"]').disabled,
        disableReachable,saveReachable:Boolean(document.querySelector('[data-action="save-packaging-template"]')),
        slotPreview:document.querySelector('[data-packaging-slot-preview="sfx"]')?.tagName,
        path:window.lastPath,aspect:getComputedStyle(currentVideo).aspectRatio,
        overflow:document.documentElement.scrollWidth>document.documentElement.clientWidth,
      });},0);
      </script>`, "utf8");
    const result = spawnSync(BROWSER, [
      "--headless=new", "--disable-gpu", "--no-first-run", "--window-size=390,844",
      `--user-data-dir=${join(temp, "profile")}`, "--dump-dom", pathToFileURL(fixture).href,
    ], { encoding: "utf8", timeout: 30_000 });
    assert.equal(result.status, 0, result.stderr);
    const match = result.stdout.match(/<pre id="probe">([^<]+)<\/pre>/);
    assert.ok(match, result.stdout.slice(-2000));
    const actual = JSON.parse(match[1].replaceAll("&quot;", '"'));
    assert.equal(actual.beforeDisabled, true);
    assert.equal(actual.afterDisabled, false);
    assert.equal(actual.disableReachable, true);
    assert.equal(actual.saveReachable, true);
    assert.equal(actual.slotPreview, "AUDIO");
    assert.equal(actual.path, "/api/tasks/task/packaging-previews/pv-1/viewed");
    assert.equal(actual.aspect, "9 / 16");
    assert.equal(actual.overflow, false);
  } finally { rmSync(temp, { recursive: true, force: true }); }
});

test("真实浏览器中已完成步骤可点击回看且未来步骤保持禁用", { skip: !BROWSER }, () => {
  const temp = mkdtempSync(join(tmpdir(), "cutroom-workflow-navigation-"));
  try {
    const cssUrl = new URL("../web-ui/public/styles.css", import.meta.url).href;
    const workflowSource = readFileSync(
      new URL("../web-ui/public/workflow-view.mjs", import.meta.url),
      "utf8",
    ).replaceAll("export ", "");
    const fixture = join(temp, "workflow-navigation.html");
    writeFileSync(fixture, `<!doctype html>
      <html><head><meta charset="utf-8"><link rel="stylesheet" href="${cssUrl}"></head>
      <body><main id="cutroom-workbench"><section class="content">
        <div class="fullflow" id="workflow" aria-label="完整任务流程"></div>
        <pre id="probe"></pre>
      </section></main>
      <script>
        ${workflowSource}
        const workflow = {
          current_stage: "fine_render",
          run_status: "awaiting_user",
          stages: Object.fromEntries(WORKFLOW_STAGES.map((stage, index) => [
            stage.key,
            { status: index < 8 ? "completed" : index === 8 ? "ready" : "not_started" },
          ])),
        };
        const container = document.querySelector("#workflow");
        let selected = null;
        const draw = (viewingStageKey = null) => renderWorkflow(container, workflow, {
          viewingStageKey,
          onSelect: (stageKey) => {
            selected = stageKey;
            draw(stageKey);
          },
        });
        draw();
        document.querySelector('[data-stage-key="packaging_confirm"]').click();
        document.querySelector('[data-stage-key="auto_qa"]').click();
        const completed = document.querySelector('[data-stage-key="packaging_confirm"]');
        const current = document.querySelector('[data-stage-key="fine_render"]');
        const future = document.querySelector('[data-stage-key="auto_qa"]');
        document.querySelector("#probe").textContent = JSON.stringify({
          selected,
          phaseCount: container.querySelectorAll("[data-phase-key]").length,
          visibleStepCount: container.querySelectorAll("[data-stage-key]").length,
          completedPressed: completed.getAttribute("aria-pressed"),
          completedCursor: getComputedStyle(completed).cursor,
          currentMarker: current.getAttribute("aria-current"),
          futureDisabled: future.disabled,
          futureCursor: getComputedStyle(future).cursor,
          documentWidth: document.documentElement.scrollWidth,
          clientWidth: document.documentElement.clientWidth,
        });
      </script></body></html>`, "utf8");

    const result = spawnSync(BROWSER, [
      "--headless=new",
      "--disable-gpu",
      "--no-first-run",
      "--allow-file-access-from-files",
      `--user-data-dir=${join(temp, "browser-profile")}`,
      "--window-size=375,900",
      "--dump-dom",
      pathToFileURL(fixture).href,
    ], { encoding: "utf8", timeout: 30_000 });

    assert.equal(result.status, 0, result.stderr);
    const match = result.stdout.match(/<pre id="probe">([^<]+)<\/pre>/);
    assert.ok(
      match,
      "浏览器没有返回步骤导航交互结果\n" + result.stderr + "\n" + result.stdout.slice(-1200),
    );
    const rendered = JSON.parse(match[1].replaceAll("&quot;", '"'));
    assert.deepEqual(rendered, {
      selected: "packaging_confirm",
      phaseCount: 4,
      visibleStepCount: 4,
      completedPressed: "true",
      completedCursor: "pointer",
      currentMarker: "step",
      futureDisabled: true,
      futureCursor: "not-allowed",
      documentWidth: rendered.clientWidth,
      clientWidth: rendered.clientWidth,
    });
  } finally {
    rmSync(temp, { recursive: true, force: true });
  }
});

test("桌面端工作台占满浏览器视口且没有悬浮卡片外框", { skip: !BROWSER }, () => {
  const temp = mkdtempSync(join(tmpdir(), "cutroom-layout-"));
  try {
    const cssUrl = new URL("../web-ui/public/styles.css", import.meta.url).href;
    const fixture = join(temp, "layout.html");
    writeFileSync(fixture, `<!doctype html>
      <html><head><meta charset="utf-8"><link rel="stylesheet" href="${cssUrl}"></head>
      <body>
        <main id="cutroom-workbench">
          <section class="app">
            <header class="top"></header>
            <div class="shell">
              <nav class="rail"></nav>
              <aside class="taskboard"></aside>
              <section class="content"></section>
            </div>
          </section>
        </main>
        <pre id="probe"></pre>
        <script>
          addEventListener("load", () => {
            const root = document.querySelector("#cutroom-workbench");
            const app = document.querySelector(".app");
            const shell = document.querySelector(".shell");
            const appRect = app.getBoundingClientRect();
            document.querySelector("#probe").textContent = JSON.stringify({
              viewportWidth: innerWidth,
              viewportHeight: innerHeight,
              rootWidth: root.getBoundingClientRect().width,
              appLeft: appRect.left,
              appWidth: appRect.width,
              appHeight: appRect.height,
              shellHeight: shell.getBoundingClientRect().height,
              rootPadding: getComputedStyle(root).padding,
              borderRadius: getComputedStyle(app).borderRadius,
              boxShadow: getComputedStyle(app).boxShadow,
            });
          });
        </script>
      </body></html>`, "utf8");

    const result = spawnSync(BROWSER, [
      "--headless=new",
      "--disable-gpu",
      "--no-first-run",
      "--allow-file-access-from-files",
      `--user-data-dir=${join(temp, "browser-profile")}`,
      "--window-size=1920,1080",
      "--dump-dom",
      pathToFileURL(fixture).href,
    ], { encoding: "utf8", timeout: 30_000 });

    assert.equal(result.status, 0, result.stderr);
    const match = result.stdout.match(/<pre id="probe">([^<]+)<\/pre>/);
    assert.ok(match, "浏览器没有返回布局测量结果");
    const layout = JSON.parse(match[1].replaceAll("&quot;", '"'));

    assert.equal(layout.rootPadding, "0px");
    assert.equal(layout.appLeft, 0);
    assert.equal(layout.appWidth, layout.rootWidth);
    assert.ok(layout.appHeight >= layout.viewportHeight);
    assert.ok(layout.shellHeight >= layout.viewportHeight - 58);
    assert.equal(layout.borderRadius, "0px");
    assert.equal(layout.boxShadow, "none");
  } finally {
    rmSync(temp, { recursive: true, force: true });
  }
});

test("正式页面用统一的本地 SVG 表达品牌、菜单和四个主导航入口", { skip: !BROWSER }, () => {
  const temp = mkdtempSync(join(tmpdir(), "cutroom-icons-"));
  try {
    const indexUrl = new URL("../web-ui/public/index.html", import.meta.url);
    const cssUrl = new URL("../web-ui/public/styles.css", import.meta.url).href;
    const fixture = join(temp, "icons.html");
    const probe = [
      '<pre id="icon-probe"></pre>',
      "<script>",
      'const icons = [...document.querySelectorAll("svg[data-icon]")].map((icon) => ({',
      "  name: icon.dataset.icon,",
      "  tag: icon.tagName.toLowerCase(),",
      '  viewBox: icon.getAttribute("viewBox"),',
      "  width: getComputedStyle(icon).width,",
      "  height: getComputedStyle(icon).height,",
      '  fill: icon.getAttribute("fill"),',
      '  stroke: icon.getAttribute("stroke"),',
      "}));",
      'const navigation = [...document.querySelectorAll(".nav")].map((button) => ({',
      "  page: button.dataset.page,",
      '  icon: button.querySelector("svg")?.dataset.icon || null,',
      "}));",
      'document.querySelector("#icon-probe").textContent = JSON.stringify({',
      "  icons,",
      "  navigation,",
      '  toggle: document.querySelector("[data-toggle-rail] svg")?.dataset.icon || null,',
      '  brand: document.querySelector(".logo svg")?.dataset.icon || null,',
      "});",
      "</script>",
      "</body>",
    ].join("\n");
    const page = readFileSync(indexUrl, "utf8")
      .replace('href="/styles.css"', 'href="' + cssUrl + '"')
      .replace('<script type="module" src="/app.mjs"></script>', "")
      .replace("</body>", probe);
    writeFileSync(fixture, page, "utf8");

    const result = spawnSync(BROWSER, [
      "--headless=new",
      "--disable-gpu",
      "--no-first-run",
      "--allow-file-access-from-files",
      "--user-data-dir=" + join(temp, "browser-profile"),
      "--window-size=1920,1080",
      "--dump-dom",
      pathToFileURL(fixture).href,
    ], { encoding: "utf8", timeout: 30_000 });

    assert.equal(result.status, 0, result.stderr);
    const match = result.stdout.match(/<pre id="icon-probe">([^<]+)<\/pre>/);
    assert.ok(match, "浏览器没有返回图标测量结果");
    const rendered = JSON.parse(match[1].replaceAll("&quot;", '"'));

    assert.deepEqual(rendered.navigation, [
      { page: "tasks", icon: "list-todo" },
      { page: "library", icon: "clapperboard" },
      { page: "packlib", icon: "package" },
      { page: "settings", icon: "settings" },
    ]);
    assert.equal(rendered.toggle, "panel-left-close");
    assert.equal(rendered.brand, "scissors");
    assert.equal(rendered.icons.length, 6);
    for (const icon of rendered.icons) {
      assert.equal(icon.tag, "svg");
      assert.equal(icon.viewBox, "0 0 24 24");
      assert.ok(["18px", "20px"].includes(icon.width));
      assert.equal(icon.width, icon.height);
      assert.equal(icon.fill, "none");
      assert.equal(icon.stroke, "currentColor");
    }
  } finally {
    rmSync(temp, { recursive: true, force: true });
  }
});

test("页头隐藏页面名称但保留品牌和右侧状态控件", { skip: !BROWSER }, () => {
  const temp = mkdtempSync(join(tmpdir(), "cutroom-header-"));
  try {
    const indexUrl = new URL("../web-ui/public/index.html", import.meta.url);
    const cssUrl = new URL("../web-ui/public/styles.css", import.meta.url).href;
    const fixture = join(temp, "header.html");
    const probe = [
      '<pre id="header-probe"></pre>',
      "<script>",
      'const visible = (selector) => getComputedStyle(document.querySelector(selector)).display !== "none";',
      'document.querySelector("#header-probe").textContent = JSON.stringify({',
      '  brand: visible(".brand"),',
      '  crumb: visible(".crumb"),',
      '  system: visible(".system"),',
      '  heavyStatus: visible(".heavy-status"),',
      '  theme: visible(".theme"),',
      "});",
      "</script>",
      "</body>",
    ].join("\n");
    const page = readFileSync(indexUrl, "utf8")
      .replace('href="/styles.css"', 'href="' + cssUrl + '"')
      .replace('<script type="module" src="/app.mjs"></script>', "")
      .replace("</body>", probe);
    writeFileSync(fixture, page, "utf8");

    const result = spawnSync(BROWSER, [
      "--headless=new",
      "--disable-gpu",
      "--no-first-run",
      "--allow-file-access-from-files",
      "--user-data-dir=" + join(temp, "browser-profile"),
      "--window-size=1920,1080",
      "--dump-dom",
      pathToFileURL(fixture).href,
    ], { encoding: "utf8", timeout: 30_000 });

    assert.equal(result.status, 0, result.stderr);
    const match = result.stdout.match(/<pre id="header-probe">([^<]+)<\/pre>/);
    assert.ok(match, "浏览器没有返回页头测量结果");
    const rendered = JSON.parse(match[1].replaceAll("&quot;", '"'));

    assert.deepEqual(rendered, {
      brand: true,
      crumb: false,
      system: true,
      heavyStatus: true,
      theme: true,
    });
  } finally {
    rmSync(temp, { recursive: true, force: true });
  }
});

test("主导航标签保持单行且折叠控制始终留在左侧栏", { skip: !BROWSER }, () => {
  const temp = mkdtempSync(join(tmpdir(), "cutroom-rail-"));
  try {
    const indexUrl = new URL("../web-ui/public/index.html", import.meta.url);
    const cssUrl = new URL("../web-ui/public/styles.css", import.meta.url).href;
    const fixture = join(temp, "rail.html");
    const probe = [
      '<pre id="rail-probe"></pre>',
      "<script>",
      'const app = document.querySelector(".app");',
      'const shell = document.querySelector(".shell");',
      'const rail = document.querySelector(".rail");',
      'const taskboard = document.querySelector(".taskboard");',
      'const content = document.querySelector(".content");',
      'const toggle = document.querySelector("[data-toggle-rail]");',
      'const nav = [...document.querySelectorAll(".nav")];',
      "const expanded = {",
      "  toggleInRail: toggle.parentElement === rail,",
      '  toggleInTop: document.querySelector(".top").contains(toggle),',
      "  railWidth: getComputedStyle(rail).width,",
      "  nav: nav.map((button) => ({",
      "    label: button.textContent.trim(),",
      "    whiteSpace: getComputedStyle(button).whiteSpace,",
      "    width: button.getBoundingClientRect().width,",
      "    scrollWidth: button.scrollWidth,",
      "  })),",
      "};",
      'app.classList.add("rail-off");',
      "const collapsed = {",
      "  railWidth: getComputedStyle(rail).width,",
      "  railDisplay: getComputedStyle(rail).display,",
      "  toggleDisplay: getComputedStyle(toggle).display,",
      "  navDisplay: nav.map((button) => getComputedStyle(button).display),",
      '  labelDisplay: nav.map((button) => button.querySelector(".nav-label") ? getComputedStyle(button.querySelector(".nav-label")).display : "missing"),',
      '  iconWidth: nav.map((button) => button.querySelector("svg").getBoundingClientRect().width),',
      "  taskboardWidth: taskboard.getBoundingClientRect().width,",
      "  contentWidth: content.getBoundingClientRect().width,",
      "  shellWidth: shell.getBoundingClientRect().width,",
      "};",
      'document.querySelector("#rail-probe").textContent = JSON.stringify({ expanded, collapsed });',
      "</script>",
      "</body>",
    ].join("\n");
    const page = readFileSync(indexUrl, "utf8")
      .replace('href="/styles.css"', 'href="' + cssUrl + '"')
      .replace('<script type="module" src="/app.mjs"></script>', "")
      .replace("</body>", probe);
    writeFileSync(fixture, page, "utf8");

    const result = spawnSync(BROWSER, [
      "--headless=new",
      "--disable-gpu",
      "--no-first-run",
      "--allow-file-access-from-files",
      "--user-data-dir=" + join(temp, "browser-profile"),
      "--window-size=1920,1080",
      "--dump-dom",
      pathToFileURL(fixture).href,
    ], { encoding: "utf8", timeout: 30_000 });

    assert.equal(result.status, 0, result.stderr);
    const match = result.stdout.match(/<pre id="rail-probe">([^<]+)<\/pre>/);
    assert.ok(match, "浏览器没有返回侧栏测量结果");
    const rendered = JSON.parse(match[1].replaceAll("&quot;", '"'));

    assert.equal(rendered.expanded.toggleInRail, true);
    assert.equal(rendered.expanded.toggleInTop, false);
    assert.equal(rendered.expanded.railWidth, "108px");
    assert.deepEqual(
      rendered.expanded.nav.map((item) => item.label),
      ["任务", "镜头素材", "包装素材", "设置"],
    );
    for (const item of rendered.expanded.nav) {
      assert.equal(item.whiteSpace, "nowrap");
      assert.ok(item.scrollWidth <= Math.ceil(item.width));
    }
    assert.equal(rendered.collapsed.railWidth, "48px");
    assert.equal(rendered.collapsed.railDisplay, "grid");
    assert.equal(rendered.collapsed.toggleDisplay, "grid");
    assert.deepEqual(rendered.collapsed.navDisplay, ["grid", "grid", "grid", "grid"]);
    assert.deepEqual(rendered.collapsed.labelDisplay, ["none", "none", "none", "none"]);
    assert.ok(rendered.collapsed.iconWidth.every((width) => width >= 20));
    assert.equal(rendered.collapsed.taskboardWidth, 242);
    assert.ok(rendered.collapsed.contentWidth > 0);
    assert.equal(
      rendered.collapsed.railWidth.replace("px", "") * 1
        + rendered.collapsed.taskboardWidth
        + rendered.collapsed.contentWidth,
      rendered.collapsed.shellWidth,
    );
  } finally {
    rmSync(temp, { recursive: true, force: true });
  }
});

test("页头提供可直接打开的同事使用手册入口", { skip: !BROWSER }, () => {
  const temp = mkdtempSync(join(tmpdir(), "cutroom-manual-link-"));
  try {
    const indexUrl = new URL("../web-ui/public/index.html", import.meta.url);
    const cssUrl = new URL("../web-ui/public/styles.css", import.meta.url).href;
    const fixture = join(temp, "manual-link.html");
    const probe = [
      '<pre id="manual-link-probe"></pre>',
      "<script>",
      'const link = document.querySelector("[data-open-manual]");',
      'document.querySelector("#manual-link-probe").textContent = JSON.stringify({',
      "  found: Boolean(link),",
      '  visible: link ? getComputedStyle(link).display !== "none" : false,',
      '  href: link?.getAttribute("href") || null,',
      '  target: link?.getAttribute("target") || null,',
      '  rel: link?.getAttribute("rel") || null,',
      "});",
      "</script>",
      "</body>",
    ].join("\n");
    const page = readFileSync(indexUrl, "utf8")
      .replace('href="/styles.css"', 'href="' + cssUrl + '"')
      .replace('<script type="module" src="/app.mjs"></script>', "")
      .replace("</body>", probe);
    writeFileSync(fixture, page, "utf8");

    const result = spawnSync(BROWSER, [
      "--headless=new",
      "--disable-gpu",
      "--no-first-run",
      "--allow-file-access-from-files",
      "--user-data-dir=" + join(temp, "browser-profile"),
      "--window-size=1920,1080",
      "--dump-dom",
      pathToFileURL(fixture).href,
    ], { encoding: "utf8", timeout: 30_000 });

    assert.equal(result.status, 0, result.stderr);
    const match = result.stdout.match(/<pre id="manual-link-probe">([^<]+)<\/pre>/);
    assert.ok(match, "浏览器没有返回手册入口状态");
    const rendered = JSON.parse(match[1].replaceAll("&quot;", '"'));
    assert.deepEqual(rendered, {
      found: true,
      visible: true,
      href: "/help.html",
      target: "_blank",
      rel: "noopener",
    });
  } finally {
    rmSync(temp, { recursive: true, force: true });
  }
});

test("五档视口无页面横向溢出且四个主导航始终可达", { skip: !BROWSER }, () => {
  const widths = [375, 768, 1024, 1440, 1920];
  for (const width of widths) {
    const temp = mkdtempSync(join(tmpdir(), "cutroom-responsive-"));
    try {
      const indexUrl = new URL("../web-ui/public/index.html", import.meta.url);
      const cssUrl = new URL("../web-ui/public/styles.css", import.meta.url).href;
      const fixture = join(temp, "responsive.html");
      const probe = [
        '<pre id="responsive-probe"></pre>',
        "<script>",
        'const app = document.querySelector(".app");',
        'const nav = [...document.querySelectorAll(".nav")];',
        'const taskboard = document.querySelector(".taskboard");',
        'const shell = document.querySelector(".shell");',
        'const content = document.querySelector(".content");',
        'app.classList.add("rail-off");',
        'document.querySelector("#responsive-probe").textContent = JSON.stringify({',
        "  viewportWidth: innerWidth,",
        "  clientWidth: document.documentElement.clientWidth,",
        "  documentWidth: document.documentElement.scrollWidth,",
        "  bodyWidth: document.body.scrollWidth,",
        "  shellWidth: shell.getBoundingClientRect().width,",
        "  contentWidth: content.getBoundingClientRect().width,",
        '  taskboardVisible: getComputedStyle(taskboard).display !== "none",',
        "  visibleNavigation: nav.filter((item) => item.getBoundingClientRect().width > 0 && item.getBoundingClientRect().height > 0).length,",
        '  labelDisplays: nav.map((item) => getComputedStyle(item.querySelector(".nav-label")).display),',
        "});",
        "</script>",
        "</body>",
      ].join("\n");
      const page = readFileSync(indexUrl, "utf8")
        .replace('href="/styles.css"', 'href="' + cssUrl + '"')
        .replace('<script type="module" src="/app.mjs"></script>', "")
        .replace("</body>", probe);
      writeFileSync(fixture, page, "utf8");
      const result = spawnSync(BROWSER, [
        "--headless=new",
        "--disable-gpu",
        "--no-first-run",
        "--allow-file-access-from-files",
        "--user-data-dir=" + join(temp, "browser-profile"),
        "--window-size=" + width + ",900",
        "--dump-dom",
        pathToFileURL(fixture).href,
      ], { encoding: "utf8", timeout: 30_000 });
      assert.equal(result.status, 0, result.stderr);
      const match = result.stdout.match(/<pre id="responsive-probe">([^<]+)<\/pre>/);
      assert.ok(match, "浏览器没有返回 " + width + "px 布局测量结果");
      const layout = JSON.parse(match[1].replaceAll("&quot;", '"'));
      assert.ok(layout.documentWidth <= layout.clientWidth, width + "px 出现文档横向溢出");
      assert.ok(layout.bodyWidth <= layout.clientWidth, width + "px 出现 body 横向溢出");
      assert.equal(layout.shellWidth, layout.clientWidth);
      assert.ok(layout.contentWidth > 0);
      assert.equal(layout.visibleNavigation, 4);
      assert.equal(layout.taskboardVisible, width > 1180);
      if (width <= 760) {
        assert.ok(layout.labelDisplays.every((display) => display !== "none"));
      }
    } finally {
      rmSync(temp, { recursive: true, force: true });
    }
  }
});
