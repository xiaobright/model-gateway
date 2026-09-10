/* 四个视图的渲染。约定：
   - 结构性骨架只建一次，之后只更新数值，这样 count-up / FLIP 才有"上一个值"
     可以过渡，不会每次轮询都从 0 重新滚一遍
   - 所有列表更新都走增量：无变化就不碰 DOM，避免动画乱闪 */

import {
  $, state, esc, fmtInt, fmtTokens, fmtBytes, fmtDur, fmtSec, fmtLeft, protocolOn,
  catalogOfGroup, catalogOfUpstream, groupLabel, splitOneM, PROTO_LABEL, PROTO_PATH, PROTOCOLS,
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
      <div class="kpi-label">转发次数</div>
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
  // 「救回」= 第二次以上的尝试还成了。只在真发生过时才占这行的位置
  setNote('req', `入 ${fmtTokens(t.input_tokens)} · 出 ${fmtTokens(t.output_tokens)} tokens`
    + (t.saved ? ` · 救回 <b>${fmtInt(t.saved)}</b>` : ''));

  setNum('p95', (t.p95 || 0) / 1000, (v) => v.toFixed(1) + 's');
  setNote('p95', '所选时间窗内的耗时分位');

  const hit = Math.round((t.cache_hit_rate || 0) * 1000) / 10;
  setNum('hit', hit, (v) => v.toFixed(1));
  setNote('hit', `${fmtTokens(t.cached_tokens)} / ${fmtTokens(t.context_tokens ?? t.input_tokens)} 输入 tokens`);
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

function cloneButtons(group) {
  if (!group || !group.api_key) return '';
  const targets = PROTOCOLS.filter((p) => p !== group.protocol);
  return targets.map((p) =>
    `<button type="button" class="btn btn-ghost btn-sm" data-act="clone-group"`
      + ` data-gid="${group.id}" data-protocol="${esc(p)}">`
      + `复制这把 key 到 ${esc(PROTO_LABEL[p] || p)}</button>`
  ).join(' ');
}

/* 一个分组下可以挂同一个模型的好几条候选（各指一个不同的上游真名），这时候光写
   「供应商 · 分组」两个圆片长得一模一样，所以 showRemote 会强制把真名显示出来。 */
function chipHtml(model, c, showGroup, showRemote, activeRouteId) {
  const live = c.upstream_enabled && c.group_enabled;
  const cool = c.cooling_ms > 0;
  const active = c.route_id === activeRouteId;
  const cls = ['chip', active ? 'chip-on' : '', live ? '' : 'chip-off',
    cool ? 'chip-cool' : ''].filter(Boolean).join(' ');
  const label = showGroup ? `${c.upstream_name} · ${c.group_name}` : c.upstream_name;
  const { bare, onem } = splitOneM(c.remote_model);
  const named = bare && (showRemote || bare !== model);
  const remote = named ? ` <span class="remote">${esc(bare)}</span>` : '';
  const wide = onem ? ' <span class="tag tag-accent">1M</span>' : '';
  const off = live ? ''
    : ` <span class="tag">${c.upstream_enabled ? '分组停用' : '停用'}</span>`;
  // 冷却 = 它连着失败过，自动降级这段时间内会跳过它（手动点它照样能切过去）。
  // 断路器按分组记，所以同分组的几条候选会一起显示冷却
  const cd = cool
    ? ` <span class="tag tag-warn" title="连续失败 ${c.fails} 次，冷却期内自动降级会跳过它">`
      + `冷却 ${fmtLeft(c.cooling_ms)}</span>` : '';
  const full = named ? `${label} · ${bare}` : label;
  const tip = active ? '当前实际使用的候选' : (c.is_active ? '保存的首选候选' : `切到 ${full}`);
  return `<span class="${cls}" data-rid="${c.route_id}">
    <button type="button" class="chip-label" data-act="switch" data-model="${esc(model)}"
            data-rid="${c.route_id}" title="${esc(tip)}">${esc(label)}${remote}${wide}${cd}${off}</button>
    <button type="button" class="chip-e" data-act="edit-candidate" data-model="${esc(model)}"
            data-rid="${c.route_id}" title="改上游真名 / 1M / 尝试顺序">✎</button>
    <button type="button" class="chip-x" data-act="del-candidate" data-model="${esc(model)}"
            data-rid="${c.route_id}" title="移除这一条候选">✕</button>
  </span>`;
}


/* 自动降级开关。按接口分开，所以在模型路由卡上跟着当前的接口筛选走：筛了哪个就只显示
   那个的开关，「全部」时两个都显示 —— 一个开关代表两种接口会让人以为 GPT 侧也在自动换站。
   实时页那份永远两个都给：那页没有接口筛选。
   不复用 .checkline：它那条 input[type=checkbox]{width:auto} 选择器权重更高，
   会把开关压成 0 宽、只剩个滑块糊在文字上。 */
function failoverSwitches(list) {
  const on = (state.failover && state.failover.enabled) || {};
  return list.map((p) => `<label class="fo-item" title="打不通就按候选顺序换下一个">
      <input type="checkbox" class="switch" ${on[p] ? 'checked' : ''}
             data-act="toggle-failover" data-fo="${p}">
      <span>${list.length > 1 ? PROTO_LABEL[p] + ' ' : '自动'}降级</span>
    </label>`).join('');
}

function failoverBox() {
  const box = $('failover-box');
  if (box) box.innerHTML = failoverSwitches(state.iface ? [state.iface] : PROTOCOLS.filter(protocolOn));
}

const EMPTY_BY_IFACE = {
  anthropic: 'Anthropic 接口下还没有模型。先在「上游站点」给某个站加一个 Anthropic 分组'
    + '（填 Claude Code 那把 key），拉取模型列表导入，或者点右上角「新增模型」自己起名字。',
  openai: 'OpenAI 接口下还没有模型。去「上游站点」展开某个分组，用「拉取模型列表」导入。',
};

export function renderRoutes() {
  failoverBox();
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
    const dead = g.active_route_id === null
      ? ' <span class="tag tag-warn"><span class="dot dot-warn"></span>无可用上游</span>'
      : g.preferred_route_id !== null && g.preferred_route_id !== g.active_route_id
        ? ' <span class="tag tag-warn"><span class="dot dot-warn"></span>正在使用备用候选</span>' : '';
    // 「全部」视图里两种接口混在一起，得标出来谁是谁
    const ifaceTag = !iface && g.protocol
      ? ` <span class="tag${g.protocol === 'anthropic' ? ' tag-accent' : ''}"`
        + ` title="在 ${esc(PROTO_PATH[g.protocol] || '')} 下暴露">${PROTO_LABEL[g.protocol]}</span>` : '';
    const stat = hot.get(g.model_name);
    const usage = stat
      ? `<span class="route-usage" title="最近 2000 条里的请求数 · P95 ${fmtSec(stat.p95)}">${fmtInt(stat.n)} 次</span>`
      : '';
    // 同一个分组下挂了这个模型的好几条候选时，圆片必须把真名写出来才分得清
    const sibs = new Map();
    for (const c of g.candidates) sibs.set(c.group_id, (sibs.get(c.group_id) || 0) + 1);
    const chips = g.candidates.map((c) =>
      chipHtml(
        g.model_name, c, multi.has(c.upstream_id), sibs.get(c.group_id) > 1, g.active_route_id,
      )).join('');

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

/* ================================================================ 实时请求 */

/* 数据来自 /admin/api/inflight（纯内存）。1 秒一刷，秒数由 tickElapsed 在本地走，
   不为了走字去打服务器。

   前两个阶段分开是这页的核心：同样的「12 秒没动静」，还没拿到状态码是站连不上或者
   干脆没在回话（也可能是系统代理的问题），已经拿到了就是模型在想 —— 处置完全不同。
   注意 connect 这一档不只是 TCP 握手：httpx 的 send() 要等到响应头才返回，所以
   「上游收了请求但迟迟不回话」也落在这一档里，文案不能写成「正在连接」。 */
const PHASE = {
  connect: ['等上游回应', '请求已经发出去了，还没拿到状态码 —— 连不上、或者上游收下了但不回话。connect 超时 8 秒'],
  wait: ['已回应，等内容', '状态码拿到了，响应体还没开始 —— 模型在想'],
  stream: ['正在返回', '第一块字节已经转给下游了'],
  done: ['已结束', ''],
};

function callDot(c) {
  if (c.phase !== 'done') return c.phase === 'connect' ? 'warn' : 'good';
  if (c.note && c.note !== 'ok') return 'crit';
  return c.status >= 400 ? 'crit' : 'good';
}

/** 「供应商 · 分组」，分组叫「默认」时省掉 —— 每行都拖一条没信息量的尾巴不值得 */
function whereText(upstream, group) {
  if (!upstream) return '';
  return group && group !== '默认' ? `${esc(upstream)} · ${esc(group)}` : esc(upstream);
}

function trailRow(t) {
  const name = t.remote_model ? ` <span class="dim mono">${esc(t.remote_model)}</span>` : '';
  return `<div class="trail-row">
      <span class="dim">第 ${t.attempt} 次</span>
      <span>${whereText(t.upstream, t.group_name) || '—'}</span>${name}
      ${statusTag(t.status)}
      <span class="grow"></span><span class="dim">${fmtDur(t.ms)}</span>
    </div>`;
}

/* 包大小 -> token 数的估值。标尺是后端从转发记录里量出来的（stats.token_ratio）。

   两个方向量的不是同一件事：上行的 `≈` 可以当**计费量**看（请求体每个字符都算进输入）；
   下行的 `≈` 是**收到手的内容**有多少 token，跟计费量不等 —— 思维链发下来的是总结过的，
   计费按完整的算。所以计费的输出量只认流末尾上游报的那个数，不拿字节去猜。 */
function estTok(bytes, dir, proto) {
  const ratio = ((state.tokens || {})[proto] || {})[dir] || 0;
  if (!bytes || !ratio) return '';
  const what = dir === 'up' ? '按过往记录估的计费量' : '按过往记录估的「收到的内容」，不是计费量';
  return ` <span title="${esc(what)}：${ratio} 字节一个 token">`
    + `≈ ${fmtTokens(Math.round(bytes / ratio))} tok</span>`;
}

/** 上游自己报的数（加粗）优先，没报就按字节估 */
function upText(c) {
  const real = c.tokens_in
    ? ` · 上下文 <b title="上游自己报的数">${fmtTokens(c.tokens_in)}</b> tok`
    : estTok(c.req_bytes, 'up', c.protocol);
  return `上行 ${fmtBytes(c.req_bytes)}${real}`;
}

/* 「收 386KB · 计费输出 9.9k tok」。流还在跑的时候只有「收到的内容」这一个口径，
   计费量要等末尾那个 usage —— 有思维链时两者会差不少，所以打个标说清楚为什么。 */
function downText(c, label) {
  const think = c.thinking
    ? ' <span class="tag" title="思维链发下来的是总结过的，但计费按完整的算 —— 所以「收到的」会明显小于「计费的」">思维链</span>'
    : '';
  const real = c.tokens_out
    ? ` · 计费输出 <b title="上游自己报的数">${fmtTokens(c.tokens_out)}</b> tok`
    : estTok(c.text_bytes, 'down', c.protocol);
  return `<span class="dim">${label} ${fmtBytes(c.sent)}${real}</span>${think}`;
}

function callHtml(c) {
  const done = c.phase === 'done';
  const { bare, onem } = splitOneM(c.remote_model);
  const wide = onem ? ' <span class="tag tag-accent">1M</span>' : '';
  // 「下游要的名字 → 它落到了哪、以什么名字」。一条线上只放一个箭头，两个箭头没人读得顺
  const parts = [];
  if (c.upstream) parts.push(esc(c.upstream));
  if (c.group_name && c.group_name !== '默认') parts.push(esc(c.group_name));
  if (bare && bare !== c.model) parts.push(`<span class="mono">${esc(bare)}</span>`);
  const where = parts.length
    ? parts.join('<span class="dim"> · </span>')
    : '<span class="dim">还没定上游</span>';
  const nth = c.attempt > 1
    ? ` <span class="tag tag-accent" title="前 ${c.attempt - 1} 个候选没打通">第 ${c.attempt} 次</span>` : '';

  const bits = [
    esc(c.client || 'unknown'),
    PROTO_LABEL[c.protocol] || esc(c.protocol),
    c.stream ? '流式' : '非流式',
    upText(c),
  ];
  // count_tokens 也登记：它会走降级、会踩断路器，「这个站为什么在被打」的答案有时就是它
  if (c.meta) bits.push('<b>count_tokens</b>');

  let statePart;
  if (done) {
    statePart = `${statusTag(c.status)}`
      + (c.note && c.note !== 'ok' ? ' ' + noteTag(c.note) : '')
      + downText(c, '收');
  } else {
    const [label, why] = PHASE[c.phase] || [c.phase, ''];
    statePart = `<span class="tone-${callDot(c)}-ink" title="${esc(why)}">${esc(label)}</span>`
      + (c.phase === 'stream' ? downText(c, '已收') : '')
      + (c.status >= 400 ? ' ' + statusTag(c.status) : '');
  }

  const trail = (c.trail || []).length
    ? `<div class="trail">${c.trail.map(trailRow).join('')}</div>` : '';

  return `<div class="call-top">
      <span class="dot dot-${callDot(c)}"></span>
      <span class="mono call-model">${esc(c.model)}</span>${wide}
      <span class="dim">→</span> <span class="call-up">${where}</span>${nth}
      <span class="grow"></span>
      <span class="call-ms" data-ms>${fmtDur(c.elapsed_ms)}</span>
      ${done ? '' : `<button type="button" class="call-cancel" data-act="cancel-call" data-id="${c.id}"
        title="${c.cancel_requested ? '正在中断' : '中断这条请求'}" aria-label="中断 ${esc(c.model)} 的请求"
        ${c.cancel_requested ? 'disabled' : ''}>×</button>`}
    </div>
    <div class="call-sub dim">${bits.join(' · ')}</div>
    <div class="call-state">${statePart}</div>
    ${trail}`;
}

/** 卡片的增量渲染：同一条请求 1 秒重画一次，位置和进场动画都不能跟着抖 */
function paintCalls(host, list) {
  const byId = new Map(
    [...host.children].filter((el) => el.dataset.id).map((el) => [el.dataset.id, el]),
  );
  const empty = host.querySelector('.empty');
  if (empty && list.length) empty.remove();

  let prev = null;
  for (const c of list) {
    const key = String(c.id);
    let el = byId.get(key);
    const fresh = !el;
    if (fresh) {
      el = document.createElement('div');
      el.dataset.id = key;
    }
    byId.delete(key);
    el.className = 'call' + (c.phase === 'done' ? ' is-done' : '') + (c.meta ? ' is-meta' : '');
    // 秒数在本地走：记下「这条是什么时候开始的」，tickElapsed 每 200ms 重算一次
    el.dataset.ticking = c.phase === 'done' ? '0' : '1';
    el.dataset.t0 = String(performance.now() - c.elapsed_ms);
    el.innerHTML = callHtml(c);
    // 已经在该在的位置就别动它 —— after() 会摘下来重插，进场动画会重播
    const inPlace = prev ? prev.nextElementSibling === el : host.firstElementChild === el;
    if (!inPlace) {
      if (prev) prev.after(el);
      else host.prepend(el);
    }
    if (fresh) slideIn(el);
    prev = el;
  }
  for (const el of byId.values()) el.remove();
}

export function renderInflight(data) {
  const counts = data.counts || { requests: 0, streams: 0 };
  $('live-total').textContent = counts.requests
    ? `${counts.requests} 个进行中${counts.streams ? ` · ${counts.streams} 流式` : ''}`
    : '空闲';
  $('badge-live').textContent = counts.requests ? String(counts.requests) : '';
  $('live-failover').innerHTML = failoverSwitches(PROTOCOLS.filter(protocolOn));
  renderBreakers(data);

  const live = data.calls || [];
  const recent = data.recent || [];
  if (!live.length && !$('live-list').querySelector('.empty')) {
    $('live-list').innerHTML = '<div class="empty">现在没有请求在跑'
      + '<br><span class="dim">有请求打进来就会出现在这里</span></div>';
  }
  paintCalls($('live-list'), live);

  // 结束的单独一块：不分开的话「正在跑」和「刚跑完」长得一样，一眼看不出现在忙不忙
  $('recent-sep').hidden = !recent.length;
  $('recent-sep').textContent = recent.length ? `刚刚结束的 ${recent.length} 条（留 90 秒）` : '';
  paintCalls($('recent-list'), recent);
}

/** 秒数本地走字。只动文本，不重排 DOM */
export function tickElapsed() {
  const now = performance.now();
  for (const el of $('live-list').children) {
    if (el.dataset.ticking !== '1') continue;
    const span = el.querySelector('[data-ms]');
    if (span) span.textContent = fmtDur(now - Number(el.dataset.t0));
  }
}

export function renderBreakers(data) {
  const rows = data.breakers || [];
  const total = state.upstreams.reduce((n, u) => n + (u.groups || []).length, 0);
  $('brk-count').textContent = rows.length
    ? `${rows.length} / ${total} 个分组有状态` : `${total} 个分组`;

  const host = $('brk-list');
  if (!rows.length) {
    host.innerHTML = '<div class="empty">所有分组都是干净的 —— 没有在冷却的，也没有连着失败的</div>';
    return;
  }
  host.innerHTML = rows.map((b) => {
    const left = b.cooling_ms > 0
      ? `<span class="tag tag-warn"><span class="dot dot-warn"></span>冷却 ${fmtLeft(b.cooling_ms)}</span>`
      // 期满但失败次数还留着：会放它试一次，再失败就直接进更长的冷却
      : '<span class="tag">冷却期满 · 会放它试一次</span>';
    return `<div class="brk-row">
      <span class="brk-name">${esc(groupLabel(b.group_id))}</span>
      ${left}
      <span class="dim">连续失败 ${b.fails} 次${b.last_status ? `，最近 ${b.last_status}` : ''}</span>
      <span class="grow"></span>
      ${b.cooled > 1 ? `<span class="dim">进过 ${b.cooled} 次冷却</span>` : ''}
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
  const names = catalogOfGroup(g.id);
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

/** 供应商弹窗里的分组列表：管理分组的主路径，点「编辑」会再叠一层分组弹窗。
    表里那个可展开的行是同一套东西的只读快照，两边最后都调 openGroup。 */
export function renderUpGroups() {
  const wrap = $('up-groups-wrap');
  const u = state.editing === null ? null : state.upstreams.find((x) => x.id === state.editing);
  if (!$('up-dialog').open || !u) { wrap.hidden = true; return; }

  const groups = u.groups || [];
  wrap.hidden = false;
  $('up-gcount').textContent = groups.length ? `${groups.length} 个` : '还没有';
  $('up-add-group').dataset.uid = String(u.id);

  const only = groups.length === 1 ? groups[0] : null;
  $('up-groups').innerHTML = groups.map((g) => {
    const n = catalogOfGroup(g.id).length;
    const tag = `<span class="tag${g.protocol === 'anthropic' ? ' tag-accent' : ''}"`
      + ` title="${esc(PROTO_PATH[g.protocol] || '')}">${PROTO_LABEL[g.protocol] || '?'}</span>`;
    return `<div class="ug-row">
      <span class="ug-name">${esc(g.name)}</span>${tag}
      <span class="ug-key">${maskKey(g.api_key)}</span>
      <span class="grow"></span>
      <span class="dim" style="font-size:12px">${n} 个模型</span>
      <input type="checkbox" class="switch" ${g.enabled ? 'checked' : ''}
             data-act="toggle-group" data-gid="${g.id}" aria-label="启用分组 ${esc(g.name)}">
      <button type="button" class="btn btn-ghost btn-sm" data-act="edit-group" data-gid="${g.id}">编辑</button>
      <button type="button" class="btn btn-danger btn-sm" data-act="del-group" data-gid="${g.id}">删除</button>
    </div>`;
  }).join('') || '<p class="dim" style="font-size:12px;margin:0">'
    + '没有分组的供应商用不了 —— 加一个，选接口、填那把 key。</p>';

  // 一把 key 两种接口都能用的站不少，而接口是分组的属性，所以给个一键复制
  const clone = $('up-clone-group');
  if (clone) clone.remove();
  const cloneHtml = cloneButtons(only);
  if (cloneHtml) {
    $('up-add-group').insertAdjacentHTML('afterend',
      `<span id="up-clone-group" class="field-row" style="display:inline-flex;gap:6px">${cloneHtml}</span>`);
  }
}

/* 出口不是「跟随系统」时在地址后面标一下：一屏上哪个站走的是另一扇门，
   得能一眼看出来，不然只有点进编辑才知道。VPS 预设只标「VPS」——
   完整 URL 里有密码，不放 tooltip。 */
function egressTag(u) {
  const raw = (u.egress || '').trim();
  if (!raw) return '';
  if (raw === 'direct') return ' <span class="tag" title="不走系统代理，从本机自己的出口出去">直连</span>';
  if (state.egressVps && raw === state.egressVps) {
    return ' <span class="tag tag-accent" title="只有这个站走 VPS 上那扇门">VPS</span>';
  }
  return ` <span class="tag tag-accent" title="只有这个站走 ${esc(raw)}">走代理</span>`;
}

export function renderUpstreams() {
  const list = state.upstreams;
  $('upstream-count').textContent = `${list.length} 个`;
  const on = list.filter((u) => u.enabled).length;
  $('badge-upstreams').textContent = `${on}/${list.length}`;
  renderUpGroups();

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
    // 整行都是展开开关（那个小三角太难瞄）。行里的按钮和开关有自己的 data-act，
    // 事件委托取的是最近的那个，所以不会被这里截走
    rows.push(`<tr class="up-row${open ? ' is-open' : ''}" data-act="toggle-groups" data-uid="${u.id}"
                   title="点这一行展开 / 收起它的分组">
      <td>
        <button type="button" class="tw" aria-expanded="${open}"
                aria-label="展开 / 收起分组">${open ? '▾' : '▸'}</button>
        ${esc(u.name)}${u.enabled ? '' : ' <span class="tag"><span class="dot dot-off"></span>停用</span>'}
        <div class="up-sub">${ifaceTags(u)}<span class="tag">${groups.length} 组</span></div>
      </td>
      <td class="mono dim truncate" title="${esc(u.base_url)}">${esc(u.base_url)}${egressTag(u)}</td>
      <td>${modelTags(catalogOfUpstream(u.id))}</td>
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
    const clone = cloneButtons(only);
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
  manual_abort: ['', '手动中断'],
  // 这一次失败被自动降级接住了：客户端没看到它，但钱和时间是真花了，所以照样留痕
  failed_over: ['warn', '已降级'],
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
  // 第几次尝试：> 1 就是前面的候选没打通、换到这个站来的
  const nth = r.attempt > 1
    ? ` <span class="tag tag-accent" title="前 ${r.attempt - 1} 个候选没打通">第 ${r.attempt} 次</span>` : '';
  return `<tr data-id="${r.id}" data-log-proto="${esc(proto)}">
      <td class="dim nowrap" title="${esc(r.ts || '')}">${esc((r.ts || '').slice(5))}</td>
      <td class="truncate" title="${esc(r.client)}">${esc(r.client)}</td>
      <td class="nowrap">${protoCell}</td>
      <td class="mono truncate" title="${esc(modelTip)}">${esc(r.model)}${remote}${r.stream ? ' <span class="tag">流</span>' : ''}</td>
      <td class="truncate" title="${esc(upTip)}">${esc(r.upstream)}${grp}${nth}</td>
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

/** 把 list（按 id 升序）prepend 进去，再按 cap 裁掉尾部 */
function insertRows(list, cap) {
  const body = $('log-body');
  const known = state.logIds;
  const tmp = document.createElement('tbody');
  tmp.innerHTML = list.map(logRow).join('');
  /* 逐个 prepend：最后插进去的那个（id 最大）留在最上面，正好接上下面按 id 降序的老行。
     这里不能先 reverse —— 一次只来一行时看不出区别，但切出去再切回来、或者标签页
     不可见时攒了十几条，一批插进来就会整块倒过来：上面那块从旧到新，还压在
     原来最新的那行上面。点「刷新」是整表重画，所以又好了。 */
  for (const tr of [...tmp.children]) {
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
  // 侧栏导航上的角标：不在实时页也该看得见「现在有几条在跑」
  $('badge-live').textContent = live.requests ? String(live.requests) : '';
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

/* 切换候选后：把"流量改道"这件事演出来 */
export function afterSwitch(model, fromRid, toRid) {
  const row = $('route-list').querySelector(`.route[data-model="${CSS.escape(model)}"]`);
  if (!row) return;
  flow(row.querySelector(`.chip[data-rid="${fromRid}"]`), row.querySelector(`.chip[data-rid="${toRid}"]`));
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
