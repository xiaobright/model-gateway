'use strict';
/* 下游气泡一次只展开一条候选链。箭头排列候选，轮盘只修改保存的首选。
   状态显示是下一次请求的起点预测，不模拟单次请求的自动降级轨迹。
   坐标只负责展示；路由写操作仍通过已有管理接口。 */

import {
  $, state, api, toast, esc, clamp, confirmBox, groupOf, upstreamOfGroup, splitOneM,
  PROTO_LABEL, PROTO_SHORT, PROTOCOLS, protocolOn,
} from './util.js';
import { orderedCandidates, attemptOrder, forwardPath, forwardTargets, moveCandidate } from './canvas-model.js';
import {
  enterStagger, slideIn, captureRects, flipElements,
  animateCanvasCamera, animateBubbleSelect, animateMenuOpen, springAnimate, SPRING,
} from './motion.js';

/* ---------------------------------------------------------------- 注入 */

const hooks = {
  openRoute: () => {},        // 打开「新增模型 / 加候选 / 改候选」弹窗
  refreshConfig: async () => {},
  openUpstream: () => {},     // 跳到「上游站点」并展开那个供应商
};

/* ---------------------------------------------------------------- 几何 */

const CORE_R = 42;          // 气泡本体半径
const PORT_R = 12;          // 端口圆片半径
const PORT_GAP = 8;         // 端口之间留的弧距，挤不下就把环撑大
const HIT = 6;              // 位移超过这么多像素才算拖动，否则算点击
const Z_MIN = 0.45, Z_MAX = 2.6;
const FIT_MIN = 0.62;
const KNOB_OUT = 26;

function ringRadius(n) {
  if (n <= 1) return CORE_R + 28;
  return Math.max(CORE_R + 28, (n * (PORT_R * 2 + PORT_GAP)) / (2 * Math.PI) + CORE_R * 0.55);
}

const boxHalf = (r) => r + KNOB_OUT + 12;
const portAngle = (i, n) => (i * 360) / n;
const polar = (r, deg) => {
  const rad = ((deg - 90) * Math.PI) / 180;
  return { x: r * Math.cos(rad), y: r * Math.sin(rad) };
};

/* ---------------------------------------------------------------- 模块状态 */

let host = null, world = null, linksG = null, fwdG = null, menu = null, pool = null, emptyEl = null;
let ready = false;                 // 布局是否已经从后端取回
let loading = false;
let needFit = false;               // 一次都没摆过：第一次画完要自动框进视野
let iface = '';                    // 画布自己的接口筛选，和概览那个互不影响
let filter = '';
let selectedKey = null;
let lastSelectedBubbleKey = null;
let lastChainKey = null;
let pos = new Map();               // 节点键 -> [x, y]
let nodeEls = new Map();           // 节点键 -> 元素（复用：轮询不重建，悬停和拖拽才不会断）
let rotState = new Map();          // 气泡键 -> 已经转过的角度（只增不减，指针才永远向前转）
let drag = null;
let saveTimer = 0;
let view = { x: 0, y: 0, z: 1 };

const BUBBLE_PREFIX = 'm|';
const bubKey = (model, proto) => `${BUBBLE_PREFIX}${model}|${proto}`;

function bubbleOf(key) {
  for (const row of state.routes) if (bubKey(row.model_name, row.protocol) === key) return row;
  return null;
}

const orderedCands = orderedCandidates;

function groupLabelOf(gid) {
  const up = upstreamOfGroup(gid);
  if (!up) return `#${gid}`;
  const grp = (up.groups || []).find((g) => g.id === gid);
  return up.groups.length > 1 && grp ? `${up.name} · ${grp.name}` : up.name;
}

function candLabel(cand) {
  const base = groupLabelOf(cand.group_id);
  const { bare } = splitOneM(cand.remote_model);
  return bare && bare !== cand.upstream_name ? `${base} · ${bare}` : base;
}

const candUsable = (c) => Boolean(c.upstream_enabled && c.group_enabled);
const coolingLeft = (c) => Math.max(0, c.cooling_ms || 0);

/* ---------------------------------------------------------------- 数据派生 */

const visibleBubbles = () => state.routes.filter((row) =>
  protocolOn(row.protocol)
  && (!iface || row.protocol === iface)
  && (!filter || row.model_name.toLowerCase().includes(filter)));

/* ---------------------------------------------------------------- 位置 */

function autoPlace() {
  const bubbles = [...visibleBubbles()].sort((a, b) =>
    a.protocol.localeCompare(b.protocol) || a.model_name.localeCompare(b.model_name));
  const cols = Math.max(2, Math.ceil(Math.sqrt(bubbles.length * 1.4)));
  bubbles.forEach((row, i) => {
    const key = bubKey(row.model_name, row.protocol);
    if (!pos.has(key)) {
      const half = boxHalf(ringRadius(row.candidates.length));
      let slot = i;
      let point;
      do {
        point = [150 + (slot % cols) * 320, 130 + Math.floor(slot / cols) * 330];
        slot += 1;
      } while (bubbles.some(other => {
        const taken = pos.get(bubKey(other.model_name, other.protocol));
        if (!taken) return false;
        const gap = half + boxHalf(ringRadius(other.candidates.length)) + 16;
        return Math.abs(taken[0] - point[0]) < gap && Math.abs(taken[1] - point[1]) < gap;
      }));
      pos.set(key, point);
    }
  });
}

const posOf = (key) => pos.get(key) || [0, 0];

function markDirty() {
  if (saveTimer) clearTimeout(saveTimer);
  saveTimer = setTimeout(flushLayout, 600);
}

async function flushLayout() {
  saveTimer = 0;
  try {
    await api('PUT', '/admin/api/canvas-layout', {
      nodes: Object.fromEntries(pos),
      view: { x: Math.round(view.x), y: Math.round(view.y), z: view.z },
    });
  } catch (e) {
    toast(`画布位置没存上：${e.message}`, 'err');
  }
}

async function ensureLayout() {
  if (ready || loading) return;
  loading = true;
  try {
    const data = await api('GET', '/admin/api/canvas-layout');
    pos = new Map(Object.entries(data.nodes || {})
      .map(([k, v]) => [k, [Number(v[0]) || 0, Number(v[1]) || 0]]));
    const v = data.view || {};
    view = { x: Number(v.x) || 0, y: Number(v.y) || 0, z: clamp(Number(v.z) || 1, Z_MIN, Z_MAX) };
    needFit = pos.size === 0;
  } catch (e) {
    toast(`画布布局读取失败，先按自动排列显示：${e.message}`, 'err');
  } finally {
    ready = true;
    loading = false;
  }
}

/* ---------------------------------------------------------------- 视图变换 */

function applyView() {
  if (!world) return;
  world.style.transform = `translate(${view.x}px, ${view.y}px) scale(${view.z})`;
  for (const g of [linksG, fwdG]) {
    g.setAttribute('transform', `translate(${view.x} ${view.y}) scale(${view.z})`);
  }
}

function toWorld(clientX, clientY) {
  const rect = host.getBoundingClientRect();
  return {
    x: (clientX - rect.left - view.x) / view.z,
    y: (clientY - rect.top - view.y) / view.z,
  };
}

function zoomAt(clientX, clientY, factor) {
  const rect = host.getBoundingClientRect();
  const sx = clientX - rect.left;
  const sy = clientY - rect.top;
  const next = clamp(view.z * factor, Z_MIN, Z_MAX);
  if (next === view.z) return;
  view.x = sx - ((sx - view.x) / view.z) * next;
  view.y = sy - ((sy - view.y) / view.z) * next;
  view.z = next;
  applyView();
  markDirty();
}

/** 框住当前筛选可见的气泡；隐藏节点和历史上游坐标不参与适配。 */
export function fitCanvas() {
  const rect = host.getBoundingClientRect();
  if (rect.width < 80 || rect.height < 80) return false;
  const keys = visibleBubbles().map(row => bubKey(row.model_name, row.protocol));
  if (!keys.length) return false;
  let x0 = Infinity, y0 = Infinity, x1 = -Infinity, y1 = -Infinity;
  for (const key of keys) {
    const [x, y] = posOf(key);
    const half = boxHalf(ringRadius(bubbleOf(key)?.candidates.length || 0));
    x0 = Math.min(x0, x - half); x1 = Math.max(x1, x + half);
    y0 = Math.min(y0, y - half); y1 = Math.max(y1, y + half);
  }
  const pad = 56;
  const z = clamp(Math.min(
    (rect.width - pad * 2) / Math.max(1, x1 - x0),
    (rect.height - pad * 2) / Math.max(1, y1 - y0),
  ), FIT_MIN, 1.05);
  const prevView = { ...view };
  view = {
    z,
    x: Math.max(24, (rect.width - (x1 - x0) * z) / 2) - x0 * z,
    y: Math.max(24, (rect.height - (y1 - y0) * z) / 2) - y0 * z,
  };
  applyView();
  if (!needFit) animateCanvasCamera(world, [linksG, fwdG], prevView, view);
  markDirty();
  return true;
}

/* ---------------------------------------------------------------- 渲染 */

function paint(el, html) {
  if (el.__html !== html) {
    el.innerHTML = html;
    el.__html = html;
    return true;
  }
  return false;
}

function renderChain() {
  const panel = $('cv-chain');
  const row = bubbleOf(selectedKey);
  if (!row) {
    lastChainKey = null;
    if (paint(panel, '<p>选择一个下游气泡，展开它的候选链。</p>')) {
      slideIn(panel, { duration: 300, distance: 6 });
    }
    return;
  }
  const sameChain = lastChainKey === selectedKey;
  const prevCardRects = sameChain
    ? captureRects(panel, '.cv-chain-card', (el) => el.querySelector('[data-rid]')?.dataset.rid)
    : null;
  lastChainKey = selectedKey;

  const data = `data-model="${esc(row.model_name)}" data-proto="${esc(row.protocol)}"`;
  const button = (act, label, extra = '', disabled = false) =>
    `<button type="button" class="btn btn-ghost btn-sm" data-act="${act}" ${data} ${extra}${disabled ? ' disabled' : ''}>${label}</button>`;
  const cands = orderedCands(row);
  const failover = Boolean(state.failover.enabled[row.protocol]);
  const attempts = attemptOrder(row, failover);
  const preferred = cands.find((c) => c.route_id === row.preferred_route_id);
  let body;
  if (row.forward_to) {
    const path = forwardPath(row, state.routes);
    body = `<p class="cv-forward-path">全部转发：${esc(row.model_name)} → ${path
      ? path.map((r) => esc(r.model_name)).join(' → ') : `${esc(row.forward_to)}（链路不可用）`}</p>
      <p>自己的 ${cands.length} 个候选保留，但不参与路由。取消转发后恢复。</p>
      ${button('cv-unforward', '取消全部转发')}`;
  } else {
    body = `<p class="cv-route-summary">保存的首选：${preferred ? esc(candLabel(preferred)) : '未指定'}<br>
      当前可用起点：${attempts.length ? esc(candLabel(attempts[0])) : '无可用候选'} · 自动降级${failover ? '已开启' : '已关闭'}
      <br>候选按箭头排序；首选先试，冷却候选在启用降级时后移。此处不是单次请求的实时轨迹。</p>
      <div class="cv-chain-list">${cands.map((c, i) => {
        const extra = `data-rid="${c.route_id}"`;
        return `<article class="cv-chain-card${c.route_id === row.preferred_route_id ? ' is-preferred' : ''}">
          <b>${i + 1}. ${esc(c.remote_model)}</b><span>${esc(groupLabelOf(c.group_id))}</span>
          <small>${!candUsable(c) ? '停用' : coolingLeft(c) ? '冷却中' : '可用'}${c.route_id === row.preferred_route_id ? ' · 首选' : ''}</small>
          <div>${button('cv-switch', '设为首选', extra, !candUsable(c) || c.route_id === row.preferred_route_id)}
          ${button('cv-move', '前移', `${extra} data-delta="-1"`, i === 0)}
          ${button('cv-move', '后移', `${extra} data-delta="1"`, i === cands.length - 1)}</div>
          <div>${button('cv-edit-cand', '编辑', extra)}${button('cv-del-cand', '移除', extra)}</div></article>`;
      }).join('<span class="cv-chain-arrow" aria-hidden="true">→</span>')}
      ${button('cv-add-cand', '＋ 连接上游')}</div>`;
  }
  const changed = paint(panel, `<div class="cv-chain-head"><b>${esc(row.model_name)}</b><span>${esc(PROTO_LABEL[row.protocol] || row.protocol)}</span>
    ${button('cv-forward', '全部转发到…')}${button('cv-del-model', '删除模型')}</div>${body}`);

  if (changed) {
    if (!sameChain) {
      const head = panel.querySelector('.cv-chain-head');
      if (head) slideIn(head, { duration: 300, distance: 6 });
      const cards = panel.querySelectorAll('.cv-chain-card');
      if (cards.length) {
        enterStagger(cards, { axis: 'x', step: 28, duration: 380, distance: 12 });
      }
    } else if (prevCardRects?.size) {
      flipElements(panel, '.cv-chain-card', (el) => el.querySelector('[data-rid]')?.dataset.rid, prevCardRects, { staggerNew: true });
    }
  }
}

function buildBubble() {
  const el = document.createElement('div');
  el.className = 'cv-node cv-bub';
  el.innerHTML = `
    <div class="cv-dial"></div>
    <button type="button" class="cv-needle" aria-label="拨盘：转到哪条就切到哪条">
      <i class="cv-line"></i><i class="cv-knob"></i>
    </button>
    <div class="cv-ports"></div>
    <button type="button" class="cv-core" aria-label="展开候选链"></button>
    <button type="button" class="cv-tag" data-act="cv-menu" title="点这里改这个模型：加候选 / 交接 / 删除"></button>
    <button type="button" class="cv-wire" title="拖到另一个模型上：整条链交给它">↦</button>`;
  return el;
}

function portsHtml(row, n, R) {
  return orderedCands(row).map((c, i) => {
    const p = polar(R, portAngle(i, n));
    const u = polar(1, portAngle(i, n));
    const usable = candUsable(c);
    const cool = coolingLeft(c);
    const on = attemptOrder(row, state.failover.enabled[row.protocol])[0]?.route_id === c.route_id;
    const cls = ['cv-port'];
    if (on) cls.push('is-on');
    if (row.preferred_route_id === c.route_id) cls.push('is-preferred');
    if (!usable) cls.push('is-off');
    if (cool) cls.push('is-cooling');
    const why = `${candLabel(c)}${usable ? '' : '（停用中）'}${cool ? `　冷却 ${Math.ceil(cool / 1000)}s` : ''}`;
    return `<button type="button" class="${cls.join(' ')}"
      style="left:calc(50% + ${p.x.toFixed(1)}px);top:calc(50% + ${p.y.toFixed(1)}px);--lx:${u.x.toFixed(3)};--ly:${u.y.toFixed(3)}"
      data-rid="${c.route_id}" title="${esc(why)}">
      <span class="cv-port-n">${i + 1}</span>
      <span class="cv-port-label">${esc(why)}</span>
    </button>`;
  }).join('');
}

function coreHtml(row) {
  const cands = orderedCands(row);
  const meta = row.forward_to
    ? `→ ${row.forward_to}`
    : (cands.length ? `${cands.length} 个候选` : '没有候选');
  return `<b class="cv-name" title="${esc(row.model_name)}">${esc(row.model_name)}</b>
    <span class="cv-meta" title="${esc(meta)}">${esc(meta)}</span>`;
}

function forwardArrowTarget(row) {
  if (!row.forward_to) return null;
  return state.routes.find(
    (r) => r.model_name === row.forward_to && r.protocol === row.protocol) || null;
}

function forwardTarget(row) {
  return forwardPath(row, state.routes)?.at(-1) || (!row.forward_to && row.candidates.length ? row : null);
}

const forwardDead = (row) => Boolean(row.forward_to) && forwardTarget(row) === null;

function paintBubble(el, row) {
  const cands = orderedCands(row);
  const n = cands.length || 1;
  const R = ringRadius(cands.length);
  const half = boxHalf(R);
  el.style.width = `${half * 2}px`;
  el.style.height = `${half * 2}px`;
  el.style.setProperty('--r', `${R}px`);
  el.style.setProperty('--kr', `${R + KNOB_OUT}px`);
  el.dataset.key = bubKey(row.model_name, row.protocol);
  el.dataset.model = row.model_name;
  el.dataset.proto = row.protocol;
  el.dataset.protoClass = row.protocol;
  el.classList.toggle('is-fwd', Boolean(row.forward_to));
  el.classList.toggle('is-selected', el.dataset.key === selectedKey);
  el.classList.toggle('is-dead', forwardDead(row));
  el.classList.toggle('is-alone', !row.forward_to && cands.length === 0);

  const dial = el.querySelector('.cv-dial');
  dial.style.width = `${R * 2}px`;
  dial.style.height = `${R * 2}px`;
  paint(el.querySelector('.cv-ports'), portsHtml(row, n, R));
  paint(el.querySelector('.cv-core'), coreHtml(row));
  const tag = el.querySelector('.cv-tag');
  tag.textContent = PROTO_SHORT[row.protocol] || row.protocol;
  tag.dataset.model = row.model_name;
  tag.dataset.proto = row.protocol;
  const [x, y] = posOf(el.dataset.key);
  el.style.left = `${x - half}px`;
  el.style.top = `${y - half}px`;
  paintNeedle(el, row, n);
}

function paintNeedle(el, row, n) {
  const needle = el.querySelector('.cv-needle');
  const cands = orderedCands(row);
  const activeId = row.forward_to ? null : row.preferred_route_id;
  const idx = cands.findIndex((c) => c.route_id === activeId);
  needle.dataset.rid = String(activeId ?? '');
  if (idx < 0) {
    needle.classList.add('is-idle');
    return;
  }
  needle.classList.remove('is-idle');
  const base = portAngle(idx, n);
  const prev = rotState.get(el.dataset.key);
  const target = prev === undefined ? base : base + Math.ceil((prev - base) / 360) * 360;
  rotState.set(el.dataset.key, target);
  needle.style.transform = `rotate(${target}deg)`;
}

function emptyHint() {
  if (filter || iface) return '没有匹配的模型，清掉上面的筛选看看。';
  const hasSource = state.upstreams.some((u) => (u.groups || []).some(
    (g) => protocolOn(g.protocol) && (g.models || []).length));
  return hasSource
    ? '还没有对下游暴露任何模型。点右上角「＋ 下游模型」，再从上游池连接候选。'
    : '还没有可用的上游。先去「上游站点」添加一个供应商、建分组并拉一次模型列表，'
      + '再回这里添加下游模型。';
}

export function renderCanvas() {
  if (!host || state.view !== 'canvas' || document.visibilityState === 'hidden') return;
  if (drag) return;
  if (!ready) { if (!loading) ensureLayout().then(renderCanvas); return; }

  autoPlace();
  const bubbles = visibleBubbles();
  if (!bubbles.some((row) => bubKey(row.model_name, row.protocol) === selectedKey)) {
    selectedKey = bubbles.length ? bubKey(bubbles[0].model_name, bubbles[0].protocol) : null;
  }
  const alive = new Set();
  const freshEls = [];

  for (const row of bubbles) {
    const key = bubKey(row.model_name, row.protocol);
    alive.add(key);
    let el = nodeEls.get(key);
    const isNew = !el;
    if (isNew) {
      el = buildBubble();
      world.append(el);
      nodeEls.set(key, el);
      freshEls.push(el);
    }
    paintBubble(el, row);
  }
  for (const [key, el] of [...nodeEls]) {
    if (alive.has(key)) continue;
    el.remove();
    nodeEls.delete(key);
    rotState.delete(key);
  }

  if (freshEls.length) {
    freshEls.slice(0, 12).forEach((el, i) => {
      springAnimate(
        el,
        [{ opacity: 0, transform: 'scale(0.68)' }, { opacity: 1, transform: 'none' }],
        { duration: 380, delay: i * 24, easing: SPRING.bouncy, fill: 'backwards' },
      );
    });
  }

  if (selectedKey !== lastSelectedBubbleKey) {
    const newBub = nodeEls.get(selectedKey);
    const oldBub = nodeEls.get(lastSelectedBubbleKey);
    lastSelectedBubbleKey = selectedKey;
    animateBubbleSelect(newBub, oldBub);
  }

  drawWires(bubbles);
  applyView();
  renderChain();
  if (needFit && bubbles.length) {
    if (fitCanvas()) needFit = false;
  }

  $('cv-count').textContent = bubbles.length
    ? `${bubbles.length} 个下游模型`
    : '';
  emptyEl.hidden = bubbles.length > 0;
  if (!bubbles.length) emptyEl.textContent = emptyHint();
}

function drawWires(bubbles) {
  const forwards = [];

  for (const row of bubbles) {
    if (!row.forward_to) continue;
    if (bubKey(row.model_name, row.protocol) !== selectedKey) continue;
    const target = forwardArrowTarget(row);
    if (target && !bubbles.includes(target)) continue;
    const [x0, y0] = posOf(bubKey(row.model_name, row.protocol));
    if (!target) {
      forwards.push(`<path class="cv-fwd is-dead" d="M ${x0 + 30} ${y0} l 44 -30"
        marker-end="url(#cv-ah-fwd)"></path>`);
      continue;
    }
    const [x1, y1] = posOf(bubKey(target.model_name, target.protocol));
    const dx = x1 - x0, dy = y1 - y0;
    const len = Math.hypot(dx, dy) || 1;
    const r0 = CORE_R;
    const r1 = CORE_R;
    const cls = `cv-fwd${forwardDead(row) ? ' is-dead' : ''}`;
    forwards.push(`<path class="${cls}" d="M ${(x0 + (dx / len) * r0).toFixed(1)} ${(y0 + (dy / len) * r0).toFixed(1)} L ${(x1 - (dx / len) * r1).toFixed(1)} ${(y1 - (dy / len) * r1).toFixed(1)}" marker-end="url(#cv-ah-fwd)"></path>`);
  }

  linksG.innerHTML = '';
  fwdG.innerHTML = forwards.join('');
}

/* ---------------------------------------------------------------- 上游池 */

function buildPool() {
  pool.innerHTML = `<div class="cv-pool-head"><b>上游池</b>
      <input type="search" id="cv-pool-filter" placeholder="过滤模型…" aria-label="过滤上游模型">
    </div><div class="cv-pool-body"></div>`;
}

export function renderPool() {
  if (!pool || state.view !== 'canvas' || document.visibilityState === 'hidden') return;
  if (drag) return;
  if (!pool.querySelector('.cv-pool-body')) buildPool();
  const input = pool.querySelector('#cv-pool-filter');
  const q = (input.value || '').trim().toLowerCase();
  const blocks = [];
  for (const up of state.upstreams) {
    const rows = [];
    for (const g of (up.groups || [])) {
      if (!protocolOn(g.protocol) || (iface && g.protocol !== iface)) continue;
      const models = (g.models || []).filter((m) => !q || m.toLowerCase().includes(q));
      if (!models.length) continue;
      rows.push(`<div class="cv-pool-grp">${esc(g.name)}<i>${esc(PROTO_SHORT[g.protocol] || g.protocol)}</i></div>`
        + models.map((m) => `<button type="button" class="cv-chip" data-gid="${g.id}" data-remote="${esc(m)}"
            data-proto="${esc(g.protocol)}"${up.enabled && g.enabled ? '' : ' disabled'}
            title="${esc(up.name)} · ${esc(g.name)} → ${esc(m)}${up.enabled && g.enabled ? '' : '（停用中）'}">${esc(m)}</button>`).join(''));
    }
    if (rows.length) {
      blocks.push(`<section class="cv-pool-up">
        <h4><button type="button" class="cv-pool-open" data-act="cv-open-up" data-uid="${up.id}"
          title="去「上游站点」看这个供应商">${esc(up.name)}</button></h4>${rows.join('')}</section>`);
    }
  }
  const body = pool.querySelector('.cv-pool-body');
  const html = blocks.join('') || '<p class="dim" style="font-size:12px;margin:0">没有可拖的上游模型。'
    + '先去「上游站点」给某个分组拉一次模型列表。</p>';
  if (body.__html !== html) {
    body.innerHTML = html;
    body.__html = html;
    enterStagger(body.children, { step: 22, duration: 320, distance: 6 });
  }
}

/* ---------------------------------------------------------------- 菜单 */

function hideMenu() {
  if (!menu) return;
  menu.hidden = true;
  document.querySelectorAll('.cv-node.is-focus').forEach((el) => el.classList.remove('is-focus'));
}

function showMenu(items, el, ev) {
  menu.innerHTML = items.map((it) => it.sep
    ? '<i class="cv-menu-sep"></i>'
    : `<button type="button" role="menuitem" class="${it.danger ? 'is-danger' : ''}" data-act="${it.act}"
        ${Object.entries(it.data || {}).map(([k, v]) => `data-${k}="${esc(v)}"`).join(' ')}
        ${it.disabled ? 'disabled' : ''}>${esc(it.label)}</button>`).join('');
  menu.hidden = false;
  el?.classList.add('is-focus');
  const rect = host.getBoundingClientRect();
  const w = menu.offsetWidth, h = menu.offsetHeight;
  let left = ev.clientX - rect.left + 6;
  let top = ev.clientY - rect.top + 6;
  if (left + w > rect.width - 8) left = Math.max(8, ev.clientX - rect.left - w - 6);
  if (top + h > rect.height - 8) top = Math.max(8, ev.clientY - rect.top - h - 6);
  menu.style.left = `${left}px`;
  menu.style.top = `${top}px`;
  animateMenuOpen(menu);
}

function portMenuItems(d, c, n) {
  const items = [
    { act: 'cv-switch', label: '切到这条', data: d, disabled: !candUsable(c) },
    { act: 'cv-edit-cand', label: '改这条候选…', data: d },
    { act: 'cv-verdict', label: '这条现在能不能用', data: d },
    { sep: true },
  ];
  items.push({
    act: 'cv-del-cand',
    label: n === 1 ? '移除（这个模型就没了）' : '移除这条候选',
    data: d, danger: true,
  });
  return items;
}

function bubbleMenuItems(d, row) {
  const items = [{ act: 'cv-add-cand', label: '加一个候选…', data: d }];
  if (row.forward_to) {
    items.push({ act: 'cv-forward', label: `改交接目标（现在 → ${row.forward_to}）`, data: d });
    items.push({ act: 'cv-unforward', label: '取消交接，用回自己的候选', data: d });
  } else if (row.candidates.length) {
    items.push({ act: 'cv-forward', label: '整条链交给另一个模型…', data: d });
  }
  items.push({ sep: true });
  items.push({ act: 'cv-del-model', label: '删除这个模型', data: d, danger: true });
  return items;
}

/* ---------------------------------------------------------------- 写操作 */

export async function switchTo(model, proto, rid) {
  const row = bubbleOf(bubKey(model, proto));
  const c = row && row.candidates.find((x) => x.route_id === Number(rid));
  if (!c) return;
  if (!c.upstream_enabled) return toast('这个供应商是停用状态，先在「上游站点」里启用它', 'err');
  if (!c.group_enabled) return toast('这个分组是停用状态，展开那一行把它打开', 'err');
  if (row.forward_to) return toast('先取消全部转发，再选择自己的首选', 'err');
  if (row.preferred_route_id === c.route_id) return;
  await api('POST', '/admin/api/models/switch', { route_id: c.route_id });
  await hooks.refreshConfig();
  toast(`${model} → ${candLabel(c)}`, 'ok');
}

export async function removeCandidate(model, proto, rid) {
  const row = bubbleOf(bubKey(model, proto));
  const c = row && row.candidates.find((x) => x.route_id === Number(rid));
  if (!c) return;
  const last = row.candidates.length === 1;
  const okay = await confirmBox({
    title: last ? '移除最后一个候选' : '移除候选',
    body: last
      ? `<b>${esc(model)}</b> 只剩这一个候选，移除后它就不再对下游暴露了。`
      : `把 <b>${esc(model)}</b> 的候选 <b>${esc(candLabel(c))}</b> 去掉？`
        + '<br><br>如果它正好是当前生效的，流量会自动落到剩下的候选之一。',
    ok: '移除',
  });
  if (!okay) return;
  await api('DELETE', `/admin/api/models?route_id=${c.route_id}`);
  await hooks.refreshConfig();
  toast('已移除', 'ok');
}

export async function removeModel(model, proto) {
  const row = bubbleOf(bubKey(model, proto));
  const n = row ? row.candidates.length : 0;
  const okay = await confirmBox({
    title: '删除模型',
    body: `删掉 <b>${esc(model)}</b> 在${esc(PROTO_LABEL[proto] || proto)}下的全部 ${n} 条候选`
      + `${row && row.forward_to ? '，以及它指向别处的交接' : ''}，它将不再从这个接口暴露。`,
    ok: '删除',
  });
  if (!okay) return;
  await api('DELETE', `/admin/api/models?model_name=${encodeURIComponent(model)}`
    + `&protocol=${encodeURIComponent(proto)}`);
  await hooks.refreshConfig();
  toast('已删除', 'ok');
}

async function attachCandidate(model, proto, group_id, remote_model) {
  const row = bubbleOf(bubKey(model, proto));
  if (!row) return;
  const grp = groupOf(group_id);
  if (!grp) return toast('这个分组已经不在了，刷新一下页面', 'err');
  if (grp.protocol !== proto) {
    return toast(`${PROTO_LABEL[proto] || proto} 的模型只能挂同接口的分组，`
      + `「${grp.name}」是 ${PROTO_LABEL[grp.protocol] || grp.protocol} 接口`, 'err');
  }
  if (row.candidates.some((c) => c.group_id === group_id && c.remote_model === remote_model)) {
    return toast('这条已经在链上了', 'err');
  }
  await api('POST', '/admin/api/models', { model_name: model, group_id, remote_model });
  await hooks.refreshConfig();
  toast(`${model} 挂上 ${groupLabelOf(group_id)} → ${remote_model}`, 'ok');
}

export async function setForward(model, proto, target) {
  await api('POST', '/admin/api/models/forward', {
    model_name: model, protocol: proto, target_model: target,
  });
  await hooks.refreshConfig();
  toast(`${model} 的整条链交给 ${target}`, 'ok');
}

export async function clearForward(model, proto) {
  await api('DELETE', '/admin/api/models/forward'
    + `?model_name=${encodeURIComponent(model)}&protocol=${encodeURIComponent(proto)}`);
  await hooks.refreshConfig();
  toast(`${model} 用回自己的候选链`, 'ok');
}

/* ---------------------------------------------------------------- 交接弹窗 */

let fwdEditing = null;

export function openForwardDialog(model, proto) {
  const row = bubbleOf(bubKey(model, proto));
  if (!row) return;
  fwdEditing = { model, proto };
  const pool = forwardTargets(row, state.routes);
  if (!pool.length) {
    return toast('这个接口下没有可用的转发目标（目标须有完整候选链且不能成环）', 'err');
  }
  $('cv-fwd-title').textContent = `「${model}」整条链交给…`;
  $('cv-fwd-hint').innerHTML = `接口：<b>${esc(PROTO_LABEL[proto] || proto)}</b>。`
    + '交接后请求直接按目标的链走，包括它的候选顺序和自动降级。';
  $('cv-fwd-target').innerHTML = pool.map((r) =>
    `<option value="${esc(r.model_name)}">${esc(r.model_name)}（${r.candidates.length} 个候选）</option>`).join('');
  if (row.forward_to) $('cv-fwd-target').value = row.forward_to;
  $('cv-fwd-clear').hidden = !row.forward_to;
  $('cv-fwd-save').textContent = row.forward_to ? '改指' : '交接';
  $('cv-fwd-dialog').showModal();
}

export async function saveForward() {
  if (!fwdEditing) return;
  const target = $('cv-fwd-target').value;
  if (!target) return toast('先选一个目标模型', 'err');
  const pending = fwdEditing;
  await setForward(pending.model, pending.proto, target);
  if (fwdEditing === pending) $('cv-fwd-dialog').close();
}

export async function clearForwardFromDialog() {
  if (!fwdEditing) return;
  const pending = fwdEditing;
  await clearForward(pending.model, pending.proto);
  if (fwdEditing === pending) $('cv-fwd-dialog').close();
}

/* ---------------------------------------------------------------- 手势 */

function bubbleUnder(clientX, clientY) {
  const el = document.elementFromPoint(clientX, clientY);
  const node = el && el.closest ? el.closest('.cv-bub') : null;
  return node ? { model: node.dataset.model, proto: node.dataset.proto, el: node } : null;
}

function ghostChip(text, clientX, clientY) {
  const el = document.createElement('div');
  el.className = 'cv-ghost';
  el.textContent = text;
  el.style.left = `${clientX}px`;
  el.style.top = `${clientY}px`;
  document.body.append(el);
  return el;
}

function listen() {
  window.addEventListener('pointermove', onMove);
  window.addEventListener('pointerup', onUp);
}

function unlisten() {
  window.removeEventListener('pointermove', onMove);
  window.removeEventListener('pointerup', onUp);
  host?.classList.remove('is-panning');
}

function onDown(ev) {
  if (ev.button !== 0) return;
  hideMenu();
  if (ev.target.closest('[data-act]')) return;
  const bubble = ev.target.closest('.cv-bub');

  if (ev.target.closest('.cv-needle')) return startDial(ev, bubble);
  if (ev.target.closest('.cv-wire')) return startForward(ev, bubble);
  if (ev.target.closest('.cv-port')) return startPort(ev, ev.target.closest('.cv-port'));
  const node = ev.target.closest('.cv-node');
  if (node) return startNode(ev, node);
  startPan(ev);
}

function onWheel(ev) {
  ev.preventDefault();
  zoomAt(ev.clientX, ev.clientY, ev.deltaY < 0 ? 1.1 : 1 / 1.1);
}

function startPan(ev) {
  drag = { kind: 'pan', moved: false, start: { x: ev.clientX, y: ev.clientY }, origin: { ...view } };
  host.classList.add('is-panning');
  listen();
}

function startNode(ev, el) {
  const key = el.dataset.key;
  if (!key) return startPan(ev);
  const [x, y] = posOf(key);
  const p = toWorld(ev.clientX, ev.clientY);
  drag = {
    kind: 'node', key, el, moved: false,
    start: { x: ev.clientX, y: ev.clientY },
    grab: { x: p.x - x, y: p.y - y },
  };
  el.classList.add('is-drag');
  listen();
}

function startDial(ev, bub) {
  if (!bub) return;
  const row = bubbleOf(bub.dataset.key);
  if (!row || row.forward_to || !row.candidates.length) return;
  drag = {
    kind: 'dial', key: bub.dataset.key, el: bub, moved: false, rid: null,
    start: { x: ev.clientX, y: ev.clientY },
  };
  bub.classList.add('is-dialing');
  listen();
}

function startPort(ev, port) {
  const bub = port.closest('.cv-bub');
  drag = {
    kind: 'port', key: bub.dataset.key, el: bub, port, moved: false,
    rid: Number(port.dataset.rid),
    start: { x: ev.clientX, y: ev.clientY },
  };
  listen();
}

function startForward(ev, bub) {
  if (!bub) return;
  const key = bub.dataset.key;
  const temp = document.createElementNS('http://www.w3.org/2000/svg', 'path');
  temp.setAttribute('class', 'cv-fwd is-live');
  fwdG.append(temp);
  drag = {
    kind: 'fwd', key, el: bub, moved: false, temp, over: null,
    center: posOf(key),
    start: { x: ev.clientX, y: ev.clientY },
  };
  bub.classList.add('is-wiring');
  listen();
}

function onMove(ev) {
  if (!drag) return;
  const dx = ev.clientX - drag.start.x;
  const dy = ev.clientY - drag.start.y;
  if (!drag.moved && Math.hypot(dx, dy) > HIT) drag.moved = true;

  if (drag.kind === 'pan') {
    view.x = drag.origin.x + dx;
    view.y = drag.origin.y + dy;
    applyView();
    return;
  }
  if (drag.kind === 'node') {
    const p = toWorld(ev.clientX, ev.clientY);
    const x = p.x - drag.grab.x, y = p.y - drag.grab.y;
    pos.set(drag.key, [x, y]);
    const w = parseFloat(drag.el.style.width) / 2;
    const h = parseFloat(drag.el.style.height) / 2;
    drag.el.style.left = `${x - w}px`;
    drag.el.style.top = `${y - h}px`;
    drawWires(visibleBubbles());
    return;
  }
  if (drag.kind === 'pool') {
    if (!drag.moved) return;
    if (!drag.ghost) drag.ghost = ghostChip(drag.label, ev.clientX, ev.clientY);
    drag.ghost.style.left = `${ev.clientX}px`;
    drag.ghost.style.top = `${ev.clientY}px`;
    const over = bubbleUnder(ev.clientX, ev.clientY);
    drag.ghost.classList.toggle('is-over', Boolean(over && over.proto === drag.grpProto));
    return;
  }
  if (drag.kind === 'dial') {
    const row = bubbleOf(drag.key);
    if (!row) return;
    const cands = orderedCands(row);
    const n = cands.length || 1;
    const p = toWorld(ev.clientX, ev.clientY);
    const [bx, by] = posOf(drag.key);
    const deg = ((Math.atan2(p.x - bx, by - p.y) * 180) / Math.PI + 360) % 360;
    const idx = Math.round((deg / 360) * n) % n;
    drag.idx = idx;
    const needle = drag.el.querySelector('.cv-needle');
    const c = cands[idx];
    drag.rid = c ? c.route_id : null;
    if (needle) needle.style.transform = `rotate(${deg}deg)`;
    return;
  }
  if (drag.kind === 'fwd') {
    const p = toWorld(ev.clientX, ev.clientY);
    const [x0, y0] = drag.center;
    drag.temp.setAttribute('d', `M ${x0} ${y0} L ${p.x.toFixed(1)} ${p.y.toFixed(1)}`);
    const over = bubbleUnder(ev.clientX, ev.clientY);
    drag.temp.classList.toggle('is-bad', Boolean(over && bubKey(over.model, over.proto) === drag.key));
    drag.over = over && bubKey(over.model, over.proto) !== drag.key ? over : null;
  }
}

function onUp(ev) {
  const d = drag;
  drag = null;
  unlisten();
  if (!d) return;
  d.el?.classList.remove('is-drag', 'is-dialing', 'is-wiring');
  d.port?.style.removeProperty('left');
  d.port?.style.removeProperty('top');

  if (d.kind === 'pan') { if (d.moved) markDirty(); return renderCanvas(); }
  if (d.kind === 'node') {
    if (d.moved) markDirty();
    else selectedKey = d.key;
    return renderCanvas();
  }

  if (d.kind === 'pool') {
    d.ghost?.remove();
    if (!d.moved) return;
    const over = bubbleUnder(ev.clientX, ev.clientY);
    if (!over) return toast('把它拖到某个下游气泡上才算挂上');
    if (over.proto !== d.grpProto) {
      return toast(`「${over.model}」是 ${PROTO_LABEL[over.proto] || over.proto} 接口的，`
        + `这个上游是 ${PROTO_LABEL[d.grpProto] || '别的'} 接口 —— 不能跨接口挂`, 'err');
    }
    return run(attachCandidate(over.model, over.proto, d.gid, d.remote));
  }

  const row = bubbleOf(d.key);
  if (!row) return renderCanvas();

  if (d.kind === 'dial') {
    if (!d.moved || d.rid === null) return renderCanvas();
    return run(switchTo(row.model_name, row.protocol, d.rid));
  }

  if (d.kind === 'port') {
    if (!d.moved) return run(switchTo(row.model_name, row.protocol, d.rid));
    return renderCanvas();
  }

  if (d.kind === 'fwd') {
    d.temp.remove();
    const over = d.over || bubbleUnder(ev.clientX, ev.clientY);
    if (!over) return;
    if (bubKey(over.model, over.proto) === d.key) return;
    if (over.proto !== row.protocol) return toast('交接只能在同一个接口内做', 'err');
    return run(setForward(row.model_name, row.protocol, over.model));
  }
}

async function run(promise) {
  try {
    await promise;
  } catch (e) {
    toast(e.message, 'err');
  } finally {
    renderCanvas();
  }
}

function onPoolDown(ev) {
  if (ev.button !== 0) return;
  const chip = ev.target.closest('.cv-chip');
  if (!chip || chip.disabled) return;
  ev.preventDefault();
  const grp = groupOf(Number(chip.dataset.gid));
  drag = {
    kind: 'pool', moved: false, ghost: null,
    start: { x: ev.clientX, y: ev.clientY },
    gid: Number(chip.dataset.gid),
    remote: chip.dataset.remote,
    grpProto: grp ? grp.protocol : '',
    label: chip.textContent,
  };
  listen();
}

/* ---------------------------------------------------------------- 绑定 */

export function initCanvas(injected) {
  Object.assign(hooks, injected);
  if (host) return;
  host = $('cv-host');
  world = $('cv-world');
  linksG = $('cv-links');
  fwdG = $('cv-forwards');
  menu = $('cv-menu');
  pool = $('cv-pool');
  emptyEl = $('cv-empty');
  if (!host) return;

  host.addEventListener('pointerdown', onDown);
  host.addEventListener('click', (ev) => {
    if (ev.detail !== 0) return; // 键盘激活，鼠标选择由拖动结束处理。
    const core = ev.target.closest('.cv-core');
    if (core) { selectedKey = core.closest('.cv-bub').dataset.key; renderCanvas(); }
    const port = ev.target.closest('.cv-port');
    if (port) {
      const row = bubbleOf(port.closest('.cv-bub').dataset.key);
      if (row) run(switchTo(row.model_name, row.protocol, port.dataset.rid));
    }
  });
  host.addEventListener('wheel', onWheel, { passive: false });
  host.addEventListener('contextmenu', (ev) => {
    const port = ev.target.closest('.cv-port');
    if (port) {
      ev.preventDefault();
      const row = bubbleOf(port.closest('.cv-bub').dataset.key);
      const c = row && row.candidates.find((x) => x.route_id === Number(port.dataset.rid));
      if (!row || !c) return;
      return showMenu(portMenuItems(
        { kind: 'port', model: row.model_name, proto: row.protocol, rid: c.route_id },
        c, row.candidates.length,
      ), port, ev);
    }
    const bub = ev.target.closest('.cv-bub');
    if (!bub) return;
    ev.preventDefault();
    const row = bubbleOf(bub.dataset.key);
    if (!row) return;
    showMenu(bubbleMenuItems(
      { kind: 'bubble', model: row.model_name, proto: row.protocol }, row,
    ), bub, ev);
  });

  pool.addEventListener('pointerdown', onPoolDown);
  pool.addEventListener('input', (ev) => {
    if (ev.target.id === 'cv-pool-filter') renderPool();
  });

  document.addEventListener('click', (ev) => {
    if (!menu || menu.hidden) return;
    if (ev.target.closest('#cv-menu') || ev.target.closest('[data-act^="cv-"]')) return;
    hideMenu();
  });
  window.addEventListener('keydown', (ev) => { if (ev.key === 'Escape') hideMenu(); });
  new ResizeObserver(() => {
    if (state.view !== 'canvas') return;
    if (needFit) renderCanvas();
    else applyView();
  }).observe(host);
}

/* ---------------------------------------------------------------- 对外 */

export function setCanvasFilter(v) {
  filter = (v || '').trim().toLowerCase();
  renderCanvas();
}

export function setCanvasIface(v) {
  iface = PROTOCOLS.includes(v) ? v : '';
  renderCanvas();
  renderPool();
}

/** 重排：忘掉手动位置，交给自动排列，并用弹簧把每个气泡滑到新位置 */
export async function relayout() {
  const okay = await confirmBox({
    title: '重新排列',
    body: '忘掉手动摆的位置，所有节点重新自动排列。路由配置不受影响。',
    ok: '重排',
    danger: false,
  });
  if (!okay) return;
  const oldPositions = new Map(pos);
  pos = new Map();
  rotState = new Map();
  renderCanvas();
  for (const [key, el] of nodeEls) {
    const prev = oldPositions.get(key);
    const next = pos.get(key);
    if (!prev || !next) continue;
    const dx = prev[0] - next[0];
    const dy = prev[1] - next[1];
    if (Math.hypot(dx, dy) > 1) {
      springAnimate(
        el,
        [{ transform: `translate3d(${dx}px, ${dy}px, 0)` }, { transform: 'none' }],
        { duration: 480, easing: SPRING.bouncy },
      );
    }
  }
  fitCanvas();
  markDirty();
  toast('已重排', 'ok');
}

/** 画布上的按钮都走 app.js 那套 data-act 分发，这里把动作表交给它 */
export const canvasActions = {
  'cv-move': async (d) => {
    const row = bubbleOf(bubKey(d.model, d.proto));
    const order = row && moveCandidate(row, d.rid, d.delta);
    if (!order || row.forward_to) return;
    await api('POST', '/admin/api/models/order', { model_name: d.model, order });
    await hooks.refreshConfig();
  },
  'cv-new-model': () => hooks.openRoute(null, null, iface || null),
  'cv-fit': () => fitCanvas(),
  'cv-reset': () => relayout(),
  'cv-open-up': ({ uid }) => hooks.openUpstream(Number(uid)),
  'cv-menu': (d, el, ev) => {
    const row = bubbleOf(bubKey(d.model, d.proto));
    if (!row) return;
    showMenu(bubbleMenuItems({ model: d.model, proto: d.proto }, row), el, ev);
  },
  'cv-switch': (d) => switchTo(d.model, d.proto, d.rid),
  'cv-edit-cand': (d) => hooks.openRoute(d.model, Number(d.rid), d.proto),
  'cv-add-cand': (d) => hooks.openRoute(d.model, null, d.proto),
  'cv-del-cand': (d) => removeCandidate(d.model, d.proto, d.rid),
  'cv-del-model': (d) => removeModel(d.model, d.proto),
  'cv-forward': (d) => openForwardDialog(d.model, d.proto),
  'cv-unforward': (d) => (d.model ? clearForward(d.model, d.proto) : clearForwardFromDialog()),
  'cv-verdict': (d) => {
    const row = bubbleOf(bubKey(d.model, d.proto));
    const c = row && row.candidates.find((x) => x.route_id === Number(d.rid));
    if (!c) return;
    toast([
      c.upstream_enabled ? '供应商启用中' : '供应商已停用',
      c.group_enabled ? '分组启用中' : '分组已停用',
      coolingLeft(c) ? `分组冷却剩 ${Math.ceil(coolingLeft(c) / 1000)}s` : '没有冷却',
      row.preferred_route_id === c.route_id ? '保存的首选' : '备用候选',
    ].join(' · '), 'ok');
  },
};
