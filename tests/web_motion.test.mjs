import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';
import vm from 'node:vm';

const source = readFileSync(new URL('../web/motion.js', import.meta.url), 'utf8').replace(/^export /gm, '');
function events(extra = {}) {
  const listeners = new Map();
  return Object.assign({
    addEventListener(type, fn) { if (!listeners.has(type)) listeners.set(type, new Set()); listeners.get(type).add(fn); },
    removeEventListener(type, fn) { listeners.get(type)?.delete(fn); },
    emit(type, value = {}) { for (const fn of listeners.get(type) || []) fn(value); },
  }, extra);
}
function surface() {
  const classes = new Set();
  const node = {
    isConnected: true, reads: 0, writes: 0, textContent: '', animated: [], classes,
    style: { setProperty(key, value) { this[key] = value; node.writes++; } },
    classList: { add: key => classes.add(key), remove: key => classes.delete(key) },
    closest: () => node, contains: target => target === node,
    getBoundingClientRect() { node.reads++; return { left: 10, top: 20 }; },
    getClientRects: () => [{}],
    animate(frames, options) {
      const animation = events({ frames, options, cancelled: false,
        cancel() { this.cancelled = true; this.emit('cancel'); } });
      node.animated.push(animation);
      return animation;
    },
  };
  return node;
}
function harness(stored = null) {
  let serial = 0;
  const frames = new Map();
  const reduced = events({ matches: false });
  const pointer = events({ matches: true });
  const root = { dataset: {}, toggleAttribute(key, on) { this[key] = on; } };
  const marker = { style: {} };
  const selected = { offsetWidth: 180, offsetHeight: 44, offsetLeft: 0, offsetTop: 98 };
  const nav = { querySelector: () => selected };
  const toggle = { setAttribute() {} };
  const transitions = [];
  const document = events({ documentElement: root, visibilityState: 'visible', modal: false,
    getElementById: id => ({ nav, 'nav-marker': marker, 'motion-toggle': toggle })[id],
    querySelector() { return this.modal ? {} : null; },
    startViewTransition(fn) {
      fn();
      let settle;
      const finished = new Promise((resolve) => { settle = resolve; });
      transitions.push({ settle });
      return { finished };
    },
  });
  const window = events({ matchMedia: query => query.includes('reduced-motion') ? reduced : pointer });
  const storage = { value: stored, getItem() { return this.value; }, setItem(_key, value) { this.value = value; } };
  const context = vm.createContext({ window, document, localStorage: storage,
    ResizeObserver: class { observe() {} disconnect() {} },
    requestAnimationFrame: fn => { frames.set(++serial, fn); return serial; },
    cancelAnimationFrame: id => frames.delete(id),
  });
  vm.runInContext(source, context);
  const api = vm.runInContext('({ initMotion, toggleMotion, moveNavMarker, slideIn, updateNumber,'
    + ' withViewTransition, readBarWidths, morphBars })', context);
  const control = api.initMotion();
  const flush = () => { const pending = [...frames.values()]; frames.clear(); pending.forEach(fn => fn()); };
  flush();
  const move = (target, x = 60, y = 80) => document.emit('pointermove', { target, clientX: x, clientY: y, pointerType: 'mouse' });
  return { api, control, document, window, frames, reduced, pointer, root, marker, selected, toggle, storage, flush, move, transitions };
}

const tick = () => new Promise((resolve) => setTimeout(resolve, 0));

/* 一根条形：querySelector 交给测试填，fillOf 拿到真正的填充条 */
function bar(key, width) {
  const fill = { style: { width } };
  return { dataset: { key }, _fill: fill, querySelector: () => fill };
}

test('高频指针移动合并成一帧，只计算并点亮最后一个容器，不自行续帧', () => {
  const h = harness(), a = surface(), b = surface();
  for (let i = 0; i < 40; i++) h.move(a, i, i);
  assert.equal(h.frames.size, 1);
  assert.equal(a.reads, 0);
  h.move(b, 100, 120);
  h.flush();
  assert.equal(a.reads, 0);
  assert.equal(b.reads, 1);
  assert.equal(b.style['--glow-x'], '90px');
  assert.equal(b.style['--glow-y'], '100px');
  assert.equal(b.classes.has('glass-lit'), true);
  assert.equal(h.frames.size, 0);
  h.move(a); h.flush();
  assert.equal(b.classes.size, 0);
  assert.equal(a.classes.size, 1);
});

test('离开、滚动、打开弹窗会取消待执行的光感，不更新已经移除的面板', () => {
  const h = harness(), a = surface();
  for (const type of ['pointerout', 'scroll', 'toggle', 'pointerdown']) {
    h.move(a); h.document.emit(type, { relatedTarget: null }); h.flush();
    assert.equal(a.reads, 0);
    assert.equal(a.classes.size, 0);
  }
  h.document.modal = true; h.move(a); h.flush();
  assert.equal(a.reads, 0);
  h.document.modal = false; h.move(a); a.isConnected = false; h.flush();
  assert.equal(a.reads, 0);
});

test('后台取消光感、导航帧和有限动画；返回前台只补一次导航布局', () => {
  const h = harness(), a = surface();
  h.api.slideIn(a); h.move(a); h.api.moveNavMarker();
  h.document.visibilityState = 'hidden'; h.document.emit('visibilitychange');
  assert.equal(h.root['data-page-hidden'], true);
  assert.equal(h.frames.size, 0);
  assert.equal(a.animated[0].cancelled, true);
  h.move(a); h.api.slideIn(a);
  assert.equal(h.frames.size, 0);
  assert.equal(a.animated.length, 1);
  h.document.visibilityState = 'visible'; h.document.emit('visibilitychange'); h.flush();
  assert.equal(h.root['data-page-hidden'], false);
  assert.equal(h.marker.style.transform, 'translate(0px, 98px)');
  assert.equal(h.frames.size, 0);
});

test('轻量模式持久化，保留短交互；系统减弱和粗指针优先停止光感', () => {
  const h = harness('light'), a = surface();
  h.move(a); h.flush();
  assert.equal(a.reads, 0);
  h.api.slideIn(a);
  assert.equal(a.animated.length, 1);
  h.api.toggleMotion(); h.flush();
  assert.equal(h.storage.value, 'standard');
  h.move(a); h.flush();
  assert.equal(a.classes.size, 1);
  h.reduced.matches = true; h.reduced.emit('change'); h.flush();
  assert.equal(a.classes.size, 0);
  assert.equal(h.toggle.disabled, true);
  assert.equal(a.animated[0].cancelled, true);
  h.api.slideIn(a);
  assert.equal(a.animated.length, 1);
  h.reduced.matches = false; h.reduced.emit('change');
  h.pointer.matches = false; h.pointer.emit('change'); h.flush();
  h.move(a); h.flush();
  assert.equal(a.reads, 1);
});

test('同值数字不重播，新值中止旧动画；隐藏元素不播放', () => {
  const h = harness(), a = surface();
  h.api.updateNumber(a, '42'); h.api.updateNumber(a, '42');
  assert.equal(a.animated.length, 1);
  h.api.updateNumber(a, '43');
  assert.equal(a.animated[0].cancelled, true);
  assert.equal(a.textContent, '43');
  a.getClientRects = () => [];
  h.api.updateNumber(a, '44');
  assert.equal(a.animated.length, 2);
  h.control.dispose();
  assert.equal(a.animated[1].cancelled, true);
  h.move(a); h.flush();
  assert.equal(a.reads, 0);
});

test('导航尺寸变化合并更新，支持移动端两行位置', () => {
  const h = harness();
  Object.assign(h.selected, { offsetLeft: 108, offsetTop: 48, offsetWidth: 104 });
  h.api.moveNavMarker(); h.api.moveNavMarker();
  assert.equal(h.frames.size, 1);
  h.flush();
  assert.equal(h.marker.style.transform, 'translate(108px, 48px)');
  assert.equal(h.marker.style.width, '104px');
  assert.equal(h.frames.size, 0);
});

test('视图切换：挂上方向、同步改 DOM，收尾后摘掉', async () => {
  const h = harness();
  const order = [];
  assert.equal(h.api.withViewTransition('down', () => order.push('painted')), true);
  assert.equal(h.root.dataset.vtDir, 'down');
  assert.deepEqual(order, ['painted'], 'DOM 变更必须在挂上方向之后、过渡之内完成');
  assert.equal(h.transitions.length, 1);
  h.transitions[0].settle();
  await tick();
  assert.equal(h.root.dataset.vtDir, undefined);
});

test('连点导航：被打断的旧过渡收尾不能擦掉新方向', async () => {
  const h = harness();
  h.api.withViewTransition('down', () => {});
  h.api.withViewTransition('up', () => {});
  assert.equal(h.root.dataset.vtDir, 'up');
  h.transitions[0].settle();     // 第一次的 finished 才到
  await tick();
  assert.equal(h.root.dataset.vtDir, 'up');
  h.transitions[1].settle();
  await tick();
  assert.equal(h.root.dataset.vtDir, undefined);
});

test('减弱动效或主题揭开进行中，视图切换不套过渡并告诉调用方', () => {
  const h = harness();
  let painted = 0;
  h.reduced.matches = true;
  assert.equal(h.api.withViewTransition('down', () => painted++), false);
  assert.equal(painted, 1);
  assert.equal(h.transitions.length, 0);
  assert.equal(h.root.dataset.vtDir, undefined);
  h.reduced.matches = false;
  h.root.dataset.vtTheme = '1';
  assert.equal(h.api.withViewTransition('up', () => painted++), false);
  assert.equal(painted, 2);
  assert.equal(h.transitions.length, 0);
  assert.equal(h.root.dataset.vtDir, undefined, '两条过渡互斥，谁也不能把方向挂上');
});

test('条形形变：按 data-key 把旧宽度还给新条，下一帧放开到目标宽度', () => {
  const h = harness();
  const host = { children: [bar('a', '40%'), bar('b', '10%')] };
  const prev = h.api.readBarWidths(host, (el) => el._fill);
  assert.equal(prev.size, 2);
  assert.equal(prev.get('a'), '40%');
  assert.equal(prev.get('b'), '10%');

  const after = [bar('a', '90%'), bar('c', '30%')];
  host.children = after;
  h.api.morphBars(host, prev, (el) => el._fill);
  assert.equal(after[0]._fill.style.width, '40%', '先拉回旧宽度当起点，否则没有过渡');
  assert.equal(after[0]._fill.style.transition, 'none');
  assert.equal(after[1]._fill.style.width, '30%', '新条没有旧宽度，直接给目标值');
  assert.equal(h.frames.size, 1, '只排一帧，不自行续帧');
  h.flush();
  assert.equal(after[0]._fill.style.width, '90%');
  assert.equal(after[0]._fill.style.transition, '');
  assert.equal(h.frames.size, 0);
});

test('条形形变：没有对得上的条就不排帧，减弱动效下直接保持目标宽度', () => {
  const h = harness();
  const host = { children: [bar('a', '40%')] };
  const prev = h.api.readBarWidths(host, (el) => el._fill);
  host.children = [bar('z', '30%')];
  h.api.morphBars(host, prev, (el) => el._fill);
  assert.equal(host.children[0]._fill.style.width, '30%');
  assert.equal(h.frames.size, 0);

  h.reduced.matches = true;
  const again = { children: [bar('a', '40%'), bar('b', '80%')] };
  h.api.morphBars(again, prev, (el) => el._fill);
  assert.equal(again.children[1]._fill.style.width, '80%');
  assert.equal(h.frames.size, 0);
});
