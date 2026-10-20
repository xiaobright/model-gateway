/* NEXUS — console application. Live data only, no mock fallbacks. */

import {
  $, $$, api, esc, fmtCompact, fmtInt, fmtMs, fmtBytes, fmtPct, fmtClock, fmtWhen,
  animNum, sparkPaths, toast, copyText, confirmDialog, initModal, initField,
  initTheme, applyTheme,
} from "./core.js";
import { mountChart } from "./chart.js";
import { renderGalaxy } from "./galaxy.js";

const S = {
  view: "overview",
  window: "24h",
  protocols: [],
  access: null,
  overview: null,
  models: [],
  upstreams: [],
  inflight: { calls: [], recent: [], counts: { requests: 0, streams: 0 } },
  requests: [],
  switches: { enabled: {} },
  failover: { enabled: {} },
  stall: 0,
  selectedModel: null,
  modelQuery: "",
  modelProtocol: "all",
  upstreamQuery: "",
  logQuery: "",
  logProtocol: "all",
  logStatus: "all",
  liveFocus: null,
  chartAnimate: true,
  lastUpdated: 0,
  online: true,
  booted: false,
};

const VIEW_META = {
  overview: { title: "总览", eyebrow: "MODEL GATEWAY / OVERVIEW", ico: "◎" },
  models: { title: "模型路由", eyebrow: "MODEL GATEWAY / ROUTING", ico: "◇" },
  upstreams: { title: "上游站点", eyebrow: "MODEL GATEWAY / UPSTREAMS", ico: "▦" },
  live: { title: "实时请求", eyebrow: "MODEL GATEWAY / IN FLIGHT", ico: "◉" },
  log: { title: "转发记录", eyebrow: "MODEL GATEWAY / REQUEST LOG", ico: "≡" },
  settings: { title: "网关设置", eyebrow: "MODEL GATEWAY / SETTINGS", ico: "⚙" },
};

const NOTE_TEXT = {
  ok: "正常结束",
  failed_over: "换站后成功",
  client_abort: "客户端提前断开",
  upstream_abort: "上游中断",
  connect_failed: "连接失败",
  truncated: "流被截断",
  manual_abort: "手动中断",
  stall_timeout: "发呆超时",
  protocol_error: "协议错误",
  same_retry: "同站重试",
  hold_retry: "截断守卫",
  http_error: "上游报错",
  pool_timeout: "连接池超时",
  returned: "已返回",
  search_failed_over: "搜索换站",
  unobserved: "未观测",
};

const PROTO_BADGE = { anthropic: "MSG", openai: "RES", "openai-chat": "CHAT" };

const PHASE_META = {
  connect: { label: "等上游回应", cls: "is-wait", note: "连接、排队或处理中" },
  wait: { label: "已回应，等内容", cls: "is-wait", note: "收到响应头，正文未到" },
  stream: { label: "正在返回", cls: "is-stream", note: "已收到内容" },
  done: { label: "已结束", cls: "", note: "" },
};

/* ------------------------------------------------------------------ 基础 */

const protocolLabel = (name) => S.protocols.find((p) => p.name === name)?.label || name;
const protocolShort = (name) => S.protocols.find((p) => p.name === name)?.short || name;
const baseUrl = () => (S.access ? `http://${S.access.host}:${S.access.port}` : "http://127.0.0.1:8317");

function modelKey(m) {
  return `${m.model_name}\u0000${m.protocol}`;
}

function statusClass(row) {
  const status = Number(row.status) || 0;
  const bad = status >= 400 || ["connect_failed", "upstream_abort", "truncated", "client_abort", "manual_abort", "stall_timeout", "protocol_error"].includes(row.note);
  if (!bad && status >= 200 && status < 400) return "ok";
  if (status >= 500 || ["connect_failed", "upstream_abort", "protocol_error"].includes(row.note)) return "bad";
  return "warn";
}

function setOnline(ok) {
  S.online = ok;
  const rail = $("#rail-state");
  rail.classList.toggle("is-on", ok);
  rail.classList.toggle("is-off", !ok);
  rail.title = ok ? "控制面已连接" : "控制面不可达";
  $("#runtime-state").textContent = ok ? "已连接" : "不可达";
}

async function safe(fn, label) {
  try {
    return await fn();
  } catch (err) {
    if (!S.online) return null;
    setOnline(false);
    if (label) toast(label, err.message, "err");
    return null;
  }
}

/* ------------------------------------------------------------------ 视图切换 */

function setView(name, { push = true } = {}) {
  if (!VIEW_META[name]) return;
  S.view = name;
  $$(".view").forEach((v) => {
    const active = v.dataset.view === name;
    v.classList.toggle("is-active", active);
    v.hidden = !active;
  });
  $$(".rail-btn[data-view]").forEach((b) => b.classList.toggle("is-active", b.dataset.view === name));
  $("#page-title").textContent = VIEW_META[name].title;
  $("#page-eyebrow").textContent = VIEW_META[name].eyebrow;
  document.title = `${VIEW_META[name].title} · NEXUS`;
  if (push) history.replaceState(null, "", `#${name}`);

  // 入场动画只在切换视图时播放；轮询刷新不应该让卡片反复跳起来。
  document.body.classList.add("view-enter");
  clearTimeout(setView._enter);
  setView._enter = setTimeout(() => document.body.classList.remove("view-enter"), 900);

  if (name === "models") renderModels();
  if (name === "upstreams") renderUpstreams();
  if (name === "live") renderLive();
  if (name === "log") {
    renderLog();
    loadRequests();
  }
  if (name === "settings") {
    renderSettings();
    loadSettings();
  }
  if (name === "overview") requestAnimationFrame(() => { moveSegPill(); drawChart(false); });
}

function bindNav() {
  $$(".rail-btn[data-view]").forEach((btn) => {
    btn.addEventListener("click", () => setView(btn.dataset.view));
  });
  $("#brand").addEventListener("click", (e) => { e.preventDefault(); setView("overview"); });
  $("#live-pill").addEventListener("click", () => setView("live"));
  $("#refresh-btn").addEventListener("click", async (e) => {
    const btn = e.currentTarget;
    btn.classList.add("is-spinning");
    await refreshAll({ manual: true });
    setTimeout(() => btn.classList.remove("is-spinning"), 500);
  });
  $("#endpoint-chip").addEventListener("click", () => copyText(`${baseUrl()}/v1`));
  $("#theme-btn").addEventListener("click", () => toggleTheme());
  $("#theme-btn-mobile").addEventListener("click", () => toggleTheme());
  $("#window-seg").addEventListener("click", (e) => {
    const btn = e.target.closest("button[data-window]");
    if (!btn || btn.classList.contains("is-on")) return;
    $$("#window-seg button").forEach((b) => b.classList.toggle("is-on", b === btn));
    moveSegPill();
    S.window = btn.dataset.window;
    S.chartAnimate = true;
    loadOverview();
  });
  window.addEventListener("resize", () => {
    moveSegPill();
    clearTimeout(bindNav._resize);
    bindNav._resize = setTimeout(() => drawChart(false), 180);
  });
}

function toggleTheme() {
  const next = document.documentElement.dataset.theme === "light" ? "dark" : "light";
  applyTheme(next);
  if (S.overview) drawChart(false);
}

function moveSegPill() {  const seg = $("#window-seg");
  const on = seg?.querySelector("button.is-on");
  const pill = seg?.querySelector(".seg-pill");
  if (!on || !pill) return;
  pill.style.width = `${on.offsetWidth}px`;
  pill.style.transform = `translateX(${on.offsetLeft - 3}px)`;
}

/* ------------------------------------------------------------------ 数据加载 */

async function loadFoundation() {
  const [protos, access, switches, failover, stall] = await Promise.all([
    safe(() => api("/protocols"), "接口清单读取失败"),
    safe(() => api("/access-info"), "接入信息读取失败"),
    safe(() => api("/protocol-switches"), "接口开关读取失败"),
    safe(() => api("/failover"), "降级设置读取失败"),
    safe(() => api("/stall-timeout"), "超时设置读取失败"),
  ]);
  if (protos) S.protocols = protos.protocols || [];
  if (access) { S.access = access; updateEndpoint(); }
  if (switches) S.switches = switches;
  if (failover) S.failover = failover;
  if (stall) S.stall = stall.seconds;
  renderProtocolChips();
  renderSettings();
}

async function loadOverview({ quiet = false } = {}) {
  const data = await safe(() => api(`/overview?window=${S.window}`), quiet ? "" : "概览数据读取失败");
  if (!data) { renderKpis(); return; }
  setOnline(true);
  S.overview = data;
  S.lastUpdated = Date.now();
  renderKpis();
  drawChart(S.chartAnimate);
  S.chartAnimate = false;
  renderPulse();
  renderHealth();
  renderHeat();
  renderGalaxyView();
  renderFooter();
}

async function loadModels({ quiet = false } = {}) {
  const data = await safe(() => api("/models"), quiet ? "" : "模型路由读取失败");
  if (!data) return;
  setOnline(true);
  S.models = data;
  if (S.selectedModel && !S.models.some((m) => m.model_name === S.selectedModel.model_name && m.protocol === S.selectedModel.protocol)) {
    S.selectedModel = null;
  }
  renderProtocolChips();
  renderModels();
  renderGalaxyView();
}

async function loadUpstreams({ quiet = false } = {}) {
  const data = await safe(() => api("/upstreams"), quiet ? "" : "上游站点读取失败");
  if (!data) return;
  setOnline(true);
  S.upstreams = data;
  renderUpstreams();
}

async function loadInflight({ quiet = false } = {}) {
  const data = await safe(() => api("/inflight"), quiet ? "" : "实时请求读取失败");
  if (!data) return;
  setOnline(true);
  S.inflight = data;
  renderLivePill();
  renderPulse();
  if (S.view === "live") renderLive();
}

async function loadRequests({ quiet = false } = {}) {
  const data = await safe(() => api("/requests?limit=200"), quiet ? "" : "转发记录读取失败");
  if (!data) return;
  setOnline(true);
  S.requests = data;
  renderLog();
}

async function loadSettings() {
  const [switches, failover, stall] = await Promise.all([
    safe(() => api("/protocol-switches"), "接口开关读取失败"),
    safe(() => api("/failover"), "降级设置读取失败"),
    safe(() => api("/stall-timeout"), "超时设置读取失败"),
  ]);
  if (switches) S.switches = switches;
  if (failover) S.failover = failover;
  if (stall) S.stall = stall.seconds;
  renderSettings();
}

async function refreshAll({ manual = false } = {}) {
  await Promise.allSettled([
    loadOverview({ quiet: !manual }),
    loadModels({ quiet: !manual }),
    loadUpstreams({ quiet: !manual }),
    loadInflight({ quiet: !manual }),
    loadSettings(),
  ]);
  if (manual) toast("已同步", "概览、模型、上游和实时状态都已刷新", "info");
}

/* ------------------------------------------------------------------ 总览：指标 */

function renderKpis() {
  const grid = $("#kpi-grid");
  const ov = S.overview;
  if (!ov) {
    if (!grid.children.length) {
      grid.innerHTML = Array.from({ length: 5 }, () => '<div class="kpi skeleton" style="height:118px"></div>').join("");
    }
    return;
  }
  const t = ov.totals || {};
  const pts = ov.series?.points || [];
  const half = Math.max(1, Math.floor(pts.length / 2));
  const sum = (arr, k) => arr.reduce((n, p) => n + (p[k] || 0), 0);
  const cur = { n: sum(pts.slice(-half), "n"), ti: sum(pts.slice(-half), "ti"), to: sum(pts.slice(-half), "to") };
  const prev = { n: sum(pts.slice(0, -half), "n"), ti: sum(pts.slice(0, -half), "ti"), to: sum(pts.slice(0, -half), "to") };
  const delta = (a, b) => (a > 0 && b > 0 ? ((a - b) / b) * 100 : null);

  const cards = [
    {
      key: "requests", label: "请求", value: t.requests || 0, fmt: fmtInt, unit: "次",
      delta: delta(cur.n, prev.n), sub: `${fmtInt(t.saved)} 次换站救回 · ${fmtInt(t.failed_over)} 次降级`,
      spark: pts.map((p) => p.n), color: "var(--brand)",
    },
    {
      key: "input", label: "输入 Tokens", value: t.input_tokens || 0, fmt: fmtCompact, unit: "",
      delta: delta(cur.ti, prev.ti), sub: `${fmtCompact(t.cached_tokens)} 来自缓存`,
      spark: pts.map((p) => p.ti), color: "var(--brand-2)",
    },
    {
      key: "output", label: "输出 Tokens", value: t.output_tokens || 0, fmt: fmtCompact, unit: "",
      delta: delta(cur.to, prev.to), sub: `平均 ${fmtInt((t.output_tokens || 0) / Math.max(1, t.requests || 1))} / 请求`,
      spark: pts.map((p) => p.to), color: "var(--brand-3)",
    },
    {
      key: "cache", label: "缓存命中率", value: (t.cache_hit_rate || 0) * 100, fmt: (v) => v.toFixed(1), unit: "%",
      delta: null, sub: `${fmtCompact(t.cached_tokens)} tokens 复用`,
      spark: pts.map((p) => (p.ti > 0 ? (p.tc / p.ti) * 100 : 0)), color: "var(--ok)",
    },
    {
      key: "p95", label: "P95 延迟", value: t.p95 || 0, fmt: (v) => fmtInt(v), unit: "ms",
      delta: null, sub: `窗口内 ${fmtInt(t.requests)} 条记录`,
      spark: null, color: "var(--warn)",
    },
  ];

  grid.innerHTML = cards.map((c, i) => {
    const spark = c.spark ? sparkPaths(c.spark, 100, 30) : null;
    const d = c.delta;
    const deltaHtml = d == null ? "" : `<span class="kpi-delta ${d >= 0 ? "up" : "down"}">${d >= 0 ? "↑" : "↓"} ${Math.abs(d).toFixed(1)}%</span>`;
    return `
    <article class="kpi" style="--i:${i};color:${c.color}">
      <div class="kpi-top">
        <span class="kpi-label">${c.label}</span>
        ${deltaHtml}
      </div>
      <div class="kpi-value"><b data-k="${c.key}">0</b>${c.unit ? `<small>${c.unit}</small>` : ""}</div>
      <div class="kpi-sub">${esc(c.sub)}</div>
      ${spark ? `<svg class="kpi-spark" viewBox="0 0 100 30" preserveAspectRatio="none" aria-hidden="true">
        <path d="${spark.area}" style="fill:currentColor;opacity:.1"/>
        <path d="${spark.line}" style="fill:none;stroke:currentColor;stroke-width:1.5;vector-effect:non-scaling-stroke;opacity:.9"/>
      </svg>` : `<div class="kpi-spark"></div>`}
    </article>`;
  }).join("");

  cards.forEach((c) => animNum($(`[data-k="${c.key}"]`), `${S.window}:${c.key}`, c.value, c.fmt));
}

/* ------------------------------------------------------------------ 总览：图表 */

function drawChart(animate) {
  if (!S.overview?.series) return;
  const wrap = $("#chart-wrap");
  if (!wrap || wrap.clientWidth < 40) return;
  mountChart($("#traffic-chart"), $("#chart-tip"), S.overview.series, { animate });
}

/* ------------------------------------------------------------------ 总览：实时脉动 */

function renderPulse() {
  const host = $("#pulse-card");
  if (!host) return;
  const counts = S.inflight?.counts || { requests: 0, streams: 0 };
  const calls = S.inflight?.calls || [];
  const recent = S.inflight?.recent || [];
  const live = counts.requests || 0;
  const hot = live > 0;

  const last = recent[0] || S.requests[0] || null;
  const lastText = last ? `${last.model || "未知模型"} · ${fmtWhen(last.ts || "")}` : "还没有记录";

  host.innerHTML = `
    <div class="pulse-head">
      <div class="pulse-title">
        <span class="live-dot ${hot ? "hot" : ""}" style="${hot ? "background:var(--ok);box-shadow:0 0 10px var(--ok)" : ""}"></span>
        实时脉动
      </div>
      <span class="tag ${hot ? "ok" : ""}">${hot ? "传输中" : "待命"}</span>
    </div>
    <div class="pulse-body">
      ${hot ? `
        <div class="pulse-number"><b>${fmtInt(live)}</b><span>条请求正在穿越</span></div>
        <div class="pulse-sub">其中 ${fmtInt(counts.streams || 0)} 条已进入流式返回</div>
        <div class="pulse-calls">
          ${calls.slice(0, 3).map((c) => {
            const phase = PHASE_META[c.phase] || PHASE_META.connect;
            return `<div class="pulse-call">
              <span class="pc-bar ${c.phase === "stream" ? "stream" : c.phase === "wait" ? "wait" : ""}"></span>
              <span class="pc-model">${esc(c.model)}</span>
              <span class="pc-arrow">→</span>
              <span class="pc-up">${esc(c.upstream || "连接中")}</span>
              <span class="pc-ms">${fmtMs(c.elapsed_ms || 0)}</span>
            </div>`;
          }).join("")}
        </div>` : `
        <div class="orb"><span class="orb-ring"></span><span class="orb-ring"></span><span class="orb-ring"></span><span class="orb-core"></span></div>
        <div class="pulse-idle"><h3>链路静默</h3><p>${esc(lastText)}</p></div>`}
    </div>
    <div class="pulse-foot">
      <span>P95 <b>${fmtInt(S.overview?.totals?.p95 || 0)}</b> ms</span>
      <span>输入 <b>${fmtCompact(S.overview?.totals?.input_tokens || 0)}</b></span>
      <span style="margin-left:auto">${hot ? "自动刷新中" : "等待请求"}</span>
    </div>`;
}

/* ------------------------------------------------------------------ 总览：健康 / 热度 */

function renderHealth() {
  const host = $("#health-list");
  if (!host) return;
  const rows = S.overview?.upstreams || [];
  const meta = $("#health-meta");
  if (meta) meta.textContent = rows.length ? `${rows.length} 个站 · 当前窗口` : "";
  if (!rows.length) {
    host.innerHTML = `<div class="empty"><span class="e-mark">▦</span><b>当前窗口没有上游记录</b><span>发起一次请求后，这里会显示成功率与 P95。</span></div>`;
    return;
  }
  host.innerHTML = rows.slice(0, 7).map((r) => {
    const rate = Number(r.ok_rate) || 0;
    const cls = rate >= 0.95 ? "ok" : rate >= 0.8 ? "warn" : "bad";
    const color = rate >= 0.95 ? "var(--ok)" : rate >= 0.8 ? "var(--warn)" : "var(--bad)";
    const R = 12.5;
    const C = 2 * Math.PI * R;
    return `
    <div class="health-row">
      <svg class="ring" viewBox="0 0 30 30" aria-hidden="true">
        <circle class="bg" cx="15" cy="15" r="${R}"/>
        <circle class="fg" cx="15" cy="15" r="${R}" stroke="${color}"
          stroke-dasharray="${C.toFixed(1)}" stroke-dashoffset="${(C * (1 - rate)).toFixed(1)}"/>
      </svg>
      <div style="min-width:0">
        <div class="health-name">${esc(r.name)}</div>
        <div class="health-sub">${fmtInt(r.n)} 次 · ${fmtPct(rate, 0)} 成功</div>
      </div>
      <div class="health-num">${fmtMs(r.p95)}<span>P95</span></div>
      <div class="health-rate ${cls}">${fmtPct(rate, 1)}</div>
      <div class="health-num">${fmtMs(r.avg)}<span>平均</span></div>
    </div>`;
  }).join("");
}

function renderHeat() {
  const host = $("#heat-list");
  if (!host) return;
  const rows = (S.overview?.models || []).slice(0, 7);
  const meta = $("#heat-meta");
  if (meta) meta.textContent = rows.length ? "按当前窗口请求数" : "";
  if (!rows.length) {
    host.innerHTML = `<div class="empty"><span class="e-mark">◇</span><b>还没有模型调用</b><span>配置好路由之后，客户端请求会把这里点亮。</span></div>`;
    return;
  }
  const max = Math.max(...rows.map((r) => r.n), 1);
  host.innerHTML = rows.map((r) => `
    <div class="heat-row" data-heat-model="${esc(r.model)}" data-heat-proto="${esc(r.protocol)}">
      <div class="heat-main">
        <div class="heat-name"><span class="proto-mark p-${esc(r.protocol)}" style="height:12px;width:3px;border-radius:1px"></span>${esc(r.model)}</div>
        <div class="heat-bar"><i style="width:${Math.max(3, (r.n / max) * 100).toFixed(1)}%"></i></div>
      </div>
      <div class="heat-n">${fmtInt(r.n)}<span>${fmtMs(r.p95)} P95</span></div>
    </div>`).join("");
  host.querySelectorAll("[data-heat-model]").forEach((row) => {
    row.addEventListener("click", () => {
      const m = S.models.find((x) => x.model_name === row.dataset.heatModel && x.protocol === row.dataset.heatProto);
      if (m) openModelDrawer(m);
      else setView("models");
    });
  });
}

/* ------------------------------------------------------------------ 星系 */

function renderGalaxyView() {
  if (!S.overview && !S.models.length) return;
  renderGalaxy($("#galaxy-stage"), $("#galaxy-panel"), S.models, S.selectedModel, {
    protocolLabel,
    onSelect: (m) => {
      S.selectedModel = { model_name: m.model_name, protocol: m.protocol };
      renderGalaxyView();
    },
    onUpstream: (id) => { S.upstreamFocus = id; setView("upstreams"); },
    onAction: async (action, ctx) => {
      if (action === "detail") {
        openModelDrawer(ctx.model);
      } else if (action === "switch") {
        await switchRoute(ctx.routeId);
      } else if (action === "live") {
        S.liveFocus = ctx.modelName;
        setView("live");
      }
    },
  });
}

function renderFooter() {
  const el = $("#foot-updated");
  if (el) {
    el.textContent = S.lastUpdated
      ? `最后同步 ${new Date(S.lastUpdated).toLocaleTimeString("zh-CN", { hour12: false })}`
      : "尚未同步";
  }
}

function updateEndpoint() {
  if (!S.access) return;
  const text = `${S.access.host}:${S.access.port}`;
  $("#endpoint-text").textContent = text;
  $("#foot-addr").textContent = text;
}

function renderLivePill() {
  const counts = S.inflight?.counts || { requests: 0 };
  const n = counts.requests || 0;
  $("#live-count").textContent = fmtInt(n);
  $("#live-pill").classList.toggle("is-hot", n > 0);
  const badge = $("#rail-live");
  badge.hidden = n === 0;
}

/* ------------------------------------------------------------------ 模型视图 */

function renderProtocolChips() {
  const host = $("#protocol-chips");
  if (host) {
    const counts = new Map();
    for (const m of S.models) counts.set(m.protocol, (counts.get(m.protocol) || 0) + 1);
    const items = [{ key: "all", label: "全部", n: S.models.length }]
      .concat(S.protocols.filter((p) => counts.has(p.name)).map((p) => ({ key: p.name, label: p.short || p.label, n: counts.get(p.name) })));
    host.innerHTML = items.map((it) => `
      <button type="button" class="chip ${S.modelProtocol === it.key ? "is-on" : ""}" data-proto="${esc(it.key)}">
        ${esc(it.label)}<span class="n">${it.n}</span>
      </button>`).join("");
    host.onclick = (e) => {
      const btn = e.target.closest("[data-proto]");
      if (!btn) return;
      S.modelProtocol = btn.dataset.proto;
      renderProtocolChips();
      renderModels();
    };
  }

  const select = $("#log-protocol");
  if (select && select.dataset.ready !== "1") {
    select.dataset.ready = "1";
    select.innerHTML = `<option value="all">全部接口</option>` + S.protocols.map((p) => `<option value="${esc(p.name)}">${esc(p.short || p.label)}</option>`).join("");
    select.value = S.logProtocol;
  }
}

function renderModels() {
  const host = $("#model-list");
  if (!host) return;
  const q = S.modelQuery.trim().toLowerCase();
  const list = S.models.filter((m) => {
    if (S.modelProtocol !== "all" && m.protocol !== S.modelProtocol) return false;
    if (!q) return true;
    const hay = [m.model_name, m.protocol, m.forward_to || ""]
      .concat((m.candidates || []).flatMap((c) => [c.upstream_name, c.remote_model, c.group_name]))
      .join(" ").toLowerCase();
    return hay.includes(q);
  });

  const count = $("#model-count");
  if (count) count.textContent = `${list.length} / ${S.models.length} 条链`;

  if (!list.length) {
    host.innerHTML = `<div class="empty"><span class="e-mark">◇</span><b>${S.models.length ? "没有匹配的模型" : "还没有下游模型"}</b>
      <span>${S.models.length ? "换个关键词或切回「全部」接口。" : "在「上游站点」登记模型，或在原管理页新增一条路由。"}</span></div>`;
    return;
  }

  host.innerHTML = list.map((m, i) => {
    const cands = m.candidates || [];
    const start = cands.find((c) => c.route_id === m.active_route_id);
    const shown = cands.slice(0, 3);
    const cooling = cands.filter((c) => c.cooling_ms > 0).length;
    const path = m.forward_to
      ? `<span class="forward-chip">↦ ${esc(m.forward_to)}</span>`
      : shown.map((c, idx) => `
          <span class="hop ${c.route_id === m.active_route_id ? "is-start" : ""} ${c.route_id === m.preferred_route_id ? "is-preferred" : ""}">
            <span class="hop-idx">${idx + 1}${c.route_id === m.preferred_route_id ? " · 首选" : ""}</span>
            <b>${esc(c.upstream_name)}</b>
            <em>${esc(c.remote_model || "同名")}</em>
          </span>${idx < shown.length - 1 ? '<span class="hop-link"></span>' : ""}`).join("")
        + (cands.length > shown.length ? `<span class="hop-more">+${cands.length - shown.length}</span>` : "");
    return `
    <button type="button" class="chain-row ${start || m.forward_to ? "" : "is-off"}" style="--i:${i}"
      data-model="${esc(m.model_name)}" data-protocol="${esc(m.protocol)}"
      aria-label="打开 ${esc(m.model_name)} 的详情">
      <div class="chain-id">
        <span class="proto-mark p-${esc(m.protocol)}"></span>
        <div style="min-width:0">
          <h3>${esc(m.model_name)}</h3>
          <span class="proto-label">${esc(protocolLabel(m.protocol))}</span>
        </div>
      </div>
      <div class="chain-path">${path}</div>
      <div class="chain-state ${start || m.forward_to ? "" : "is-off"}">
        <b>${m.forward_to ? `↦ ${esc(m.forward_to)}` : start ? `<i class="st-dot"></i>${esc(start.upstream_name)}` : "无可用起点"}</b>
        <span>${m.forward_to ? "整条链已交接" : `${cands.length} 候选${cooling ? ` · ${cooling} 冷却` : ""}`}</span>
      </div>
      <svg class="chev" viewBox="0 0 24 24" aria-hidden="true"><path d="m9 5 7 7-7 7"/></svg>
    </button>`;
  }).join("");

  host.onclick = (e) => {
    const row = e.target.closest("[data-model]");
    if (!row) return;
    const m = S.models.find((x) => x.model_name === row.dataset.model && x.protocol === row.dataset.protocol);
    if (m) openModelDrawer(m);
  };
}

/* ------------------------------------------------------------------ 抽屉 */

let drawerOpen = null;

function openDrawer(eyebrow, html) {
  $("#drawer-eyebrow").textContent = eyebrow;
  $("#drawer-body").innerHTML = html;
  $("#drawer-body").scrollTop = 0;
  $("#scrim").hidden = false;
  $("#drawer").hidden = false;
  drawerOpen = true;
  document.body.style.overflow = "hidden";
}

function closeDrawer() {
  if (!drawerOpen) return;
  $("#scrim").hidden = true;
  $("#drawer").hidden = true;
  drawerOpen = false;
  document.body.style.overflow = "";
}

function openModelDrawer(model) {
  const m = S.models.find((x) => x.model_name === model.model_name && x.protocol === model.protocol);
  if (!m) { toast("模型不存在", "它可能刚刚被删除或改了名字", "err"); return; }
  const usage = (S.overview?.models || []).find((x) => x.model === m.model_name && x.protocol === m.protocol);
  const cands = m.candidates || [];
  const start = cands.find((c) => c.route_id === m.active_route_id);
  const others = S.models.filter((x) => x.protocol === m.protocol && x.model_name !== m.model_name);

  const candHtml = cands.length ? cands.map((c, i) => {
    const tags = [];
    if (c.route_id === m.preferred_route_id) tags.push('<span class="tag brand">首选</span>');
    if (c.route_id === m.active_route_id) tags.push('<span class="tag ok">起点</span>');
    if (!c.group_enabled) tags.push('<span class="tag warn">分组停用</span>');
    if (!c.upstream_enabled) tags.push('<span class="tag warn">站停用</span>');
    if (c.cooling_ms > 0) tags.push(`<span class="tag bad">冷却 ${fmtMs(c.cooling_ms)}</span>`);
    return `
    <div class="cand ${c.route_id === m.active_route_id ? "is-start" : ""} ${c.cooling_ms > 0 ? "is-cool" : ""}">
      <div class="cand-top">
        <span class="cand-idx">${i + 1}</span>
        <span class="cand-up">${esc(c.upstream_name)}</span>
        <span class="cand-tags">${tags.join("")}</span>
      </div>
      <div class="cand-mid">
        <code>${esc(c.remote_model || "同名")}</code><span class="sep">/</span><span>${esc(c.group_name)}</span>
        ${c.cooling_ms > 0 ? `<span class="sep">/</span><span style="color:var(--bad)">${c.fails} 次失败</span>` : ""}
      </div>
      <div class="cand-actions">
        ${c.route_id !== m.preferred_route_id ? `<button type="button" class="btn btn-sm" data-act="prefer" data-route="${c.route_id}">设为首选</button>` : ""}
        ${i > 0 ? `<button type="button" class="btn btn-sm" data-act="up" data-route="${c.route_id}">前移</button>` : ""}
        ${i < cands.length - 1 ? `<button type="button" class="btn btn-sm" data-act="down" data-route="${c.route_id}">后移</button>` : ""}
        <button type="button" class="btn btn-sm btn-danger" data-act="remove" data-route="${c.route_id}">移除</button>
      </div>
    </div>`;
  }).join("") : `<div class="empty" style="padding:24px"><span class="e-mark">↦</span><b>这条链没有自己的候选</b><span>请求完全按交接目标走。</span></div>`;

  openDrawer("MODEL CHAIN", `
    <div class="dh-title">
      <span class="proto-mark p-${esc(m.protocol)}" style="height:42px"></span>
      <div style="min-width:0">
        <h2>${esc(m.model_name)}</h2>
        <div class="dh-meta">
          <span class="tag brand">${esc(protocolLabel(m.protocol))}</span>
          <span class="tag">${cands.length} 候选</span>
          ${start ? `<span class="tag ok">起点 ${esc(start.upstream_name)}</span>` : '<span class="tag bad">无可用起点</span>'}
          ${m.forward_to ? `<span class="tag warn">↦ ${esc(m.forward_to)}</span>` : ""}
        </div>
      </div>
    </div>

    <div class="dh-stats">
      <div class="dh-stat"><span>窗口请求</span><b>${fmtInt(usage?.n || 0)}</b></div>
      <div class="dh-stat"><span>平均耗时</span><b>${fmtMs(usage?.avg || 0)}</b></div>
      <div class="dh-stat"><span>P95</span><b>${fmtMs(usage?.p95 || 0)}</b></div>
      <div class="dh-stat"><span>输出 Tokens</span><b>${fmtCompact(usage?.to || 0)}</b></div>
    </div>

    <div class="section-label">候选链 · 自动降级顺序</div>
    <div class="cand-list">${candHtml}</div>

    <div class="section-label">重命名</div>
    <div class="form-grid">
      <div class="form-row">
        <label>客户端看到的名字
          <input class="input" id="rename-input" value="${esc(m.model_name)}" autocomplete="off">
        </label>
        <button type="button" class="btn" data-act="rename">保存</button>
      </div>
      <p class="form-note">旧名字立刻不再暴露，客户端要改用新名字。候选、首选、顺序和交接关系原地保留。</p>
    </div>

    <div class="section-label">整条链交接</div>
    ${m.forward_to ? `
      <div class="forward-chip" style="margin-bottom:8px">↦ ${esc(m.forward_to)}</div>
      <button type="button" class="btn btn-sm" data-act="unforward">取消交接，恢复自己的链</button>
      <p class="form-note" style="margin-top:8px">交接后请求直接按目标的候选顺序走，自己的候选只是暂时不用。</p>`
    : others.length ? `
      <div class="form-grid">
        <div class="form-row">
          <label>交给同一接口下的另一个模型
            <select class="input wide" id="forward-select" style="width:100%;padding:0 8px">
              ${others.map((o) => `<option value="${esc(o.model_name)}">${esc(o.model_name)}</option>`).join("")}
            </select>
          </label>
          <button type="button" class="btn" data-act="forward">交接</button>
        </div>
        <p class="form-note">目标必须在本接口下有可用候选链，否则请求会 404。可以接着往下一环交，但不能绕圈。</p>
      </div>`
    : `<p class="form-note">这个接口下还没有别的模型可以接手。</p>`}

    <div class="danger-zone">
      <b>删除这条链</b>
      <p>候选与首选一起删除，客户端立刻看不到 ${esc(m.model_name)}。只是暂时不用时，先停用上游分组。</p>
      <button type="button" class="btn btn-sm btn-danger" data-act="delete-chain">删除整条链</button>
    </div>
  `);

  $("#drawer-body").onclick = (e) => {
    const btn = e.target.closest("[data-act]");
    if (!btn) return;
    handleModelAction(m, btn.dataset.act, Number(btn.dataset.route || 0));
  };
}

async function handleModelAction(model, act, routeId) {
  const cands = model.candidates || [];
  if (act === "prefer") {
    await switchRoute(routeId);
    openModelDrawer(model);
    return;
  }
  if (act === "up" || act === "down") {
    const idx = cands.findIndex((c) => c.route_id === routeId);
    const swap = act === "up" ? idx - 1 : idx + 1;
    if (idx < 0 || swap < 0 || swap >= cands.length) return;
    const order = cands.map((c) => c.route_id);
    [order[idx], order[swap]] = [order[swap], order[idx]];
    await runAction(
      () => api("/models/order", { method: "POST", body: { model_name: model.model_name, order } }),
      "顺序已更新",
    );
    await Promise.all([loadModels(), loadOverview({ quiet: true })]);
    openModelDrawer(model);
    return;
  }
  if (act === "remove") {
    const last = cands.length <= 1 && !model.forward_to;
    const ok = await confirmDialog(
      last ? "移除最后一个候选？" : "移除这个候选？",
      last
        ? `${model.model_name} 没有别的候选了，移除后它会从客户端列表里消失。`
        : `${model.model_name} 将不再经过这条候选。其他候选不受影响。`,
      last ? "移除并下线" : "移除",
    );
    if (!ok) return;
    await runAction(() => api(`/models?route_id=${routeId}`, { method: "DELETE" }), "候选已移除");
    await Promise.all([loadModels(), loadOverview({ quiet: true })]);
    const still = S.models.find((x) => x.model_name === model.model_name && x.protocol === model.protocol);
    if (still) {
      openModelDrawer(still);
    } else {
      S.selectedModel = null;
      renderGalaxyView();
      closeDrawer();
      toast("模型已下线", `${model.model_name} 没有剩余候选，客户端列表里已经看不到它`, "info");
    }
    return;
  }
  if (act === "rename") {
    const input = $("#rename-input");
    const next = input?.value.trim();
    if (!next || next === model.model_name) return;
    await runAction(
      () => api("/models/rename", { method: "POST", body: { model_name: model.model_name, protocol: model.protocol, new_name: next } }),
      "已重命名，客户端需要改用新名字",
    );
    S.selectedModel = null;
    await Promise.all([loadModels(), loadOverview({ quiet: true })]);
    closeDrawer();
    return;
  }
  if (act === "forward") {
    const target = $("#forward-select")?.value;
    if (!target) return;
    await runAction(
      () => api("/models/forward", { method: "POST", body: { model_name: model.model_name, protocol: model.protocol, target_model: target } }),
      `已交给 ${target}`,
    );
    await loadModels();
    openModelDrawer(model);
    return;
  }
  if (act === "unforward") {
    await runAction(
      () => api(`/models/forward?model_name=${encodeURIComponent(model.model_name)}&protocol=${encodeURIComponent(model.protocol)}`, { method: "DELETE" }),
      "已取消交接",
    );
    await loadModels();
    openModelDrawer(model);
    return;
  }
  if (act === "delete-chain") {
    const ok = await confirmDialog(
      "删除整条链？",
      `${model.model_name}（${protocolLabel(model.protocol)}）的 ${cands.length} 个候选会一起删除，客户端立刻看不到它。`,
      "删除",
    );
    if (!ok) return;
    await runAction(
      () => api(`/models?model_name=${encodeURIComponent(model.model_name)}&protocol=${encodeURIComponent(model.protocol)}`, { method: "DELETE" }),
      "整条链已删除",
    );
    S.selectedModel = null;
    await Promise.all([loadModels(), loadOverview({ quiet: true })]);
    closeDrawer();
  }
}

function openNewModelDrawer() {
  const groups = [];
  for (const u of S.upstreams) {
    if (!u.enabled) continue;
    for (const g of u.groups || []) {
      if (g.enabled) groups.push({ ...g, upstream_name: u.name });
    }
  }
  if (!groups.length) {
    toast("没有可用的分组", "先在「上游站点」启用一个分组，再回来创建模型", "err");
    return;
  }
  const protos = [...new Set(groups.map((g) => g.protocol))];

  openDrawer("NEW DOWNSTREAM MODEL", `
    <div class="dh-title">
      <div>
        <h2 style="font-size:18px">新增一个下游模型</h2>
        <div class="dh-meta"><span class="tag">创建后立刻对客户端暴露</span></div>
      </div>
    </div>
    <div class="form-grid" style="margin-top:18px">
      <label class="field">客户端看到的名字
        <input class="input wide" id="nm-name" placeholder="例如 my-chat" autocomplete="off">
        <span class="hint">这个名字以后只改显示名就行，不用动客户端配置里的地址。同名且同接口已存在时，会为它新增一条候选。</span>
      </label>
      <label class="field">接口
        <select class="select" id="nm-proto">
          ${protos.map((p) => `<option value="${esc(p)}">${esc(protocolLabel(p))}</option>`).join("")}
        </select>
        <span class="hint">按客户端使用的接口选。同名模型在另一种接口下是另一条链。</span>
      </label>
      <label class="field">挂到哪个分组
        <select class="select" id="nm-group"></select>
      </label>
      <label class="field">上游真实模型名
        <input class="input wide" id="nm-remote" list="nm-remote-list" placeholder="留空则与上面的名字相同" autocomplete="off">
        <datalist id="nm-remote-list"></datalist>
        <span class="hint">必须是这个站真实支持的名字；不确定就从候选里选一个。</span>
      </label>
      <button type="button" class="btn btn-primary" data-create-model style="height:38px;margin-top:4px">创建并暴露</button>
      <p class="form-note">创建后可以在详情里继续加候选、调顺序或设首选。接口与分组必须匹配。</p>
    </div>
  `);

  const protoSel = $("#nm-proto");
  const groupSel = $("#nm-group");
  const remoteList = $("#nm-remote-list");
  const fillGroups = () => {
    const p = protoSel.value;
    const matched = groups.filter((g) => g.protocol === p);
    groupSel.innerHTML = matched.map((g) => `<option value="${g.id}">${esc(g.upstream_name)} / ${esc(g.name)}</option>`).join("");
    fillRemote();
  };
  const fillRemote = () => {
    const g = groups.find((x) => String(x.id) === groupSel.value);
    remoteList.innerHTML = (g?.models || []).map((m) => `<option value="${esc(m)}"></option>`).join("");
  };
  protoSel.onchange = fillGroups;
  groupSel.onchange = fillRemote;
  fillGroups();

  $("#drawer-body").onclick = async (e) => {
    if (!e.target.closest("[data-create-model]")) return;
    const name = $("#nm-name").value.trim();
    const remote = $("#nm-remote").value.trim();
    const groupId = Number(groupSel.value);
    const protocol = protoSel.value;
    if (!name) {
      toast("先起一个名字", "客户端列表里显示的就是它", "err");
      $("#nm-name").focus();
      return;
    }
    if (!groupId) {
      toast("没有可用的分组", "这个接口下没有启用中的分组", "err");
      return;
    }
    const btn = e.target.closest("[data-create-model]");
    btn.classList.add("is-busy");
    try {
      await api("/models", { method: "POST", body: { model_name: name, group_id: groupId, remote_model: remote } });
      toast("模型已创建", `${name} 现在会出现在客户端列表里`);
      await Promise.all([loadModels(), loadOverview({ quiet: true })]);
      const created = S.models.find((m) => m.model_name === name && m.protocol === protocol);
      S.selectedModel = { model_name: name, protocol };
      renderGalaxyView();
      if (created) openModelDrawer(created); else closeDrawer();
    } catch (err) {
      toast("创建没有完成", err.message, "err");
      btn.classList.remove("is-busy");
    }
  };
}

async function switchRoute(routeId) {  if (!routeId) return;
  await runAction(() => api("/models/switch", { method: "POST", body: { route_id: routeId } }), "首选已切换，新请求生效");
  await Promise.all([loadModels(), loadOverview({ quiet: true })]);
}

async function runAction(fn, success) {
  try {
    await fn();
    if (success) toast(success);
  } catch (err) {
    toast("操作没有完成", err.message, "err");
  }
}

/* ------------------------------------------------------------------ 上游视图 */

const EGRESS_TEXT = { "": "跟随系统", system: "跟随系统", direct: "直连" };

function renderUpstreams() {
  const host = $("#upstream-grid");
  if (!host) return;
  const q = S.upstreamQuery.trim().toLowerCase();
  const list = S.upstreams.filter((u) => {
    if (!q) return true;
    const hay = [u.name, u.base_url].concat((u.groups || []).map((g) => g.name)).join(" ").toLowerCase();
    return hay.includes(q);
  });
  const count = $("#upstream-count");
  if (count) count.textContent = `${list.length} / ${S.upstreams.length} 个站`;

  if (!list.length) {
    host.innerHTML = `<div class="empty" style="grid-column:1/-1"><span class="e-mark">▦</span><b>${S.upstreams.length ? "没有匹配的站点" : "还没有上游站点"}</b>
      <span>${S.upstreams.length ? "换个关键词试试。" : "在原管理页的「上游站点」里添加第一个供应商和分组。"}</span></div>`;
    return;
  }

  const health = new Map((S.overview?.upstreams || []).map((r) => [r.name, r]));

  host.innerHTML = list.map((u, i) => {
    const h = health.get(u.name);
    const groups = u.groups || [];
    const initial = (u.name || "?").trim().slice(0, 1).toUpperCase();
    const egress = EGRESS_TEXT[u.egress_kind] || "代理";
    return `
    <article class="station ${u.enabled ? "" : "is-off"} ${S.upstreamFocus === u.id ? "is-focus" : ""}" style="--i:${i}" data-station="${u.id}">
      <header class="station-head">
        <span class="station-mark">${esc(initial)}</span>
        <div class="station-id">
          <h3>${esc(u.name)}</h3>
          <a href="#" data-copy="${esc(u.base_url)}" title="点击复制站点地址">${esc(u.base_url)}</a>
        </div>
        <label class="switch" title="${u.enabled ? "停用这个站" : "启用这个站"}">
          <input type="checkbox" data-up="${u.id}" ${u.enabled ? "checked" : ""} aria-label="启用 ${esc(u.name)}">
          <i></i>
        </label>
      </header>
      <div class="station-meta">
        <span class="tag">${esc(egress)}</span>
        ${(u.supports || []).map((p) => `<span class="tag">${esc(protocolShort(p))}</span>`).join("")}
        ${h ? `<span class="tag ${h.ok_rate >= 0.95 ? "ok" : h.ok_rate >= 0.8 ? "warn" : "bad"}">健康 ${fmtPct(h.ok_rate, 1)}</span>` : ""}
        <span class="tag">${groups.length} 组</span>
      </div>
      <div class="group-list">
        ${groups.length ? groups.map((g) => `
          <div class="group-row ${g.enabled ? "" : "is-off"}">
            <span class="bar p-${esc(g.protocol)}"></span>
            <b title="${esc(g.name)}">${esc(g.name)}</b>
            <span class="gproto">${esc(protocolShort(g.protocol))}</span>
            <span class="gkey mono">${esc(g.key_masked || "无 key")}</span>
            <span class="gcount">${(g.models || []).length} 模型</span>
            <label class="switch is-sm" title="${g.enabled ? "停用分组" : "启用分组"}">
              <input type="checkbox" data-group="${g.id}" ${g.enabled ? "checked" : ""} aria-label="启用分组 ${esc(g.name)}">
              <i></i>
            </label>
          </div>`).join("") : `<div class="group-row" style="grid-template-columns:1fr;color:var(--dim)">这个站还没有分组</div>`}
      </div>
      <footer class="station-foot">
        <button type="button" class="btn btn-sm" data-probe="${u.id}">测一下</button>
        <span class="sig">${h ? `${fmtInt(h.n)} 次 · P95 ${fmtMs(h.p95)}` : "当前窗口无记录"}</span>
      </footer>
    </article>`;
  }).join("");

  host.onclick = (e) => {
    const copy = e.target.closest("[data-copy]");
    if (copy) {
      e.preventDefault();
      copyText(copy.dataset.copy);
      return;
    }
    const probe = e.target.closest("[data-probe]");
    if (probe) probeUpstream(Number(probe.dataset.probe), probe);
  };
  host.onchange = async (e) => {
    const up = e.target.closest("input[data-up]");
    if (up) {
      await runAction(
        () => api(`/upstreams/${up.dataset.up}/enabled`, { method: "POST", body: { enabled: up.checked } }),
        up.checked ? "站点已启用" : "站点已停用",
      );
      await Promise.all([loadUpstreams(), loadModels(), loadOverview({ quiet: true })]);
      return;
    }
    const group = e.target.closest("input[data-group]");
    if (group) {
      await runAction(
        () => api(`/groups/${group.dataset.group}/enabled`, { method: "POST", body: { enabled: group.checked } }),
        group.checked ? "分组已启用" : "分组已停用",
      );
      await Promise.all([loadUpstreams(), loadModels(), loadOverview({ quiet: true })]);
    }
  };

  if (S.upstreamFocus && S._upstreamScrolledFor !== S.upstreamFocus) {
    const card = host.querySelector(`[data-station="${S.upstreamFocus}"]`);
    if (card) {
      S._upstreamScrolledFor = S.upstreamFocus;
      setTimeout(() => card.scrollIntoView({ behavior: "smooth", block: "center" }), 80);
    }
  }
}

async function probeUpstream(id, btn) {
  const label = btn.textContent;
  btn.classList.add("is-busy");
  btn.textContent = "探测中…";
  try {
    const r = await api(`/upstreams/${id}/probe`, { method: "POST" });
    const results = r.results || [];
    const ok = results.some((x) => x.ok);
    const detail = results.map((x) => `${x.label} ${x.ok ? `通 ${x.status}` : "不通"} · ${fmtMs(x.ms)}`).join("；");
    toast(ok ? "网络已通" : "三扇门都不通", detail, ok ? "ok" : "err");
  } catch (err) {
    toast("探测失败", err.message, "err");
  } finally {
    btn.classList.remove("is-busy");
    btn.textContent = label;
  }
}

/* ------------------------------------------------------------------ 实时视图 */

function renderLive() {
  const calls = S.inflight?.calls || [];
  const recent = S.inflight?.recent || [];
  const counts = S.inflight?.counts || { requests: 0, streams: 0 };
  const summary = $("#live-summary");
  const grid = $("#live-grid");
  const recentHost = $("#live-recent");
  if (!grid) return;

  const connect = calls.filter((c) => c.phase === "connect").length;
  const wait = calls.filter((c) => c.phase === "wait").length;
  if (summary) {
    summary.innerHTML = `
      <div class="lsum"><span>正在执行</span><b>${fmtInt(counts.requests || 0)}<small>条</small></b></div>
      <div class="lsum"><span>流式返回</span><b>${fmtInt(counts.streams || 0)}<small>条</small></b></div>
      <div class="lsum"><span>等上游回应</span><b>${fmtInt(connect)}<small>条</small></b></div>
      <div class="lsum"><span>已回应等内容</span><b>${fmtInt(wait)}<small>条</small></b></div>`;
  }

  const visible = S.liveFocus ? calls.filter((c) => c.model === S.liveFocus) : calls;
  if (!visible.length) {
    grid.innerHTML = S.liveFocus
      ? `<div class="empty" style="grid-column:1/-1;padding:34px">
          <span class="e-mark">◉</span><b>「${esc(S.liveFocus)}」当前没有进行中的请求</b>
          <span>清掉筛选可以看到全部请求。</span>
          <button type="button" class="btn btn-sm" data-clear-focus>显示全部</button>
        </div>`
      : `<div class="live-idle">
          <div class="orb"><span class="orb-ring"></span><span class="orb-ring"></span><span class="orb-ring"></span><span class="orb-core"></span></div>
          <h3>链路静默</h3>
          <p>客户端发起请求后，卡片会立刻出现在这里。每 2 秒刷新一次，结束后的记录保留 90 秒。</p>
          <div class="live-idle-meta">
            <span><b>${fmtInt(counts.streams || 0)}</b> 流式</span>
            <span><b>${fmtInt(recent.length)}</b> 刚结束</span>
            <span><b>${fmtInt(S.overview?.totals?.p95 || 0)}</b> ms P95</span>
          </div>
        </div>`;
    grid.onclick = (e) => {
      if (e.target.closest("[data-clear-focus]")) { S.liveFocus = null; renderLive(); }
    };
  } else {
    grid.innerHTML = visible.map((c) => {
      const phase = PHASE_META[c.phase] || PHASE_META.connect;
      const stepIdx = c.phase === "stream" ? 2 : c.phase === "wait" ? 1 : 0;
      const steps = [0, 1, 2].map((i) => `<span class="step ${i < stepIdx ? "done" : i === stepIdx ? "now" : ""}"></span>`).join("");
      return `
      <article class="live-card ${phase.cls}">
        <div class="lc-head">
          <span>${esc(c.client || "未知客户端")}</span>
          <span class="tag">${esc(protocolShort(c.protocol))}</span>
          <span class="tag ${c.attempt > 1 ? "warn" : ""}">第 ${c.attempt || 1} 次尝试</span>
        </div>
        <div class="lc-model" title="${esc(c.model)}">${esc(c.model)}</div>
        <div class="lc-up"><i>→</i><b>${esc(c.upstream || "连接中…")}</b><span style="color:var(--dim);font-family:var(--mono);font-size:9.5px">${esc(c.remote_model || "")}</span></div>
        <div class="lc-phase">${steps}</div>
        <div class="lc-phase-label"><span>连接</span><span>等首字</span><span>返回</span></div>
        <div class="lc-stats">
          <span><b class="js-elapsed" data-ms="${c.elapsed_ms || 0}" data-at="${performance.now()}">${fmtMs(c.elapsed_ms || 0)}</b>耗时</span>
          <span><b>${fmtBytes(c.sent || 0)}</b>已接收</span>
          <span><b>${fmtCompact((c.tokens_in || 0) + (c.tokens_out || 0))}</b>Tokens</span>
          ${c.thinking ? '<span class="lc-thinking"><i></i><i></i><i></i>思维链</span>' : ""}
        </div>
        <div class="lc-foot">
          <span class="trail">${esc((c.trail || []).slice(-2).join(" → ") || phase.note)}</span>
          <button type="button" class="btn btn-sm btn-danger" data-cancel="${c.id}">中断</button>
        </div>
      </article>`;
    }).join("");
    grid.onclick = async (e) => {
      const btn = e.target.closest("[data-cancel]");
      if (!btn) return;
      btn.classList.add("is-busy");
      await runAction(() => api(`/inflight/${btn.dataset.cancel}/cancel`, { method: "POST" }), "已请求中断，等待上游断开");
      await loadInflight({ quiet: true });
    };
  }

  if (recentHost) {
    recentHost.className = "recent-list";
    recentHost.innerHTML = recent.length ? recent.map((c) => `
      <div class="recent-row">
        <span class="rt">${esc(c.client || "—")}</span>
        <span class="rm">${esc(c.model || "—")}</span>
        <span class="ru">${esc(c.upstream || "—")}</span>
        <span class="rn">${esc(NOTE_TEXT[c.note] || c.note || (c.status ? `HTTP ${c.status}` : "结束"))}</span>
        <span class="rd">${fmtMs(c.elapsed_ms || 0)}</span>
        <span class="rn">${statusClass(c) === "ok" ? '<span class="status-pill ok">完成</span>' : '<span class="status-pill bad">异常</span>'}</span>
      </div>`).join("") : `<div class="empty" style="padding:26px"><b>还没有刚结束的请求</b><span>结束后的请求会在这里保留 90 秒。</span></div>`;
  }
}

/* ------------------------------------------------------------------ 记录视图 */

function renderLog() {
  const body = $("#log-body");
  const empty = $("#log-empty");
  if (!body) return;
  const q = S.logQuery.trim().toLowerCase();
  const rows = S.requests.filter((r) => {
    if (S.logProtocol !== "all" && r.protocol !== S.logProtocol) return false;
    const cls = statusClass(r);
    if (S.logStatus === "ok" && cls !== "ok") return false;
    if (S.logStatus === "bad" && cls === "ok") return false;
    if (S.logStatus === "2xx" && !(r.status >= 200 && r.status < 300)) return false;
    if (S.logStatus === "4xx" && !(r.status >= 400 && r.status < 500)) return false;
    if (S.logStatus === "5xx" && !(r.status >= 500)) return false;
    if (!q) return true;
    return [r.client, r.model, r.upstream, r.note, r.remote_model, r.group_name, String(r.status)]
      .join(" ").toLowerCase().includes(q);
  });

  const count = $("#log-count");
  if (count) count.textContent = `筛出 ${rows.length} / ${S.requests.length} 条`;

  if (!rows.length) {
    body.innerHTML = "";
    if (empty) {
      empty.hidden = false;
      empty.innerHTML = `<b>${S.requests.length ? "没有匹配的记录" : "还没有转发记录"}</b>${S.requests.length ? "调整筛选条件试试。" : "客户端发出第一条请求后，这里会记录下来。"}`;
    }
    return;
  }
  if (empty) empty.hidden = true;

  body.innerHTML = rows.slice(0, 200).map((r) => {
    const cls = statusClass(r);
    return `
    <tr data-req="${r.id}">
      <td class="t-time">${esc(fmtClock(r.ts))}</td>
      <td class="t-client">${esc(r.client || "—")}</td>
      <td class="t-model" title="${esc(r.model)}">${esc(r.model || "—")}</td>
      <td class="t-up">${esc(r.upstream || "—")}</td>
      <td><span class="status-pill ${cls}">${r.status || "—"}</span></td>
      <td class="t-num">${fmtMs(r.duration_ms)}</td>
      <td class="t-num">${r.input_tokens ? fmtCompact(r.input_tokens) : "—"}</td>
      <td class="t-num">${r.output_tokens ? fmtCompact(r.output_tokens) : "—"}</td>
      <td class="t-note">${esc(NOTE_TEXT[r.note] || r.note || "")}</td>
    </tr>`;
  }).join("");

  body.onclick = (e) => {
    const tr = e.target.closest("[data-req]");
    if (!tr) return;
    const row = S.requests.find((r) => String(r.id) === tr.dataset.req);
    if (row) openLogDrawer(row);
  };
}

function openLogDrawer(r) {
  const cls = statusClass(r);
  openDrawer("REQUEST DETAIL", `
    <div class="dh-title">
      <div>
        <h2>#${esc(r.id)}</h2>
        <div class="dh-meta">
          <span class="status-pill ${cls}">${r.status || "—"} ${esc(NOTE_TEXT[r.note] || r.note || "")}</span>
          <span class="tag brand">${esc(protocolLabel(r.protocol))}</span>
          ${r.attempt > 1 ? `<span class="tag warn">第 ${r.attempt} 次尝试</span>` : ""}
        </div>
      </div>
    </div>

    <div class="dh-stats">
      <div class="dh-stat"><span>耗时</span><b>${fmtMs(r.duration_ms)}</b></div>
      <div class="dh-stat"><span>输入</span><b>${fmtCompact(r.input_tokens || 0)}</b></div>
      <div class="dh-stat"><span>输出</span><b>${fmtCompact(r.output_tokens || 0)}</b></div>
      <div class="dh-stat"><span>缓存读</span><b>${fmtCompact(r.cached_tokens || 0)}</b></div>
    </div>

    <div class="section-label">这次尝试</div>
    <div class="detail-grid">
      ${detailRow("时间", r.ts)}
      ${detailRow("客户端", r.client)}
      ${detailRow("下游模型", r.model, true)}
      ${detailRow("上游站点", r.upstream)}
      ${detailRow("分组", r.group_name)}
      ${detailRow("上游真名", r.remote_model || "（未记录）", true)}
      ${detailRow("请求体", fmtBytes(r.req_bytes))}
      ${detailRow("内嵌数据", fmtBytes(r.req_blob_bytes || 0))}
      ${detailRow("响应体", fmtBytes(r.resp_bytes))}
      ${detailRow("正文内容", fmtBytes(r.resp_text_bytes || 0))}
      ${detailRow("思维链", r.thinking ? "有" : "无")}
      ${detailRow("加密推理", fmtBytes(r.encrypted_bytes || 0))}
      ${detailRow("流式", r.stream ? "是" : "否")}
      ${detailRow("尝试序号", String(r.attempt || 1))}
    </div>
    <p class="form-note" style="margin-top:10px">HTTP 200 也可能是截断。判定「正常结束」看的是备注和是否有完整收尾，不只看状态码。</p>
  `);
  $("#drawer-body").onclick = null;
}

function detailRow(label, value, mono = false) {
  return `<div class="detail-row"><span>${esc(label)}</span><b class="${mono ? "mono" : ""}">${esc(value == null || value === "" ? "—" : value)}</b></div>`;
}

/* ------------------------------------------------------------------ 设置视图 */

function renderSettings() {
  const protoHost = $("#protocol-switches");
  if (protoHost) {
    protoHost.innerHTML = S.protocols.map((p) => {
      const on = S.switches?.enabled?.[p.name] !== false;
      return `
      <div class="setting-row">
        <span class="sr-icon">${esc(PROTO_BADGE[p.name] || (p.short || p.name).slice(0, 3).toUpperCase())}</span>
        <div class="sr-main">
          <b>${esc(p.label)}</b>
          <span>${esc(p.path)} · ${esc(p.client || "")}</span>
        </div>
        <label class="switch" title="${on ? "停用" : "启用"}">
          <input type="checkbox" data-switch="${esc(p.name)}" ${on ? "checked" : ""} aria-label="启用 ${esc(p.label)}">
          <i></i>
        </label>
      </div>`;
    }).join("");
    protoHost.onchange = async (e) => {
      const input = e.target.closest("input[data-switch]");
      if (!input) return;
      await runAction(
        () => api("/protocol-switches", { method: "POST", body: { protocol: input.dataset.switch, enabled: input.checked } }),
        input.checked ? "接口已启用" : "接口已停用，相关模型和历史一起隐藏",
      );
      await Promise.all([loadSettings(), loadModels(), loadUpstreams(), loadOverview({ quiet: true })]);
    };
  }

  const failHost = $("#failover-settings");
  if (failHost) {
    failHost.innerHTML = S.protocols.map((p) => {
      const on = Boolean(S.failover?.enabled?.[p.name]);
      return `
      <div class="setting-row">
        <span class="sr-icon">↯</span>
        <div class="sr-main">
          <b>${esc(p.short || p.label)} 自动换站</b>
          <span>失败时按候选顺序继续试</span>
        </div>
        <label class="switch" title="${on ? "关闭" : "开启"}">
          <input type="checkbox" data-failover="${esc(p.name)}" ${on ? "checked" : ""} aria-label="${esc(p.label)} 自动换站">
          <i></i>
        </label>
      </div>`;
    }).join("");
    failHost.onchange = async (e) => {
      const input = e.target.closest("input[data-failover]");
      if (!input) return;
      await runAction(
        () => api("/failover", { method: "POST", body: { protocol: input.dataset.failover, enabled: input.checked } }),
        input.checked ? "已开启自动换站" : "已关闭自动换站",
      );
      await loadSettings();
      await loadModels();
    };
  }

  const stallRow = $("#stall-row");
  if (stallRow) {
    stallRow.innerHTML = `
      <label for="stall-input">发呆超时</label>
      <input class="input" id="stall-input" type="number" min="0" max="3600" step="1" value="${Number(S.stall) || 0}">
      <span class="hint">秒 · 0 表示关闭。只中断当前请求，不记为故障。</span>
      <button type="button" class="btn" data-stall-save style="margin-left:auto">保存</button>`;
    stallRow.querySelector("[data-stall-save]").addEventListener("click", async () => {
      const value = Number($("#stall-input").value);
      if (!Number.isFinite(value) || value < 0 || value > 3600) {
        toast("数值不合法", "请输入 0 到 3600 之间的秒数", "err");
        return;
      }
      await runAction(() => api("/stall-timeout", { method: "PUT", body: { seconds: value } }), value ? `发呆超时设为 ${value} 秒` : "发呆超时已关闭");
      await loadSettings();
    });
  }

  const accessHost = $("#access-list");
  if (accessHost) {
    const base = baseUrl();
    const rows = [
      { key: "openai", label: "OpenAI Responses", client: "Codex", url: `${base}/v1` },
      { key: "openai-chat", label: "OpenAI Chat Completions", client: "OpenAI SDK / opencode", url: `${base}/v1` },
      { key: "anthropic", label: "Anthropic Messages", client: "Claude Code", url: base },
    ].filter((r) => S.switches?.enabled?.[r.key] !== false);

    accessHost.innerHTML = rows.map((r) => `
      <button type="button" class="access-row" data-copy-url="${esc(r.url)}">
        <div class="ar-main"><b>${esc(r.label)} · ${esc(r.client)}</b><code>${esc(r.url)}</code></div>
        <svg viewBox="0 0 24 24" aria-hidden="true"><path d="M9.6 14.4 14.4 9.6M7.2 12 4.8 14.4a3.4 3.4 0 0 0 4.8 4.8L12 16.8M12 7.2l2.4-2.4a3.4 3.4 0 0 1 4.8 4.8L16.8 12"/></svg>
      </button>`).join("")
      + `<p class="access-note">模型名填「模型」页里起的名字，API Key 填任意非空占位值（例如 <code class="mono">local-only</code>）。Claude Code 的地址不要加 <code class="mono">/v1</code>，它会自己补。</p>`;
    accessHost.onclick = (e) => {
      const row = e.target.closest("[data-copy-url]");
      if (row) copyText(row.dataset.copyUrl);
    };
  }
}

/* ------------------------------------------------------------------ 命令面板 */

let paletteItems = [];
let paletteIndex = 0;

function buildPalette() {
  const items = [];
  for (const [key, meta] of Object.entries(VIEW_META)) {
    items.push({ group: "视图", ico: meta.ico, label: meta.title, hint: "跳转", run: () => setView(key), search: key });
  }
  for (const m of S.models) {
    items.push({
      group: "模型",
      ico: "◇",
      label: m.model_name,
      hint: protocolShort(m.protocol),
      search: `${m.model_name} ${m.protocol} ${(m.candidates || []).map((c) => c.upstream_name).join(" ")}`,
      run: () => { setView("models"); openModelDrawer(m); },
    });
  }
  for (const u of S.upstreams) {
    items.push({
      group: "上游",
      ico: "▦",
      label: u.name,
      hint: u.base_url.replace(/^https?:\/\//, ""),
      search: `${u.name} ${u.base_url}`,
      run: () => setView("upstreams"),
    });
  }
  items.push(
    { group: "操作", ico: "⧉", label: "复制 OpenAI 接入地址", hint: `${baseUrl()}/v1`, run: () => copyText(`${baseUrl()}/v1`) },
    { group: "操作", ico: "⧉", label: "复制 Claude Code 地址", hint: baseUrl(), run: () => copyText(baseUrl()) },
    { group: "操作", ico: "◐", label: "切换明暗主题", hint: "外观", run: () => applyTheme(document.documentElement.dataset.theme === "light" ? "dark" : "light") },
    { group: "操作", ico: "↻", label: "刷新全部数据", hint: "同步", run: () => refreshAll({ manual: true }) },
    { group: "操作", ico: "⚙", label: "打开网关设置", hint: "设置", run: () => setView("settings") },
  );
  return items;
}

function openPalette() {
  paletteItems = buildPalette();
  paletteIndex = 0;
  $("#palette").hidden = false;
  const input = $("#palette-input");
  input.value = "";
  renderPalette("");
  setTimeout(() => input.focus(), 20);
}

function closePalette() {
  $("#palette").hidden = true;
}

function renderPalette(query) {
  const q = query.trim().toLowerCase();
  const scored = paletteItems
    .map((item) => {
      const hay = `${item.group} ${item.label} ${item.search || ""} ${item.hint || ""}`.toLowerCase();
      if (!q) return { item, score: 1 };
      const idx = hay.indexOf(q);
      if (idx < 0) return null;
      return { item, score: idx === 0 ? 3 : hay.split(/\s+/).some((w) => w.startsWith(q)) ? 2 : 1 };
    })
    .filter(Boolean)
    .sort((a, b) => b.score - a.score);

  const list = scored.map((s) => s.item);
  const host = $("#palette-results");
  if (!list.length) {
    host.innerHTML = `<div class="palette-empty">没有找到「${esc(query)}」</div>`;
    return;
  }
  if (paletteIndex >= list.length) paletteIndex = 0;

  let lastGroup = "";
  host.innerHTML = list.map((item, i) => {
    const group = item.group !== lastGroup ? `<div class="pr-group">${esc(item.group)}</div>` : "";
    lastGroup = item.group;
    return `${group}<button type="button" class="pr-item ${i === paletteIndex ? "is-cur" : ""}" data-pi="${i}">
      <span class="pr-ico">${esc(item.ico)}</span>
      <span class="pr-main"><b>${esc(item.label)}</b></span>
      <span class="pr-hint">${esc(item.hint || "")}</span>
    </button>`;
  }).join("");

  host.onclick = (e) => {
    const btn = e.target.closest("[data-pi]");
    if (!btn) return;
    const item = list[Number(btn.dataset.pi)];
    if (!item) return;
    closePalette();
    item.run();
  };
  host._list = list;

  const cur = host.querySelector(".is-cur");
  if (cur) cur.scrollIntoView({ block: "nearest" });
}

function movePalette(delta) {
  const host = $("#palette-results");
  const list = host._list || [];
  if (!list.length) return;
  paletteIndex = (paletteIndex + delta + list.length) % list.length;
  host.querySelectorAll(".pr-item").forEach((el, i) => el.classList.toggle("is-cur", i === paletteIndex));
  const cur = host.querySelector(".is-cur");
  if (cur) cur.scrollIntoView({ block: "nearest" });
}

/* ------------------------------------------------------------------ 启动 */

function bindEvents() {
  $("#scrim").addEventListener("click", closeDrawer);
  $("#drawer-close").addEventListener("click", closeDrawer);
  $("#palette-btn").addEventListener("click", openPalette);
  $("[data-close-palette]").addEventListener("click", closePalette);

  $("#palette-input").addEventListener("input", (e) => { paletteIndex = 0; renderPalette(e.target.value); });
  $("#palette-input").addEventListener("keydown", (e) => {
    if (e.key === "ArrowDown") { e.preventDefault(); movePalette(1); }
    else if (e.key === "ArrowUp") { e.preventDefault(); movePalette(-1); }
    else if (e.key === "Enter") {
      e.preventDefault();
      const list = $("#palette-results")._list || [];
      const item = list[paletteIndex];
      if (item) { closePalette(); item.run(); }
    }
  });

  document.addEventListener("keydown", (e) => {
    const mod = e.ctrlKey || e.metaKey;
    if (mod && e.key.toLowerCase() === "k") {
      e.preventDefault();
      if ($("#palette").hidden) openPalette(); else closePalette();
    }
    if (e.key === "Escape") {
      if (!$("#modal").hidden) return;
      if (!$("#palette").hidden) closePalette();
      else if (drawerOpen) closeDrawer();
    }
  });

  const debounce = (fn, ms) => {
    let t;
    return (...args) => { clearTimeout(t); t = setTimeout(() => fn(...args), ms); };
  };

  $("#model-search").addEventListener("input", debounce((e) => {
    S.modelQuery = e.target.value;
    renderModels();
  }, 110));
  $("#upstream-search").addEventListener("input", debounce((e) => {
    S.upstreamQuery = e.target.value;
    renderUpstreams();
  }, 110));
  $("#log-search").addEventListener("input", debounce((e) => {
    S.logQuery = e.target.value;
    renderLog();
  }, 110));
  $("#log-protocol").addEventListener("change", (e) => { S.logProtocol = e.target.value; renderLog(); });
  $("#log-status").addEventListener("change", (e) => { S.logStatus = e.target.value; renderLog(); });
  $("#new-model-btn").addEventListener("click", openNewModelDrawer);
  $("#log-clear").addEventListener("click", async () => {
    const ok = await confirmDialog("清空转发记录？", `将删除 ${S.requests.length} 条历史记录。模型配置、路由和用量统计的累计值不受影响。`, "清空");
    if (!ok) return;
    await runAction(() => api("/requests", { method: "DELETE" }), "记录已清空");
    await Promise.all([loadRequests(), loadOverview({ quiet: true })]);
  });
  $("#shutdown-btn").addEventListener("click", async () => {
    const ok = await confirmDialog("关闭网关？", "所有转发会立刻停止，网页随之失去控制面。托盘模式下可以再次启动。", "关闭网关");
    if (!ok) return;
    await runAction(() => api("/shutdown", { method: "POST" }), "关闭请求已发送");
    setOnline(false);
    $("#runtime-state").textContent = "正在关闭";
  });
}

function startPolling() {
  setInterval(() => {
    if (document.hidden) return;
    loadInflight({ quiet: true });
  }, 2500);

  setInterval(() => {
    if (document.hidden || S.view !== "overview") return;
    loadOverview({ quiet: true });
  }, 12000);

  setInterval(() => {
    if (document.hidden || S.view !== "log") return;
    loadRequests({ quiet: true });
  }, 10000);

  setInterval(() => {
    if (document.hidden) return;
    loadModels({ quiet: true });
    loadUpstreams({ quiet: true });
  }, 30000);

  setInterval(() => {
    $$(".js-elapsed").forEach((el) => {
      const base = Number(el.dataset.ms) || 0;
      const at = Number(el.dataset.at) || performance.now();
      el.textContent = fmtMs(base + (performance.now() - at));
    });
  }, 250);
}

async function boot() {
  initTheme();
  initField();
  initModal();
  bindNav();
  bindEvents();

  const hash = location.hash.replace("#", "");
  const initial = VIEW_META[hash] ? hash : "overview";
  await loadFoundation();
  await Promise.allSettled([
    loadOverview(),
    loadModels(),
    loadUpstreams(),
    loadInflight(),
    loadRequests(),
  ]);
  S.booted = true;
  setView(initial, { push: false });
  requestAnimationFrame(() => {
    moveSegPill();
    if (initial === "overview") drawChart(true);
  });
  renderFooter();
  startPolling();
}

boot();
