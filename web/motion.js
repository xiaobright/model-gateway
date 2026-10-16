/* 全局丝滑物理弹簧动效引擎（Model Gateway Motion System）
   统一管理：
   1. 物理弹簧曲线（snappy / smooth / bouncy / gentle）与有限时长 WAAPI 动画
   2. 侧栏导航胶囊（#nav-marker）与全站分段控制器胶囊（.seg::before）的弹簧滑动
   3. 主视图切换（View Transitions API + 视图内部区块级联弹簧入场）
   4. 列表 / 候选卡片 FLIP（First-Last-Invert-Play）物理重排过渡
   5. 详情面板切换、表格分组折叠展开、弹窗与折叠面板（<details>）、Toast 退场 */

export const reduceMotion = () => Boolean(window.matchMedia?.('(prefers-reduced-motion: reduce)')?.matches);

export const SPRING = {
  snappy: 'cubic-bezier(0.22, 1.18, 0.36, 1)',
  smooth: 'cubic-bezier(0.16, 1, 0.3, 1)',
  bouncy: 'cubic-bezier(0.22, 1.36, 0.36, 1)',
  gentle: 'cubic-bezier(0.2, 0.96, 0.28, 1)',
  exit: 'cubic-bezier(0.32, 0, 0.67, 0)',
};

const animations = new Map();

// CSS token 是页面实际使用的曲线（包括 linear() 回弹）；JS 动画沿用同一套值。
function syncSpringCurves() {
  if (typeof getComputedStyle !== 'function') return;
  const style = getComputedStyle(document.documentElement);
  for (const name of Object.keys(SPRING)) {
    SPRING[name] = style.getPropertyValue(`--spring-${name}`).trim() || SPRING[name];
  }
}
syncSpringCurves();

export function springAnimate(el, frames, options = {}) {
  if (!el) return null;
  animations.get(el)?.cancel();
  if (reduceMotion() || document.visibilityState === 'hidden' || typeof el.animate !== 'function') return null;
  if (el.getClientRects && !el.getClientRects().length) return null;
  const animation = el.animate(frames, { duration: 360, easing: SPRING.smooth, ...options });
  animations.set(el, animation);
  const done = () => { if (animations.get(el) === animation) animations.delete(el); };
  animation.addEventListener?.('finish', done, { once: true });
  animation.addEventListener?.('cancel', done, { once: true });
  return animation;
}

const animate = springAnimate;

/* ---------------------------------------------------------------- 基础元素动效 */

export function enterStagger(els, options = {}) {
  if (!els) return;
  const {
    step = 26,
    limit = 14,
    duration = 380,
    distance = 10,
    scale = 0.985,
    axis = 'y',
    easing = SPRING.bouncy,
  } = options;
  const list = [...els].filter((el) => el && !el.hidden);
  list.slice(0, limit).forEach((el, i) => {
    const shift = axis === 'x' ? `translate3d(${distance}px, 0, 0)` : `translate3d(0, ${distance}px, 0)`;
    animate(
      el,
      [
        { opacity: 0.22, transform: `${shift} scale(${scale})` },
        { opacity: 1, transform: 'none' },
      ],
      { duration, delay: i * step, easing, fill: 'backwards' },
    );
  });
}

export function pulse(el, { duration = 520 } = {}) {
  animate(
    el,
    [
      { backgroundColor: 'var(--accent-wash)', transform: 'scale(0.992)' },
      { backgroundColor: 'var(--accent-wash)', transform: 'scale(1.008)', offset: 0.38 },
      { backgroundColor: 'transparent', transform: 'none' },
    ],
    { duration, easing: SPRING.bouncy },
  );
}

export function slideIn(el, { duration = 360, distance = 10, axis = 'y' } = {}) {
  const shift = axis === 'x' ? `translate3d(${distance}px, 0, 0)` : `translate3d(0, ${distance}px, 0)`;
  animate(
    el,
    [
      { opacity: 0.28, transform: `${shift} scale(0.985)` },
      { opacity: 1, transform: 'none' },
    ],
    { duration, easing: SPRING.bouncy },
  );
}

/** 新记录的入场和高亮放在同一条动画里，避免后者取消前者。 */
export function enterHighlight(el) {
  animate(el, [
    { opacity: 0.28, transform: 'translateY(10px) scale(0.985)', backgroundColor: 'var(--accent-wash)' },
    { opacity: 1, transform: 'scale(1.008)', backgroundColor: 'var(--accent-wash)', offset: 0.55 },
    { opacity: 1, transform: 'none', backgroundColor: 'transparent' },
  ], { duration: 520, easing: SPRING.bouncy });
}

/* 只做轻量丝滑过渡、不大幅位移外框：给「内容换了但视口不许跳动」的场景用（如路由详情切换模型）。 */
export function fadeIn(el, { duration = 260 } = {}) {
  animate(
    el,
    [
      { opacity: 0.38, transform: 'translateY(4px) scale(0.994)' },
      { opacity: 1, transform: 'none' },
    ],
    { duration, easing: SPRING.smooth },
  );
}

/* ---------------------------------------------------------------- 详情面板与视图级联入场 */

/** 选中不同模型时，详情面板内部头部、摘要与候选卡片做物理弹簧级联过渡 */
export function animateDetailSwap(detailEl) {
  if (!detailEl || reduceMotion()) return;
  const head = detailEl.querySelector('.detail-head');
  const forward = detailEl.querySelector('.route-forward');
  const facts = detailEl.querySelector('.route-facts');
  const cands = detailEl.querySelectorAll('.candidate');
  const empty = detailEl.querySelector('.empty');

  if (head) {
    animate(
      head,
      [{ opacity: 0.35, transform: 'translate3d(8px, 0, 0)' }, { opacity: 1, transform: 'none' }],
      { duration: 320, easing: SPRING.smooth },
    );
  }
  if (forward) {
    animate(
      forward,
      [{ opacity: 0.3, transform: 'translate3d(0, 6px, 0) scale(0.985)' }, { opacity: 1, transform: 'none' }],
      { duration: 340, delay: 25, easing: SPRING.bouncy, fill: 'backwards' },
    );
  }
  if (facts) {
    animate(
      facts,
      [{ opacity: 0.35, transform: 'translate3d(0, 6px, 0)' }, { opacity: 1, transform: 'none' }],
      { duration: 340, delay: 40, easing: SPRING.smooth, fill: 'backwards' },
    );
  }
  if (cands.length) {
    enterStagger(cands, { step: 30, duration: 380, distance: 9, scale: 0.98 });
  } else if (empty) {
    slideIn(empty, { duration: 320, distance: 8 });
  }
}

/** 切换主视图或子视图（如 路由列表 <-> 编排视图）时，级联驱动新视图内各区块的物理弹簧入场 */
export function animateViewEnter(viewEl, dir = 'down', { withVt = false } = {}) {
  if (!viewEl || reduceMotion()) return;
  const blocks = [
    viewEl.querySelector('.view-head'),
    viewEl.querySelector('.workspace-status'),
    viewEl.querySelector('.route-filters'),
    viewEl.querySelector('.log-filters'),
    ...viewEl.querySelectorAll(':scope > .kpis > .kpi'),
    ...viewEl.querySelectorAll(':scope > .route-workspace > *'),
    ...viewEl.querySelectorAll(':scope > .card, :scope > .cols > .card'),
  ].filter((el) => el && !el.hidden);

  const isHoriz = dir === 'left' || dir === 'right';
  const sign = dir === 'up' || dir === 'left' ? -1 : 1;
  const dist = (withVt ? 8 : 14) * sign;
  const startOpacity = withVt ? 0.78 : 0.25;

  blocks.slice(0, 10).forEach((el, i) => {
    const shift = isHoriz
      ? `translate3d(${dist}px, 0, 0)`
      : `translate3d(0, ${dist}px, 0)`;
    animate(
      el,
      [
        { opacity: startOpacity, transform: `${shift} scale(0.99)` },
        { opacity: 1, transform: 'none' },
      ],
      {
        duration: 420,
        delay: i * 24,
        easing: SPRING.bouncy,
        fill: 'backwards',
      },
    );
  });

}

/* ---------------------------------------------------------------- FLIP 物理布局重排 */

/** 记录容器内一组带 key 元素的屏幕坐标，供 DOM 更新后执行 FLIP 弹簧位移动画 */
export function captureRects(host, selector, keyOf) {
  const map = new Map();
  if (!host || reduceMotion() || typeof host.querySelectorAll !== 'function') return map;
  for (const el of host.querySelectorAll(selector)) {
    if (el.hidden || !el.getClientRects?.().length) continue;
    const key = keyOf(el);
    if (key) map.set(key, el.getBoundingClientRect());
  }
  return map;
}

/** 在 DOM 更新后对比新旧坐标，对发生位置移动的元素播放物理弹簧平滑归位动画 */
export function flipElements(host, selector, keyOf, prevRects, { duration = 420, staggerNew = false } = {}) {
  if (!host || !prevRects || !prevRects.size || reduceMotion() || typeof host.querySelectorAll !== 'function') return;
  const fresh = [];
  for (const el of host.querySelectorAll(selector)) {
    if (el.hidden || !el.getClientRects?.().length) continue;
    const key = keyOf(el);
    const oldRect = key ? prevRects.get(key) : null;
    if (!oldRect) {
      fresh.push(el);
      continue;
    }
    const newRect = el.getBoundingClientRect();
    const dx = oldRect.left - newRect.left;
    const dy = oldRect.top - newRect.top;
    if (Math.abs(dx) < 1 && Math.abs(dy) < 1) continue;
    animate(
      el,
      [
        { transform: `translate3d(${dx}px, ${dy}px, 0) scale(1.015)`, zIndex: 2 },
        { transform: 'none', zIndex: 'auto' },
      ],
      { duration, easing: SPRING.bouncy },
    );
  }
  if (staggerNew && fresh.length) {
    enterStagger(fresh, { step: 24, duration: 340, distance: 8 });
  }
}

/* ---------------------------------------------------------------- 表格分组展开与折叠面板 */

const rowMotions = new Map();

function animateRowHeights(rows, opening) {
  const running = [], wrappers = [];
  if (!rows?.length || reduceMotion() || document.visibilityState === 'hidden') {
    return { finished: Promise.resolve(), cancel() {} };
  }
  const list = [...rows], current = new Map();
  // 快速展开后立即收起时，先记住可见高度再拆掉上一轮临时壳，避免壳互相嵌套。
  for (const row of list) {
    if (!rowMotions.has(row)) continue;
    for (const cell of row.cells) {
      const style = getComputedStyle(cell);
      current.set(cell, { height: `${cell.firstElementChild.getBoundingClientRect().height}px`,
        padding: { paddingTop: style.paddingTop, paddingBottom: style.paddingBottom, borderBottomWidth: style.borderBottomWidth } });
    }
  }
  for (const previous of new Set(list.map(row => rowMotions.get(row)))) previous?.cancel();
  for (const [i, row] of list.entries()) {
    for (const cell of row.cells) {
      const wrap = document.createElement('div');
      wrap.style.overflow = 'hidden';
      while (cell.firstChild) wrap.append(cell.firstChild);
      cell.append(wrap);
      wrappers.push({ cell, wrap });
      const style = getComputedStyle(cell);
      const full = { paddingTop: style.paddingTop, paddingBottom: style.paddingBottom, borderBottomWidth: style.borderBottomWidth };
      const zero = { paddingTop: '0px', paddingBottom: '0px', borderBottomWidth: '0px' };
      const height = `${wrap.offsetHeight}px`;
      const bodyFull = { height, opacity: 1 }, bodyZero = { height: '0px', opacity: 0 };
      const start = current.get(cell);
      const options = { duration: opening ? 300 : 180, delay: opening ? Math.min(i, 8) * 18 : 0,
        easing: opening ? SPRING.smooth : SPRING.exit, fill: 'both' };
      running.push(animate(cell, [start?.padding || (opening ? zero : full), opening ? full : zero], options));
      running.push(animate(wrap, [start ? { height: start.height, opacity: 1 } : (opening ? bodyZero : bodyFull), opening ? bodyFull : bodyZero], options));
    }
  }
  const clean = () => {
    for (const { cell, wrap } of wrappers) {
      while (wrap.firstChild) cell.insertBefore(wrap.firstChild, wrap);
      wrap.remove();
    }
    wrappers.length = 0;
    for (const animation of running) animation?.cancel();
    for (const row of list) if (rowMotions.get(row) === control) rowMotions.delete(row);
  };
  const finished = Promise.all(running.map(a => a?.finished.catch(() => {}))).then(clean);
  const control = { finished, cancel: clean };
  for (const row of list) rowMotions.set(row, control);
  return control;
}

export function animateExpandRows(rows) {
  return animateRowHeights(rows, true);
}

export function animateCollapseRows(rows) {
  return animateRowHeights(rows, false);
}

/* 弹窗关闭保留 top layer 到退场结束；重开时取消旧关闭，避免延迟收尾关掉新会话。 */
const closingDialogs = new Map();

export function openDialog(dialog) {
  closingDialogs.get(dialog)?.cancel();
  dialog.showModal();
}

export function closeDialog(dialog, returnValue) {
  if (!dialog?.open) return Promise.resolve();
  if (closingDialogs.has(dialog)) return closingDialogs.get(dialog).finished;
  const inert = dialog.inert;
  let resolve;
  const finished = new Promise(done => { resolve = done; });
  const pending = { finished, cancel: () => finish(false) };
  let animation;
  const finish = (close) => {
    if (closingDialogs.get(dialog) !== pending) return;
    closingDialogs.delete(dialog);
    delete dialog.dataset.closing;
    dialog.inert = inert;
    if (close && dialog.open) {
      if (returnValue === undefined) dialog.close();
      else dialog.close(returnValue);
    }
    animation?.cancel();
    resolve();
  };
  closingDialogs.set(dialog, pending);
  const style = typeof getComputedStyle === 'function' ? getComputedStyle(dialog) : null;
  const from = { opacity: style?.opacity || 1, transform: style?.transform || 'none' };
  for (const current of dialog.getAnimations?.() || []) current.cancel();
  dialog.dataset.closing = '1';
  dialog.inert = true;
  animation = animate(dialog, [from, { opacity: 0, transform: 'translateY(8px) scale(0.98)' }],
    { duration: 180, easing: SPRING.exit, fill: 'forwards' });
  if (animation) animation.finished.catch(() => {}).then(() => finish(true));
  else finish(true);
  return finished;
}

export function animateMenuOpen(menuEl) {
  if (!menuEl || reduceMotion()) return;
  animate(
    menuEl,
    [
      { opacity: 0, transform: 'translateY(-6px) scale(0.92)' },
      { opacity: 1, transform: 'none' },
    ],
    { duration: 280, easing: SPRING.bouncy },
  );
  const items = menuEl.querySelectorAll?.('button');
  if (items?.length) {
    [...items].forEach((btn, i) => {
      animate(
        btn,
        [{ opacity: 0, transform: 'translateX(-5px)' }, { opacity: 1, transform: 'none' }],
        { duration: 240, delay: i * 18, easing: SPRING.snappy, fill: 'backwards' },
      );
    });
  }
}

export function animateToastOut(el, onDone) {
  if (!el || reduceMotion() || typeof el.animate !== 'function' || !el.getClientRects?.().length) {
    onDone?.();
    return;
  }
  if (el.dataset.closing === '1') return;
  el.dataset.closing = '1';
  const h = el.offsetHeight;
  const anim = el.animate(
    [
      { opacity: 1, transform: 'none', maxHeight: `${h}px`, marginTop: '8px' },
      { opacity: 0, transform: 'translate3d(0, 8px, 0) scale(0.94)', maxHeight: '0px', marginTop: '0px', paddingTop: '0px', paddingBottom: '0px' },
    ],
    { duration: 220, easing: SPRING.exit, fill: 'forwards' },
  );
  anim.finished.catch(() => {}).finally(() => onDone?.());
}

/* ---------------------------------------------------------------- 视图切换 */

/* 视图切换：新旧两块主区横向/纵向错开推过去，规则在 style.css 的 data-vt-dir 上。
   dir 只有 'down'（往后翻）和 'up'（往回翻）两种，按导航顺序定。
   主题切换走的是另一套 —— data-vt-theme 的圆形揭开要把所有 view-transition-name
   摘掉，两条路同时跑会互相抢名字、把页面撕开，所以互斥。
   返回是否真的套上了过渡，没套上时调用方自己补一个淡入。 */
let vtSeq = 0;

export function withViewTransition(dir, mutate, afterPaint) {
  const root = document.documentElement;
  const seq = ++vtSeq;
  if (reduceMotion() || typeof document.startViewTransition !== 'function' || root.dataset.vtTheme) {
    delete root.dataset.vtDir;
    mutate();
    afterPaint?.(false);
    return false;
  }
  root.dataset.vtDir = dir;
  // 连点导航会把上一次的过渡打断，它的收尾不能把新过渡的方向擦掉
  const vt = document.startViewTransition(mutate);
  // ready 在新快照捕获之后才兑现；此时目标页已显示，内部 WAAPI 动画能实际播放。
  vt.ready.then(() => { if (seq === vtSeq) afterPaint?.(true); }).catch(() => {
    vt.updateCallbackDone.then(() => { if (seq === vtSeq) afterPaint?.(false); }).catch(() => {});
  });
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
  if (!host?.children) return prev;
  for (const el of host.children) {
    const fill = fillOf(el);
    if (fill) prev.set(el.dataset.key, fill.style.width);
  }
  return prev;
}

export function morphBars(host, prev, fillOf) {
  if (!host?.children || reduceMotion()) return;
  if (!prev || !prev.size) {
    // 首次渲染时：保持 fill.style.width 同步不变，用 WAAPI 让条形从 0% 弹簧生长出来
    [...host.children].forEach((row, i) => {
      const fill = fillOf(row);
      if (fill && fill.style.width) {
        animate(
          fill,
          [{ width: '0%' }, { width: fill.style.width }],
          { duration: 540, delay: i * 24, easing: SPRING.smooth, fill: 'backwards' },
        );
      }
    });
    return;
  }
  const pending = [];
  for (const row of host.children) {
    const old = prev.get(row.dataset.key);
    const fill = fillOf(row);
    if (!fill) continue;
    if (!old) {
      if (fill.style.width) {
        animate(
          fill,
          [{ width: '0%' }, { width: fill.style.width }],
          { duration: 520, easing: SPRING.smooth, fill: 'backwards' },
        );
      }
      continue;
    }
    if (fill.style.width === old) continue;
    const target = fill.style.width;
    fill.style.transition = 'none';
    fill.style.width = old;
    pending.push({ fill, target });
  }
  if (!pending.length) return;
  void host.offsetWidth; // 提交旧宽度，避免同一帧的目标值把过渡起点合并掉。
  requestAnimationFrame(() => {
    for (const { fill, target } of pending) {
      fill.style.transition = '';
      fill.style.width = target;
    }
  });
}

export function updateNumber(el, text) {
  if (!el || el.textContent === text) return;
  const prevNum = parseFloat(String(el.textContent).replace(/[^0-9.-]/g, ''));
  const nextNum = parseFloat(String(text).replace(/[^0-9.-]/g, ''));
  const dir = !Number.isNaN(prevNum) && !Number.isNaN(nextNum) && nextNum < prevNum ? -1 : 1;
  el.textContent = text;
  animate(
    el,
    [
      { opacity: 0.3, transform: `translate3d(0, ${dir * 7}px, 0) scale(0.94)` },
      { opacity: 1, transform: 'none' },
    ],
    { duration: 320, easing: SPRING.bouncy },
  );
}

/* ---------------------------------------------------------------- 分段控制器胶囊指示器 */

export function syncSegMarkers(scope = document) {
  if (!scope?.querySelectorAll) return;
  for (const seg of scope.querySelectorAll('.seg')) {
    const active = seg.querySelector('.seg-item.is-on');
    if (!active || !active.offsetWidth) {
      seg.style.setProperty('--seg-opacity', '0');
      delete seg.dataset.segReady;
      continue;
    }
    seg.style.setProperty('--seg-x', `${active.offsetLeft}px`);
    seg.style.setProperty('--seg-y', `${active.offsetTop}px`);
    seg.style.setProperty('--seg-w', `${active.offsetWidth}px`);
    seg.style.setProperty('--seg-h', `${active.offsetHeight}px`);
    seg.style.setProperty('--seg-opacity', '1');
    seg.dataset.segReady = '1';
  }
}

let controller = null;
export function moveNavMarker() {
  controller?.moveMarker();
  syncSegMarkers();
}
export function toggleMotion() { return controller?.toggle(); }

export function initMotion() {
  controller?.dispose();
  syncSpringCurves();
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
      if (selected) {
        marker.style.width = selected.offsetWidth + 'px';
        marker.style.height = selected.offsetHeight + 'px';
        marker.style.transform = `translate(${selected.offsetLeft}px, ${selected.offsetTop}px)`;
        marker.style.opacity = '1';
      }
      syncSegMarkers();
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
  on(document, 'submit', (event) => {
    if (event.target?.method !== 'dialog') return;
    event.preventDefault();
    closeDialog(event.target.closest('dialog'), event.submitter?.value);
  });
  on(document, 'toggle', (event) => {
    clearLight();
    // <details class="adv"> 展开时为内部内容播放物理弹簧展开动效
    const details = event.target;
    if (details?.tagName === 'DETAILS' && details.open && visible() && !reduced.matches) {
      const body = details.querySelector?.('.adv-body');
      if (body) {
        animate(
          body,
          [
            { opacity: 0, transform: 'translate3d(0, -6px, 0) scale(0.99)' },
            { opacity: 1, transform: 'none' },
          ],
          { duration: 320, easing: SPRING.bouncy },
        );
      }
    }
  }, true);
  on(document, 'click', () => {
    if (visible()) requestAnimationFrame(() => syncSegMarkers());
  }, { passive: true });
  on(document, 'scroll', clearLight, { passive: true, capture: true });
  on(document, 'visibilitychange', sync);
  on(window, 'blur', clearLight);
  on(window, 'resize', () => { clearLight(); moveMarker(); }, { passive: true });
  on(reduced, 'change', sync);
  on(pointer, 'change', sync);
  const observer = new ResizeObserver(moveMarker);
  if (nav) observer.observe(nav);
  for (const seg of document.querySelectorAll?.('.seg') || []) observer.observe(seg);

  // 监听弹窗 [open] 状态变化，在弹窗打开时驱动内部表单项的物理弹簧级联入场
  let dlgObserver = null;
  if (typeof MutationObserver === 'function' && document.body) {
    dlgObserver = new MutationObserver((mutations) => {
      if (!visible() || reduced.matches) return;
      for (const m of mutations) {
        const dlg = m.target;
        if (dlg?.tagName === 'DIALOG' && dlg.open) {
          const head = dlg.querySelector?.('.dlg-head');
          const body = dlg.querySelector?.('.dlg-body');
          const foot = dlg.querySelector?.('.dlg-foot');
          if (head) animate(head, [{ opacity: 0.4, transform: 'translateY(-4px)' }, { opacity: 1, transform: 'none' }], { duration: 280, easing: SPRING.smooth });
          if (body?.children?.length) {
            enterStagger(body.children, { step: 22, duration: 340, distance: 8, scale: 0.99, limit: 8 });
          }
          if (foot) animate(foot, [{ opacity: 0.4, transform: 'translateY(4px)' }, { opacity: 1, transform: 'none' }], { duration: 300, delay: 40, easing: SPRING.smooth, fill: 'backwards' });
        }
      }
    });
    for (const dlg of document.querySelectorAll?.('dialog') || []) {
      dlgObserver.observe(dlg, { attributes: true, attributeFilter: ['open'] });
    }
  }
  for (const dlg of document.querySelectorAll?.('dialog') || []) {
    on(dlg, 'cancel', (event) => { event.preventDefault(); closeDialog(dlg); });
  }

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
      for (const pending of closingDialogs.values()) pending.cancel();
      if (navFrame) cancelAnimationFrame(navFrame);
      observer.disconnect();
      dlgObserver?.disconnect();
      listeners.forEach(off => off());
    },
  };
  sync();
  return controller;
}
