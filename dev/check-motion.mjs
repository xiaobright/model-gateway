// 动效验收：先运行 uv run python dev/preview-workbench.py --port 8341。
// node dev/check-motion.mjs [隔离预览端口]。临时 Chrome 配置和截图不影响日常浏览器。
import assert from 'node:assert/strict';
import { spawn, spawnSync } from 'node:child_process';
import { mkdtempSync, mkdirSync, readFileSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { setTimeout as sleep } from 'node:timers/promises';

const port = Number(process.argv[2] || 8341);
assert.ok(Number.isInteger(port) && port > 0 && port <= 65535 && port !== 8317,
  '必须使用独立 preview-workbench 端口，禁止连接正式网关 8317');
const origin = `http://127.0.0.1:${port}`;
const profile = mkdtempSync(join(tmpdir(), 'mg-motion-'));
const output = 'dev/.preview/motion-qa';
mkdirSync(output, { recursive: true });
const chrome = spawn(process.env.CHROME || 'C:/Program Files/Google/Chrome/Application/chrome.exe', [
  '--headless=new', '--remote-debugging-port=0', `--user-data-dir=${profile}`,
  '--no-first-run', '--no-default-browser-check', '--disable-background-networking',
  '--window-size=1280,900', '--force-device-scale-factor=1', 'about:blank',
], { stdio: 'ignore', windowsHide: true });
let launchError;
chrome.on('error', error => { launchError = error; });
let ws, serial = 0, checks = 0;
const pending = new Map(), errors = [];
const send = (method, params = {}) => new Promise((resolve, reject) => {
  const id = ++serial;
  const timer = setTimeout(() => { pending.delete(id); reject(new Error(`CDP 超时: ${method}`)); }, 10000);
  pending.set(id, { resolve, reject, timer });
  ws.send(JSON.stringify({ id, method, params }));
});
const evaluate = async expression => {
  const value = await send('Runtime.evaluate', { expression, returnByValue: true, awaitPromise: true });
  if (value.exceptionDetails) throw new Error(value.exceptionDetails.exception?.description || value.exceptionDetails.text);
  return value.result.value;
};
async function wait(expression) {
  const deadline = Date.now() + 8000;
  while (Date.now() < deadline) {
    if (await evaluate(expression)) return;
    await sleep(40);
  }
  throw new Error(`页面条件未满足: ${expression}`);
}
function check(label, pass, details) {
  assert.ok(pass, `${label}: ${JSON.stringify(details)}`);
  checks++;
  console.log(`PASS ${label}${details ? ` ${JSON.stringify(details)}` : ''}`);
}
const frames = 'new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)))';
async function click(selector) {
  const point = await evaluate(`(()=>{const el=document.querySelector(${JSON.stringify(selector)});if(!el) throw new Error('缺少控件'); el.scrollIntoView({behavior:'instant',block:'nearest'});const r=el.getBoundingClientRect();return {x:r.x+r.width/2,y:r.y+r.height/2};})()`);
  await send('Input.dispatchMouseEvent', { type: 'mousePressed', ...point, button: 'left', buttons: 1, clickCount: 1 });
  await send('Input.dispatchMouseEvent', { type: 'mouseReleased', ...point, button: 'left', buttons: 0, clickCount: 1 });
}
async function view(name) {
  await click(`.nav-item[data-view="${name}"]`);
  await wait(`!document.querySelector('.view[data-view="${name}"]').hidden && !document.documentElement.dataset.vtDir`);
}
async function reduce(value) {
  await send('Emulation.setEmulatedMedia', { features: value ? [{ name: 'prefers-reduced-motion', value: 'reduce' }] : [] });
  // MediaQueryList change 随浏览器帧异步派发，等动效控制器处理完再断言。
  await wait(`document.getElementById('motion-toggle').disabled === ${value}`);
}
async function screenshot(name) {
  const { data } = await send('Page.captureScreenshot', { format: 'png' });
  writeFileSync(join(output, `${name}.png`), Buffer.from(data, 'base64'));
}

try {
  let debugPort;
  for (let i = 0; i < 60; i++) {
    if (launchError) throw launchError;
    try { debugPort = Number(readFileSync(join(profile, 'DevToolsActivePort'), 'utf8').split('\n')[0]); break; }
    catch { await sleep(100); }
  }
  assert.ok(debugPort, '临时 Chrome 未启动');
  const targets = await (await fetch(`http://127.0.0.1:${debugPort}/json/list`)).json();
  const target = targets.find(item => item.type === 'page');
  assert.ok(target?.webSocketDebuggerUrl, '没有 Chrome 页面');
  ws = new WebSocket(target.webSocketDebuggerUrl);
  await new Promise((resolve, reject) => { ws.onopen = resolve; ws.onerror = reject; });
  ws.onmessage = event => {
    const message = JSON.parse(event.data);
    if (message.method === 'Runtime.exceptionThrown') errors.push(message.params.exceptionDetails.exception?.description || message.params.exceptionDetails.text);
    const job = pending.get(message.id);
    if (!job) return;
    pending.delete(message.id);
    clearTimeout(job.timer);
    if (message.error) job.reject(new Error(JSON.stringify(message.error)));
    else job.resolve(message.result);
  };
  await send('Runtime.enable');
  await send('Page.enable');
  await send('Page.navigate', { url: origin });
  await wait(`document.querySelectorAll('#route-list .route').length > 0 && !document.documentElement.dataset.vtDir`);
  await evaluate(`window.motionQA=[];window.originalAnimate=Element.prototype.animate;Element.prototype.animate=function(frames,options){const a=window.originalAnimate.call(this,frames,options);motionQA.push({el:this,frames,options,a,view:this.closest('.view')?.dataset.view});return a;};`);

  await view('settings');
  check('页面级联在新视图显示后执行', await evaluate(`motionQA.some(x=>x.view==='settings'&&x.options.duration===420&&!x.a.playState.includes('idle'))`));
  check('CSS 与 JS 使用同一条弹簧曲线', await evaluate(`motionQA.filter(x=>x.view==='settings'&&x.options.duration===420).every(x=>x.options.easing===getComputedStyle(document.documentElement).getPropertyValue('--spring-bouncy').trim())`));

  await view('upstreams');
  await click('[data-act="new-upstream"]');
  const dialog = await evaluate(`(async()=>{const {closeDialog}=await import('/static/motion.js');const d=document.getElementById('up-dialog');const finished=closeDialog(d);const during={open:d.open,inert:d.inert,closing:d.dataset.closing};await finished;return {during,open:d.open,inert:d.inert,display:getComputedStyle(d).display};})()`);
  check('弹窗退场后关闭且不留下布局', dialog.during.open && dialog.during.inert && dialog.during.closing === '1' && !dialog.open && !dialog.inert && dialog.display === 'none', dialog);
  const reopen = await evaluate(`(async()=>{const {closeDialog,openDialog}=await import('/static/motion.js');const d=document.getElementById('up-dialog');openDialog(d);const closing=closeDialog(d);openDialog(d);await closing;await ${frames};const result={open:d.open,inert:d.inert,closing:d.dataset.closing};await closeDialog(d);return result;})()`);
  check('关闭途中重开不受旧关闭影响', reopen.open && !reopen.inert && !reopen.closing, reopen);
  await click('[data-act="new-upstream"]');
  await send('Input.dispatchKeyEvent', { type: 'keyDown', key: 'Escape', code: 'Escape', windowsVirtualKeyCode: 27 });
  await send('Input.dispatchKeyEvent', { type: 'keyUp', key: 'Escape', code: 'Escape', windowsVirtualKeyCode: 27 });
  await wait(`!document.getElementById('up-dialog').open`);
  check('原生 Esc 关闭走退场且恢复窗口状态', await evaluate(`!document.getElementById('up-dialog').inert && !document.getElementById('up-dialog').dataset.closing`));
  await click('[data-act="new-upstream"]');
  await evaluate(`(async()=>{const {confirmBox}=await import('/static/util.js');window.qaConfirmation=confirmBox({title:'动效验收',body:'仅检查确认框返回值，不提交配置',danger:false});await ${frames};})()`);
  await click('#cf-ok');
  const confirmed = await evaluate(`(async()=>{const answer=await qaConfirmation;const result={answer,parentOpen:document.getElementById('up-dialog').open,confirmationOpen:document.getElementById('confirm-dialog').open};const {closeDialog}=await import('/static/motion.js');await closeDialog(document.getElementById('up-dialog'));return result;})()`);
  check('嵌套确认框退场保留结果，不关闭父窗口', confirmed.answer && confirmed.parentOpen && !confirmed.confirmationOpen, confirmed);

  await click('.up-row[data-uid="1"]');
  await wait(`document.querySelectorAll('.grp-row[data-parent-uid="1"]').length > 0`);
  await evaluate(`Promise.all([...document.querySelectorAll('.grp-row[data-parent-uid="1"]')].flatMap(el=>el.getAnimations({subtree:true})).map(a=>a.finished.catch(()=>{})))`);
  const collapsed = await evaluate(`(async()=>{const {toggleUpstreamGroups}=await import('/static/views.js');const rows=[...document.querySelectorAll('.grp-row[data-parent-uid="1"]')];const height=()=>rows.reduce((n,r)=>n+r.getBoundingClientRect().height,0);const start=height(),samples=[];let done=false;const closing=toggleUpstreamGroups(1).then(()=>done=true);while(!done){await new Promise(requestAnimationFrame);samples.push(height());}await closing;return {start,samples,remaining:document.querySelectorAll('.grp-row[data-parent-uid="1"]').length};})()`);
  check('分组收起逐帧缩小空间再移除', collapsed.start > 0 && collapsed.remaining === 0 && collapsed.samples.some(n => n > 0 && n < collapsed.start - 1), collapsed);
  const rapid = await evaluate(`(async()=>{const {toggleUpstreamGroups,renderUpstreams}=await import('/static/views.js');await toggleUpstreamGroups(1);await ${frames};const closing=toggleUpstreamGroups(1);renderUpstreams();await closing;await ${frames};return {remaining:document.querySelectorAll('.grp-row[data-parent-uid="1"]').length,wrappers:document.querySelectorAll('.grp-row td > div[style*="overflow"]').length};})()`);
  check('快速展开再收起及中途刷新不留下临时壳', rapid.remaining === 0 && rapid.wrappers === 0, rapid);
  await screenshot('upstreams');

  await view('live');
  const breakers = await evaluate(`(async()=>{const {renderBreakers}=await import('/static/views.js');const host=document.getElementById('brk-list');renderBreakers({breakers:[]});const data={breakers:[{group_id:1,cooling_ms:9000,fails:2,cooled:2,last_status:503}]};renderBreakers(data);const row=host.firstElementChild,label=row.querySelector('[data-cooling-label]');const before=motionQA.filter(x=>x.el.classList.contains('brk-row')).length;data.breakers[0].cooling_ms=8000;renderBreakers(data);const result={sameRow:host.firstElementChild===row,sameLabel:row.querySelector('[data-cooling-label]')===label,label:label.textContent,before,after:motionQA.filter(x=>x.el.classList.contains('brk-row')).length};renderBreakers({breakers:[]});return result;})()`);
  check('倒计时复用行及文字节点，不重播入场', breakers.sameRow && breakers.sameLabel && breakers.label === '冷却 8s' && breakers.before > 0 && breakers.before === breakers.after, breakers);

  await view('log');
  const log = await evaluate(`(async()=>{const {renderLog}=await import('/static/views.js');const record={id:1000001,protocol:'openai',status:200,note:'ok',model:'motion-preview',remote_model:'preview',client:'Motion QA',upstream:'Preview',group_name:'默认',input_tokens:1,output_tokens:1,duration_ms:1000};renderLog([record]);document.getElementById('log-follow').checked=true;renderLog([{...record,id:1000002},record]);const a=motionQA.filter(x=>x.el.dataset.id==='1000002');return {count:a.length,duration:a[0]?.options.duration,frames:a[0]?.frames,state:a[0]?.a.playState};})()`);
  check('新增记录只播放一条入场加高亮', log.count === 1 && log.duration === 520 && log.frames[0].opacity < 1 && log.frames[0].backgroundColor && log.state !== 'idle', log);

  await view('overview');
  const refresh = await evaluate(`(async()=>{const {state}=await import('/static/util.js');const {renderHealth,renderHot}=await import('/static/views.js');const original=state.overview;state.overview={upstreams:[{name:'Preview A',ok_rate:0.8,p95:1200,n:10}],models:[{model:'preview',protocol:'openai',n:10,bad:1,p95:1000}]};renderHealth();renderHot();const count=()=>motionQA.filter(x=>x.el.matches('.health-row,.bar-row')).length;const before=count();state.overview.upstreams[0].n=11;state.overview.upstreams[0].ok_rate=0.9;state.overview.models[0].n=12;renderHealth();renderHot();const after=count();state.overview=original;renderHealth();renderHot();return {before,after};})()`);
  check('概览已有条目刷新不重播入场', refresh.before === refresh.after, refresh);
  await evaluate(`(async()=>{const {donut}=await import('/static/charts.js');window.qaDonut=donut(0);qaDonut.node.style.cssText='position:absolute;left:-9999px;top:0;width:66px;height:66px';document.body.append(qaDonut.node);qaDonut.set(25);await Promise.all(qaDonut.node.getAnimations({subtree:true}).map(a=>a.finished.catch(()=>{})));})()`);
  await reduce(true);
  const ring = await evaluate(`(()=>{qaDonut.set(80);const arc=qaDonut.node.querySelectorAll('circle')[1];return {label:qaDonut.node.querySelector('span').textContent,attribute:Number(arc.getAttribute('stroke-dashoffset')),computed:parseFloat(getComputedStyle(arc).strokeDashoffset),animations:arc.getAnimations().length};})()`);
  check('减弱动效后环形图与最新数值一致', ring.label === '80%' && Math.abs(ring.attribute - ring.computed) < 0.01 && ring.animations === 0, ring);
  await reduce(false);
  await evaluate(`qaDonut.set(50)`);
  await reduce(true);
  const interrupted = await evaluate(`(()=>{const arc=qaDonut.node.querySelectorAll('circle')[1];return {attribute:Number(arc.getAttribute('stroke-dashoffset')),computed:parseFloat(getComputedStyle(arc).strokeDashoffset),animations:arc.getAnimations().length};})()`);
  check('系统减弱取消在途图表动画并显示最新值', Math.abs(interrupted.attribute - interrupted.computed) < 0.01 && interrupted.animations === 0, interrupted);

  await send('Emulation.setDeviceMetricsOverride', { width: 1000, height: 720, deviceScaleFactor: 1, mobile: false });
  await view('routes');
  const scroll = await evaluate(`(async()=>{const {selectRouteWorkspace}=await import('/static/route-view.js');window.scrollTo({top:0,behavior:'instant'});const original=Element.prototype.scrollIntoView;let behavior;Element.prototype.scrollIntoView=function(options){behavior=options.behavior;return original.call(this,options);};try{selectRouteWorkspace('fast-chat','openai');const immediate=scrollY;await ${frames};return {behavior,immediate,after:scrollY,reduced:matchMedia('(prefers-reduced-motion: reduce)').matches};}finally{Element.prototype.scrollIntoView=original;}})()`);
  check('窄屏减弱动效立即定位详情，不平滑滚动', scroll.reduced && scroll.behavior === 'instant' && scroll.immediate > 0 && scroll.immediate === scroll.after, scroll);
  await reduce(false);
  await send('Emulation.clearDeviceMetricsOverride');
  await evaluate(`qaDonut.node.remove();Element.prototype.animate=window.originalAnimate;window.scrollTo({top:0,behavior:'instant'});`);
  await view('routes');
  await screenshot('routes');
  check('没有页面运行异常', errors.length === 0, errors);
  console.log(`${checks}/${checks} browser checks passed; screenshots: ${output}`);
} catch (error) {
  console.error(error.message);
  process.exitCode = 1;
} finally {
  for (const job of pending.values()) { clearTimeout(job.timer); job.reject(new Error('验收结束')); }
  ws?.close();
  if (chrome.pid) {
    if (process.platform === 'win32') spawnSync('taskkill', ['/PID', String(chrome.pid), '/T', '/F'], { stdio: 'ignore', windowsHide: true });
    else chrome.kill();
  }
}
