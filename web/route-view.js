// AI生成
/* 模型工作台：只派生和展示配置。所有写入仍通过 app 的 data-act 入口。
   选中模型按「名字 + 接口」保存，刷新、筛选和删除后重新校验。 */
import { $, state, esc, fmtInt, fmtSec, fmtLeft, protocolOn, PROTO_LABEL, splitOneM } from './util.js';
import { modelKey as routeKey, orderedCandidates, forwardPath, usable } from './route-model.js';
import { fadeIn, enterStagger, animateDetailSwap, captureRects, flipElements, pulse, reduceMotion, createIndicator, MOTION } from './motion.js';

let selectedRouteKey = null;
let lastFilterSig = null;
let lastDetailKey = null;

export function routeSummary(row, rows = state.routes) {
  const path = row.forward_to ? forwardPath(row, rows) : [];
  const target = row.forward_to ? path?.at(-1) : row;
  const candidates = target ? orderedCandidates(target) : [];
  // active_route_id 是后端给出的下一次请求起点，不能拿保存的首选冒充。
  const next = candidates.find(c => c.route_id === target?.active_route_id) || null;
  const preferred = candidates.find(c => c.route_id === target?.preferred_route_id) || null;
  let label = '可用', tone = 'good';
  if (!next) { label = row.forward_to && !path ? '转发目标不可用' : '无可用候选'; tone = 'warn'; }
  else if (row.forward_to) { label = '模型转发'; tone = 'accent'; }
  else if (preferred && preferred.route_id !== next.route_id) { label = '使用备用起点'; tone = 'warn'; }
  else if (next.cooling_ms > 0) { label = '起点冷却中'; tone = 'warn'; }
  return { path, target, candidates, next, preferred, label, tone };
}

const candidateName = c => c ? `${c.upstream_name}${c.group_name && c.group_name !== '默认' ? ' · ' + c.group_name : ''}` : '无';
const attrs = row => `data-model="${esc(row.model_name)}" data-proto="${esc(row.protocol)}"`;
const tag = (label, tone = '') => `<span class="tag${tone ? ' tag-' + tone : ''}">${esc(label)}</span>`;

function replaceIfChanged(host, html) {
  if (!host || host.__routeHtml === html) return false;
  // 替换轮询更新中的列表时保留当前按钮焦点，键盘操作不会跳回页面顶部。
  const focused = host.contains?.(document.activeElement) ? document.activeElement : null;
  const token = focused?.dataset.focusKey;
  const scroll = host.scrollTop;
  host.innerHTML = html;
  host.__routeHtml = html;
  host.scrollTop = scroll;
  if (token) [...host.querySelectorAll('[data-focus-key]')].find(el => el.dataset.focusKey === token)?.focus({ preventScroll: true });
  return true;
}

/* 选中态不写进列表 HTML：只要它进了 innerHTML，切换选择就会让 replaceIfChanged
   整表重建，蓝条的过渡永远等不到起点。改为渲染后按 key 单独同步类，切换时只切
   类不重建。 */
function syncRouteSelection(host) {
  for (const el of host.querySelectorAll('.route')) {
    const on = el.dataset.focusKey === selectedRouteKey;
    el.classList.toggle('is-selected', on);
    el.setAttribute('aria-pressed', on ? 'true' : 'false');
  }
  moveRouteMarker(host);
}

/* 蓝条是 .route-table 下一条独立的 .route-marker：切完类按选中行的位置把整条滑
   过去。位置取 offsetTop / offsetHeight（相对 .route-table 的 padding box，与 marker
   的绝对定位同一起点，表头高度自然被算进去），不用 getBoundingClientRect：入场的
   stagger / FLIP 会给行加 transform，而 rect 会被 transform 污染，首帧量出来就偏下
   一个身位（刷新后「往下歪」的根因，行升回原位后 marker 却不重量，一直偏着）。offset*
   是布局值，不受 transform / 滚动影响，量到的永远是行的最终位置。
   首帧（进页面后第一次落位）直接落位，不播放滑入；之后交给弹簧内核，连续点选时
   从当前值和速度继续。缺布局信息（测试桩）时藏起来。 */
let markerPlaced = false;
let markerIndicator = null;

function moveRouteMarker(host, { instant = false } = {}) {
  const marker = $('route-marker');
  if (!marker) return;
  if (!markerIndicator) {
    markerIndicator = createIndicator(({ y, h, opacity }) => {
      marker.style.transform = `translateY(${y.toFixed(2)}px)`;
      marker.style.height = `${Math.max(0, h).toFixed(2)}px`;
      marker.style.opacity = opacity.toFixed(3);
    }, {
      y: { ...MOTION.route, value: 0 },
      h: { ...MOTION.size, value: 0 },
      opacity: { ...MOTION.fade, value: 0 },
    });
  }
  const table = marker.parentElement;
  const row = host?.querySelector?.('.route.is-selected');
  const height = row?.offsetHeight;
  if (!table || !row || !height || typeof row.offsetTop !== 'number') {
    markerIndicator.hide({ instant: !markerPlaced });
    markerPlaced = false;   // 没有可见目标时复位，下次重新出现仍直接落位
    return;
  }
  markerIndicator.move(
    { y: row.offsetTop, h: height, opacity: 1 },
    { instant: instant || !markerPlaced },
  );
  markerPlaced = true;
}

/* 页面长度由内容决定，但不允许内容变化把用户的视口收回去：右卡或列表一变短，
   浏览器布局后会把 scrollTop 夹回可用范围，看起来就是「闪回去」。这里用一块底部
   留白把文档兜到「当前滚动位置 + 视口」高，同一次任务里补齐，用户看不到中间态；
   用户往上滑、内容再次变高、或缩到窄屏时，留白按需收回，向下滚不进留白。
   y 必须在改动 DOM 之前抓：替换之后读到的 scrollY 可能已经被夹过了。 */
let padHeight = 0, lastY = 0, padFrame = 0;

function setPad(next) {
  if (next === padHeight) return;
  padHeight = next;
  const pad = $('route-pad');
  if (pad) pad.style.height = next + 'px';
}

function pinScrollExtent(y = window.scrollY) {
  if (!window.matchMedia?.('(min-width: 1101px)').matches) return;
  lastY = y;
  const natural = document.documentElement.scrollHeight - padHeight;
  const next = Math.max(0, y + window.innerHeight - natural);
  setPad(next);
  if (window.scrollY !== y) window.scrollTo(0, y);   // 布局夹走了就夹回来，此时高度已兜住
}

export function initRoutePad() {
  window.addEventListener('scroll', () => {
    if (!padHeight || padFrame) return;
    lastY = window.scrollY;
    padFrame = requestAnimationFrame(() => { padFrame = 0; pinScrollExtent(); });
  }, { passive: true });

  // 改变窗口高度会改变最大滚动距离：按记住的位置重新兜住，不因为缩放窗口闪回去
  window.addEventListener('resize', () => {
    if (padHeight) {
      if (window.matchMedia?.('(min-width: 1101px)').matches) pinScrollExtent(lastY);
      else setPad(0);
    }
    // 缩放 / 字体变化会改行高，蓝条按新行高立刻重落位（无过渡），不留在旧位置
    moveRouteMarker($('route-list'), { instant: true });
  });

  // 字体晚于首帧加载会改行高：就绪后按最终行高把蓝条重落一次
  document.fonts?.ready?.then(() => moveRouteMarker($('route-list'), { instant: true }));
}

export function selectRouteWorkspace(model, protocol) {
  const nextKey = routeKey(model, protocol);
  const changed = selectedRouteKey !== nextKey;
  selectedRouteKey = nextKey;
  renderRouteWorkspace();
  if (changed) {
    const detail = $('route-detail');
    fadeIn(detail);
    animateDetailSwap(detail);
    const selectedRow = $('route-list')?.querySelector('.route.is-selected');
    if (selectedRow) pulse(selectedRow, { duration: 360 });
  }
  if (window.matchMedia?.('(max-width: 1100px)').matches) {
    $('route-detail')?.scrollIntoView({ block: 'start', behavior: reduceMotion() ? 'instant' : 'smooth' });
  }
}

export function renderRouteWorkspace() {
  const host = $('route-list');
  if (!host) return;
  const y = window.scrollY;   // 留白钉死用：必须在任何 DOM 改动之前抓
  const all = state.routes.filter(r => protocolOn(r.protocol));
  const kw = state.filter.trim().toLowerCase();
  const filterSig = `${state.iface}|${kw}`;
  const filterChanged = lastFilterSig !== null && lastFilterSig !== filterSig;
  lastFilterSig = filterSig;

  const list = all.filter(r => (!state.iface || r.protocol === state.iface)
    && (!kw || r.model_name.toLowerCase().includes(kw)));
  $('route-count').textContent = `${list.length}${list.length === all.length ? '' : ' / ' + all.length} 个模型`;
  const attention = all.filter(r => routeSummary(r).tone === 'warn').length;
  if ($('route-summary')) $('route-summary').textContent = `${all.length} 个模型 · ${all.reduce((n, r) => n + r.candidates.length, 0)} 条候选`;
  if ($('route-attention')) {
    $('route-attention').textContent = attention ? `${attention} 个模型需要留意` : all.length ? '每个模型均有请求起点' : '暂无启用接口下的模型';
    $('route-attention').classList.toggle('tone-warn-ink', attention > 0);
  }
  if (!list.some(r => routeKey(r.model_name, r.protocol) === selectedRouteKey)) {
    selectedRouteKey = list.length ? routeKey(list[0].model_name, list[0].protocol) : null;
  }
  const hot = new Map((state.overview?.models || []).map(m => [routeKey(m.model, m.protocol), m]));
  const html = list.map(row => {
    const summary = routeSummary(row);
    const stat = hot.get(routeKey(row.model_name, row.protocol));
    const next = summary.next;
    return `<button type="button" class="route" ${attrs(row)}
      data-act="select-route" data-focus-key="${esc(routeKey(row.model_name, row.protocol))}"
      aria-pressed="false" aria-label="查看 ${esc(row.model_name)} · ${esc(PROTO_LABEL[row.protocol] || row.protocol)} 的路由">
      <span class="route-name"><strong>${esc(row.model_name)}</strong><small>${esc(PROTO_LABEL[row.protocol] || row.protocol)}</small></span>
      <span class="route-destination"><strong>${esc(candidateName(next))}</strong><small>${row.forward_to ? '经 ' + esc(row.forward_to) + ' 转发' : next ? esc(splitOneM(next.remote_model).bare) : '检查候选及启用状态'}</small></span>
      <span class="route-state">${tag(summary.label, summary.tone)}<small>${row.candidates.length} 个候选</small></span>
      <span class="route-usage" title="所选用量时间窗${stat ? ' · P95 ' + fmtSec(stat.p95) : ''}">${stat ? fmtInt(stat.n) + ' 次' : '—'}</span>
    </button>`;
  }).join('');
  const hiddenByProtocol = !all.length && Object.values(state.protocolEnabled).some(on => on === false);
  const emptyTitle = all.length ? '没有匹配的模型' : hiddenByProtocol ? '当前没有可见模型' : '从第一个模型开始';
  const emptyHint = all.length ? '调整搜索词或接口筛选。' : hiddenByProtocol ? '有接口处于关闭状态。已关闭接口的配置仍然保留，可在网关设置中重新启用。' : '先添加上游站点和分组，再创建模型路由。';

  const prevRects = captureRects(host, '.route', (el) => el.dataset.focusKey);
  const changed = replaceIfChanged(host, html || `<div class="empty"><strong>${emptyTitle}</strong><p>${emptyHint}</p>${all.length ? '' : hiddenByProtocol ? '<button class="btn btn-ghost" data-act="go-settings">管理接口开关</button>' : '<button class="btn btn-ghost" data-act="go-upstreams">添加上游站点</button>'}</div>`);
  if (changed) {
    if (filterChanged) {
      enterStagger(host.children, { step: 22, duration: 360, distance: 8 });
    } else if (!prevRects.size) {
      // 首次进入用纯淡入（distance 0）：行不做纵向位移，刚落位的蓝条才不会和行错开半个身位
      enterStagger(host.children, { step: 22, duration: 360, distance: 0 });
    } else {
      flipElements(host, '.route', (el) => el.dataset.focusKey, prevRects, { staggerNew: true });
    }
  }
  syncRouteSelection(host);
  renderRouteDetail(list.find(r => routeKey(r.model_name, r.protocol) === selectedRouteKey));
  pinScrollExtent(y);
}

function renderRouteDetail(row) {
  const host = $('route-detail');
  if (!host) return;
  if (!row) {
    lastDetailKey = null;
    if (replaceIfChanged(host, '<div class="empty">选中一个模型，查看路由详情。</div>')) {
      fadeIn(host);
    }
    return;
  }
  const currentKey = routeKey(row.model_name, row.protocol);
  const sameModel = lastDetailKey === currentKey;
  const prevCandRects = sameModel
    ? captureRects(host, '.candidate', (el) => el.querySelector('[data-rid]')?.dataset.rid)
    : null;
  lastDetailKey = currentKey;

  const s = routeSummary(row);
  const own = orderedCandidates(row);
  const buttons = (action, text, extra = '', cls = 'btn-ghost') => `<button type="button" class="btn btn-sm ${cls}" data-act="${action}" ${attrs(row)} ${extra}>${text}</button>`;
  const head = `<div class="detail-head"><span class="eyebrow">路由详情</span><h2>${esc(row.model_name)}</h2>${tag(PROTO_LABEL[row.protocol] || row.protocol)}${tag(s.label, s.tone)}</div>`;
  const forwarding = row.forward_to ? `<div class="route-forward"><b>全部转发</b><p>${esc(row.model_name)} → ${s.path ? s.path.map(r => esc(r.model_name)).join(' → ') : esc(row.forward_to) + '（不可用）'}</p><div class="action-row">${buttons('fwd-open', '更改目标')}${buttons('fwd-clear', '恢复自己的候选')}</div></div>` : '';
  const policy = Boolean(state.failover?.enabled?.[row.protocol]);
  const reason = row.forward_to ? '下次请求使用最终目标的候选；自己的候选保留。'
    : !s.next ? '没有可用候选，请检查候选配置、供应商和分组是否启用。'
    : s.preferred && s.preferred.route_id !== s.next.route_id
      ? (s.preferred.cooling_ms > 0 ? '首选处于冷却期，按自动换站规则后移。' : '首选暂不可用，使用当前可用候选。')
      : s.next.cooling_ms > 0 ? (policy ? '可用候选均在冷却，仍保留当前尝试起点。' : '自动换站关闭，冷却状态不会改变当前起点。')
        : '新请求从这里开始；进行中的请求继续使用原上游。';
  const facts = `<dl class="route-facts"><div><dt>保存的首选${row.forward_to ? '（目标）' : ''}</dt><dd>${esc(candidateName(s.preferred))}</dd></div><div><dt>下次请求起点</dt><dd class="tone-accent-ink">${esc(candidateName(s.next))}</dd></div><div><dt>失败自动换站</dt><dd>${policy ? '开启' : '关闭'}${buttons('go-settings', '设置')}</dd></div></dl><p class="detail-note">${reason}</p>`;
  const candidates = own.map((c, i) => {
    const { bare, onem } = splitOneM(c.remote_model);
    const enabled = usable(c);
    const current = !row.forward_to && c.route_id === row.active_route_id;
    const preferred = c.route_id === row.preferred_route_id;
    const extra = `data-rid="${c.route_id}"`;
    return `<article class="candidate${current ? ' is-current' : ''}${enabled ? '' : ' is-disabled'}">
      <div class="candidate-heading"><span class="candidate-number">${i + 1}</span><strong>${esc(candidateName(c))}</strong></div>
      <div class="candidate-model mono">${esc(bare)}${onem ? tag('1M', 'accent') : ''}</div>
      <div class="candidate-tags">${preferred ? tag('首选', 'accent') : ''}${current ? tag('下次起点', 'good') : ''}${!enabled ? tag(c.upstream_enabled ? '分组停用' : '站点停用') : ''}${c.cooling_ms > 0 ? tag('冷却 ' + fmtLeft(c.cooling_ms), 'warn') : ''}${row.forward_to ? tag('暂不参与路由') : ''}</div>
      <div class="candidate-actions">${buttons('switch', c.cooling_ms > 0 ? '切换并清除冷却' : preferred && current ? '已设为首选' : '设为首选', `${extra} data-focus-key="switch-${c.route_id}"${!enabled || row.forward_to || (preferred && current && !c.cooling_ms) ? ' disabled' : ''}`)}
        ${buttons('edit-candidate', '编辑', extra + ` data-focus-key="edit-${c.route_id}"`)}
        <span class="grow"></span>${buttons('cand-shift', '↑', `${extra} data-delta="-1" aria-label="候选 ${i + 1} 前移" data-focus-key="up-${c.route_id}"${i === 0 || row.forward_to ? ' disabled' : ''}`)}${buttons('cand-shift', '↓', `${extra} data-delta="1" aria-label="候选 ${i + 1} 后移" data-focus-key="down-${c.route_id}"${i === own.length - 1 || row.forward_to ? ' disabled' : ''}`)}
        ${buttons('del-candidate', '移除', extra, 'btn-danger')}
      </div></article>`;
  }).join('');
  const detailChanged = replaceIfChanged(host, head + forwarding + facts + `<div class="detail-section-head"><h3>候选顺序</h3>${buttons('add-candidate', '＋ 添加')}</div><p class="detail-note">首选先试，其余按顺序尝试；换首选不会改变这里的排序。</p>` + (candidates || '<p class="empty">这个模型没有自己的候选。</p>')
    + `<div class="detail-footer">${!row.forward_to ? buttons('fwd-open', '全部转发到…') : ''}${buttons('rename-model', '重命名')}${buttons('del-model', '删除模型', '', 'btn-danger')}</div>`);

  if (detailChanged && sameModel && prevCandRects?.size) {
    flipElements(host, '.candidate', (el) => el.querySelector('[data-rid]')?.dataset.rid, prevCandRects, { staggerNew: true });
  }
}
