/* 模型工作台：只派生和展示配置。所有写入仍通过 app 的 data-act 入口。
   选中模型按「名字 + 接口」保存，刷新、筛选和删除后重新校验。 */
import { $, state, esc, fmtInt, fmtSec, fmtLeft, protocolOn, PROTO_LABEL, splitOneM } from './util.js';
import { modelKey as routeKey, orderedCandidates, forwardPath, usable } from './canvas-model.js';
import { slideIn } from './motion.js';

let selectedRouteKey = null;

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
  if (!host || host.__routeHtml === html) return;
  // 替换轮询更新中的列表时保留当前按钮焦点，键盘操作不会跳回页面顶部。
  const focused = host.contains?.(document.activeElement) ? document.activeElement : null;
  const token = focused?.dataset.focusKey;
  const scroll = host.scrollTop;
  host.innerHTML = html;
  host.__routeHtml = html;
  host.scrollTop = scroll;
  if (token) [...host.querySelectorAll('[data-focus-key]')].find(el => el.dataset.focusKey === token)?.focus({ preventScroll: true });
}

export function selectRouteWorkspace(model, protocol) {
  const changed = selectedRouteKey !== routeKey(model, protocol);
  selectedRouteKey = routeKey(model, protocol);
  renderRouteWorkspace();
  if (changed) slideIn($('route-detail'));
  if (window.matchMedia?.('(max-width: 1100px)').matches) $('route-detail')?.scrollIntoView({ block: 'start' });
}

export function renderRouteWorkspace() {
  const host = $('route-list');
  if (!host) return;
  const all = state.routes.filter(r => protocolOn(r.protocol));
  const kw = state.filter.trim().toLowerCase();
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
    const active = routeKey(row.model_name, row.protocol) === selectedRouteKey;
    const stat = hot.get(routeKey(row.model_name, row.protocol));
    const next = summary.next;
    return `<button type="button" class="route${active ? ' is-selected' : ''}" ${attrs(row)}
      data-act="select-route" data-focus-key="${esc(routeKey(row.model_name, row.protocol))}"
      aria-pressed="${active}" aria-label="查看 ${esc(row.model_name)} · ${esc(PROTO_LABEL[row.protocol] || row.protocol)} 的路由">
      <span class="route-name"><strong>${esc(row.model_name)}</strong><small>${esc(PROTO_LABEL[row.protocol] || row.protocol)}</small></span>
      <span class="route-destination"><strong>${esc(candidateName(next))}</strong><small>${row.forward_to ? '经 ' + esc(row.forward_to) + ' 转发' : next ? esc(splitOneM(next.remote_model).bare) : '检查候选及启用状态'}</small></span>
      <span class="route-state">${tag(summary.label, summary.tone)}<small>${row.candidates.length} 个候选</small></span>
      <span class="route-usage" title="所选用量时间窗${stat ? ' · P95 ' + fmtSec(stat.p95) : ''}">${stat ? fmtInt(stat.n) + ' 次' : '—'}</span>
    </button>`;
  }).join('');
  const hiddenByProtocol = !all.length && Object.values(state.protocolEnabled).some(on => on === false);
  const emptyTitle = all.length ? '没有匹配的模型' : hiddenByProtocol ? '当前没有可见模型' : '从第一个模型开始';
  const emptyHint = all.length ? '调整搜索词或接口筛选。' : hiddenByProtocol ? '有接口处于关闭状态。已关闭接口的配置仍然保留，可在网关设置中重新启用。' : '先添加上游站点和分组，再创建模型路由。';
  replaceIfChanged(host, html || `<div class="empty"><strong>${emptyTitle}</strong><p>${emptyHint}</p>${all.length ? '' : hiddenByProtocol ? '<button class="btn btn-ghost" data-act="go-settings">管理接口开关</button>' : '<button class="btn btn-ghost" data-act="go-upstreams">添加上游站点</button>'}</div>`);
  renderRouteDetail(list.find(r => routeKey(r.model_name, r.protocol) === selectedRouteKey));
}

function renderRouteDetail(row) {
  const host = $('route-detail');
  if (!host) return;
  if (!row) { replaceIfChanged(host, '<div class="empty">选中一个模型，查看路由详情。</div>'); return; }
  const s = routeSummary(row);
  const own = orderedCandidates(row);
  const buttons = (action, text, extra = '', cls = 'btn-ghost') => `<button type="button" class="btn btn-sm ${cls}" data-act="${action}" ${attrs(row)} ${extra}>${text}</button>`;
  const head = `<div class="detail-head"><span class="eyebrow">路由详情</span><h2>${esc(row.model_name)}</h2>${tag(PROTO_LABEL[row.protocol] || row.protocol)}${tag(s.label, s.tone)}</div>`;
  const forwarding = row.forward_to ? `<div class="route-forward"><b>全部转发</b><p>${esc(row.model_name)} → ${s.path ? s.path.map(r => esc(r.model_name)).join(' → ') : esc(row.forward_to) + '（不可用）'}</p><div class="action-row">${buttons('cv-forward', '更改目标')}${buttons('cv-unforward', '恢复自己的候选')}</div></div>` : '';
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
        <span class="grow"></span>${buttons('cv-move', '↑', `${extra} data-delta="-1" aria-label="候选 ${i + 1} 前移" data-focus-key="up-${c.route_id}"${i === 0 || row.forward_to ? ' disabled' : ''}`)}${buttons('cv-move', '↓', `${extra} data-delta="1" aria-label="候选 ${i + 1} 后移" data-focus-key="down-${c.route_id}"${i === own.length - 1 || row.forward_to ? ' disabled' : ''}`)}
        ${buttons('del-candidate', '移除', extra, 'btn-danger')}
      </div></article>`;
  }).join('');
  replaceIfChanged(host, head + forwarding + facts + `<div class="detail-section-head"><h3>候选顺序</h3>${buttons('add-candidate', '＋ 添加')}</div><p class="detail-note">首选先试，其余按顺序尝试；换首选不会改变这里的排序。</p>` + (candidates || '<p class="empty">这个模型没有自己的候选。</p>')
    + `<div class="detail-footer">${!row.forward_to ? buttons('cv-forward', '全部转发到…') : ''}${buttons('cv-del-model', '删除模型', '', 'btn-danger')}</div>`);
}
