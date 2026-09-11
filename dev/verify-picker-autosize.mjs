// 验证模型列表的自适应高度行为：
//   条目少 → 高度等于实际内容高度（下面不留空白）
//   条目多 → 长到上限后内部滚动
// 只请求已在运行的服务，不启动/停止任何服务进程。
import { spawn } from 'node:child_process';
import { writeFileSync } from 'node:fs';
import { setTimeout as sleep } from 'node:timers/promises';

const PORT = process.argv[2] || '8317';
const APP = `http://127.0.0.1:${PORT}`;
const CHROME = process.env.CHROME || 'C:/Program Files/Google/Chrome/Application/chrome.exe';
const DEBUG_PORT = 9226;

const chrome = spawn(CHROME, [
  '--headless=new', `--remote-debugging-port=${DEBUG_PORT}`,
  '--user-data-dir=' + process.env.TEMP + '/mg-autoh',
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
      ws.onmessage = (e) => {
        const m = JSON.parse(e.data);
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

const results = [];
const check = (n, ok, d) => { results.push(ok); console.log(`${ok?'PASS':'FAIL'}  ${n}${d?'  — '+d:''}`); };

try {
  await connect();
  await send('Page.enable'); await send('Runtime.enable');
  await send('Page.navigate', { url: `${APP}/` });
  await sleep(2800);
  await ev(`new Promise(r=>{let n=0;const t=setInterval(()=>{
    if(document.querySelectorAll('#upstream-body tr').length>0||n++>40){clearInterval(t);r(1);}},200);})`);

  // 打开分组编辑弹窗并注入指定条数的模型，测量列表高度
  const measure = async (count) => ev(`(async () => {
    const dlg = document.getElementById('group-dialog');
    if (!dlg.open) {
      document.getElementById('grp-import').hidden = false;
      dlg.showModal();
    }
    await new Promise(r => requestAnimationFrame(() => requestAnimationFrame(r)));
    const host = document.getElementById('grp-picker');
    host.innerHTML = '<div class="pick-sep">这个分组的上游模型 N 个</div>'
      + Array.from({length: ${count}}, (_, i) =>
        '<label><input type="checkbox"> model-' + String(i).padStart(3,'0') + '</label>').join('');
    await new Promise(r => requestAnimationFrame(() => requestAnimationFrame(r)));
    const r = host.getBoundingClientRect();
    const cs = getComputedStyle(host);
    // 测量「纯内容高度」：去掉 max-height 后 scrollHeight 就是自然高度
    const naturalH = host.scrollHeight;
    return {
      boxH: r.height, naturalH, clientH: host.clientHeight,
      scrollH: host.scrollHeight,
      maxH: cs.maxHeight, minH: cs.minHeight,
      scrollable: host.scrollHeight > host.clientHeight,
      dlgH: dlg.getBoundingClientRect().height,
      vh: window.innerHeight,
    };
  })()`);

  // 场景 A：条目少（3 条）→ 应贴合内容，下面不留空白
  const few = await measure(3);
  console.log('少数条目:', JSON.stringify(few));
  const fewNatural = await ev(`(() => {
    const host=document.getElementById('grp-picker');
    const before=host.style.maxHeight;
    host.style.maxHeight='none';
    const h=host.getBoundingClientRect().height;
    host.style.maxHeight=before;
    return h;
  })()`);
  check('条目少时高度贴合内容（不留空白）',
    Math.abs(few.boxH - fewNatural) <= 2,
    `实测 ${few.boxH.toFixed(0)}px vs 自然高 ${fewNatural.toFixed(0)}px`);
  check('条目少时不出现滚动条', !few.scrollable,
    `scrollH=${few.scrollH} clientH=${few.clientH}`);
  await shot('autoh-01-few');

  // 场景 B：条目多（60 条）→ 长到上限后滚动
  const many = await measure(60);
  console.log('大量条目:', JSON.stringify(many));
  check('条目多时被上限截住并出现滚动条',
    many.scrollable && many.boxH <= 370,
    `框高 ${many.boxH.toFixed(0)}px, scrollH=${many.scrollH} > clientH=${many.clientH}`);
  check('条目多时弹窗仍不超出视口',
    many.dlgH <= many.vh + 1,
    `弹窗高 ${many.dlgH.toFixed(0)} / 视口 ${many.vh}`);
  await shot('autoh-02-many');

  // 场景 C：滚到底最后一条可见
  const reach = await ev(`(() => {
    const host=document.getElementById('grp-picker');
    host.scrollTop = host.scrollHeight;
    const ls=host.querySelectorAll('label'); const last=ls[ls.length-1].getBoundingClientRect();
    const pr=host.getBoundingClientRect();
    return last.bottom <= pr.bottom+2 && last.top >= pr.top-2;
  })()`);
  check('条目多时能滚到最后一条', reach);

  // 场景 D：中等条目（8 条）→ 仍应贴合内容（未到上限）
  const mid = await measure(8);
  const midNatural = await ev(`(() => {
    const host=document.getElementById('grp-picker');
    const before=host.style.maxHeight; host.style.maxHeight='none';
    const h=host.getBoundingClientRect().height; host.style.maxHeight=before; return h;
  })()`);
  check('中等条目（未到上限）贴合内容',
    Math.abs(mid.boxH - midNatural) <= 2 && !mid.scrollable,
    `实测 ${mid.boxH.toFixed(0)} vs 自然 ${midNatural.toFixed(0)}, 可滚=${mid.scrollable}`);

  await ev(`document.getElementById('group-dialog').close()`);

  const ok = results.filter(Boolean).length;
  console.log(`\n==== ${ok}/${results.length} 通过 ====`);
  process.exitCode = ok === results.length ? 0 : 1;
} catch (e) {
  console.error('出错：', e.message);
  process.exitCode = 2;
} finally {
  try { ws && ws.close(); } catch {}
  chrome.kill();   // 只关我自己拉起的无头 Chrome，绝不动用户的服务
}
