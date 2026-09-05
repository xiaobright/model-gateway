'use strict';
/* ================================================================
   Model Gateway 控制台 · 入口
   分工：模型路由视图管「哪个模型走哪个上游」，上游视图管「站点怎么连
   + 批量导入模型」，转发记录视图只读，概览视图看全局。所有交互走事件
   委托（元素上写 data-act），不往 HTML 里拼 onclick 字符串。
   ================================================================ */

import {
  $, state, api, toast, confirmBox, run, esc,
  modelsOfGroup, modelsOfUpstream, groupLabel, upstreamOfGroup, groupOf, remotesOfGroup,
  supportsIface, groupsOfIface, splitOneM, withOneM,
  PROTO_LABEL, PROTO_PATH, PROTO_CLIENT, PROTOCOLS,
} from './util.js';
import { withViewTransition, moveMarker, reduceMotion, initSpotlightAndTilt, refreshLightTargets } from './motion.js';
import * as views from './views.js';

/* ---------------------------------------------------------------- 数据 */

async function refreshConfig() {
  [state.upstreams, state.routes, state.failover] = await Promise.all([
    api('GET', '/admin/api/upstreams'),
    api('GET', '/admin/api/models'),
    api('GET', '/admin/api/failover'),
  ]);
  views.renderRoutes();
  views.renderUpstreams();   // 里面会把供应商弹窗的分组列表一起刷
  syncPickerChecks();
}

async function refreshStats() {
  state.stats = await api('GET', '/admin/api/stats');
  views.renderLive();
  views.updateKpiLive();
}

/* skipSeries：切时间窗时由 animateWindowChange 负责在淡出淡入之间换图，
   这里就别先原地渲染一次，否则新数据会先闪一下再被淡出 */
async function refreshOverview({ skipSeries = false } = {}) {
  state.overview = await api('GET', `/admin/api/overview?window=${state.window}&top=8`);
  views.renderKpis();
  if (!skipSeries) views.renderSeries();
  views.renderHealth();
  views.renderHot();
  views.renderRoutes();     // 路由卡上要显示"这个模型用了多少次"
  views.renderUpstreams();  // 上游视图上要显示成功率 / P95
}

async function refreshLog() {
  const rows = await api('GET', '/admin/api/requests?limit=50');
  views.renderLog(rows);
}

/* ---------------------------------------------------------------- 视图路由 */

const VIEWS = ['overview', 'upstreams', 'log'];
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
  if (v === state.iface) return;
  state.iface = v;
  localStorage.setItem('mg-iface', v);
  for (const b of $('seg-iface').children) b.classList.toggle('is-on', b.dataset.iface === v);
  views.renderRoutes();
}

function setProto(p) {
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
    ${copyRow('API 地址（填到 <code>/v1</code> 为止）', `${origin}/v1`)}
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

function openUpstream(id) {
  state.editing = id;
  const u = id === null ? null : state.upstreams.find((x) => x.id === id);
  $('up-title').textContent = u ? `编辑供应商：${u.name}` : '添加供应商';
  $('up-name').value = u ? u.name : '';
  $('up-base').value = u ? u.base_url : '';
  $('up-override').value = u ? (u.header_override || '') : '';
  $('up-enabled').checked = u ? u.enabled : true;
  baseHint();
  markOverride();

  const hint = $('up-groups-hint');
  hint.hidden = Boolean(u);
  if (!u) hint.innerHTML = '保存后就能在这里加第一个分组：选接口（Anthropic / OpenAI）＋ 填那把 key。';
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

function upstreamPayload() {
  return {
    name: $('up-name').value.trim(),
    base_url: $('up-base').value.trim(),
    enabled: $('up-enabled').checked,
    header_override: $('up-override').value.trim(),
  };
}

async function saveUpstream() {
  const payload = upstreamPayload();
  if (!payload.name || !payload.base_url) return toast('名称和 Base URL 都要填', 'err');
  if (payload.header_override) {
    try {
      const parsed = JSON.parse(payload.header_override);
      if (typeof parsed !== 'object' || parsed === null || Array.isArray(parsed)) throw new Error('必须是一个对象');
    } catch (e) {
      return toast('请求头覆写不是合法 JSON：' + e.message, 'err');
    }
  }
  if (state.editing === null) {
    const created = await api('POST', '/admin/api/upstreams', payload);
    // 供应商弹窗不关：分组就在它下半部分管。新建的供应商还没有分组、用不了，
    // 所以直接把分组弹窗叠上去；关掉那层就回到这儿，新分组已经列在下面了
    state.editing = created.id;
    $('up-title').textContent = `编辑供应商：${created.name}`;
    $('up-groups-hint').hidden = true;
    await refreshConfig();
    toast('已保存，接着建第一个分组', 'ok');
    openGroup(created.id, null);
    return;
  }
  await api('PUT', `/admin/api/upstreams/${state.editing}`, payload);
  toast('已保存', 'ok');
  markOverride();
  await refreshConfig();
}

/* ---------------------------------------------------------------- 分组弹窗 */

/* 分组管「走哪种接口、用哪把 key、能看到哪些模型」。模型列表必须跟着 key 走 —— 同一个站的
   两把 key 能拉到的东西常常不一样，这也是分组存在的理由。 */
function fillIfaceSelect(selectId, value) {
  $(selectId).innerHTML = PROTOCOLS.map((p) =>
    `<option value="${p}">${PROTO_LABEL[p]}　${PROTO_PATH[p]} · ${PROTO_CLIENT[p]}</option>`).join('');
  $(selectId).value = value;
}

function openGroup(upstreamId, gid) {
  const up = state.upstreams.find((u) => u.id === Number(upstreamId));
  if (!up) return toast('供应商不存在了，刷新一下', 'err');
  const g = gid === null ? null : (up.groups || []).find((x) => x.id === gid);
  state.editingUp = up.id;
  state.editingGroup = g ? g.id : null;

  $('grp-title').textContent = g ? `编辑分组：${up.name} · ${g.name}` : `给「${up.name}」加分组`;
  $('grp-name').value = g ? g.name : '默认';
  $('grp-key').value = g ? g.api_key : '';
  $('grp-key').type = 'password';
  $('grp-enabled').checked = g ? g.enabled : true;

  // 新分组的接口：这个站只有一种接口时默认补上缺的那种，否则跟当前分段
  const have = up.supports || [];
  const missing = PROTOCOLS.find((p) => !have.includes(p));
  fillIfaceSelect('grp-proto', g ? g.protocol
    : (have.length === 1 && missing ? missing : (state.iface || 'openai')));

  // 有候选就不给改接口了：候选是「这个模型在哪个接口下暴露」的唯一记录（后端也会拒）
  const taken = g ? modelsOfGroup(g.id).length : 0;
  $('grp-proto').disabled = taken > 0;
  $('grp-proto-hint').hidden = taken === 0;
  if (taken) {
    $('grp-proto-hint').innerHTML = `这个分组下有 <b>${taken}</b> 个模型候选，接口锁住了 ——`
      + ' 要换接口就给另一种接口新建一个分组，别把已录入的模型悄悄换成另一种线格式。';
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
  $('grp-name').focus();
}

async function saveGroup() {
  const payload = {
    name: $('grp-name').value.trim(),
    protocol: $('grp-proto').value,
    api_key: $('grp-key').value.trim(),
    enabled: $('grp-enabled').checked,
  };
  if (!payload.name) return toast('分组名不能为空', 'err');
  if (state.editingGroup === null) {
    const created = await api('POST', `/admin/api/upstreams/${state.editingUp}/groups`, payload);
    state.editingGroup = created.id;
    const owner = state.upstreams.find((x) => x.id === state.editingUp);
    $('grp-title').textContent = `编辑分组：${owner ? owner.name : ''} · ${created.name}`;
    $('grp-import').hidden = false;
    $('grp-move-wrap').hidden = false;
    toast('已保存，接着挑模型', 'ok');
  } else {
    const moveTo = Number($('grp-upstream').value);
    if (moveTo && moveTo !== state.editingUp) payload.upstream_id = moveTo;
    await api('PUT', `/admin/api/groups/${state.editingGroup}`, payload);
    if (payload.upstream_id) state.editingUp = payload.upstream_id;
    toast(payload.upstream_id ? '已保存并搬到新供应商下' : '已保存', 'ok');
  }
  await refreshConfig();
  renderPicker();
}

/* ---------------------------------------------------------------- 分组里的模型

   这一段是「所见即所存」：勾选框的状态就是库里的候选，勾上/取消当场发请求，没有
   「导入所选」也没有保存按钮。上面的组名 / 接口 / key 才是要保存的东西。
   已录入的排在前面（勾着），后面是这次拉取到、还没录入的。 */

let pulled = [];   // 最近一次从上游拉到的模型名；换分组就清空

const pickRow = (name, on) => `<label class="${on ? 'on' : ''}">
    <input type="checkbox" ${on ? 'checked' : ''} data-act="pick-toggle" data-name="${esc(name)}">
    <span>${esc(name)}</span>
  </label>`;

function renderPicker() {
  const gid = state.editingGroup;
  const host = $('grp-picker');
  if (gid === null) { host.innerHTML = ''; return; }

  const mine = modelsOfGroup(gid);
  const owned = new Set(mine);
  const rest = pulled.filter((m) => !owned.has(m));

  const head = `<div class="pick-sep">这个分组里的 ${mine.length} 个</div>`;
  const body = mine.length
    ? mine.map((m) => pickRow(m, true)).join('')
    : '<div class="dim" style="padding:4px 6px;font-size:12px">还没有。点「拉取模型列表」，或者手动填一个。</div>';
  const tail = rest.length
    ? `<div class="pick-sep">上游还有 ${rest.length} 个没录入</div>${rest.map((m) => pickRow(m, false)).join('')}`
    : '';

  host.innerHTML = head + body + tail;
  $('grp-mcount').textContent = `${mine.length} 个`;
  applyPickerFilter();
}

/** 请求失败或别处改了配置之后，把勾选状态拉回库里的真相，顺序不动（重排会让人点空） */
function syncPickerChecks() {
  const gid = state.editingGroup;
  if (gid === null || !$('group-dialog').open) return;
  const owned = new Set(modelsOfGroup(gid));
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
  // 过滤到一个不剩的那节连小标题一起收起，别留个「上游还有 42 个没录入」的空壳
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

async function addModels(names) {
  if (!names.length) return;
  const r = await api('POST', '/admin/api/models/bulk-add', {
    group_id: state.editingGroup, model_names: names,
  });
  const skipped = r.skipped || [];
  // 撞上「已经在另一种接口下暴露」的名字只跳过它，剩下的照样进；但得说清是哪些
  if (skipped.length) {
    toast(
      `加上了 ${r.added} 个，跳过 ${skipped.length} 个（${skipped.slice(0, 3).join('、')}`
      + `${skipped.length > 3 ? ' 等' : ''}已经在另一种接口下暴露了）`,
      'err',
    );
    return;
  }
  toast(names.length === 1 ? `已加上 ${names[0]}` : `加上了 ${r.added} 个`, 'ok');
}

/** 取消勾选就是删候选。删到某个模型一个候选都不剩时先问一句 —— 那等于把模型下线了 */
async function removeModel(name) {
  const row = state.routes.find((r) => r.model_name === name);
  if (row && row.candidates.length === 1) {
    const okay = await confirmBox({
      title: '这是它唯一的候选',
      body: `<b>${esc(name)}</b> 只在这个分组里有候选，取消勾选等于把这个模型下线，`
        + '下游再调它就是 404。<br><br>只是想换个站的话，先在「模型路由」里给它加一个别的候选。',
      ok: '下线它',
    });
    if (!okay) return false;
  }
  await api(
    'DELETE',
    `/admin/api/models?model_name=${encodeURIComponent(name)}&group_id=${state.editingGroup}`,
  );
  return true;
}

/* ---------------------------------------------------------------- 路由弹窗 */

/* 一个弹窗三种用法：新增模型 / 给已有模型加候选 / 改某个候选（上游真名 + 1M）。
   「接口」在这里只是个过滤器 —— 真正决定模型走哪种接口的是它候选所在分组的接口。 */
function openRoute(model, gid) {
  const row = model ? state.routes.find((r) => r.model_name === model) : null;
  const cand = row && gid ? row.candidates.find((c) => c.group_id === gid) : null;
  // 已有模型的接口已经定了；新增时跟当前分段（分段在「全部」就默认 OpenAI）
  const iface = row ? row.protocol : (state.iface || 'openai');
  const pool = upstreamsFor(iface);
  if (!pool.length) {
    return toast(`没有 ${PROTO_LABEL[iface]} 接口的分组，先去「上游站点」给某个站加一个`, 'err');
  }
  state.editingCand = cand ? { model, gid } : null;

  $('route-title').textContent = cand ? `改候选：${model}` : (model ? `给「${model}」加候选` : '新增模型');
  $('rt-save').textContent = cand ? '保存' : '添加';
  $('rt-model').value = model || '';
  $('rt-model').readOnly = Boolean(model);
  fillIfaceSelect('rt-iface', iface);
  $('rt-iface-wrap').hidden = Boolean(row);   // 已有模型的接口不给改
  fillUpstreamSelect('rt-upstream', pool);
  $('rt-upstream').disabled = Boolean(cand);
  $('rt-group').disabled = Boolean(cand);     // 改候选就是改这一条，别顺手换成另一个分组

  if (cand) {
    $('rt-upstream').value = String(cand.upstream_id);
    fillGroupSelect('rt-group', cand.upstream_id, iface);
    $('rt-group').value = String(gid);
  } else {
    // 加候选时先落在「还有空分组」的供应商上：默认给第一个的话，遇到它唯一的分组
    // 已经是候选（很常见），弹窗一开就是个死局，点保存只能换来一句 409
    const taken = new Set(row ? row.candidates.map((c) => c.group_id) : []);
    const free = pool.find((u) => groupsOfIface(u, iface).some((g) => !taken.has(g.id)));
    if (!free && row) {
      return toast(
        `${model} 在每个 ${PROTO_LABEL[iface]} 分组下都已经是候选了 —— `
        + '要再加就先去「上游站点」建一个新分组',
        'err',
      );
    }
    if (free) $('rt-upstream').value = String(free.id);
    fillGroupSelect('rt-group', $('rt-upstream').value, iface);
    markTakenGroups(model);
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
  const row = state.routes.find((r) => r.model_name === cand.model);
  const ids = row ? row.candidates.map((c) => c.group_id) : [];
  const at = ids.indexOf(cand.gid);
  $('rt-order-hint').textContent = at < 0 ? '' : `第 ${at + 1} / ${ids.length} 位`;
  for (const btn of $('rt-order-wrap').querySelectorAll('[data-act="move-cand"]')) {
    const to = at + Number(btn.dataset.dir);
    btn.disabled = at < 0 || to < 0 || to >= ids.length;
  }
}

/* 「上游那边的真实模型名」得跟分组对上 —— 同一个站两把 key 能看到的东西都不一样，
   靠记是记不住的。分组一选定就把那个分组能拉到的模型灌进 datalist，点输入框直接选；
   拉不动的站退回「这个分组已经用过的那些名字」，照样能填。 */
const remoteCache = new Map();      // gid -> string[]
const remoteBusy = new Set();
const remoteDead = new Set();       // 拉过一次没成的，别每次开弹窗都再撞一遍

function fillRemoteList(gid) {
  const hint = $('rt-remote-hint');
  if (!gid) { $('rt-remote-list').innerHTML = ''; hint.textContent = ''; return; }
  const pulledNames = remoteCache.get(gid);
  const names = [...new Set([...(pulledNames || []), ...remotesOfGroup(gid)])];
  $('rt-remote-list').innerHTML = names.map((n) => `<option value="${esc(n)}"></option>`).join('');

  if (pulledNames) hint.textContent = `这个分组能拉到 ${pulledNames.length} 个模型，点输入框下拉选`;
  else if (remoteBusy.has(gid)) hint.textContent = '正在拉这个分组的模型列表…';
  else if (remoteDead.has(gid)) hint.textContent = names.length
    ? `拉不动这个分组，下拉里是它已经用过的 ${names.length} 个名字`
    : '拉不动这个分组的模型列表，手动填';
  else hint.textContent = '';

  if (!pulledNames && !remoteBusy.has(gid) && !remoteDead.has(gid)) pullRemoteList(gid);
}

async function pullRemoteList(gid) {
  remoteBusy.add(gid);
  $('rt-remote-hint').textContent = '正在拉这个分组的模型列表…';
  try {
    const data = await api('GET', `/admin/api/groups/${gid}/remote-models`);
    remoteCache.set(gid, data.models);
  } catch {
    remoteDead.add(gid);          // 公益站三天两头连不上，静默降级就行，别弹提示条
  } finally {
    remoteBusy.delete(gid);
    // 拉的过程里可能已经换了分组、或者把弹窗关了
    if ($('route-dialog').open && Number($('rt-group').value) === gid) fillRemoteList(gid);
  }
}

/** 已经是候选的分组在下拉里禁掉，比提交后再报 409 友好 */
function markTakenGroups(model) {
  const group = model ? state.routes.find((r) => r.model_name === model) : null;
  const taken = new Set(group ? group.candidates.map((c) => c.group_id) : []);
  for (const opt of $('rt-group').options) {
    if (!taken.has(Number(opt.value))) continue;
    opt.disabled = true;
    opt.textContent += '（已是候选）';
  }
  const first = [...$('rt-group').options].find((o) => !o.disabled);
  if (first) $('rt-group').value = first.value;
}

async function saveRoute() {
  const editing = Boolean(state.editingCand);
  const model = $('rt-model').value.trim();
  if (!model) return toast('模型名不能为空', 'err');
  const gid = Number($('rt-group').value);
  if (!gid) return toast('这个供应商没有对应接口的分组', 'err');
  // 勾了 1M 就把后缀写进真实模型名：网关转发时摘掉它、换成 anthropic-beta 头
  const remote = withOneM(
    $('rt-remote').value.trim() || model,
    $('rt-onem').checked && !$('rt-onem-wrap').hidden,
  );
  await api(editing ? 'PUT' : 'POST', '/admin/api/models', {
    model_name: model, group_id: gid, remote_model: remote,
  });
  $('route-dialog').close();
  await refreshConfig();
  toast(editing ? '已保存' : '已添加', 'ok');
}

/* ---------------------------------------------------------------- 动作表 */

const ACTIONS = {
  'copy-endpoint': copyEndpoint,

  'copy-text': async ({ text }) => {
    try {
      await navigator.clipboard.writeText(text);
      toast('已复制', 'ok');
    } catch {
      toast('复制失败，手动选中复制吧', 'err');
    }
  },

  'show-access': showAccess,

  'toggle-theme': cycleTheme,

  'close-dialog': (_d, el) => el.closest('dialog').close(),

  'new-upstream': () => openUpstream(null),
  'edit-upstream': ({ uid }) => openUpstream(Number(uid)),

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
      await api('PUT', `/admin/api/upstreams/${u.id}`, {
        name: u.name, base_url: u.base_url, header_override: u.header_override,
        enabled: el.checked,
      });
    } catch (e) {
      el.checked = !el.checked;
      throw e;
    }
    await refreshConfig();
  },

  'del-upstream': async ({ uid }) => {
    const id = Number(uid);
    const u = state.upstreams.find((x) => x.id === id);
    const n = modelsOfUpstream(id).length;
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

  'clone-group': async ({ gid }) => {
    const created = await api('POST', `/admin/api/groups/${gid}/clone`);
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
    const n = modelsOfGroup(id).length;
    const last = up && (up.groups || []).length === 1;
    const okay = await confirmBox({
      title: '删除分组',
      body: `要删掉 <b>${esc(up ? up.name : '?')}</b> 下的 ${PROTO_LABEL[g ? g.protocol : ''] || ''}`
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

  /* 拉取用的是**服务端存着的**那把 key。key 改了没保存就点拉取，拉的是旧 key，
     回来一个 401 让人一头雾水 —— 先把改动落库，再拉。 */
  'pull-models': async () => {
    if (groupDirty()) await saveGroup();
    $('grp-pull-status').textContent = '拉取中…';
    try {
      const data = await api('GET', `/admin/api/groups/${state.editingGroup}/remote-models`);
      pulled = data.models;
      remoteCache.set(state.editingGroup, data.models);
      remoteDead.delete(state.editingGroup);
      renderPicker();
      const mine = modelsOfGroup(state.editingGroup);
      const hit = pulled.filter((m) => mine.includes(m)).length;
      $('grp-pull-status').textContent = `上游列出 ${pulled.length} 个，其中 ${hit} 个已录入`;
    } catch (e) {
      $('grp-pull-status').textContent = '';
      throw e;
    }
  },

  /* 勾选即生效：勾上=加候选，取消=删候选。失败就把勾回滚到库里的真相。 */
  'pick-toggle': async ({ name }, el) => {
    if (state.editingGroup === null) return;
    const label = el.closest('label');
    label.classList.add('busy');
    try {
      if (el.checked) await addModels([name]);
      else await removeModel(name);
    } finally {
      label.classList.remove('busy');
      await refreshConfig();      // 末尾的 syncPickerChecks 负责把勾选拉回真相
    }
  },

  'pick-all': async () => {
    const names = visibleRows(false);
    if (!names.length) return toast('没有可加的了', 'ok');
    await addModels(names);
    await refreshConfig();
    renderPicker();
  },

  'pick-none': async () => {
    const names = visibleRows(true);
    if (!names.length) return toast('本来就一个都没勾', 'ok');
    // 只在这个分组有候选的模型会直接下线，这个后果得先说清楚
    const orphan = names.filter((n) => {
      const row = state.routes.find((r) => r.model_name === n);
      return row && row.candidates.length === 1;
    });
    const okay = await confirmBox({
      title: '清空这个分组的模型',
      body: `要把 <b>${names.length}</b> 个模型从这个分组里去掉。`
        + (orphan.length
          ? `<br><br>其中 <b>${orphan.length}</b> 个只有这一个候选，去掉就等于下线，下游再调是 404。`
          : '<br><br>它们在别的分组还有候选，流量会自动落到那边。'),
      ok: '去掉',
    });
    if (!okay) return;
    for (const name of names) {
      await api(
        'DELETE',
        `/admin/api/models?model_name=${encodeURIComponent(name)}&group_id=${state.editingGroup}`,
      );
    }
    await refreshConfig();
    renderPicker();
    toast(`去掉了 ${names.length} 个`, 'ok');
  },

  /* 上游不肯列全的时候（不少站的 /v1/models 就是残的）自己填一个 */
  'manual-add': async () => {
    const name = $('grp-manual').value.trim();
    if (!name) return toast('填个模型 id', 'err');
    if (modelsOfGroup(state.editingGroup).includes(name)) return toast('这个分组已经有它了', 'err');
    await addModels([name]);
    $('grp-manual').value = '';
    await refreshConfig();
    renderPicker();
  },

  'new-route': () => openRoute('', null),
  'add-candidate': ({ model }) => openRoute(model, null),
  'edit-candidate': ({ model, gid }) => openRoute(model, Number(gid)),

  /* 自动降级开关。按接口分开：Claude 侧的中转站坏得勤、值得自动换；
     GPT 侧除了 站A 都是要花钱的站，花钱图稳定，得手动确认。
     属性名用 data-fo 而不是 data-iface —— 后者被模型路由那个分段选择器占了，
     开关会连带把筛选也切掉（preset-fp 当初就踩过这个坑）。 */
  'toggle-failover': async ({ fo }, el) => {
    try {
      state.failover = await api('POST', '/admin/api/failover', {
        protocol: fo, enabled: el.checked,
      });
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
    const row = state.routes.find((r) => r.model_name === cand.model);
    if (!row) return;
    const ids = row.candidates.map((c) => c.group_id);
    const at = ids.indexOf(cand.gid);
    const to = at + Number(dir);
    if (at < 0 || to < 0 || to >= ids.length) return;
    ids.splice(to, 0, ids.splice(at, 1)[0]);
    await api('POST', '/admin/api/models/order', { model_name: cand.model, order: ids });
    await refreshConfig();
    // rAF：run() 的 finally 会把刚点的那个按钮重新启用，得等它跑完再按新位置算禁用
    requestAnimationFrame(orderHint);
  },

  'switch': async ({ model, gid }) => {
    const id = Number(gid);
    const group = state.routes.find((r) => r.model_name === model);
    const cand = group && group.candidates.find((c) => c.group_id === id);
    if (!cand || cand.is_active) return;
    if (!cand.upstream_enabled) return toast('这个供应商是停用状态，先在「上游站点」里启用它', 'err');
    if (!cand.group_enabled) return toast('这个分组是停用状态，展开那一行把它打开', 'err');
    const fromGid = group.active_group_id;
    await api('POST', '/admin/api/models/switch', { model_name: model, group_id: id });
    await refreshConfig();
    views.afterSwitch(model, fromGid, id);
    toast(`${model} → ${groupLabel(id)}`, 'ok');
  },

  'del-candidate': async ({ model, gid }) => {
    const id = Number(gid);
    const group = state.routes.find((r) => r.model_name === model);
    const last = group && group.candidates.length === 1;
    const okay = await confirmBox({
      title: last ? '移除最后一个候选' : '移除候选',
      body: last
        ? `<b>${esc(model)}</b> 只剩这一个候选，移除后它就不再对下游暴露了。`
        : `把 <b>${esc(model)}</b> 从 <b>${esc(groupLabel(id))}</b> 的候选里去掉？`
          + '<br><br>如果它正好是当前生效的，流量会自动落到剩下的候选之一。',
      ok: '移除',
    });
    if (!okay) return;
    await api('DELETE', `/admin/api/models?model_name=${encodeURIComponent(model)}&group_id=${id}`);
    await refreshConfig();
    toast('已移除', 'ok');
  },

  'del-model': async ({ model }) => {
    const group = state.routes.find((r) => r.model_name === model);
    const n = group ? group.candidates.length : 0;
    const okay = await confirmBox({
      title: '删除模型',
      body: `删掉 <b>${esc(model)}</b> 的全部 ${n} 条候选，它将不再出现在 /v1/models 里。`
        + '<br><br>上游站点本身不受影响，之后还能重新导入。',
      ok: '删除',
    });
    if (!okay) return;
    await api('DELETE', `/admin/api/models?model_name=${encodeURIComponent(model)}`);
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

  // 两个分段选择器：data-iface 在模型路由上，data-proto 在转发记录上。
  // 记录行用的是 data-log-proto、候选圆片用 data-gid，都不会被这里的 closest 命中
  const ifaceSeg = ev.target.closest('[data-iface]');
  if (ifaceSeg) { setIface(ifaceSeg.dataset.iface); return; }

  const proto = ev.target.closest('[data-proto]');
  if (proto) { setProto(proto.dataset.proto); return; }

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

// 供应商换了就把分组下拉重填一遍；接口换了连供应商池一起换
$('rt-upstream').addEventListener('change', () => {
  fillGroupSelect('rt-group', $('rt-upstream').value, $('rt-iface').value);
  markTakenGroups($('rt-model').value.trim());
  fillRemoteList(Number($('rt-group').value));
});

// 分组定了才知道「上游真名」能填哪些
$('rt-group').addEventListener('change', () => fillRemoteList(Number($('rt-group').value)));

$('rt-iface').addEventListener('change', () => {
  const iface = $('rt-iface').value;
  const pool = upstreamsFor(iface);
  if (!pool.length) toast(`没有 ${PROTO_LABEL[iface]} 接口的分组，先去「上游站点」加一个`, 'err');
  fillUpstreamSelect('rt-upstream', pool);
  fillGroupSelect('rt-group', $('rt-upstream').value, iface);
  markTakenGroups($('rt-model').value.trim());
  fillRemoteList(Number($('rt-group').value));
  $('rt-onem-wrap').hidden = iface !== 'anthropic';
});

// 站根填/改的时候把补出来的两个地址实时显示出来，免得又把 /v1 带上
$('up-base').addEventListener('input', baseHint);
$('up-override').addEventListener('input', markOverride);

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
   这里**不清** state.editing / editingUp / editingGroup：close 是排成任务异步触发的，
   会晚于紧接着打开的下一个弹窗跑 —— 清掉的话「建完供应商接着建分组」就会往
   /upstreams/null/groups 发请求。这几个字段每次 open* 都会重设，留着旧值没人读得到。
   分组弹窗另有一份 editingUp，所以它叠在供应商弹窗上面开着、甚至把分组搬到别的
   供应商，也不会把底下那个弹窗的目标换掉。 */
$('up-dialog').addEventListener('close', () => run(null, refreshConfig));
$('group-dialog').addEventListener('close', () => run(null, refreshConfig));

// route-dialog 的 editingCand 同理：也只由 openRoute 负责重设，close 时不动

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
setInterval(() => { if (ticking()) run(null, refreshStats); }, 3000);

// 慢轮取统计和记录：数据量大一些，15 秒足够。配置也跟着刷 —— 断路器的冷却剩余
// 在候选圆片上是要走字的，而且从别处（另一个标签页、手动切换）改过的配置也该跟上
setInterval(() => {
  if (!ticking()) return;
  run(null, async () => {
    await Promise.all([refreshOverview(), refreshLog(), refreshConfig()]);
  });
}, 15000);

/* ---------------------------------------------------------------- 启动 */

applyTheme(localStorage.getItem('mg-theme') || 'auto');
$('endpoint').textContent = `${location.origin}/v1`;
views.initLogFollow();   // 「自动跟随新记录」的勾选状态变化时补插攒下的行
for (const b of $('seg-window').children) b.classList.toggle('is-on', b.dataset.window === state.window);

state.iface = localStorage.getItem('mg-iface') || '';
for (const b of $('seg-iface').children) b.classList.toggle('is-on', b.dataset.iface === state.iface);

const initial = VIEWS.includes(location.hash.slice(1)) ? location.hash.slice(1) : 'overview';
currentView = '';
showView(initial);

run(null, async () => {
  await Promise.all([refreshConfig(), refreshOverview(), refreshStats()]);
  await refreshLog();
});

// 侧栏高亮条的初始位置要等布局算完，否则会量到 0
requestAnimationFrame(() => {
  moveMarker($('nav-marker'), document.querySelector(`.nav-item[data-view="${currentView}"]`));
  initSpotlightAndTilt();
});
