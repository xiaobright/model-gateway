// 验证「新增模型」里合并进来的「在各站查找」在真实页面上跑得通：
//   1. 弹窗默认停在查找；两段切换会把表单换过去换回来；
//   2. 扫描结果按「完全同名默认勾上 / 部分匹配默认不勾」渲染、按接口分段；
//   3. 提交之后上游目录和模型路由里都真的有那些候选，弹窗收起；
//   4. 再开一次，同一批命中会标明「已暴露给下游」。
// 只对**临时起的那个实例**做写操作：建一个上游、用完把它删掉（候选随分组级联下线）。
// 用法：node dev/verify-scan-add.mjs [port]
import { spawn } from 'node:child_process';
import { writeFileSync } from 'node:fs';
import { setTimeout as sleep } from 'node:timers/promises';

const PORT = process.argv[2] || '8399';
const APP = `http://127.0.0.1:${PORT}`;
const CHROME = process.env.CHROME || 'C:/Program Files/Google/Chrome/Application/chrome.exe';
const DEBUG_PORT = 9231;

const chrome = spawn(CHROME, [
  '--headless=new', `--remote-debugging-port=${DEBUG_PORT}`,
  '--user-data-dir=' + process.env.TEMP + '/mg-scan-add',
  '--no-first-run', '--no-default-browser-check',
  '--window-size=1400,900', 'about:blank',
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
const ev = async (expr) => {
  const r = await send('Runtime.evaluate', { expression: expr, returnByValue: true, awaitPromise: true });
  if (r.exceptionDetails) throw new Error(r.exceptionDetails.exception?.description || 'eval 失败');
  return r.result.value;
};
const shot = async (name) => {
  const { data } = await send('Page.captureScreenshot', { format: 'png' });
  writeFileSync(`dev/${name}.png`, Buffer.from(data, 'base64'));
  console.log('已保存 dev/' + name + '.png');
};
const results = [];
const check = (name, ok, detail) => {
  results.push(ok);
  console.log(`${ok ? 'PASS' : 'FAIL'}  ${name}${detail ? '  — ' + detail : ''}`);
};

const api = async (method, path, body) => {
  const resp = await fetch(APP + path, {
    method, headers: body ? { 'Content-Type': 'application/json' } : {},
    body: body ? JSON.stringify(body) : undefined,
  });
  const text = await resp.text();
  if (!resp.ok) throw new Error(`${method} ${path} -> ${resp.status} ${text.slice(0, 300)}`);
  return text ? JSON.parse(text) : null;
};

// 一个只活在这次验证里的上游。它不需要真的能连上：扫描出来的名字来自本地目录，
// 「拉取失败」那条路径本来就允许目录里的名字参与匹配。
// 模型名故意带 __verify 后缀：预览库里已经有好几个站登记了 gpt-5.6-luna，
// 用通用的名字会把这些站一起扫进来，就分不清命中的是哪个站了。
const UP_NAME = '__scan-verify';
let upstreamId = null;
const MODEL = 'gpt-5.6-__verify';
const PARTIAL = `${MODEL}-preview-2026`;

const typeModel = (name) => ev(`(() => {
  const el = document.getElementById('rt-model');
  el.value = ${JSON.stringify(name)};
  el.dispatchEvent(new Event('input', { bubbles: true }));
})()`);

// 扫描真的会去问所有站，慢站要等；只等我们关心的那两行出现
const waitRows = () => ev(`new Promise(r => {
  let n = 0;
  const t = setInterval(() => {
    const rows = [...document.querySelectorAll('#rt-picker label.pick-row')];
    const hit = rows.some((l) => l.textContent.includes('preview-2026'));
    if (hit || n++ > 300) { clearInterval(t); r({ rows: rows.length, waiting: n >= 300 }); }
  }, 200);
})`);

const readPicks = () => ev(`(() => {
  const rows = [...document.querySelectorAll('#rt-picker label.pick-row')].map((l) => ({
    name: l.querySelector('.pick-name').textContent,
    level: l.querySelector('.tag').textContent,
    where: l.querySelector('.pick-where').textContent.trim(),
    checked: l.querySelector('input').checked,
  }));
  return {
    rows,
    seps: [...document.querySelectorAll('#rt-picker .pick-sep')].map((s) => s.textContent.trim()),
    status: document.getElementById('rt-scan-status').textContent,
    count: document.getElementById('rt-count').textContent,
    save: document.getElementById('rt-save').textContent,
    saveDisabled: document.getElementById('rt-save').disabled,
    notes: [...document.querySelectorAll('#rt-picker .pick-note')].map((n) => n.textContent.trim()),
  };
})()`);

try {
  const made = await api('POST', '/admin/api/upstreams', {
    name: UP_NAME, base_url: `http://127.0.0.1:${PORT}`, enabled: true,
  });
  upstreamId = made.id;
  const gExact = made.groups.length ? made.groups[0].id
    : (await api('POST', `/admin/api/upstreams/${upstreamId}/groups`,
      { name: '默认', protocol: 'openai', api_key: 'verify-key' })).id;
  const gPartial = (await api('POST', `/admin/api/upstreams/${upstreamId}/groups`,
    { name: 'partial', protocol: 'anthropic', api_key: 'verify-key' })).id;
  // 完全同名的一条 + 只沾一部分的一条
  await api('POST', `/admin/api/groups/${gExact}/models`, { model_names: [MODEL] });
  await api('POST', `/admin/api/groups/${gPartial}/models`, { model_names: [PARTIAL] });
  console.log(`临时上游已建：id=${upstreamId}，分组 ${gExact} / ${gPartial}`);

  await connect();
  await send('Page.enable'); await send('Runtime.enable');
  await send('Page.navigate', { url: `${APP}/` });
  await sleep(2500);
  // 等首屏配置到位
  await ev(`new Promise(r=>{let n=0;const t=setInterval(()=>{
    if(document.querySelectorAll('#route-list .route, #route-list > *').length || n++ > 40){
      clearInterval(t); r(1);}},200);})`);

  const opened = await ev(`(() => {
    const btn = document.querySelector('[data-act="new-route"]');
    if (!btn) return 'no-button';
    btn.click();
    return {
      open: document.getElementById('route-dialog').open,
      scan: !document.getElementById('rt-scan').hidden,
      manual: !document.getElementById('rt-manual').hidden,
      mode: !document.getElementById('rt-mode').hidden,
      save: document.getElementById('rt-save').textContent,
      wide: document.getElementById('route-dialog').classList.contains('wide'),
    };
  })()`);
  console.log('打开弹窗:', JSON.stringify(opened));
  check('顶栏只剩「新增模型」，点开默认落在「在各站查找」',
    opened && opened.open && opened.scan && !opened.manual && opened.mode && opened.wide,
    JSON.stringify(opened));

  // 两段切换
  await ev(`document.querySelector('[data-act="route-mode"][data-mode="manual"]').click()`);
  const manual = await ev(`(() => ({
    scan: document.getElementById('rt-scan').hidden,
    manual: document.getElementById('rt-manual').hidden,
    save: document.getElementById('rt-save').textContent,
    wide: document.getElementById('route-dialog').classList.contains('wide'),
  }))()`);
  check('切到「手动指定」露出原来的表单，弹窗收回窄尺寸',
    manual.scan === true && manual.manual === false && manual.save === '添加' && manual.wide === false,
    JSON.stringify(manual));

  // 宽度必须是弹过去的：切回查找时逐帧采样，得看到中间值
  await sleep(450);
  const settledManual = await ev(`Math.round(document.getElementById('route-dialog').getBoundingClientRect().width)`);
  const grow = await ev(`new Promise(r => {
    const dlg = document.getElementById('route-dialog');
    const samples = [];
    const start = performance.now();
    document.querySelector('[data-act="route-mode"][data-mode="scan"]').click();
    const step = () => {
      samples.push({ w: Math.round(dlg.getBoundingClientRect().width),
                     morphing: dlg.classList.contains('is-morphing') });
      if (performance.now() - start < 700) requestAnimationFrame(step);
      else r({ samples, final: Math.round(dlg.getBoundingClientRect().width),
               morphing: dlg.classList.contains('is-morphing') });
    };
    requestAnimationFrame(step);
  })`);
  const widths = [...new Set(grow.samples.map((s) => s.w))];
  check('切回「在各站查找」时框是弹过去的，不是瞬移',
    settledManual <= 430 && grow.final >= 700
      && widths.some((w) => w > settledManual + 20 && w < grow.final - 20)
      && grow.samples.some((s) => s.morphing) && grow.morphing === false,
    JSON.stringify({ settledManual, final: grow.final, widths: widths.slice(0, 10) }));

  // 查不到时的出口：结果说明里那个「切到手动指定」要真能点
  await typeModel('__verify-nothing-here');
  await ev(`document.querySelector('[data-act="rt-scan"]').click()`);
  await ev(`new Promise(r => {
    let n = 0;
    const t = setInterval(() => {
      const note = document.querySelector('#rt-picker .pick-note');
      if ((note && /都没有这个名字/.test(note.textContent)) || n++ > 200) { clearInterval(t); r(1); }
    }, 200);
  })`);
  await shot('scan-00-empty');
  const escape = await ev(`(() => {
    const b = document.querySelector('#rt-picker [data-act="route-mode"]');
    if (!b) return null;
    const text = b.textContent.trim();
    b.click();
    return { text, scan: document.getElementById('rt-scan').hidden,
             manual: document.getElementById('rt-manual').hidden };
  })()`);  check('查不到时给一个能点的「切到手动指定」出口',
    escape && escape.text === '切到手动指定' && escape.scan === true && escape.manual === false,
    JSON.stringify(escape));
  await ev(`document.querySelector('[data-act="route-mode"][data-mode="scan"]').click()`);

  await typeModel(MODEL);
  await ev(`document.querySelector('[data-act="rt-scan"]').click()`);
  console.log('扫描等待结果:', JSON.stringify(await waitRows()));

  const dom = await readPicks();
  console.log('界面上的行:', JSON.stringify(dom.rows));
  console.log('状态:', dom.status, '|', dom.count, '|', dom.save);

  const exact = dom.rows.filter((r) => r.level.startsWith('完全') || r.level.includes('同名'));
  const partial = dom.rows.filter((r) => r.level.includes('部分'));
  check('完全同名的行默认勾上', exact.length > 0 && exact.every((r) => r.checked),
    JSON.stringify(exact.map((r) => [r.name, r.checked])));
  check('部分匹配的行默认不勾', partial.length > 0 && partial.every((r) => !r.checked),
    JSON.stringify(partial.map((r) => [r.name, r.checked])));
  check('按协议分段展示', dom.seps.some((s) => s.includes('Responses')) && dom.seps.some((s) => s.includes('Messages')),
    JSON.stringify(dom.seps));
  check('底部说清勾了几条，按钮跟着数走',
    /共 \d+ 条候选，已勾 \d+ 条/.test(dom.count) && /添加选中的 \d+ 条/.test(dom.save) && dom.saveDisabled === false,
    `${dom.count} | ${dom.save} | disabled=${dom.saveDisabled}`);
  await shot('scan-01-results');

  // 提交：只提交默认勾上的那些（界面上就是这个状态）
  const before = await api('GET', '/admin/api/models');
  const beforeCount = (before.find((r) => r.model_name === MODEL)?.candidates || []).length;
  await ev(`document.getElementById('rt-save').click()`);
  const done = await ev(`new Promise(r => {
    let n = 0;
    const t = setInterval(() => {
      const dlg = document.getElementById('route-dialog');
      const hint = document.getElementById('rt-pick-hint').textContent;
      if (!dlg.open || /已加 \\d+ 条候选|这次没有新增/.test(hint)) {
        clearInterval(t); r({ open: dlg.open, hint });
      } else if (n++ > 150) { clearInterval(t); r({ open: dlg.open, hint: '超时：' + hint }); }
    }, 200);
  })`);
  console.log('提交结果:', JSON.stringify(done));
  check('全都加上了就把弹窗收掉', done.open === false, JSON.stringify(done));
  await shot('scan-02-after');

  const after = await api('GET', '/admin/api/models');
  const row = after.find((r) => r.model_name === MODEL);
  const cands = row ? row.candidates : [];
  check('模型路由里真的多出了这个模型的候选', cands.length > beforeCount,
    `${beforeCount} -> ${cands.length}`);
  const upstreamDetail = (await api('GET', '/admin/api/upstreams')).find((u) => u.id === upstreamId);
  const cat = upstreamDetail.groups.find((g) => g.id === gExact).models;
  check('上游模型目录里也登记上了', cat.includes(MODEL), JSON.stringify(cat));

  // 再开一次：同一批命中会标明「已经暴露给下游」，加是幂等的
  await ev(`document.querySelector('[data-act="new-route"]').click()`);
  await typeModel(MODEL);
  await ev(`document.querySelector('[data-act="rt-scan"]').click()`);
  await waitRows();
  const again = await readPicks();
  check('重开一次会标明「已暴露给下游」',
    again.rows.some((r) => r.where.includes('已暴露给下游')),
    JSON.stringify(again.rows.map((r) => r.where)));

  // 幂等：接口层再提交一次不该多出候选
  const dup = await api('POST', '/admin/api/models/batch', {
    model_name: MODEL, groups: cands.map((c) => ({ group_id: c.group_id, remote_model: c.remote_model })),
  });
  check('重复提交是幂等的', dup.committed === 0 && dup.skipped.length === cands.length,
    JSON.stringify({ committed: dup.committed, skipped: dup.skipped }));
  const finalRow = (await api('GET', '/admin/api/models')).find((r) => r.model_name === MODEL);
  check('候选数没有因为重复提交变多', finalRow.candidates.length === cands.length,
    `${cands.length} -> ${finalRow.candidates.length}`);

  const ok = results.filter(Boolean).length;
  console.log(`\n==== ${ok}/${results.length} 通过 ====`);
  process.exitCode = ok === results.length ? 0 : 1;
} catch (e) {
  console.error('出错：', e.message);
  process.exitCode = 2;
} finally {
  try { ws && ws.close(); } catch { /* 已经断了 */ }
  chrome.kill();
  if (upstreamId !== null) {
    try {
      await api('DELETE', `/admin/api/upstreams/${upstreamId}`);
      console.log(`临时上游 ${upstreamId} 已删除（候选随分组级联下线）`);
    } catch (e) {
      console.error('清理失败，请手工删掉上游', UP_NAME, e.message);
    }
  }
}
