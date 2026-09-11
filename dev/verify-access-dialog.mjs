// 验证「下游接入」弹窗与确认框的叠加/关闭行为，并截图。
import { spawn } from 'node:child_process';
import { writeFileSync } from 'node:fs';
import { setTimeout as sleep } from 'node:timers/promises';

const PORT = process.argv[2] || '8317';
const APP = `http://127.0.0.1:${PORT}`;
const CHROME = process.env.CHROME || 'C:/Program Files/Google/Chrome/Application/chrome.exe';
const DEBUG_PORT = 9224;

const chrome = spawn(CHROME, [
  '--headless=new', `--remote-debugging-port=${DEBUG_PORT}`,
  '--user-data-dir=' + process.env.TEMP + '/mg-shot2',
  '--no-first-run', '--no-default-browser-check',
  '--window-size=1280,900', 'about:blank',
], { stdio: 'ignore' });

let ws; const pending = new Map(); let nextId = 1;
const send = (method, params = {}) => new Promise((res, rej) => {
  const id = nextId++; pending.set(id, { res, rej });
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
        if (m.id && pending.has(m.id)) {
          const { res, rej } = pending.get(m.id); pending.delete(m.id);
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
  writeFileSync(`dev/${name}.png`, Buffer.from(data, 'base64'));
  console.log('已保存 dev/' + name + '.png');
};

const results = [];
const check = (n, ok, d) => { results.push(ok); console.log(`${ok?'PASS':'FAIL'}  ${n}${d?'  — '+d:''}`); };

try {
  await connect();
  await send('Page.enable'); await send('Runtime.enable');
  await send('Page.navigate', { url: `${APP}/` });
  await sleep(2800);
  await evaluate(`new Promise(r=>{let n=0;const t=setInterval(()=>{
    if(document.querySelectorAll('#upstream-body tr').length>0||n++>40){clearInterval(t);r(1);}},200);})`);

  // 打开「下游接入」弹窗（侧栏底部按钮）
  const openAccess = await evaluate(`(() => {
    const btn = document.querySelector('[data-act="show-access"]');
    if (!btn) return false;
    btn.click(); return true;
  })()`);
  check('找到并点击「下游接入」按钮', openAccess);
  await sleep(900);
  const accessOpen = await evaluate(
    `document.getElementById('access-dialog').open`);
  check('下游接入弹窗已打开', accessOpen);
  await shot('fix-04-access-dialog');

  // 关键：点关闭按钮必须能关掉
  const closed = await evaluate(`(async () => {
    const dlg = document.getElementById('access-dialog');
    const x = dlg.querySelector('[data-act="close-dialog"]');
    if (!x) return 'no-close-btn';
    x.click();
    await new Promise(r => setTimeout(r, 300));
    return dlg.open ? 'still-open' : 'closed';
  })()`);
  check('「下游接入」弹窗能正常关闭', closed === 'closed', `结果=${closed}`);

  // 在「下游接入」开着的情况下，叠一个确认框，再关确认框
  const overlay = await evaluate(`(async () => {
    const { confirmBox } = await import('/static/util.js?v=9');
    const p = confirmBox({ title: '删除供应商', body: '确认框叠在下游接入之上，测试层级与关闭。', ok: '删除' });
    await new Promise(r => setTimeout(r, 300));
    const cfd = document.getElementById('confirm-dialog');
    const acc = document.getElementById('access-dialog');
    const topmost = [...document.querySelectorAll('dialog[open]')].pop();
    const info = {
      confirmOpen: cfd.open,
      accessOpen: acc.open,
      topmost: topmost ? topmost.id : null,
    };
    // 关掉确认框
    cfd.querySelector('#cf-ok').click();
    const val = await p;
    await new Promise(r => setTimeout(r, 200));
    info.afterClose = cfd.open ? 'still-open' : 'closed';
    info.returnValue = val;
    return info;
  })()`);
  check('确认框能叠在其它弹窗之上', overlay.confirmOpen && overlay.topmost === 'confirm-dialog',
    JSON.stringify(overlay));
  check('确认框点「确定」能关掉并返回 ok',
    overlay.afterClose === 'closed' && overlay.returnValue === true,
    JSON.stringify(overlay));

  // 最终页面不应残留任何打开的弹窗
  const leftover = await evaluate(
    `[...document.querySelectorAll('dialog[open]')].map(d => d.id)`);
  check('操作结束后没有残留的弹窗', leftover.length === 0,
    `残留=[${leftover.join(', ')}]`);

  // 关键回归点：关闭的 <dialog> 必须彻底不参与布局。
  // 若给 `dialog` 直接设了 display:flex，会覆盖 UA 的 `dialog:not([open]){display:none}`，
  // 于是关掉的弹窗仍以盒子留在文档末尾、撑高页面并在底部「显形」（幽灵窗口）。
  const ghost = await evaluate(`(() => {
    const closed = [...document.querySelectorAll('dialog:not([open])')];
    const layout = closed.filter(d => {
      const cs = getComputedStyle(d);
      const r = d.getBoundingClientRect();
      return cs.display !== 'none' && (r.height > 0 || r.width > 0);
    }).map(d => {
      const r = d.getBoundingClientRect();
      return d.id + ':' + getComputedStyle(d).display + ':' + Math.round(r.height);
    });
    return {
      closedCount: closed.length,
      ghosts: layout,
      docH: document.documentElement.scrollHeight,
      bodyH: document.body.scrollHeight,
    };
  })()`);
  check('关闭的弹窗不占布局空间（无幽灵窗口）',
    ghost.ghosts.length === 0,
    ghost.ghosts.length ? `异常=${ghost.ghosts.join(', ')}` : `检查了 ${ghost.closedCount} 个已关闭弹窗`);
  check('文档高度不被已关闭弹窗撑高',
    ghost.docH <= ghost.bodyH + 2,
    `documentElement=${ghost.docH} vs body=${ghost.bodyH}`);

  const ok = results.filter(Boolean).length;
  console.log(`\n==== ${ok}/${results.length} 通过 ====`);
  process.exitCode = ok === results.length ? 0 : 1;
} catch (e) {
  console.error('出错：', e.message);
  process.exitCode = 2;
} finally {
  try { ws && ws.close(); } catch {}
  chrome.kill();
}
