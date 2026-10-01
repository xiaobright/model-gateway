// 编排画布的浏览器验收：不只看画出来了没有，走一遍四个核心操作，并核对后端真的变了。
// 用法：node dev/check-canvas.mjs [port]
//
// 前提：另开一个终端跑 .venv\Scripts\python.exe dev/preview-canvas.py --port 8331
// 截图写到 dev/canvas-qa/。
import { spawn } from 'node:child_process';
import { mkdirSync, writeFileSync } from 'node:fs';
import { setTimeout as sleep } from 'node:timers/promises';

const PORT = process.argv[2] || '8331';
const APP = `http://127.0.0.1:${PORT}`;
const CHROME = process.env.CHROME || 'C:/Program Files/Google/Chrome/Application/chrome.exe';
const DEBUG_PORT = Number(process.env.CDP_PORT || 9231);
const OUT = process.env.CANVAS_QA_DIR || 'dev/canvas-qa';
mkdirSync(OUT, { recursive: true });

let failures = 0;
const ok = (label, pass, extra = '') => {
  if (!pass) failures += 1;
  console.log(`${pass ? 'PASS' : 'FAIL'}  ${label}${extra ? '  ' + extra : ''}`);
};

const chrome = spawn(CHROME, [
  '--headless=new', `--remote-debugging-port=${DEBUG_PORT}`,
  '--user-data-dir=' + process.env.TEMP + '/mg-canvas-profile',
  '--no-first-run', '--no-default-browser-check',
  '--window-size=1440,960', '--force-device-scale-factor=1',
  'about:blank',
], { stdio: 'ignore' });

let ws;
const pending = new Map();
const consoleErrors = [];
let nextId = 1;
const send = (method, params = {}) => new Promise((res, rej) => {
  const id = nextId++;
  pending.set(id, { res, rej });
  ws.send(JSON.stringify({ id, method, params }));
});

async function connect() {
  for (let i = 0; i < 40; i++) {
    try {
      const list = await (await fetch(`http://127.0.0.1:${DEBUG_PORT}/json/list`)).json();
      const page = list.find((t) => t.type === 'page');
      if (!page) { await sleep(250); continue; }
      ws = new WebSocket(page.webSocketDebuggerUrl);
      await new Promise((res, rej) => { ws.onopen = res; ws.onerror = rej; });
      ws.onmessage = (ev) => {
        const m = JSON.parse(ev.data);
        if (m.method === 'Runtime.exceptionThrown') {
          consoleErrors.push(m.params.exceptionDetails.exception?.description
            || m.params.exceptionDetails.text);
        }
        if (m.method === 'Runtime.consoleAPICalled' && m.params.type === 'error') {
          consoleErrors.push(m.params.args.map((a) => a.value || a.description).join(' '));
        }
        if (m.id && pending.has(m.id)) {
          const { res, rej } = pending.get(m.id);
          pending.delete(m.id);
          m.error ? rej(new Error(JSON.stringify(m.error))) : res(m.result);
        }
      };
      return;
    } catch { await sleep(250); }
  }
  throw new Error('连不上 Chrome');
}

const evaluate = async (expr) => {
  const r = await send('Runtime.evaluate',
    { expression: expr, returnByValue: true, awaitPromise: true });
  if (r.exceptionDetails) throw new Error(r.exceptionDetails.exception?.description);
  return r.result.value;
};

const shot = async (name) => {
  const { data } = await send('Page.captureScreenshot', { format: 'png' });
  writeFileSync(`${OUT}/${name}.png`, Buffer.from(data, 'base64'));
};

/* 真实鼠标事件。合成 PointerEvent 走不了浏览器的指针状态机，
   拖拽用例必须用 Input.dispatchMouseEvent 才作数 */
const mouse = (type, x, y) => send('Input.dispatchMouseEvent', {
  type, x: Math.round(x), y: Math.round(y), button: 'left',
  buttons: type === 'mouseReleased' ? 0 : 1, clickCount: 1,
});

async function drag(from, to, steps = 14) {
  await mouse('mousePressed', from.x, from.y);
  for (let i = 1; i <= steps; i += 1) {
    await mouse('mouseMoved', from.x + ((to.x - from.x) * i) / steps,
      from.y + ((to.y - from.y) * i) / steps);
    await sleep(16);
  }
  await mouse('mouseReleased', to.x, to.y);
  await sleep(500);
}

const api = async (path) => (await fetch(`${APP}${path}`)).json();

try {
  const upstreams = await api('/admin/api/upstreams');
  if (!upstreams.length || upstreams.some(u => !u.base_url?.includes('.example.invalid'))) throw new Error('仅可对假站点预览库运行');
  await connect();
  await send('Page.enable');
  await send('Runtime.enable');
  await send('Page.navigate', { url: `${APP}/#canvas` });
  await sleep(2600);
  const click = async (selector) => {
    const p = await evaluate(`(() => { const e = document.querySelector(${JSON.stringify(selector)}); e.scrollIntoView({block:'center'}); const b=e.getBoundingClientRect(); return {x:b.x+b.width/2,y:b.y+b.height/2}; })()`);
    await mouse('mousePressed', p.x, p.y); await mouse('mouseReleased', p.x, p.y); await sleep(350);
  };
  const select = async (name, proto='openai') => click(`.cv-bub[data-model="${name}"][data-proto="${proto}"] .cv-core`);
  const row = async (name, proto='openai') => (await api('/admin/api/models')).find(r => r.model_name === name && r.protocol === proto);
  await select('my-chat');
  ok('只展开选中的一个气泡', await evaluate(`document.querySelectorAll('.cv-bub.is-selected').length === 1 && [...document.querySelectorAll('.cv-bub:not(.is-selected) .cv-ports')].every(e => getComputedStyle(e).display === 'none')`));
  ok('候选链包含完整名称和顺序操作', await evaluate(`document.querySelectorAll('#cv-chain .cv-chain-card').length >= 3 && document.querySelectorAll('.cv-up,.cv-link').length === 0`));
  const before = await row('my-chat');
  const otherProto = await row('my-chat', 'openai-chat');
  const candidate = before.candidates.find(c => c.upstream_enabled && c.group_enabled && c.route_id !== before.preferred_route_id);
  await click(`#cv-chain [data-act="cv-switch"][data-rid="${candidate.route_id}"]`);
  const switched = await row('my-chat');
  ok('设首选只修改首选，不改候选顺序', switched.preferred_route_id === candidate.route_id && JSON.stringify(switched.candidates.map(c=>c.route_id)) === JSON.stringify(before.candidates.map(c=>c.route_id)));
  ok('同名其他接口不变', JSON.stringify(await row('my-chat','openai-chat')) === JSON.stringify(otherProto));
  const rid = switched.candidates[1].route_id;
  await click(`#cv-chain [data-act="cv-move"][data-rid="${rid}"][data-delta="-1"]`);
  const reordered = await row('my-chat');
  ok('箭头链前移落库且保留首选', reordered.candidates[0].route_id === rid && reordered.preferred_route_id === candidate.route_id);
  // 用真实鼠标拨盘，核对后端首选；不以 CSS 旋转作为成功证据。
  await evaluate(`document.getElementById('cv-host').scrollIntoView({block:'center'})`);
  const dial = await evaluate(`(() => {
    const e=document.querySelector('.cv-bub.is-selected'); const b=e.getBoundingClientRect();
    const k=e.querySelector('.cv-knob').getBoundingClientRect();
    const ports=[...e.querySelectorAll('.cv-port:not(.is-off)')];
    const p=ports.find(p=>!p.classList.contains('is-preferred')); const t=p.getBoundingClientRect();
    return {from:{x:k.x+k.width/2,y:k.y+k.height/2},to:{x:t.x+t.width/2,y:t.y+t.height/2},rid:Number(p.dataset.rid)};
  })()`);
  await drag(dial.from,dial.to);
  ok('拨盘设置首选', (await row('my-chat')).preferred_route_id === dial.rid);
  // 上游池拖拽挂载，并验证重复拖拽不增加候选。
  const source = '.cv-chip[data-remote="o5-mini"]';
  await evaluate(`document.querySelector(${JSON.stringify(source)}).scrollIntoView({block:'center'})`);
  const points = await evaluate(`(() => {const a=document.querySelector(${JSON.stringify(source)}).getBoundingClientRect();const b=document.querySelector('.cv-bub.is-selected .cv-core').getBoundingClientRect();return {a:{x:a.x+a.width/2,y:a.y+a.height/2},b:{x:b.x+b.width/2,y:b.y+b.height/2}};})()`);
  const existing = (await row('my-chat')).candidates.find(c => c.remote_model === 'o5-mini');
  const count = (await row('my-chat')).candidates.length;
  await drag(points.a,points.b);
  ok('上游池拖拽连接候选', (await row('my-chat')).candidates.length === count+(existing ? 0 : 1));
  await drag(points.a,points.b);
  ok('重复连接不增加候选', (await row('my-chat')).candidates.length === count+(existing ? 0 : 1));
  await select('alias-chat');
  ok('全部转发显示完整路径且隐藏自身轮盘', await evaluate(`document.getElementById('cv-chain').textContent.includes('全部转发：alias-chat → my-chat') && getComputedStyle(document.querySelector('.cv-bub.is-selected .cv-ports')).display === 'none'`));
  await click('#cv-chain [data-act="cv-forward"]');
  const targets = await evaluate(`[...document.getElementById('cv-fwd-target').options].map(o=>o.value)`);
  ok('排除会成环的目标', !targets.includes('alias2-chat'));
  await evaluate(`document.getElementById('cv-fwd-dialog').close()`);
  await select('fast-chat');
  await click('#cv-chain [data-act="cv-forward"]');
  const multi = await evaluate(`[...document.getElementById('cv-fwd-target').options].map(o=>o.value)`);
  ok('允许转发到已有转发链', multi.includes('alias2-chat'));
  await evaluate(`document.getElementById('cv-fwd-target').value='alias2-chat'`);
  await click('#cv-fwd-save');
  ok('多跳全部转发生效', (await row('fast-chat')).forward_to === 'alias2-chat');
  await click('#cv-chain [data-act="cv-unforward"]');
  ok('取消转发恢复自己的候选', !(await row('fast-chat')).forward_to && (await row('fast-chat')).candidates.length === 1);
  await select('my-chat');
  const geo = await evaluate(`(() => {const b=document.querySelector('.cv-bub.is-selected .cv-core').getBoundingClientRect();return {x:b.x+b.width/2,y:b.y+b.height/2};})()`);
  const layoutBefore = await api('/admin/api/canvas-layout');
  await drag(geo,{x:geo.x+30,y:geo.y+20});
  await sleep(800);
  const layoutAfter = await api('/admin/api/canvas-layout');
  ok('气泡坐标保存', JSON.stringify(layoutBefore.nodes['m|my-chat|openai']) !== JSON.stringify(layoutAfter.nodes['m|my-chat|openai']));
  await send('Page.reload'); await sleep(1800);
  ok('刷新后坐标保留', JSON.stringify((await api('/admin/api/canvas-layout')).nodes['m|my-chat|openai']) === JSON.stringify(layoutAfter.nodes['m|my-chat|openai']));
  await select('my-chat');
  const center = await evaluate(`(() => {const b=document.getElementById('cv-host').getBoundingClientRect();return {x:b.x+b.width/2,y:b.y+b.height/2};})()`);
  await send('Input.dispatchMouseEvent',{type:'mouseWheel',x:center.x,y:center.y,deltaX:0,deltaY:-100});
  await sleep(800);
  ok('滚轮缩放保存', (await api('/admin/api/canvas-layout')).view.z > layoutAfter.view.z);
  await click('[data-act="cv-fit"]');
  const core = await evaluate(`(() => {const b=document.querySelector('.cv-bub.is-selected .cv-core').getBoundingClientRect();return {x:b.x+b.width/2,y:b.y+b.height/2};})()`);
  await send('Input.dispatchMouseEvent',{type:'mousePressed',x:core.x,y:core.y,button:'right',clickCount:1});
  await send('Input.dispatchMouseEvent',{type:'mouseReleased',x:core.x,y:core.y,button:'right',clickCount:1});
  ok('气泡右键菜单可用', await evaluate(`!document.getElementById('cv-menu').hidden`));
  await evaluate(`document.body.click()`);
  await evaluate(`document.querySelectorAll('#toasts .toast').forEach(e=>e.click())`);
  await evaluate(`document.querySelector('section[data-view="canvas"]').scrollIntoView({block:'start'})`);
  await shot('focused-chain');
  await send('Emulation.setDeviceMetricsOverride', {width: 760,height:1000,deviceScaleFactor:1,mobile:false});
  await sleep(300);
  ok('窄窗口候选链在容器内滚动', await evaluate(`document.getElementById('cv-chain').getBoundingClientRect().right <= innerWidth + 1`));
  await shot('focused-chain-narrow');
  ok('无 JS 未捕获错误', consoleErrors.length===0, consoleErrors.join(' | '));
  console.log(failures ? `${failures} 项失败` : '全部通过');
} catch (e) {
  console.error('验收脚本自身出错：', e.message);
  failures += 1;
} finally {
  try { ws && ws.close(); } catch { /* 忽略 */ }
  chrome.kill();
  process.exit(failures ? 1 : 0);
}
