/* 物理弹簧内核：位置和速度一起保存，重定向只改目标，不清状态。
   导航胶囊、分段胶囊、路由选中条和视图推拉都走这里，所以快速连点、点反方向
   时能从当前帧的速度继续，而不是每次从零重新缓动。

   一次性入场（弹窗、列表新行、图表）仍用 WAAPI；它们没有「被打断后继续」的
   语义，但曲线来自同一组物理参数（见 style.css 的 --spring-*）。 */

export const MOTION = {
  // 导航胶囊的位移：比第一版慢一档，回弹也更明显（约 9% 过冲）
  nav: { stiffness: 450, damping: 26, mass: 1, precision: 0.08 },
  // 宽高变化阻尼大一些，避免胶囊被拉成橡皮；比 x 稍早收住
  size: { stiffness: 520, damping: 46, mass: 1, precision: 0.08 },
  // 分段胶囊和路由选中条：和导航同一档回弹
  seg: { stiffness: 500, damping: 27, mass: 1, precision: 0.08 },
  route: { stiffness: 450, damping: 26, mass: 1, precision: 0.08 },
  // 整页推拉：约 575ms 落位，带 10% 左右过冲，能看出「推过去再弹一下」
  view: { stiffness: 340, damping: 22, mass: 1, precision: 0.08 },
  // 指示器透明度只走阻尼，不跟着位移一起弹
  fade: { stiffness: 420, damping: 40, mass: 1, precision: 0.01 },
  // 视图交叉淡出比推拉快：两层半透明玻璃叠着时，慢淡出会看到两层文字
  viewFade: { stiffness: 1600, damping: 76, mass: 1, precision: 0.01 },
};

/* ---------------------------------------------------------------- 不依赖 DOM 的标量弹簧 */

export function createSpringValue({
  value = 0,
  velocity = 0,
  stiffness,
  damping,
  mass = 1,
  precision = 0.08,
} = {}) {
  let x = Number(value) || 0;
  let v = Number(velocity) || 0;
  let target = x;

  return {
    get value() { return x; },
    get velocity() { return v; },
    get target() { return target; },
    set(next, { keepVelocity = true } = {}) {
      target = Number(next) || 0;
      if (!keepVelocity) v = 0;
    },
    place(next) {
      x = target = Number(next) || 0;
      v = 0;
    },
    /** 推进 dt 秒；返回是否已经停在目标上。 */
    step(dt) {
      const span = Math.max(0, Math.min(dt, 1 / 30));
      if (!span) return false;
      const steps = Math.max(1, Math.ceil(span / (1 / 240)));
      const h = span / steps;
      for (let i = 0; i < steps; i++) {
        const force = -stiffness * (x - target) - damping * v;
        v += (force / mass) * h;
        x += v * h;
      }
      if (Math.abs(x - target) <= precision && Math.abs(v) <= precision * 16) {
        x = target;
        v = 0;
        return true;
      }
      return false;
    },
  };
}

/* ---------------------------------------------------------------- 带统一帧调度的弹簧组
   一个组里的多个值（x / y / w / h 或 x / opacity）共享一次 onFrame，避免每个
   属性各写一次 style。manual: true 时由调用方推进，测试不用依赖真实 rAF。 */

const running = new Set();
let frame = 0;
let last = 0;
const raf = typeof requestAnimationFrame === 'function'
  ? (fn) => requestAnimationFrame(fn)
  : (fn) => setTimeout(() => fn(now()), 16);

function now() {
  return typeof performance !== 'undefined' && typeof performance.now === 'function'
    ? performance.now()
    : Date.now();
}

export function prefersReducedMotion() {
  return Boolean(typeof window !== 'undefined'
    && window.matchMedia?.('(prefers-reduced-motion: reduce)')?.matches);
}

export function motionAllowed() {
  if (typeof document !== 'undefined' && document.visibilityState === 'hidden') return false;
  return !prefersReducedMotion();
}

function schedule() {
  if (frame) return;
  last = now();
  frame = raf(tick);
}

function tick(timestamp) {
  frame = 0;
  const at = Number(timestamp) || now();
  const dt = last ? Math.min(Math.max((at - last) / 1000, 1 / 240), 1 / 30) : 1 / 60;
  last = at;
  for (const group of [...running]) group.step(dt);
  if (running.size) schedule();
  else last = 0;
}

export function settleAllMotions() {
  for (const group of [...running]) group.finish();
}

export function createSpringGroup(spec, onFrame, { manual = false, onRest = null } = {}) {
  const springs = {};
  for (const [key, raw] of Object.entries(spec)) {
    const { value = 0, velocity = 0, ...params } = raw;
    springs[key] = createSpringValue({ value, velocity, ...params });
  }

  let active = false;
  const snapshot = () => Object.fromEntries(
    Object.entries(springs).map(([key, spring]) => [key, spring.value]),
  );

  const write = () => onFrame?.(snapshot());

  const stop = () => {
    active = false;
    running.delete(group);
  };

  const group = {
    get active() { return active; },
    values: snapshot,
    /** 只改目标；当前值和速度都留着，所以可以连续打断。 */
    set(targets, { instant = false } = {}) {
      let changed = false;
      for (const [key, next] of Object.entries(targets)) {
        if (!springs[key]) continue;
        if (Math.abs(springs[key].target - next) < 1e-6) continue;
        springs[key].set(next);
        changed = true;
      }
      if (!changed) return false;
      if (instant || !motionAllowed()) {
        group.finish();
        return true;
      }
      if (!active) {
        active = true;
        if (!manual) {
          running.add(group);
          schedule();
        }
      }
      return true;
    },
    /** 直接落到给定值，不播放动画（首帧、resize、减弱动效）。 */
    place(values) {
      for (const [key, next] of Object.entries(values)) springs[key]?.place(next);
      stop();
      write();
    },
    finish() {
      for (const spring of Object.values(springs)) spring.place(spring.target);
      stop();
      write();
      onRest?.();
    },
    /** 推进一帧；返回是否已静止。 */
    step(dt) {
      let rest = true;
      for (const spring of Object.values(springs)) {
        if (!spring.step(dt)) rest = false;
      }
      write();
      if (rest) {
        stop();
        onRest?.();
      }
      return rest;
    },
    dispose() {
      stop();
    },
  };

  return group;
}

/* ---------------------------------------------------------------- 曲线采样
   给 CSS / WAAPI 的一次性动效生成同族的 linear()。只用来「长得像」，不做
   打断；需要打断的场景走上面的运行时。 */

function advance(state, params) {
  const { stiffness, damping, mass = 1 } = params;
  const h = 1 / 1200;
  const force = -stiffness * (state.x - 1) - damping * state.v;
  state.v += (force / mass) * h;
  state.x += state.v * h;
}

export function settleTime(params, { velocity = 0, precision = 0.001, max = 1.2 } = {}) {
  const state = { x: 0, v: velocity };
  let t = 0;
  let quiet = 0;
  while (t < max) {
    advance(state, params);
    t += 1 / 1200;
    if (Math.abs(1 - state.x) <= precision && Math.abs(state.v) <= precision * 10) {
      quiet += 1 / 1200;
      if (quiet >= 0.05) break;
    } else {
      quiet = 0;
    }
  }
  return t;
}

const round = (value, digits = 4) => Number(value.toFixed(digits));

export function springEasing(params, { velocity = 0, samples = 24 } = {}) {
  const total = settleTime(params, { velocity });
  const state = { x: 0, v: velocity };
  const step = total / samples;
  const points = [0];
  let t = 0;
  for (let i = 1; i <= samples; i++) {
    const until = step * i;
    while (t < until) {
      advance(state, params);
      t += 1 / 1200;
    }
    points.push(i === samples ? 1 : state.x);
  }
  const body = points.slice(1).map((value, i) => {
    const percent = ((i + 1) / samples) * 100;
    return `${round(value)} ${round(percent, 2)}%`;
  }).join(', ');
  return {
    easing: `linear(0, ${body})`,
    duration: Math.max(120, Math.round(total * 1000)),
  };
}

/* 页面隐藏或用户开启「减少动态效果」时，所有在途弹簧立刻落到目标。
   放在模块级只注册一次，不跟随 initMotion 的多次初始化/销毁。 */
if (typeof document !== 'undefined' && document.addEventListener) {
  document.addEventListener('visibilitychange', () => {
    if (document.visibilityState === 'hidden') settleAllMotions();
  });
}

if (typeof window !== 'undefined' && window.matchMedia) {
  window.matchMedia('(prefers-reduced-motion: reduce)')
    .addEventListener?.('change', (event) => {
      if (event.matches) settleAllMotions();
    });
}
