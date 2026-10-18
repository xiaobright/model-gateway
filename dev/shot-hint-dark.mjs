// 一次性：深色模式下抽查问号与排版（设置页 + 供应商弹窗）。
import { spawn } from 'node:child_process';
import { writeFileSync } from 'node:fs';
import { setTimeout as sleep } from 'node:timers/promises';

const PORT = process.argv[2] || '8341';
const APP = `http://127.0.0.1:${PORT}`;
const CHROME = process.env.CHROME || 'C:/Program Files/Google/Chrome/Application/chrome.exe';
const DEBUG_PORT = 9227;
const chrome = spawn(CHROME, ['--headless=new', `--remote-debugging-port=${DEBUG_PORT}`,
  '--user-data-dir=' + process.env.TEMP + '/mg-hint-dark', '--no-first-run', '--no-default-browser-check',
  '--window-size=1280,900', 'about:blank'], { stdio: 'ignore' });
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
  const r = await send('Runtime.evaluate', { expression: expr, returnByValue: true, awaitPromise: true });
  if (r.exceptionDetails) throw new Error(r.exceptionDetails.exception?.description);
  return r.result.value;
};
const shot = async (name) => {
  const { data } = await send('Page.captureScreenshot', { format: 'png' });
  writeFileSync(`dev/${name}.png`, Buffer.from(data, 'base64'));
  console.log('已保存 dev/' + name + '.png');
};
try {
  await connect();
  await send('Page.enable'); await send('Runtime.enable');
  await send('Page.navigate', { url: `${APP}/` });
  await sleep(2600);
  await evaluate(`new Promise(r=>{let n=0;const t=setInterval(()=>{
    if(document.querySelectorAll('#route-list > *').length>0||n++>40){clearInterval(t);r(1);}},200);})`);
  // 切到深色：主题循环 auto → light → dark
  for (let i = 0; i < 3; i++) {
    const now = await evaluate(`document.documentElement.dataset.theme`);
    if (now === 'dark') break;
    await evaluate(`document.querySelector('[data-act="toggle-theme"]').click()`);
    await sleep(900);
  }
  const theme = await evaluate(`document.documentElement.dataset.theme`);
  console.log('当前主题：', theme);
  await evaluate(`document.querySelector('.nav-item[data-view="settings"]').click()`);
  await sleep(800);
  await shot('hint-08-settings-dark');
  await evaluate(`document.querySelector('.nav-item[data-view="upstreams"]').click()`);
  await sleep(500);
  await evaluate(`document.querySelector('[data-act="new-upstream"]').click()`);
  await sleep(900);
  await shot('hint-09-supplier-dark');
  await evaluate(`document.querySelector('#up-dialog [data-act="close-dialog"]').click()`);
  await sleep(400);
  const q = await evaluate(`(() => {
    const el = document.querySelector('.view[data-view="settings"] .card-head .hint-q');
    const cs = getComputedStyle(el);
    return { color: cs.color, border: cs.borderColor, w: el.offsetWidth, h: el.offsetHeight };
  })()`);
  console.log('深色下问号样式：', JSON.stringify(q));
} catch (e) {
  console.error('出错：', e.message);
  process.exitCode = 2;
} finally {
  try { ws && ws.close(); } catch {}
  chrome.kill();
}
