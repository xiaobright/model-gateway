/* 动效工具箱：Apple 级物理质感与行云流水动效
   原则：
   1. 物理弹簧质量感（阻尼、弹性形变、Squash & Stretch）
   2. 真实空间互动（鼠标随动高光聚光灯、3D 微倾斜微视差）
   3. 优先 GPU 合成层与 Web Animations API，低开销 120fps 满帧
   4. 严格响应 prefers-reduced-motion 无障碍降级 */

export const reduceMotion = () =>
  window.matchMedia('(prefers-reduced-motion: reduce)').matches;

export const EASE_SPRING = 'cubic-bezier(.34, 1.35, .4, 1)';
export const EASE_APPLE = 'cubic-bezier(.2, .95, .25, 1)';
export const EASE_FLUID = 'cubic-bezier(.32, .72, 0, 1)';
export const EASE_BOUNCE = 'cubic-bezier(.34, 1.56, .64, 1)';
export const EASE_OUT = 'cubic-bezier(.16, 1, .3, 1)';

/* ---------------------------------------------------------------- 视图切换 */

/**
 * 用 View Transitions 包住 DOM 变更。
 * dir 支持 'left' | 'right' | 'down' | 'up'，实现横向画卷平滑推移或上下层级推移。
 */
let vtSeq = 0;

export function withViewTransition(dir, mutate) {
  if (reduceMotion() || typeof document.startViewTransition !== 'function') {
    mutate();
    return Promise.resolve();
  }
  const root = document.documentElement;
  root.dataset.vtDir = dir;
  // 连点导航会把上一次的过渡打断，它的收尾不能把新过渡的方向擦掉
  const seq = ++vtSeq;
  const vt = document.startViewTransition(mutate);
  return vt.finished
    .catch(() => { /* 切换被打断不算错误 */ })
    .finally(() => { if (seq === vtSeq) delete root.dataset.vtDir; });
}

/* ---------------------------------------------------------------- FLIP */

export function flip(els, mutate, { duration = 440, stagger = 0, axis = 'x' } = {}) {
  if (reduceMotion()) return mutate();

  const first = new Map();
  for (const el of els) first.set(el, el.getBoundingClientRect());
  mutate();

  let i = 0;
  for (const el of els) {
    const a = first.get(el);
    if (!a) continue;
    const b = el.getBoundingClientRect();
    const dx = a.left - b.left;
    const dy = a.top - b.top;
    const sx = b.width ? a.width / b.width : 1;
    const sy = b.height ? a.height / b.height : 1;
    const still = Math.abs(dx) < 0.5 && Math.abs(dy) < 0.5
      && Math.abs(sx - 1) < 0.005 && Math.abs(sy - 1) < 0.005;
    if (still) continue;

    const from = axis === 'x'
      ? `translate(${dx}px, ${dy}px) scaleX(${sx})`
      : `translate(${dx}px, ${dy}px) scale(${sx}, ${sy})`;
    el.animate(
      [{ transform: from, transformOrigin: 'left center' }, { transform: 'none', transformOrigin: 'left center' }],
      { duration, easing: EASE_APPLE, delay: i++ * stagger, fill: 'both' },
    );
  }
}

/* ---------------------------------------------------------------- 数字滚轮 (Odometer) */

const DIGITS = ['0', '1', '2', '3', '4', '5', '6', '7', '8', '9'];

/**
 * 机械仪表式数字滚轮：将数值拆分成单字符滚轮槽。
 * 每一个数字列是一个包含 0-9 的竖向滚动条，数字变化时像机械表一样垂直滑过。
 */
export function createOdometer(hostEl) {
  hostEl.classList.add('odometer-host');
  let currentStr = '';

  function set(valStr) {
    const targetStr = String(valStr);
    if (targetStr === currentStr) return;

    if (reduceMotion()) {
      hostEl.textContent = targetStr;
      currentStr = targetStr;
      return;
    }

    // 重新构建槽结构
    hostEl.innerHTML = '';
    const chars = targetStr.split('');
    chars.forEach((ch, idx) => {
      if (DIGITS.includes(ch)) {
        const slot = document.createElement('span');
        slot.className = 'odo-slot';
        const ribbon = document.createElement('span');
        ribbon.className = 'odo-ribbon';
        // 0-9
        ribbon.innerHTML = DIGITS.map(d => `<span class="odo-digit">${d}</span>`).join('');
        slot.appendChild(ribbon);
        hostEl.appendChild(slot);

        const targetIndex = Number(ch);
        const delay = idx * 35; // 错峰滚动，产生流水波浪感
        ribbon.style.transform = `translateY(0%)`;
        requestAnimationFrame(() => {
          ribbon.animate([
            { transform: `translateY(0%)` },
            { transform: `translateY(-${targetIndex * 10}%)` }
          ], {
            duration: 680,
            delay,
            easing: EASE_SPRING,
            fill: 'forwards'
          });
        });
      } else {
        const symbol = document.createElement('span');
        symbol.className = 'odo-symbol';
        symbol.textContent = ch;
        hostEl.appendChild(symbol);
      }
    });

    currentStr = targetStr;
  }

  return { set };
}

/** 兼容旧的 countUp 函数，平滑插值过渡数字 */
export function countUp(el, to, { duration = 720, format = (v) => String(Math.round(v)) } = {}) {
  const from = Number(el.dataset.value ?? to);
  el.dataset.value = String(to);
  if (reduceMotion() || Math.abs(to - from) < 0.5) {
    el.textContent = format(to);
    return;
  }
  const t0 = performance.now();
  const step = (now) => {
    const p = Math.min(1, (now - t0) / duration);
    const eased = 1 - Math.pow(1 - p, 4);
    el.textContent = format(from + (to - from) * eased);
    if (p < 1) requestAnimationFrame(step);
  };
  requestAnimationFrame(step);
}

/* ---------------------------------------------------------------- 流光飞线 */

export function flow(fromEl, toEl, { duration = 580 } = {}) {
  if (!fromEl || !toEl || reduceMotion()) return;
  const a = fromEl.getBoundingClientRect();
  const b = toEl.getBoundingClientRect();
  const ax = a.left + a.width / 2;
  const ay = a.top + a.height / 2;
  const bx = b.left + b.width / 2;
  const by = b.top + b.height / 2;

  const dot = document.createElement('i');
  dot.className = 'flow-dot';
  document.body.append(dot);

  const midX = (ax + bx) / 2;
  const midY = Math.min(ay, by) - 30;

  const anim = dot.animate([
    { transform: `translate3d(${ax}px, ${ay}px, 0) scale(.4)`, opacity: 0 },
    { transform: `translate3d(${midX}px, ${midY}px, 0) scale(1.3)`, opacity: 1, offset: 0.45 },
    { transform: `translate3d(${bx}px, ${by}px, 0) scale(.5)`, opacity: 0 },
  ], { duration, easing: EASE_SPRING });

  anim.finished.catch(() => {}).finally(() => dot.remove());
}

/* ---------------------------------------------------------------- 导航指示条：液体弹性拉伸 */

let lastMarkerTop = null;

export function moveMarker(marker, target) {
  if (!marker || !target) return;
  const box = target.getBoundingClientRect();
  const host = marker.parentElement.getBoundingClientRect();
  const top = box.top - host.top;

  if (reduceMotion()) {
    marker.style.transition = 'none';
    marker.style.transform = `translateY(${top}px)`;
    marker.style.height = `${box.height}px`;
    requestAnimationFrame(() => { marker.style.transition = ''; });
    return;
  }

  const prevTop = lastMarkerTop ?? top;
  lastMarkerTop = top;
  const dy = top - prevTop;

  // 如果位移显著，施加物理弹簧拉伸与回弹（Squash & Stretch）
  if (Math.abs(dy) > 15) {
    const stretch = 1 + Math.min(0.28, Math.abs(dy) / 220);
    const origin = dy > 0 ? 'top center' : 'bottom center';

    marker.style.transformOrigin = origin;
    marker.animate([
      { transform: `translateY(${prevTop}px) scaleY(1)` },
      { transform: `translateY(${prevTop + dy * 0.45}px) scaleY(${stretch})`, offset: 0.38 },
      { transform: `translateY(${top}px) scaleY(0.92)`, offset: 0.78 },
      { transform: `translateY(${top}px) scaleY(1)` }
    ], {
      duration: 480,
      easing: EASE_SPRING,
      fill: 'forwards'
    });
  } else {
    marker.style.transform = `translateY(${top}px)`;
  }

  marker.style.height = `${box.height}px`;
}

/* ---------------------------------------------------------------- 入场 / 脉冲 / 滑入 */

export function enterStagger(els, { step = 30, from = 14 } = {}) {
  if (reduceMotion()) return;
  [...els].slice(0, 40).forEach((el, i) => {
    el.animate(
      [
        { opacity: 0, transform: `translate3d(0, ${from}px, 0) scale(.97)` },
        { opacity: 1, transform: 'none' }
      ],
      { duration: 420, delay: i * step, easing: EASE_APPLE, fill: 'backwards' },
    );
  });
}

export function pulse(el, { duration = 2000 } = {}) {
  if (!el || reduceMotion()) return;
  el.animate(
    [
      { background: 'var(--flash)', transform: 'scale(1.01)' },
      { background: 'var(--flash)', transform: 'none', offset: 0.2 },
      { background: 'transparent', transform: 'none' }
    ],
    { duration, easing: 'ease-out' },
  );
}

export function slideIn(el) {
  if (!el || reduceMotion()) return;
  el.animate(
    [
      { opacity: 0, transform: 'translate3d(0, -12px, 0) scale(.98)' },
      { opacity: 1, transform: 'none' },
    ],
    { duration: 360, easing: EASE_APPLE },
  );
}

/* ---------------------------------------------------------------- 空间交互：全局光斑追踪与 3D 微透视弹性回正 */

const clamp = (v, lo, hi) => Math.min(hi, Math.max(lo, v));

/* 悬浮微倾斜。要固定的量是「卡片上任何一点在屏幕上最多移动多少像素」：
   perspective(P) 下绕中心转 ax / ay（弧度），角点 (±w, ±h) 的深度
   z ≈ ±h·ax ∓ w·ay，投影后该点位移 ≈ |z| / P × R（R 为半对角线长）。
   把预算对半分给两个输入轴，就得到
       h·ax = w·ay = TRAVEL_PX · P / (2R)
   于是大卡片只转一点点、小卡片转得多，而任何尺寸、任何一点的位移上限都是
   TRAVEL_PX，横推和纵推鼠标的出力也一样。

   注意一定要按角点算：若按「边中点位移 = half²·角度/P」去定，解出的
   ax / ay = w²/h²，宽扁卡片会把短边那一轴多转 长宽比 倍（1130×263 是 4.3 倍）——
   横向推鼠标几乎看不出动静，纵向一推整张卡被透视拉成大梯形，角点能跑出 35px。 */
const TRAVEL_PX = 8;                            // 想整体加减幅度，只调这个数
const PERSPECTIVE = 280;                        // 一阶效果与 P 无关，取小是为了角度小、不出现 cos 压扁
const MAX_SKEW = 0.06;                          // 位移不超过半对角线的 6%，等价于把透视形变卡在 6%
const LIFT_PX = (PERSPECTIVE * 0.002).toFixed(2); // 固定放大 0.2% 的抬起感，与尺寸无关
const RAD2DEG = 180 / Math.PI;

function tiltGeometry(card) {
  const halfW = Math.max(1, card.offsetWidth / 2);
  const halfH = Math.max(1, card.offsetHeight / 2);
  const radius = Math.hypot(halfW, halfH);
  const travel = Math.min(TRAVEL_PX, radius * MAX_SKEW);
  const budget = (travel * PERSPECTIVE) / (2 * radius);   // 每轴分到的 half × 角度
  return {
    halfW,
    halfH,
    maxRotX: (budget / halfH) * RAD2DEG,
    maxRotY: (budget / halfW) * RAD2DEG,
  };
}

export function initSpotlightAndTilt(container = document.body) {
  if (reduceMotion()) return;

  const tiltCards = container.querySelectorAll('.card, .kpi');
  tiltCards.forEach((card) => {
    if (card.dataset.tiltInit) return;
    card.dataset.tiltInit = 'true';

    card.addEventListener('pointerenter', () => {
      // 去掉 transform 的过渡，倾斜才能贴着鼠标走；其余过渡保持和样式表一致
      card.style.transition = 'box-shadow .35s cubic-bezier(.2, .95, .25, 1), border-color .35s';
    });

    card.addEventListener('pointermove', (e) => {
      const { halfW, halfH, maxRotX, maxRotY } = tiltGeometry(card);
      const rect = card.getBoundingClientRect();
      // 变换绕中心走，中心点不受自身 transform 影响，可以直接量；
      // 半边长则取布局尺寸，免得读到被转歪撑大的外接框，一帧一帧自激抖动
      const normX = clamp((e.clientX - rect.left - rect.width / 2) / halfW, -1, 1);
      const normY = clamp((e.clientY - rect.top - rect.height / 2) / halfH, -1, 1);

      card.style.transform = `perspective(${PERSPECTIVE}px) `
        + `rotateX(${(-normY * maxRotX).toFixed(2)}deg) rotateY(${(normX * maxRotY).toFixed(2)}deg) `
        + `translateZ(${LIFT_PX}px)`;
    });

    card.addEventListener('pointerleave', () => {
      // 鼠标移出时，用 Apple 经典高阻尼弹簧曲线平滑归位，消除突兀吸附
      card.style.transition = 'transform .65s cubic-bezier(.34, 1.35, .4, 1), '
        + 'box-shadow .4s ease, border-color .4s';
      card.style.transform = `perspective(${PERSPECTIVE}px) rotateX(0deg) rotateY(0deg) translateZ(0px)`;
    });
  });

  bindGlobalLight();
}

/* ---------------------------------------------------------------- 全局光照

   三件事共用一套状态：
   1) 环境光斑 —— 指针附近的卡片按距离亮起（--mouse-x/y + --spotlight-opacity）。
   2) 按压光 —— 一个固定定位的光球跟着指针，按下时放大提亮，松开缩回。
      它挂在 .shell 后面（z-index 0），所以是从玻璃底下透出来的，而不是糊在卡片上。
   3) 按压增益 —— 一个根变量 --press（0~1），同时放大光斑的半径、亮度和作用范围，
      于是「按下去，周围的玻璃一起亮一下」。改一个变量，十几张卡一起响应。

   性能上关键的一条：先把所有目标的 rect 量完缓存起来，再统一写自定义属性。
   原来的写法是「读一个 rect、写一个属性」交替，每次写都让下一次读被迫刷新
   样式和布局 —— 12 个元素实测每帧 4.43ms，改成缓存 + 批量写之后 0.04ms。 */

const LIGHT_SEL = '.card, .kpi, .sidebar, .pill';
const REACH = 420;          // 环境光斑的作用半径（px）
const PRESS_REACH = 260;    // 按下时额外扩出去多少
const PRESS_IN = 90;        // 按下的起效时间（ms）
const PRESS_OUT = 420;      // 松开的回落时间（ms）

let lightBound = false;
let targets = [];           // [{ el, rect }]，指针移动时不再量布局
let needsMeasure = true;
let glow = null;
let raf = null;
let press = 0;              // 当前按压强度
let pressTarget = 0;
let pressAt = 0;
let pointer = { x: -9999, y: -9999, live: false, touch: false };

function remeasure() {
  targets = [];
  for (const el of document.querySelectorAll(LIGHT_SEL)) {
    const rect = el.getBoundingClientRect();
    if (rect.width) targets.push({ el, rect });
  }
}

function paintLight() {
  const { x, y, live } = pointer;
  const reach = REACH + press * PRESS_REACH;

  for (const { el, rect } of targets) {
    let opacity = 0;
    if (live) {
      const dx = Math.max(rect.left - x, 0, x - rect.right);
      const dy = Math.max(rect.top - y, 0, y - rect.bottom);
      const dist = Math.hypot(dx, dy);
      if (dist < reach) {
        el.style.setProperty('--mouse-x', `${(x - rect.left).toFixed(1)}px`);
        el.style.setProperty('--mouse-y', `${(y - rect.top).toFixed(1)}px`);
        opacity = 1 - dist / reach;
      }
    }
    el.style.setProperty('--spotlight-opacity', opacity.toFixed(3));
  }

  if (glow) {
    // 只动 transform 和 opacity，全程走合成层，不产生重绘
    glow.style.transform = `translate3d(${x}px, ${y}px, 0) scale(${(0.5 + press * 0.75).toFixed(3)})`;
    glow.style.opacity = (live ? 0.14 + press * 0.86 : 0).toFixed(3);
  }
  document.documentElement.style.setProperty('--press', press.toFixed(3));
}

/** 按压强度自己缓动到目标值；只有在动的时候才占着 rAF。
    一帧里严格「先读完再写完」：量 rect 是读，写自定义属性是写，混着来就会逼出强制布局。 */
function tick() {
  raf = null;
  if (needsMeasure) { remeasure(); needsMeasure = false; }

  const now = performance.now();
  const span = pressTarget > press ? PRESS_IN : PRESS_OUT;
  const step = Math.min(1, (now - pressAt) / span);
  pressAt = now;
  press += (pressTarget - press) * (1 - Math.pow(1 - step, 3));
  if (Math.abs(pressTarget - press) < 0.012) press = pressTarget;
  paintLight();
  if (press !== pressTarget) schedule();
}

function schedule() {
  if (raf === null) raf = requestAnimationFrame(tick);
}

/* PC 和触屏的操作逻辑不一样，这里分两套：
   - 鼠标：有 hover，光斑常驻跟随；按下只是把它提亮扩大；指针离开窗口才熄灭。
   - 触屏：没有 hover，光只在手指按住时存在，抬手就整体退掉 —— 否则光斑会
     停在最后一次触摸的位置不动，看着像卡住了（原来的实现只监听 mouseleave，
     触屏上永远不会触发）。 */
function bindGlobalLight() {
  if (lightBound) return;
  lightBound = true;

  glow = document.createElement('i');
  glow.className = 'press-glow';
  glow.setAttribute('aria-hidden', 'true');
  document.body.append(glow);

  needsMeasure = true;

  const move = (e) => {
    if (!e.isPrimary) return;          // 多指时只认第一根
    pointer.x = e.clientX;
    pointer.y = e.clientY;
    pointer.live = true;
    pointer.touch = e.pointerType !== 'mouse';
    schedule();
  };

  window.addEventListener('pointermove', move, { passive: true });

  window.addEventListener('pointerdown', (e) => {
    if (!e.isPrimary) return;
    move(e);
    pressTarget = 1;
    pressAt = performance.now();
    schedule();
  }, { passive: true });

  const release = (e) => {
    if (e && !e.isPrimary) return;
    pressTarget = 0;
    pressAt = performance.now();
    if (pointer.touch) pointer.live = false;   // 触屏抬手即熄
    schedule();
  };
  window.addEventListener('pointerup', release, { passive: true });
  window.addEventListener('pointercancel', release, { passive: true });  // 手势被浏览器接走（滚动等）

  document.addEventListener('mouseleave', () => {
    pointer.live = false;
    pressTarget = 0;
    pressAt = performance.now();
    schedule();
  });

  // 卡片位置只在滚动 / 改窗口 / 内容高度变化时才变，标记一下让下一帧统一重量
  const refresh = () => { needsMeasure = true; schedule(); };
  window.addEventListener('scroll', refresh, { passive: true });
  window.addEventListener('resize', refresh, { passive: true });

  // 列表长短一变，下面的卡片就整体位移了；盯着 .shell 的高度最省事
  const shell = document.querySelector('.shell');
  if (shell && typeof ResizeObserver === 'function') {
    new ResizeObserver(refresh).observe(shell);
  }
}

/** 视图切换、列表重渲染之后调用：卡片换了位置，缓存的 rect 要跟着更新 */
export function refreshLightTargets() {
  if (!lightBound) return;
  needsMeasure = true;
  schedule();
}
