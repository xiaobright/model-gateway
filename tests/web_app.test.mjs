import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';
import vm from 'node:vm';

import * as util from '../web/util.js';
import * as groupEditor from '../web/group-editor.js';
import { createRefreshQueue } from '../web/async-state.js';

// 执行真实 app.js 的处理函数，只替换 DOM/网络和启动轮询的宿主。
// 不复制被测函数；Promise 由用例控制，关闭/重开等竞态不用真实网络或 sleep。
const source = readFileSync(new URL('../web/app.js', import.meta.url), 'utf8')
  .split('/* ---------------------------------------------------------------- 启动 */')[0]
  .replace(/^import [\s\S]*? from '[^']+';\r?\n/gm, '');
const tick = () => new Promise((resolve) => setImmediate(resolve));
const noop = () => {};
function deferred() {
  let resolve, reject;
  const promise = new Promise((yes, no) => { resolve = yes; reject = no; });
  return { promise, resolve, reject };
}

function harness(t) {
  util.setProtocolMetadata([
    { name: 'openai', label: 'Responses', path: '/v1/responses', client: 'test' },
    { name: 'openai-chat', label: 'Chat', path: '/v1/chat/completions', client: 'test' },
  ]);
  Object.assign(util.state, {
    upstreams: [], routes: [], stats: null, overview: null, editing: null,
    editingUp: null, editingGroup: null, editingCand: null, iface: '', filter: '',
    view: 'overview', protocolEnabled: {}, failover: { enabled: {}, breakers: [] },
  });
  groupEditor.invalidateGroupEdit();
  const elements = new Map();
  const element = (id) => {
    if (!elements.has(id)) {
      const listeners = new Map();
      elements.set(id, {
        value: '', textContent: '', innerHTML: '', checked: false, open: false,
        dataset: {}, style: {}, children: [], hidden: false, scrollTop: 0,
        classList: { add: noop, remove: noop, toggle: noop },
        querySelectorAll: () => [], focus: noop, scrollIntoView: noop,
        showModal() { this.open = true; },
        close() { this.open = false; this.dispatch('close'); },
        addEventListener(type, callback) { listeners.set(type, callback); },
        dispatch(type) { return listeners.get(type)?.({ target: this }); },
      });
    }
    return elements.get(id);
  };
  const h = { element, requests: [], refreshes: [], timers: [], toasts: [], blockRefresh: false };
  const oldFetch = globalThis.fetch;
  globalThis.fetch = async (path, options) => {
    const job = { ...deferred(), path, method: options.method, body: options.body && JSON.parse(options.body) };
    h.requests.push(job);
    return new Response(JSON.stringify(await job.promise), { status: 200 });
  };
  t.after(() => { globalThis.fetch = oldFetch; groupEditor.invalidateGroupEdit(); });
  const context = vm.createContext({
    ...util, ...groupEditor, createRefreshQueue, $: element,
    document: {
      addEventListener: noop, querySelectorAll: () => [], visibilityState: 'visible',
      querySelector: () => [...elements.values()].find((e) => e.open),
    },
    window: { addEventListener: noop }, localStorage: { getItem: () => null, setItem: noop },
    location: { origin: 'http://127.0.0.1', hash: '' }, history: { replaceState: noop },
    views: new Proxy({}, { get: () => noop }),
    // canvas.js 的行为在真实浏览器里验证（dev/check-canvas.mjs），这里只补 app.js
    // 求值阶段就要读到的出口 —— ACTIONS 里 spread 了 canvasActions，少了它整个模块都跑不起来
    canvasActions: {}, initCanvas: noop, renderCanvas: noop, renderPool: noop,
    setCanvasFilter: noop, setCanvasIface: noop, saveForward: async () => {},
    toast: (...args) => h.toasts.push(args), run: (_el, fn) => fn(), confirmBox: async () => true,
    requestAnimationFrame: (fn) => fn(), setInterval: (fn, ms) => h.timers.push({ fn, ms }),
    withViewTransition: (_dir, fn) => fn(), moveMarker: noop, reduceMotion: () => true,
    initSpotlightAndTilt: noop, refreshLightTargets: noop,
    refreshForTest: () => {
      const job = deferred();
      h.refreshes.push(job);
      if (!h.blockRefresh) job.resolve();
      return job.promise;
    },
  });
  vm.runInContext(source, context, { filename: 'web/app.js' });
  vm.runInContext('refreshConfig = refreshForTest;', context);
  h.app = vm.runInContext(`({
    openUpstream, saveUpstream, probeUpstream, openGroup, saveGroup, openRoute,
    fillRemoteList, saveRewrite, actions: ACTIONS, groupDirty,
    openBatchAdd, scanModels, commitBatch, syncBatchSelection,
    editingKey: () => editingKey
  })`, context);
  h.evaluate = (code) => vm.runInContext(code, context);
  h.latest = () => h.requests.at(-1);
  h.releaseRefreshes = () => h.refreshes.forEach((job) => job.resolve());
  h.fillUpstream = (name) => {
    element('up-name').value = name;
    element('up-base').value = `https://${name}.example`;
  };
  return h;
}

const provider = (id, groups = []) => ({
  id, name: `site-${id}`, base_url: `https://site-${id}.example`, enabled: true,
  egress_kind: 'system', groups, supports: ['openai'],
});
const group = (id) => ({ id, name: `group-${id}`, protocol: 'openai', enabled: true, upstream_id: 1, models: [] });

async function editGroup(h, id, key) {
  const opening = h.app.openGroup(1, id);
  h.latest().resolve({ api_key: key });
  await opening;
}

test('新建 A 的保存后刷新不能劫持重开的新建 B 会话', async (t) => {
  const h = harness(t);
  h.blockRefresh = true;
  await h.app.openUpstream(null);
  assert.equal(h.element('up-egress-kind').value, '', 'system 必须映射到真实的选项值');
  h.fillUpstream('A');
  const savingA = h.app.saveUpstream();
  h.latest().resolve({ id: 101, name: 'A' });
  await tick();
  assert.equal(h.refreshes.length, 1);
  h.element('up-dialog').close();
  await h.app.openUpstream(null);
  h.fillUpstream('B');
  h.releaseRefreshes();
  await savingA;
  assert.equal(util.state.editing, null);
  assert.equal(h.element('up-name').value, 'B');
  assert.equal(h.element('group-dialog').open, false);
  const savingB = h.app.saveUpstream();
  assert.equal(h.latest().method, 'POST');
  assert.equal(h.latest().path, '/admin/api/upstreams');
  assert.equal(h.latest().body.name, 'B');
  util.state.upstreams = [provider(202)];
  h.latest().resolve({ id: 202, name: 'B' });
  await tick();
  h.releaseRefreshes();
  await savingB;
  assert.equal(util.state.editing, 202);
  assert.equal(util.state.editingUp, 202);
});

test('等 VPS 预设时捕获表单快照，不读取随后改动的字段', async (t) => {
  const h = harness(t);
  await h.app.openUpstream(null);
  h.fillUpstream('original');
  h.element('up-egress-kind').value = 'vps';
  const saving = h.app.saveUpstream();
  h.fillUpstream('later-edit');
  h.latest().resolve({ vps: 'http://proxy.example:8080' });
  await tick();
  assert.equal(h.latest().body.name, 'original');
  assert.equal(h.latest().body.egress, 'http://proxy.example:8080');
  h.element('up-dialog').close();
  h.latest().resolve({ id: 1, name: 'original' });
  await saving;
});

test('等 VPS 预设时关闭重开，不给新会话发起旧保存', async (t) => {
  const h = harness(t);
  await h.app.openUpstream(null);
  h.fillUpstream('A');
  h.element('up-egress-kind').value = 'vps';
  const saving = h.app.saveUpstream();
  h.element('up-dialog').close();
  await h.app.openUpstream(null);
  h.fillUpstream('B');
  h.latest().resolve({ vps: 'http://proxy.example:8080' });
  await saving;
  assert.equal(h.requests.length, 1, '只有预设 GET，不能额外 POST/PUT');
});

test('同一供应商重复打开，出口旧响应不能覆盖新响应', async (t) => {
  const h = harness(t);
  util.state.upstreams = [{ ...provider(1), egress_kind: 'proxy' }];
  const first = h.app.openUpstream(1);
  const oldRequest = h.latest();
  const second = h.app.openUpstream(1);
  h.latest().resolve({ egress: 'http://new.example:8080' });
  await second;
  oldRequest.resolve({ egress: 'http://old.example:8080' });
  await first;
  assert.equal(h.element('up-egress-url').value, 'http://new.example:8080');
});

for (const fails of [false, true]) {
  test(`迟到的出口探测${fails ? '失败' : '成功'}不写新弹窗`, async (t) => {
    const h = harness(t);
    util.state.upstreams = [provider(1)];
    await h.app.openUpstream(1);
    const probing = h.app.probeUpstream();
    h.element('up-dialog').close();
    await h.app.openUpstream(null);
    const hint = h.element('up-egress-hint').textContent;
    if (fails) h.latest().reject(new Error('old error'));
    else h.latest().resolve({ results: [{ ok: true, status: 200, label: 'old', ms: 1 }] });
    await probing;
    assert.equal(h.element('up-egress-hint').textContent, hint);
    assert.equal(h.element('up-egress-hint').innerHTML, '');
  });
}

test('旧分组保存只失效旧缓存，不能改新分组的 Key 基线', async (t) => {
  const h = harness(t);
  util.state.upstreams = [provider(1, [group(21), group(22)])];
  const seed = groupEditor.pullRemoteModels(21, 'test-seed-21', () => true);
  h.latest().resolve({ models: ['old-key-model'] });
  await seed;
  await editGroup(h, 21, 'old-key');
  h.element('grp-key').value = 'changed-key';
  const saving = h.app.saveGroup();
  const saveRequest = h.latest();
  await editGroup(h, 22, 'key-B');
  saveRequest.resolve({});
  await saving;
  assert.equal(groupEditor.remoteModels(21), undefined, '切走也要废弃旧 Key 拉到的列表');
  assert.equal(h.app.editingKey(), 'key-B');
  assert.equal(h.element('grp-key').value, 'key-B');
  assert.equal(h.app.groupDirty(), false);
});

test('旧手动添加返回后，不清空新会话的输入框', async (t) => {
  const h = harness(t);
  util.state.upstreams = [provider(1, [group(31), group(32)])];
  await editGroup(h, 31, 'key-A');
  h.element('grp-manual').value = 'model-A';
  const adding = h.app.actions['manual-add']();
  const oldRequest = h.latest();
  await editGroup(h, 32, 'key-B');
  h.element('grp-manual').value = 'draft-B';
  oldRequest.resolve({ added: 1 });
  await adding;
  assert.equal(h.element('grp-manual').value, 'draft-B');
});

test('模型写入后的刷新晚到时，不重画新弹窗', async (t) => {
  const h = harness(t);
  util.state.upstreams = [provider(1, [group(41), group(42)])];
  await editGroup(h, 41, 'key-A');
  h.blockRefresh = true;
  h.evaluate('let pickerPaints = 0; renderPicker = () => { pickerPaints += 1; };');
  const label = { classList: { add: noop, remove: noop } };
  const adding = h.app.actions['pick-toggle']({ name: 'm' }, { checked: true, closest: () => label });
  h.latest().resolve({ added: 1 });
  await tick();
  assert.equal(h.refreshes.length, 1);
  await editGroup(h, 42, 'key-B');
  const paints = h.evaluate('pickerPaints');
  h.releaseRefreshes();
  await adding;
  assert.equal(h.evaluate('pickerPaints'), paints);
});

test('加候选只显示已登记模型，不请求上游，并将名称匹配项排前', async (t) => {
  const h = harness(t);
  const gid = 51;
  const g = { ...group(gid), models: ['zeta-model', 'gpt-6-astra-fast', 'alpha-model'] };
  util.state.upstreams = [
    { ...provider(2, [g]), name: 'Other provider' },
    { ...provider(1, [g]), name: 'AgentRouter' },
  ];
  h.element('rt-upstream').value = '1';
  h.element('rt-group').value = String(gid);
  h.app.openRoute('gpt-6-astra', null, 'openai');
  assert.equal(h.requests.length, 0, '打开候选弹窗不能请求 remote-models');
  assert.match(h.element('rt-remote-list').innerHTML, /gpt-6-astra-fast.*alpha-model.*zeta-model/);
  assert.match(h.element('rt-remote-hint').textContent, /已登记 3 个上游模型/);
  assert.equal(h.element('route-dialog').open, true);
});

test('按展开宽度预排整颗胶囊，不为所有胶囊留空', () => {
  const css = readFileSync(new URL('../web/style.css', import.meta.url), 'utf8');
  assert.match(css, /\.route-cands\s*\{[^}]*flex-direction:\s*column/s);
  assert.match(css, /\.route-cand-line\s*\{[^}]*flex-wrap:\s*wrap/s);
  assert.match(css, /\.chip-measure-natural,\s*\.chip-measure-expanded\s*\{[^}]*align-self:\s*flex-start/s,
    '测量时不能被纵向 flex 容器拉伸成整行宽度');
  assert.match(css, /\.chip-measure-expanded \.chip-e\s*\{[^}]*width:\s*20px/s);
  assert.doesNotMatch(css, /\.chip:hover \.chip-label[^}]*padding-right/s);
  assert.doesNotMatch(css, /\.chip-label\s*\{[^}]*padding-right:\s*48px/s);
});

test('整颗展开后放不下时预先移到下一行', (t) => {
  const h = harness(t);
  const views = readFileSync(new URL('../web/views.js', import.meta.url), 'utf8')
    .replace(/^import [\s\S]*? from '[^']+';\r?\n/gm, '')
    .replace(/^export /gm, '');
  h.evaluate(views);
  const rows = JSON.parse(h.evaluate(`JSON.stringify(planCandidateRows([
    { natural: 40, expanded: 55 },
    { natural: 35, expanded: 65 },
    { natural: 30, expanded: 45 },
  ], 110, 7))`));
  assert.deepEqual(rows, [[0], [1, 2]]);
});

test('旧 close 事件到达时，新分组仍在取 Key 也不能被作废', async (t) => {
  const h = harness(t);
  util.state.upstreams = [provider(1, [group(61), group(62)])];
  await editGroup(h, 61, 'key-A');
  h.element('group-dialog').open = false; // 原生 close 事件稍后才到
  const opening = h.app.openGroup(1, 62);
  h.element('group-dialog').dispatch('close');
  h.latest().resolve({ api_key: 'key-B' });
  await opening;
  assert.equal(h.element('group-dialog').open, true);
  assert.equal(util.state.editingGroup, 62);
  assert.equal(h.element('grp-key').value, 'key-B');
});

test('保存文本规则期间的新编辑不被旧回包抹掉', async (t) => {
  const h = harness(t);
  h.element('rewrite-rules').value = '[{"from":"old","to":"x"}]';
  const saving = h.app.saveRewrite();
  const later = '[{"from":"new","to":"y"}]';
  h.element('rewrite-rules').value = later;
  h.latest().resolve({ rules: [{ from: 'old', to: 'x' }] });
  await saving;
  assert.equal(h.element('rewrite-rules').value, later);
  assert.equal(h.element('rewrite-count').textContent, '未保存');
});

test('3 秒轮询只取 live，实时页不重复轮询 stats', async (t) => {
  const h = harness(t);
  const fast = h.timers.find((timer) => timer.ms === 3000);
  util.state.view = 'live';
  fast.fn();
  assert.equal(h.requests.length, 0);
  util.state.view = 'overview';
  fast.fn();
  assert.equal(h.latest().path, '/admin/api/stats?live_only=true');
  h.latest().resolve({ live: { requests: 2, streams: 1 } });
  await tick();
  assert.equal(util.state.stats.live.requests, 2);
});

test('路由次数按协议对应；热榜只裁展示，不裁其他模型的次数', (t) => {
  const h = harness(t);
  const views = readFileSync(new URL('../web/views.js', import.meta.url), 'utf8')
    .replace(/^import [\s\S]*? from '[^']+';\r?\n/gm, '')
    .replace(/^export /gm, '');
  h.evaluate(views);
  util.state.overview = { models: [
    { model: 'shared', protocol: 'openai', n: 3, p95: 10, bad: 0 },
    { model: 'shared', protocol: 'openai-chat', n: 1, p95: 10, bad: 0 },
    ...Array.from({ length: 10 }, (_, i) => ({ model: `other-${i}`, protocol: 'openai', n: 1, p95: 10, bad: 0 })),
  ] };
  util.state.routes = [
    ['shared', 'openai'], ['shared', 'openai-chat'], ['other-9', 'openai'],
  ].map(([model_name, protocol]) => ({
    model_name, protocol, candidates: [], active_route_id: null, preferred_route_id: null,
  }));
  h.evaluate('renderRoutes()');
  const rows = h.element('route-list').innerHTML.split('<div class="route" ').slice(1);
  assert.match(rows[0], /class="route-usage"[^>]*>3 次/);
  assert.match(rows[1], /class="route-usage"[^>]*>1 次/);
  assert.match(rows[2], /class="route-usage"[^>]*>1 次/);
  h.evaluate(`
    let hotRows = [];
    barRow = (data) => { hotRows.push(data); return { dataset: {} }; };
    document.createDocumentFragment = () => ({ append() {} });
    $('hot-list').append = () => {};
    enterStagger = () => {};
    renderHot();
  `);
  assert.equal(h.evaluate('hotRows.length'), 8);
  assert.equal(h.evaluate('hotRows[0].label'), 'shared · Responses');
  assert.equal(h.evaluate('hotRows[1].label'), 'shared · Chat');
});

/* ---------------------------------------------------------------- 批量添加模型 */

// 走 DOM 的那部分（勾选框是渲染出来的 HTML，测试宿主里没有真的 DOM）用一个
// 按 data 属性筛的最小实现顶上：验的是「哪些行默认勾上」「提交发的是什么」，
// 不是浏览器怎么画。
function stubBatchRows(h, rows) {
  const boxes = rows.map((r) => {
    const attrs = { gid: String(r.group_id), remote: r.remote, act: 'batch-pick', done: r.done };
    return {
      checked: r.checked, disabled: false,
      dataset: new Proxy(attrs, { set: (t, k, v) => { t[k] = v; return true; } }),
      closest: () => ({ classList: { add: noop, remove: noop, toggle: noop } }),
    };
  });
  h.element('batch-picker').querySelectorAll = (sel) => (sel.includes('batch-pick') ? boxes : []);
  return boxes;
}

const scanRow = (gid, protocol, matches, extra = {}) => ({
  group_id: gid, group_name: `group-${gid}`, upstream_name: `site-${gid}`,
  protocol, protocol_enabled: true, group_enabled: true, matches, error: '', ...extra,
});

test('扫描结果：完全同名的默认勾上，部分匹配不许默认勾', async (t) => {
  const h = harness(t);
  h.app.openBatchAdd();
  h.element('batch-model').value = 'gpt-test';
  const scanning = h.app.scanModels();
  h.latest().resolve({
    model_name: 'gpt-test', scanned: 3, matched: 3, failed: 0, ms: 12,
    groups: [
      scanRow(1, 'openai', [{ remote_model: 'gpt-test', level: 'exact', from_catalog: false }]),
      scanRow(2, 'anthropic', [
        { remote_model: 'gpt-testing-preview', level: 'partial', from_catalog: false }]),
      scanRow(3, 'openai-chat', [
        { remote_model: 'gpt-test', level: 'exact', from_catalog: true }]),
    ],
  });
  await scanning;
  const html = h.element('batch-picker').innerHTML;
  assert.match(html, /data-gid="1"[\s\S]*?data-remote="gpt-test"[^>]*checked/, '完全同名默认勾上');
  assert.match(html, /data-gid="3"[\s\S]*?data-remote="gpt-test"[^>]*checked/, '另一种接口下的同名也默认勾上');
  assert.match(html, /data-gid="2"[\s\S]*?data-remote="gpt-testing-preview"/, '部分匹配照样列出来');
  assert.doesNotMatch(html, /data-remote="gpt-testing-preview"[^>]*checked/, '部分匹配不许默认勾');
  assert.match(html, /部分匹配/);
  assert.match(html, /目录里已登记/, '本地目录来的要标出来');
  assert.match(html, /data-act="batch-pick"/);
  // 段按「这一段里最好有多像」排：只有部分匹配的 Messages 段不能顶在 Responses 前面。
  // anthropic 不在这个宿主注册的协议表里，所以段名退回协议名本身。
  const order = [...html.matchAll(/pick-sep">([^<]+)</g)].map((m) => m[1]);
  assert.deepEqual(order, ['Responses', 'Chat', 'anthropic'],
    `段的顺序应该是 ${JSON.stringify(order)}`);
});

test('扫描结果为空时说明白，不给一个空列表', async (t) => {
  const h = harness(t);
  h.app.openBatchAdd();
  h.element('batch-model').value = 'nope';
  const scanning = h.app.scanModels();
  h.latest().resolve({
    model_name: 'nope', scanned: 2, matched: 0, failed: 1, ms: 8,
    groups: [scanRow(1, 'openai', [], { error: '拉取超时' })],
  });
  await scanning;
  const html = h.element('batch-picker').innerHTML;
  assert.match(html, /没问到/);
  assert.match(html, /拉取超时/);
  assert.equal(h.element('batch-save').disabled, true);
});

test('提交只发勾中的行，并且加过的行不会重复提交', async (t) => {
  const h = harness(t);
  h.app.openBatchAdd();
  h.element('batch-model').value = 'gpt-test';
  const scanning = h.app.scanModels();
  h.latest().resolve({
    model_name: 'gpt-test', scanned: 2, matched: 2, failed: 0, ms: 5,
    groups: [
      scanRow(1, 'openai', [{ remote_model: 'gpt-test', level: 'exact', from_catalog: false }]),
      scanRow(2, 'openai', [
        { remote_model: 'gpt-test', level: 'exact', from_catalog: false },
        { remote_model: 'gpt-test-2024', level: 'partial', from_catalog: false },
      ]),
    ],
  });
  await scanning;
  const boxes = stubBatchRows(h, [
    { group_id: 1, remote: 'gpt-test', checked: true },
    { group_id: 2, remote: 'gpt-test', checked: true },
    { group_id: 2, remote: 'gpt-test-2024', checked: false },
  ]);
  h.app.syncBatchSelection();
  const saving = h.app.commitBatch();
  const job = h.latest();
  assert.equal(job.path, '/admin/api/models/batch');
  assert.deepEqual(job.body.groups, [
    { group_id: 1, remote_model: 'gpt-test' },
    { group_id: 2, remote_model: 'gpt-test' },
  ], '没勾的那条不能进去');
  job.resolve({ committed: 2, skipped: [], protocols: ['openai'] });
  h.releaseRefreshes();
  await saving;
  assert.match(h.element('batch-hint').textContent, /已加 2 条候选/);
  assert.equal(h.toasts.at(-1)[0], '已添加 2 条候选');

  // 加过的两条在重新渲染后已经是 disabled 的（渲染走 batchAdded 这个集合，
  // 不靠 dataset —— 提交后的 refreshConfig 会把整个列表重画一遍）
  for (const box of boxes) box.disabled = box.dataset.remote !== 'gpt-test-2024';
  h.element('batch-picker').querySelectorAll = (sel) => (sel.includes('batch-pick') ? boxes : []);
  h.app.syncBatchSelection();
  assert.equal(h.element('batch-save').disabled, true, '没有新勾的了');
  const before = h.requests.length;
  await h.app.commitBatch();
  assert.equal(h.requests.length, before, '不许再发一次提交');
  assert.match(h.toasts.at(-1)[0], /没有新的了/);
  assert.equal(h.element('batch-save').disabled, true, '按钮要重新摆回不可点');
});

test('弹窗关掉之后，迟到的扫描结果不许写进新会话', async (t) => {
  const h = harness(t);
  h.app.openBatchAdd();
  h.element('batch-model').value = 'gpt-test';
  const scanning = h.app.scanModels();
  h.element('batch-dialog').close();      // 触发 close 监听：旧扫描作废
  h.app.openBatchAdd();
  h.latest().resolve({
    model_name: 'gpt-test', scanned: 1, matched: 1, failed: 0, ms: 3,
    groups: [scanRow(1, 'openai', [{ remote_model: 'gpt-test', level: 'exact', from_catalog: false }])],
  });
  await scanning;
  assert.equal(h.element('batch-picker').innerHTML, '');
  assert.equal(h.element('batch-count').textContent, '');
  assert.equal(h.element('batch-status').textContent, '填个模型名，点「扫描所有上游站」');
});
