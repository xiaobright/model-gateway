/* 共享工具：转义、格式化、HTTP、提示条、确认框。
   这个模块会被 views/charts/app 都 import，所以只放无副作用的纯逻辑
   和 DOM 小工具，不碰业务状态以外的东西。 */

import { animateToastOut } from './motion.js';

export const $ = (id) => document.getElementById(id);

/* ---------------------------------------------------------------- 共享状态 */

export const state = {
  upstreams: [],     // 每个供应商带着自己的 groups
  routes: [],
  stats: null,      // /admin/api/stats?live_only=true：只轮询内存中的活跃数
  overview: null,   // /admin/api/overview：时间线 + 健康 + 热度
  failover: { enabled: {}, breakers: [] },
  tokens: {},
  filter: '',
  iface: '',         // 模型路由按接口筛选：'' | 'anthropic' | 'openai'
  proto: '',         // 转发记录的协议筛选：'' | 'anthropic' | 'openai'
  editing: null,      // 供应商弹窗的目标 id；openUpstream(null) = 新建供应商
  editingUp: null,
  editingGroup: null, // 正在编辑的分组 id，null = 新建
  editingCand: null,  // 正在改的候选 {model, rid}，null = 新增
  openUpstreams: new Set(),  // 「上游站点」里展开了分组的那几行
  view: 'routes',
  window: '24h',
  logIds: new Set(), // 已渲染过的 request_log id，用来做增量 diff
  protocolsReady: false,
  protocolEnabled: {},
  searchTarget: null,
  stallTimeout: null,
};

/** 接口是否启用。没拿到开关状态时按启用处理，避免首屏把东西全藏起来。 */
export const protocolOn = (p) => state.protocolEnabled[p] !== false;

/* ---------------------------------------------------------------- 格式化 */

const ESC_MAP = { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' };

export const esc = (v) => String(v).replace(/[&<>"']/g, (c) => ESC_MAP[c]);

export const fmtInt = (n) => (n ?? 0).toLocaleString('zh-CN');

export function fmtTokens(n) {
  if (n === null || n === undefined) return '-';
  if (n >= 1e6) return (n / 1e6).toFixed(1) + 'M';
  if (n >= 1e4) {
    const k = n / 1e3;
    return k >= 999.95 ? (n / 1e6).toFixed(1) + 'M' : k.toFixed(1) + 'K';
  }
  return String(n);
}

export function fmtBytes(n) {
  if (!n) return '0B';
  if (n >= 1 << 20) return (n / (1 << 20)).toFixed(1) + 'MB';
  if (n >= 1 << 10) return (n / (1 << 10)).toFixed(1) + 'KB';
  return n + 'B';
}

export const fmtDur = (ms) => (ms >= 10000 ? Math.round(ms / 1000) + 's' : (ms / 1000).toFixed(1) + 's');

/** 毫秒 -> 秒，保留一位；给 P95 这类大数用 */
export const fmtSec = (ms) => (ms >= 1000 ? (ms / 1000).toFixed(1) + 's' : Math.round(ms) + 'ms');

/** 冷却剩余：毫秒 -> m:ss / 12s */
export function fmtLeft(ms) {
  const s = Math.max(0, Math.round(ms / 1000));
  return s >= 60 ? `${Math.floor(s / 60)}:${String(s % 60).padStart(2, '0')}` : `${s}s`;
}

/** epoch 秒 -> HH:MM，给时间线横轴用 */
export function fmtClock(epochSec) {
  const d = new Date(epochSec * 1000);
  return `${String(d.getHours()).padStart(2, '0')}:${String(d.getMinutes()).padStart(2, '0')}`;
}

export function fmtAxis(epochSec, bucket) {
  const d = new Date(epochSec * 1000);
  if (bucket >= 86400) return `${d.getMonth() + 1}/${d.getDate()}`;
  return `${String(d.getHours()).padStart(2, '0')}:${String(d.getMinutes()).padStart(2, '0')}`;
}

/* ---------------------------------------------------------------- HTTP */

function detailOf(data) {
  const d = data && data.detail;
  if (!d) return '';
  if (typeof d === 'string') return d;
  if (Array.isArray(d)) return d.map((e) => e.msg || JSON.stringify(e)).join('；');
  return JSON.stringify(d);
}

export async function api(method, path, body) {
  const opts = { method, headers: {} };
  if (body !== undefined) {
    opts.headers['Content-Type'] = 'application/json';
    opts.body = JSON.stringify(body);
  }
  const resp = await fetch(path, opts);
  const text = await resp.text();
  let data = null;
  try { data = text ? JSON.parse(text) : null; } catch { /* 非 JSON 响应 */ }
  if (!resp.ok) throw new Error(detailOf(data) || `${resp.status} ${resp.statusText}`);
  return data;
}

/* ---------------------------------------------------------------- 提示条 */

/* 每次新 toast 都重新 show 一遍：top layer 是后进的在上，这样即使二级弹窗
   已经开着，提示条也会叠在它和它的模糊背景之上，而不是被一起糊掉。 */
function liftToasts() {
  const box = $('toasts');
  if (typeof box.showPopover !== 'function') return;
  if (box.matches(':popover-open')) box.hidePopover();
  box.showPopover();
}

function dropToasts() {
  const box = $('toasts');
  if (box.children.length) return;
  if (typeof box.hidePopover === 'function' && box.matches(':popover-open')) box.hidePopover();
}

export function toast(msg, kind) {
  const box = $('toasts');
  const el = document.createElement('div');
  el.className = 'toast' + (kind ? ' toast-' + kind : '');
  el.innerHTML = `<span class="dot dot-${kind === 'err' ? 'crit' : 'good'}"></span><span></span>`;
  el.lastElementChild.textContent = msg;

  const removeNow = () => { el.remove(); dropToasts(); };
  const kill = () => animateToastOut(el, removeNow);
  el.addEventListener('click', kill);
  box.append(el);
  liftToasts();
  setTimeout(kill, kind === 'err' ? 5000 : 2600);
}

/* 确认框：另一个 <dialog>，比下层弹窗后进 top layer，所以永远在最上面 */
export function confirmBox({ title, body, ok = '确定', danger = true }) {
  const dlg = $('confirm-dialog');
  $('cf-title').textContent = title;
  $('cf-body').innerHTML = body;
  const btn = $('cf-ok');
  btn.textContent = ok;
  btn.className = 'btn ' + (danger ? 'btn-danger-solid' : '');
  dlg.returnValue = '';
  dlg.showModal();
  return new Promise((resolve) => {
    dlg.addEventListener('close', () => resolve(dlg.returnValue === 'ok'), { once: true });
  });
}

/* 点了就禁用按钮，避免重复提交；错误统一弹提示条 */
export async function run(el, fn) {
  if (el && el.disabled) return;
  const wasLocked = Boolean(el && el.dataset.locked);
  if (el) el.disabled = true;
  try {
    await fn();
  } catch (e) {
    toast(e.message, 'err');
  } finally {
    if (el && !wasLocked && !el.dataset.locked) el.disabled = false;
  }
}

/* ---------------------------------------------------------------- 小工具 */

export const clamp = (v, lo, hi) => Math.min(hi, Math.max(lo, v));

export const upstreamOfGroup = (gid) =>
  state.upstreams.find((u) => (u.groups || []).some((g) => g.id === gid)) || null;

export const groupOf = (gid) => {
  const up = upstreamOfGroup(gid);
  return up ? (up.groups.find((g) => g.id === gid) || null) : null;
};

export const catalogOfGroup = (gid) => {
  const grp = groupOf(gid);
  return grp ? [...(grp.models || [])] : [];
};

export const catalogOfUpstream = (uid) => {
  const up = state.upstreams.find((u) => u.id === uid);
  if (!up) return [];
  const seen = new Set();
  for (const g of up.groups || []) for (const m of g.models || []) seen.add(m);
  return [...seen];
};

export const routeCountOfGroup = (gid) =>
  state.routes.reduce((n, r) => n + r.candidates.filter((c) => c.group_id === gid).length, 0);

export const routeCountOfUpstream = (uid) =>
  state.routes.reduce((n, r) => n + r.candidates.filter((c) => c.upstream_id === uid).length, 0);

export function groupLabel(gid) {
  const up = upstreamOfGroup(gid);
  if (!up) return `#${gid}`;
  const grp = up.groups.find((g) => g.id === gid);
  return up.groups.length > 1 && grp ? `${up.name} · ${grp.name}` : up.name;
}

export const groupsOfIface = (upstream, iface) =>
  (upstream.groups || []).filter((g) => !iface || g.protocol === iface);

export const supportsIface = (upstream, iface) =>
  !iface || (upstream.supports || []).includes(iface);

export let PROTO_INFO = Object.create(null);
export let PROTOCOLS = [];
export let PROTO_LABEL = Object.create(null);
export let PROTO_SHORT = Object.create(null);
export let PROTO_PATH = Object.create(null);
export let PROTO_CLIENT = Object.create(null);

export function setProtocolMetadata(items) {
  if (!Array.isArray(items) || !items.length) throw new Error('协议列表为空');
  const info = Object.create(null);
  for (const item of items) {
    if (!item || typeof item.name !== 'string' || !item.name.trim()
      || typeof item.label !== 'string' || !item.label.trim()
      || typeof item.path !== 'string' || !item.path.trim()
      || typeof item.client !== 'string' || !item.client.trim()) {
      throw new Error('协议元数据不完整');
    }
    const name = item.name.trim().toLowerCase();
    if (info[name]) throw new Error(`协议重复：${name}`);
    const label = item.label.trim();
    info[name] = {
      label,
      short: typeof item.short === 'string' && item.short.trim() ? item.short.trim() : label,
      path: item.path,
      client: item.client,
      supports_1m: Boolean(item.supports_1m),
    };
  }
  const names = Object.keys(info);
  PROTO_INFO = info;
  PROTOCOLS = names;
  PROTO_LABEL = Object.fromEntries(names.map((p) => [p, info[p].label]));
  PROTO_SHORT = Object.fromEntries(names.map((p) => [p, info[p].short]));
  PROTO_PATH = Object.fromEntries(names.map((p) => [p, info[p].path]));
  PROTO_CLIENT = Object.fromEntries(names.map((p) => [p, info[p].client]));
  return names;
}

const ONE_M_RE = /\[(1m|1000k|1024k|1048k)\]\s*$/i;

export const splitOneM = (remote) => ({
  bare: (remote || '').replace(ONE_M_RE, '').trim(),
  onem: ONE_M_RE.test(remote || ''),
});

export const withOneM = (bare, onem) => (onem ? `${bare}[1m]` : bare);
