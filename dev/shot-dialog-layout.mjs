// 打开分组弹窗并截图，人工确认视觉效果。
// 用法：node dev/shot-dialog-layout.mjs [port]
import { spawn } from 'node:child_process';
import { writeFileSync } from 'node:fs';
import { setTimeout as sleep } from 'node:timers/promises';

const PORT = process.argv[2] || '8317';
const APP = `http://127.0.0.1:${PORT}`;
const CHROME = process.env.CHROME || 'C:/Program Files/Google/Chrome/Application/chrome.exe';
const DEBUG_PORT = 9223;

const chrome = spawn(CHROME, [
  '--headless=new', `--remote-debugging-port=${DEBUG_PORT}`,
  '--user-data-dir=' + process.env.TEMP + '/mg-shot-profile',
  '--no-first-run', '--no-default-browser-check',
  '--window-size=1280,900', '--force-device-scale-factor=1.5',
  'about:blank',
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
  const file = `dev/${name}.png`;
  writeFileSync(file, Buffer.from(data, 'base64'));
  console.log('已保存', file);
};

try {
  await connect();
  await send('Page.enable'); await send('Runtime.enable');
  await send('Page.navigate', { url: `${APP}/` });
  await sleep(2800);
  await evaluate(`new Promise(r => { let n=0; const t=setInterval(()=>{
    if (document.querySelectorAll('#upstream-body tr').length>0||n++>40){clearInterval(t);r(1);} },200); })`);

  // 用真实的「编辑分组」入口打开弹窗，验证打开时的自动滚动是否生效
  const opened = await evaluate(`(async () => {
    // 先展开一个供应商行，露出它下面的分组「编辑」按钮
    const toggles = [...document.querySelectorAll('#upstream-body [data-act="toggle-groups"]')];
    if (toggles.length) toggles[0].click();
    await new Promise(r => setTimeout(r, 400));
    const btns = [...document.querySelectorAll('[data-act="edit-group"]')];
    if (btns.length) { btns[0].click(); return btns.length; }
    return 0;
  })()`);
  console.log('点开的分组编辑按钮数：', opened);
  await sleep(1200);
  await shot('fix-01-group-dialog-open');

  // 再滚到底截一张，确认全部模型都能看到
  await evaluate(`(() => { const b=document.querySelector('#group-dialog .dlg-body');
    if (b) b.scrollTop = b.scrollHeight; })()`);
  await sleep(500);
  await shot('fix-02-group-dialog-scrolled');

  await evaluate(`document.getElementById('group-dialog').close()`);
  await sleep(300);

  // 确认弹窗（问题 2 的关不掉的窗口）
  await evaluate(`(() => {
    const dlg = document.getElementById('confirm-dialog');
    document.getElementById('cf-title').textContent = '删除供应商';
    document.getElementById('cf-body').innerHTML =
      '<p>要删掉供应商 <b>测试</b>、它下面的 3 个分组，以及 12 条模型候选。</p>';
    dlg.showModal();
  })()`);
  await sleep(500);
  await shot('fix-03-confirm-dialog');
} finally {
  try { ws && ws.close(); } catch {}
  chrome.kill();
}
