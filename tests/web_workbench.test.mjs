import assert from 'node:assert/strict';
import test from 'node:test';
import { readFileSync } from 'node:fs';
import vm from 'node:vm';
import { routeSummary } from '../web/route-view.js';
import { requestResult } from '../web/request-result.js';
import * as util from '../web/util.js';
import * as canvasModel from '../web/canvas-model.js';

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
  for (const note of ['truncated', 'hold_retry', 'upstream_abort', 'stall_timeout', 'failed_over', 'protocol_error']) {
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
  assert.equal(requestResult({ status: 200, note: 'protocol_error' }).label, '上游协议错误');
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

/* 路由视图的滚动钉死：内容长度变化不许把用户的视口夹回去。
   文档高度用变量模拟：get scrollHeight 时按浏览器规则把 scrollTop 夹到
   「总高 - 视口」内，替换 innerHTML 后的强制布局就会走到这条夹紧逻辑。 */
function routePadHarness({ desktop = true } = {}) {
  const source = readFileSync(new URL('../web/route-view.js', import.meta.url), 'utf8')
    .replace(/^import [\s\S]*? from '[^']+';\r?\n/gm, '').replace(/^export /gm, '');
  Object.assign(util.state, {
    routes: [], filter: '', iface: '', overview: null,
    protocolEnabled: { openai: true }, failover: { enabled: {} },
  });
  const elements = new Map();
  const element = (id) => {
    if (!elements.has(id)) elements.set(id, {
      innerHTML: '', textContent: '', scrollTop: 0, style: {}, dataset: {},
      contains: () => false, querySelectorAll: () => [], focus: () => {},
      classList: { toggle: () => {} },
    });
    return elements.get(id);
  };
  const listeners = new Map();
  const scrolls = [];
  let natural = 1500;
  const win = {
    innerHeight: 800, scrollY: 700,
    addEventListener: (type, fn) => listeners.set(type, fn),
    matchMedia: (q) => ({ matches: desktop ? q.includes('min-width') : q.includes('max-width') }),
    scrollTo: (_x, y) => { win.scrollY = y; scrolls.push(y); },
    requestAnimationFrame: (fn) => fn(),
  };
  const pad = element('route-pad');
  const documentElement = {
    get scrollHeight() {
      const total = natural + (parseFloat(pad.style.height) || 0);
      win.scrollY = Math.min(win.scrollY, Math.max(0, total - win.innerHeight));
      return total;
    },
  };
  const context = vm.createContext({
    ...util, ...canvasModel, routeKey: canvasModel.modelKey, fadeIn: () => {},
    requestAnimationFrame: (fn) => fn(),
    $: element, document: { documentElement, activeElement: null }, window: win,
  });
  vm.runInContext(source, context, { filename: 'web/route-view.js' });
  vm.runInContext('initRoutePad()', context);
  return {
    context, pad, win, scrolls, listeners, element,
    setNatural: (n) => { natural = n; },
    routes: (list) => { util.state.routes = list; },
  };
}

const tallRow = () => row({ model_name: 'tall', candidates: [candidate(1), candidate(2), candidate(3)] });
const shortRow = () => row({ model_name: 'short', candidates: [candidate(4)] });

test('右卡变短时钉死视口：底部留白兜住文档高度，不把用户夹回去', () => {
  const h = routePadHarness();
  h.routes([tallRow(), shortRow()]);
  h.setNatural(1500);
  vm.runInContext('renderRouteWorkspace()', h.context);
  assert.equal(h.pad.style.height, undefined, '高度够用就不留白');
  assert.deepEqual(h.scrolls, [], '没被夹过就不该有滚动');

  h.setNatural(1000);   // 换到只有 1 个候选的模型，文档变矮
  vm.runInContext("selectRouteWorkspace('short', 'openai')", h.context);
  assert.equal(h.pad.style.height, '500px', '留白补到 700 + 800');
  assert.equal(h.win.scrollY, 700, '视口一个像素都没动');
  assert.deepEqual(h.scrolls, [700], '被布局夹走后在同一次任务里夹回原位');
});

test('留白在用户往上滑时按需收回，滑到底后滚不回留白', () => {
  const h = routePadHarness();
  h.routes([tallRow(), shortRow()]);
  h.setNatural(1000);
  vm.runInContext("selectRouteWorkspace('short', 'openai')", h.context);
  assert.equal(h.pad.style.height, '500px');

  h.win.scrollY = 400;   // 用户往上滑
  h.listeners.get('scroll')();
  assert.equal(h.pad.style.height, '200px', '留白跟着收缩');
  h.win.scrollY = 0;
  h.listeners.get('scroll')();
  assert.equal(h.pad.style.height, '0px', '滑到顶全部收回');

  h.win.scrollY = 200;   // 到底了，再往下滚也没有留白可去
  h.listeners.get('scroll')();
  assert.equal(h.pad.style.height, '0px');
});

test('内容再次变高时撤掉留白，又能一路滑到底', () => {
  const h = routePadHarness();
  h.routes([tallRow(), shortRow()]);
  h.setNatural(1000);
  vm.runInContext("selectRouteWorkspace('short', 'openai')", h.context);
  h.win.scrollY = 0;
  h.listeners.get('scroll')();
  assert.equal(h.pad.style.height, '0px');

  h.setNatural(1500);
  h.win.scrollY = 700;   // 换回候选多的模型，重新滑到底
  vm.runInContext("selectRouteWorkspace('tall', 'openai')", h.context);
  assert.equal(h.pad.style.height, '0px', '内容高过视口就不需要留白');
  assert.equal(h.win.scrollY, 700, '位置仍然没动');
});

test('窄屏不留白，点选后照旧 scrollIntoView 到详情', () => {
  const h = routePadHarness({ desktop: false });
  h.routes([tallRow(), shortRow()]);
  let intoView = 0;
  h.element('route-detail').scrollIntoView = () => { intoView++; };
  h.setNatural(1000);
  h.win.scrollY = 700;
  vm.runInContext("selectRouteWorkspace('short', 'openai')", h.context);
  assert.equal(intoView, 1, '窄屏仍把详情滚进视口');
  assert.equal(h.pad.style.height, undefined, '窄屏整个跳过钉高度');
  assert.equal(h.win.scrollY, 700, 'pin 没跑，滚动交给浏览器');
});
