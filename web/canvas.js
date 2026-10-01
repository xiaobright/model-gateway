'use strict';
/* 编排画布：下游气泡 + 候选环（轮盘）+ 上游节点 + 挂载箭头 + 转发箭头。

这套界面只表达三件事，别的一律不进画布：

1. **下游气泡**——一个「模型名 + 接口」= 一条链。一个气泡就是一个下游模型。
2. **候选环（轮盘）**——绕着气泡一圈的端口就是这条链的候选，**顺时针方向就是自动降级的
   尝试顺序**。指针指向的那条是「现在实际会先打的」，端口上的数字是顺序位。
3. **箭头**——上游节点连到端口 = 这条上游挂在这个模型上；气泡连气泡 = 整条链交给那边。

两条硬规矩（定死了才好维护）：

- **顺序只有一个出处：环。** 箭头永远不表达顺序，它只表示「挂上了」。所以调顺序是拖端口，
  不是挪箭头。
- **拨盘只「选」，不改结构。** 指针转到哪个端口就是切到那条候选；自动降级是指针自己顺时针
  转到下一个端口。改真名、删候选走端口菜单，别让同一个手势既选又改。

手势分工（同一个气泡上重叠了好几个动作，必须互不抢）：

| 位置 | 左键点 | 拖动 |
| --- | --- | --- |
| 气泡本体 | 无（只是按住） | 挪气泡 |
| 指针 / 针尖圆点 | 无 | **拨盘**：转到最近那个端口，松手就切过去 |
| 端口圆片 | **切到这条** | **调降级顺序** |
| 端口 / 气泡右键 | 打开菜单 | — |
| 气泡上的 ↦ | — | **拉一条交接箭头**到另一个气泡 |
| 上游池的片子 | — | 拖到某个气泡上 = 挂一条候选 |

坐标是纯展示状态，存在 `/admin/api/canvas-layout`，和路由配置完全分开：画布坏了、位置丢了，
只是节点回到自动摆放，不可能把配置带坏。
*/

import {
  $, state, api, toast, esc, clamp, confirmBox, groupOf, upstreamOfGroup, splitOneM,
  PROTO_LABEL, PROTO_SHORT, PROTOCOLS, protocolOn,
} from './util.js';

/* ---------------------------------------------------------------- 注入 */

/* app.js 注入的回调。画布不认识「弹窗」「刷新」这些事：写操作的入口全部从外面进来，
   画布只管画和收手势。 */
const hooks = {
  openRoute: () => {},        // 打开「新增模型 / 加候选 / 改候选」弹窗
  refreshConfig: async () => {},
  openUpstream: () => {},     // 跳到「上游站点」并展开那个供应商
};

/* ---------------------------------------------------------------- 几何 */

const CORE_R = 42;          // 气泡本体半径
const PORT_R = 12;          // 端口圆片半径
const PORT_GAP = 8;         // 端口之间留的弧距，挤不下就把环撑大
const UP_W = 168;           // 上游节点宽
const UP_H = 50;            // 上游节点高
const HIT = 6;              // 位移超过这么多像素才算拖动，否则算点击
/* 缩放下限不能太小：环上的端口是按世界坐标排的，缩到 0.25 时相邻端口在屏幕上会
   叠到一起，点哪个都点不准（点到的是叠在最上面那个）。0.45 时最挤的环仍有 20px 间距 */
const Z_MIN = 0.45, Z_MAX = 2.6;
/* 首次自动框视野时缩放的底线。再往下字就要靠猜了，宁可让上游那几列留在视野外，
   反正它只是参考层，平移一下就能看到 */
const FIT_MIN = 0.62;

/* 针尖要站在环**外面**：和端口同半径的话，端口在 DOM 里排在指针后面、会盖住针尖，
   拖到的就成了端口（变成调顺序），拨盘根本拨不动。往外挪一段就互不相干，
   正好也是老式轮盘那个指挡的位置。 */
const KNOB_OUT = 26;

/** 环半径：候选越多圈越大，保证端口之间还有地方站 */
function ringRadius(n) {
  if (n <= 1) return CORE_R + 28;
  return Math.max(CORE_R + 28, (n * (PORT_R * 2 + PORT_GAP)) / (2 * Math.PI) + CORE_R * 0.55);
}

/** 气泡外框半边长：要装得下环外那个指挡（R + KNOB_OUT + 半径）再留点标签的余量 */
const boxHalf = (r) => r + KNOB_OUT + 12;

/** 第 i 个端口的角度（度）。0 在最上面，顺时针递增 —— 也就是降级的方向。 */
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
let pos = new Map();               // 节点键 -> [x, y]
let nodeEls = new Map();           // 节点键 -> 元素（复用：轮询不重建，悬停和拖拽才不会断）
let rotState = new Map();          // 气泡键 -> 已经转过的角度（只增不减，指针才永远向前转）
let seenActive = new Map();        // 气泡键 -> 上次看到的 active_route_id，用来认自动降级
let drag = null;
let saveTimer = 0;
let view = { x: 0, y: 0, z: 1 };

/* 键的格式写在这儿，和后端 gateway/canvas.py 一一对应。改一处必须改两处。 */
const BUBBLE_PREFIX = 'm|';
const UPSTREAM_PREFIX = 'u|';
const bubKey = (model, proto) => `${BUBBLE_PREFIX}${model}|${proto}`;
const upKey = (group_id, remote) => `${UPSTREAM_PREFIX}${group_id}|${remote}`;

function bubbleOf(key) {
  for (const row of state.routes) if (bubKey(row.model_name, row.protocol) === key) return row;
  return null;
}

/** 候选按 priority 排。后端已经排好了，这里只是不依赖它地再保证一次 —— 环上位置就是顺序 */
const orderedCands = (row) =>
  [...row.candidates].sort((a, b) => (a.priority - b.priority) || (a.route_id - b.route_id));

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

/** 候选能不能用：站和分组都启用着。停用的照样画，只是灰掉 —— 配置还在，不该从画布上消失 */
const candUsable = (c) => Boolean(c.upstream_enabled && c.group_enabled);
const coolingLeft = (c) => Math.max(0, c.cooling_ms || 0);

/* ---------------------------------------------------------------- 数据派生 */

/** 要画的下游气泡：接口没被停用、且过了名字筛选 */
const visibleBubbles = () => state.routes.filter((row) =>
  protocolOn(row.protocol)
  && (!iface || row.protocol === iface)
  && (!filter || row.model_name.toLowerCase().includes(filter)));

/** 所有候选用到的上游节点。同一个 (分组, 真名) 只画一个 —— 几个模型共用它时是同一个节点 */
function upstreamNodes() {
  const map = new Map();
  for (const row of visibleBubbles()) {
    for (const c of row.candidates) {
      const key = upKey(c.group_id, c.remote_model);
      if (!map.has(key)) {
        map.set(key, {
          key, group_id: c.group_id, remote_model: c.remote_model,
          upstream_id: c.upstream_id, upstream_name: c.upstream_name,
          group_name: c.group_name, protocol: c.protocol,
          enabled: candUsable(c),
        });
      }
    }
  }
  return [...map.values()];
}

/* ---------------------------------------------------------------- 位置 */

/** 还没摆过的节点给个可预测的位置：上游一列在左，气泡网格在右。

    间距按「首次打开能一眼看清」定的：气泡 320×330、上游节点 62 高、一列最多 10 个。
    排得太散的话首次适配会缩到 0.5 以下，字就糊了。 */
function autoPlace() {
  const bubbles = [...visibleBubbles()].sort((a, b) =>
    a.protocol.localeCompare(b.protocol) || a.model_name.localeCompare(b.model_name));
  const cols = Math.max(2, Math.ceil(Math.sqrt(bubbles.length * 1.4)));
  bubbles.forEach((row, i) => {
    const key = bubKey(row.model_name, row.protocol);
    if (!pos.has(key)) {
      pos.set(key, [150 + (i % cols) * 320, 130 + Math.floor(i / cols) * 330]);
    }
  });
  const ups = upstreamNodes().sort((a, b) =>
    a.upstream_name.localeCompare(b.upstream_name) || a.remote_model.localeCompare(b.remote_model));
  ups.forEach((node, i) => {
    if (!pos.has(node.key)) {
      pos.set(node.key, [-260 - Math.floor(i / 10) * 200, 70 + (i % 10) * 62]);
    }
  });
}

const posOf = (key) => pos.get(key) || [0, 0];

/** 位置改了先攒着：拖一次会触发几百次 pointermove，不能每次都打接口 */
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
    // 存不上不影响用：画的还是当前这份，只是刷新后回到上次存下的位置
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
    // 一次都没摆过：自动排列的位置可能落在视口外（模型一多就会排到第四列去）。
    // 不自动框一次的话，新用户打开看到的是空白，会以为页面坏了。
    needFit = pos.size === 0;
  } catch (e) {
    // 拿不到就当没摆过：画布必须「能用」优先，不能因为一份坐标打不开
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

/** 屏幕坐标 -> 世界坐标。画布内的所有几何运算都在世界坐标里做 */
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
  // 让光标底下的那个世界点钉在原地：缩放前后它对应的屏幕位置相同
  view.x = sx - ((sx - view.x) / view.z) * next;
  view.y = sy - ((sy - view.y) / view.z) * next;
  view.z = next;
  applyView();
  markDirty();
}

/** 把节点框进视野。返回是否真的算过 —— 视图还没显示出来时量不到尺寸，不能硬算。

    `scope` 决定框谁：首次进来自动框的是**气泡**（上游节点是参考层，为一列竖着排的
    上游把缩放压到 0.5 以下，字就全糊了）；用户手动点「适配」才连上游一起框。 */
export function fitCanvas(scope = 'bubbles') {
  const rect = host.getBoundingClientRect();
  // 切换视图用的是 View Transition，DOM 变形发生在下一帧的回调里：刚 showView 完就
  // 量的话会量到 0×0，算出来的缩放会掉到下限、把所有节点叠成一团
  if (rect.width < 80 || rect.height < 80) return false;
  const prefix = scope === 'all' ? '' : BUBBLE_PREFIX;
  const keys = [...pos.keys()].filter((k) => k.startsWith(prefix));
  if (!keys.length) return false;
  let x0 = Infinity, y0 = Infinity, x1 = -Infinity, y1 = -Infinity;
  for (const key of keys) {
    const [x, y] = posOf(key);
    const half = key.startsWith(BUBBLE_PREFIX) ? boxHalf(ringRadius(6)) : UP_W / 2;
    x0 = Math.min(x0, x - half); x1 = Math.max(x1, x + half);
    y0 = Math.min(y0, y - half); y1 = Math.max(y1, y + half);
  }
  const pad = 56;
  const z = clamp(Math.min(
    (rect.width - pad * 2) / Math.max(1, x1 - x0),
    (rect.height - pad * 2) / Math.max(1, y1 - y0),
  ), scope === 'all' ? Z_MIN : FIT_MIN, 1.05);
  view = {
    z,
    x: (rect.width - (x1 - x0) * z) / 2 - x0 * z,
    y: (rect.height - (y1 - y0) * z) / 2 - y0 * z,
  };
  applyView();
  markDirty();
  return true;
}

/* ---------------------------------------------------------------- 渲染 */

/** 只在内容真的变了才动 innerHTML：15 秒轮询一次，重建会把悬停和选中状态一起抹掉 */
function paint(el, html) {
  if (el.__html !== html) { el.innerHTML = html; el.__html = html; }
}

/* 气泡的骨架建一次就不动了。指针必须是常驻元素：它一变就重建的话，旋转的过渡动画
   永远来不及播 —— 而「自动拨动」正是靠这个过渡看出来的。

   顺序有讲究：指针排在端口**前面**，端口才画在它上面（不然那条线会横穿端口圆片）；
   核心圆排在指针后面，针就被「从圆心里长出来」，只露出针尖那一段。 */
function buildBubble() {
  const el = document.createElement('div');
  el.className = 'cv-node cv-bub';
  el.innerHTML = `
    <div class="cv-dial"></div>
    <button type="button" class="cv-needle" aria-label="拨盘：转到哪条就切到哪条">
      <i class="cv-line"></i><i class="cv-knob"></i>
    </button>
    <div class="cv-ports"></div>
    <div class="cv-core"></div>
    <button type="button" class="cv-tag" data-act="cv-menu" title="点这里改这个模型：加候选 / 交接 / 删除"></button>
    <button type="button" class="cv-wire" title="拖到另一个模型上：整条链交给它">↦</button>`;
  return el;
}

function portsHtml(row, n, R) {
  return orderedCands(row).map((c, i) => {
    const p = polar(R, portAngle(i, n));
    const u = polar(1, portAngle(i, n));      // 向外的单位向量，标签顺着它往外推
    const usable = candUsable(c);
    const cool = coolingLeft(c);
    const on = row.active_route_id === c.route_id && !row.forward_to;
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
  // 交接中的气泡：中间那行直接写出交给谁，比「已交出」有用得多
  const meta = row.forward_to
    ? `→ ${row.forward_to}`
    : (cands.length ? `${cands.length} 个候选` : '没有候选');
  return `<b class="cv-name" title="${esc(row.model_name)}">${esc(row.model_name)}</b>
    <span class="cv-meta" title="${esc(meta)}">${esc(meta)}</span>`;
}

/** 箭头指向**直接**目标那一行（配置里写的就是它）。目标不在画布上就返回 null */
function forwardArrowTarget(row) {
  if (!row.forward_to) return null;
  return state.routes.find(
    (r) => r.model_name === row.forward_to && r.protocol === row.protocol) || null;
}

/** 顺着转发链走到最终落地的那一行。null = 目标不存在、绕成环、或者落地那边没有候选。

    多跳要按后端那套走完整条链：`a → b → c` 里 a 的直接目标是 b（它自己一个候选都没有），
    但整条链是通的，不能因为 b 没有候选就说 a 断了。上限和后端 FORWARD_MAX_HOPS 对齐。 */
function forwardTarget(row) {
  const HOPS = 16;
  let cur = row;
  const seen = new Set([bubKey(row.model_name, row.protocol)]);
  for (let i = 0; i < HOPS; i += 1) {
    if (!cur.forward_to) return cur.candidates.length ? cur : null;
    if (seen.has(bubKey(cur.forward_to, cur.protocol))) return null;   // 绕回来了
    const next = state.routes.find(
      (r) => r.model_name === cur.forward_to && r.protocol === cur.protocol);
    if (!next) return null;
    seen.add(bubKey(next.model_name, next.protocol));
    cur = next;
  }
  return null;
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

/** 指针的角度。只增不减地累加，切到更靠前的端口也不会倒着转回去 —— 降级永远顺时针。 */
function paintNeedle(el, row, n) {
  const needle = el.querySelector('.cv-needle');
  const cands = orderedCands(row);
  const activeId = row.forward_to ? null : row.active_route_id;
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

function paintUpstream(el, node) {
  el.classList.toggle('is-off', !node.enabled);
  el.dataset.proto = node.protocol;
  el.dataset.protoClass = node.protocol;
  paint(el, `<span class="cv-up-name" title="${esc(node.upstream_name)}">${esc(node.upstream_name)}</span>
    <span class="cv-up-remote" title="${esc(node.remote_model)}">${esc(node.remote_model)}</span>
    <span class="cv-up-grp">${esc(node.group_name)}</span>`);
  const [x, y] = posOf(node.key);
  el.style.left = `${x - UP_W / 2}px`;
  el.style.top = `${y - UP_H / 2}px`;
}

/** 空白画布上写什么。刚装好的机器上游也是空的，那时候「从上游池拖一个过来」根本做不到 */
function emptyHint() {
  if (filter || iface) return '没有匹配的模型，清掉上面的筛选看看。';
  const hasSource = state.upstreams.some((u) => (u.groups || []).some(
    (g) => protocolOn(g.protocol) && (g.models || []).length));
  return hasSource
    ? '还没有对下游暴露任何模型。点右上角「＋ 下游模型」，或者从左边的上游池拖一个模型过来。'
    : '还没有可用的上游。先去「上游站点」添加一个供应商、建分组并拉一次模型列表，'
      + '再回这里把模型拖出来。';
}

export function renderCanvas() {
  if (!host) return;
  // 拖拽中间不重画：15 秒一次的配置轮询要是正好落在这里，会把正在拖的那个端口、
  // 或者正在拨的那根指针重建掉，手势当场跟丢。松手时 onUp 自己会重画一次
  if (drag) return;
  if (!ready) { ensureLayout().then(renderCanvas); return; }

  autoPlace();
  const bubbles = visibleBubbles();
  const ups = upstreamNodes();
  const alive = new Set();

  for (const row of bubbles) {
    const key = bubKey(row.model_name, row.protocol);
    alive.add(key);
    let el = nodeEls.get(key);
    if (!el) {
      el = buildBubble();
      world.append(el);
      nodeEls.set(key, el);
    }
    paintBubble(el, row);
  }
  for (const node of ups) {
    alive.add(node.key);
    let el = nodeEls.get(node.key);
    if (!el) {
      el = document.createElement('div');
      el.className = 'cv-node cv-up';
      el.dataset.key = node.key;
      el.style.width = `${UP_W}px`;
      el.style.height = `${UP_H}px`;
      world.append(el);
      nodeEls.set(node.key, el);
    }
    paintUpstream(el, node);
  }
  // 已经不在画面上的节点收掉（被筛掉、被删掉、接口被停用）
  for (const [key, el] of [...nodeEls]) {
    if (alive.has(key)) continue;
    el.remove();
    nodeEls.delete(key);
    rotState.delete(key);
    seenActive.delete(key);
  }

  drawWires(bubbles, ups);
  applyView();
  detectFallback(bubbles);
  // 首次进来框一次视野。量不到尺寸就先不做，下一个 rAF 再试 —— needFit 不清掉，
  // 所以不会漏；量到了就会自己停
  if (needFit && bubbles.length) {
    if (fitCanvas('bubbles')) needFit = false;
    else requestAnimationFrame(() => { if (needFit) renderCanvas(); });
  }

  $('cv-count').textContent = bubbles.length
    ? `${bubbles.length} 个下游模型 · ${ups.length} 个上游`
    : '';
  emptyEl.hidden = bubbles.length > 0;
  if (!bubbles.length) emptyEl.textContent = emptyHint();
}

/** 认「自动降级」：active 变了但没人拨盘，就是断路器把流量拨走了，闪一下让人看见 */
function detectFallback(bubbles) {
  for (const row of bubbles) {
    const key = bubKey(row.model_name, row.protocol);
    const now = row.forward_to ? null : row.active_route_id;
    const was = seenActive.get(key);
    if (was !== undefined && was !== now && !drag && was !== null) {
      const el = nodeEls.get(key);
      if (el) {
        el.classList.add('is-shift');
        setTimeout(() => el.classList.remove('is-shift'), 1000);
      }
    }
    seenActive.set(key, now);
  }
}

function drawWires(bubbles, ups) {
  const upPos = new Map(ups.map((n) => [n.key, posOf(n.key)]));
  const links = [];
  const forwards = [];

  for (const row of bubbles) {
    const key = bubKey(row.model_name, row.protocol);
    const [bx, by] = posOf(key);
    const cands = orderedCands(row);
    const n = cands.length || 1;
    const R = ringRadius(cands.length);
    cands.forEach((c, i) => {
      const from = upPos.get(upKey(c.group_id, c.remote_model));
      if (!from) return;
      const p = polar(R, portAngle(i, n));
      const usable = candUsable(c);
      const on = row.active_route_id === c.route_id && !row.forward_to;
      links.push(`<path class="cv-link${on ? ' is-on' : ''}${usable ? '' : ' is-off'}"
        d="M ${from[0]} ${from[1]} L ${(bx + p.x).toFixed(1)} ${(by + p.y).toFixed(1)}"
        marker-end="url(#${on ? 'cv-ah-on' : 'cv-ah'})"></path>`);
    });
  }

  for (const row of bubbles) {
    if (!row.forward_to) continue;
    const target = forwardArrowTarget(row);
    const [x0, y0] = posOf(bubKey(row.model_name, row.protocol));
    if (!target) {
      // 断链：目标是明确指过的，但那边现在没有这个模型。画根短刺提醒，不偷偷改回自己的候选
      forwards.push(`<path class="cv-fwd is-dead" d="M ${x0 + 30} ${y0} l 44 -30"
        marker-end="url(#cv-ah-fwd)"></path>`);
      continue;
    }
    const [x1, y1] = posOf(bubKey(target.model_name, target.protocol));
    const dx = x1 - x0, dy = y1 - y0;
    const len = Math.hypot(dx, dy) || 1;
    // 箭头正好落在两边的环上（不是更外面），读起来就是「从那个盘指进这个盘」；
    // 落在框边上会离得老远，像没接上
    const r0 = ringRadius(orderedCands(row).length);
    const r1 = ringRadius(orderedCands(target).length);
    const cls = `cv-fwd${forwardDead(row) ? ' is-dead' : ''}`;
    forwards.push(`<path class="${cls}" d="M ${(x0 + (dx / len) * r0).toFixed(1)} ${(y0 + (dy / len) * r0).toFixed(1)} L ${(x1 - (dx / len) * r1).toFixed(1)} ${(y1 - (dy / len) * r1).toFixed(1)}" marker-end="url(#cv-ah-fwd)"></path>`);
  }

  linksG.innerHTML = links.join('');
  fwdG.innerHTML = forwards.join('');
}

/* ---------------------------------------------------------------- 上游池 */

function buildPool() {
  pool.innerHTML = `<div class="cv-pool-head"><b>上游池</b>
      <input type="search" id="cv-pool-filter" placeholder="过滤模型…" aria-label="过滤上游模型">
    </div><div class="cv-pool-body"></div>`;
}

export function renderPool() {
  if (!pool) return;
  if (drag) return;   // 同上：正拖着上游池里的片子时不要重建池子
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
  if (body.__html !== html) { body.innerHTML = html; body.__html = html; }
}

/* ---------------------------------------------------------------- 菜单 */

function hideMenu() {
  if (!menu) return;
  menu.hidden = true;
  document.querySelectorAll('.cv-node.is-focus').forEach((el) => el.classList.remove('is-focus'));
}

/** 光标旁边弹个小菜单。位置按屏幕算，所以菜单不跟着画布缩放 */
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
  if (row.active_route_id === c.route_id) return;
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

/** 从上游池 / 另一个上游节点挂一条候选上来。重复的交给后端 409，这里先说清楚 */
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
  // 目标必须同接口、而且**自己有候选** —— 后端也这么校验，先把不可能的选项藏掉
  const pool = state.routes.filter((r) =>
    r.protocol === proto && r.model_name !== model && r.candidates.length && !r.forward_to);
  if (!pool.length) {
    return toast('这个接口下没有别的可交接模型 —— 目标自己得先有候选链', 'err');
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
  const pending = { ...fwdEditing };
  await setForward(pending.model, pending.proto, target);
  $('cv-fwd-dialog').close();
}

export async function clearForwardFromDialog() {
  if (!fwdEditing) return;
  const pending = { ...fwdEditing };
  await clearForward(pending.model, pending.proto);
  $('cv-fwd-dialog').close();
}

/* ---------------------------------------------------------------- 手势 */

/** 指针落在哪个气泡上（松手时判定）。_ghost 设了 pointer-events:none，不会挡住探测 */
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

/* 花括号里的一堆 pointerdown 分支都从这里进。和 click 分发抢手势的地方只有一个原则：
   带 data-act 的元素归 click 分发管（.cv-tag 这种），其余归手势。 */
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
    drawWires(visibleBubbles(), upstreamNodes());
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
  if (drag.kind === 'dial' || drag.kind === 'port') {
    const row = bubbleOf(drag.key);
    if (!row) return;
    const cands = orderedCands(row);
    const n = cands.length || 1;
    const p = toWorld(ev.clientX, ev.clientY);
    const [bx, by] = posOf(drag.key);
    // 换算成「0 = 正上方、顺时针为正」的角度，正好对上端口的角度定义
    const deg = ((Math.atan2(p.x - bx, by - p.y) * 180) / Math.PI + 360) % 360;
    const idx = Math.round((deg / 360) * n) % n;
    drag.idx = idx;
    const needle = drag.el.querySelector('.cv-needle');
    if (drag.kind === 'dial') {
      const c = cands[idx];
      drag.rid = c ? c.route_id : null;
      if (needle) needle.style.transform = `rotate(${deg}deg)`;
    } else {
      const R = ringRadius(cands.length);
      const off = polar(R, deg);
      drag.port.style.left = `calc(50% + ${off.x.toFixed(1)}px)`;
      drag.port.style.top = `calc(50% + ${off.y.toFixed(1)}px)`;
    }
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

  // 平移和挪节点没有写操作，走不到下面那个 run()；这里补一次重画，
  // 好让拖拽期间被跳过的配置刷新马上补上
  if (d.kind === 'pan') { if (d.moved) markDirty(); return renderCanvas(); }
  if (d.kind === 'node') { if (d.moved) markDirty(); return renderCanvas(); }

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
    // 没拨动就不动配置，只把指针弹回它该在的位置
    if (!d.moved || d.rid === null) return renderCanvas();
    return run(switchTo(row.model_name, row.protocol, d.rid));
  }

  if (d.kind === 'port') {
    if (!d.moved) return run(switchTo(row.model_name, row.protocol, d.rid));
    const cands = orderedCands(row);
    const from = cands.findIndex((c) => c.route_id === d.rid);
    const to = d.idx ?? from;
    if (from < 0 || to < 0 || from === to) return renderCanvas();
    const ids = cands.map((c) => c.route_id);
    ids.splice(to, 0, ids.splice(from, 1)[0]);
    return run((async () => {
      await api('POST', '/admin/api/models/order', { model_name: row.model_name, order: ids });
      await hooks.refreshConfig();
      toast('降级顺序已更新', 'ok');
    })());
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

/* 手势里的写操作统一吞异常并提示，最后都重画一次 —— 失败也不能把画布留在拖拽态 */
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

  // 点空白关菜单。挂在 click 而不是 pointerdown：菜单里的按钮走 click 分发，
  // 在 pointerdown 阶段就关掉的话，动作还没执行菜单就没了
  document.addEventListener('click', (ev) => {
    if (!menu || menu.hidden) return;
    if (ev.target.closest('#cv-menu') || ev.target.closest('[data-act^="cv-"]')) return;
    hideMenu();
  });
  window.addEventListener('keydown', (ev) => { if (ev.key === 'Escape') hideMenu(); });
  new ResizeObserver(() => applyView()).observe(host);
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

/** 重排：忘掉手动位置，交给自动排列。只在用户点「重排」时才做 */
export async function relayout() {
  const okay = await confirmBox({
    title: '重新排列',
    body: '忘掉手动摆的位置，所有节点重新自动排列。路由配置不受影响。',
    ok: '重排',
    danger: false,
  });
  if (!okay) return;
  pos = new Map();
  rotState = new Map();
  seenActive = new Map();
  renderCanvas();
  fitCanvas();
  markDirty();
  toast('已重排', 'ok');
}

/** 画布上的按钮都走 app.js 那套 data-act 分发，这里把动作表交给它 */
export const canvasActions = {
  'cv-new-model': () => hooks.openRoute(null, null, iface || null),
  'cv-fit': () => fitCanvas('all'),
  'cv-reset': () => relayout(),
  'cv-open-up': ({ uid }) => hooks.openUpstream(Number(uid)),
  // 气泡下方那个接口角标：点它是「这个模型能做什么」，和右键气泡同一个菜单
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
      row.active_route_id === c.route_id ? '当前就在打这条' : '现在轮不到它',
    ].join(' · '), 'ok');
  },
};
