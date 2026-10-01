import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import vm from 'node:vm';
import * as model from '../web/canvas-model.js';
import { attemptOrder, forwardPath, forwardTargets, moveCandidate } from '../web/canvas-model.js';

const cand = (id, extra = {}) => ({ route_id: id, priority: id, upstream_enabled: true, group_enabled: true, ...extra });
const row = (name, extra = {}) => ({ model_name: name, protocol: 'openai', candidates: [cand(1)], ...extra });

test('首选与排序独立，冷却后移，停用跳过，关闭降级只取起点', () => {
  const r = row('a', { candidates: [cand(3), cand(1), cand(2, { cooling_ms: 100 }), cand(4, { group_enabled: false })], preferred_route_id: 2 });
  assert.deepEqual(attemptOrder(r, true).map(c => c.route_id), [1, 3, 2]);
  assert.deepEqual(attemptOrder(r, false).map(c => c.route_id), [2]);
  assert.deepEqual(moveCandidate(r, 3, -1), [1, 3, 2, 4]);
  assert.equal(r.preferred_route_id, 2);
  assert.deepEqual(r.candidates.map(c => c.route_id), [3, 1, 2, 4]);
  assert.equal(moveCandidate(r, 1, -1), null);
  assert.deepEqual(attemptOrder({ ...r, forward_to: 'b' }, true), []);
});

test('全部转发支持多跳并排除成环、断链、跨接口和超长路径', () => {
  const rows = [row('a'), row('b', { forward_to: 'c', candidates: [] }), row('c'), row('cycle', { forward_to: 'a' }), row('dead', { candidates: [] }), row('other', { protocol: 'anthropic' })];
  assert.deepEqual(forwardTargets(rows[0], rows).map(r => r.model_name), ['b', 'c']);
  assert.deepEqual(forwardPath({ ...rows[0], forward_to: 'b' }, rows).map(r => r.model_name), ['b', 'c']);
  const long = Array.from({ length: 18 }, (_, i) => row(String(i), i < 17 ? { forward_to: String(i + 1) } : {}));
  assert.equal(forwardPath(long[0], long), null);
  assert.equal(forwardPath(long[1], long).length, 16);
});

test('当前起点仍可设为首选；已保存首选和全部转发不重复写入', async () => {
  const r = row('a', { candidates: [cand(1), cand(2)], preferred_route_id: 1, active_route_id: 2 });
  const calls = [];
  const context = vm.createContext({
    ...model, state: { routes: [r] },
    api: async (...args) => calls.push(args), toast: () => {},
    upstreamOfGroup: () => null, splitOneM: () => ({ bare: '' }),
  });
  const source = readFileSync(new URL('../web/canvas.js', import.meta.url), 'utf8')
    .replace(/^import [\s\S]*? from '[^']+';\r?\n/gm, '').replace(/^export /gm, '');
  vm.runInContext(source, context);
  await context.switchTo('a', 'openai', 2);
  assert.equal(calls.length, 1);
  assert.equal(calls[0][1], '/admin/api/models/switch');
  assert.equal(calls[0][2].route_id, 2);
  await context.switchTo('a', 'openai', 1);
  r.forward_to = 'b';
  await context.switchTo('a', 'openai', 2);
  assert.equal(calls.length, 1);
});
