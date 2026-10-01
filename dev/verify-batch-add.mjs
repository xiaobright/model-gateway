// 验证「批量添加模型」在真实页面上跑得通：
//   1. 扫描结果按「完全同名默认勾上 / 部分匹配默认不勾」渲染；
//   2. 提交之后上游目录和模型路由里都真的有那些候选；
//   3. 再点一次是幂等的（不会多出一条候选）。
// 只对**临时起的那个实例**做写操作：建一个上游、用完把它删掉（候选随分组级联下线）。
// 用法：node dev/verify-batch-add.mjs [port]
import { spawn } from 'node:child_process';
import { writeFileSync } from 'node:fs';
import { setTimeout as sleep } from 'node:timers/promises';

const PORT = process.argv[2] || '8399';
const APP = `http://127.0.0.1:${PORT}`;
const CHROME = process.env.CHROME || 'C:/Program Files/Google/Chrome/Application/chrome.exe';
const DEBUG_PORT = 9231;

const chrome = spawn(CHROME, [
  '--headless=new', `--remote-debugging-port=${DEBUG_PORT}`,
  '--user-data-dir=' + process.env.TEMP + '/mg-batch',
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
const UP_NAME = '__batch-verify';
let upstreamId = null;
const MODEL = 'gpt-5.6-luna';

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
  await api('POST', `/admin/api/groups/${gPartial}/models`, { model_names: [`${MODEL}-preview-2026`] });
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
    const btn = document.querySelector('[data-act="batch-add"]');
    if (!btn) return 'no-button';
    btn.click();
    return document.getElementById('batch-dialog').open;
  })()`);
  check('模型路由页有「批量添加模型」按钮并打开弹窗', opened === true, String(opened));

  // 扫描真的会去问所有站，慢站要等；只等我们关心的那两行出现
  await ev(`(() => { document.getElementById('batch-model').value = ${JSON.stringify(MODEL)}; })()`);
  await ev(`document.querySelector('[data-act="batch-scan"]').click()`);
  const waited = await ev(`new Promise(r => {
    let n = 0;
    const t = setInterval(() => {
      const rows = [...document.querySelectorAll('#batch-picker label.pick-row')];
      const hit = rows.some((l) => l.textContent.includes('partial'));
      if (hit || n++ > 300) { clearInterval(t); r({ rows: rows.length, waiting: n >= 300 }); }
    }, 200);
  })`);
  console.log('扫描等待结果:', JSON.stringify(waited));

  const dom = await ev(`(() => {
    const rows = [...document.querySelectorAll('#batch-picker label.pick-row')].map((l) => ({
      name: l.querySelector('.pick-name').textContent,
      level: l.querySelector('.tag').textContent,
      where: l.querySelector('.pick-where').textContent.trim(),
      checked: l.querySelector('input').checked,
    }));
    return {
      rows,
      seps: [...document.querySelectorAll('#batch-picker .pick-sep')].map((s) => s.textContent.trim()),
      // 每一段里第一条的等级，以及这一段有没有完全同名的行
      sectionFirstLevels: (() => {
        const out = [];
        for (const node of document.getElementById('batch-picker').children) {
          if (node.classList.contains('pick-sep')) {
            out.push({ sep: node.textContent.trim(), first: '', firstPartial: false, hasExact: false });
          } else if (node.classList.contains('pick-row') && out.length) {
            const last = out[out.length - 1];
            const partial = node.classList.contains('is-partial');
            if (!last.first) { last.first = node.querySelector('.tag').textContent; last.firstPartial = partial; }
            if (!partial) last.hasExact = true;
          }
        }
        return out;
      })(),
      status: document.getElementById('batch-status').textContent,
      count: document.getElementById('batch-count').textContent,
      save: document.getElementById('batch-save').textContent,
      saveDisabled: document.getElementById('batch-save').disabled,
      notes: [...document.querySelectorAll('#batch-picker .pick-note')].map((n) => n.textContent.trim()),
    };
  })()`);
  console.log('界面上的行:', JSON.stringify(dom.rows));
  console.log('段:', JSON.stringify(dom.sectionFirstLevels));
  console.log('状态:', dom.status, '|', dom.count, '|', dom.save);

  const exact = dom.rows.filter((r) => r.level.startsWith('完全') || r.level.includes('同名'));
  const partial = dom.rows.filter((r) => r.level.includes('部分'));
  check('完全同名的行默认勾上', exact.length > 0 && exact.every((r) => r.checked),
    JSON.stringify(exact.map((r) => [r.name, r.checked])));
  check('部分匹配的行默认不勾', partial.length > 0 && partial.every((r) => !r.checked),
    JSON.stringify(partial.map((r) => [r.name, r.checked])));
  check('同一段里部分匹配不排在完全同名之前',
    dom.sectionFirstLevels.every((s) => !(s.firstPartial && s.hasExact)),
    JSON.stringify(dom.sectionFirstLevels.map((s) => [s.sep, s.first])));
  const withExact = dom.sectionFirstLevels.filter((s) => s.hasExact).map((s) => s.sep);
  const withoutExact = dom.sectionFirstLevels.filter((s) => s.first && !s.hasExact).map((s) => s.sep);
  check('只有部分匹配的段排在有完全同名的段之后',
    !withoutExact.length || dom.sectionFirstLevels.findIndex((s) => s.sep === withoutExact[0])
      > dom.sectionFirstLevels.findIndex((s) => s.sep === withExact[withExact.length - 1]),
    JSON.stringify({ withExact, withoutExact }));
  check('按协议分段展示', dom.seps.some((s) => s.includes('Responses')) && dom.seps.some((s) => s.includes('Messages')),
    JSON.stringify(dom.seps));
  await shot('batch-01-scan');

  // 提交：只提交默认勾上的那些（界面上就是这个状态）
  const before = await api('GET', '/admin/api/models');
  const beforeCount = (before.find((r) => r.model_name === MODEL)?.candidates || []).length;
  await ev(`document.getElementById('batch-save').click()`);
  const done = await ev(`new Promise(r => {
    let n = 0;
    const t = setInterval(() => {
      const hint = document.getElementById('batch-hint').textContent;
      // 成功之后按钮会因为有内容而重新校验文字，所以等的是结果说明而不是禁用状态
      if (/已加 \\d+ 条候选|这次没有新增/.test(hint)) { clearInterval(t); r(hint); }
      else if (n++ > 150) { clearInterval(t); r('超时：' + hint); }
    }, 200);
  })`);
  // 结果说明是在 commitBatch 收尾之前写的：等按钮的状态稳定下来再量，别量到中间态
  await sleep(700);
  console.log('提交结果:', done);
  check('提交后给出结果说明', /已加 \d+ 条候选|这次没有新增/.test(done), done);
  const afterDom = await ev(`(() => {
    const b = document.getElementById('batch-save');
    return { disabled: b.disabled, text: b.textContent, opacity: getComputedStyle(b).opacity,
             rows: document.querySelectorAll('#batch-picker input[data-act="batch-pick"]').length,
             added: [...document.querySelectorAll('#batch-picker label.pick-row.busy')].length,
             picked: [...document.querySelectorAll('#batch-picker input[data-act="batch-pick"]')]
               .filter((i) => i.checked || i.disabled).length,
             stillPickable: [...document.querySelectorAll('#batch-picker input[data-act="batch-pick"]')]
               .filter((i) => !i.disabled && i.checked).length };
  })()`);
  console.log('提交后的按钮:', JSON.stringify(afterDom));
  check('加过的行置灰、按钮也置灰不再重复提交',
    afterDom.disabled === true && afterDom.added === afterDom.picked && afterDom.stillPickable === 0,
    JSON.stringify(afterDom));
  await shot('batch-02-added');

  const after = await api('GET', '/admin/api/models');
  const row = after.find((r) => r.model_name === MODEL);
  const cands = row ? row.candidates : [];
  check('模型路由里真的多出了这个模型的候选', cands.length > beforeCount,
    `${beforeCount} -> ${cands.length}`);
  const upstreamDetail = (await api('GET', '/admin/api/upstreams')).find((u) => u.id === upstreamId);
  const cat = upstreamDetail.groups.find((g) => g.id === gExact).models;
  check('上游模型目录里也登记上了', cat.includes(MODEL), JSON.stringify(cat));

  // 幂等：界面上那批已经标记为「加过」，再点一次不该再发出重复候选
  const again = await api('POST', '/admin/api/models/batch', {
    model_name: MODEL, groups: cands.map((c) => ({ group_id: c.group_id, remote_model: c.remote_model })),
  });
  check('重复提交是幂等的', again.committed === 0 && again.skipped.length === cands.length,
    JSON.stringify({ committed: again.committed, skipped: again.skipped }));
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
