import assert from 'node:assert/strict';
import test from 'node:test';
import { readFileSync } from 'node:fs';
import vm from 'node:vm';
import { routeSummary } from '../web/route-view.js';
import { requestResult } from '../web/request-result.js';

const candidate = (id, extra = {}) => ({ route_id: id, priority: id,
  group_enabled: true, upstream_enabled: true, cooling_ms: 0, ...extra });
const row = (extra = {}) => ({ model_name: 'shared', protocol: 'openai',
  candidates: [candidate(1), candidate(2)], preferred_route_id: 1, active_route_id: 1, ...extra });

test('保存的首选与后端给出的下次起点分别展示，不自行模拟请求', () => {
  const s = routeSummary(row({ active_route_id: 2,
    candidates: [candidate(1, { cooling_ms: 1000 }), candidate(2)] }));
  assert.equal(s.preferred.route_id, 1);
  assert.equal(s.next.route_id, 2);
  assert.equal(s.label, '使用备用起点');
  const cooling = routeSummary(row({ candidates: [candidate(1, { cooling_ms: 1000 })] }));
  assert.equal(cooling.next.route_id, 1, '关闭换站时后端可继续选择冷却候选');
  assert.equal(cooling.label, '起点冷却中');
});

test('转发按模型及协议解析，并显示最终目标；断链与停用不冒充可用', () => {
  const target = row();
  const chat = row({ protocol: 'openai-chat', active_route_id: 9, candidates: [candidate(9)] });
  const source = row({ model_name: 'alias', forward_to: 'shared', candidates: [] });
  const alias2 = row({ model_name: 'alias2', forward_to: 'alias', candidates: [] });
  const s = routeSummary(alias2, [source, chat, target, alias2]);
  assert.equal(s.target, target);
  assert.deepEqual(s.path.map(r => r.model_name), ['alias', 'shared']);
  assert.equal(s.next.route_id, 1);
  assert.equal(routeSummary(source, [chat]).label, '转发目标不可用');
  const disabled = row({ active_route_id: null, candidates: [candidate(1, { group_enabled: false })] });
  assert.equal(routeSummary(disabled).next, null);
  assert.equal(routeSummary(source, [disabled]).label, '无可用候选');
  assert.equal(routeSummary(row({ candidates: [], active_route_id: null })).tone, 'warn');
});

test('请求结果优先于 HTTP 200，主动中断与未知结果不画成成功', () => {
  for (const note of ['truncated', 'hold_retry', 'upstream_abort', 'stall_timeout', 'failed_over']) {
    const result = requestResult({ status: 200, note });
    assert.equal(result.issue, true);
    assert.notEqual(result.tone, 'good');
  }
  for (const note of ['client_abort', 'manual_abort']) {
    assert.deepEqual(requestResult({ status: 200, note }).tone, '');
    assert.equal(requestResult({ status: 200, note }).issue, true);
  }
  assert.equal(requestResult({ status: 200, note: 'new-outcome' }).label, 'new-outcome');
  assert.equal(requestResult({ status: 503 }).label, '请求失败');
  assert.equal(requestResult({ status: 0 }).label, '未收到响应');
  assert.equal(requestResult({ status: 200, note: 'ok' }).issue, false);
});

test('隐藏画布不取布局、不生成节点或安排动画帧；打开时只发一个布局请求', async () => {
  const state = { view: 'routes' };
  let requests = 0;
  const source = readFileSync(new URL('../web/canvas.js', import.meta.url), 'utf8')
    .replace(/^import [\s\S]*? from '[^']+';\r?\n/gm, '').replace(/^export /gm, '');
  const context = vm.createContext({ state, document: { visibilityState: 'visible' },
    orderedCandidates: () => [], api: () => { requests++; return new Promise(() => {}); } });
  vm.runInContext(source + '\nhost = {}; pool = {}; renderCanvas(); renderPool();', context);
  assert.equal(requests, 0);
  state.view = 'canvas';
  vm.runInContext('renderCanvas(); renderCanvas();', context);
  assert.equal(requests, 1);
  state.view = 'routes';
  vm.runInContext('renderCanvas(); renderPool();', context);
  assert.equal(requests, 1);
});

test('批量添加新模型时保留旧气泡位置，新气泡寻找空位', () => {
  const state = { routes: [row({model_name:'b'}), row({model_name:'c'})] };
  const source = readFileSync(new URL('../web/canvas.js', import.meta.url), 'utf8')
    .replace(/^import [\s\S]*? from '[^']+';\r?\n/gm, '').replace(/^export /gm, '');
  const context = vm.createContext({ state, orderedCandidates: () => [], protocolOn: () => true });
  vm.runInContext(source + '\nautoPlace();', context);
  const before = vm.runInContext('JSON.stringify([...pos])', context);
  state.routes.unshift(row({model_name:'a'}));
  vm.runInContext('autoPlace()', context);
  const after = JSON.parse(vm.runInContext('JSON.stringify([...pos])', context));
  for (const [key, point] of JSON.parse(before)) assert.deepEqual(after.find(([k])=>k===key)[1], point);
  assert.equal(new Set(after.map(([,p])=>p.join(','))).size, 3);
});
