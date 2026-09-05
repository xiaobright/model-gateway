'use strict';
/* ================================================================
   Model Gateway 控制台 · 入口
   分工：模型路由视图管「哪个模型走哪个上游」，上游视图管「站点怎么连
   + 批量导入模型」，转发记录视图只读，概览视图看全局。所有交互走事件
   委托（元素上写 data-act），不往 HTML 里拼 onclick 字符串。
   ================================================================ */

import {
  $, state, api, toast, confirmBox, run, esc,
  modelsOfGroup, modelsOfUpstream, groupLabel, upstreamOfGroup, groupOf, supportsSide, SIDE_LABEL,
} from './util.js';
import { withViewTransition, moveMarker, reduceMotion, initSpotlightAndTilt, refreshLightTargets } from './motion.js';
import * as views from './views.js';

/* ---------------------------------------------------------------- 数据 */

async function refreshConfig() {
  [state.upstreams, state.routes] = await Promise.all([
    api('GET', '/admin/api/upstreams'),
    api('GET', '/admin/api/models'),
  ]);
  views.renderRoutes();
  views.renderUpstreams();
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

/* ---------------------------------------------------------------- 分侧与协议筛选 */

/* 两个都是纯前端筛选，不重新拉数据。
   data-side 在模型路由的分段上，data-proto 在转发记录的分段上，各自独立不会撞。 */
function setSide(s) {
  if (s === state.side) return;
  state.side = s;
  localStorage.setItem('mg-side', s);
  for (const b of $('seg-side').children) b.classList.toggle('is-on', b.dataset.side === s);
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

/** 已录入的模型名里属于这个档位的那个；没有就给个默认名 */
function tierModel(tier) {
  const hit = state.routes.map((r) => r.model_name).filter((n) => n.toLowerCase().includes(tier.key));
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
      里录入的一致，没配过的名字会 404（只有认得出档位关键字的才会被兜到同档位那条配置上）。
      fable 档没有对应的环境变量，在 Claude Code 里用 <code>/model</code> 选。
    </p>`;
  $('access-dialog').showModal();
}

/* ---------------------------------------------------------------- Claude 档位 */

/** 只列标了 Claude 的供应商 —— 没打标记的也列出来，否则新装的库一个都选不出来 */
function claudeUpstreams() {
  return state.upstreams.filter((u) => supportsSide(u, 'anthropic'));
}

function fillUpstreamSelect(selectId, pool) {
  $(selectId).innerHTML = pool.map((u) =>
    `<option value="${u.id}">${esc(u.name)}${u.enabled ? '' : '（停用）'}</option>`).join('');
}

/** 供应商选好之后填它的分组；只有一个分组时下拉框还是留着，省得布局跳 */
function fillGroupSelect(selectId, upstreamId) {
  const up = state.upstreams.find((u) => u.id === Number(upstreamId));
  const groups = (up && up.groups) || [];
  $(selectId).innerHTML = groups.map((g) =>
    `<option value="${g.id}">${esc(g.name)}${g.enabled ? '' : '（停用）'}</option>`).join('')
    || '<option value="">（这个供应商还没有分组）</option>';
}

function openTiers() {
  const pool = claudeUpstreams();
  if (!pool.length) {
    return toast('没有标了 Claude 的供应商。先去「上游站点」给支持 Anthropic 的站勾上 Claude', 'err');
  }
  fillUpstreamSelect('tier-upstream', pool);
  fillGroupSelect('tier-group', $('tier-upstream').value);
  $('tier-models').innerHTML = '';
  $('tier-pull-status').textContent = '';
  $('tier-grid').innerHTML = `
    <span class="t-head">档位</span>
    <span class="t-head">对下游暴露的名字</span>
    <span class="t-head">上游那边的真实名</span>
    <span class="t-head">1M</span>`
    + TIERS.map((t) => `
      <span class="t-name">${t.key}</span>
      <input type="text" data-tier="${t.key}" data-role="name" value="${esc(tierModel(t))}"
             placeholder="留空跳过" autocomplete="off">
      <input type="text" data-tier="${t.key}" data-role="remote" list="tier-models"
             placeholder="留空 = 同名" autocomplete="off">
      <input type="checkbox" data-tier="${t.key}" data-role="onem" aria-label="${t.key} 档用 1M 上下文">`).join('');
  $('tier-dialog').showModal();
  $('tier-upstream').focus();
}

function tierRows() {
  return TIERS.map((t) => {
    const pick = (role) => $('tier-grid').querySelector(`[data-tier="${t.key}"][data-role="${role}"]`);
    return { key: t.key, name: pick('name').value.trim(), remote: pick('remote'), onem: pick('onem').checked };
  });
}

/** 拉到模型列表后只在档位关键字唯一命中时自动填，多个候选就交给下拉框，不瞎猜 */
function prefillRemotes(models) {
  let filled = 0;
  for (const row of tierRows()) {
    if (row.remote.value.trim()) continue;
    const hit = models.filter((m) => m.toLowerCase().includes(row.key));
    if (hit.length === 1) { row.remote.value = hit[0]; filled += 1; }
  }
  return filled;
}

async function saveTiers() {
  const gid = Number($('tier-group').value);
  if (!gid) return toast('这个供应商还没有分组', 'err');
  const wanted = tierRows()
    .map((r) => ({ ...r, remote: r.remote.value.trim() }))
    .filter((r) => r.name);
  if (!wanted.length) return toast('四个档位都是空的', 'err');

  let added = 0;
  const failed = [];
  for (const r of wanted) {
    // 勾了 1M 就把后缀写进真实模型名：网关转发时摘掉它、换成 anthropic-beta 头
    const remote = (r.remote || r.name) + (r.onem ? '[1m]' : '');
    try {
      await api('POST', '/admin/api/models', {
        model_name: r.name, group_id: gid, remote_model: remote, side: 'anthropic',
      });
      added += 1;
    } catch (e) {
      failed.push(`${r.key}: ${e.message}`);
    }
  }
  await refreshConfig();
  $('tier-dialog').close();
  if (added) toast(`建了 ${added} 档${failed.length ? `，${failed.length} 档没建成` : ''}`, 'ok');
  if (failed.length) toast(failed.join('；'), 'err');
}

/* ---------------------------------------------------------------- 供应商弹窗 */

/* 供应商管「站在哪、怎么连」，key 和模型列表在分组里。新建时这里还有一个 API Key 字段，
   它落到自动创建的「默认」分组上 —— 一把 key 的常见情况就不用再开一次分组弹窗了。 */
function openUpstream(id) {
  state.editing = id;
  const u = id === null ? null : state.upstreams.find((x) => x.id === id);
  $('up-title').textContent = u ? `编辑供应商：${u.name}` : '添加供应商';
  $('up-name').value = u ? u.name : '';
  $('up-base').value = u ? u.base_url : '';
  $('up-override').value = u ? (u.header_override || '') : '';
  $('up-enabled').checked = u ? u.enabled : true;
  $('up-key').value = '';
  $('up-key').type = 'password';
  // 编辑时不给 key 字段：一个供应商可能有好几把，改哪一把得说清楚，所以只在分组里改
  $('up-key-wrap').hidden = Boolean(u);
  const marks = u ? (u.protocols || []) : ['anthropic', 'openai'];
  $('up-proto-anthropic').checked = marks.includes('anthropic');
  $('up-proto-openai').checked = marks.includes('openai');
  const hint = $('up-groups-hint');
  hint.hidden = !u;
  if (u) {
    const names = (u.groups || []).map((g) => g.name).join('、') || '（没有分组）';
    hint.innerHTML = `Key 和模型列表按分组维护。当前分组：<b>${esc(names)}</b>`
      + ' —— 在列表里展开这一行去改。';
  }
  $('up-dialog').showModal();
  $('up-name').focus();
}

function upstreamPayload() {
  const protocols = [];
  if ($('up-proto-anthropic').checked) protocols.push('anthropic');
  if ($('up-proto-openai').checked) protocols.push('openai');
  return {
    name: $('up-name').value.trim(),
    base_url: $('up-base').value.trim(),
    enabled: $('up-enabled').checked,
    header_override: $('up-override').value.trim(),
    protocols,
    api_key: $('up-key').value.trim(),
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
    await refreshConfig();
    $('up-dialog').close();
    // 建完直接把默认分组的弹窗接上：紧接着要干的事就是拉模型列表
    const gid = created.groups && created.groups[0] && created.groups[0].id;
    toast('已保存', 'ok');
    if (gid) openGroup(created.id, gid);
    return;
  }
  await api('PUT', `/admin/api/upstreams/${state.editing}`, payload);
  toast('已保存', 'ok');
  await refreshConfig();
}

/* ---------------------------------------------------------------- 分组弹窗 */

/* 分组管「用哪把 key、能看到哪些模型」。模型列表必须跟着 key 走 —— 同一个站的两把 key
   能拉到的东西常常不一样，这也是分组存在的理由。 */
function openGroup(upstreamId, gid) {
  const up = state.upstreams.find((u) => u.id === Number(upstreamId));
  if (!up) return toast('供应商不存在了，刷新一下', 'err');
  const g = gid === null ? null : (up.groups || []).find((x) => x.id === gid);
  state.editing = up.id;
  state.editingGroup = g ? g.id : null;

  $('grp-title').textContent = g ? `编辑分组：${up.name} · ${g.name}` : `给「${up.name}」加分组`;
  $('grp-name').value = g ? g.name : '';
  $('grp-key').value = g ? g.api_key : '';
  $('grp-key').type = 'password';
  $('grp-enabled').checked = g ? g.enabled : true;
  fillUpstreamSelect('grp-upstream', state.upstreams);
  $('grp-upstream').value = String(up.id);
  $('grp-move-wrap').hidden = !g;      // 新建时没得搬

  $('grp-side').innerHTML = ['openai', 'anthropic']
    .map((s) => `<option value="${s}">${SIDE_LABEL[s]}</option>`).join('');
  // 「导入为哪一侧」的默认值，按信号强弱：这个分组已录入模型的侧别 > 站点只标了一侧 > 当前分段
  const already = new Set(state.routes
    .filter((r) => g && (r.candidates || []).some((c) => c.group_id === g.id))
    .map((r) => r.side).filter(Boolean));
  const marks = up.protocols || [];
  if (already.size === 1) $('grp-side').value = [...already][0];
  else if (marks.length === 1) $('grp-side').value = marks[0];
  else if (state.side) $('grp-side').value = state.side;

  resetPicker();
  $('grp-import').hidden = !g;
  if (g) $('grp-mcount').textContent = `已录入 ${modelsOfGroup(g.id).length} 个`;
  $('group-dialog').showModal();
  $('grp-name').focus();
}

function resetPicker() {
  $('grp-picker').hidden = true;
  $('grp-picker').innerHTML = '';
  $('grp-pull-status').textContent = '';
  $('grp-pick-bar').hidden = true;
  $('grp-picker-filter').value = '';
}

async function saveGroup() {
  const payload = {
    name: $('grp-name').value.trim(),
    api_key: $('grp-key').value.trim(),
    enabled: $('grp-enabled').checked,
  };
  if (!payload.name) return toast('分组名不能为空', 'err');
  if (state.editingGroup === null) {
    const created = await api('POST', `/admin/api/upstreams/${state.editing}/groups`, payload);
    state.editingGroup = created.id;
    $('grp-import').hidden = false;
    $('grp-move-wrap').hidden = false;
    $('grp-mcount').textContent = '已录入 0 个';
    toast('已保存，接着可以拉取模型', 'ok');
  } else {
    const moveTo = Number($('grp-upstream').value);
    if (moveTo && moveTo !== state.editing) payload.upstream_id = moveTo;
    await api('PUT', `/admin/api/groups/${state.editingGroup}`, payload);
    if (payload.upstream_id) state.editing = payload.upstream_id;
    toast(payload.upstream_id ? '已保存并搬到新供应商下' : '已保存', 'ok');
  }
  await refreshConfig();
}

function renderPicker(models) {
  const existing = new Set(modelsOfGroup(state.editingGroup));
  $('grp-picker').innerHTML = models.map((m) => `
    <label>
      <input type="checkbox" value="${esc(m)}" ${existing.has(m) ? '' : 'checked'}>
      <span>${esc(m)}</span>
      ${existing.has(m) ? '<span class="tag tag-good">已录入</span>' : ''}
    </label>`).join('') || '<div class="dim" style="padding:6px">上游没返回任何模型</div>';
  $('grp-picker').hidden = false;
  $('grp-pick-bar').hidden = false;
  $('grp-pull-status').textContent = `共 ${models.length} 个，未录入的已默认勾上`;
}

/* ---------------------------------------------------------------- 路由弹窗 */

function openRoute(model) {
  const group = model ? state.routes.find((r) => r.model_name === model) : null;
  // 追加候选时侧别已经定了，新增模型时跟当前分段（分段在「全部」就默认 GPT）
  const side = group ? group.side : (state.side || 'openai');
  const pool = state.upstreams.filter((u) => supportsSide(u, side));
  if (!pool.length) {
    return toast(`没有标了 ${SIDE_LABEL[side] || side} 的供应商，先去「上游站点」勾上`, 'err');
  }

  $('route-title').textContent = model ? `给「${model}」加候选` : '新增模型';
  $('rt-model').value = model || '';
  $('rt-model').readOnly = Boolean(model);
  $('rt-remote').value = '';
  $('rt-side').innerHTML = ['openai', 'anthropic']
    .map((s) => `<option value="${s}">${SIDE_LABEL[s]}</option>`).join('');
  $('rt-side').value = side;
  $('rt-side-wrap').hidden = Boolean(model);   // 已有模型的侧别不给改
  fillUpstreamSelect('rt-upstream', pool);
  fillGroupSelect('rt-group', $('rt-upstream').value);
  markTakenGroups(model);
  $('route-dialog').showModal();
  (model ? $('rt-upstream') : $('rt-model')).focus();
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
  const model = $('rt-model').value.trim();
  if (!model) return toast('模型名不能为空', 'err');
  const gid = Number($('rt-group').value);
  if (!gid) return toast('这个供应商还没有分组', 'err');
  await api('POST', '/admin/api/models', {
    model_name: model,
    group_id: gid,
    remote_model: $('rt-remote').value.trim(),
    side: $('rt-side').value,
  });
  $('route-dialog').close();
  await refreshConfig();
  toast('已添加', 'ok');
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

  'new-tiers': openTiers,

  'tier-pull': async () => {
    const gid = Number($('tier-group').value);
    if (!gid) return toast('这个供应商还没有分组', 'err');
    $('tier-pull-status').textContent = '拉取中…';
    try {
      const data = await api('GET', `/admin/api/groups/${gid}/remote-models`);
      $('tier-models').innerHTML = data.models.map((m) => `<option value="${esc(m)}">`).join('');
      const filled = prefillRemotes(data.models);
      $('tier-pull-status').textContent = `共 ${data.models.length} 个${filled ? `，自动填了 ${filled} 档` : ''}，第三列可下拉选`;
    } catch (e) {
      $('tier-pull-status').textContent = '';
      throw e;
    }
  },

  'new-upstream': () => openUpstream(null),
  'edit-upstream': ({ uid }) => openUpstream(Number(uid)),

  'toggle-groups': ({ uid }) => {
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
        protocols: u.protocols, enabled: el.checked,
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

  'toggle-group': async ({ gid }, el) => {
    const g = groupOf(Number(gid));
    try {
      await api('PUT', `/admin/api/groups/${g.id}`, {
        name: g.name, api_key: g.api_key, enabled: el.checked,
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
    const okay = await confirmBox({
      title: '删除分组',
      body: `要删掉 <b>${esc(up ? up.name : '?')}</b> 下的分组 <b>${esc(g ? g.name : id)}</b>，`
        + `连带它的 ${n} 条模型候选。`
        + '<br><br>这把 key 也会一起没掉。只想临时停用的话，把它的开关关掉就行。',
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

  'preset-codex': () => {
    $('up-override').value = '{\n  "user-agent": "codex_cli_rs",\n  "originator": "codex_cli_rs"\n}';
  },

  'pull-models': async () => {
    $('grp-pull-status').textContent = '拉取中…';
    try {
      const data = await api('GET', `/admin/api/groups/${state.editingGroup}/remote-models`);
      renderPicker(data.models);
    } catch (e) {
      $('grp-pull-status').textContent = '';
      throw e;
    }
  },

  'pick-all': () => togglePicked(true),
  'pick-none': () => togglePicked(false),

  'import-picked': async () => {
    const boxes = [...$('grp-picker').querySelectorAll('input[type="checkbox"]')];
    const names = boxes.filter((b) => b.checked).map((b) => b.value);
    if (!names.length) return toast('一个都没勾选', 'err');
    const r = await api('POST', '/admin/api/models/bulk-add', {
      group_id: state.editingGroup, model_names: names, side: $('grp-side').value,
    });
    await refreshConfig();
    $('grp-mcount').textContent = `已录入 ${modelsOfGroup(state.editingGroup).length} 个`;
    renderPicker(boxes.map((b) => b.value));
    toast(`导入了 ${r.added} 个${r.added < names.length ? '（重复的已跳过）' : ''}`, 'ok');
  },

  'new-route': () => openRoute(''),
  'add-candidate': ({ model }) => openRoute(model),

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

function togglePicked(on) {
  for (const box of $('grp-picker').querySelectorAll('label:not([hidden]) input[type="checkbox"]')) {
    box.checked = on;
  }
}

/* ---------------------------------------------------------------- 事件绑定 */

document.addEventListener('click', (ev) => {
  const nav = ev.target.closest('.nav-item');
  if (nav) { showView(nav.dataset.view); return; }

  const seg = ev.target.closest('[data-window]');
  if (seg) { setWindow(seg.dataset.window); return; }

  // 两个分段选择器：data-side 在模型路由上，data-proto 在转发记录上。
  // 记录行用的是 data-log-proto、候选圆片用 data-gid，都不会被这里的 closest 命中
  const sideSeg = ev.target.closest('[data-side]');
  if (sideSeg) { setSide(sideSeg.dataset.side); return; }

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
  run($('route-form').querySelector('[type="submit"]'), saveRoute);
});

$('tier-form').addEventListener('submit', (ev) => {
  ev.preventDefault();
  run($('tier-form').querySelector('[type="submit"]'), saveTiers);
});

// 供应商换了就把分组下拉重填一遍，这三个弹窗都是「先选供应商再选分组」
$('rt-upstream').addEventListener('change', () => {
  fillGroupSelect('rt-group', $('rt-upstream').value);
  markTakenGroups($('rt-model').value.trim());
});

$('tier-upstream').addEventListener('change', () => {
  fillGroupSelect('tier-group', $('tier-upstream').value);
  $('tier-pull-status').textContent = '';
  $('tier-models').innerHTML = '';
});

// Esc 关闭也要走这里，所以刷新放在 close 上而不是「取消」按钮上
$('up-dialog').addEventListener('close', () => {
  state.editing = null;
  run(null, refreshConfig);
});

$('group-dialog').addEventListener('close', () => {
  state.editing = null;
  state.editingGroup = null;
  run(null, refreshConfig);
});

$('route-filter').addEventListener('input', (ev) => {
  state.filter = ev.target.value;
  views.renderRoutes();
});

$('grp-picker-filter').addEventListener('input', (ev) => {
  const kw = ev.target.value.trim().toLowerCase();
  for (const label of $('grp-picker').querySelectorAll('label')) {
    label.hidden = Boolean(kw) && !label.textContent.toLowerCase().includes(kw);
  }
});

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

// 慢轮取统计和记录：数据量大一些，15 秒足够
setInterval(() => {
  if (!ticking()) return;
  run(null, async () => {
    await Promise.all([refreshOverview(), refreshLog()]);
  });
}, 15000);

/* ---------------------------------------------------------------- 启动 */

applyTheme(localStorage.getItem('mg-theme') || 'auto');
$('endpoint').textContent = `${location.origin}/v1`;
views.initLogFollow();   // 「自动跟随新记录」的勾选状态变化时补插攒下的行
for (const b of $('seg-window').children) b.classList.toggle('is-on', b.dataset.window === state.window);

state.side = localStorage.getItem('mg-side') || '';
for (const b of $('seg-side').children) b.classList.toggle('is-on', b.dataset.side === state.side);

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
