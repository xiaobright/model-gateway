import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';
import vm from 'node:vm';

const springSource = readFileSync(new URL('../web/spring.js', import.meta.url), 'utf8')
  .replace(/^export /gm, '');
const motionSource = readFileSync(new URL('../web/motion.js', import.meta.url), 'utf8')
  .replace(/^import [\s\S]*? from '[^']+';\r?\n/gm, '')
  .replace(/^export \{[^}]*\} from '[^']+';\r?\n/gm, '')
  .replace(/^export /gm, '');
const source = springSource + '\n' + motionSource;
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
      let resolve, reject;
      const finished = new Promise((yes, no) => { resolve = yes; reject = no; });
      finished.catch(() => {});
      const animation = events({ frames, options, cancelled: false,
        finished,
        finish() { this.emit('finish'); resolve(); },
        cancel() { this.cancelled = true; this.emit('cancel'); reject(new Error('cancelled')); } });
      node.animated.push(animation);
      return animation;
    },
  };
  return node;
}
function harness(stored = null, { autoReady = true, springTokens = {}, dialogs = [] } = {}) {
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
    querySelectorAll: selector => selector === 'dialog' ? dialogs : [],
    startViewTransition(fn) {
      let settle, markReady, rejectReady, updateDone;
      const finished = new Promise((resolve) => { settle = resolve; });
      const ready = new Promise((resolve, reject) => { markReady = resolve; rejectReady = reject; });
      const updateCallbackDone = new Promise((resolve) => { updateDone = resolve; });
      const paint = () => { fn(); updateDone(); };
      transitions.push({ settle, paint, markReady, rejectReady });
      if (autoReady) Promise.resolve().then(() => { paint(); markReady(); });
      return { finished, ready, updateCallbackDone };
    },
  });
  const window = events({ matchMedia: query => query.includes('reduced-motion') ? reduced : pointer });
  const storage = { value: stored, getItem() { return this.value; }, setItem(_key, value) { this.value = value; } };
  let clock = 0;
  const context = vm.createContext({ window, document, localStorage: storage,
    performance: { now: () => clock },
    getComputedStyle: el => ({ opacity: '1', transform: 'none',
      getPropertyValue: key => el === root ? springTokens[key] || '' : '' }),
    ResizeObserver: class { observe() {} disconnect() {} },
    requestAnimationFrame: fn => { frames.set(++serial, fn); return serial; },
    cancelAnimationFrame: id => frames.delete(id),
  });
  vm.runInContext(source, context);
  const api = vm.runInContext('({ initMotion, toggleMotion, moveNavMarker, slideIn, updateNumber,'
    + ' initViewStage, showViewLayer, createIndicator, syncSegMarkers, readBarWidths, morphBars,'
    + ' enterHighlight, closeDialog, openDialog, springAnimate })', context);
  const control = api.initMotion();
  const flush = (step = 16) => {
    clock += step;
    const pending = [...frames.values()];
    frames.clear();
    pending.forEach(fn => fn(clock));
  };
  const settle = (limit = 240) => {
    let i = 0;
    while (frames.size && i < limit) { flush(); i++; }
    return frames.size === 0;
  };
  flush();
  const move = (target, x = 60, y = 80) => document.emit('pointermove', { target, clientX: x, clientY: y, pointerType: 'mouse' });
  return { api, control, document, window, frames, reduced, pointer, root, marker, selected, toggle, storage, flush, settle, move, transitions };
}

const tick = () => new Promise((resolve) => setTimeout(resolve, 0));

/* 一根条形：querySelector 交给测试填，fillOf 拿到真正的填充条 */
function bar(key, width) {
  const fill = { style: { width } };
  return { dataset: { key }, _fill: fill, querySelector: () => fill };
}

function viewSurface(name, visible = false) {
  const classes = new Set();
  return {
    dataset: { view: name }, hidden: !visible, inert: false, attrs: {}, style: {}, classes,
    classList: {
      add: (key) => classes.add(key),
      remove: (key) => classes.delete(key),
      contains: (key) => classes.has(key),
    },
    setAttribute(key, value) { this.attrs[key] = value; },
    removeAttribute(key) { delete this.attrs[key]; },
  };
}

function stageSurface(views) {
  const classes = new Set();
  return {
    dataset: {}, classes,
    classList: {
      add: (key) => classes.add(key),
      remove: (key) => classes.delete(key),
      contains: (key) => classes.has(key),
    },
    querySelectorAll: (selector) => (selector === '.view' ? views : []),
    contains: (el) => views.includes(el),
  };
}

const viewY = (el) => { const raw = (el.style.transform || '').replace('translate3d(', '').replace(')', '').split(','); return raw.length >= 2 ? parseFloat(raw[1]) : NaN; };

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
  assert.equal(h.marker.style.transform, 'translate3d(0.00px, 98.00px, 0)');
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

const markerX = (marker) => Number(/translate3d\((-?[\d.]+)px/.exec(marker.style.transform)?.[1]);

test('导航尺寸变化合并更新；连续改目标从当前位置继续', () => {
  const h = harness();
  Object.assign(h.selected, { offsetLeft: 0, offsetTop: 98, offsetWidth: 180, offsetHeight: 44 });
  h.api.moveNavMarker();
  h.flush();
  h.settle();
  Object.assign(h.selected, { offsetLeft: 108, offsetTop: 48, offsetWidth: 104 });
  h.api.moveNavMarker();
  h.api.moveNavMarker();
  assert.equal(h.frames.size, 1, '同一帧内的多次移动只排一次');
  h.flush();   // moveMarker 的 rAF：读位置、起弹簧
  h.flush();   // 弹簧第一帧
  const mid = markerX(h.marker);
  assert.ok(mid > 0 && mid < 108, `第一帧应已离开起点，实际 ${mid}`);

  Object.assign(h.selected, { offsetLeft: 0, offsetTop: 98, offsetWidth: 180 });
  h.api.moveNavMarker();
  assert.equal(markerX(h.marker), mid, '改目标不能把当前值清回起点');
  h.flush();
  const after = markerX(h.marker);
  assert.ok(after > 0 && after < 108, `反向途中也不能跳到任一端点，实际 ${after}`);
  h.settle();
  assert.match(h.marker.style.transform, /translate3d\(0\.00px, 98\.00px, 0\)/);
  assert.equal(h.marker.style.width, '180.00px');
});

test('视图舞台：新视图立刻可见可点，离场层断开命中并在落定后隐藏', () => {
  const h = harness();
  const views = ['a', 'b', 'c'].map((name, i) => viewSurface(name, i === 0));
  const stage = stageSurface(views);
  h.api.initViewStage(stage);
  assert.equal(h.api.showViewLayer(views[0], 'down', { initial: true }), false);
  assert.equal(views[0].hidden, false);

  assert.equal(h.api.showViewLayer(views[1], 'down'), true);
  assert.equal(views[1].hidden, false, '新视图当帧就要显示');
  assert.equal(views[0].classes.has('is-leaving'), true);
  assert.equal(views[0].inert, true);
  assert.equal(views[0].attrs['aria-hidden'], 'true');
  assert.match(views[1].style.transform, /translate3d\(0, 48\.00px/);

  h.flush();
  assert.equal(views[0].hidden, false, '离场层还在动，不能提前藏');
  h.settle();
  assert.equal(views[0].hidden, true);
  assert.equal(views[0].classes.has('is-leaving'), false);
  assert.equal(views[0].style.transform, '');
  assert.equal(views[1].style.transform, '', '落定后清掉内联 transform，不留下合成层');
  assert.equal(stage.dataset.moving, undefined);
});

test('视图舞台：连点第三个栏位时，半路的那层直接改目标，不回到起点', () => {
  const h = harness();
  const views = ['a', 'b', 'c'].map((name, i) => viewSurface(name, i === 0));
  const stage = stageSurface(views);
  h.api.initViewStage(stage);
  h.api.showViewLayer(views[0], 'down', { initial: true });

  h.api.showViewLayer(views[1], 'down');
  for (let i = 0; i < 3; i++) h.flush();
  const mid = viewY(views[1]);
  assert.ok(mid < 48 && mid > 0, `第二层应停在半路，实际 ${mid}`);

  assert.equal(h.api.showViewLayer(views[2], 'down'), true);
  assert.equal(viewY(views[1]), mid, '半路的那层不能被重新放到起点');
  assert.equal(views[1].classes.has('is-leaving'), true);
  assert.equal(views[2].hidden, false);
  assert.match(views[2].style.transform, /translate3d\(0, 48\.00px/);

  // 立刻点回第二层：它正从半路离场，应该原地反向，而不是重新从 +48 进来
  const before = viewY(views[1]);
  assert.equal(h.api.showViewLayer(views[1], 'up'), true);
  assert.equal(viewY(views[1]), before);
  assert.equal(views[1].classes.has('is-leaving'), false);
  h.settle();
  assert.equal(views[1].hidden, false);
  assert.equal(views[2].hidden, true);
  assert.equal(views[0].hidden, true, '更早的层应被收掉');
});

test('视图舞台：减弱动效直接切换，不排动画帧', () => {
  const h = harness();
  const views = ['a', 'b'].map((name, i) => viewSurface(name, i === 0));
  h.api.initViewStage(stageSurface(views));
  h.api.showViewLayer(views[0], 'down', { initial: true });
  h.reduced.matches = true;
  assert.equal(h.api.showViewLayer(views[1], 'down'), false);
  assert.equal(views[1].hidden, false);
  assert.equal(views[0].hidden, true);
  assert.equal(h.frames.size, 0);
});

test('JS 动画采用 CSS 的实际弹簧 token，默认曲线也在初始化后更新', () => {
  const curve = 'linear(0, 0.42 9%, 0.82 19%, 1.07 41%, 1)';
  const h = harness(null, { springTokens: { '--spring-bouncy': curve, '--spring-smooth': 'linear(0, 0.5 25%, 1)' } });
  const a = surface();
  h.api.slideIn(a);
  assert.equal(a.animated[0].options.easing, curve);
  h.api.springAnimate(a, [{ opacity: 0 }, { opacity: 1 }]);
  assert.equal(a.animated[1].options.easing, 'linear(0, 0.5 25%, 1)');
});

test('新记录的淡入和高亮同时执行，只有一条未取消的动画', () => {
  const h = harness(), a = surface();
  h.api.enterHighlight(a);
  assert.equal(a.animated.length, 1);
  assert.equal(a.animated[0].cancelled, false);
  assert.ok(a.animated[0].frames[0].opacity < 1);
  assert.match(a.animated[0].frames[0].transform, /translateY/);
  assert.equal(a.animated[0].frames[0].backgroundColor, 'var(--accent-wash)');
  assert.equal(a.animated[0].frames.at(-1).transform, 'none');
});

function dialogSurface() {
  return Object.assign(surface(), events({ open: true, inert: false, dataset: {}, closes: 0,
    close(value) { this.open = false; this.returnValue = value; this.closes++; },
    showModal() { this.open = true; }, getAnimations: () => [] }));
}

test('弹窗退场完成再关闭，重复关闭只执行一次，并保留确认结果', async () => {
  const h = harness(), dlg = dialogSurface();
  const closing = h.api.closeDialog(dlg, 'ok');
  assert.equal(h.api.closeDialog(dlg, 'cancel'), closing);
  assert.equal(dlg.open, true);
  assert.equal(dlg.inert, true);
  dlg.animated[0].finish();
  await closing;
  assert.equal(dlg.open, false);
  assert.equal(dlg.inert, false);
  assert.equal(dlg.dataset.closing, undefined);
  assert.equal(dlg.closes, 1);
  assert.equal(dlg.returnValue, 'ok');
});

test('关闭未完成就重开弹窗，旧动画结束不能关掉新窗口', async () => {
  const h = harness(), dlg = dialogSurface();
  const closing = h.api.closeDialog(dlg);
  h.api.openDialog(dlg);
  await closing;
  dlg.animated[0].finish();
  await tick();
  assert.equal(dlg.open, true);
  assert.equal(dlg.inert, false);
  assert.equal(dlg.closes, 0);
  assert.equal(dlg.animated[0].cancelled, true);
});

test('Esc 与确认框提交共用退场；切换系统减弱时直接完成关闭', async () => {
  const dlg = dialogSurface();
  const h = harness(null, { dialogs: [dlg] });
  let prevented = 0;
  dlg.emit('cancel', { preventDefault: () => prevented++ });
  h.reduced.matches = true;
  h.reduced.emit('change');
  await tick();
  assert.equal(dlg.open, false);
  assert.equal(prevented, 1);
  h.api.openDialog(dlg);
  h.document.emit('submit', { target: { method: 'dialog', closest: () => dlg },
    submitter: { value: 'ok' }, preventDefault: () => prevented++ });
  assert.equal(dlg.open, false);
  assert.equal(dlg.returnValue, 'ok');
  assert.equal(prevented, 2);
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
