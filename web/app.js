'use strict';
/* ================================================================
   Model Gateway 控制台 · 入口
   分工：模型路由视图管「哪个模型走哪个上游」，上游视图管「站点怎么连
   + 批量导入模型」，转发记录视图只读，概览视图看全局。所有交互走事件
   委托（元素上写 data-act），不往 HTML 里拼 onclick 字符串。
   ================================================================ */

import {
  $, state, api, toast, confirmBox, run, esc,
  catalogOfGroup, catalogOfUpstream, routeCountOfGroup, routeCountOfUpstream,
  groupLabel, upstreamOfGroup, groupOf, protocolOn,
  supportsIface, groupsOfIface, splitOneM, withOneM,
  PROTO_LABEL, PROTO_SHORT, PROTO_PATH, PROTO_CLIENT, PROTOCOLS, setProtocolMetadata,
} from './util.js';
import { withViewTransition, moveMarker, reduceMotion, initSpotlightAndTilt, refreshLightTargets } from './motion.js';
import * as views from './views.js';
import { createRefreshQueue } from './async-state.js';
import {
  beginGroupEdit, currentGroupEdit, updateGroupEdit,
  isCurrentGroupEdit, closeGroupEdit, remoteModels, remoteBusy, remoteDead,
  pullRemoteModels, invalidateRemoteModels, beginModelWrites, endModelWrites,
} from './group-editor.js';

/* ---------------------------------------------------------------- 数据 */

let protocolLoading = false;

/* 接口开关：只渲染启用的那些。停用的接口不出现在筛选器里，列表里也没有它的东西 */
function renderProtocolSwitches() {
  const host = $('proto-switches');
  if (!host) return;
  host.innerHTML = PROTOCOLS.map((p) => {
    const on = protocolOn(p);
    const name = PROTO_SHORT[p] || p;
    const tip = on
      ? `停用后：${name} 的模型、分组和只有它的站都隐藏，请求被拒绝。配置保留，随时可再打开`
      : `重新启用 ${name}`;
    return `<label class="fo-item${on ? '' : ' is-off'}" title="${esc(tip)}">
      <span>${esc(name)}</span>
      <input type="checkbox" class="switch" data-act="toggle-protocol"
             data-proto="${esc(p)}" ${on ? 'checked' : ''} aria-label="启用 ${esc(name)} 接口">
    </label>`;
  }).join('');
}

function renderProtocolControls() {
  const enabled = PROTOCOLS.filter(protocolOn);
  const make = (attr) => `<button type="button" class="seg-item" ${attr}="">全部</button>`
    + enabled.map((p) => `<button type="button" class="seg-item" ${attr}="${esc(p)}">`
      + `${esc(PROTO_LABEL[p])}</button>`).join('');
  $('seg-iface').innerHTML = make('data-iface');
  $('seg-proto').innerHTML = make('data-proto');
  for (const b of $('seg-iface').children) b.classList.toggle('is-on', b.dataset.iface === state.iface);
  for (const b of $('seg-proto').children) b.classList.toggle('is-on', b.dataset.proto === state.proto);
  $('protocol-status').textContent = '';
}

// Preserve the current new-model default when OpenAI is registered; a test or
// future deployment with another first protocol still gets a valid choice.
const defaultProtocol = () => {
  const enabled = PROTOCOLS.filter(protocolOn);
  return enabled.includes('openai') ? 'openai' : (enabled[0] || PROTOCOLS[0]);
};

function protocolStatus(message, retry = false) {
  const host = $('protocol-status');
  if (!host) return;
  host.textContent = message;
  if (retry) {
    host.insertAdjacentHTML('beforeend',
      ' <button type="button" class="btn btn-ghost btn-sm" data-act="retry-protocols">重试</button>');
  }
}

async function refreshProtocols() {
  if (protocolLoading) return;
  protocolLoading = true;
  try {
    const [data, switches] = await Promise.all([
      api('GET', '/admin/api/protocols'),
      api('GET', '/admin/api/protocol-switches').catch(() => null),
    ]);
    const firstLoad = !state.protocolsReady;
    setProtocolMetadata(data && data.protocols);
    if (switches && switches.enabled) state.protocolEnabled = switches.enabled;
    state.protocolsReady = true;
    if (firstLoad) {
      const saved = localStorage.getItem('mg-iface') || '';
      state.iface = PROTOCOLS.includes(saved) && protocolOn(saved) ? saved : '';
    }
    // 当前筛选可能指向一个刚被停用的接口，拉回「全部」
    if (state.iface && !protocolOn(state.iface)) state.iface = '';
    if (state.proto && !protocolOn(state.proto)) state.proto = '';
    renderProtocolControls();
    renderProtocolSwitches();
    views.renderRoutes();
    views.renderUpstreams();
  } catch (e) {
    protocolStatus(PROTOCOLS.length ? `协议选项刷新失败：${e.message}` : '协议选项加载失败', true);
    throw e;
  } finally {
    protocolLoading = false;
  }
}

let failoverRequestSeq = 0;
let liveRequestSeq = 0;
let failoverAppliedSeq = 0;
let liveAppliedSeq = 0;

const configRefresh = createRefreshQueue(
  async () => {
    const presets = await api('GET', '/admin/api/egress-presets').catch(() => null);
    const [upstreams, routes, failover, searchTarget, rewriteRules] = await Promise.all([
      api('GET', '/admin/api/upstreams'),
      api('GET', '/admin/api/models'),
      api('GET', '/admin/api/failover'),
      api('GET', '/admin/api/standalone-search-target').catch(() => null),
      api('GET', '/admin/api/rewrite-rules').catch(() => null),
    ]);
    return { presets, upstreams, routes, failover, searchTarget, rewriteRules };
  },
  (data, args) => {
    state.egressVps = data.presets ? data.presets.vps : null;
    state.upstreams = data.upstreams;
    state.routes = data.routes;
    state.searchTarget = data.searchTarget;
    if (args.seq >= failoverAppliedSeq) {
      failoverAppliedSeq = args.seq;
      state.failover = data.failover;
    }
    syncEgressPreset();
    views.renderRoutes();
    views.renderUpstreams();
    renderSearchTarget();
    renderRewriteRules(data.rewriteRules);
    syncPickerChecks();
  },
);

function refreshConfig(options) {
  return configRefresh({ seq: ++failoverRequestSeq }, options);
}

const statsRefresh = createRefreshQueue(
  () => api('GET', '/admin/api/stats'),
  (data, args) => {
    if (args.seq < liveAppliedSeq) return;
    liveAppliedSeq = args.seq;
    state.stats = data;
    views.renderLive();
    views.updateKpiLive();
  },
);

function refreshStats(options) {
  return statsRefresh({ seq: ++liveRequestSeq }, options);
}

/* skipSeries：切时间窗时由 animateWindowChange 负责在淡出淡入之间换图，
   这里就别先原地渲染一次，否则新数据会先闪一下再被淡出 */
const overviewRefresh = createRefreshQueue(
  ({ window }) => api('GET', `/admin/api/overview?window=${window}&top=8`),
  (data, args) => {
    if (args.window !== state.window || args.seq < (state.overviewSeq || 0)) return;
    state.overviewSeq = args.seq;
    state.overview = data;
    views.renderKpis();
    if (!args.skipSeries) views.renderSeries();
    views.renderHealth();
    views.renderHot();
    views.renderRoutes();
    views.renderUpstreams();
  },
);

function refreshOverview({ skipSeries = false, allowIntermediate = false } = {}) {
  const seq = (state.overviewSeq || 0) + 1;
  state.overviewSeq = seq;
  return overviewRefresh({ window: state.window, skipSeries, seq }, { allowIntermediate });
}

const logRefresh = createRefreshQueue(
  () => api('GET', '/admin/api/requests?limit=50'),
  (rows) => views.renderLog(rows),
);

function refreshLog(options) {
  return logRefresh({}, options);
}

/* 「实时」那一页：1 秒一刷。接口是纯内存的，不碰数据库。共享的 failover/live 字段
   用发起序号保护，较早返回的配置快照不能覆盖较晚的实时快照。 */
const inflightRefresh = createRefreshQueue(
  () => api('GET', '/admin/api/inflight'),
  (data, args) => {
    if (args.failoverSeq >= failoverAppliedSeq) {
      failoverAppliedSeq = args.failoverSeq;
      state.failover = { enabled: data.failover || {}, breakers: data.breakers || [] };
      state.tokens = data.tokens || {};
    }
    if (args.liveSeq >= liveAppliedSeq) {
      liveAppliedSeq = args.liveSeq;
      state.stats = { ...(state.stats || {}), live: data.counts };
      views.renderLive();
      views.renderInflight(data);
    }
  },
);

function refreshInflight(options) {
  return inflightRefresh(
    { failoverSeq: ++failoverRequestSeq, liveSeq: ++liveRequestSeq },
    options,
  );
}

/* ---------------------------------------------------------------- 视图路由 */

const VIEWS = ['overview', 'live', 'upstreams', 'log'];
let currentView = '';

function paintView(name) {
  for (const sec of document.querySelectorAll('.view')) {
    sec.hidden = sec.dataset.view !== name;
  }
  for (const btn of document.querySelectorAll('.nav-item')) {
    const on = btn.dataset.view === name;
    btn.classList.toggle('is-active', on);
    if (on) btn.setAttribute('aria-current', 'page');
    else btn.removeAttribute('aria-current');
  }
  moveMarker($('nav-marker'), document.querySelector(`.nav-item[data-view="${name}"]`));
  requestAnimationFrame(() => {
    initSpotlightAndTilt();
    refreshLightTargets();   // 换视图后哪些卡片可见、在哪，都变了
  });
}

function showView(name) {
  const next = VIEWS.indexOf(name);
  const cur = VIEWS.indexOf(currentView);
  if (next < 0 || next === cur) return;
  const dir = cur < 0 || next > cur ? 'down' : 'up';
  currentView = name;
  state.view = name;
  withViewTransition(dir, () => paintView(name));
  history.replaceState(null, '', '#' + name);
  if (name === 'log') run(null, refreshLog);
  if (name === 'live') run(null, refreshInflight);
}

/* ---------------------------------------------------------------- 时间窗 */

async function setWindow(w) {
  if (w === state.window) return;
  state.window = w;
  for (const b of $('seg-window').children) {
    b.classList.toggle('is-on', b.dataset.window === w);
  }
  await run(null, async () => {
    await refreshOverview({ skipSeries: true });
    views.animateWindowChange(() => views.renderSeries());
  });
}

/* ---------------------------------------------------------------- 接口与协议筛选 */

/* 两个都是纯前端筛选，不重新拉数据。
   data-iface 在模型路由的分段上，data-proto 在转发记录的分段上，各自独立不会撞。 */
function setIface(v) {
  v = PROTOCOLS.includes(v) ? v : '';
  if (v === state.iface) return;
  state.iface = v;
  localStorage.setItem('mg-iface', v);
  for (const b of $('seg-iface').children) b.classList.toggle('is-on', b.dataset.iface === v);
  views.renderRoutes();
}

function setProto(p) {
  p = PROTOCOLS.includes(p) ? p : '';
  if (p === state.proto) return;
  state.proto = p;
  for (const b of $('seg-proto').children) b.classList.toggle('is-on', b.dataset.proto === p);
  views.applyLogFilter();
}

/* ---------------------------------------------------------------- 主题 */
const THEMES = ['auto', 'light', 'dark'];
const THEME_ICON = { auto: '◐', light: '☀', dark: '☾' };

function applyTheme(name) {
  if (name === 'auto') document.documentElement.removeAttribute('data-theme');
  else document.documentElement.dataset.theme = name;
  $('theme-icon').textContent = THEME_ICON[name];
  localStorage.setItem('mg-theme', name);
}

/* 主题切换做成从按钮扩散出去的圆：整页拍成一张快照压在底下，新配色那张用
   圆形 clip 一点点盖上来。圆心和半径通过 CSS 变量交给 style.css 里的关键帧。

   这里一律用百分比、不用 px。实测在 devicePixelRatio 非 1 的环境（Windows 150%
   缩放，dpr=1.5）下，写进 view-transition 伪元素 clip-path 的 px 长度会被 dpr
   缩掉：圆心落在按钮位置的 1/1.5 处、半径也只有需要值的 1/1.5，于是圆扩到七成
   就到终点，最后一帧剩下的屏幕直接翻色。百分比是相对伪元素参照框解析的，
   缩放会自然抵消，dpr=1 和 1.5 下都正确。 */
let themeSeq = 0;
const VT_VARS = ['--vt-x', '--vt-y', '--vt-r', '--vt-duration'];

function cycleTheme(dataset, el) {
  const now = localStorage.getItem('mg-theme') || 'auto';
  const next = THEMES[(THEMES.indexOf(now) + 1) % THEMES.length];
  if (reduceMotion() || typeof document.startViewTransition !== 'function') {
    applyTheme(next);
    return;
  }

  // 圆心固定取按钮几何中心：比点击坐标更稳，键盘触发（clientX 为 0）时也一样
  const btn = el || document.querySelector('[data-act="toggle-theme"]');
  const box = btn ? btn.getBoundingClientRect() : null;
  const vw = window.innerWidth;
  const vh = window.innerHeight;
  const x = box ? box.left + box.width / 2 : vw / 2;
  const y = box ? box.top + box.height / 2 : vh / 2;

  // 半径要够到四个角里最远的那个
  const reach = Math.hypot(Math.max(x, vw - x), Math.max(y, vh - y));
  // circle() 的百分比半径按 √(w²+h²)/√2 解析，换算过去；多给 1% 兜住取整误差
  const radiusPct = (reach * Math.SQRT2 * 100) / Math.hypot(vw, vh) + 1;

  const root = document.documentElement;
  const seq = ++themeSeq;

  root.style.setProperty('--vt-x', `${((x / vw) * 100).toFixed(3)}%`);
  root.style.setProperty('--vt-y', `${((y / vh) * 100).toFixed(3)}%`);
  root.style.setProperty('--vt-r', `${radiusPct.toFixed(3)}%`);
  root.style.setProperty('--vt-duration', '560ms');
  root.dataset.vtTheme = '1';

  const vt = document.startViewTransition(() => {
    applyTheme(next);
  });

  vt.finished.catch(() => {}).finally(() => {
    if (seq !== themeSeq) return;   // 连点时已有新的切换在跑，别擦掉它的圆心
    delete root.dataset.vtTheme;
    for (const name of VT_VARS) root.style.removeProperty(name);
  });
}

/* ---------------------------------------------------------------- 端点 */

async function copyEndpoint() {
  const text = `${location.origin}/v1`;
  try {
    await navigator.clipboard.writeText(text);
    toast('已复制 ' + text, 'ok');
  } catch {
    toast('复制失败，请手动复制：' + text, 'err');
  }
}

/* ---------------------------------------------------------------- 下游接入信息 */

/* Codex 要填到 /v1 为止，Claude Code 的 ANTHROPIC_BASE_URL 反而不能带 /v1（它自己会拼
   /v1/messages）。两个值不一样，侧栏只放一个的话粘错就是一路 404，所以单独开个弹窗写清楚。 */
const TIERS = [
  { key: 'opus', fallback: 'claude-opus-5', env: 'ANTHROPIC_DEFAULT_OPUS_MODEL' },
  { key: 'sonnet', fallback: 'claude-sonnet-5', env: 'ANTHROPIC_DEFAULT_SONNET_MODEL' },
  { key: 'haiku', fallback: 'claude-haiku-4-5', env: 'ANTHROPIC_DEFAULT_HAIKU_MODEL' },
  { key: 'fable', fallback: 'claude-fable-5-1', env: '' },
];

/** 已录入的 Anthropic 接口模型里属于这个档位的那个；没有就给个默认名 */
function tierModel(tier) {
  const hit = state.routes
    .filter((r) => r.protocol === 'anthropic')
    .map((r) => r.model_name)
    .filter((n) => n.toLowerCase().includes(tier.key));
  return hit[0] || tier.fallback;
}

function copyRow(labelHtml, value) {
  // 标签必须整段包在一个 span 里：.field 是 flex column，裸着放的 <code> 会各自成为
  // 一个 flex item，把「API 地址（填到 /v1 为止）」拆成三行
  return `<div class="access-row"><div class="field"><span>${labelHtml}</span>
      <span class="key-wrap">
        <input type="text" readonly value="${esc(value)}">
        <button type="button" class="btn btn-ghost btn-sm" data-act="copy-text" data-text="${esc(value)}">复制</button>
      </span>
    </div></div>`;
}

function showAccess() {
  const origin = location.origin;
  const env = [
    `setx ANTHROPIC_BASE_URL ${origin}`,
    'setx ANTHROPIC_AUTH_TOKEN local',
    ...TIERS.filter((t) => t.env).map((t) => `setx ${t.env} ${tierModel(t)}`),
  ].join('\n');

  $('access-body').innerHTML = `
    <div class="dlg-section">Codex / OpenAI 兼容客户端</div>
    ${copyRow('API 地址（填到 <code>/v1</code> 为止；Responses 与 Chat Completions 都从这里走）', `${origin}/v1`)}
    <div class="dlg-section divider">Claude Code</div>
    ${copyRow('<b>ANTHROPIC_BASE_URL</b>：不要带 <code>/v1</code>，它自己会拼 <code>/v1/messages</code>', origin)}
    <div class="access-row">
      <div class="field"><span>环境变量（Windows，设完要重开终端；模型名取自你已录入的档位）</span>
        <textarea readonly rows="${env.split('\n').length}">${esc(env)}</textarea>
      </div>
      <div class="field-row" style="margin-top:7px">
        <span class="grow"></span>
        <button type="button" class="btn btn-ghost btn-sm" data-act="copy-text" data-text="${esc(env)}">复制全部</button>
      </div>
    </div>
    <p class="card-hint" style="margin:16px 0 0">
      两边的 API Key 都随便填 —— 本地服务没做鉴权，真正的 key 存在每个分组里。模型名必须和「模型路由」
      里录入的一致，而且要在对应的接口下：Claude Code 打的是 <code>/v1/messages</code>，只能用
      Anthropic 接口下的模型，反之同理。没配过的名字会 404（只有认得出档位关键字的才会被兜到
      同接口、同档位那条配置上）。fable 档没有对应的环境变量，在 Claude Code 里用 <code>/model</code> 选。
    </p>`;
  $('access-dialog').showModal();
}

/* ---------------------------------------------------------------- 两级选择器 */

/* 「先选供应商、再选分组」在两个弹窗里都要用。接口是分组的属性，所以供应商这一级
   只列「有那种接口的分组」的站 —— 没有 Anthropic 分组的站不该出现在 Anthropic 模型的选择里。 */
function upstreamsFor(iface) {
  return state.upstreams.filter((u) => supportsIface(u, iface));
}

function fillUpstreamSelect(selectId, pool) {
  $(selectId).innerHTML = pool.map((u) =>
    `<option value="${u.id}">${esc(u.name)}${u.enabled ? '' : '（停用）'}</option>`).join('');
}

/** 供应商选好之后填它在这个接口下的分组 */
function fillGroupSelect(selectId, upstreamId, iface) {
  const up = state.upstreams.find((u) => u.id === Number(upstreamId));
  const groups = up ? groupsOfIface(up, iface) : [];
  $(selectId).innerHTML = groups.map((g) =>
    `<option value="${g.id}">${esc(g.name)}${g.enabled ? '' : '（停用）'}</option>`).join('')
    || '<option value="">（这个供应商没有这种接口的分组）</option>';
}

/* ---------------------------------------------------------------- 供应商弹窗 */

/* 供应商只管「站在哪、怎么连」：一个站根，key 和接口都在分组里。base_url 不带 /v1 ——
   两种接口的路径都在 /v1 底下，网关按接口自己补，所以这里把它显示出来免得填错。 */
function baseHint() {
  const root = $('up-base').value.trim().replace(/\/+$/, '').replace(/\/v1$/i, '');
  $('up-base-hint').innerHTML = root
    ? `将转发到 <code>${esc(root)}/v1/responses</code> 与 <code>${esc(root)}/v1/messages</code>`
    : '填到域名（或站点路径）为止，末尾的 /v1 会被自动去掉';
}

/* 出口：'' 跟随系统 / 'direct' 直连 / 'vps' 预设门 / 一个代理 URL。界面上拆成
   「多选一 + URL」两个控件，因为前两个是选择、最后一个才要打字。 */
const EGRESS_TIP = {
  '': '默认。httpx 会读环境变量和 Windows 注册表里的系统代理（Clash 那种），所以这个站跟着你的代理走',
  direct: '不走任何代理，从本机自己的出口出去。被机房 IP 拉黑的站要用这个',
  vps: '走你在 VPS 上部署的那扇门（地址在设置表 egress_vps 里，保存的是同一个 URL）',
  proxy: '只有这个站走这个代理。填 http:// 或 socks5://（vless/ss 得先由本机内核落成一个这样的端口）',
};

function egressKind() {
  return $('up-egress-kind').value;
}

function egressValue() {
  const kind = egressKind();
  if (kind === 'vps') return state.egressVps || '';
  return kind === 'proxy' ? $('up-egress-url').value.trim() : kind;
}

function egressHint() {
  const kind = egressKind();
  $('up-egress-url').hidden = kind !== 'proxy';
  $('up-egress-hint').textContent = EGRESS_TIP[kind] || '';
}

function fillEgress(value) {
  const raw = (value || '').trim();
  const kind = raw === '' || raw === 'direct' ? raw
    : state.egressVps && raw === state.egressVps ? 'vps' : 'proxy';
  $('up-egress-kind').value = kind;
  $('up-egress-url').value = kind === 'proxy' ? raw : '';
  egressHint();
}

/* VPS 这扇门是运维事实（VPS 上跑着 gost），不是代码里的常量：部署了（设置表里有
   egress_vps）下拉里才有这个选项。值本身是完整 URL，存到站上和手填没区别。 */
function syncEgressPreset() {
  $('up-egress-vps-opt').hidden = !state.egressVps;
}

/* 「测一下」：同一个站从每扇门各打一次。公益站按 IP 屏蔽，而校园网 IP 和机房 IP
   各自被不同的站拉黑 —— 这个问题只能实测，猜不出来。 */
async function probeUpstream() {
  if (state.editing === null) return toast('先保存这个供应商，再测出口', 'err');
  const host = $('up-egress-hint');
  host.textContent = '正在从每扇门各打一次…';
  try {
    const data = await api('POST', `/admin/api/upstreams/${state.editing}/probe`);
    if (!Array.isArray(data.results)) throw new Error('响应里没有 results');
    host.innerHTML = data.results.map((r) => {
      const dot = r.ok ? (r.status < 400 ? 'good' : 'warn') : 'crit';
      const what = r.ok ? `${r.status}` : esc(r.error.slice(0, 60));
      return `<span class="dot dot-${dot}"></span>${esc(r.label)} ${what} <span class="dim">${r.ms}ms</span>`;
    }).join(' &nbsp; ') + '<br><span class="dim">拿到状态码就算这扇门能到站（401 也算 —— 问的是网络，不是 key）</span>';
  } catch (e) {
    host.textContent = `测出口失败：${e.message}`;
  }
}

function openUpstream(id) {
  state.editing = id;
  const u = id === null ? null : state.upstreams.find((x) => x.id === id);
  $('up-title').textContent = u ? `编辑供应商：${u.name}` : '添加供应商';
  $('up-name').value = u ? u.name : '';
  $('up-base').value = u ? u.base_url : '';
  $('up-override').value = u ? (u.header_override || '') : '';
  $('up-retry').value = u ? (u.retry_rules || '') : '';
  $('up-enabled').checked = u ? u.enabled : true;
  fillEgress(u ? u.egress : '');
  baseHint();
  markOverride();
  markRetry();

  const hint = $('up-groups-hint');
  hint.hidden = Boolean(u);
  if (!u) hint.innerHTML = '保存后就能在这里加第一个分组：选接口（Anthropic Messages / OpenAI Responses / Chat Completions）＋ 填那把 key。';
  $('up-dialog').showModal();
  views.renderUpGroups();      // 已有的供应商在这儿直接管分组，不用回列表里展开
  $('up-name').focus();
}

/* 指纹覆写是低频功能，收在 <details> 里。已经设了的话把它展开、并在标题上标几个头，
   否则「这个站到底改过没有」得点开才知道。 */
function markOverride() {
  const raw = $('up-override').value.trim();
  const tag = $('up-ov-tag');
  let count = 0;
  if (raw) {
    try { count = Object.keys(JSON.parse(raw)).length; } catch { count = -1; }
  }
  tag.hidden = !raw;
  tag.textContent = count < 0 ? '格式有问题' : `${count} 个头`;
  tag.className = count < 0 ? 'tag tag-warn' : 'tag tag-accent';
  $('up-adv').open = Boolean(raw);
}

function markRetry() {
  const raw = $('up-retry').value.trim();
  const tag = $('up-retry-tag');
  let count = 0;
  if (raw) {
    try {
      const parsed = JSON.parse(raw);
      if (!Array.isArray(parsed)) throw new Error('not array');
      count = parsed.length;
    } catch { count = -1; }
  }
  tag.hidden = !raw;
  tag.textContent = count < 0 ? '格式有问题' : `${count} 条`;
  tag.className = count < 0 ? 'tag tag-warn' : 'tag tag-accent';
  $('up-retry-adv').open = Boolean(raw);
}

const RETRY_PRESETS = {
  transient400: '[{"status":400,"times":2,"delay_ms":0}]',
  capacity: '[{"status":503,"times":2,"delay_ms":300}]',
};

function upstreamPayload(source = null, enabled = null) {
  return {
    name: source ? source.name : $('up-name').value.trim(),
    base_url: source ? source.base_url : $('up-base').value.trim(),
    enabled: enabled ?? (source ? source.enabled : $('up-enabled').checked),
    header_override: source ? (source.header_override || '') : $('up-override').value.trim(),
    egress: source ? (source.egress || '') : egressValue(),
    retry_rules: source ? (source.retry_rules || '') : $('up-retry').value.trim(),
  };
}

async function saveUpstream() {
  const payload = upstreamPayload();
  if (!payload.name || !payload.base_url) return toast('名称和 Base URL 都要填', 'err');
  if (egressKind() === 'proxy' && !payload.egress) {
    return toast('「走指定代理」得填代理地址；想跟随系统代理就选那一项', 'err');
  }
  if (payload.header_override) {
    try {
      const parsed = JSON.parse(payload.header_override);
      if (typeof parsed !== 'object' || parsed === null || Array.isArray(parsed)) throw new Error('必须是一个对象');
    } catch (e) {
      return toast('请求头覆写不是合法 JSON：' + e.message, 'err');
    }
  }
  if (payload.retry_rules) {
    try {
      const parsed = JSON.parse(payload.retry_rules);
      if (!Array.isArray(parsed)) throw new Error('必须是一个数组');
    } catch (e) {
      return toast('同站重试不是合法 JSON：' + e.message, 'err');
    }
  }
  if (state.editing === null) {
    const created = await api('POST', '/admin/api/upstreams', payload);
    // 供应商弹窗不关：分组就在它下半部分管。新建的供应商还没有分组、用不了，
    // 所以直接把分组弹窗叠上去；关掉那层就回到这儿，新分组已经列在下面了。
    // 请求在途时弹窗被关掉的话，就别为一个放弃的流程再叠分组弹窗。
    const stillEditing = $('up-dialog').open && state.editing === null;
    await refreshConfig();
    if (!stillEditing) {
      toast('已保存', 'ok');
      return;
    }
    state.editing = created.id;
    $('up-title').textContent = `编辑供应商：${created.name}`;
    $('up-groups-hint').hidden = true;
    toast('已保存，接着建第一个分组', 'ok');
    openGroup(created.id, null);
    return;
  }
  const editingAt = state.editing;
  await api('PUT', `/admin/api/upstreams/${editingAt}`, payload);
  await refreshConfig();
  if (state.editing !== editingAt) return;  // 迟到响应：别去动另一个供应商的表单
  toast('已保存', 'ok');
  markOverride();
}

/* ---------------------------------------------------------------- 分组弹窗 */

/* 分组管「走哪种接口、用哪把 key、能看到哪些模型」。模型列表必须跟着 key 走 —— 同一个站的
   两把 key 能拉到的东西常常不一样，这也是分组存在的理由。 */
function fillIfaceSelect(selectId, value) {
  $(selectId).innerHTML = PROTOCOLS.map((p) =>
    `<option value="${p}">${esc(PROTO_LABEL[p])}　${esc(PROTO_PATH[p])} · ${esc(PROTO_CLIENT[p])}</option>`).join('');
  $(selectId).value = value;
}

function openGroup(upstreamId, gid) {
  if (!PROTOCOLS.length) return toast('协议选项还没加载完成，请稍后重试', 'err');
  const up = state.upstreams.find((u) => u.id === Number(upstreamId));
  if (!up) return toast('供应商不存在了，刷新一下', 'err');
  const g = gid === null ? null : (up.groups || []).find((x) => x.id === gid);
  const token = beginGroupEdit(up.id, g ? g.id : null);
  $('group-dialog').dataset.groupSession = String(token.seq);

  $('grp-title').textContent = g ? `编辑分组：${up.name} · ${g.name}` : `给「${up.name}」加分组`;
  $('grp-name').value = g ? g.name : '默认';
  $('grp-key').value = g ? g.api_key : '';
  $('grp-key').type = 'password';
  $('grp-enabled').checked = g ? g.enabled : true;

  // 新分组的接口：这个站只有一种接口时默认补上缺的那种，否则跟当前分段
  const have = up.supports || [];
  const missing = PROTOCOLS.find((p) => !have.includes(p));
  fillIfaceSelect('grp-proto', g ? g.protocol
    : (have.length === 1 && missing ? missing : (state.iface || defaultProtocol())));

  // 有下游候选就不给改接口了：候选是「这个模型在哪个接口下暴露」的唯一记录（后端也会拒）
  const taken = g ? routeCountOfGroup(g.id) : 0;
  $('grp-proto').disabled = taken > 0;
  $('grp-proto-hint').hidden = taken === 0;
  if (taken) {
    $('grp-proto-hint').innerHTML = `这个分组下有 <b>${taken}</b> 条下游候选，接口锁住了 ——`
      + ' 要换接口就给另一种接口新建一个分组，别把已暴露的模型悄悄换成另一种线格式。';
  }

  fillUpstreamSelect('grp-upstream', state.upstreams);
  $('grp-upstream').value = String(up.id);
  $('grp-move-wrap').hidden = !g;      // 新建时没得搬

  pulled = [];
  $('grp-pull-status').textContent = '';
  $('grp-picker-filter').value = '';
  $('grp-manual').value = '';
  $('grp-import').hidden = !g;
  renderPicker();
  $('group-dialog').showModal();
  if (g) {
    // 编辑已有分组：核心操作是勾选上游模型，而字段区较高、列表在下方，
    // 打开就把列表带进视野，别让用户以为「拉到的模型显示不出来」。
    requestAnimationFrame(() => {
      const body = document.querySelector('#group-dialog .dlg-body');
      const pick = $('grp-picker');
      if (body && pick) {
        body.scrollTop = Math.max(0,
          pick.offsetTop - body.clientHeight + pick.offsetHeight + 24);
      }
    });
  } else {
    $('grp-name').focus();
  }
}

async function saveGroup() {
  const token = currentGroupEdit();
  if (!token || !isCurrentGroupEdit(token)) return null;
  const original = token.groupId === null ? null : groupOf(token.groupId);
  const targetUpstream = token.upstreamId;
  const targetGroup = token.groupId;
  const payload = {
    name: $('grp-name').value.trim(),
    protocol: $('grp-proto').value,
    api_key: $('grp-key').value.trim(),
    enabled: $('grp-enabled').checked,
  };
  if (!payload.name) return toast('分组名不能为空', 'err');
  let movedTo = targetUpstream;
  let created = null;
  if (targetGroup === null) {
    created = await api('POST', `/admin/api/upstreams/${targetUpstream}/groups`, payload);
  } else {
    const moveTo = Number($('grp-upstream').value);
    if (moveTo && moveTo !== targetUpstream) {
      payload.upstream_id = moveTo;
      movedTo = moveTo;
    }
    await api('PUT', `/admin/api/groups/${targetGroup}`, payload);
  }
  await refreshConfig();
  if (!isCurrentGroupEdit(token)) return null;
  if (created) {
    updateGroupEdit(token, targetUpstream, created.id);
    const owner = state.upstreams.find((x) => x.id === targetUpstream);
    $('grp-title').textContent = `编辑分组：${owner ? owner.name : ''} · ${created.name}`;
    $('grp-import').hidden = false;
    $('grp-move-wrap').hidden = false;
    toast('已保存，接着挑模型', 'ok');
  } else {
    updateGroupEdit(token, movedTo, targetGroup);
    toast(payload.upstream_id ? '已保存并搬到新供应商下' : '已保存', 'ok');
  }
  if (original && (original.api_key !== payload.api_key
    || original.protocol !== payload.protocol || original.upstream_id !== movedTo)) {
    invalidateRemoteModels(targetGroup);
  }
  renderPicker();
  return currentGroupEdit()?.groupId ?? null;
}

/* ---------------------------------------------------------------- 分组里的上游模型

   这一段管的是**上游模型目录**（这个分组能调到的上游真名），不是下游暴露。
   勾选框的状态就是目录里的登记，勾上/取消当场发请求，没有「导入所选」也没有保存按钮。
   要不要对外暴露在「模型路由」里单独做 —— 这里只回答「这个站有什么」。 */

let pulled = [];   // 当前分组弹窗最近一次拉到的模型名；换分组就清空

const pickRow = (name, on) => `<label class="${on ? 'on' : ''}">
    <input type="checkbox" ${on ? 'checked' : ''} data-act="pick-toggle" data-name="${esc(name)}">
    <span>${esc(name)}</span>
  </label>`;

function renderPicker() {
  const gid = state.editingGroup;
  const host = $('grp-picker');
  if (gid === null) { host.innerHTML = ''; return; }

  const mine = catalogOfGroup(gid);
  const owned = new Set(mine);
  const rest = pulled.filter((m) => !owned.has(m));

  const head = `<div class="pick-sep">这个分组的上游模型 ${mine.length} 个</div>`;
  const body = mine.length
    ? mine.map((m) => pickRow(m, true)).join('')
    : '<div class="dim" style="padding:4px 6px;font-size:12px">还没有。点「拉取模型列表」，或者手动填一个。</div>';
  const tail = rest.length
    ? `<div class="pick-sep">上游还有 ${rest.length} 个没登记</div>${rest.map((m) => pickRow(m, false)).join('')}`
    : '';

  host.innerHTML = head + body + tail;
  $('grp-mcount').textContent = `${mine.length} 个`;
  applyPickerFilter();
}

/** 请求失败或别处改了配置之后，把勾选状态拉回库里的真相，顺序不动（重排会让人点空） */
function syncPickerChecks() {
  const gid = state.editingGroup;
  if (gid === null || !$('group-dialog').open) return;
  const owned = new Set(catalogOfGroup(gid));
  for (const box of $('grp-picker').querySelectorAll('input[data-act="pick-toggle"]')) {
    box.checked = owned.has(box.dataset.name);
    box.closest('label').classList.toggle('on', box.checked);
  }
  $('grp-mcount').textContent = `${owned.size} 个`;
}

function applyPickerFilter() {
  const kw = $('grp-picker-filter').value.trim().toLowerCase();
  const host = $('grp-picker');
  for (const label of host.querySelectorAll('label')) {
    label.hidden = Boolean(kw) && !label.textContent.toLowerCase().includes(kw);
  }
  if (!kw) {
    for (const sep of host.querySelectorAll('.pick-sep')) sep.hidden = false;
    return;
  }
  // 过滤到一个不剩的那节连小标题一起收起，别留个「上游还有 42 个没登记」的空壳
  let sep = null;
  let seen = 0;
  const settle = () => { if (sep) sep.hidden = seen === 0; };
  for (const node of host.children) {
    if (node.classList.contains('pick-sep')) { settle(); sep = node; seen = 0; continue; }
    if (node.tagName === 'LABEL' && !node.hidden) seen += 1;
  }
  settle();
}

const visibleRows = (checked) =>
  [...$('grp-picker').querySelectorAll('label:not([hidden]) input[data-act="pick-toggle"]')]
    .filter((b) => b.checked === checked).map((b) => b.dataset.name);

/** 登记上游模型：只写目录，不产生下游候选。 */
async function addModels(names, token = currentGroupEdit()) {
  if (!names.length) return;
  if (!token || !isCurrentGroupEdit(token) || token.groupId === null) return false;
  const r = await api('POST', `/admin/api/groups/${token.groupId}/models`, { model_names: names });
  if (!isCurrentGroupEdit(token)) return false;
  toast(names.length === 1 ? `已登记上游模型 ${names[0]}` : `登记了 ${r.added} 个上游模型`, 'ok');
  return true;
}

/** 取消勾选 = 从这个分组的目录里去掉这个上游模型。指向它的下游映射已经无处可去，
    会一起下线 —— 先问一句说清楚。 */
async function removeModel(name) {
  const token = currentGroupEdit();
  if (!token || !isCurrentGroupEdit(token) || token.groupId === null) return false;
  const gid = token.groupId;
  const refs = [];
  for (const row of state.routes) {
    if (row.candidates.some((c) => c.group_id === gid && c.remote_model === name)) {
      refs.push(row.model_name);
    }
  }
  if (refs.length) {
    const many = refs.length > 5 ? `${refs.slice(0, 5).map(esc).join('、')} 等` : refs.map(esc).join('、');
    const okay = await confirmBox({
      title: '连同下游映射一起下线',
      body: `上游模型 <b>${esc(name)}</b> 还有 <b>${refs.length}</b> 条下游映射（${many}），`
        + '去掉它会把这些候选一起下线。<br><br>只是不想暴露、还想留在这个站的目录里的话，'
        + '去「模型路由」里删掉对应候选即可。',
      ok: '一起下线',
    });
    if (!okay) return false;
  }
  if (!isCurrentGroupEdit(token)) return false;
  await api(
    'DELETE',
    `/admin/api/groups/${gid}/models?remote_model=${encodeURIComponent(name)}`,
  );
  return isCurrentGroupEdit(token);
}

/* ---------------------------------------------------------------- 路由弹窗 */

/* 一个弹窗三种用法：新增模型 / 给已有模型加候选 / 改某个候选（上游真名 + 1M）。
   「接口」在这里只是个过滤器 —— 真正决定候选走哪种接口的是它所在分组的接口。
   同名模型可以在多种接口下各挂一条链，所以定位行要 (model, proto) 两个字段。
   候选用它自己的 route_id 指：同一个分组下可以挂同一个模型的好几条真名。 */
function openRoute(model, rid, proto) {
  if (!PROTOCOLS.length) return toast('协议选项还没加载完成，请稍后重试', 'err');
  const row = model
    ? state.routes.find((r) => r.model_name === model && (!proto || r.protocol === proto))
    : null;
  const cand = row && rid ? row.candidates.find((c) => c.route_id === rid) : null;
  // 改候选时接口跟那条候选走（不能改）；新增/加候选时默认该链的接口，也允许换一种
  const iface = cand
    ? row.protocol
    : (proto || (row ? row.protocol : null) || state.iface || defaultProtocol());
  const pool = upstreamsFor(iface);
  if (!pool.length) {
    return toast(`没有 ${PROTO_LABEL[iface]} 接口的分组，先去「上游站点」给某个站加一个`, 'err');
  }
  state.editingCand = cand ? { model, rid, proto: row.protocol } : null;
  routeRemoteSeq += 1;

  $('route-title').textContent = cand ? `改候选：${model}` : (model ? `给「${model}」加候选` : '新增模型');
  $('rt-save').textContent = cand ? '保存' : '添加';
  $('rt-model').value = model || '';
  $('rt-model').readOnly = Boolean(model);
  fillIfaceSelect('rt-iface', iface);
  // 只有「改某个候选」才锁接口；给已有模型加候选允许选另一种接口（同名多协议）
  $('rt-iface-wrap').hidden = Boolean(cand);
  fillUpstreamSelect('rt-upstream', pool);
  $('rt-upstream').disabled = Boolean(cand);
  $('rt-group').disabled = Boolean(cand);     // 改候选就是改这一条，别顺手换成另一个分组

  if (cand) {
    $('rt-upstream').value = String(cand.upstream_id);
    fillGroupSelect('rt-group', cand.upstream_id, iface);
    $('rt-group').value = String(cand.group_id);
  } else {
    fillGroupSelect('rt-group', $('rt-upstream').value, iface);
  }

  const { bare, onem } = splitOneM(cand ? cand.remote_model : '');
  $('rt-remote').value = bare && bare !== model ? bare : '';
  $('rt-onem').checked = onem;
  $('rt-onem-wrap').hidden = iface !== 'anthropic';   // beta 头只有 Anthropic 那边有
  fillRemoteList(Number($('rt-group').value));
  // 顺序只在「改某个候选」时能调：新增的那条还不在链上
  $('rt-order-wrap').hidden = !cand;
  orderHint();
  $('route-dialog').showModal();
  (model ? $('rt-remote') : $('rt-model')).focus();
}

/** 「第 2 / 5 位」以及两个按钮该不该禁用 */
function orderHint() {
  const cand = state.editingCand;
  if (!cand) return;
  const row = state.routes.find(
    (r) => r.model_name === cand.model && (!cand.proto || r.protocol === cand.proto),
  );
  const ids = row ? row.candidates.map((c) => c.route_id) : [];
  const at = ids.indexOf(cand.rid);
  $('rt-order-hint').textContent = at < 0 ? '' : `第 ${at + 1} / ${ids.length} 位`;
  for (const btn of $('rt-order-wrap').querySelectorAll('[data-act="move-cand"]')) {
    const to = at + Number(btn.dataset.dir);
    btn.disabled = at < 0 || to < 0 || to >= ids.length;
  }
}

/* 「上游那边的真实模型名」得跟分组对上 —— 同一个站两把 key 能看到的东西都不一样，
   靠记是记不住的。分组一选定就把那个分组能拉到的模型灌进 datalist，点输入框直接选；
   拉不动的站退回「这个分组已经用过的那些名字」，照样能填。 */
let routeRemoteSeq = 0;

function fillRemoteList(gid) {
  const hint = $('rt-remote-hint');
  if (!gid) { $('rt-remote-list').innerHTML = ''; hint.textContent = ''; return; }
  const pulledNames = remoteModels(gid);
  const names = [...new Set([...(pulledNames || []), ...catalogOfGroup(gid)])];
  $('rt-remote-list').innerHTML = names.map((n) => `<option value="${esc(n)}"></option>`).join('');

  // 加候选时，同一个分组下已经挂着的那几条真名不能再重复（后端会 409），先说清楚
  const dup = state.editingCand ? [] : takenRemotes($('rt-model').value.trim(), gid, $('rt-iface').value);
  if (dup.length) {
    hint.innerHTML = `这个分组下已经有 <b>${dup.length}</b> 条这个模型的候选`
      + `（${dup.map(esc).join('、')}），再加一条得换个真名`;
    return;
  }
  if (pulledNames) hint.textContent = `这个分组能拉到 ${pulledNames.length} 个模型，点输入框下拉选`;
  else if (remoteBusy.has(gid)) hint.textContent = '正在拉这个分组的模型列表…';
  else if (remoteDead.has(gid)) hint.textContent = names.length
    ? `拉不动这个分组，下拉里是它已经用过的 ${names.length} 个名字`
    : '拉不动这个分组的模型列表，手动填';
  else hint.textContent = '';

  if (!pulledNames && !remoteBusy.has(gid) && !remoteDead.has(gid)) pullRemoteList(gid);
}

/** 这个模型在这个分组下已经占用的上游真名（同名多协议时必须按接口定位链） */
function takenRemotes(model, gid, proto = '') {
  const row = model
    ? state.routes.find((r) => r.model_name === model && (!proto || r.protocol === proto))
    : null;
  if (!row) return [];
  return row.candidates.filter((c) => c.group_id === gid).map((c) => c.remote_model);
}

async function pullRemoteList(gid) {
  const seq = routeRemoteSeq;
  $('rt-remote-hint').textContent = '正在拉这个分组的模型列表…';
  return pullRemoteModels(
    gid,
    `route:${seq}:${gid}`,
    () => routeRemoteSeq === seq && Number($('rt-group').value) === gid,
    {
      onSuccess: () => { if ($('route-dialog').open) fillRemoteList(gid); },
      onFinally: () => { if ($('route-dialog').open) fillRemoteList(gid); },
    },
  );
}

async function saveRoute() {
  const seq = routeRemoteSeq;
  const cand = state.editingCand ? { ...state.editingCand } : null;
  const model = $('rt-model').value.trim();
  if (!model) return toast('模型名不能为空', 'err');
  // 勾了 1M 就把后缀写进真实模型名：网关转发时摘掉它、换成 anthropic-beta 头
  const remote = withOneM(
    $('rt-remote').value.trim() || model,
    $('rt-onem').checked && !$('rt-onem-wrap').hidden,
  );
  if (cand) {
    await api('PUT', '/admin/api/models', { route_id: cand.rid, remote_model: remote });
  } else {
    const gid = Number($('rt-group').value);
    if (!gid) return toast('这个供应商没有对应接口的分组', 'err');
    await api('POST', '/admin/api/models', {
      model_name: model, group_id: gid, remote_model: remote,
    });
  }
  await refreshConfig();
  // The request belongs to the captured candidate.  If the dialog was closed
  // and reopened while it was in flight, its result must not close or toast
  // the newer editor session.
  if (routeRemoteSeq !== seq) return;
  $('route-dialog').close();
  toast(cand ? '已保存' : '已添加', 'ok');
}

/** 候选的显示名：「供应商 · 分组」，真名和模型名不一样时补上真名 ——
    同一个分组挂了好几条时，全靠真名区分是哪一条 */
function candLabel(model, cand) {
  const base = groupLabel(cand.group_id);
  const { bare } = splitOneM(cand.remote_model);
  return bare && bare !== model ? `${base} · ${bare}` : base;
}

/* ---------------------------------------------------------------- 搜索上游 */

/* Codex 的 Alpha Search 是独立端点：默认按模型的候选链走，但有些站搜索能用而普通模型不通，
   所以允许单独指定一个 OpenAI 分组（+ 可选模型名）。存的是「分组 + 模型」两个设置。 */
function searchUpstreamPool() {
  return state.upstreams.filter((u) => (u.groups || []).some((g) => g.protocol === 'openai'));
}

function renderSearchTarget() {
  const tag = $('search-target-tag');
  if (!tag) return;
  const gid = state.searchTarget && state.searchTarget.group_id;
  const group = gid ? groupOf(Number(gid)) : null;
  if (!group) { tag.hidden = true; tag.textContent = ''; return; }
  const up = upstreamOfGroup(group.id);
  const model = state.searchTarget.model ? ` · ${state.searchTarget.model}` : '';
  tag.hidden = false;
  tag.textContent = `${up ? up.name : '?'} · ${group.name}${model}`;
  tag.title = '当前搜索专用上游；点「搜索上游」可改';
}

function fillSearchGroups(upstreamId, selectedGid) {
  const up = state.upstreams.find((u) => u.id === Number(upstreamId));
  const groups = up ? groupsOfIface(up, 'openai') : [];
  $('sr-group').innerHTML = groups.map((g) =>
    `<option value="${g.id}">${esc(g.name)}${g.enabled ? '' : '（停用）'}</option>`).join('')
    || '<option value="">（这个供应商没有 OpenAI 分组）</option>';
  if (selectedGid) $('sr-group').value = String(selectedGid);
}

function syncSearchFields() {
  const on = Boolean($('sr-upstream').value);
  $('sr-group').disabled = !on;
  $('sr-model').disabled = !on;
  $('sr-hint').textContent = on
    ? '搜索请求会优先打到这个分组，再按它自己的候选链降级。'
    : '当前：跟着被搜索模型自己的候选链走（默认）。';
}

let searchDialogSeq = 0;

function openSearch() {
  if (!PROTOCOLS.length) return toast('协议选项还没加载完成，请稍后重试', 'err');
  searchDialogSeq += 1;
  const target = state.searchTarget || {};
  const gid = target.group_id ? Number(target.group_id) : null;
  const up = gid ? upstreamOfGroup(gid) : null;
  const pool = searchUpstreamPool();
  $('sr-upstream').innerHTML = '<option value="">跟随模型的候选链（默认）</option>'
    + pool.map((u) =>
      `<option value="${u.id}">${esc(u.name)}${u.enabled ? '' : '（停用）'}</option>`).join('');
  $('sr-upstream').value = up ? String(up.id) : '';
  fillSearchGroups(up ? up.id : '', gid);
  $('sr-model').value = target.model || '';
  syncSearchFields();
  $('search-dialog').showModal();
}

async function saveSearch() {
  const seq = searchDialogSeq;
  const uid = $('sr-upstream').value;
  let message;
  if (!uid) {
    await api('PUT', '/admin/api/standalone-search-target', { group_id: null, model: null });
    message = '搜索上游已恢复为跟随候选链';
  } else {
    const gid = Number($('sr-group').value);
    if (!gid) return toast('这个供应商没有可用的 OpenAI 分组', 'err');
    const model = $('sr-model').value.trim();
    await api('PUT', '/admin/api/standalone-search-target', {
      group_id: gid, model: model || null,
    });
    message = '搜索上游已保存';
  }
  await refreshConfig();
  // 请求在途时弹窗被关掉又重开：旧响应不能去关新会话的弹窗
  if (seq !== searchDialogSeq) return;
  $('search-dialog').close();
  toast(message, 'ok');
}

/* ------------------------------------------------- 上游敏感词绕行（请求改写） */

/* 规则直接用 JSON 原文编辑：跟「请求头覆写」一个路子 —— 这类低频、形状自由的配置，
   与其做一堆表单积木，不如让人直接改 JSON，写错了让后端挡回来。 */
let rewriteDirty = false;

function renderRewriteRules(data) {
  const box = $('rewrite-rules');
  if (!box) return;
  if (data == null) return;  // GET 失败：别拿空表覆盖正在编辑或上次成功的内容
  const rules = data.rules || [];
  state.rewriteRules = rules;
  // 有未保存的编辑（失焦也算）时别回填，15 秒轮询/弹窗关闭都走这里
  if (!rewriteDirty && document.activeElement !== box) {
    box.value = rules.length ? JSON.stringify(rules, null, 2) : '';
  }
  const count = $('rewrite-count');
  if (count) {
    count.textContent = rewriteDirty ? '未保存' : (rules.length ? `${rules.length} 条` : '未启用');
  }
}

async function saveRewrite() {
  const box = $('rewrite-rules');
  const text = (box.value || '').trim();
  let rules = [];
  if (text) {
    try {
      rules = JSON.parse(text);
    } catch {
      return toast('不是合法 JSON，改好再保存', 'err');
    }
    if (!Array.isArray(rules)) return toast('要是一个数组：[{"from":"x","to":"y"}]', 'err');
  }
  const saved = await api('PUT', '/admin/api/rewrite-rules', { rules });
  rewriteDirty = false;
  renderRewriteRules(saved);
  toast(rules.length ? `已保存 ${rules.length} 条规则` : '已清空规则', 'ok');
}

/* ---------------------------------------------------------------- 动作表 */

const ACTIONS = {
  'copy-endpoint': copyEndpoint,

  'save-rewrite': saveRewrite,

  'copy-text': async ({ text }) => {
    try {
      await navigator.clipboard.writeText(text);
      toast('已复制', 'ok');
    } catch {
      toast('复制失败，手动选中复制吧', 'err');
    }
  },

  'show-access': showAccess,

  'retry-protocols': async () => {
    await refreshProtocols();
    await Promise.all([refreshConfig(), refreshOverview(), refreshStats()]);
    await refreshLog();
    toast('协议选项已恢复', 'ok');
  },

  'go-live': () => showView('live'),

  'cancel-call': async ({ id }) => {
    const result = await api('POST', `/admin/api/inflight/${Number(id)}/cancel`);
    await refreshInflight();
    toast(result.cancelled ? '已请求中断' : '请求已结束', 'ok');
  },

  'toggle-theme': cycleTheme,

  'close-dialog': (_d, el) => el.closest('dialog').close(),

  'new-upstream': () => openUpstream(null),
  'edit-upstream': ({ uid }) => openUpstream(Number(uid)),
  'probe-upstream': () => probeUpstream(),

  // 整行都是展开开关，所以要放过「我只是想选中那段 base_url」
  'toggle-groups': ({ uid }) => {
    const sel = window.getSelection();
    if (sel && !sel.isCollapsed && sel.toString().trim()) return;
    const id = Number(uid);
    if (state.openUpstreams.has(id)) state.openUpstreams.delete(id);
    else state.openUpstreams.add(id);
    views.renderUpstreams();
  },

  'toggle-upstream': async ({ uid }, el) => {
    const u = state.upstreams.find((x) => x.id === Number(uid));
    try {
      await api('PUT', `/admin/api/upstreams/${u.id}`, upstreamPayload(u, el.checked));
    } catch (e) {
      el.checked = !el.checked;
      throw e;
    }
    await refreshConfig();
  },

  'del-upstream': async ({ uid }) => {
    const id = Number(uid);
    const u = state.upstreams.find((x) => x.id === id);
    const n = routeCountOfUpstream(id);
    const groups = (u.groups || []).length;
    const okay = await confirmBox({
      title: '删除供应商',
      body: `要删掉供应商 <b>${esc(u.name)}</b>、它下面的 ${groups} 个分组，以及 ${n} 条模型候选。`
        + '<br><br>在别的分组还有候选的模型会自动切到剩下的候选上；只在这里有候选的模型会不再对下游暴露。',
      ok: '删除',
    });
    if (!okay) return;
    await api('DELETE', `/admin/api/upstreams/${id}`);
    await refreshConfig();
    toast('已删除', 'ok');
  },

  'new-group': ({ uid }) => openGroup(Number(uid), null),

  'edit-group': ({ gid }) => {
    const up = upstreamOfGroup(Number(gid));
    if (up) openGroup(up.id, Number(gid));
  },

  'clone-group': async ({ gid, protocol }) => {
    const created = await api(
      'POST',
      `/admin/api/groups/${gid}/clone`,
      protocol ? { protocol } : undefined,
    );
    await refreshConfig();
    toast(`已复制成 ${PROTO_LABEL[created.protocol]} 接口的分组「${created.name}」`, 'ok');
    openGroup(created.upstream_id, created.id);
  },

  'toggle-group': async ({ gid }, el) => {
    const g = groupOf(Number(gid));
    try {
      await api('PUT', `/admin/api/groups/${g.id}`, {
        name: g.name, protocol: g.protocol, api_key: g.api_key, enabled: el.checked,
      });
    } catch (e) {
      el.checked = !el.checked;
      throw e;
    }
    await refreshConfig();
  },

  'del-group': async ({ gid }) => {
    const id = Number(gid);
    const g = groupOf(id);
    const up = upstreamOfGroup(id);
    const n = routeCountOfGroup(id);
    const last = up && (up.groups || []).length === 1;
    const okay = await confirmBox({
      title: '删除分组',
      body: `要删掉 <b>${esc(up ? up.name : '?')}</b> 下的 ${esc(PROTO_LABEL[g ? g.protocol : ''] || '')}`
        + ` 分组 <b>${esc(g ? g.name : id)}</b>，连带它的 ${n} 条模型候选。`
        + '<br><br>这把 key 也会一起没掉。只想临时停用的话，把它的开关关掉就行。'
        + (last ? '<br><br>它是这个供应商唯一的分组，删完这个站就没有可用的 key 了。' : ''),
      ok: '删除',
    });
    if (!okay) return;
    await api('DELETE', `/admin/api/groups/${id}`);
    await refreshConfig();
    toast('已删除', 'ok');
  },

  'peek-key': (ds) => {
    const el = $(ds.target || 'grp-key');
    el.type = el.type === 'password' ? 'text' : 'password';
  },

  'preset-fp': ({ fp }) => {
    $('up-override').value = fp === 'anthropic'
      ? '{\n  "user-agent": "claude-cli/2.0.0 (external, cli)",\n  "x-app": "cli"\n}'
      : '{\n  "user-agent": "codex_cli_rs",\n  "originator": "codex_cli_rs"\n}';
    markOverride();
  },

  'preset-retry': ({ r }) => {
    $('up-retry').value = RETRY_PRESETS[r] || '';
    markRetry();
  },

  /* 拉取用的是**服务端存着的**那把 key。key 改了没保存就点拉取，拉的是旧 key，
     回来一个 401 让人一头雾水 —— 先把改动落库，再拉。 */
  'pull-models': async () => {
    const token = currentGroupEdit();
    if (!token || !isCurrentGroupEdit(token)) return;
    if (groupDirty()) {
      const saved = await saveGroup();
      if (saved === null || !isCurrentGroupEdit(token)) return;
    }
    const gid = token.groupId;
    if (gid === null) return;
    $('grp-pull-status').textContent = '拉取中…';
    await pullRemoteModels(
      gid,
      `group:${token.seq}:${gid}`,
      () => isCurrentGroupEdit(token),
      {
        onSuccess: (models) => {
          pulled = models;
          renderPicker();
          const mine = catalogOfGroup(gid);
          const hit = pulled.filter((m) => mine.includes(m)).length;
          $('grp-pull-status').textContent = `上游列出 ${pulled.length} 个，其中 ${hit} 个已登记`;
          // 字段多的时候列表会被挤到可视区下方，拉完直接把它带进视野，
          // 省得用户以为「拉到了但显示不出来」还得自己找滚动位置
          $('grp-picker').scrollIntoView({ block: 'nearest' });
        },
        onFailure: (error) => {
          $('grp-pull-status').textContent = '';
          toast(error.message, 'err');
        },
        onFinally: () => {
          // 迟到的 finally 也只允许当前编辑会话改状态提示。
          if (isCurrentGroupEdit(token) && !remoteModels(gid)) $('grp-pull-status').textContent = '';
        },
      },
    );
  },

  /* 勾选即生效：勾上=登记上游模型，取消=从目录里去掉。失败就把勾回滚到库里的真相。 */
  'pick-toggle': async ({ name }, el) => {
    const token = currentGroupEdit();
    if (!token || !isCurrentGroupEdit(token)) return;
    if (!beginModelWrites(token, [name])) return toast('这项正在保存，请稍后再试', 'err');
    const label = el.closest('label');
    label.classList.add('busy');
    try {
      if (el.checked) await addModels([name]);
      else await removeModel(name);
    } finally {
      endModelWrites(token, [name]);
      label.classList.remove('busy');
      if (isCurrentGroupEdit(token)) {
        await refreshConfig();      // 末尾的 syncPickerChecks 负责把勾选拉回真相
        renderPicker();             // 勾掉的项要落到「没登记」那节（或消失），不能留在已登记里
      }
    }
  },

  'pick-all': async () => {
    const token = currentGroupEdit();
    if (!token || !isCurrentGroupEdit(token)) return;
    const names = visibleRows(false);
    if (!names.length) return toast('没有可加的了', 'ok');
    if (!beginModelWrites(token, names)) return toast('有模型正在保存，请稍后再试', 'err');
    try {
      await addModels(names, token);
      if (isCurrentGroupEdit(token)) {
        await refreshConfig();
        renderPicker();
      }
    } finally {
      endModelWrites(token, names);
    }
  },

  'pick-none': async () => {
    const token = currentGroupEdit();
    if (!token || !isCurrentGroupEdit(token)) return;
    const gid = token.groupId;
    const names = visibleRows(true);
    if (!names.length) return toast('本来就一个都没勾', 'ok');
    // 指向这些上游模型的下游映射会一起下线，这个后果得先说清楚
    const refs = new Set();
    for (const row of state.routes) {
      for (const c of row.candidates) {
        if (c.group_id === gid && names.includes(c.remote_model)) refs.add(row.model_name);
      }
    }
    const okay = await confirmBox({
      title: '清空这个分组的上游模型',
      body: `要把 <b>${names.length}</b> 个上游模型从这个分组的目录里去掉。`
        + (refs.size
          ? `<br><br>其中 <b>${refs.size}</b> 条下游映射指向它们，会一起下线。`
          : '<br><br>它们还没有被下游暴露，去掉只是不再登记。'),
      ok: '去掉',
    });
    if (!okay) return;
    if (!isCurrentGroupEdit(token)) return;
    if (!beginModelWrites(token, names)) return toast('有模型正在保存，请稍后再试', 'err');
    try {
      for (const name of names) {
        await api(
          'DELETE',
          `/admin/api/groups/${gid}/models?remote_model=${encodeURIComponent(name)}`,
        );
      }
    } finally {
      // 中途失败也要把界面拉回真实状态：前面几条已经删掉了，不能只等重开弹窗
      endModelWrites(token, names);
      if (isCurrentGroupEdit(token)) {
        await refreshConfig();
        renderPicker();
      }
    }
    toast(`去掉了 ${names.length} 个`, 'ok');
  },

  /* 上游不肯列全的时候（不少站的 /v1/models 就是残的）自己填一个 */
  'manual-add': async () => {
    const token = currentGroupEdit();
    if (!token || !isCurrentGroupEdit(token)) return;
    const name = $('grp-manual').value.trim();
    if (!name) return toast('填个模型 id', 'err');
    if (catalogOfGroup(token.groupId).includes(name)) return toast('这个分组已经有它了', 'err');
    if (!beginModelWrites(token, [name])) return toast('这项正在保存，请稍后再试', 'err');
    try {
      await addModels([name], token);
    } finally {
      endModelWrites(token, [name]);
    }
    $('grp-manual').value = '';
    if (isCurrentGroupEdit(token)) {
      await refreshConfig();
      renderPicker();
    }
  },

  'open-search': openSearch,
  'new-route': () => openRoute('', null),
  'add-candidate': ({ model, proto }) => openRoute(model, null, proto),
  'edit-candidate': ({ model, rid, proto }) => openRoute(model, Number(rid), proto),

  /* 接口全局开关：停用后这种接口的模型 / 分组 / 只有它的站都隐藏，转发直接拒绝。
     只影响展示与转发，描述符和配置都留着，打开就回来。 */
  'toggle-protocol': async ({ proto }, el) => {
    try {
      const data = await api('POST', '/admin/api/protocol-switches', {
        protocol: proto, enabled: el.checked,
      });
      state.protocolEnabled = data.enabled || state.protocolEnabled;
    } catch (e) {
      el.checked = !el.checked;
      throw e;
    }
    if (state.iface && !protocolOn(state.iface)) setIface('');
    if (state.proto && !protocolOn(state.proto)) setProto('');
    renderProtocolControls();
    renderProtocolSwitches();
    state.logIds.clear();   // 转发记录整表重画，别留下停用协议的旧行
    await Promise.all([refreshConfig(), refreshOverview(), refreshStats(), refreshLog()]);
    const name = PROTO_SHORT[proto] || PROTO_LABEL[proto] || proto;
    toast(`${name} 接口已${el.checked ? '启用' : '停用'}`, 'ok');
  },

  /* 自动降级开关。按接口分开：Claude 侧的中转站坏得勤、值得自动换；
     GPT 侧几乎都是要花钱的站，花钱图稳定，得手动确认。
     属性名用 data-fo 而不是 data-iface —— 后者被模型路由那个分段选择器占了，
     开关会连带把筛选也切掉（preset-fp 当初就踩过这个坑）。 */
  'toggle-failover': async ({ fo }, el) => {
    try {
      const result = await api('POST', '/admin/api/failover', {
        protocol: fo, enabled: el.checked,
      });
      // A slower config/inflight GET may have started before this write.  Give
      // the write a newer shared-state sequence so that old responses cannot
      // flip the switch back visually.
      const seq = ++failoverRequestSeq;
      failoverAppliedSeq = seq;
      // POST 只回 enabled；breakers 是/ inflight 带来的，不能整体替换掉
      state.failover = { ...(state.failover || {}), ...result };
    } catch (e) {
      el.checked = !el.checked;
      throw e;
    }
    views.renderRoutes();
    toast(`${PROTO_LABEL[fo]} 自动降级已${el.checked ? '开启' : '关闭'}`, 'ok');
  },

  /* 候选在链上前移 / 后移一位。即时生效，不用保存 */
  'move-cand': async ({ dir }) => {
    const cand = state.editingCand;
    if (!cand) return;
    const row = state.routes.find((r) =>
      r.model_name === cand.model && (!cand.proto || r.protocol === cand.proto));
    if (!row) return;
    const ids = row.candidates.map((c) => c.route_id);
    const at = ids.indexOf(cand.rid);
    const to = at + Number(dir);
    if (at < 0 || to < 0 || to >= ids.length) return;
    ids.splice(to, 0, ids.splice(at, 1)[0]);
    await api('POST', '/admin/api/models/order', { model_name: cand.model, order: ids });
    await refreshConfig();
    // rAF：run() 的 finally 会把刚点的那个按钮重新启用，得等它跑完再按新位置算禁用
    requestAnimationFrame(orderHint);
  },

  'switch': async ({ model, rid, proto }) => {
    const id = Number(rid);
    const group = state.routes.find((r) => r.model_name === model && r.protocol === proto);
    const cand = group && group.candidates.find((c) => c.route_id === id);
    if (!cand) return;
    if (!cand.upstream_enabled) return toast('这个供应商是停用状态，先在「上游站点」里启用它', 'err');
    if (!cand.group_enabled) return toast('这个分组是停用状态，展开那一行把它打开', 'err');
    const from = group.active_route_id;
    await api('POST', '/admin/api/models/switch', { route_id: id });
    await refreshConfig();
    views.afterSwitch(model, from, id, proto);
    toast(`${model} → ${candLabel(model, cand)}`, 'ok');
  },

  'del-candidate': async ({ model, rid, proto }) => {
    const id = Number(rid);
    const group = state.routes.find((r) => r.model_name === model && r.protocol === proto);
    const cand = group && group.candidates.find((c) => c.route_id === id);
    const last = group && group.candidates.length === 1;
    const okay = await confirmBox({
      title: last ? '移除最后一个候选' : '移除候选',
      body: last
        ? `<b>${esc(model)}</b> 只剩这一个候选，移除后它就不再对下游暴露了。`
        : `把 <b>${esc(model)}</b> 的候选 <b>${esc(cand ? candLabel(model, cand) : id)}</b> 去掉？`
          + '<br><br>如果它正好是当前生效的，流量会自动落到剩下的候选之一。',
      ok: '移除',
    });
    if (!okay) return;
    await api('DELETE', `/admin/api/models?route_id=${id}`);
    await refreshConfig();
    toast('已移除', 'ok');
  },

  'del-model': async ({ model, proto }) => {
    const group = state.routes.find((r) => r.model_name === model && r.protocol === proto);
    const n = group ? group.candidates.length : 0;
    const other = state.routes.find((r) => r.model_name === model && r.protocol !== proto);
    const okay = await confirmBox({
      title: '删除模型',
      body: `删掉 <b>${esc(model)}</b> 在${esc(PROTO_LABEL[proto] || proto || '该接口')}下的全部 ${n} 条候选，`
        + `它将不再从这个接口的 /v1/models 里暴露。`
        + (other ? '<br><br>它在其它接口下还有候选，不受影响。' : '')
        + '<br><br>上游站点本身不受影响，之后还能重新导入。',
      ok: '删除',
    });
    if (!okay) return;
    await api('DELETE', `/admin/api/models?model_name=${encodeURIComponent(model)}&protocol=${encodeURIComponent(proto || '')}`);
    await refreshConfig();
    toast('已删除', 'ok');
  },

  'reload-log': async () => {
    state.logIds.clear();
    await refreshLog();
  },

  'clear-log': async () => {
    const okay = await confirmBox({
      title: '清空转发记录',
      body: '会删掉全部历史记录，累计次数和缓存命中率一起归零。上游和模型配置不受影响。',
      ok: '清空',
    });
    if (!okay) return;
    await api('DELETE', '/admin/api/requests');
    state.logIds.clear();
    await Promise.all([refreshLog(), refreshStats(), refreshOverview()]);
    toast('已清空', 'ok');
  },
};

/* 分组弹窗上半部分（组名 / 接口 / key / 启用）有没有改过。拉取和删组之前要知道。 */
function groupDirty() {
  const g = state.editingGroup === null ? null : groupOf(state.editingGroup);
  if (!g) return false;
  return $('grp-name').value.trim() !== g.name
    || $('grp-proto').value !== g.protocol
    || $('grp-key').value.trim() !== g.api_key
    || $('grp-enabled').checked !== g.enabled;
}

/* ---------------------------------------------------------------- 事件绑定 */

document.addEventListener('click', (ev) => {
  const nav = ev.target.closest('.nav-item');
  if (nav) { showView(nav.dataset.view); return; }

  const seg = ev.target.closest('[data-window]');
  if (seg) { setWindow(seg.dataset.window); return; }

  // 分段选择器各自锁在自己的 seg 容器里。候选圆片 / 模型行也带 data-proto
  //（多协议同名模型要分清是哪条链），绝不能被这里的 closest 截走 —— 否则
  // 点「切换 / ✎ / ✕ / 删除」只会去切转发记录的协议筛选，动作本身永远不执行
  const ifaceSeg = ev.target.closest('#seg-iface [data-iface]');
  if (ifaceSeg) { setIface(ifaceSeg.dataset.iface); return; }

  const protoSeg = ev.target.closest('#seg-proto [data-proto]');
  if (protoSeg) { setProto(protoSeg.dataset.proto); return; }

  const el = ev.target.closest('[data-act]');
  if (!el || el.tagName === 'INPUT') return;
  const fn = ACTIONS[el.dataset.act];
  if (!fn) return;
  ev.preventDefault();
  run(el, () => fn(el.dataset, el, ev));
});

// 开关类控件走 change：在 click 里 preventDefault 会把勾选状态弹回去
document.addEventListener('change', (ev) => {
  const el = ev.target.closest('[data-act]');
  if (!el || el.tagName !== 'INPUT') return;
  const fn = ACTIONS[el.dataset.act];
  if (fn) run(null, () => fn(el.dataset, el));
});

$('up-form').addEventListener('submit', (ev) => {
  ev.preventDefault();
  run($('up-save'), saveUpstream);
});

$('group-form').addEventListener('submit', (ev) => {
  ev.preventDefault();
  run($('grp-save'), saveGroup);
});

$('route-form').addEventListener('submit', (ev) => {
  ev.preventDefault();
  run($('rt-save'), saveRoute);
});

$('search-form').addEventListener('submit', (ev) => {
  ev.preventDefault();
  run($('sr-save'), saveSearch);
});

// 供应商换了就把分组下拉重填一遍；接口换了连供应商池一起换
$('rt-upstream').addEventListener('change', () => {
  fillGroupSelect('rt-group', $('rt-upstream').value, $('rt-iface').value);
  fillRemoteList(Number($('rt-group').value));
});

// 分组定了才知道「上游真名」能填哪些
$('rt-group').addEventListener('change', () => fillRemoteList(Number($('rt-group').value)));

// 搜索上游：供应商换了就重填它的 OpenAI 分组
$('sr-upstream').addEventListener('change', () => {
  fillSearchGroups($('sr-upstream').value, null);
  syncSearchFields();
});

$('rt-iface').addEventListener('change', () => {
  const iface = $('rt-iface').value;
  const pool = upstreamsFor(iface);
  if (!pool.length) toast(`没有 ${PROTO_LABEL[iface]} 接口的分组，先去「上游站点」加一个`, 'err');
  fillUpstreamSelect('rt-upstream', pool);
  fillGroupSelect('rt-group', $('rt-upstream').value, iface);
  fillRemoteList(Number($('rt-group').value));
  $('rt-onem-wrap').hidden = iface !== 'anthropic';
});

// 站根填/改的时候把补出来的两个地址实时显示出来，免得又把 /v1 带上
$('up-base').addEventListener('input', baseHint);
$('up-override').addEventListener('input', markOverride);
$('up-retry').addEventListener('input', markRetry);
$('rewrite-rules').addEventListener('input', () => {
  rewriteDirty = true;
  const count = $('rewrite-count');
  if (count) count.textContent = '未保存';
});
$('up-egress-kind').addEventListener('change', egressHint);

/* 手填模型名：回车直接加，别提交整个表单（那是保存分组的按钮）。
   过滤框也一样 —— 它是 type=search，回车默认会提交表单。 */
$('grp-manual').addEventListener('keydown', (ev) => {
  if (ev.key !== 'Enter') return;
  ev.preventDefault();
  run(null, ACTIONS['manual-add']);
});

$('grp-picker-filter').addEventListener('keydown', (ev) => {
  if (ev.key === 'Enter') ev.preventDefault();
});

/* 弹窗关掉后刷一次列表（Esc 关闭也走这里，所以挂在 close 上而不是关闭按钮上）。
   编辑目标不再靠清空全局字段来表示失效，而由 group-editor 的会话序号管理。若旧
   close 事件晚于紧接着打开的新弹窗，dialog.open 已经为真，不能把新会话一起作废。 */
$('up-dialog').addEventListener('close', () => run(null, refreshConfig));
$('group-dialog').addEventListener('close', (ev) => {
  if (!ev.target.open) closeGroupEdit(ev.target.dataset.groupSession);
  run(null, refreshConfig);
});

// route-dialog 的 editingCand 同理：也只由 openRoute 负责重设，close 时不动
$('route-dialog').addEventListener('close', () => { routeRemoteSeq += 1; });

$('route-filter').addEventListener('input', (ev) => {
  state.filter = ev.target.value;
  views.renderRoutes();
});

$('grp-picker-filter').addEventListener('input', applyPickerFilter);

window.addEventListener('resize', () => {
  moveMarker($('nav-marker'), document.querySelector(`.nav-item[data-view="${currentView}"]`));
});

window.addEventListener('hashchange', () => {
  const target = location.hash.slice(1);
  if (VIEWS.includes(target) && target !== currentView) showView(target);
});

/* ---------------------------------------------------------------- 轮询 */

const ticking = () => document.visibilityState === 'visible' && !document.querySelector('dialog[open]');

// 快轮只取活跃流：3 秒一次，让"进行中的请求"真的是实时的
setInterval(() => {
  if (ticking()) run(null, () => refreshStats({ allowIntermediate: true }));
}, 3000);

/* 「实时」页只在自己显示时轮询，1 秒一次 —— 那个接口是纯内存的，不碰数据库。
   秒数不靠轮询走字：本地每 200ms 按「这条什么时候开始的」重算一遍，
   否则要么一秒跳一格，要么得把轮询压到 200ms 去。 */
setInterval(() => {
  if (ticking() && state.view === 'live') {
    run(null, () => refreshInflight({ allowIntermediate: true }));
  }
}, 1000);

setInterval(() => {
  if (state.view === 'live' && document.visibilityState === 'visible') views.tickElapsed();
}, 200);

// 慢轮取统计和记录：数据量大一些，15 秒足够。配置也跟着刷 —— 断路器的冷却剩余
// 在候选圆片上是要走字的，而且从别处（另一个标签页、手动切换）改过的配置也该跟上
setInterval(() => {
  if (!ticking()) return;
  run(null, async () => {
    await Promise.all([
      refreshOverview({ allowIntermediate: true }),
      refreshLog({ allowIntermediate: true }),
      refreshConfig({ allowIntermediate: true }),
    ]);
  });
}, 15000);

/* ---------------------------------------------------------------- 启动 */

applyTheme(localStorage.getItem('mg-theme') || 'auto');
$('endpoint').textContent = `${location.origin}/v1`;
views.initLogFollow();   // 「自动跟随新记录」的勾选状态变化时补插攒下的行
for (const b of $('seg-window').children) b.classList.toggle('is-on', b.dataset.window === state.window);

state.iface = '';
renderProtocolControls();
for (const b of $('seg-iface').children) b.classList.toggle('is-on', b.dataset.iface === state.iface);

const initial = VIEWS.includes(location.hash.slice(1)) ? location.hash.slice(1) : 'overview';
currentView = '';
showView(initial);

run(null, async () => {
  await refreshProtocols();
  await Promise.all([refreshConfig(), refreshOverview(), refreshStats()]);
  await refreshLog();
});

// 侧栏高亮条的初始位置要等布局算完，否则会量到 0
requestAnimationFrame(() => {
  moveMarker($('nav-marker'), document.querySelector(`.nav-item[data-view="${currentView}"]`));
  initSpotlightAndTilt();
});
