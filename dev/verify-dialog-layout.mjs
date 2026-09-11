// 验证弹窗布局修复：用 headless Chrome 打开控制台，测量弹窗与列表的几何，
// 确认「关不掉的窗口」和「滚不到底」两个问题都已解决。
// 不依赖任何 npm 包 —— Node 22 自带 WebSocket 和 fetch。
//
// 用法：node dev/verify-dialog-layout.mjs [port]

import { spawn } from 'node:child_process';
import { setTimeout as sleep } from 'node:timers/promises';

const PORT = process.argv[2] || '8317';
const APP = `http://127.0.0.1:${PORT}`;
const CHROME = process.env.CHROME
  || 'C:/Program Files/Google/Chrome/Application/chrome.exe';
const DEBUG_PORT = 9222;

const results = [];
const check = (name, ok, detail) => {
  results.push({ name, ok, detail });
  console.log(`${ok ? 'PASS' : 'FAIL'}  ${name}${detail ? `  — ${detail}` : ''}`);
};

const chrome = spawn(CHROME, [
  '--headless=new',
  `--remote-debugging-port=${DEBUG_PORT}`,
  '--user-data-dir=' + process.env.TEMP + '/mg-verify-profile',
  '--no-first-run', '--no-default-browser-check',
  '--window-size=1280,900',
  'about:blank',
], { stdio: 'ignore' });

let ws;
const pending = new Map();
let nextId = 1;

const send = (method, params = {}) => new Promise((resolve, reject) => {
  const id = nextId++;
  pending.set(id, { resolve, reject });
  ws.send(JSON.stringify({ id, method, params }));
});

async function connect() {
  for (let i = 0; i < 40; i++) {
    try {
      // 先确保有一个页面 target，再连它自己的 WebSocket（连着浏览器端点
      // 是发不出 Page.* 的，会报 'Page.enable' wasn't found）
      const list = await (await fetch(`http://127.0.0.1:${DEBUG_PORT}/json/list`)).json();
      let page = list.find((t) => t.type === 'page');
      if (!page) {
        await fetch(`http://127.0.0.1:${DEBUG_PORT}/json/new?about:blank`,
          { method: 'PUT' }).catch(() => {});
        const l2 = await (await fetch(`http://127.0.0.1:${DEBUG_PORT}/json/list`)).json();
        page = l2.find((t) => t.type === 'page');
      }
      if (!page || !page.webSocketDebuggerUrl) { await sleep(250); continue; }
      ws = new WebSocket(page.webSocketDebuggerUrl);
      await new Promise((res, rej) => {
        ws.onopen = res;
        ws.onerror = rej;
      });
      ws.onmessage = (ev) => {
        const msg = JSON.parse(ev.data);
        if (msg.id && pending.has(msg.id)) {
          const { resolve, reject } = pending.get(msg.id);
          pending.delete(msg.id);
          if (msg.error) reject(new Error(JSON.stringify(msg.error)));
          else resolve(msg.result);
        }
      };
      return;
    } catch { await sleep(250); }
  }
  throw new Error('无法连接 Chrome 调试端口');
}

const evaluate = async (expr) => {
  const r = await send('Runtime.evaluate', {
    expression: expr, returnByValue: true, awaitPromise: true,
  });
  if (r.exceptionDetails) {
    throw new Error(r.exceptionDetails.exception?.description || 'evaluate 失败');
  }
  return r.result.value;
};

try {
  await connect();
  await send('Page.enable');
  await send('Runtime.enable');

  await send('Page.navigate', { url: `${APP}/` });
  await sleep(2500);

  // 等应用脚本把数据拉回来
  await evaluate(`new Promise(r => {
    let n = 0;
    const t = setInterval(() => {
      if (document.querySelectorAll('#upstream-body tr').length > 0 || n++ > 40) {
        clearInterval(t); r(true);
      }
    }, 200);
  })`);

  const upstreamCount = await evaluate(
    `document.querySelectorAll('#upstream-body tr').length`);
  check('页面已加载出上游数据', upstreamCount > 0, `上游行数 ${upstreamCount}`);

  // ---- 问题 2：确认弹窗（无 form 包壳的结构）----
  // 直接构造一个确认框，模拟「删供应商」这类操作
  const confirmGeom = await evaluate(`(async () => {
    const dlg = document.getElementById('confirm-dialog');
    document.getElementById('cf-title').textContent = '删除供应商';
    document.getElementById('cf-body').innerHTML =
      '<p>要删掉供应商 <b>测试</b>、它下面的 3 个分组，以及 12 条模型候选。</p>'
      + '<p>在别的分组还有候选的模型会自动切到剩下的候选上。</p>'.repeat(6);
    dlg.showModal();
    await new Promise(r => requestAnimationFrame(() => requestAnimationFrame(r)));
    const dr = dlg.getBoundingClientRect();
    const foot = dlg.querySelector('.dlg-foot');
    const fr = foot.getBoundingClientRect();
    const okBtn = document.getElementById('cf-ok');
    const br = okBtn.getBoundingClientRect();
    const vh = window.innerHeight;
    return {
      dialogBottom: dr.bottom, dialogTop: dr.top, vh,
      footBottom: fr.bottom, footH: fr.height,
      okBtnBottom: br.bottom, okBtnH: br.height,
      dialogOverflow: getComputedStyle(dlg).overflow,
      dialogDisplay: getComputedStyle(dlg).display,
    };
  })()`);

  check('确认弹窗高度不超出视口',
    confirmGeom.dialogBottom <= confirmGeom.vh + 1,
    `弹窗底 ${confirmGeom.dialogBottom.toFixed(0)} / 视口高 ${confirmGeom.vh}`);
  check('确认弹窗底部（取消/确定）可见',
    confirmGeom.okBtnH > 0 && confirmGeom.okBtnBottom <= confirmGeom.vh + 1
      && confirmGeom.footH > 0,
    `确定按钮底 ${confirmGeom.okBtnBottom.toFixed(0)}, 高 ${confirmGeom.okBtnH.toFixed(0)}`);
  check('确认弹窗用 flex 列布局接管',
    confirmGeom.dialogDisplay === 'flex',
    `display=${confirmGeom.dialogDisplay}, overflow=${confirmGeom.dialogOverflow}`);

  // 确认弹窗正文能滚（内容超长时）
  const confirmScroll = await evaluate(`(() => {
    const body = document.querySelector('#confirm-dialog .dlg-body');
    return { sh: body.scrollHeight, ch: body.clientHeight,
             canScroll: body.scrollHeight > body.clientHeight,
             overflowY: getComputedStyle(body).overflowY };
  })()`);
  check('确认弹窗正文可滚动（长内容不撑破）',
    confirmScroll.overflowY === 'auto' || confirmScroll.overflowY === 'scroll',
    `scrollHeight=${confirmScroll.sh}, clientHeight=${confirmScroll.ch}`);

  await evaluate(`document.getElementById('confirm-dialog').close()`);

  // ---- 问题 1：分组弹窗里的上游模型列表 ----
  const pickerGeom = await evaluate(`(async () => {
    const dlg = document.getElementById('group-dialog');
    document.getElementById('grp-import').hidden = false;
    // 铺 40 条假模型，模拟「拉取列表后条目很多」
    const host = document.getElementById('grp-picker');
    host.innerHTML = Array.from({length: 40}, (_, i) =>
      '<label><input type="checkbox" data-act="pick-toggle"> model-' +
      String(i).padStart(2, '0') + '-a-fairly-long-model-identifier</label>').join('');
    dlg.showModal();
    await new Promise(r => requestAnimationFrame(() => requestAnimationFrame(r)));
    const p = host.getBoundingClientRect();
    const body = document.querySelector('#group-dialog .dlg-body');
    const br = body.getBoundingClientRect();
    const wrap = document.getElementById('grp-import');
    const wr = wrap.getBoundingClientRect();
    body.scrollTop = 0;
    const dlgRect = dlg.getBoundingClientRect();
    const cs = getComputedStyle(host);
    const wcs = getComputedStyle(wrap);
    return {
      pickerH: p.height, pickerTop: p.top, pickerBottom: p.bottom,
      bodyBottom: br.bottom, vh: window.innerHeight,
      dlgBottom: dlgRect.bottom, dlgTop: dlgRect.top,
      scrollH: host.scrollHeight, clientH: host.clientHeight,
      overflowY: cs.overflowY, minH: cs.minHeight, maxH: cs.maxHeight,
      wrapH: wr.height, wrapFlex: wcs.flex, wrapMinH: wcs.minHeight,
      bodyH: br.height, bodyScrollH: body.scrollHeight,
      bodyClientH: body.clientHeight, bodyOverflow: getComputedStyle(body).overflowY,
    };
  })()`);

  check('模型列表足够高，一屏能看到 6 行以上',
    pickerGeom.pickerH >= 260,
    `实测高 ${pickerGeom.pickerH.toFixed(0)}px (min ${pickerGeom.minH}, max ${pickerGeom.maxH})`);
  // 关键：列表可能比视口低，但整个弹窗不能越界，且正文必须能滚动，
  // 用户滚一下正文就能把列表带进视野 —— 这才是「够得到底下的模型」。
  check('分组弹窗整体不超出视口',
    pickerGeom.dlgBottom <= pickerGeom.vh + 1,
    `弹窗底 ${pickerGeom.dlgBottom.toFixed(0)} / 视口高 ${pickerGeom.vh}`);
  check('分组弹窗正文可滚动（能把列表滚进视野）',
    (pickerGeom.bodyOverflow === 'auto' || pickerGeom.bodyOverflow === 'scroll')
      && pickerGeom.bodyScrollH > pickerGeom.bodyClientH,
    `正文 overflowY=${pickerGeom.bodyOverflow}, 可见高 ${pickerGeom.bodyClientH.toFixed(0)} < 内容高 ${pickerGeom.bodyScrollH.toFixed(0)}`);

  // 真正模拟用户操作：把正文滚到底，列表应完整出现在视口内
  const reachable = await evaluate(`(() => {
    const body = document.querySelector('#group-dialog .dlg-body');
    body.scrollTop = body.scrollHeight;
    const host = document.getElementById('grp-picker');
    const pr = host.getBoundingClientRect();
    const head = document.querySelector('#group-dialog .dlg-head').getBoundingClientRect();
    return {
      listTop: pr.top, listBottom: pr.bottom,
      headBottom: head.bottom, vh: window.innerHeight,
      fullyVisible: pr.top >= head.bottom - 1 && pr.bottom <= window.innerHeight + 1,
    };
  })()`);
  check('正文滚到底后，模型列表完整可见（可操作）',
    reachable.fullyVisible,
    `列表 ${reachable.listTop.toFixed(0)}~${reachable.listBottom.toFixed(0)}, 头底 ${reachable.headBottom.toFixed(0)}, 视口 ${reachable.vh}`);

  // 模拟滚到底：最后一条应能进入可视区
  const canReachLast = await evaluate(`(() => {
    const host = document.getElementById('grp-picker');
    host.scrollTop = host.scrollHeight;
    const labels = host.querySelectorAll('label');
    const last = labels[labels.length - 1].getBoundingClientRect();
    const pr = host.getBoundingClientRect();
    return last.bottom <= pr.bottom + 2 && last.top >= pr.top - 2;
  })()`);
  check('能滚到列表最后一条并完整显示', canReachLast);

  // ---- 下游接入弹窗（也是无 form 壳的结构）----
  await evaluate(`document.getElementById('group-dialog').close()`);
  const accessGeom = await evaluate(`(async () => {
    const dlg = document.getElementById('access-dialog');
    document.getElementById('access-body').innerHTML =
      Array.from({length: 30}, (_, i) => '<p>接入说明第 ' + i + ' 行</p>').join('');
    dlg.showModal();
    await new Promise(r => requestAnimationFrame(() => requestAnimationFrame(r)));
    const dr = dlg.getBoundingClientRect();
    const body = dlg.querySelector('.dlg-body');
    const head = dlg.querySelector('.dlg-head');
    return { dlgBottom: dr.bottom, dlgTop: dr.top, vh: window.innerHeight,
             headH: head.getBoundingClientRect().height,
             bodyH: body.getBoundingClientRect().height,
             bodyScrollable: body.scrollHeight > body.clientHeight };
  })()`);
  check('下游接入弹窗不超出视口',
    accessGeom.dlgBottom <= accessGeom.vh + 1 && accessGeom.dlgTop >= -1,
    `顶 ${accessGeom.dlgTop.toFixed(0)} 底 ${accessGeom.dlgBottom.toFixed(0)} / 视口 ${accessGeom.vh}`);
  check('下游接入弹窗标题栏可见且正文可滚',
    accessGeom.headH > 0 && accessGeom.bodyScrollable,
    `头高 ${accessGeom.headH.toFixed(0)}, 正文可滚 ${accessGeom.bodyScrollable}`);

  await evaluate(`document.getElementById('access-dialog').close()`);

  const failed = results.filter((r) => !r.ok);
  console.log(`\n==== ${results.length - failed.length}/${results.length} 通过 ====`);
  process.exitCode = failed.length ? 1 : 0;
} catch (e) {
  console.error('验证脚本出错：', e.message);
  process.exitCode = 2;
} finally {
  try { ws && ws.close(); } catch {}
  chrome.kill();
}
