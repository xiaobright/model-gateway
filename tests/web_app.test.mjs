// AI生成
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';
import vm from 'node:vm';

import * as util from '../web/util.js';
import * as groupEditor from '../web/group-editor.js';
import { createRefreshQueue } from '../web/async-state.js';
import * as routeModel from '../web/route-model.js';
import { requestResult } from '../web/request-result.js';

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
    editingUp: null, editingGroup: null, editingCand: null, iface: '', filter: '', proto: '', logIds: new Set(),
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
        querySelectorAll: () => [], querySelector: () => null, contains: () => false,
        focus: noop, scrollIntoView: noop,
        showModal() { this.open = true; },
        close() { this.open = false; this.dispatch('close'); },
        addEventListener(type, callback) { listeners.set(type, callback); },
        dispatch(type) { return listeners.get(type)?.({ target: this }); },
      });
    }
    return elements.get(id);
  };
  let motionReduced = true;
  const vtVars = new Map();
  const vtRoot = {
    style: { setProperty: (k, v) => vtVars.set(k, v), removeProperty: (k) => vtVars.delete(k) },
    dataset: {},
  };
  const h = { element, requests: [], refreshes: [], timers: [], toasts: [], blockRefresh: false,
    vt: { vars: vtVars, root: vtRoot, calls: [], runs: true }, slides: [],
    setReduceMotion: (v) => { motionReduced = v; } };
  const oldFetch = globalThis.fetch;
  globalThis.fetch = async (path, options) => {
    const job = { ...deferred(), path, method: options.method, body: options.body && JSON.parse(options.body) };
    h.requests.push(job);
    return new Response(JSON.stringify(await job.promise), { status: 200 });
  };
  t.after(() => { globalThis.fetch = oldFetch; groupEditor.invalidateGroupEdit(); });
  const context = vm.createContext({
    ...util, ...groupEditor, ...routeModel, routeKey: routeModel.modelKey, performance,
    requestResult, createRefreshQueue, $: element,
    document: {
      addEventListener: noop, querySelectorAll: () => [], visibilityState: 'visible',
      querySelector: () => [...elements.values()].find((e) => e.open),
      documentElement: vtRoot,
      startViewTransition: (fn) => { fn(); return { finished: Promise.resolve() }; },
    },
    window: { addEventListener: noop, innerWidth: 1000, innerHeight: 800, scrollY: 0, scrollTo: noop }, localStorage: { getItem: () => null, setItem: noop },
    location: { origin: 'http://127.0.0.1', hash: '' }, history: { replaceState: noop },
    views: new Proxy({}, { get: () => noop }),
    // forward-dialog.js 的出口 —— ACTIONS 里 spread 了 forwardActions，少了它整个模块都跑不起来
    forwardActions: {}, initForwardDialog: noop, saveForward: async () => {},
    toast: (...args) => h.toasts.push(args), run: (_el, fn) => fn(), confirmBox: async () => true,
    requestAnimationFrame: (fn) => fn(), setInterval: (fn, ms) => h.timers.push({ fn, ms }),
    withViewTransition: (dir, fn, afterPaint) => {
      if (!h.vt.runs) { fn(); afterPaint?.(false); return false; }
      h.vt.calls.push(dir); fn(); afterPaint?.(true); return true;
    },
    reduceMotion: () => motionReduced,
    moveNavMarker: noop, slideIn: (el) => h.slides.push(el), fadeIn: noop, toggleMotion: noop,
    readBarWidths: () => new Map(), morphBars: noop,
    enterStagger: noop, springAnimate: noop, SPRING: { bouncy: 'ease', smooth: 'ease', snappy: 'ease' },
    animateViewEnter: noop, syncSegMarkers: noop,
    openDialog: dlg => dlg.showModal(), closeDialog: dlg => { dlg.close(); return Promise.resolve(); },
    captureRects: () => new Map(), flipElements: noop, animateDetailSwap: noop, pulse: noop,
    updateNumber: (el, value) => { if (el) el.textContent = value; },
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
    openRename, saveRename,
    fillRemoteList, saveRewrite, actions: ACTIONS, groupDirty,
    openBatchAdd, scanModels, commitBatch, syncBatchSelection,
    editingKey: () => editingKey, showView
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

/* -------------------------------------------------- 分组选择器：对应到哪个下游 */

const stubPickLabels = (h, rows) => {
  const labels = rows.map((r) => ({
    classList: { add: noop, remove: noop },
    querySelector: (s) => (s.startsWith('input')
      ? { checked: false, dataset: { name: r.name } }
      : { value: r.map }),
  }));
  h.element('grp-picker').querySelectorAll = (sel) => (sel.includes('label') ? labels : []);
  return labels;
};

test('分组选择器：每行带「对应到哪个下游」，默认按档位预选，已暴露的锁着', async (t) => {
  const h = harness(t);
  const gid = 61;
  util.state.upstreams = [provider(1, [{ ...group(gid), models: ['gpt-test'] }])];
  await editGroup(h, gid, 'key-A');
  h.evaluate(`
    pulled = ['gpt-test', 'kimi-k2', 'gpt-test-2024', 'locked-one'];
    pickPlans = new Map([
      ['gpt-test', { exact: ['gpt-test'], partial: [], exposed_as: [] }],
      ['kimi-k2', { exact: [], partial: [], exposed_as: [] }],
      ['gpt-test-2024', { exact: [], partial: ['gpt-test'], exposed_as: [] }],
      ['locked-one', { exact: [], partial: [], exposed_as: ['my-chat'] }],
    ]);
    renderPicker();
  `);
  const html = h.element('grp-picker').innerHTML;
  assert.match(html, /<option value="down:gpt-test" selected>对应到完全匹配：gpt-test<\/option>/,
    '完全同名默认对应到它');
  assert.match(html, /<option value="new" selected>新开下游模型：kimi-k2<\/option>/,
    '下游没有的默认新开');
  assert.match(html, /<option value="catalog" selected>只留着，不对下游展示<\/option>/,
    '只沾一部分的默认只留着');
  assert.match(html, /对应到部分匹配：gpt-test/, '部分匹配也给选项');
  assert.match(html, /data-name="locked-one"[\s\S]*?disabled/, '已暴露的行是锁着的');
  assert.match(html, /已暴露给下游：my-chat/);
});

test('拉取模型列表后，一并问回每个名字对应到哪些下游', async (t) => {
  const h = harness(t);
  const gid = 62;
  util.state.upstreams = [provider(1, [{ ...group(gid), models: [] }])];
  await editGroup(h, gid, 'key-A');
  const pulling = h.app.actions['pull-models']();
  const seen = h.requests.length;
  h.latest().resolve({ models: ['gpt-test'] });     // GET remote-models
  for (let i = 0; i < 20 && h.requests.length <= seen; i++) await tick();
  const planJob = h.latest();
  assert.equal(planJob.path, `/admin/api/groups/${gid}/models/plan`);
  assert.deepEqual(planJob.body, { model_names: ['gpt-test'] });
  planJob.resolve({
    plans: [{ remote_model: 'gpt-test', exact: ['gpt-test'], partial: [], exposed_as: [] }],
  });
  await pulling;
  assert.match(h.element('grp-picker').innerHTML, /对应到完全匹配：gpt-test/);
});

test('分组选择器：勾上时按选择框对应到下游，发的是建候选', async (t) => {
  const h = harness(t);
  const gid = 63;
  util.state.upstreams = [provider(1, [{ ...group(gid), models: [] }])];
  await editGroup(h, gid, 'key-A');
  h.evaluate(`
    pulled = ['kimi-k2'];
    pickPlans = new Map([['kimi-k2', { exact: [], partial: [], exposed_as: [] }]]);
    renderPicker();
  `);
  const label = {
    classList: { add: noop, remove: noop },
    querySelector: (s) => (s.startsWith('input')
      ? { checked: true, dataset: { name: 'kimi-k2' } }
      : { value: 'new' }),
  };
  const adding = h.app.actions['pick-toggle']({ name: 'kimi-k2' }, { checked: true, closest: () => label });
  const job = h.latest();
  assert.equal(job.path, '/admin/api/models');
  assert.deepEqual(job.body, { model_name: 'kimi-k2', group_id: gid, remote_model: 'kimi-k2' });
  const seen = h.requests.length;
  job.resolve({ route_id: 7, model_name: 'kimi-k2', group_id: gid, remote_model: 'kimi-k2', protocol: 'openai' });
  for (let i = 0; i < 20 && h.requests.length <= seen; i++) await tick();
  // 新开了一个下游模型：同一批的建议整批重问，后面的行才沾得到匹配
  assert.equal(h.latest().path, `/admin/api/groups/${gid}/models/plan`);
  assert.deepEqual(h.latest().body, { model_names: ['kimi-k2'] });
  h.latest().resolve({
    plans: [{ remote_model: 'kimi-k2', exact: [], partial: [], exposed_as: ['kimi-k2'] }],
  });
  h.releaseRefreshes();
  await adding;
  assert.match(h.toasts.at(-1)[0], /已新建下游模型 kimi-k2/);
  // 对应过之后那一行锁着，只说明现状
  assert.match(h.element('grp-picker').innerHTML, /已暴露给下游：kimi-k2/);
});

/* 实测：一批里先新开 mistral-large-4，后面的 mistral-large-4:tool 打开弹窗时还没有
   它、建议里没有匹配。新开之后要重问建议，那一行才看得到「对应到部分匹配」。 */
test('新开下游模型后重问建议：同批后面的行沾到部分匹配', async (t) => {
  const h = harness(t);
  const gid = 68;
  util.state.upstreams = [provider(1, [{ ...group(gid), models: [] }])];
  await editGroup(h, gid, 'key-A');
  h.evaluate(`
    pulled = ['mistral-large-4', 'mistral-large-4:tool'];
    pickPlans = new Map([
      ['mistral-large-4', { exact: [], partial: [], exposed_as: [] }],
      ['mistral-large-4:tool', { exact: [], partial: [], exposed_as: [] }],
    ]);
    renderPicker();
  `);
  const label = {
    classList: { add: noop, remove: noop },
    querySelector: (s) => (s.startsWith('input')
      ? { checked: true, dataset: { name: 'mistral-large-4' } }
      : { value: 'new' }),
  };
  const adding = h.app.actions['pick-toggle']({ name: 'mistral-large-4' }, { checked: true, closest: () => label });
  const job = h.latest();
  assert.deepEqual(job.body, { model_name: 'mistral-large-4', group_id: gid, remote_model: 'mistral-large-4' });
  const seen = h.requests.length;
  job.resolve({ route_id: 5, model_name: 'mistral-large-4', group_id: gid, remote_model: 'mistral-large-4', protocol: 'openai' });
  for (let i = 0; i < 20 && h.requests.length <= seen; i++) await tick();
  assert.equal(h.latest().path, `/admin/api/groups/${gid}/models/plan`);
  assert.deepEqual(h.latest().body, { model_names: ['mistral-large-4', 'mistral-large-4:tool'] });
  h.latest().resolve({
    plans: [
      { remote_model: 'mistral-large-4', exact: ['mistral-large-4'], partial: [], exposed_as: ['mistral-large-4'] },
      { remote_model: 'mistral-large-4:tool', exact: [], partial: ['mistral-large-4'], exposed_as: [] },
    ],
  });
  h.releaseRefreshes();
  await adding;
  assert.match(h.element('grp-picker').innerHTML,
    /data-name="mistral-large-4:tool"[\s\S]*?对应到部分匹配：mistral-large-4/,
    '后一行现在看得到刚建的下游模型');
});

/* 实测：同名模型在另一种接口下已经暴露着。那边暴露了不等于这边也有（两条链互不
   可见），提示要说清是在本接口下暴露，别让人以为新建了一个模型。 */
test('同名模型只在另一种接口下有过：提示说的是在本接口下暴露', async (t) => {
  const h = harness(t);
  const gid = 69;
  util.state.upstreams = [provider(1, [{ ...group(gid), models: [] }])];
  util.state.routes = [
    { model_name: 'kimi-k2', protocol: 'anthropic', candidates: [{ group_id: 999, remote_model: 'kimi-k2' }] },
  ];
  await editGroup(h, gid, 'key-A');
  h.evaluate(`
    pulled = ['kimi-k2'];
    pickPlans = new Map([['kimi-k2', { exact: [], partial: [], exposed_as: [] }]]);
    renderPicker();
  `);
  const label = {
    classList: { add: noop, remove: noop },
    querySelector: (s) => (s.startsWith('input')
      ? { checked: true, dataset: { name: 'kimi-k2' } }
      : { value: 'new' }),
  };
  const adding = h.app.actions['pick-toggle']({ name: 'kimi-k2' }, { checked: true, closest: () => label });
  const seen = h.requests.length;
  h.latest().resolve({ route_id: 11, model_name: 'kimi-k2', group_id: gid, remote_model: 'kimi-k2', protocol: 'openai' });
  for (let i = 0; i < 20 && h.requests.length <= seen; i++) await tick();
  h.latest().resolve({ plans: [{ remote_model: 'kimi-k2', exact: [], partial: [], exposed_as: ['kimi-k2'] }] });
  h.releaseRefreshes();
  await adding;
  assert.match(h.toasts.at(-1)[0], /已把 kimi-k2 暴露到 Responses 接口（另一种接口下已有同名模型）/);
});

test('本接口已经有同名下游模型：算对应过去，不说是新接口暴露', async (t) => {
  const h = harness(t);
  const gid = 70;
  util.state.upstreams = [provider(1, [{ ...group(gid), models: [] }])];
  util.state.routes = [
    { model_name: 'kimi-k2', protocol: 'openai', candidates: [{ group_id: 999, remote_model: 'kimi-k2' }] },
  ];
  await editGroup(h, gid, 'key-A');
  h.evaluate(`
    pulled = ['kimi-k2'];
    pickPlans = new Map([['kimi-k2', { exact: ['kimi-k2'], partial: [], exposed_as: [] }]]);
    renderPicker();
  `);
  const label = {
    classList: { add: noop, remove: noop },
    querySelector: (s) => (s.startsWith('input')
      ? { checked: true, dataset: { name: 'kimi-k2' } }
      : { value: 'down:kimi-k2' }),
  };
  const adding = h.app.actions['pick-toggle']({ name: 'kimi-k2' }, { checked: true, closest: () => label });
  const seen = h.requests.length;
  h.latest().resolve({ route_id: 12, model_name: 'kimi-k2', group_id: gid, remote_model: 'kimi-k2', protocol: 'openai' });
  h.releaseRefreshes();
  await adding;
  assert.match(h.toasts.at(-1)[0], /已把 kimi-k2 对应到下游 kimi-k2/);
  assert.equal(h.requests.length, seen, '对应到建议里已有的模型不用重问建议');
});

test('分组选择器：没勾的行换选择只是记下，勾上的行换选择当场对应', async (t) => {
  const h = harness(t);
  const gid = 64;
  util.state.upstreams = [provider(1, [{ ...group(gid), models: [] }])];
  await editGroup(h, gid, 'key-A');
  h.evaluate(`
    pulled = ['gpt-test-2024'];
    pickPlans = new Map([['gpt-test-2024', { exact: [], partial: ['gpt-test'], exposed_as: [] }]]);
    renderPicker();
  `);
  const unchecked = {
    classList: { add: noop, remove: noop },
    querySelector: (s) => (s.startsWith('input')
      ? { checked: false, dataset: { name: 'gpt-test-2024' } }
      : { value: 'down:gpt-test' }),
  };
  const seen = h.requests.length;
  await h.app.actions['pick-map']({ name: 'gpt-test-2024' },
    { value: 'down:gpt-test', closest: () => unchecked });
  assert.equal(h.requests.length, seen, '没勾的行不该发请求');
  assert.equal(h.evaluate('pickPending.get("gpt-test-2024")'), 'down:gpt-test');
  // 重画之后待生效的选择还在，不会被默认值冲掉
  h.evaluate('renderPicker()');
  assert.match(h.element('grp-picker').innerHTML, /<option value="down:gpt-test" selected>/);

  const checked = {
    classList: { add: noop, remove: noop },
    querySelector: (s) => (s.startsWith('input')
      ? { checked: true, dataset: { name: 'gpt-test-2024' } }
      : { value: 'down:gpt-test' }),
  };
  const mapping = h.app.actions['pick-map']({ name: 'gpt-test-2024' },
    { value: 'down:gpt-test', closest: () => checked });
  const job = h.latest();
  assert.equal(job.path, '/admin/api/models');
  assert.deepEqual(job.body, { model_name: 'gpt-test', group_id: gid, remote_model: 'gpt-test-2024' });
  job.resolve({ route_id: 9, model_name: 'gpt-test', group_id: gid, remote_model: 'gpt-test-2024', protocol: 'openai' });
  h.releaseRefreshes();
  await mapping;
  assert.match(h.toasts.at(-1)[0], /已把 gpt-test-2024 对应到下游 gpt-test/);
});

test('全选按每行的选择处理：只留着的批量登记，要对应的逐条建候选', async (t) => {
  const h = harness(t);
  const gid = 65;
  util.state.upstreams = [provider(1, [{ ...group(gid), models: [] }])];
  await editGroup(h, gid, 'key-A');
  // 对应到建议里已有的下游模型：不新开链，就不用重问建议
  h.evaluate(`pickPlans = new Map([['gpt-test', { exact: ['gpt-test'], partial: [], exposed_as: [] }]]);`);
  stubPickLabels(h, [
    { name: 'gpt-test', map: 'down:gpt-test' },
    { name: 'kimi-k2', map: 'catalog' },
  ]);
  const all = h.app.actions['pick-all']();
  const seen = h.requests.length;
  assert.equal(h.latest().path, `/admin/api/groups/${gid}/models`);
  assert.deepEqual(h.latest().body, { model_names: ['kimi-k2'] }, '只留着的一起发');
  h.latest().resolve({ added: 1 });
  for (let i = 0; i < 20 && h.requests.length <= seen; i++) await tick();
  assert.equal(h.latest().path, '/admin/api/models');
  assert.deepEqual(h.latest().body, { model_name: 'gpt-test', group_id: gid, remote_model: 'gpt-test' });
  h.latest().resolve({ route_id: 3, model_name: 'gpt-test', group_id: gid, remote_model: 'gpt-test', protocol: 'openai' });
  h.releaseRefreshes();
  await all;
  assert.equal(h.requests.length, seen + 1, '对应到已有模型不该再重问建议');
  assert.match(h.toasts.at(-1)[0], /登记 1 个，对应下游 1 条/);
});

/* 实测回归：直接在「上游站点」把上游模型加到某个下游底下，再打开分组弹窗（没有点
   「拉取模型列表」）—— 已暴露的要认出来、锁上，不能再按「新开」建出重复。 */
test('打开分组弹窗就问回对应建议：已暴露的锁上并显示真实下游名', async (t) => {
  const h = harness(t);
  const gid = 66;
  const remote = 'deepseek-ai/DeepSeek-V4-Flash-0731';
  util.state.upstreams = [provider(1, [{ ...group(gid), models: [remote, 'glm-5.3-flash'] }])];
  util.state.routes = [
    { model_name: 'deepseek-v4-flash', protocol: 'openai', candidates: [{ group_id: gid, remote_model: remote }] },
    { model_name: 'glm-5.3-flash', protocol: 'openai', candidates: [{ group_id: gid, remote_model: 'glm-5.3-flash' }] },
  ];
  await editGroup(h, gid, 'key-A');
  // 打开弹窗就该问建议，不等「拉取模型列表」
  const planJob = h.latest();
  assert.equal(planJob.path, `/admin/api/groups/${gid}/models/plan`);
  assert.deepEqual(planJob.body, { model_names: [remote, 'glm-5.3-flash'] });
  planJob.resolve({
    plans: [
      { remote_model: remote, exact: [], partial: ['deepseek-v4-flash'], exposed_as: ['deepseek-v4-flash'] },
      { remote_model: 'glm-5.3-flash', exact: ['glm-5.3-flash'], partial: [], exposed_as: ['glm-5.3-flash'] },
    ],
  });
  for (let i = 0; i < 20 && !/已暴露给下游/.test(h.element('grp-picker').innerHTML); i++) await tick();
  const html = h.element('grp-picker').innerHTML;
  assert.match(html, /data-name="deepseek-ai\/DeepSeek-V4-Flash-0731"[\s\S]*?已暴露给下游：deepseek-v4-flash/,
    '带厂商前缀的真名要认出它挂在短名底下');
  assert.match(html, /data-name="glm-5.3-flash"[\s\S]*?已暴露给下游：glm-5.3-flash/);
  assert.doesNotMatch(html, /新开下游模型：deepseek-ai/, '已暴露的不该还默认新开');
});

test('旧分组的对应建议回包不能污染重开的新分组', async (t) => {
  const h = harness(t);
  util.state.upstreams = [provider(1, [
    { ...group(69), models: ['shared'] },
    { ...group(70), models: ['shared'] },
  ])];
  await editGroup(h, 69, 'key-A');
  const oldPlan = h.latest();
  await editGroup(h, 70, 'key-B');
  const newPlan = h.latest();
  newPlan.resolve({
    plans: [{ remote_model: 'shared', exact: [], partial: [], exposed_as: [] }],
  });
  await tick();
  assert.deepEqual(h.evaluate('pickPlans.get("shared")'), {
    remote_model: 'shared', exact: [], partial: [], exposed_as: [],
  });
  oldPlan.resolve({
    plans: [{ remote_model: 'shared', exact: ['wrong-group'], partial: [], exposed_as: [] }],
  });
  await tick();
  assert.deepEqual(h.evaluate('pickPlans.get("shared")'), {
    remote_model: 'shared', exact: [], partial: [], exposed_as: [],
  });
});

test('对应建议失败后重绘不重复请求', async (t) => {
  const h = harness(t);
  const gid = 71;
  util.state.upstreams = [provider(1, [{ ...group(gid), models: ['shared'] }])];
  await editGroup(h, gid, 'key-A');
  const first = h.latest();
  first.reject(new Error('建议接口暂时不可用'));
  await tick();
  const before = h.requests.length;
  h.evaluate('renderPicker()');
  await tick();
  assert.equal(h.requests.length, before);
});

test('建议没问到也不许建重复：已经挂在别的下游名下的按「新开」点也被拦下', async (t) => {
  const h = harness(t);
  const gid = 67;
  const remote = 'deepseek-ai/DeepSeek-V4-Flash-0731';
  util.state.upstreams = [provider(1, [{ ...group(gid), models: [remote] }])];
  util.state.routes = [
    { model_name: 'deepseek-v4-flash', protocol: 'openai', candidates: [{ group_id: gid, remote_model: remote }] },
  ];
  await editGroup(h, gid, 'key-A');
  h.latest().reject(new Error('建议接口炸了'));   // 建议拿不到：行退回「新开」默认
  await tick();
  assert.match(h.element('grp-picker').innerHTML, /新开下游模型：deepseek-ai/);
  const label = {
    classList: { add: noop, remove: noop },
    querySelector: (s) => (s.startsWith('input')
      ? { checked: true, dataset: { name: remote } }
      : { value: 'new' }),
  };
  const before = h.requests.length;
  await h.app.actions['pick-toggle']({ name: remote }, { checked: true, closest: () => label });
  assert.equal(h.requests.length, before, '不许发建候选的请求');
  assert.match(h.toasts.at(-1)[0], /已经挂在「deepseek-v4-flash」底下/);
  // 那一行转为锁着的，显示真实的下游名
  assert.match(h.element('grp-picker').innerHTML, /已暴露给下游：deepseek-v4-flash/);
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

function loadViews(h) {
  for (const file of ['route-view', 'views']) {
    h.evaluate(readFileSync(new URL(`../web/${file}.js`, import.meta.url), 'utf8')
      .replace(/^import [\s\S]*? from '[^']+';\r?\n/gm, '')
      .replace(/^export /gm, ''));
  }
}

test('改下游模型名只提交名字，提示同名模型在别的接口下不受影响', async (t) => {
  const h = harness(t);
  util.state.routes = [
    { model_name: 'old', protocol: 'openai', candidates: [{ route_id: 7 }], active_route_id: 7, preferred_route_id: 7 },
    { model_name: 'old', protocol: 'openai-chat', candidates: [] },
  ];
  h.app.openRename('old', 'openai');
  assert.equal(h.element('rename-dialog').open, true);
  assert.equal(h.element('rn-name').value, 'old');
  assert.match(h.element('rn-hint').innerHTML, /Chat/);

  h.element('rn-name').value = 'new';
  const saving = h.app.saveRename();
  assert.equal(h.latest().method, 'POST');
  assert.equal(h.latest().path, '/admin/api/models/rename');
  assert.deepEqual(h.latest().body, { model_name: 'old', protocol: 'openai', new_name: 'new' });
  h.latest().resolve({ ok: true });
  await saving;
  assert.equal(h.element('rename-dialog').open, false);
  assert.deepEqual(h.toasts.at(-1), ['old 已改名为 new', 'ok']);
});

test('工作台刷新保留同名模型的协议选择；筛选后重新校验选择', (t) => {
  const h = harness(t);
  loadViews(h);
  util.state.routes = ['openai', 'openai-chat'].map(protocol => ({
    model_name: 'shared', protocol, candidates: [], active_route_id: null,
  }));
  h.evaluate("selectRouteWorkspace('shared', 'openai-chat'); renderRoutes()");
  assert.match(h.element('route-detail').innerHTML, /<span class="tag">Chat<\/span>/);
  assert.match(h.element('route-detail').innerHTML, /data-act="rename-model"/);
  const before = h.element('route-list').innerHTML;
  let writes = 0;
  Object.defineProperty(h.element('route-list'), 'innerHTML', {
    get: () => before, set: () => { writes++; }, configurable: true,
  });
  h.evaluate('renderRoutes()');
  assert.equal(writes, 0, '数据未变不重建列表');
  util.state.iface = 'openai';
  h.evaluate('renderRoutes()');
  assert.match(h.element('route-detail').innerHTML, /<span class="tag">Responses<\/span>/);
  util.state.filter = 'missing';
  h.evaluate('renderRoutes()');
  assert.match(h.element('route-detail').innerHTML, /选中一个模型/);
});

test('工作台更新保留键盘焦点和滚动；接口关闭后提示恢复配置', (t) => {
  const h = harness(t);
  loadViews(h);
  util.state.routes = [{ model_name: 'shared', protocol: 'openai', candidates: [] }];
  h.evaluate('renderRoutes()');
  const host = h.element('route-list');
  let focused = 0;
  const key = JSON.stringify(['shared', 'openai']);
  host.contains = () => true;
  host.scrollTop = 48;
  host.querySelectorAll = () => [{ dataset: { focusKey: key }, classList: { toggle: () => {} }, setAttribute: () => {}, focus: () => { focused++; } }];
  h.evaluate(`document.activeElement = { dataset: { focusKey: ${JSON.stringify(key)} } };`);
  util.state.overview = { models: [{ model: 'shared', protocol: 'openai', n: 7 }] };
  h.evaluate('renderRoutes()');
  assert.equal(focused, 1);
  assert.equal(host.scrollTop, 48);
  util.state.protocolEnabled.openai = false;
  h.evaluate('renderRoutes()');
  assert.match(host.innerHTML, /当前没有可见模型/);
  assert.match(host.innerHTML, /data-act="go-settings"/);
});

test('记录按协议、结果和关键词组合筛选，详情保留已显示尝试的数据', (t) => {
  const h = harness(t);
  loadViews(h);
  const records = [
    { id: 1, model: 'shared', protocol: 'openai', status: 200, note: 'truncated' },
    { id: 2, model: 'shared', protocol: 'openai-chat', status: 200, note: 'ok' },
    { id: 3, model: 'other', protocol: 'openai', status: 503, note: '' },
  ];
  h.evaluate(`lastRows = ${JSON.stringify(records)}; lastRows.forEach(logRow);`);
  const rows = records.map(r => ({ dataset: { id: String(r.id), logProto: r.protocol,
    logIssue: String(requestResult(r).issue), logSearch: r.model }, hidden: false }));
  h.element('log-body').children = rows;
  h.element('log-issues').checked = true;
  h.element('log-filter').value = 'shared';
  util.state.proto = 'openai';
  h.evaluate('applyLogFilter(); showRequestDetails(1)');
  assert.deepEqual(rows.map(r => Boolean(r.hidden)), [false, true, true]);
  assert.equal(h.element('log-count').textContent, '1 / 3 条');
  assert.match(h.element('request-detail-body').innerHTML, /流被截断/);
  assert.match(h.element('request-detail-body').innerHTML, /HTTP 200/);
  assert.equal(h.element('request-dialog').open, true);
  h.element('log-filter').value = 'absent';
  h.evaluate('applyLogFilter()');
  assert.equal(h.element('log-no-match').hidden, false);
});

test('实时卡片只有耗时变化时，不重建内容或中断按钮', (t) => {
  const h = harness(t);
  loadViews(h);
  const host = h.element('live-list'), card = h.element('call-1'), timer = h.element('call-ms');
  card.dataset.id = '1';
  card.querySelector = () => timer;
  host.children = [card];
  host.firstElementChild = card;
  let writes = 0;
  Object.defineProperty(card, 'innerHTML', { set: () => { writes++; } });
  h.evaluate("paintCalls($('live-list'), [{id:1,phase:'wait',model:'shared',elapsed_ms:1000}])");
  h.evaluate("paintCalls($('live-list'), [{id:1,phase:'wait',model:'shared',elapsed_ms:2000}])");
  assert.equal(writes, 1);
  assert.equal(timer.textContent, '2.0s');
});

test('慢轮询只为当前页面拉用量或记录，隐藏标签页停止轮询', async (t) => {
  const h = harness(t);
  h.evaluate("let pollCalls = []; refreshOverview = async () => pollCalls.push('overview'); refreshLog = async () => pollCalls.push('log');");
  const slow = h.timers.find(timer => timer.ms === 15000);
  util.state.view = 'routes';
  slow.fn();
  await tick();
  assert.equal(h.evaluate("pollCalls.join(',')"), 'overview');
  util.state.view = 'log';
  slow.fn();
  await tick();
  assert.equal(h.evaluate("pollCalls.join(',')"), 'overview,log');
  const refreshCount = h.refreshes.length;
  h.evaluate("document.visibilityState = 'hidden'");
  slow.fn();
  await tick();
  assert.equal(h.refreshes.length, refreshCount);
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
  loadViews(h);
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
  const rows = h.element('route-list').innerHTML.split('<button type="button" class="route').slice(1);
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

test('明暗切换的圆形揭开：圆心取按钮中心，半径按百分比够到最远角', async (t) => {
  const h = harness(t);
  h.setReduceMotion(false);
  // 视口 1000×800，按钮中心 (25, 40)，离它最远的角是右下 (1000, 800)
  h.app.actions['toggle-theme']({}, { getBoundingClientRect: () => ({ left: 10, top: 20, width: 30, height: 40 }) });
  assert.equal(h.vt.vars.get('--vt-x'), '2.500%');
  assert.equal(h.vt.vars.get('--vt-y'), '5.000%');
  assert.equal(h.vt.vars.get('--vt-duration'), '560ms');
  assert.equal(h.vt.root.dataset.vtTheme, '1');
  assert.equal(h.vt.root.dataset.theme, 'light', 'auto 的下一档是 light，且已在过渡回调里应用');

  // circle() 的百分比半径按 √(w²+h²)/√2 解析成 px，换算回来必须真的盖到最远角
  const radiusPx = parseFloat(h.vt.vars.get('--vt-r')) / 100 * (Math.hypot(1000, 800) / Math.SQRT2);
  assert.ok(radiusPx >= Math.hypot(1000 - 25, 800 - 40), `半径 ${radiusPx.toFixed(1)}px 盖不到最远角`);

  await tick();
  assert.equal(h.vt.root.dataset.vtTheme, undefined, '过渡结束后要摘掉 data-vt-theme');
  assert.equal(h.vt.vars.size, 0, '过渡结束后要清掉圆心半径变量');
});

test('减少动态时主题切换不走视图过渡，直接换', (t) => {
  const h = harness(t);
  h.app.actions['toggle-theme']({}, { getBoundingClientRect: () => ({ left: 0, top: 0, width: 10, height: 10 }) });
  assert.equal(h.vt.root.dataset.theme, 'light');
  assert.equal(h.vt.vars.size, 0);
  assert.equal(h.vt.root.dataset.vtTheme, undefined);
});

test('视图切换按导航顺序定横向方向，同视图和未知名不切', (t) => {
  const h = harness(t);
  h.vt.calls.length = 0;              // 启动时已经翻到默认视图一次
  h.app.showView('live');             // routes(0) → live(1)
  h.app.showView('routes');           // 往回翻
  h.app.showView('routes');           // 同视图
  h.app.showView('nope');             // 不存在的视图
  assert.deepEqual(h.vt.calls, ['down', 'up']);
});

test('没套上过渡时视图切换自己补淡入，不重复播放入场动画', (t) => {
  const h = harness(t);
  h.vt.calls.length = 0;
  h.vt.runs = false;
  h.app.showView('overview');
  assert.equal(h.vt.calls.length, 0);
  assert.equal(h.slides.length, 1);
  h.vt.runs = true;
  h.app.showView('settings');
  assert.equal(h.vt.calls.length, 1, '套上过渡就不再补淡入，否则两段动画叠在一起');
  assert.equal(h.slides.length, 1);
});
