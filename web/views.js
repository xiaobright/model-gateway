/* 四个视图的渲染。约定：
   - 结构性骨架只建一次，之后只更新数值，这样 count-up / FLIP 才有"上一个值"
     可以过渡，不会每次轮询都从 0 重新滚一遍
   - 所有列表更新都走增量：无变化就不碰 DOM，避免动画乱闪 */

import {
  $, state, esc, fmtInt, fmtTokens, fmtBytes, fmtDur, fmtSec,
  modelsOfGroup, modelsOfUpstream, splitOneM, PROTO_LABEL, PROTO_PATH,
} from './util.js';
import { countUp, createOdometer, initSpotlightAndTilt, enterStagger, slideIn, pulse, reduceMotion, flow } from './motion.js';
import { sparkline, donut, areaChart, barRow } from './charts.js';

/* 只建一次的图表与滚轮实例 */
let ring = null;
let chart = null;
const odometers = {};

const EMPTY = '<div class="empty">暂无数据</div>';

/* ================================================================ KPI */

function kpiSkeleton() {
  const host = $('kpis');
  if (host.childElementCount) return;
  host.innerHTML = `
    <div class="kpi">
      <div class="kpi-label">进行中的请求</div>
      <div class="kpi-value"><span class="live-dot" data-live-dot></span><span data-num="live">0</span></div>
      <div class="kpi-note" data-note="live">等待请求</div>
      <div class="kpi-spark" data-spark="live"></div>
    </div>
    <div class="kpi">
      <div class="kpi-label">累计转发</div>
      <div class="kpi-value"><span data-num="req">0</span><span class="unit">次</span></div>
      <div class="kpi-note" data-note="req"></div>
      <div class="kpi-spark" data-spark="req"></div>
    </div>
    <div class="kpi">
      <div class="kpi-label">P95 延迟</div>
      <div class="kpi-value"><span data-num="p95">0</span></div>
      <div class="kpi-note" data-note="p95"></div>
      <div class="kpi-spark" data-spark="p95"></div>
    </div>
    <div class="kpi kpi-ring">
      <div class="kpi-main">
        <div class="kpi-label">缓存命中</div>
        <div class="kpi-value"><span data-num="hit">0</span><span class="unit">%</span></div>
        <div class="kpi-note" data-note="hit"></div>
      </div>
      <div class="kpi-ring-host" data-ring></div>
    </div>`;
  ring = donut(0);
  $('kpis').querySelector('[data-ring]').append(ring.node);

  odometers.live = createOdometer($('kpis').querySelector('[data-num="live"]'));
  odometers.req = createOdometer($('kpis').querySelector('[data-num="req"]'));
  odometers.p95 = createOdometer($('kpis').querySelector('[data-num="p95"]'));
  odometers.hit = createOdometer($('kpis').querySelector('[data-num="hit"]'));
  initSpotlightAndTilt($('kpis'));
}

export function renderKpis() {
  kpiSkeleton();
  // 累计值和 P95 只从 overview 来（/stats 不带分位数），live 两个接口都有
  const t = (state.overview && state.overview.totals) || state.stats || {};
  const live = (state.stats && state.stats.live)
    || (state.overview && state.overview.live) || { requests: 0, streams: 0 };
  const ov = state.overview;
  const points = (ov && ov.series && ov.series.points) || [];

  const setNum = (key, value, format) => {
    const str = format ? format(value) : String(value);
    if (odometers[key]) {
      odometers[key].set(str);
    } else {
      const el = $('kpis').querySelector(`[data-num="${key}"]`);
      if (el) countUp(el, value, { format });
    }
  };
  const setNote = (key, html) => {
    const el = $('kpis').querySelector(`[data-note="${key}"]`);
    if (el) el.innerHTML = html;
  };

  setNum('live', live.requests, (v) => String(Math.round(v)));
  setNote('live', live.streams
    ? `其中 <b>${live.streams}</b> 条是流式`
    : (live.requests ? '均非流式' : '空闲'));
  const dot = $('kpis').querySelector('[data-live-dot]');
  if (dot) dot.classList.toggle('is-live', live.requests > 0);

  setNum('req', t.requests || 0, (v) => fmtInt(Math.round(v)));
  setNote('req', `入 ${fmtTokens(t.input_tokens)} · 出 ${fmtTokens(t.output_tokens)} tokens`);

  setNum('p95', (t.p95 || 0) / 1000, (v) => v.toFixed(1) + 's');
  setNote('p95', '最近 2000 条请求的耗时分位');

  const hit = Math.round((t.cache_hit_rate || 0) * 1000) / 10;
  setNum('hit', hit, (v) => v.toFixed(1));
  setNote('hit', `${fmtTokens(t.cached_tokens)} / ${fmtTokens(t.input_tokens)} 输入 tokens`);
  ring.set(hit);

  // 前两格的 sparkline 用时间线数据：一个看近期忙闲，一个看累计趋势的切线
  const fillSpark = (key, values, color) => {
    const host = $('kpis').querySelector(`[data-spark="${key}"]`);
    if (!host) return;
    host.innerHTML = '';
    host.append(sparkline(values, { stroke: color }));
  };
  fillSpark('live', points.map((p) => p.n), 'var(--accent)');
  fillSpark('req', points.map((p) => p.n), 'var(--accent)');
  fillSpark('p95', points.map((p) => (p.n ? p.to / p.n : 0)), 'var(--warn)');
}

/* ================================================================ 概览：时间线 / 健康 / 热度 */

export function renderSeries() {
  const host = $('chart-series');
  const ov = state.overview;
  const points = (ov && ov.series && ov.series.points) || [];
  const bucket = (ov && ov.series && ov.series.bucket) || 60;

  $('series-legend').innerHTML = `
    <span class="legend-item"><i class="sw sw-ok"></i>请求数</span>
    <span class="legend-item"><i class="sw sw-err"></i>其中失败</span>
    <span class="legend-item"><i class="sw sw-tok"></i>输出 tokens</span>`;

  if (!points.length || !points.some((p) => p.n > 0)) {
    const wider = { '1h': '24 小时', '24h': '7 天' }[state.window];
    host.innerHTML = `<div class="empty">这个时间窗内没有转发记录${
      wider ? `<br><span class="dim">换成「${wider}」看看</span>` : ''
    }</div>`;
    chart = null;
    return;
  }
  if (!chart || !host.contains(chart.node)) {
    host.innerHTML = '';
    chart = areaChart(points, bucket);
    host.append(chart.node);
  } else {
    chart.update(points, bucket);
  }
}

function toneOf(rate) {
  if (rate >= 0.95) return 'good';
  if (rate >= 0.8) return 'warn';
  return 'crit';
}

export function renderHealth() {
  const host = $('health-list');
  const rows = (state.overview && state.overview.upstreams) || [];
  $('health-count').textContent = rows.length ? `${rows.length} 个` : '';
  if (!rows.length) { host.innerHTML = EMPTY; return; }

  const next = rows.map((u) => {
    const pct = Math.round(u.ok_rate * 1000) / 10;
    const tone = toneOf(u.ok_rate);
    return `<div class="health-row">
      <span class="h-name" title="${esc(u.name)}">${esc(u.name)}</span>
      <span class="h-bar"><i class="tone-${tone}" style="width:${Math.max(2, u.ok_rate * 100)}%"></i></span>
      <span class="h-num tone-${tone}-ink">${pct}%</span>
      <span class="h-p95 dim">P95 ${fmtSec(u.p95)}</span>
      <span class="h-n dim">${fmtInt(u.n)} 次</span>
    </div>`;
  }).join('');
  host.innerHTML = next;
  enterStagger(host.children);
}

export function renderHot() {
  const host = $('hot-list');
  const rows = (state.overview && state.overview.models) || [];
  $('hot-count').textContent = rows.length ? `Top ${rows.length}` : '';
  if (!rows.length) { host.innerHTML = EMPTY; return; }

  const max = Math.max(...rows.map((m) => m.n), 1);

  // 记住旧宽度，重建后先把新条拉回旧宽度再放开，就有"此消彼长"的形变
  const prev = new Map();
  for (const el of host.children) {
    const fill = el.querySelector('.bar-fill');
    if (fill) prev.set(el.dataset.key, fill.style.width);
  }

  const frag = document.createDocumentFragment();
  for (const m of rows) {
    const row = barRow({
      label: m.model,
      value: m.n,
      max,
      badge: fmtInt(m.n),
      tone: m.bad / Math.max(1, m.n) > 0.2 ? 'crit' : 'accent',
      note: fmtSec(m.p95),
    });
    row.dataset.key = m.model;
    frag.append(row);
  }
  host.innerHTML = '';
  host.append(frag);

  if (!reduceMotion() && prev.size) {
    const pending = [...host.children].map((row) => {
      const fill = row.querySelector('.bar-fill');
      const target = fill.style.width;
      const old = prev.get(row.dataset.key);
      if (old) { fill.style.transition = 'none'; fill.style.width = old; }
      return { fill, target, animated: Boolean(old) };
    });
    requestAnimationFrame(() => {
      for (const item of pending) {
        if (!item.animated) continue;
        item.fill.style.transition = '';
        item.fill.style.width = item.target;
      }
    });
  }
  enterStagger(host.children, { step: 22 });
}

/* ================================================================ 模型路由 */

/* 候选挂在**分组**上。供应商只有一个分组时圆片只写供应商名，多个才写「供应商 · 分组」，
   否则满屏都是「· 默认」。 */
function multiGroupIds() {
  return new Set(state.upstreams.filter((u) => (u.groups || []).length > 1).map((u) => u.id));
}

function chipHtml(model, c, showGroup) {
  const live = c.upstream_enabled && c.group_enabled;
  const cls = ['chip', c.is_active ? 'chip-on' : '', live ? '' : 'chip-off'].filter(Boolean).join(' ');
  const label = showGroup ? `${c.upstream_name} · ${c.group_name}` : c.upstream_name;
  const { bare, onem } = splitOneM(c.remote_model);
  const remote = bare && bare !== model ? ` <span class="remote">${esc(bare)}</span>` : '';
  const wide = onem ? ' <span class="tag tag-accent">1M</span>' : '';
  const off = live ? ''
    : ` <span class="tag">${c.upstream_enabled ? '分组停用' : '停用'}</span>`;
  const tip = c.is_active ? '当前生效的分组' : `切到 ${label}`;
  return `<span class="${cls}" data-gid="${c.group_id}">
    <button type="button" class="chip-label" data-act="switch" data-model="${esc(model)}"
            data-gid="${c.group_id}" title="${esc(tip)}">${esc(label)}${remote}${wide}${off}</button>
    <button type="button" class="chip-e" data-act="edit-candidate" data-model="${esc(model)}"
            data-gid="${c.group_id}" title="改上游真名 / 1M">✎</button>
    <button type="button" class="chip-x" data-act="del-candidate" data-model="${esc(model)}"
            data-gid="${c.group_id}" title="从这个分组移除该模型">✕</button>
  </span>`;
}

const EMPTY_BY_IFACE = {
  anthropic: 'Anthropic 接口下还没有模型。先在「上游站点」给某个站加一个 Anthropic 分组'
    + '（填 Claude Code 那把 key），拉取模型列表导入，或者点右上角「新增模型」自己起名字。',
  openai: 'OpenAI 接口下还没有模型。去「上游站点」展开某个分组，用「拉取模型列表」导入。',
};

export function renderRoutes() {
  const kw = state.filter.trim().toLowerCase();
  const iface = state.iface;
  const byIface = iface ? state.routes.filter((r) => r.protocol === iface) : state.routes;
  const list = kw ? byIface.filter((r) => r.model_name.toLowerCase().includes(kw)) : byIface;
  const total = state.routes.length;
  $('route-count').textContent = (kw || iface)
    ? `${list.length} / ${total} 个模型`
    : `${total} 个模型`;

  if (!total) {
    $('route-list').innerHTML =
      '<div class="empty">还没有模型。先在「上游站点」加一个供应商，给它建一个分组（选接口 + 填 key），'
      + '再用「拉取模型列表」导入，或点右上角「新增模型」。</div>';
    return;
  }
  if (!list.length) {
    $('route-list').innerHTML = `<div class="empty">${
      kw ? '没有匹配的模型名' : (EMPTY_BY_IFACE[iface] || '这个接口下还没有模型')
    }</div>`;
    return;
  }

  // 请求次数来自概览统计，用来回答"这个模型到底有没有在用"
  const hot = new Map(((state.overview && state.overview.models) || []).map((m) => [m.model, m]));
  const multi = multiGroupIds();

  $('route-list').innerHTML = list.map((g) => {
    const dead = g.active_group_id === null
      ? ' <span class="tag tag-warn"><span class="dot dot-warn"></span>无可用上游</span>' : '';
    // 「全部」视图里两种接口混在一起，得标出来谁是谁
    const ifaceTag = !iface && g.protocol
      ? ` <span class="tag${g.protocol === 'anthropic' ? ' tag-accent' : ''}"`
        + ` title="在 ${esc(PROTO_PATH[g.protocol] || '')} 下暴露">${PROTO_LABEL[g.protocol]}</span>` : '';
    const stat = hot.get(g.model_name);
    const usage = stat
      ? `<span class="route-usage" title="最近 2000 条里的请求数 · P95 ${fmtSec(stat.p95)}">${fmtInt(stat.n)} 次</span>`
      : '';
    const chips = g.candidates
      .map((c) => chipHtml(g.model_name, c, multi.has(c.upstream_id))).join('');
    return `<div class="route" data-model="${esc(g.model_name)}">
      <div class="route-name">${esc(g.model_name)}${ifaceTag}${dead}</div>
      <div class="route-cands">${chips}</div>
      <div class="route-side">${usage}</div>
      <div class="route-actions">
        <button class="btn btn-ghost btn-sm" data-act="add-candidate" data-model="${esc(g.model_name)}">+ 候选</button>
        <button class="btn btn-danger btn-sm" data-act="del-model" data-model="${esc(g.model_name)}">删除</button>
      </div>
    </div>`;
  }).join('');
}

/* ================================================================ 上游站点 */

/* 分组声明的是「这把 key 走哪种接口」，能不能真的通是另一件事 —— 只能看实际跑过的请求。
   这一列就是干这个的：每种接口跑过几次 + 按成功率上色。 */
function protoTags(health) {
  const by = (health && health.by_protocol) || {};
  const keys = Object.keys(by).sort();
  if (!keys.length) return '<span class="dim">—</span>';
  return keys.map((p) => {
    const s = by[p];
    const pct = Math.round(s.ok_rate * 1000) / 10;
    const label = PROTO_LABEL[p] || p;
    return `<span class="tag tag-${toneOf(s.ok_rate)}" title="${esc(label)} 跑过 ${fmtInt(s.n)} 次，成功率 ${pct}%">`
      + `${esc(label)} ${fmtInt(s.n)}</span>`;
  }).join(' ');
}

/** 供应商支持哪几种接口，是它下面分组的接口去重（后端算好放在 supports 里） */
function ifaceTags(u) {
  const marks = u.supports || [];
  if (!marks.length) return '<span class="tag tag-warn">还没有分组</span>';
  return marks.map((p) =>
    `<span class="tag${p === 'anthropic' ? ' tag-accent' : ''}">${PROTO_LABEL[p] || p}</span>`).join(' ');
}

const maskKey = (key) => (key ? `${esc(key.slice(0, 6))}… ${key.length}` : '<span class="dim">透传客户端</span>');

function modelTags(names) {
  if (!names.length) return '<span class="dim">未录入</span>';
  // 只列两个：这一列不宽，三个标签会各占一行把行高撑起来
  return names.slice(0, 2).map((n) => `<span class="tag mono">${esc(n)}</span>`).join(' ')
    + (names.length > 2 ? ` <span class="tag">+${names.length - 2}</span>` : '');
}

function groupRow(u, g) {
  const names = modelsOfGroup(g.id);
  const tag = `<span class="tag${g.protocol === 'anthropic' ? ' tag-accent' : ''}"`
    + ` title="${esc(PROTO_PATH[g.protocol] || '')}">${PROTO_LABEL[g.protocol] || '?'}</span>`;
  return `<tr class="grp-row">
    <td class="grp-name">${esc(g.name)} ${tag}${g.enabled ? '' : ' <span class="tag">停用</span>'}</td>
    <td class="mono dim nowrap">${maskKey(g.api_key)}</td>
    <td>${modelTags(names)}</td>
    <td colspan="2" class="dim nowrap">${names.length} 个模型</td>
    <td><input type="checkbox" class="switch" ${g.enabled ? 'checked' : ''}
               data-act="toggle-group" data-gid="${g.id}"
               aria-label="启用分组 ${esc(g.name)}"></td>
    <td class="nowrap">
      <button class="btn btn-ghost btn-sm" data-act="edit-group" data-gid="${g.id}">编辑</button>
      <button class="btn btn-danger btn-sm" data-act="del-group" data-gid="${g.id}">删除</button>
    </td></tr>`;
}

export function renderUpstreams() {
  const list = state.upstreams;
  $('upstream-count').textContent = `${list.length} 个`;
  const on = list.filter((u) => u.enabled).length;
  $('badge-upstreams').textContent = `${on}/${list.length}`;

  if (!list.length) {
    $('upstream-body').innerHTML =
      '<tr><td colspan="7" class="empty">还没有供应商，点右上角「添加供应商」开始</td></tr>';
    return;
  }

  const health = new Map(((state.overview && state.overview.upstreams) || []).map((u) => [u.name, u]));
  const rows = [];

  for (const u of list) {
    const groups = u.groups || [];
    const open = state.openUpstreams.has(u.id);
    const h = health.get(u.name);
    const stat = h
      ? `<span class="tone-${toneOf(h.ok_rate)}-ink">${Math.round(h.ok_rate * 1000) / 10}%</span>`
        + `<span class="dim"> · ${fmtSec(h.p95)}</span>`
      : '<span class="dim">—</span>';
    rows.push(`<tr>
      <td>
        <button type="button" class="tw" data-act="toggle-groups" data-uid="${u.id}"
                aria-expanded="${open}" title="展开 / 收起分组">${open ? '▾' : '▸'}</button>
        ${esc(u.name)}${u.enabled ? '' : ' <span class="tag"><span class="dot dot-off"></span>停用</span>'}
        <div class="up-sub">${ifaceTags(u)}<span class="tag">${groups.length} 组</span></div>
      </td>
      <td class="mono dim truncate" title="${esc(u.base_url)}">${esc(u.base_url)}</td>
      <td>${modelTags(modelsOfUpstream(u.id))}</td>
      <td class="nowrap">${protoTags(h)}</td>
      <td class="num nowrap">${stat}</td>
      <td><input type="checkbox" class="switch" ${u.enabled ? 'checked' : ''}
                 data-act="toggle-upstream" data-uid="${u.id}"
                 aria-label="启用 ${esc(u.name)}"></td>
      <td class="nowrap">
        <button class="btn btn-ghost btn-sm" data-act="edit-upstream" data-uid="${u.id}">编辑</button>
        <button class="btn btn-danger btn-sm" data-act="del-upstream" data-uid="${u.id}">删除</button>
      </td></tr>`);
    if (!open) continue;
    for (const g of groups) rows.push(groupRow(u, g));
    // 一把 key 两种接口都能用的站不少，而接口是分组的属性，所以给个一键复制
    const only = groups.length === 1 ? groups[0] : null;
    const other = only && only.protocol === 'anthropic' ? 'openai' : 'anthropic';
    const clone = only && only.api_key
      ? `<button class="btn btn-ghost btn-sm" data-act="clone-group" data-gid="${only.id}">`
        + `复制这把 key 到 ${PROTO_LABEL[other]}</button>` : '';
    rows.push(`<tr class="grp-row"><td colspan="7">
      <button class="btn btn-ghost btn-sm" data-act="new-group" data-uid="${u.id}">＋ 添加分组</button>
      ${clone}
      <span class="dim" style="font-size:12px;margin-left:8px">${groups.length
        ? '同一个站的另一把 key，或者另一种接口' : '这个供应商还没有分组，先加一个（选接口 + 填 key）'}</span>
    </td></tr>`);
  }
  $('upstream-body').innerHTML = rows.join('');
}

/* ================================================================ 转发记录 */

function statusTag(status) {
  const kind = status >= 500 || status === 401 || status === 403 ? 'crit'
    : status >= 400 ? 'warn' : status >= 300 ? '' : 'good';
  const cls = kind ? `tag tag-${kind}` : 'tag';
  return `<span class="${cls}">${kind ? `<span class="dot dot-${kind}"></span>` : ''}${status}</span>`;
}

const NOTE_LABEL = {
  truncated: ['warn', '流被截断'],
  connect_failed: ['crit', '连不上'],
  upstream_abort: ['crit', '上游断流'],
  client_abort: ['', '客户端断开'],
};

function noteTag(note) {
  if (!note || note === 'ok') return '<span class="dim">—</span>';
  const [kind, label] = NOTE_LABEL[note] || ['', note];
  return `<span class="tag${kind ? ' tag-' + kind : ''}">${esc(label)}</span>`;
}

function logRow(r) {
  const tokens = r.input_tokens === null
    ? '<span class="dim">—</span>'
    : `${fmtInt(r.input_tokens)} / ${fmtInt(r.output_tokens ?? 0)} / ${fmtInt(r.cached_tokens ?? 0)}`;
  // 档位名映射到上游真名时把真名跟在后面：不然从记录里看不出实际跑的是哪个模型
  const remote = r.remote_model ? ` <span class="dim">→ ${esc(r.remote_model)}</span>` : '';
  const modelTip = r.remote_model ? `${r.model} → ${r.remote_model}` : r.model;
  const proto = r.protocol || '';
  const protoCell = proto
    ? `<span class="tag${proto === 'anthropic' ? ' tag-accent' : ''}">${esc(PROTO_LABEL[proto] || proto)}</span>`
    : '<span class="dim">—</span>';
  // 分组名只在不是「默认」时才写出来，不然每一行都拖一条没信息量的尾巴
  const grp = r.group_name && r.group_name !== '默认'
    ? ` <span class="dim">· ${esc(r.group_name)}</span>` : '';
  const upTip = r.group_name ? `${r.upstream} · ${r.group_name}` : r.upstream;
  return `<tr data-id="${r.id}" data-log-proto="${esc(proto)}">
      <td class="dim nowrap" title="${esc(r.ts || '')}">${esc((r.ts || '').slice(5))}</td>
      <td class="truncate" title="${esc(r.client)}">${esc(r.client)}</td>
      <td class="nowrap">${protoCell}</td>
      <td class="mono truncate" title="${esc(modelTip)}">${esc(r.model)}${remote}${r.stream ? ' <span class="tag">流</span>' : ''}</td>
      <td class="truncate" title="${esc(upTip)}">${esc(r.upstream)}${grp}</td>
      <td>${statusTag(r.status)}</td>
      <td class="num mono nowrap">${tokens}</td>
      <td class="num dim nowrap" title="上行 ${fmtBytes(r.req_bytes)} · 下行 ${fmtBytes(r.resp_bytes)}">${fmtDur(r.duration_ms)}</td>
      <td>${noteTag(r.note)}</td>
    </tr>`;
}

/**
 * 增量渲染：只把没见过的 id 插到顶部，已有的行不动。
 * 这样轮询再快也不会让表格闪，新行才有"飞进来"的对比。
 */
let lastRows = [];
// 关掉「自动跟随」时新记录先攒在这里。用户可能正在往下翻旧记录，
// 这时候硬插一行会把他的视口顶走，先记账、勾选后一次性补上更省心。
let pending = [];
const PENDING_CAP = 200;

const followOn = () => {
  const box = $('log-follow');
  return box ? box.checked : true;
};

function setFollowHint() {
  const hint = $('follow-hint');
  if (!hint) return;
  hint.textContent = pending.length ? `自动跟随新记录（+${pending.length}）` : '自动跟随新记录';
}

/** 勾选状态变化时调用：把攒下的行一次性补齐 */
function flushPending() {
  if (!pending.length) return;
  const list = pending.sort((a, b) => a.id - b.id);
  pending = [];
  setFollowHint();

  // 一次补太多就别逐条播动画了，二三十条飞进来只会糊成一片
  if (list.length > 20) {
    const body = $('log-body');
    body.innerHTML = lastRows.map(logRow).join('');
    enterStagger([...body.children].slice(0, 12), { duration: 260 });
    applyLogFilter();
    return;
  }
  insertRows(list, lastRows.length);
}

/** 把 list 按 id 从大到小 prepend 进去，再按 cap 裁掉尾部 */
function insertRows(list, cap) {
  const body = $('log-body');
  const known = state.logIds;
  const tmp = document.createElement('tbody');
  tmp.innerHTML = list.map(logRow).join('');
  const added = [...tmp.children].reverse();   // 大 id 先插，最后大 id 在最上面
  for (const tr of added) {
    body.prepend(tr);
    known.add(Number(tr.dataset.id));
    slideIn(tr);
    pulse(tr);
  }
  while (body.children.length > cap) {
    const last = body.lastElementChild;
    if (last.dataset.id) known.delete(Number(last.dataset.id));
    last.remove();
  }
  applyLogFilter();
}

/* 协议筛选做成「只是把行藏起来」：增量渲染那套 diff（logIds / pending）完全不用知道
   筛选的存在，切档位也不会重建表格、不会重播动画。每次插行之后重跑一遍即可。 */
export function applyLogFilter() {
  const want = state.proto;
  let shown = 0;
  for (const tr of $('log-body').children) {
    if (!tr.dataset.id) continue;                     // 空状态那一行不参与
    const hide = Boolean(want) && tr.dataset.logProto !== want;
    tr.hidden = hide;
    if (!hide) shown += 1;
  }
  const total = lastRows.length;
  $('log-count').textContent = total
    ? (want ? `${shown} / ${total} 条` : `最近 ${total} 条`)
    : '';
}

export function initLogFollow() {
  const box = $('log-follow');
  if (!box) return;
  box.addEventListener('change', () => {
    if (box.checked) flushPending();
  });
  setFollowHint();
}

export function renderLog(rows) {
  paintLog(rows);
  applyLogFilter();
}

function paintLog(rows) {
  const body = $('log-body');
  const known = state.logIds;
  lastRows = rows;

  if (!rows.length) {
    body.innerHTML = '<tr><td colspan="9" class="empty">还没有转发记录</td></tr>';
    known.clear();
    pending = [];
    setFollowHint();
    return;
  }

  const incoming = rows.filter((r) => !known.has(r.id)).sort((a, b) => a.id - b.id);
  if (!known.size || body.querySelector('.empty')) {
    body.innerHTML = rows.map(logRow).join('');
    known.clear();
    for (const r of rows) known.add(r.id);
    pending = [];
    setFollowHint();
    return;
  }
  if (!incoming.length) return;

  if (!followOn()) {
    // 先记进 known，否则下一轮还会把它们当新行重复攒一遍
    for (const r of incoming) known.add(r.id);
    pending = pending.concat(incoming).slice(-PENDING_CAP);
    setFollowHint();
    return;
  }

  insertRows(incoming, rows.length);
}

/* ================================================================ 侧栏活跃指示 */

export function renderLive() {
  const live = (state.stats && state.stats.live)
    || (state.overview && state.overview.live) || { requests: 0, streams: 0 };
  $('live-count').textContent = String(live.requests);
  $('live-sub').textContent = live.streams
    ? `${live.streams} 条流式`
    : (live.requests ? '非流式' : '空闲');
  $('live-dot').classList.toggle('is-live', live.requests > 0);
}

/** 只更新"进行中"那一格。快轮每 3 秒跑一次，不能把整个 KPI 重新滚一遍。 */
export function updateKpiLive() {
  const live = (state.stats && state.stats.live)
    || (state.overview && state.overview.live) || { requests: 0, streams: 0 };
  if (odometers.live) {
    odometers.live.set(String(live.requests));
  } else {
    const el = $('kpis').querySelector('[data-num="live"]');
    if (el) countUp(el, live.requests, { duration: 420, format: (v) => String(Math.round(v)) });
  }
  const note = $('kpis').querySelector('[data-note="live"]');
  if (note) {
    note.innerHTML = live.streams
      ? `其中 <b>${live.streams}</b> 条是流式`
      : (live.requests ? '均非流式' : '空闲');
  }
  const dot = $('kpis').querySelector('[data-live-dot]');
  if (dot) dot.classList.toggle('is-live', live.requests > 0);
}

/* 切换分组后：把"流量改道"这件事演出来 */
export function afterSwitch(model, fromGid, toGid) {
  const row = $('route-list').querySelector(`.route[data-model="${CSS.escape(model)}"]`);
  if (!row) return;
  flow(row.querySelector(`.chip[data-gid="${fromGid}"]`), row.querySelector(`.chip[data-gid="${toGid}"]`));
}

/**
 * 时间窗切换：不同窗口的桶数不一样（61 / 25 / 8），路径没法逐点插值，
 * 所以走「先横向淡出、换数据、再横向淡入」，进出用不同曲线。
 *
 * 淡出那条是 fill: forwards，淡入必须真正接管之后才能撤掉它 —— 否则淡入一结束，
 * 淡出的 forwards 又把 opacity 按回 0，图表就是「闪两下然后消失」，而且再也不回来。
 */
export function animateWindowChange(mutate) {
  const host = $('chart-series');
  if (!host || reduceMotion() || typeof host.animate !== 'function') { mutate(); return; }

  // 连着换时间窗时上一轮可能还没跑完，先清掉，免得两条 forwards 叠着把图表按死
  for (const anim of host.getAnimations()) anim.cancel();

  const out = host.animate(
    [{ opacity: 1, transform: 'none' }, { opacity: 0, transform: 'translateX(-14px)' }],
    { duration: 150, easing: 'ease-in', fill: 'forwards' },
  );
  out.finished.catch(() => {}).then(() => {
    mutate();
    const back = host.animate(
      [{ opacity: 0, transform: 'translateX(14px)' }, { opacity: 1, transform: 'none' }],
      { duration: 360, easing: 'cubic-bezier(.2,.9,.2,1)', fill: 'backwards' },
    );
    back.ready.catch(() => {}).then(() => out.cancel());
  });
}
