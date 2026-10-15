/* 短交互使用有限动画；光感只服务当前指针下的一个容器，没有常驻 JS 帧循环。 */
export const reduceMotion = () => window.matchMedia('(prefers-reduced-motion: reduce)').matches;
const animations = new Map();
const spring = 'cubic-bezier(.22, 1, .36, 1)';
const animate = (el, frames, options = {}) => {
  if (!el || reduceMotion() || document.visibilityState === 'hidden' || typeof el.animate !== 'function') return;
  if (el.getClientRects && !el.getClientRects().length) return;
  animations.get(el)?.cancel();
  const animation = el.animate(frames, { duration: 320, easing: spring, ...options });
  animations.set(el, animation);
  const done = () => { if (animations.get(el) === animation) animations.delete(el); };
  animation.addEventListener('finish', done, { once: true });
  animation.addEventListener('cancel', done, { once: true });
  return animation;
};

export function enterStagger(els) {
  [...els].slice(0, 8).forEach((el, i) => animate(el,
    [{ opacity: .3, transform: 'translateY(8px)' }, { opacity: 1, transform: 'none' }], { delay: i * 28 }));
}

export function pulse(el, { duration = 460 } = {}) {
  animate(el, [{ backgroundColor: 'var(--accent-wash)' }, { backgroundColor: 'transparent' }], { duration });
}

export function slideIn(el) {
  animate(el, [{ opacity: .35, transform: 'translateY(7px)' }, { opacity: 1, transform: 'none' }]);
}

/* 只换透明度、不位移：给「内容换了但视口不许动」的场景用（如路由详情切换模型）。 */
export function fadeIn(el) {
  animate(el, [{ opacity: .4 }, { opacity: 1 }], { duration: 220 });
}

/* ---------------------------------------------------------------- 视图切换 */

/* 视图切换：新旧两块主区横向错开推过去，规则在 style.css 的 data-vt-dir 上。
   dir 只有 'down'（往后翻）和 'up'（往回翻）两种，按导航顺序定。
   主题切换走的是另一套 —— data-vt-theme 的圆形揭开要把所有 view-transition-name
   摘掉，两条路同时跑会互相抢名字、把页面撕开，所以互斥。
   返回是否真的套上了过渡，没套上时调用方自己补一个淡入。 */
let vtSeq = 0;

export function withViewTransition(dir, mutate) {
  const root = document.documentElement;
  if (reduceMotion() || typeof document.startViewTransition !== 'function' || root.dataset.vtTheme) {
    mutate();
    return false;
  }
  root.dataset.vtDir = dir;
  // 连点导航会把上一次的过渡打断，它的收尾不能把新过渡的方向擦掉
  const seq = ++vtSeq;
  const vt = document.startViewTransition(mutate);
  vt.finished
    .catch(() => { /* 切换被打断不算错误 */ })
    .finally(() => { if (seq === vtSeq) delete root.dataset.vtDir; });
  return true;
}

/* ---------------------------------------------------------------- 条形图形变 */

/* 条形列表换数据时让还认识的条从旧宽度长过来。重建 DOM 的话新元素一插进来就是
   目标宽度，CSS 过渡没有起点就不会触发，所以先按 data-key 记下旧宽度，重建后把
   对得上的条拉回旧值，下一帧再放开交给样式表里的 width 过渡。 */
export function readBarWidths(host, fillOf) {
  const prev = new Map();
  for (const el of host.children) {
    const fill = fillOf(el);
    if (fill) prev.set(el.dataset.key, fill.style.width);
  }
  return prev;
}

export function morphBars(host, prev, fillOf) {
  if (!prev || !prev.size || reduceMotion()) return;
  const pending = [];
  for (const row of host.children) {
    const old = prev.get(row.dataset.key);
    const fill = fillOf(row);
    if (!old || !fill || fill.style.width === old) continue;
    const target = fill.style.width;
    fill.style.transition = 'none';
    fill.style.width = old;
    pending.push({ fill, target });
  }
  if (!pending.length) return;
  requestAnimationFrame(() => {
    for (const { fill, target } of pending) {
      fill.style.transition = '';
      fill.style.width = target;
    }
  });
}

export function updateNumber(el, text) {
  if (!el || el.textContent === text) return;
  el.textContent = text;
  animate(el, [{ opacity: .35, transform: 'translateY(5px)' }, { opacity: 1, transform: 'none' }], { duration: 260 });
}

let controller = null;
export function moveNavMarker() { controller?.moveMarker(); }
export function toggleMotion() { return controller?.toggle(); }

export function initMotion() {
  controller?.dispose();
  const root = document.documentElement;
  const nav = document.getElementById('nav');
  const marker = document.getElementById('nav-marker');
  const toggle = document.getElementById('motion-toggle');
  const reduced = window.matchMedia('(prefers-reduced-motion: reduce)');
  const pointer = window.matchMedia('(hover: hover) and (pointer: fine)');
  let mode = 'standard';
  try { if (localStorage.getItem('mg-motion') === 'light') mode = 'light'; } catch { /* 隐私模式使用默认值。 */ }
  let active = null, point = null, glowFrame = 0, navFrame = 0;
  const listeners = [];
  const on = (target, type, fn, options) => {
    target.addEventListener(type, fn, options);
    listeners.push(() => target.removeEventListener(type, fn, options));
  };
  const visible = () => document.visibilityState !== 'hidden';
  const lightEnabled = () => visible() && !reduced.matches && pointer.matches && mode === 'standard';
  const clearLight = () => {
    if (glowFrame) cancelAnimationFrame(glowFrame);
    glowFrame = 0;
    active?.classList.remove('glass-lit');
    active = point = null;
  };
  const stopAnimations = () => {
    for (const animation of animations.values()) animation.cancel();
    animations.clear();
  };
  const moveMarker = () => {
    if (navFrame || !visible() || !nav || !marker) return;
    navFrame = requestAnimationFrame(() => {
      navFrame = 0;
      const selected = nav.querySelector('.is-active');
      if (!selected) return;
      marker.style.width = selected.offsetWidth + 'px';
      marker.style.height = selected.offsetHeight + 'px';
      marker.style.transform = `translate(${selected.offsetLeft}px, ${selected.offsetTop}px)`;
      marker.style.opacity = '1';
    });
  };
  const sync = () => {
    root.dataset.motion = mode;
    root.toggleAttribute('data-page-hidden', !visible());
    if (toggle) {
      toggle.textContent = reduced.matches ? '动效 · 跟随系统减弱' : `动效 · ${mode === 'light' ? '轻量' : '标准'}`;
      toggle.disabled = reduced.matches;
      toggle.setAttribute('aria-label', reduced.matches ? '系统已开启减少动态效果' : `当前为${mode === 'light' ? '轻量' : '标准'}动效，点击切换`);
    }
    clearLight();
    if (!visible() || reduced.matches) {
      stopAnimations();
      if (navFrame) cancelAnimationFrame(navFrame);
      navFrame = 0;
    }
    if (visible()) moveMarker();
  };
  on(document, 'pointermove', (event) => {
    if (!lightEnabled() || event.pointerType === 'touch' || document.querySelector('dialog[open]')) { clearLight(); return; }
    const target = event.target.closest?.('.route-table, .route-detail, .card, .kpi');
    if (!target) { clearLight(); return; }
    if (target !== active) { clearLight(); active = target; }
    point = { x: event.clientX, y: event.clientY };
    if (glowFrame) return;
    glowFrame = requestAnimationFrame(() => {
      glowFrame = 0;
      if (!active?.isConnected || !lightEnabled()) { clearLight(); return; }
      const rect = active.getBoundingClientRect();
      active.style.setProperty('--glow-x', `${point.x - rect.left}px`);
      active.style.setProperty('--glow-y', `${point.y - rect.top}px`);
      active.classList.add('glass-lit');
    });
  }, { passive: true });
  on(document, 'pointerout', (event) => {
    if (active && !active.contains(event.relatedTarget)) clearLight();
  }, { passive: true });
  on(document, 'pointerdown', clearLight, { passive: true });
  on(document, 'keydown', clearLight);
  on(document, 'toggle', clearLight, true);
  on(document, 'scroll', clearLight, { passive: true, capture: true });
  on(document, 'visibilitychange', sync);
  on(window, 'blur', clearLight);
  on(window, 'resize', () => { clearLight(); moveMarker(); }, { passive: true });
  on(reduced, 'change', sync);
  on(pointer, 'change', sync);
  const observer = new ResizeObserver(moveMarker);
  if (nav) observer.observe(nav);
  controller = {
    moveMarker,
    toggle() {
      if (!reduced.matches) mode = mode === 'standard' ? 'light' : 'standard';
      try { localStorage.setItem('mg-motion', mode); } catch { /* 当前会话仍然生效。 */ }
      sync();
      return mode;
    },
    dispose() {
      clearLight();
      stopAnimations();
      if (navFrame) cancelAnimationFrame(navFrame);
      observer.disconnect();
      listeners.forEach(off => off());
    },
  };
  sync();
  return controller;
}
