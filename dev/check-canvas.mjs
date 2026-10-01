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

/** 首页那张说明图。用假站点和假模型名，别把真实站名带进仓库。

    只截画布区中段：整块 .cv-wrap 高七八百像素，上下大半是空的，裁掉才看得清细节 */
const readmeShot = async () => {
  const box = await evaluate(`(() => {
    const b = document.querySelector('.cv-wrap').getBoundingClientRect();
    return { x: b.left, y: b.top, width: b.width, height: b.height };
  })()`);
  const TOP = 84;
  const { data } = await send('Page.captureScreenshot', {
    format: 'png',
    clip: {
      x: box.x,
      y: box.y + TOP,
      width: box.width,
      height: Math.min(440, box.height - TOP),
      scale: 1.5,
    },
  });
  writeFileSync('dev/canvas-shot.png', Buffer.from(data, 'base64'));
  console.log('已保存 dev/canvas-shot.png（首页用）');
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

/** 某个气泡在屏幕上的位置、环半径和每个端口的坐标 */
const bubble = (model, proto) => evaluate(`(() => {
  const el = document.querySelector('.cv-bub[data-model="${model}"][data-proto="${proto}"]');
  if (!el) return null;
  const box = el.getBoundingClientRect();
  const cx = box.left + box.width / 2, cy = box.top + box.height / 2;
  const r = parseFloat(getComputedStyle(el).getPropertyValue('--r'));
  const ports = [...el.querySelectorAll('.cv-port')].map((p, i) => {
    const b = p.getBoundingClientRect();
    return { i, n: p.querySelector('.cv-port-n').textContent.trim(),
      rid: Number(p.dataset.rid),
      x: b.left + b.width / 2, y: b.top + b.height / 2,
      on: p.classList.contains('is-on'), preferred: p.classList.contains('is-preferred'),
      off: p.classList.contains('is-off'), cooling: p.classList.contains('is-cooling') };
  });
  const needle = el.querySelector('.cv-needle');
  return { cx, cy, r, ports,
    rot: needle.style.transform,
    idle: needle.classList.contains('is-idle') };
})()`);

/** 环上某个角度（0 = 正上方，顺时针）对应的屏幕点 */
const onRing = (b, deg) => {
  const rad = ((deg - 90) * Math.PI) / 180;
  return { x: b.cx + b.r * Math.cos(rad), y: b.cy + b.r * Math.sin(rad) };
};

const modelRow = async (name) =>
  (await api('/admin/api/models')).find((r) => r.model_name === name);

try {
  await connect();
  await send('Page.enable');
  await send('Runtime.enable');
  // 先把画布坐标清空：这样每次运行都从「第一次打开」的同一状态开始，
  // 「首次自动适配」这类断言才有意义（路由配置不动，那部分断言本来就是相对的）
  await fetch(`${APP}/admin/api/canvas-layout`, {
    method: 'PUT',
    headers: { 'content-type': 'application/json' },
    body: JSON.stringify({ nodes: {}, view: { x: 0, y: 0, z: 1 } }),
  });
  await send('Page.navigate', { url: `${APP}/#canvas` });
  await sleep(1500);
  await evaluate(`new Promise(r => { let n = 0; const t = setInterval(() => {
    if (document.querySelectorAll('.cv-bub').length || n++ > 60) { clearInterval(t); r(1); } }, 200); })`);
  await sleep(600);

  // ---------- 1. 画出来了没有 ----------
  const shape = await evaluate(`(() => ({
    view: document.querySelector('section.view[data-view="canvas"]').hidden ? 'hidden' : 'shown',
    bubbles: document.querySelectorAll('.cv-bub').length,
    ups: document.querySelectorAll('.cv-up').length,
    links: document.querySelectorAll('.cv-link').length,
    forwards: document.querySelectorAll('.cv-fwd').length,
    chips: document.querySelectorAll('.cv-chip').length,
    count: document.getElementById('cv-count').textContent,
  }))()`);
  ok('编排视图可见', shape.view === 'shown');
  // openai 下 7 个（含 2 个纯转发）+ openai-chat 下 1 个同名模型 = 8 个气泡
  ok('气泡数 = 8（同名模型在两种接口下各算一个）', shape.bubbles === 8, JSON.stringify(shape));
  ok('上游节点画出来了', shape.ups >= 6, `ups=${shape.ups}`);
  // 挂载箭头数 = 库里候选总数（不写死，跑第二遍时库里有上一轮加的候选）
  const allModels = await api('/admin/api/models');
  const candTotal = allModels.reduce((n, r) => n + r.candidates.length, 0);
  ok('一个候选一根挂载箭头', shape.links === candTotal, `links=${shape.links} 候选=${candTotal}`);
  ok('交接箭头 = 2 条', shape.forwards === 2, `forwards=${shape.forwards}`);
  ok('上游池有可拖的模型', shape.chips > 10, `chips=${shape.chips}`);
  await shot('canvas-01-initial');

  // ---------- 1.5 首次打开要自动框进视野 ----------
  // 框的是气泡（上游节点是参考层，为它一列竖排把缩放压到 0.5 以下，字就糊了）
  const inView = await evaluate(`(() => {
    const host = document.getElementById('cv-host').getBoundingClientRect();
    const off = (sel) => [...document.querySelectorAll(sel)].filter((n) => {
      const b = n.getBoundingClientRect();
      return b.right < host.left || b.left > host.right || b.bottom < host.top || b.top > host.bottom;
    }).length;
    return { bubbles: document.querySelectorAll('.cv-bub').length, offBubbles: off('.cv-bub'),
      ups: document.querySelectorAll('.cv-up').length,
      z: Number(getComputedStyle(document.getElementById('cv-world')).transform.split(',')[0].replace(/[^0-9.-]/g, '')) || 0 };
  })()`);
  ok('首次打开所有气泡都在视野内', inView.offBubbles === 0, JSON.stringify(inView));
  const fitZ = await api('/admin/api/canvas-layout');
  ok('首次适配的缩放不低于可读底线', fitZ.view.z >= 0.6, `z=${fitZ.view.z}`);

  // ---------- 2. 环的顺序 = 降级顺序，指针指向当前生效 ----------
  const my = await bubble('my-chat', 'openai');
  const row = await modelRow('my-chat');
  ok('my-chat 环上有 3 个端口', my.ports.length === 3, `ports=${my.ports.length}`);
  ok('端口编号就是顺序 1/2/3',
    my.ports.map((p) => p.n).join('') === '123', my.ports.map((p) => p.n).join(''));
  const onPort = my.ports.find((p) => p.on);
  ok('只有一个端口是「当前生效」', my.ports.filter((p) => p.on).length === 1);
  ok('生效的端口就是后端的 active_route_id',
    onPort && onPort.rid === row.active_route_id,
    `画布=${onPort && onPort.rid} 后端=${row.active_route_id}`);
  ok('停用的站点在环上是虚的', my.ports.some((p) => p.off), JSON.stringify(my.ports.map((p) => p.off)));
  ok('指针没有 idle（有生效候选）', !my.idle, my.rot);

  const fwd = await bubble('alias-chat', 'openai');
  ok('纯转发模型没有候选端口', fwd.ports.length === 0);
  ok('纯转发模型指针是隐藏的（它没有自己的轮盘）', fwd.idle === true);

  // ---------- 3. 点端口 = 切到那条 ----------
  // 挑一条「能用又不是当前生效」的：第 3 个端口指着停用的站，点它本来就该被拒
  const clickable = my.ports.find((p) => !p.on && !p.off);
  ok('存在一条可切过去的候选', Boolean(clickable), JSON.stringify(my.ports));
  await mouse('mousePressed', clickable.x, clickable.y);
  await mouse('mouseReleased', clickable.x, clickable.y);
  await sleep(900);
  const afterClick = await modelRow('my-chat');
  ok('点端口切到了那条候选', afterClick.active_route_id === clickable.rid,
    `期望=${clickable.rid} 实际=${afterClick.active_route_id}`);

  // 点停用站的端口不该切过去，而该给出提示
  const deadPort = my.ports.find((p) => p.off);
  await mouse('mousePressed', deadPort.x, deadPort.y);
  await mouse('mouseReleased', deadPort.x, deadPort.y);
  await sleep(800);
  const afterDead = await modelRow('my-chat');
  ok('点停用候选的端口不会切过去', afterDead.active_route_id === afterClick.active_route_id);

  // ---------- 4. 拨盘：拖针尖旋转到另一个端口 ----------
  const before = await bubble('my-chat', 'openai');
  const wantIdx = before.ports.findIndex((p) => !p.on && !p.off);
  ok('存在可拨过去的端口', wantIdx >= 0);
  const knobPos = () => evaluate(`(() => {
    const k = document.querySelector('.cv-bub[data-model="my-chat"][data-proto="openai"] .cv-knob');
    const b = k.getBoundingClientRect();
    return { x: b.left + b.width / 2, y: b.top + b.height / 2 };
  })()`);
  const wantDeg = (wantIdx * 360) / before.ports.length;
  await drag(await knobPos(), onRing(before, wantDeg), 16);
  await sleep(900);
  const afterDial = await modelRow('my-chat');
  ok('拨盘拨到某条候选就切了过去', afterDial.active_route_id === before.ports[wantIdx].rid,
    `期望=${before.ports[wantIdx].rid} 实际=${afterDial.active_route_id}`);
  // 拨过之后针尖应该停在新的那个端口上，而不是弹回去
  const rotAfter = await evaluate(`document.querySelector(
    '.cv-bub[data-model="my-chat"][data-proto="openai"] .cv-needle').style.transform`);
  ok('指针停在新端口上', Math.abs(
    (((parseFloat(rotAfter.replace(/[^0-9.-]/g, '')) % 360) + 360) % 360) - wantDeg) < 1,
    `${rotAfter} 期望 ${wantDeg}°`);
  await shot('canvas-02-dialed');

  // ---------- 5. 拖端口调降级顺序 ----------
  const wide = await bubble('wide-chat', 'openai');
  const orderBefore = (await modelRow('wide-chat')).candidates.map((c) => c.route_id);
  // 把最后那个端口拖到第 1 个的位置（0°）
  await drag(
    { x: wide.ports[4].x, y: wide.ports[4].y },
    onRing(wide, 0),
    18,
  );
  await sleep(900);
  const orderAfter = (await modelRow('wide-chat')).candidates.map((c) => c.route_id);
  ok('拖端口改了降级顺序（最后一条挪到最前）',
    orderAfter[0] === orderBefore[4] && orderAfter.length === orderBefore.length,
    `${orderBefore.join(',')} -> ${orderAfter.join(',')}`);
  await shot('canvas-03-reordered');

  // ---------- 6. 从上游池拖一个模型到气泡上 = 挂候选 ----------
  const fast = await bubble('fast-chat', 'openai');
  const fastRow = await modelRow('fast-chat');
  const taken = fastRow.candidates.map((c) => `${c.group_id}|${c.remote_model}`);
  // 挑一个**确实还没挂上**的：跑第二遍时有些上游已经在链上了，不能拿它验「多了一条」
  const chip = await evaluate(`(() => {
    const taken = ${JSON.stringify(taken)};
    const c = [...document.querySelectorAll('.cv-chip')].find((el) => {
      if (el.disabled || el.dataset.proto !== 'openai') return false;
      return !taken.includes(el.dataset.gid + '|' + el.dataset.remote);
    });
    if (!c) return null;
    c.scrollIntoView({ block: 'center' });
    const b = c.getBoundingClientRect();
    return { x: b.left + b.width / 2, y: b.top + b.height / 2, remote: c.textContent.trim() };
  })()`);
  ok('上游池里找到还没挂过的模型', Boolean(chip), JSON.stringify(taken));
  if (chip) {
    const candsBefore = fastRow.candidates.length;
    await drag(chip, { x: fast.cx, y: fast.cy }, 18);
    await sleep(1100);
    const candsAfter = (await modelRow('fast-chat')).candidates.length;
    ok(`拖上去之后多了一条候选（${chip.remote}）`, candsAfter === candsBefore + 1,
      `${candsBefore} -> ${candsAfter}`);

    // 同一个再拖一次不该重复挂上（后端 409，前端先说清楚）
    await drag(chip, { x: fast.cx, y: fast.cy }, 18);
    await sleep(1100);
    ok('同一个上游拖第二次不会重复挂',
      (await modelRow('fast-chat')).candidates.length === candsAfter);
  }
  await shot('canvas-04-attached');

  // ---------- 7. 挪一个气泡 -> 坐标落库 -> 刷新后位置保持 ----------
  // 注意：整页的滚动位置会影响 viewport 坐标（前面的 scrollIntoView 可能把页面滚下去了），
  // 所以这里一律换算回**世界坐标**再比，不拿屏幕坐标直接比
  const worldOf = (model, proto) => evaluate(`(() => {
    const el = document.querySelector('.cv-bub[data-model="${model}"][data-proto="${proto}"]');
    const host = document.getElementById('cv-host').getBoundingClientRect();
    const t = getComputedStyle(document.getElementById('cv-world')).transform;
    const m = t.match(/matrix\\(([^)]+)\\)/);
    const [z, , , , tx, ty] = m ? m[1].split(',').map(Number) : [1, 0, 0, 1, 0, 0];
    const b = el.getBoundingClientRect();
    return {
      x: ((b.left + b.width / 2) - host.left - tx) / z,
      y: ((b.top + b.height / 2) - host.top - ty) / z,
    };
  })()`);

  const wideBefore = await worldOf('wide-chat', 'openai');
  const wideNow = await bubble('wide-chat', 'openai');
  const shift = { x: 120, y: 90 };
  await drag({ x: wideNow.cx, y: wideNow.cy },
    { x: wideNow.cx + shift.x, y: wideNow.cy + shift.y }, 12);
  await sleep(1100);          // 等自动保存那 600ms 的防抖

  const saved = await api('/admin/api/canvas-layout');
  const keys = Object.keys(saved.nodes);
  ok('画布坐标已持久化', keys.length >= 7, `键数=${keys.length}`);
  ok('下游和上游的键都在',
    keys.some((k) => k.startsWith('m|')) && keys.some((k) => k.startsWith('u|')));

  const wideAfter = await worldOf('wide-chat', 'openai');
  const zoomNow = saved.view.z;
  ok('挪动真的改了世界坐标（按屏幕位移换算）',
    Math.abs(wideAfter.x - wideBefore.x - shift.x / zoomNow) < 6
    && Math.abs(wideAfter.y - wideBefore.y - shift.y / zoomNow) < 6,
    `${JSON.stringify(wideBefore)} -> ${JSON.stringify(wideAfter)} z=${zoomNow}`);

  await send('Page.reload');
  await sleep(2800);
  await evaluate(`new Promise((r) => { let n = 0; const t = setInterval(() => {
    if (document.querySelectorAll('.cv-bub').length || n++ > 60) { clearInterval(t); r(1); } }, 200); })`);
  const wideBack = await worldOf('wide-chat', 'openai');
  ok('刷新后位置保持（世界坐标一致）',
    Math.abs(wideBack.x - wideAfter.x) < 2 && Math.abs(wideBack.y - wideAfter.y) < 2,
    `${JSON.stringify(wideAfter)} -> ${JSON.stringify(wideBack)}`);
  const viewBack = await api('/admin/api/canvas-layout');
  ok('刷新后视野也保持（没有重新自动框）',
    Math.abs(viewBack.view.z - saved.view.z) < 0.001,
    `${saved.view.z} -> ${viewBack.view.z}`);
  await shot('canvas-07-after-reload');

  // ---------- 9. 菜单开得出来 ----------
  const menuItems = await evaluate(`(() => {
    const el = document.querySelector('.cv-bub[data-model="my-chat"][data-proto="openai"]');
    const box = el.getBoundingClientRect();
    el.dispatchEvent(new MouseEvent('contextmenu', { bubbles: true,
      clientX: box.left + box.width / 2, clientY: box.top + box.height / 2 }));
    const m = document.getElementById('cv-menu');
    return { hidden: m.hidden, items: [...m.querySelectorAll('button')].map((b) => b.textContent.trim()) };
  })()`);
  ok('气泡右键菜单打得开', !menuItems.hidden, JSON.stringify(menuItems.items));
  ok('菜单里有「整条链交给另一个模型」',
    menuItems.items.some((t) => t.includes('整条链交给')));
  await shot('canvas-05-menu');

  // 角标是同一个菜单的另一个入口（右键不好发现，这个看得见）
  const tagMenu = await evaluate(`(() => {
    document.getElementById('cv-menu').hidden = true;
    const tag = document.querySelector('.cv-bub[data-model="my-chat"][data-proto="openai"] .cv-tag');
    const b = tag.getBoundingClientRect();
    tag.dispatchEvent(new MouseEvent('click', { bubbles: true, cancelable: true,
      clientX: b.left + b.width / 2, clientY: b.top + b.height / 2 }));
    const m = document.getElementById('cv-menu');
    return { hidden: m.hidden, items: [...m.querySelectorAll('button')].map((x) => x.textContent.trim()) };
  })()`);
  ok('点接口角标也能打开同一个菜单', !tagMenu.hidden, JSON.stringify(tagMenu.items));

  // 端口的菜单走右键
  const portMenu = await evaluate(`(() => {
    document.getElementById('cv-menu').hidden = true;
    const port = document.querySelector('.cv-bub[data-model="my-chat"][data-proto="openai"] .cv-port:not(.is-on)');
    const b = port.getBoundingClientRect();
    port.dispatchEvent(new MouseEvent('contextmenu', { bubbles: true,
      clientX: b.left + b.width / 2, clientY: b.top + b.height / 2 }));
    const m = document.getElementById('cv-menu');
    return { hidden: m.hidden, items: [...m.querySelectorAll('button')].map((x) => x.textContent.trim()) };
  })()`);
  ok('端口右键菜单打得开', !portMenu.hidden, JSON.stringify(portMenu.items));
  ok('端口菜单里有「移除这条候选」',
    portMenu.items.some((t) => t.includes('移除')));

  // ---------- 10. 交接弹窗 ----------
  const fwdDialog = await evaluate(`(() => {
    // 上一步把菜单停在了端口那个上，先自己把气泡菜单打开
    document.getElementById('cv-menu').hidden = true;
    const tag = document.querySelector('.cv-bub[data-model="my-chat"][data-proto="openai"] .cv-tag');
    const b = tag.getBoundingClientRect();
    tag.dispatchEvent(new MouseEvent('click', { bubbles: true, cancelable: true,
      clientX: b.left + b.width / 2, clientY: b.top + b.height / 2 }));
    const btn = [...document.querySelectorAll('#cv-menu button')]
      .find((x) => x.textContent.includes('整条链交给'));
    if (!btn) return null;
    btn.click();
    const dlg = document.getElementById('cv-fwd-dialog');
    return { open: dlg.open,
      options: [...document.getElementById('cv-fwd-target').options].map((o) => o.value) };
  })()`);
  await sleep(400);
  ok('交接弹窗打开了', fwdDialog && fwdDialog.open);
  ok('目标候选里排除了自己',
    fwdDialog && !fwdDialog.options.includes('my-chat'), JSON.stringify(fwdDialog && fwdDialog.options));
  await shot('canvas-06-forward-dialog');
  await evaluate(`document.getElementById('cv-fwd-dialog').close()`);

  // ---------- 11. 缩放与平移 ----------
  const hostBox = await evaluate(`(() => {
    const b = document.getElementById('cv-host').getBoundingClientRect();
    return { x: b.left + b.width / 2, y: b.top + b.height / 2 };
  })()`);
  const zoomBefore = (await api('/admin/api/canvas-layout')).view.z;
  await send('Input.dispatchMouseEvent',
    { type: 'mouseWheel', x: hostBox.x, y: hostBox.y, deltaX: 0, deltaY: -120 });
  await sleep(800);
  const zoomAfter = await api('/admin/api/canvas-layout');
  ok('滚轮能缩放', zoomAfter.view.z > zoomBefore, `${zoomBefore} -> ${zoomAfter.view.z}`);

  // ---------- 12. 恢复干净状态，出首页那张图 ----------
  // 前面留了个开着的菜单、还被拖过一颗气泡，直接截会很乱：先清掉布局让它重新自动排列
  await evaluate(`document.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true }))`);
  await fetch(`${APP}/admin/api/canvas-layout`, {
    method: 'PUT',
    headers: { 'content-type': 'application/json' },
    body: JSON.stringify({ nodes: {}, view: { x: 0, y: 0, z: 1 } }),
  });
  await send('Page.reload');
  await sleep(2900);
  await evaluate(`new Promise((r) => { let n = 0; const t = setInterval(() => {
    if (document.querySelectorAll('.cv-bub').length || n++ > 60) { clearInterval(t); r(1); } }, 200); })`);

  // 「适配」会把上游那几列也框进来，缩到 0.45 谁都看不清。首页要的是「一眼看懂环和指针」，
  // 所以自己算一个视野：对准 my-chat 放到 0.95，直接用公开接口写进去
  const geo = await evaluate(`(() => {
    const host = document.getElementById('cv-host').getBoundingClientRect();
    return { w: host.width, h: host.height };
  })()`);
  const auto = await api('/admin/api/canvas-layout');
  const [mx, myc] = auto.nodes['m|my-chat|openai'];
  const Z = 0.95;
  await fetch(`${APP}/admin/api/canvas-layout`, {
    method: 'PUT',
    headers: { 'content-type': 'application/json' },
    body: JSON.stringify({
      nodes: auto.nodes,
      view: { x: Math.round(geo.w * 0.44 - mx * Z), y: Math.round(geo.h * 0.46 - myc * Z), z: Z },
    }),
  });
  await send('Page.reload');
  await sleep(2900);
  await evaluate(`new Promise((r) => { let n = 0; const t = setInterval(() => {
    if (document.querySelectorAll('.cv-bub').length || n++ > 60) { clearInterval(t); r(1); } }, 200); })`);
  await sleep(500);
  await readmeShot();

  // 全程没抛过未捕获异常才算干净。放在最后断言，前面所有步骤的报错都会累到这里
  ok('全程没有 JS 未捕获错误', consoleErrors.length === 0, consoleErrors.slice(0, 3).join(' | '));

  console.log(`\n${failures ? `${failures} 项失败` : '全部通过'}`);
} catch (e) {
  console.error('验收脚本自身出错：', e.message);
  failures += 1;
} finally {
  try { ws && ws.close(); } catch { /* 忽略 */ }
  chrome.kill();
  process.exit(failures ? 1 : 0);
}
