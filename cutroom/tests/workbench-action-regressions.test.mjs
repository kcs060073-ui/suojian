import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import vm from 'node:vm';
import test from 'node:test';
import { spawnSync } from 'node:child_process';
import { existsSync, mkdtempSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { pathToFileURL } from 'node:url';

const app = readFileSync(new URL('../web-ui/public/app.mjs', import.meta.url), 'utf8');
const block = (start, end) => app.slice(app.indexOf(start), app.indexOf(end, app.indexOf(start)));

test('重新锁定画面成功后回到实际阶段，普通粗剪编辑仍保留编辑页', async () => {
  const state = { selectedTaskId: 'task-a', viewingStageKey: null, workspaceOverride: 'rough-cut' };
  const displayed = [];
  const context = vm.createContext({ state, api: async () => ({}), jsonRequest: () => ({}),
    refreshBootstrap: async () => {}, openTask: async () => displayed.push(state.workspaceOverride) });
  vm.runInContext(block('async function taskPost(', 'function packagingPreviewKey('), context);
  await context.taskPost('/rough-cut/undo');
  await context.taskPost('/picture/lock');
  assert.deepEqual(displayed, ['rough-cut', null]);
});

test('读取包装预览不会同步重入渲染，避免重复面板与失效控件', async () => {
  let resolve;
  const response = new Promise(done => { resolve = done; });
  let renders = 0;
  const state = { loadingPackagingPreviews: new Set(), packagingPreviews: {} };
  const context = vm.createContext({ state, api: () => response,
    packagingPreviewKey: (task, plan) => `${task}:${plan}`,
    rerenderSelectedPackaging: () => { renders++; }, schedulePackagingPreviewRefresh: () => {} });
  vm.runInContext(block('async function loadPackagingPreview(', 'async function requestPackagingPreview('), context);
  const pending = context.loadPackagingPreview('task-a', 'plan-a');
  assert.equal(renders, 0, '渲染途中发起读取不能立刻清空并重复渲染同一面板');
  resolve({ status: 'ready' });
  await pending;
  assert.equal(renders, 1);
});

test('包装预览回包不能重绘正在编辑的粗剪页', () => {
  let renders = 0;
  const state = { selectedTaskId: 'task-a', selectedPlanId: 'plan-a', taskDetail: {}, workspaceOverride: 'rough-cut' };
  const context = vm.createContext({ state, renderTaskWorkspace: () => { renders++; } });
  vm.runInContext(block('function rerenderSelectedPackaging(', 'function schedulePackagingPreviewRefresh('), context);
  context.rerenderSelectedPackaging('task-a', 'plan-a');
  assert.equal(renders, 0);
  state.workspaceOverride = null;
  context.rerenderSelectedPackaging('task-a', 'plan-a');
  assert.equal(renders, 1);
  state.taskDetail.workflow = { current_stage: 'full_review' };
  context.rerenderSelectedPackaging('task-a', 'plan-a');
  assert.equal(renders, 1, '不能打断正在审片的播放器');
  state.viewingStageKey = 'packaging_confirm';
  context.rerenderSelectedPackaging('task-a', 'plan-a');
  assert.equal(renders, 2, '历史包装页仍应正常显示预览');
});

test('复制包装后刷新前选中新组合，不继续显示旧方案', async () => {
  const state = { selectedTaskId: 'task-a', selectedPlanId: 'old', viewingStageKey: null };
  let visible;
  const context = vm.createContext({ state, api: async () => ({ planId: 'new' }), jsonRequest: () => ({}),
    refreshBootstrap: async () => {}, openTask: async () => { visible = state.selectedPlanId; } });
  vm.runInContext(block('async function taskPost(', 'function packagingPreviewKey('), context);
  await context.taskPost('/packaging/compose', { cloneFrom: 'old' });
  assert.equal(visible, 'new');
});

test('异步按钮立即反馈并阻止重复提交，失败后可重试', async () => {
  let reject;
  let calls = 0;
  const errors = [];
  const pending = new Promise((_, fail) => { reject = fail; });
  const control = { textContent: '生成推荐方案', disabled: false, attributes: {},
    setAttribute(key, value) { this.attributes[key] = value; } };
  const context = vm.createContext({ showActionError: error => errors.push(error.message) });
  vm.runInContext(block('function button(', 'function renderAiDisclosure('), context);
  assert.equal(typeof context.runButtonAction, 'function');
  const action = () => { calls++; return pending; };
  const first = context.runButtonAction(control, '正在生成…', action);
  assert.equal(control.disabled, true);
  assert.equal(control.textContent, '正在生成…');
  await context.runButtonAction(control, '正在生成…', action);
  assert.equal(calls, 1);
  reject(new Error('连接失败'));
  await first;
  assert.deepEqual(errors, ['连接失败']);
  assert.equal(control.disabled, false);
  assert.equal(control.textContent, '生成推荐方案');
});

const browser = ['C:/Program Files/Google/Chrome/Application/chrome.exe',
  'C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe'].find(existsSync);

test('真实浏览器：包装首次加载只有一组控件，复制与重新推荐立即选中新方案', { skip: !browser }, () => {
  const dir = mkdtempSync(join(tmpdir(), 'cutroom-action-regression-'));
  const flatten = name => readFileSync(new URL(`../web-ui/public/${name}`, import.meta.url), 'utf8')
    .replace(/import\s*\{[\s\S]*?\}\s*from\s*['"][^'"]+['"];?/g, '').replaceAll('export ', '');
  const source = [
    block('function element(', 'function statusBadge('),
    block('function inlineFeedback(', 'function setButtonBusy('),
    block('function button(', 'function renderAiDisclosure('),
    block('async function taskPost(', 'async function jobPost('),
    block('function renderPlanInspector(', 'function renderReview('),
    block('function renderTaskWorkspace(', 'async function openTask('),
    flatten('packaging-preview.mjs'), flatten('packaging-editor.mjs'),
  ].join('\n');
  const fixture = join(dir, 'actions.html');
  writeFileSync(fixture, `<!doctype html><meta charset="utf-8">
    <main id="root"><section id="workspace-primary"></section><aside id="workspace-inspector"></aside></main>
    <pre id="probe"></pre><script>
    ${source}
    const root=document.querySelector('#root');
    const detail={task:{task_id:'task-a',task_type:'ad_mix'},sceneCards:[],packagingPlans:[
      {plan_id:'old',plan_name:'原方案',origin_type:'system_recommendation',items:[]}]};
    const state={selectedTaskId:'task-a',selectedPlanId:null,taskDetail:detail,viewingStageKey:null,
      workspaceOverride:null,bootstrap:{packagingCatalog:[]},packagingPreviews:{},loadingPackagingPreviews:new Set(),
      packagingPreviewPolls:new Map(),packagingTemplates:{ad_mix:[]},loadingPackagingTemplates:new Set(),packagingViewed:new Set()};
    const errors=[];const showActionError=e=>errors.push(e.message);const showToast=()=>{};
    const renderStageSummary=()=>{};const buildWorkflowViewContext=()=>({readOnly:false,workspace:'packaging'});
    const buildRecoveryWorkspace=()=>null;const jsonRequest=(method,body)=>({method,body:JSON.stringify(body)});
    const refreshBootstrap=async()=>{};const openTask=async()=>renderTaskWorkspace(detail);
    const api=async(path,options)=>{
      if(path.endsWith('/compose')){detail.packagingPlans.push({plan_id:'new',plan_name:'复制组合',items:[]});return {planId:'new'};}
      if(path.endsWith('/propose')){detail.packagingPlans.push({plan_id:'recommended',plan_name:'新推荐',origin_type:'system_recommendation',items:[]});return {planIds:['recommended']};}
      return {status:'not_requested'};
    };
    (async()=>{
      renderTaskWorkspace(detail);
      const initialControls=root.querySelectorAll('[data-action="request-packaging-preview"]').length;
      await new Promise(r=>setTimeout(r,0));
      const settledControls=root.querySelectorAll('[data-action="request-packaging-preview"]').length;
      const clone=[...root.querySelectorAll('button')].find(b=>b.textContent==='复制为自选组合');clone.click();
      const cloneBusy=clone.disabled;
      await new Promise(r=>setTimeout(r,0));
      const copiedSelection=state.selectedPlanId;
      const copiedHeading=root.querySelector('#workspace-inspector h2').textContent;
      const propose=[...root.querySelectorAll('button')].find(b=>b.textContent==='重新生成推荐方案');propose.click();
      const proposeBusy=propose.disabled;
      await new Promise(r=>setTimeout(r,0));
      document.querySelector('#probe').textContent=JSON.stringify({initialControls,settledControls,cloneBusy,
        copiedSelection,copiedHeading,proposeBusy,recommendedSelection:state.selectedPlanId,errors});
    })().catch(e=>{document.querySelector('#probe').textContent=JSON.stringify({error:e.stack});});
    </script>`, 'utf8');
  const result = spawnSync(browser, ['--headless=new', '--disable-gpu', '--no-first-run',
    `--user-data-dir=${join(dir, 'profile')}`, '--virtual-time-budget=2000', '--dump-dom', pathToFileURL(fixture).href],
  { encoding: 'utf8', timeout: 30000 });
  assert.equal(result.status, 0, result.stderr);
  const match = result.stdout.match(/<pre id="probe">([^<]+)<\/pre>/);
  assert.ok(match, result.stderr + result.stdout.slice(-2000));
  const actual = JSON.parse(match[1].replaceAll('&quot;', '"').replaceAll('&gt;', '>').replaceAll('&lt;', '<').replaceAll('&amp;', '&'));
  assert.deepEqual(actual, { initialControls: 1, settledControls: 1, cloneBusy: true,
    copiedSelection: 'new', copiedHeading: '复制组合', proposeBusy: true, recommendedSelection: 'recommended', errors: [] });
});
