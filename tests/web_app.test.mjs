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

test('候选弹窗重开同一组，会新拉列表；同会话仍去重', async (t) => {
  const h = harness(t);
  const gid = 51;
  groupEditor.invalidateRemoteModels(gid);
  util.state.upstreams = [provider(1, [group(gid)])];
  h.element('rt-upstream').value = '1';
  h.element('rt-group').value = String(gid);
  h.app.openRoute('', null, 'openai');
  const oldRequest = h.latest();
  h.app.fillRemoteList(gid);
  assert.equal(h.requests.length, 1, '同会话不能重复 GET');
  h.element('route-dialog').close();
  h.app.openRoute('', null, 'openai');
  assert.equal(h.requests.length, 2, '旧会话 busy 不能挡住重开后的 GET');
  h.element('route-dialog').dispatch('close'); // 旧 close 事件晚于新会话打开，不能作废新请求
  const currentRequest = h.latest();
  oldRequest.resolve({ models: ['stale-model'] });
  await tick();
  assert.equal(groupEditor.remoteModels(gid), undefined);
  currentRequest.resolve({ models: ['current-model'] });
  await tick();
  assert.deepEqual(groupEditor.remoteModels(gid), ['current-model']);
  assert.match(h.element('rt-remote-list').innerHTML, /current-model/);
  assert.doesNotMatch(h.element('rt-remote-hint').textContent, /正在拉/);
  assert.equal(groupEditor.remoteBusy.has(gid), false);
  assert.equal(h.element('route-dialog').open, true);
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
