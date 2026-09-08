/* 共享工具：转义、格式化、HTTP、提示条、确认框。
   这个模块会被 views/charts/app 都 import，所以只放无副作用的纯逻辑
   和 DOM 小工具，不碰业务状态以外的东西。 */

export const $ = (id) => document.getElementById(id);

/* ---------------------------------------------------------------- 共享状态 */

export const state = {
  upstreams: [],     // 每个供应商带着自己的 groups
  routes: [],
  stats: null,      // /admin/api/stats：累计值 + 活跃流
  overview: null,   // /admin/api/overview：时间线 + 健康 + 热度
  // /admin/api/failover：{enabled:{anthropic,openai}, breakers:[{group_id,cooling_ms,...}]}
  // 开关按接口分开：Claude 侧全是坏得勤的中转站，GPT 侧要花钱的站得手动确认
  failover: { enabled: {}, breakers: [] },
  // /admin/api/inflight 的 tokens：每种接口每个方向「多少字节摊一个 token」，
  // 后端从转发记录里量的。0 = 量不出来，「实时」页上那一段就不显示
  tokens: {},
  filter: '',
  iface: '',         // 模型路由按接口筛选：'' | 'anthropic' | 'openai'
  proto: '',         // 转发记录的协议筛选：'' | 'anthropic' | 'openai'
  // 下面这些是「当前打开的弹窗在编辑谁」。分组目标由 group-editor 会话设置并配序号；
  // 弹窗关闭时不清旧 id，迟到 close 不能让下一次新建请求打到 null。
  editing: null,      // 供应商弹窗的目标 id；openUpstream(null) = 新建供应商
  // 分组弹窗单独记自己的供应商：它可以叠在供应商弹窗之上开着，而「搬到别的供应商」
  // 会改这个值 —— 要是和 editing 共用，一搬就把底下那个弹窗的目标也换掉了
  editingUp: null,
  editingGroup: null, // 正在编辑的分组 id，null = 新建
  editingCand: null,  // 正在改的候选 {model, rid}，null = 新增
  openUpstreams: new Set(),  // 「上游站点」里展开了分组的那几行
  view: 'overview',
  // 默认 24 小时：1 小时窗口在空闲时段是空的，一进来看到空图会以为坏了
  window: '24h',
  logIds: new Set(), // 已渲染过的 request_log id，用来做增量 diff
  protocolsReady: false,
};

/* ---------------------------------------------------------------- 格式化 */

const ESC_MAP = { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' };

export const esc = (v) => String(v).replace(/[&<>"']/g, (c) => ESC_MAP[c]);

export const fmtInt = (n) => (n ?? 0).toLocaleString('zh-CN');

export function fmtTokens(n) {
  if (n === null || n === undefined) return '-';
  if (n >= 1e6) return (n / 1e6).toFixed(1) + 'M';
  if (n >= 1e4) return (n / 1e3).toFixed(1) + 'K';
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

  const kill = () => { el.remove(); dropToasts(); };
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
  if (el) el.disabled = true;
  try {
    await fn();
  } catch (e) {
    toast(e.message, 'err');
  } finally {
    if (el) el.disabled = false;
  }
}

/* ---------------------------------------------------------------- 小工具 */

export const clamp = (v, lo, hi) => Math.min(hi, Math.max(lo, v));

/* 候选是挂在**分组**上的：同一个站的两把 key 能看到的模型不一样，所以「录入了哪些模型」
   既有分组维度，也有供应商维度（它下面所有分组的并集）。 */
export const modelsOfGroup = (gid) =>
  state.routes.filter((r) => r.candidates.some((c) => c.group_id === gid)).map((r) => r.model_name);

export const modelsOfUpstream = (uid) =>
  state.routes.filter((r) => r.candidates.some((c) => c.upstream_id === uid)).map((r) => r.model_name);

/** 这个分组已经用过的「上游真名」（去掉 [1m] 后缀）。拉不动的站至少还能从这里下拉选。 */
export const remotesOfGroup = (gid) => {
  const out = new Set();
  for (const r of state.routes) {
    for (const c of r.candidates) {
      if (c.group_id !== gid) continue;
      const bare = splitOneM(c.remote_model).bare;
      if (bare) out.add(bare);
    }
  }
  return [...out];
};

export const upstreamOfGroup = (gid) =>
  state.upstreams.find((u) => (u.groups || []).some((g) => g.id === gid)) || null;

export const groupOf = (gid) => {
  const up = upstreamOfGroup(gid);
  return up ? (up.groups.find((g) => g.id === gid) || null) : null;
};

/** 「供应商 · 分组」。只有一个分组时省掉分组名，不然满屏都是「· 默认」 */
export function groupLabel(gid) {
  const up = upstreamOfGroup(gid);
  if (!up) return `#${gid}`;
  const grp = up.groups.find((g) => g.id === gid);
  return up.groups.length > 1 && grp ? `${up.name} · ${grp.name}` : up.name;
}

/** 这个供应商在某个接口下的分组。没有就说明它不支持这种接口 */
export const groupsOfIface = (upstream, iface) =>
  (upstream.groups || []).filter((g) => !iface || g.protocol === iface);

/** 接口是分组的属性，所以「这个供应商支不支持某接口」= 它有没有那种接口的分组 */
export const supportsIface = (upstream, iface) =>
  !iface || (upstream.supports || []).includes(iface);

/* 接口 = 一条请求走哪种线格式，也就是它打进来的那个路径。列表由后端描述符的只读投影
   在启动时填入；下面几个映射仍是给现有调用点用的派生值，不再在前端另登记协议 ID。 */
export let PROTO_INFO = Object.create(null);
export let PROTOCOLS = [];
export let PROTO_LABEL = Object.create(null);
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
    info[name] = {
      label: item.label,
      path: item.path,
      client: item.client,
      supports_1m: Boolean(item.supports_1m),
    };
  }
  const names = Object.keys(info);
  PROTO_INFO = info;
  PROTOCOLS = names;
  PROTO_LABEL = Object.fromEntries(names.map((p) => [p, info[p].label]));
  PROTO_PATH = Object.fromEntries(names.map((p) => [p, info[p].path]));
  PROTO_CLIENT = Object.fromEntries(names.map((p) => [p, info[p].client]));
  return names;
}

/* 1M 上下文是「上游真名」上的一个后缀（`名字[1m]`）：网关转发时摘掉它、换成
   anthropic-beta 头。这里只管在界面和存储之间来回翻译，规则和 gateway/naming.py 对齐。 */
const ONE_M_RE = /\[(1m|1000k|1024k|1048k)\]\s*$/i;

export const splitOneM = (remote) => ({
  bare: (remote || '').replace(ONE_M_RE, '').trim(),
  onem: ONE_M_RE.test(remote || ''),
});

export const withOneM = (bare, onem) => (onem ? `${bare}[1m]` : bare);
