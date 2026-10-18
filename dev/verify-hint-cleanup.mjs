// 验收「小字清理」：常驻说明 → 标题/标签旁的悬停问号（?），删掉的段落不留排版坑。
// 用法：先起隔离实例（.venv/Scripts/python.exe dev/preview-workbench.py --port 8341），再跑本脚本。
import { spawn } from 'node:child_process';
import { writeFileSync } from 'node:fs';
import { setTimeout as sleep } from 'node:timers/promises';

const PORT = process.argv[2] || '8341';
const APP = `http://127.0.0.1:${PORT}`;
const CHROME = process.env.CHROME || 'C:/Program Files/Google/Chrome/Application/chrome.exe';
const DEBUG_PORT = 9226;

const chrome = spawn(CHROME, [
  '--headless=new', `--remote-debugging-port=${DEBUG_PORT}`,
  '--user-data-dir=' + process.env.TEMP + '/mg-hint-q',
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
const check = (n, ok, d) => { results.push(ok); console.log(`${ok ? 'PASS' : 'FAIL'}  ${n}${d ? '  — ' + d : ''}`); };
const showView = (v) => evaluate(`document.querySelector('[data-view="${v}"]')?.classList.contains('is-on')
  ? 1 : (document.querySelector('.nav-item[data-view="${v}"]').click(), 1)`);

try {
  await connect();
  await send('Page.enable'); await send('Runtime.enable');
  await send('Page.navigate', { url: `${APP}/` });
  await sleep(2600);
  await evaluate(`new Promise(r=>{let n=0;const t=setInterval(()=>{
    if(document.querySelectorAll('#route-list .route-row, #route-list > *').length>0||n++>40){clearInterval(t);r(1);}},200);})`);

  // ---- 0. 结构不变量：舞台里只住 .view；设置页专属卡片不许掉到视图外 ----
  // 真实事故：设置页某张卡片多一个 </div>，把 .cols 和 </section> 提前吞掉，
  // 后面两张卡掉出 #view-stage，于是每个视图底部都渲染一遍。
  const stageKids = await evaluate(`[...document.getElementById('view-stage').children]
    .map(el => el.tagName.toLowerCase() + '.' + (el.className || '(无类名)').split(' ')[0])`);
  check('视图舞台里只有 .view 段（没有掉出视图的卡片）',
    stageKids.length > 0 && stageKids.every((k) => k.startsWith('section.view')),
    stageKids.join(' | '));
  const leaked = await evaluate(`(() => {
    const names = ['联网搜索上游', '请求文本替换'];
    const out = [];
    for (const card of document.querySelectorAll('.card')) {
      const h = (card.querySelector('.card-head h2')?.textContent || '').trim();
      if (names.includes(h) && !card.closest('.view[data-view="settings"]')) out.push(h);
    }
    return out;
  })()`);
  check('设置页专属卡片不出现在别的视图', leaked.length === 0, `泄漏=[${leaked.join(', ')}]`);
  const settingsCards = await evaluate(`[...document.querySelectorAll('.view[data-view="settings"] .card-head h2')].map(h => h.textContent.trim())`);
  check('设置页五张卡片齐全', settingsCards.length === 5,
    `[${settingsCards.join('、')}]`);

  // ---- 1. 静态问号就位、title 都非空 ----
  const qs = await evaluate(`(() => {
    const out = [];
    for (const q of document.querySelectorAll('.hint-q')) {
      const head = q.closest('.card-head, .dlg-head, .view-head, .dlg-section, .appearance-row, .log-filters, .field-row, .dlg-foot, .field');
      out.push({
        id: q.id || '',
        title: q.title || '',
        where: head ? (head.className.split(' ')[0] + ':' + (head.querySelector('h2, .appearance-label, .dlg-section')?.textContent?.trim().slice(0, 8) || '')) : '?',
        inOpenDialog: Boolean(q.closest('dialog[open]')),
      });
    }
    return out;
  })()`);
  // style-q / up-egress-q 的 title 由 JS 在渲染/开弹窗时写，启动瞬间为空是正常的
  const dynamicIds = ['style-q', 'up-egress-q'];
  const emptyTitle = qs.filter((q) => !q.title && !dynamicIds.includes(q.id));
  check('所有静态问号都带 title', emptyTitle.length === 0, `共 ${qs.length} 个，空 title ${emptyTitle.length} 个`);
  const styleQ = await evaluate(`document.getElementById('style-q').title`);
  const styleExpect = await evaluate(`(async () => {
    const { STYLES } = await import('/static/theme.js');
    const cur = document.documentElement.dataset.style || STYLES[0].id;
    return STYLES.find((s) => s.id === cur)?.hint || '';
  })()`);
  check('风格问号启动后拿到当前风格说明', styleQ === styleExpect && Boolean(styleQ), styleQ ? styleQ.slice(0, 18) + '…' : '空');

  // ---- 2. 各视图不再有常驻说明段落 ----
  const hints = await evaluate(`(() => {
    const vis = (el) => el && !el.closest('[hidden]') && el.offsetParent !== null;
    const seen = [];
    for (const p of document.querySelectorAll('.card-hint')) {
      if (vis(p) && p.textContent.trim()) seen.push(p.textContent.trim().slice(0, 24));
    }
    return seen;
  })()`);
  check('界面里没有常驻的 card-hint 小字', hints.length === 0, `残留=[${hints.join(' | ')}]`);

  // 实时请求 / 上游站点 / 转发记录：问号在标题旁，正文直接接内容
  await showView('live');
  await sleep(500);
  const liveQ = await evaluate(`(() => {
    const h = document.querySelector('.view[data-view="live"] .view-head');
    const q = h.querySelector('.hint-q');
    return { ok: Boolean(q), title: q?.title || '', gap: q ? Math.round(q.getBoundingClientRect().left - h.querySelector('h2').getBoundingClientRect().right) : -1 };
  })()`);
  check('实时请求：标题旁问号就位', liveQ.ok && liveQ.title.includes('90 秒'), `title=${liveQ.title.slice(0, 16)}…`);
  await shot('hint-01-live');

  await showView('upstreams');
  await sleep(500);
  const upQ = await evaluate(`(() => {
    const h = document.querySelector('.view[data-view="upstreams"] .view-head');
    const q = h.querySelector('.hint-q');
    const card = document.querySelector('.view[data-view="upstreams"] .card');
    return { ok: Boolean(q), title: q?.title || '', cardPadTop: card ? getComputedStyle(card).paddingTop : '' };
  })()`);
  check('上游站点：标题旁问号就位', upQ.ok && upQ.title.includes('分组保存接口'), `title=${upQ.title.slice(0, 16)}…`);
  await shot('hint-02-upstreams');

  await showView('log');
  await sleep(400);
  const logQ = await evaluate(`document.querySelector('.log-filters .hint-q')?.title || ''`);
  check('转发记录：筛选行尾是问号不是小字', logQ.includes('已经加载'), logQ.slice(0, 18));
  await showView('routes');

  // ---- 3. 设置页：外观卡片只剩三行，切风格问号跟着换说明 ----
  await showView('settings');
  await sleep(500);
  const appearance = await evaluate(`(() => {
    const rows = document.querySelectorAll('.view[data-view="settings"] .appearance-row').length;
    const q = document.getElementById('style-q');
    const items = document.querySelectorAll('#seg-style .seg-item').length;
    return { rows, items, title: q.title };
  })()`);
  check('外观卡片：三行设置、无说明段落', appearance.rows === 3, `rows=${appearance.rows}`);
  // 目前只登记了一个风格，切无可切；title 与登记表一致即接线正确（动态更新由单测盯）
  check('风格问号说明与登记表一致', appearance.title === styleExpect, `title=${appearance.title.slice(0, 14)}…`);
  await sleep(700);
  await shot('hint-03-settings');

  // ---- 4. 悬停问号有视觉反馈（颜色变主题色）----
  const hover = await evaluate(`(async () => {
    const q = document.querySelector('.view[data-view="settings"] .card-head .hint-q');
    const r = q.getBoundingClientRect();
    const before = getComputedStyle(q).color;
    const box = await new Promise((res) => {
      const cdpl = [];
      res(cdpl);
    });
    return { before, x: r.left + r.width / 2, y: r.top + r.height / 2 };
  })()`);
  await send('Input.dispatchMouseEvent', { type: 'mouseMoved', x: hover.x, y: hover.y });
  await sleep(350);
  const hoverAfter = await evaluate(`getComputedStyle(document.querySelector('.view[data-view="settings"] .card-head .hint-q')).color`);
  check('问号悬停变色（hover 有反馈）', hover.before !== hoverAfter, `${hover.before} → ${hoverAfter}`);

  // ---- 5. 供应商弹窗：出口问号随选择换说明、高级区无废话段、底部是问号 ----
  await showView('upstreams');
  await sleep(400);
  await evaluate(`document.querySelector('[data-act="new-upstream"]').click()`);
  await sleep(800);
  const sup = await evaluate(`(() => {
    const q = document.getElementById('up-egress-q');
    const t0 = q.title;
    document.getElementById('up-egress-kind').value = 'direct';
    document.getElementById('up-egress-kind').dispatchEvent(new Event('change', { bubbles: true }));
    const t1 = q.title;
    document.getElementById('up-egress-kind').value = '';
    document.getElementById('up-egress-kind').dispatchEvent(new Event('change', { bubbles: true }));
    const footQ = document.querySelector('#up-dialog .dlg-foot .hint-q');
    const retryAdv = document.getElementById('up-retry-adv');
    const holdAdv = document.getElementById('up-hold-adv');
    retryAdv.open = true; holdAdv.open = true;
    const noise = [...retryAdv.querySelectorAll('.dim'), ...holdAdv.querySelectorAll('.dim')]
      .filter((el) => el.textContent.trim() && el.id !== 'up-egress-hint').length;
    return { t0: t0.slice(0, 12), t1: t1.slice(0, 12), changed: t0 !== t1, footOk: Boolean(footQ?.title), noise };
  })()`);
  check('出口问号：默认/直连两份说明切换', sup.changed && sup.t1.includes('不走任何代理'), `${sup.t0}… → ${sup.t1}…`);
  check('同站重试 / 200 截断：展开后无解释性小字', sup.noise === 0, `dim 说明块=${sup.noise}`);
  check('供应商弹窗底部：保存按钮旁是问号', sup.footOk);
  await sleep(500);
  await shot('hint-04-supplier');

  // 探测结果仍写进行内提示（问号只扛静态说明）
  await evaluate(`document.querySelector('#up-dialog [data-act="close-dialog"]').click()`);
  await sleep(500);

  // ---- 6. 分组弹窗：模型目录小节带问号，底部无小字 ----
  await evaluate(`document.querySelector('[data-act="edit-upstream"]').click()`);
  await sleep(900);
  await evaluate(`document.querySelector('#up-groups [data-act="edit-group"]').click()`);
  await sleep(900);
  const grp = await evaluate(`(() => {
    const q = document.getElementById('grp-catalog-q');
    const vis = !document.getElementById('grp-import').hidden;
    const foot = document.getElementById('group-dialog').querySelector('.dlg-foot');
    return { title: q?.title || '', vis, footDim: foot.querySelector('.dim') ? 1 : 0,
      bodyOverflow: (() => { const b = document.getElementById('group-dialog').querySelector('.dlg-body');
        return b.scrollWidth - b.clientWidth; })() };
  })()`);
  check('分组弹窗：模型目录问号就位且目录已展开', grp.vis && grp.title.includes('立刻生效'), `title=${grp.title.slice(0, 14)}…`);
  check('分组弹窗：底部无「此按钮仅保存」小字', grp.footDim === 0);
  check('分组弹窗：无横向溢出', grp.bodyOverflow <= 1, `overflow=${grp.bodyOverflow}px`);
  await sleep(500);
  await shot('hint-05-group');

  // ---- 7. 搜索上游 / 转发弹窗：开场白进问号 ----
  await evaluate(`document.querySelector('#group-dialog [data-act="close-dialog"]').click()`);
  await sleep(400);
  await evaluate(`document.querySelector('#up-dialog [data-act="close-dialog"]').click()`);
  await sleep(400);
  await evaluate(`document.querySelector('[data-act="open-search"]').click()`);
  await sleep(800);
  const sr = await evaluate(`(() => {
    const d = document.getElementById('search-dialog');
    const q = d.querySelector('.dlg-head .hint-q');
    const bodyHint = d.querySelectorAll('.dlg-body .card-hint').length;
    const b = d.querySelector('.dlg-body');
    return { title: q?.title || '', bodyHint, overflow: b.scrollWidth - b.clientWidth };
  })()`);
  check('搜索上游：问号在标题旁、正文无开场白', sr.title.includes('alpha/search') && sr.bodyHint === 0, `title=${sr.title.slice(0, 16)}…`);
  check('搜索上游：无横向溢出', sr.overflow <= 1, `overflow=${sr.overflow}px`);
  await shot('hint-06-search');
  await evaluate(`document.querySelector('#search-dialog [data-act="close-dialog"]').click()`);
  await sleep(400);

  await showView('routes');
  await sleep(600);
  await evaluate(`document.querySelector('#route-list .route-row, #route-list > *')?.click()`);
  await sleep(700);
  const fwd = await evaluate(`(async () => {
    const btn = document.querySelector('[data-act="fwd-open"]');
    if (!btn) return { noBtn: true };
    btn.click();
    await new Promise(r => setTimeout(r, 700));
    const d = document.getElementById('fwd-dialog');
    const q = d.querySelector('.dlg-head .hint-q');
    const dims = [...d.querySelectorAll('.dlg-body .dim')].filter((el) => el.textContent.trim()).length;
    const b = d.querySelector('.dlg-body');
    return { title: q?.title || '', dims, overflow: b.scrollWidth - b.clientWidth };
  })()`);
  check('转发弹窗：问号在标题旁、尾部说明段已收', !fwd.noBtn && fwd.title.includes('同接口') && fwd.dims === 0,
    JSON.stringify(fwd).slice(0, 90));
  check('转发弹窗：无横向溢出', fwd.overflow <= 1, `overflow=${fwd.overflow}px`);
  await shot('hint-07-forward');
  await evaluate(`document.querySelector('#fwd-dialog [data-act="close-dialog"]').click()`);
  await sleep(400);

  // ---- 8. 收尾：没有残留弹窗、文档高度正常 ----
  const leftover = await evaluate(`[...document.querySelectorAll('dialog[open]')].map(d => d.id)`);
  check('操作结束后没有残留的弹窗', leftover.length === 0, `残留=[${leftover.join(', ')}]`);

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
