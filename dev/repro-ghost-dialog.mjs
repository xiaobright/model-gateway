// 复现「关闭弹窗后底部出现幽灵窗口」：打开若干个弹窗→关闭→滚动到页面底部，
// 检查 DOM 里还有哪些 dialog[open]、以及页面底部是否出现异常元素。
import { spawn } from 'node:child_process';
import { writeFileSync } from 'node:fs';
import { setTimeout as sleep } from 'node:timers/promises';

const PORT = process.argv[2] || '8317';
const APP = `http://127.0.0.1:${PORT}`;
const CHROME = process.env.CHROME || 'C:/Program Files/Google/Chrome/Application/chrome.exe';
const DEBUG_PORT = 9225;

const chrome = spawn(CHROME, [
  '--headless=new', `--remote-debugging-port=${DEBUG_PORT}`,
  '--user-data-dir=' + process.env.TEMP + '/mg-ghost',
  '--no-first-run', '--no-default-browser-check',
  '--window-size=1280,800', 'about:blank',
], { stdio: 'ignore' });

let ws; const pending = new Map(); let nextId = 1;
const send = (m, p = {}) => new Promise((res, rej) => {
  const id = nextId++; pending.set(id, { res, rej });
  ws.send(JSON.stringify({ id, method: m, params: p }));
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
  throw new Error('连不上');
}
const ev = async (e) => {
  const r = await send('Runtime.evaluate',
    { expression: e, returnByValue: true, awaitPromise: true });
  if (r.exceptionDetails) throw new Error(r.exceptionDetails.exception?.description);
  return r.result.value;
};
const shot = async (n) => {
  const { data } = await send('Page.captureScreenshot', { format: 'png' });
  writeFileSync(`dev/${n}.png`, Buffer.from(data, 'base64'));
  console.log('已保存 dev/' + n + '.png');
};

try {
  await connect();
  await send('Page.enable'); await send('Runtime.enable');
  await send('Page.navigate', { url: `${APP}/` });
  await sleep(2800);
  await ev(`new Promise(r=>{let n=0;const t=setInterval(()=>{
    if(document.querySelectorAll('#upstream-body tr').length>0||n++>40){clearInterval(t);r(1);}},200);})`);

  // 复现用户操作：打开分组编辑 → 打开下游接入 → 弹确认框 → 逐个关闭
  const trace = [];
  const step = async (label, expr, delay = 700) => {
    const v = await ev(expr);
    await sleep(delay);
    trace.push([label, v]);
    console.log(label, '=>', JSON.stringify(v));
  };

  // 1) 打开分组编辑（真实入口）
  await step('打开分组编辑', `(async()=>{
    const tg=[...document.querySelectorAll('#upstream-body [data-act="toggle-groups"]')];
    if(tg.length) tg[0].click();
    await new Promise(r=>setTimeout(r,400));
    const b=[...document.querySelectorAll('[data-act="edit-group"]')];
    if(b.length) b[0].click();
    await new Promise(r=>setTimeout(r,300));
    return [...document.querySelectorAll('dialog[open]')].map(d=>d.id);
  })()`);

  // 2) 叠开下游接入
  await step('再开下游接入', `(async()=>{
    document.querySelector('[data-act="show-access"]').click();
    await new Promise(r=>setTimeout(r,300));
    return [...document.querySelectorAll('dialog[open]')].map(d=>d.id);
  })()`);
  await shot('ghost-01-two-dialogs');

  // 3) 关闭下游接入
  await step('关闭下游接入', `(async()=>{
    const d=document.getElementById('access-dialog');
    d.querySelector('[data-act="close-dialog"]').click();
    await new Promise(r=>setTimeout(r,400));
    return [...document.querySelectorAll('dialog[open]')].map(d=>d.id);
  })()`);

  // 4) 关闭分组编辑
  await step('关闭分组编辑', `(async()=>{
    const d=document.getElementById('group-dialog');
    const x=d.querySelector('[data-act="close-dialog"]');
    if(x) x.click(); else d.close();
    await new Promise(r=>setTimeout(r,400));
    return [...document.querySelectorAll('dialog[open]')].map(d=>d.id);
  })()`);

  // 5) 全关后滚动到页面最底部
  const afterClose = await ev(`(async()=>{
    const info = {};
    info.openDialogs = [...document.querySelectorAll('dialog[open]')].map(d=>d.id);
    // 所有 dialog 的几何：即使没 open，也量一下有没有占位
    info.allDialogs = [...document.querySelectorAll('dialog')].map(d=>{
      const r = d.getBoundingClientRect();
      const cs = getComputedStyle(d);
      return { id:d.id, open:d.open, display:cs.display, position:cs.position,
               top:Math.round(r.top), bottom:Math.round(r.bottom),
               w:Math.round(r.width), h:Math.round(r.height),
               visibility:cs.visibility, opacity:cs.opacity };
    });
    info.docH = document.documentElement.scrollHeight;
    info.bodyH = document.body.scrollHeight;
    info.winH = window.innerHeight;
    // 页面里有没有非 dialog 的、跑到文档底部的异常元素
    info.bodyChildren = [...document.body.children].map(c=>{
      const r=c.getBoundingClientRect();
      return { tag:c.tagName, cls:c.className, top:Math.round(r.top), h:Math.round(r.height) };
    });
    window.scrollTo(0, document.documentElement.scrollHeight);
    await new Promise(r=>setTimeout(r,300));
    info.scrollY = window.scrollY;
    return info;
  })()`);
  console.log('=== 全关后状态 ===');
  console.log(JSON.stringify(afterClose, null, 2));
  await shot('ghost-02-after-close-bottom');

  // 6) 再往下滚，看有没有幽灵
  await ev(`window.scrollTo(0, document.documentElement.scrollHeight + 200)`);
  await sleep(400);
  await shot('ghost-03-bottom-extra');
} catch (e) {
  console.error('出错：', e.message);
  process.exitCode = 2;
} finally {
  try { ws && ws.close(); } catch {}
  chrome.kill();
}
