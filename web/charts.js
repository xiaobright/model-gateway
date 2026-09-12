/* 手写 SVG 图表：Apple 级平滑贝塞尔曲线与柔光投影
   统一做法：
   - SVG 虚拟坐标系 + preserveAspectRatio="none" 弹性自适应
   - Catmull-Rom 三次样条插值，消除折线的机械生硬感
   - vector-effect="non-scaling-stroke" 锁定线宽 */

import { esc } from './util.js';

const NS = 'http://www.w3.org/2000/svg';
const VW = 300;   // 虚拟坐标系宽
const VH = 100;   // 虚拟坐标系高
const PAD_TOP = 10;

let uid = 0;

function el(name, attrs = {}) {
  const node = document.createElementNS(NS, name);
  for (const [k, v] of Object.entries(attrs)) node.setAttribute(k, v);
  return node;
}

const reduce = () => window.matchMedia('(prefers-reduced-motion: reduce)').matches;

/* ---------------------------------------------------------------- 样条曲线生成器 */

function splinePath(coords) {
  if (!coords || coords.length < 2) return '';
  let d = `M ${coords[0].x.toFixed(1)},${coords[0].y.toFixed(1)}`;
  for (let i = 0; i < coords.length - 1; i++) {
    const p0 = coords[Math.max(0, i - 1)];
    const p1 = coords[i];
    const p2 = coords[i + 1];
    const p3 = coords[Math.min(coords.length - 1, i + 2)];

    // Catmull-Rom to Cubic Bezier
    const cp1x = p1.x + (p2.x - p0.x) / 6;
    const cp1y = p1.y + (p2.y - p0.y) / 6;
    const cp2x = p2.x - (p3.x - p1.x) / 6;
    const cp2y = p2.y - (p3.y - p1.y) / 6;

    d += ` C ${cp1x.toFixed(1)},${cp1y.toFixed(1)} ${cp2x.toFixed(1)},${cp2y.toFixed(1)} ${p2.x.toFixed(1)},${p2.y.toFixed(1)}`;
  }
  return d;
}

function splineArea(coords, bottomY = VH) {
  if (!coords || coords.length < 2) return '';
  const curve = splinePath(coords);
  const last = coords[coords.length - 1];
  const first = coords[0];
  return `${curve} L ${last.x.toFixed(1)},${bottomY} L ${first.x.toFixed(1)},${bottomY} Z`;
}

/* ---------------------------------------------------------------- 迷你平滑趋势线 */

/** 给 KPI 用的平滑 sparkline，返回 <svg> 元素。 */
export function sparkline(values, { stroke = 'var(--accent)', fill = true } = {}) {
  const pts = (values || []).map((v) => Number(v) || 0);
  const svg = el('svg', {
    viewBox: `0 0 ${VW} 30`, preserveAspectRatio: 'none', class: 'spark', 'aria-hidden': 'true',
  });
  if (pts.length < 2) return svg;

  const max = Math.max(1, ...pts);
  const coords = pts.map((v, i) => ({
    x: (i / (pts.length - 1)) * VW,
    y: Math.max(4, 28 - (v / max) * 23),
  }));

  if (fill) {
    const gid = `sp${uid++}`;
    const defs = el('defs');
    const grad = el('linearGradient', { id: gid, x1: '0', y1: '0', x2: '0', y2: '1' });
    grad.append(
      el('stop', { offset: '0%', 'stop-color': stroke, 'stop-opacity': '.35' }),
      el('stop', { offset: '100%', 'stop-color': stroke, 'stop-opacity': '0' }),
    );
    defs.append(grad);
    svg.append(defs);
    svg.append(el('path', {
      d: splineArea(coords, 30),
      fill: `url(#${gid})`,
    }));
  }

  svg.append(el('path', {
    d: splinePath(coords),
    fill: 'none', stroke, 'stroke-width': '2',
    'vector-effect': 'non-scaling-stroke', 'stroke-linecap': 'round', 'stroke-linejoin': 'round',
  }));
  return svg;
}

/* ---------------------------------------------------------------- 环形图 */

/** 缓存命中率这类单百分比。返回 {node, set(pct)}，set 会做过渡。 */
export function donut(pct, { size = 66, thickness = 7 } = {}) {
  const r = (size - thickness) / 2;
  const c = 2 * Math.PI * r;
  const svg = el('svg', { viewBox: `0 0 ${size} ${size}`, class: 'donut' });
  const track = el('circle', {
    cx: size / 2, cy: size / 2, r, fill: 'none',
    stroke: 'var(--track)', 'stroke-width': thickness,
  });
  const arc = el('circle', {
    cx: size / 2, cy: size / 2, r, fill: 'none',
    stroke: 'var(--accent)', 'stroke-width': thickness, 'stroke-linecap': 'round',
    transform: `rotate(-90 ${size / 2} ${size / 2})`,
    'stroke-dasharray': c, 'stroke-dashoffset': c,
  });
  svg.append(track, arc);

  const shell = document.createElement('div');
  shell.className = 'donut-wrap';
  shell.append(svg);

  const label = document.createElement('span');
  label.className = 'donut-label';
  shell.append(label);

  const set = (value) => {
    const p = Math.max(0, Math.min(100, Number(value) || 0));
    label.textContent = Math.round(p) + '%';
    const offset = c * (1 - p / 100);
    if (reduce()) { arc.setAttribute('stroke-dashoffset', offset); return; }
    arc.animate(
      [{ strokeDashoffset: arc.getAttribute('stroke-dashoffset') || c }, { strokeDashoffset: offset }],
      { duration: 750, easing: 'cubic-bezier(.2,.95,.25,1)', fill: 'forwards' },
    ).finished.catch(() => {}).finally(() => arc.setAttribute('stroke-dashoffset', offset));
  };
  set(pct);
  return { node: shell, set };
}

/* ---------------------------------------------------------------- 吞吐时间线面积图 */

export function areaChart(points, bucket, { onHover } = {}) {
  const wrap = document.createElement('div');
  wrap.className = 'chart';

  const svg = el('svg', {
    viewBox: `0 0 ${VW} ${VH}`, preserveAspectRatio: 'none', class: 'chart-svg',
    role: 'img', 'aria-label': '请求量与输出 token 随时间的分布',
  });
  const defs = el('defs');
  const gOk = el('linearGradient', { id: `ok${uid++}`, x1: '0', y1: '0', x2: '0', y2: '1' });
  gOk.append(
    el('stop', { offset: '0%', 'stop-color': 'var(--accent)', 'stop-opacity': '.42' }),
    el('stop', { offset: '100%', 'stop-color': 'var(--accent)', 'stop-opacity': '.02' }),
  );
  const gErr = el('linearGradient', { id: `er${uid++}`, x1: '0', y1: '0', x2: '0', y2: '1' });
  gErr.append(
    el('stop', { offset: '0%', 'stop-color': 'var(--crit)', 'stop-opacity': '.55' }),
    el('stop', { offset: '100%', 'stop-color': 'var(--crit)', 'stop-opacity': '.1' }),
  );
  defs.append(gOk, gErr);

  const grid = el('g', { class: 'chart-grid' });
  for (let i = 1; i <= 2; i++) {
    const y = (VH / 3) * i;
    grid.append(el('line', {
      x1: 0, y1: y, x2: VW, y2: y, stroke: 'var(--hair)',
      'stroke-width': '1', 'vector-effect': 'non-scaling-stroke',
    }));
  }

  const areaOk = el('path', { fill: `url(#${gOk.id})` });
  const areaErr = el('path', { fill: `url(#${gErr.id})` });
  const lineOk = el('path', {
    fill: 'none', stroke: 'var(--accent)', 'stroke-width': '2',
    'vector-effect': 'non-scaling-stroke', 'stroke-linecap': 'round', 'stroke-linejoin': 'round',
  });
  const lineTok = el('path', {
    fill: 'none', stroke: 'var(--good)', 'stroke-width': '1.5',
    'stroke-dasharray': '5 3', 'vector-effect': 'non-scaling-stroke', 'opacity': '.88',
  });
  svg.append(defs, grid, areaErr, areaOk, lineTok, lineOk);

  const cross = document.createElement('div');
  cross.className = 'chart-cross';
  const knob = document.createElement('div');
  knob.className = 'chart-knob';
  const tip = document.createElement('div');
  tip.className = 'chart-tip';
  const axis = document.createElement('div');
  axis.className = 'chart-axis';

  wrap.append(svg, cross, knob, tip, axis);

  let current = points || [];
  let curBucket = bucket;

  const geom = (list) => {
    const max = Math.max(1, ...list.map((p) => p.n || 0));
    const maxTok = Math.max(1, ...list.map((p) => p.to || 0));
    const x = (i) => (list.length > 1 ? (i / (list.length - 1)) * VW : VW / 2);
    const y = (v) => VH - (v / max) * (VH - PAD_TOP);
    const yTok = (v) => VH - (v / maxTok) * (VH - PAD_TOP);
    return { max, x, y, yTok };
  };

  const draw = (list, bsec) => {
    current = list || [];
    curBucket = bsec ?? curBucket;
    if (!current.length) {
      areaOk.removeAttribute('d');
      areaErr.removeAttribute('d');
      lineOk.removeAttribute('d');
      lineTok.removeAttribute('d');
      return;
    }
    const { x, y, yTok } = geom(current);

    const coordsOk = current.map((p, i) => ({ x: x(i), y: y(p.n || 0) }));
    const coordsErr = current.map((p, i) => ({ x: x(i), y: y(p.err || 0) }));
    const coordsTok = current.map((p, i) => ({ x: x(i), y: yTok(p.to || 0) }));

    areaErr.setAttribute('d', splineArea(coordsErr, VH));
    areaOk.setAttribute('d', splineArea(coordsOk, VH));

    lineOk.setAttribute('d', splinePath(coordsOk));
    lineTok.setAttribute('d', splinePath(coordsTok));

    axis.innerHTML = '';
    const labels = pickAxis(current.length);
    for (const i of labels) {
      const span = document.createElement('span');
      span.textContent = axisLabel(current[i].t, curBucket);
      span.style.left = `${(i / Math.max(1, current.length - 1)) * 100}%`;
      axis.append(span);
    }
  };

  const hide = () => { cross.style.opacity = 0; knob.style.opacity = 0; tip.style.opacity = 0; };

  svg.addEventListener('mousemove', (ev) => {
    if (!current.length) return;
    const box = svg.getBoundingClientRect();
    const ratio = Math.max(0, Math.min(1, (ev.clientX - box.left) / box.width));
    const i = Math.round(ratio * (current.length - 1));
    const p = current[i];
    const leftPct = (i / Math.max(1, current.length - 1)) * 100;
    const { y } = geom(current);

    cross.style.left = `${leftPct}%`;
    cross.style.opacity = 1;
    knob.style.left = `${leftPct}%`;
    knob.style.top = `${(y(p.n || 0) / VH) * 100}%`;
    knob.style.opacity = 1;
    tip.style.opacity = 1;
    tip.innerHTML = onHover ? onHover(p) : defaultTip(p);
    tip.style.left = `${leftPct}%`;
    tip.style.transform = leftPct > 62 ? 'translate(-100%, -50%)' : 'translate(12px, -50%)';
    tip.style.top = `${(y(p.n || 0) / VH) * 100}%`;
  });
  svg.addEventListener('mouseleave', hide);

  draw(points, bucket);
  hide();
  return { node: wrap, update: draw, hide };
}

function defaultTip(p) {
  return `<b>${p.n || 0}</b> 次${p.err ? ` · <span class="tip-err">${esc(p.err)} 失败</span>` : ''}`
    + `<br><span class="dim">出 ${fmtK(p.to)} tok</span>`;
}

function fmtK(v) {
  const n = Number(v) || 0;
  if (n >= 1e6) return (n / 1e6).toFixed(1) + 'M';
  if (n >= 1e3) return (n / 1e3).toFixed(1) + 'K';
  return String(n);
}

function pickAxis(len) {
  if (len <= 1) return [0];
  // 桶数少时 count 不能超过 len，否则 Math.round 会算出重复下标，标签叠在一起
  const count = Math.min(len, len > 40 ? 4 : 5);
  const picks = Array.from(
    { length: count }, (_, k) => Math.round((k / (count - 1)) * (len - 1)),
  );
  return [...new Set(picks)];
}

function axisLabel(epochSec, bucket) {
  const d = new Date(epochSec * 1000);
  if (bucket >= 86400) return `${d.getMonth() + 1}/${d.getDate()}`;
  return `${String(d.getHours()).padStart(2, '0')}:${String(d.getMinutes()).padStart(2, '0')}`;
}

/* ---------------------------------------------------------------- 条形 / 排行榜 */

export function barRow({ label, value, max, badge, tone = 'accent', note = '' }) {
  const row = document.createElement('div');
  row.className = 'bar-row';
  const pct = max > 0 ? Math.max(2, (value / max) * 100) : 0;
  row.innerHTML = `
    <span class="bar-label mono" title="${esc_(label)}">${esc_(label)}</span>
    <span class="bar-track"><i class="bar-fill tone-${tone}" style="width:${pct}%"></i></span>
    <span class="bar-num">${esc_(badge ?? String(value))}</span>
    ${note ? `<span class="bar-note dim">${esc_(note)}</span>` : ''}`;
  return row;
}

function esc_(v) {
  return String(v).replace(/[&<>"']/g, (c) => (
    { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]
  ));
}
