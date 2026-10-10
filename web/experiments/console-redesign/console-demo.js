'use strict';
/* 控制台改版样稿：只用页面内演示数据，不请求管理接口、不调用供应商。 */

// ---------- 小工具 ----------
const $ = (s, r = document) => r.querySelector(s);
const $$ = (s, r = document) => [...r.querySelectorAll(s)];
const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
const clamp = (v, a, b) => Math.max(a, Math.min(b, v));
const rand = (a, b) => a + Math.random() * (b - a);
const randInt = (a, b) => Math.floor(rand(a, b + 1));
const pick = arr => arr[Math.floor(Math.random() * arr.length)];
const now = () => Date.now();

function seeded(seed) {
  let s = seed >>> 0 || 1;
  return () => { s ^= s << 13; s >>>= 0; s ^= s >>> 17; s ^= s << 5; s >>>= 0; return s / 4294967296; };
}
function hash(str) {
  let h = 2166136261;
  for (const c of str) { h ^= c.codePointAt(0); h = Math.imul(h, 16777619); }
  return h >>> 0;
}
function fmtDur(ms) {
  if (ms == null) return '—';
  if (ms < 1000) return Math.round(ms) + 'ms';
  if (ms < 60000) return (ms / 1000).toFixed(1) + 's';
  return Math.floor(ms / 60000) + 'm' + String(Math.floor(ms % 60000 / 1000)).padStart(2, '0') + 's';
}
const fmtK = n => n >= 1000 ? (n / 1000).toFixed(n >= 10000 ? 0 : 1) + 'k' : String(Math.round(n));
const pad2 = n => String(n).padStart(2, '0');
function fmtClock(t) { const d = new Date(t); return `${pad2(d.getHours())}:${pad2(d.getMinutes())}:${pad2(d.getSeconds())}`; }
function fmtDate(t) { const d = new Date(t); return `${d.getFullYear()}-${pad2(d.getMonth() + 1)}-${pad2(d.getDate())} ${fmtClock(t)}`; }
function fmtCountdown(ms) { const s = Math.max(0, Math.ceil(ms / 1000)); return `${Math.floor(s / 60)}:${pad2(s % 60)}`; }

let toastTimer;
function toast(text) {
  const el = $('#toast');
  el.textContent = text;
  el.classList.add('show');
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => el.classList.remove('show'), 2400);
}
async function copyText(text) {
  try { await navigator.clipboard.writeText(text); }
  catch {
    const ta = document.createElement('textarea');
    ta.value = text; ta.style.position = 'fixed'; ta.style.opacity = '0';
    document.body.appendChild(ta); ta.select();
    try { document.execCommand('copy'); } finally { ta.remove(); }
  }
}

const ICONS = {
  routes: 'M5 18V7a2 2 0 0 1 2-2h11M14 2l4 3-4 3M5 18l4-4m-4 4-4-4',
  flow: 'M4 6h4v4H4zM4 14h4v4H4zM16 10h4v4h-4zM8 8c4 0 4 4 8 4M8 16c4 0 4-4 8-4',
  live: 'M3 12h4l3-8 4 16 3-8h4',
  log: 'M8 6h13M8 12h13M8 18h13M3 6h.01M3 12h.01M3 18h.01',
  upstreams: 'M4 5h16v5H4zM4 14h16v5H4zM8 7.5h.01M8 16.5h.01',
  settings: 'M12 9a3 3 0 1 0 0 6 3 3 0 0 0 0-6ZM12 2v3M12 19v3M2 12h3M19 12h3M5 5l2 2M17 17l2 2M19 5l-2 2M7 17l-2 2',
  arrow: 'M5 12h14M13 6l6 6-6 6',
};
const svgIcon = (name, cls = '') => `<svg class="${cls}" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="${ICONS[name]}"/></svg>`;

// ---------- 演示数据 ----------
const PROTOS = {
  responses: { label: 'Responses', path: '/v1/responses', client: 'codex' },
  chat: { label: 'Chat', path: '/v1/chat/completions', client: 'opencode' },
  messages: { label: 'Messages', path: '/v1/messages', client: 'claude-code' },
};
const PROTO_KEYS = Object.keys(PROTOS);
const protoBadge = (p, off = false) => `<span class="proto${off ? ' off' : ''}" data-p="${p}">${PROTOS[p].label}</span>`;

const sites = [
  { id: 'A', name: '站点A', base: 'https://api.site-a.example', egress: '跟随系统代理', enabled: true, groups: [
    { id: 'A1', name: '默认', proto: 'responses', key: 'sk-…a91f', models: ['gpt-5.6-luna', 'gpt-5.6-sol', 'gpt-5.4', 'o5-mini'], enabled: true },
    { id: 'A2', name: 'claude', proto: 'messages', key: 'sk-…77c2', models: ['claude-opus-4-5', 'claude-sonnet-4-5', 'claude-opus-4-5[1m]'], enabled: true },
  ] },
  { id: 'B', name: '站点B', base: 'https://relay.site-b.example', egress: '直连', enabled: true, groups: [
    { id: 'B1', name: '默认', proto: 'responses', key: 'sk-…0b3e', models: ['gpt-5.6-luna', 'deepseek-v3'], enabled: true },
    { id: 'B2', name: '备用key', proto: 'responses', key: 'sk-…e410', models: ['gpt-5.6-luna', 'gpt-5.4'], enabled: true },
    { id: 'B3', name: 'chat', proto: 'chat', key: 'sk-…9d21', models: ['deepseek-v3', 'qwen3-max'], enabled: true },
  ] },
  { id: 'C', name: '站点C', base: 'https://gw.site-c.example', egress: '指定代理 127.0.0.1:7890', enabled: true, groups: [
    { id: 'C1', name: '默认', proto: 'responses', key: 'sk-…5aa8', models: ['deepseek-v3', 'gpt-5.4'], enabled: true },
    { id: 'C2', name: 'anthropic', proto: 'messages', key: 'sk-…c06d', models: ['claude-sonnet-4-5', 'claude-opus-4-5'], enabled: true },
    { id: 'C3', name: 'chat', proto: 'chat', key: '', models: ['deepseek-v3'], enabled: false },
  ] },
];
const GROUPS = new Map();
for (const site of sites) for (const group of site.groups) GROUPS.set(group.id, { site, group });
const G = id => GROUPS.get(id);
const groupLabel = id => { const x = G(id); return x ? `${x.site.name} · ${x.group.name}` : id; };

// 每个分组的演示失败率与延迟，用来驱动模拟流量和健康指标
const GROUP_PROFILE = { A1: [0.04, 2100], A2: [0.05, 3400], B1: [0.12, 1800], B2: [0.32, 2600], B3: [0.08, 1500], C1: [0.08, 2900], C2: [0.06, 3900], C3: [0.2, 4000] };
const health = new Map();
for (const [id, [fail, p95]] of Object.entries(GROUP_PROFILE)) {
  health.set(id, { failRate: fail, p95, ok: 0, total: 0, streak: 0, lastError: '' });
}
const cooldowns = new Map([['B2', now() + 84000]]);
health.get('B2').streak = 0;
health.get('B2').lastError = '429 Too Many Requests';

let candSeq = 0;
const cand = (group, remote) => ({ id: 'c' + (++candSeq), group, remote });
const routes = [
  { name: 'my-chat', proto: 'responses', candidates: [cand('A1', 'gpt-5.6-luna'), cand('B2', 'gpt-5.6-luna'), cand('C1', 'deepseek-v3')], requests: 1240 },
  { name: 'coder-main', proto: 'responses', candidates: [cand('A1', 'gpt-5.6-sol'), cand('B1', 'gpt-5.6-luna')], requests: 612 },
  { name: 'deepseek-v3', proto: 'responses', candidates: [cand('B1', 'deepseek-v3'), cand('C1', 'deepseek-v3')], requests: 388 },
  { name: 'fast-mini', proto: 'responses', candidates: [cand('A1', 'o5-mini')], requests: 205 },
  { name: 'alias-chat', proto: 'responses', candidates: [cand('C1', 'gpt-5.4')], forward: 'my-chat', requests: 63 },
  { name: 'claude-opus-4-5', proto: 'messages', candidates: [cand('A2', 'claude-opus-4-5'), cand('C2', 'claude-opus-4-5')], requests: 540 },
  { name: 'claude-sonnet-4-5', proto: 'messages', candidates: [cand('C2', 'claude-sonnet-4-5'), cand('A2', 'claude-sonnet-4-5')], requests: 296 },
  { name: 'long-context', proto: 'messages', candidates: [cand('A2', 'claude-opus-4-5[1m]')], requests: 41 },
  { name: 'my-chat', proto: 'chat', candidates: [cand('B3', 'deepseek-v3'), cand('C3', 'deepseek-v3')], requests: 193 },
  { name: 'old-alias', proto: 'chat', candidates: [], forward: 'gone-model', requests: 0 },
];
for (const r of routes) initRoute(r);
function initRoute(r) {
  r.key = r.proto + '|' + r.name;
  r.preferred = r.candidates[0]?.id ?? null;
  r.forward = r.forward ?? null;
  const rnd = seeded(hash(r.key));
  const base = r.requests / 60;
  r.series = Array.from({ length: 60 }, (_, i) => {
    const wave = 0.6 + 0.4 * Math.sin(i / 7 + rnd() * 2);
    const total = r.requests ? Math.max(0, Math.round(base * wave * (0.5 + rnd()) + (rnd() < 0.06 ? base * 2 : 0))) : 0;
    const fails = total && rnd() < 0.1 ? Math.min(total, 1 + Math.floor(rnd() * 3)) : 0;
    return { total, fails };
  });
}

const settings = {
  protocols: { responses: true, chat: true, messages: true },
  failover: { responses: true, chat: true, messages: false },
  stallTimeout: 60,
  simulate: true,
};

// ---------- 路由规则（样稿内的简化版本） ----------
const findRoute = (proto, name) => routes.find(r => r.proto === proto && r.name === name);
const isCooling = gid => (cooldowns.get(gid) ?? 0) > now();
function candState(c) {
  const x = G(c.group);
  if (!x || !x.group.enabled || !x.site.enabled) return 'off';
  if (isCooling(c.group)) return 'cool';
  return 'ok';
}
// 顺着交接链找到真正负责路由的模型；目标不存在或成环都算断链
function resolveRoute(r) {
  const seen = new Set();
  let cur = r;
  while (cur.forward) {
    if (seen.has(cur.key)) return { route: null, broken: 'loop' };
    seen.add(cur.key);
    const next = findRoute(cur.proto, cur.forward);
    if (!next) return { route: null, broken: 'missing', missing: cur.forward };
    cur = next;
  }
  return { route: cur, broken: null };
}
const preferredCand = r => r.candidates.find(c => c.id === r.preferred) ?? r.candidates[0] ?? null;
// 下一次请求的起点：首选可用就用首选；开启自动换站时冷却/停用的候选后移
function attemptOrder(r) {
  const cs = r.candidates;
  if (!cs.length) return [];
  const pi = Math.max(0, cs.findIndex(c => c.id === r.preferred));
  const rotated = cs.slice(pi).concat(cs.slice(0, pi));
  if (!settings.failover[r.proto]) return candState(rotated[0]) === 'off' ? [] : [rotated[0]];
  const ok = rotated.filter(c => candState(c) === 'ok');
  const cool = rotated.filter(c => candState(c) === 'cool');
  return ok.concat(cool);
}
const nextStart = r => attemptOrder(r)[0] ?? null;
function routeStatus(r) {
  if (!settings.protocols[r.proto]) return { level: 'off', text: '接口已关闭' };
  if (r.forward) {
    const res = resolveRoute(r);
    if (res.broken === 'missing') return { level: 'err', text: `交接目标 ${res.missing} 不存在` };
    if (res.broken) return { level: 'err', text: '交接链成环' };
    const s = routeStatus(res.route);
    return s.level === 'err' ? s : { level: 'fwd', text: `→ ${r.forward}` };
  }
  if (!r.candidates.length) return { level: 'err', text: '没有候选' };
  const start = nextStart(r);
  if (!start) return { level: 'err', text: '没有可用候选' };
  if (candState(start) === 'cool') return { level: 'warn', text: `候选都在冷却，仍会尝试 ${groupLabel(start.group)}` };
  const pref = preferredCand(r);
  if (start.id !== pref.id) return { level: 'warn', text: `首选不可用，从 ${groupLabel(start.group)} 开始` };
  if (r.candidates.some(c => candState(c) === 'cool')) return { level: 'warn', text: '有候选在冷却' };
  return { level: 'ok', text: `${groupLabel(start.group)} · ${start.remote}` };
}

// ---------- 模拟流量 ----------
const ERRORS = {
  429: 'Too Many Requests: 当前分组请求过多，请稍后再试',
  500: 'Internal Server Error: upstream returned an unexpected error',
  502: 'Bad Gateway: upstream connect error or disconnect before headers',
  503: 'Service Unavailable: model is at capacity, please retry later',
};
const CLIENT_EXTRA = { responses: ['codex', 'codex', 'cursor'], chat: ['opencode', 'cherry-studio'], messages: ['claude-code'] };
const live = [];
const records = [];
let reqSeq = 0;

// 一次请求的计划：按尝试顺序生成每段时间；失败、无内容、换站都写进计划
function buildPlan(route, { forHistory = false } = {}) {
  const res = resolveRoute(route);
  if (!res.route) return null;
  const order = attemptOrder(res.route);
  if (!order.length) return null;
  const attempts = [];
  for (let i = 0; i < order.length; i++) {
    const c = order[i];
    const prof = health.get(c.group) ?? { failRate: 0.05, p95: 2500 };
    const failRate = candState(c) === 'cool' ? 0.6 : prof.failRate;
    const canRetry = i < order.length - 1 && settings.failover[res.route.proto];
    if (Math.random() < failRate) {
      const code = pick([429, 429, 429, 502, 503, 500]);
      attempts.push({ group: c.group, remote: c.remote, code, outcome: 'fail', error: `${code} ${ERRORS[code]}`, segs: [{ k: 'wait', d: rand(250, 1400) }] });
      if (canRetry) continue;
      break;
    }
    const segs = [{ k: 'wait', d: rand(250, 1300) }, { k: 'ttft', d: rand(200, Math.min(2200, prof.p95 * 0.7)) }];
    const tps = randInt(38, 165);
    let outcome = 'ok', error = '', code = 200;
    if (Math.random() < 0.14) {
      segs.push({ k: 'stream', d: rand(1200, 4000), tps });
      let stall = rand(7000, 24000);
      if (settings.stallTimeout > 0 && stall > settings.stallTimeout * 1000) {
        stall = settings.stallTimeout * 1000;
        segs.push({ k: 'stall', d: stall });
        outcome = 'stall';
        error = `无内容超时：${settings.stallTimeout} 秒没有收到新内容，已断开这一次尝试`;
        attempts.push({ group: c.group, remote: c.remote, code, outcome, error, segs, tps });
        if (canRetry) continue;
        break;
      }
      segs.push({ k: 'stall', d: stall });
      segs.push({ k: 'stream', d: rand(1500, 5000), tps });
    } else {
      segs.push({ k: 'stream', d: forHistory ? rand(2000, 22000) : rand(2500, 15000), tps });
    }
    attempts.push({ group: c.group, remote: c.remote, code, outcome, error, segs, tps });
    break;
  }
  const proto = route.proto;
  const tokensIn = randInt(1800, 46000);
  return {
    id: 'r' + (++reqSeq),
    client: pick(CLIENT_EXTRA[proto]),
    proto, model: route.name, via: route.forward ? res.route.name : null,
    attempts, tokensIn, cache: Math.round(tokensIn * rand(0.2, 0.85)),
    total: attempts.reduce((n, a) => n + a.segs.reduce((m, s) => m + s.d, 0), 0),
  };
}
const attemptDur = a => a.segs.reduce((n, s) => n + s.d, 0);
function streamedTokens(a, elapsedInAttempt) {
  let off = 0, out = 0;
  for (const s of a.segs) {
    const vis = clamp(elapsedInAttempt - off, 0, s.d);
    if (s.k === 'stream') out += (a.tps * vis) / 1000;
    off += s.d;
  }
  return Math.round(out);
}
function finalOutcome(plan) {
  const last = plan.attempts[plan.attempts.length - 1];
  if (last.outcome === 'ok') return { status: 'ok', code: 200 };
  if (last.outcome === 'stall') return { status: 'error', code: 'stall' };
  return { status: 'error', code: last.code };
}
// 记录：转发记录页和抽屉共用的形状
function planToRecord(plan, t0, cancelledAt = null) {
  const end = cancelledAt ? cancelledAt - t0 : plan.total;
  let off = 0;
  const attempts = [];
  for (const a of plan.attempts) {
    if (off >= end) break;
    const d = attemptDur(a);
    const cut = off + d > end;
    attempts.push({ group: a.group, remote: a.remote, code: cut ? null : a.code, outcome: cut ? 'cancel' : a.outcome, error: cut ? '' : a.error, dur: Math.min(d, end - off), tps: a.tps, ttft: a.segs[0].d + (a.segs[1]?.k === 'ttft' ? a.segs[1].d : 0) });
    off += d;
  }
  const last = plan.attempts[attempts.length - 1];
  const lastOff = attempts.slice(0, -1).reduce((n, a) => n + a.dur, 0);
  const out = last ? streamedTokens(last, end - lastOff) : 0;
  const fo = cancelledAt ? { status: 'cancel', code: '中断' } : finalOutcome(plan);
  const okAtt = attempts.find(a => a.outcome === 'ok' || a.outcome === 'cancel');
  return {
    id: plan.id, t: t0, client: plan.client, proto: plan.proto, model: plan.model, via: plan.via,
    attempts, status: fo.status, code: fo.code, dur: end,
    ttft: okAtt ? lastOff + okAtt.ttft : null,
    tokens: { in: plan.tokensIn, out, cache: plan.cache, estimated: fo.status !== 'ok' },
  };
}
// 健康与冷却：同一分组连续失败 2 次冷却 90 秒（样稿不做翻倍）
function applyAttemptResult(a) {
  const h = health.get(a.group);
  if (!h) return;
  h.total++;
  if (a.outcome === 'ok') { h.ok++; h.streak = 0; return; }
  h.streak++;
  h.lastError = a.outcome === 'stall' ? '无内容超时' : `${a.code} ${ERRORS[a.code]?.split(':')[0] ?? ''}`;
  if (h.streak >= 2) { cooldowns.set(a.group, now() + 90000); h.streak = 0; onStateChange(); }
}
function successRate(gid) {
  const h = health.get(gid);
  if (!h || !h.total) return 1 - (h?.failRate ?? 0);
  return h.ok / h.total;
}

function spawnRequest() {
  const pool = routes.filter(r => settings.protocols[r.proto] && r.requests > 0 && resolveRoute(r).route);
  const totalW = pool.reduce((n, r) => n + r.requests, 0);
  if (!totalW) return;
  let x = Math.random() * totalW;
  const route = pool.find(r => (x -= r.requests) <= 0) ?? pool[0];
  const plan = buildPlan(route);
  if (!plan) return;
  route.requests++;
  route.series[59].total++;
  live.unshift({ plan, t0: now(), applied: 0, cancelledAt: null, done: false, doneAt: null, record: null });
}
function cancelLive(id) {
  const req = live.find(x => x.plan.id === id);
  if (!req || req.done) return;
  req.cancelledAt = now();
  finishLive(req);
  toast('已中断这条请求，其他请求不受影响');
}
function finishLive(req) {
  req.done = true;
  req.doneAt = now();
  req.record = planToRecord(req.plan, req.t0, req.cancelledAt);
  records.unshift(req.record);
  if (req.record.status !== 'ok') { const r = findRoute(req.plan.proto, req.plan.model); if (r) r.series[59].fails++; }
  newRecordIds.add(req.record.id);
}
const newRecordIds = new Set();
function simTick() {
  const t = now();
  for (const req of live) {
    if (req.done) continue;
    const el = t - req.t0;
    let off = 0;
    req.plan.attempts.forEach((a, i) => {
      off += attemptDur(a);
      if (i >= req.applied && el >= off) { applyAttemptResult(a); req.applied = i + 1; }
    });
    if (el >= req.plan.total) finishLive(req);
  }
  for (let i = live.length - 1; i >= 0; i--) {
    if (live[i].done && t - live[i].doneAt > 90000) live.splice(i, 1);
  }
}
// 历史记录：用同一套计划生成过去两小时的转发记录
function seedHistory() {
  const pool = routes.filter(r => r.requests > 0);
  let t = now() - 2 * 3600 * 1000;
  const items = [];
  while (t < now() - 20000) {
    t += rand(30000, 150000);
    const totalW = pool.reduce((n, r) => n + r.requests, 0);
    let x = Math.random() * totalW;
    const route = pool.find(r => (x -= r.requests) <= 0) ?? pool[0];
    const plan = buildPlan(route, { forHistory: true });
    if (!plan) continue;
    const cancelled = Math.random() < 0.03 ? t + plan.total * rand(0.2, 0.8) : null;
    items.push(planToRecord(plan, t, cancelled));
  }
  records.push(...items.reverse());
}

// ---------- 页面状态 ----------
const VIEWS = [
  { id: 'routes', label: '路由', title: '模型路由' },
  { id: 'flow', label: '关系图', title: '流量关系图' },
  { id: 'live', label: '实时', title: '实时请求' },
  { id: 'log', label: '记录', title: '转发记录' },
  { id: 'upstreams', label: '上游', title: '上游站点' },
  { id: 'settings', label: '设置', title: '网关设置' },
];
const ui = {
  view: 'routes', selected: routes[0].key, routeQuery: '', routeProto: 'all',
  flowProto: 'all', flowFocus: null, liveFilter: 'all', logStatus: 'all', logProto: 'all', logQuery: '',
  drawer: null, dragging: false, dlgMode: null,
};
const reduceMotion = matchMedia('(prefers-reduced-motion: reduce)');

// 每秒刷新的区域只在内容真的变了才改 DOM，避免点击落在重绘中间被吞掉
function setHTML(el, html) {
  if (el._html === html) return;
  el._html = html;
  el.innerHTML = html;
}
function renderRail() {
  const liveCount = live.filter(x => !x.done).length;
  setHTML($('#rail'), VIEWS.map((v, i) => `${i === 5 ? '<span class="rail-sep"></span>' : ''}
    <button class="rail-item" data-go="${v.id}" ${ui.view === v.id ? 'aria-current="page"' : ''} title="${v.title}">
      ${svgIcon(v.id)}<span>${v.label}</span>${v.id === 'live' && liveCount ? `<span class="rail-badge">${liveCount}</span>` : ''}
    </button>`).join(''));
}
function renderStatusbar() {
  const running = live.filter(x => !x.done).length;
  const fo = PROTO_KEYS.filter(p => settings.failover[p]).length;
  setHTML($('#statusbar'), `
    <button class="sb-item" data-go="live"><span class="dot ${running ? 'ok pulse' : 'off'}"></span>${running} 进行中</button>
    <span class="sb-sep"></span>
    <button class="sb-item" data-go="settings" title="接口开关">${PROTO_KEYS.map(p => `<span style="color:${settings.protocols[p] ? `var(--p-${p})` : 'var(--faint)'}">${PROTOS[p].label} ${settings.protocols[p] ? '✓' : '✕'}</span>`).join(' ')}</button>
    <span class="sb-sep"></span>
    <button class="sb-item" data-go="settings">自动换站 ${fo === 3 ? '开' : fo === 0 ? '关' : `${fo}/3 接口`}</button>
    <button class="sb-item" data-go="settings">无内容超时 ${settings.stallTimeout ? settings.stallTimeout + 's' : '关'}</button>
    <span class="sb-sep"></span>
    <span class="sb-item">冷却中 ${[...cooldowns.keys()].filter(isCooling).length} 个分组</span>
    <span class="grow"></span>
    <span class="sb-item faint">样稿 · 演示数据，不连接网关</span>
    <button class="sb-item mono" data-act="copy-endpoint" title="复制 OpenAI 兼容接入地址">http://127.0.0.1:8317/v1</button>`);
}
function setView(id, { push = true } = {}) {
  if (!VIEWS.some(v => v.id === id)) id = 'routes';
  ui.view = id;
  for (const sec of $$('.view')) sec.hidden = sec.dataset.view !== id;
  if (push && location.hash !== '#' + id) history.replaceState(null, '', '#' + id);
  renderRail();
  renderView();
  $('#content').scrollTop = 0;
}
function renderView() {
  ({ routes: renderRoutes, flow: renderFlow, live: renderLive, log: renderLog, upstreams: renderUpstreams, settings: renderSettings })[ui.view]();
}
function onStateChange() {
  if (ui.dragging) return;
  renderStatusbar();
  if (ui.view === 'routes') { renderModelList(); renderDetail(); }
  else if (ui.view !== 'live' && ui.view !== 'log') renderView();
}

// ---------- 模型路由 ----------
function sparkline(series, color) {
  const pts = series.slice(-30).map(x => x.total);
  const max = Math.max(1, ...pts);
  const xy = pts.map((v, i) => [i * (64 / 29), 18 - (v / max) * 15]);
  const line = xy.map(([x, y]) => `${x.toFixed(1)},${y.toFixed(1)}`).join(' ');
  return `<svg class="spark" viewBox="0 0 64 20" aria-hidden="true"><path d="M0,20 L${line.replace(/ /g, ' L')} L64,20 Z" fill="${color}"/><polyline points="${line}" stroke="${color}"/></svg>`;
}
function renderRoutes() {
  const sec = $('.view[data-view="routes"]');
  if (!sec.dataset.ready) {
    sec.dataset.ready = '1';
    sec.innerHTML = `
      <header class="view-head">
        <div><h1>模型路由</h1><p class="sub" id="route-summary"></p></div><span class="grow"></span>
        <button class="btn btn-ghost" data-act="go-flow">看整体关系图</button>
        <button class="btn" data-act="new-route">＋ 新增模型</button>
      </header>
      <div class="route-layout">
        <section class="panel model-list" aria-label="模型列表">
          <div class="list-tools">
            <input type="search" class="search" id="route-q" placeholder="搜索模型、站点或上游真名…" aria-label="筛选模型">
            <div class="chips" id="route-proto"></div>
          </div>
          <div class="list-head" aria-hidden="true"><span></span><span>模型 / 下次起点</span><span>接口</span><span>近 30 分</span></div>
          <div class="model-scroll" id="model-list" role="listbox" aria-label="模型"></div>
          <div class="list-foot" id="route-foot"></div>
        </section>
        <section class="detail" id="route-detail" aria-live="polite"></section>
      </div>`;
    $('#route-q').addEventListener('input', e => { ui.routeQuery = e.target.value; renderModelList(); });
  }
  renderModelList();
  renderDetail();
}
function renderModelList() {
  const q = ui.routeQuery.trim().toLowerCase();
  const counts = { all: routes.length };
  for (const p of PROTO_KEYS) counts[p] = routes.filter(r => r.proto === p).length;
  $('#route-proto').innerHTML = ['all', ...PROTO_KEYS].map(p => `<button class="chip" data-route-proto="${p}" aria-pressed="${ui.routeProto === p}">${p === 'all' ? '全部' : PROTOS[p].label}<span class="n">${counts[p]}</span></button>`).join('');
  const list = routes.filter(r => (ui.routeProto === 'all' || r.proto === ui.routeProto) &&
    `${r.name} ${r.forward ?? ''} ${r.candidates.map(c => groupLabel(c.group) + ' ' + c.remote).join(' ')}`.toLowerCase().includes(q));
  const issues = routes.filter(r => routeStatus(r).level === 'err').length;
  const warn = routes.filter(r => routeStatus(r).level === 'warn').length;
  $('#route-summary').textContent = `${routes.length} 个下游模型 · ${routes.reduce((n, r) => n + r.candidates.length, 0)} 条候选 · ${issues ? issues + ' 个需要处理' : '全部可用'}${warn ? ` · ${warn} 个已自动绕开` : ''}`;
  $('#route-foot').innerHTML = `<span>显示 ${list.length} / ${routes.length}</span><span class="grow"></span><span class="faint">↑↓ 切换 · 拖卡片排序</span>`;
  if (!list.some(r => r.key === ui.selected) && list[0]) ui.selected = list[0].key;
  $('#model-list').innerHTML = list.length ? list.map(r => {
    const st = routeStatus(r);
    const color = `var(--p-${r.proto})`;
    return `<button class="model-row${st.level === 'off' ? ' disabled' : ''}" role="option" data-route="${esc(r.key)}" aria-selected="${r.key === ui.selected}">
      <span class="dot ${st.level}"></span>
      <span style="min-width:0"><span class="model-name">${esc(r.name)}</span><span class="model-sub">${esc(st.text)}</span></span>
      ${protoBadge(r.proto, !settings.protocols[r.proto])}
      ${sparkline(r.series, color)}
    </button>`;
  }).join('') : '<p class="empty">没有匹配的模型</p>';
}

function candCard(r, c, i, start, pref) {
  const st = candState(c);
  const h = health.get(c.group);
  const isStart = start && start.id === c.id;
  const isPref = pref && pref.id === c.id;
  const cd = cooldowns.get(c.group);
  const stateLine = st === 'cool'
    ? `<span class="cand-state cool">冷却中 <span data-cd="${c.group}">${fmtCountdown(cd - now())}</span> · 跳过</span>`
    : st === 'off' ? '<span class="cand-state off">分组已停用</span>' : '';
  return `<div class="cand${isStart ? ' is-start' : ''}${st === 'cool' ? ' is-cool' : ''}${st === 'off' ? ' is-off' : ''}" draggable="true" data-cand="${c.id}" tabindex="0"
      aria-label="候选 ${i + 1}：${esc(groupLabel(c.group))} ${esc(c.remote)}">
    ${isStart ? '<span class="start-flag">下次请求起点</span>' : ''}
    <div class="cand-top"><span class="cand-idx">${i + 1}</span><span class="dot ${st}"></span><span class="grow">${esc(groupLabel(c.group))}</span>${isPref ? '<span class="tag accent">首选</span>' : ''}</div>
    <div class="cand-remote" title="上游真实模型名">${esc(c.remote)}</div>
    <div class="cand-metrics">
      <div class="metric"><b>${(successRate(c.group) * 100).toFixed(0)}%</b><span>成功率</span></div>
      <div class="metric"><b>${fmtDur(h?.p95)}</b><span>P95 耗时</span></div>
      ${stateLine}
    </div>
    <div class="cand-actions">
      <button class="btn btn-ghost btn-sm" data-move="-1" data-cid="${c.id}" ${i === 0 ? 'disabled' : ''} aria-label="前移">◂</button>
      <button class="btn btn-ghost btn-sm" data-move="1" data-cid="${c.id}" ${i === r.candidates.length - 1 ? 'disabled' : ''} aria-label="后移">▸</button>
      <span class="grow"></span>
      ${isPref ? '' : `<button class="btn btn-ghost btn-sm" data-prefer="${c.id}">设为首选</button>`}
      <button class="btn btn-danger btn-sm" data-remove="${c.id}" aria-label="移除候选">✕</button>
    </div>
  </div>`;
}
function seriesChart(series) {
  const W = 600, H = 120, pad = 18, bw = (W - 4) / series.length;
  const max = Math.max(1, ...series.map(x => x.total));
  const bars = series.map((x, i) => {
    const h = (x.total / max) * (H - pad - 6), fh = (x.fails / max) * (H - pad - 6);
    const xx = 2 + i * bw;
    return `<rect class="bar" x="${xx.toFixed(1)}" y="${(H - pad - h).toFixed(1)}" width="${(bw - 2).toFixed(1)}" height="${h.toFixed(1)}" rx="1.5"/>` +
      (x.fails ? `<rect class="bar-fail" x="${xx.toFixed(1)}" y="${(H - pad - fh).toFixed(1)}" width="${(bw - 2).toFixed(1)}" height="${Math.max(2, fh).toFixed(1)}" rx="1.5"/>` : '');
  }).join('');
  return `<svg class="chart" viewBox="0 0 ${W} ${H}" preserveAspectRatio="none" aria-hidden="true">${bars}
    <line class="axis" x1="0" x2="${W}" y1="${H - pad + 0.5}" y2="${H - pad + 0.5}"/>
    <text x="2" y="${H - 4}">60 分钟前</text><text x="${W / 2}" y="${H - 4}" text-anchor="middle">30 分钟前</text><text x="${W - 2}" y="${H - 4}" text-anchor="end">现在</text></svg>`;
}
function recentRows(list) {
  if (!list.length) return '<p class="empty" style="padding:16px">还没有请求</p>';
  return list.map(rec => `<button class="recent-row" data-record="${rec.id}">
    <span class="dim num">${fmtClock(rec.t)}</span>${codeCell(rec)}
    <span class="up">${esc(groupLabel(rec.attempts.at(-1)?.group))}${rec.attempts.length > 1 ? ` · 第 ${rec.attempts.length} 次尝试` : ''}</span>
    <span class="dim num">${fmtDur(rec.dur)}</span></button>`).join('');
}
function codeCell(rec) {
  if (rec.status === 'running') return '<span class="code" style="color:var(--accent)">…</span>';
  if (rec.status === 'cancel') return '<span class="code warn">中断</span>';
  if (rec.code === 'stall') return '<span class="code err">超时</span>';
  return `<span class="code ${rec.status === 'ok' ? 'ok' : 'err'}">${rec.code}</span>`;
}
function renderDetail() {
  const box = $('#route-detail');
  const r = routes.find(x => x.key === ui.selected);
  if (!r) { box.innerHTML = '<div class="panel empty">选择左侧的模型查看候选链</div>'; return; }
  const st = routeStatus(r);
  const start = nextStart(r), pref = preferredCand(r);
  const fails = r.series.reduce((n, x) => n + x.fails, 0), total = r.series.reduce((n, x) => n + x.total, 0);
  let callout = '';
  if (st.level === 'off') callout = `<div class="callout warn"><span class="dot warn"></span><div>${PROTOS[r.proto].label} 接口已在设置里关闭：客户端调用会被拒绝，配置保留。<button class="link" data-go="settings">去打开</button></div></div>`;
  else if (st.level === 'err') callout = `<div class="callout err"><span class="dot err"></span><div><b>${esc(st.text)}</b>：客户端调用这个模型会失败。${r.forward ? '改指另一个模型或取消交接。' : '添加候选或启用对应分组。'}</div></div>`;
  else if (st.level === 'warn') callout = `<div class="callout warn"><span class="dot warn"></span><div>${esc(st.text)}。${settings.failover[r.proto] ? '自动换站已开启，冷却结束后回到首选。' : ''}</div></div>`;
  if (r.proto in settings.failover && !settings.failover[r.proto] && r.candidates.length > 1 && !r.forward)
    callout += `<div class="callout info"><span class="dot fwd"></span><div>${PROTOS[r.proto].label} 的自动换站已关闭：只使用起点，失败不会尝试后面的候选。</div></div>`;

  let chain;
  if (r.forward) {
    const res = resolveRoute(r);
    const target = findRoute(r.proto, r.forward);
    chain = `<div class="fwd-card${res.broken ? ' broken' : ''}">
      <div class="grow"><div class="dim" style="font-size:12px">整条链已交给</div><div class="big">→ ${esc(r.forward)}${res.broken === 'missing' ? '（不存在）' : ''}</div>
        ${target ? `<div class="mini-chain">${target.candidates.map((c, i) => `<span>${i + 1}. ${esc(groupLabel(c.group))} · ${esc(c.remote)}</span>`).join('') || '<span>目标没有候选</span>'}</div>` : ''}
        <div class="dim" style="font-size:12px;margin-top:8px">自己的 ${r.candidates.length} 条候选保留但不参与路由。</div></div>
      ${target ? `<button class="btn btn-ghost btn-sm" data-route="${esc(target.key)}">打开 ${esc(target.name)}</button>` : ''}
      <button class="btn btn-ghost btn-sm" data-act="forward">改指</button>
      <button class="btn btn-danger btn-sm" data-act="unforward">取消交接</button></div>`;
  } else {
    const arrow = `<span class="lane-arrow">${svgIcon('arrow')}</span>`;
    chain = `<div class="lane" id="lane">${r.candidates.map((c, i) => candCard(r, c, i, start, pref)).join(arrow)}${r.candidates.length ? arrow : ''}
      <button class="cand add" data-act="add-cand">＋ 添加候选<span class="faint" style="font-size:11.5px">同接口的上游分组</span></button></div>`;
  }
  const mine = records.filter(x => x.proto === r.proto && x.model === r.name).slice(0, 7);
  box.innerHTML = `
    <div class="panel">
      <div class="detail-head">
        <span class="dot ${st.level}"></span><h2>${esc(r.name)}</h2>${protoBadge(r.proto, !settings.protocols[r.proto])}
        <span class="path mono">POST ${PROTOS[r.proto].path}</span><span class="grow"></span>
        ${r.forward ? '' : '<button class="btn btn-ghost btn-sm" data-act="forward">整条链交给…</button>'}
        <button class="btn btn-ghost btn-sm" data-act="copy-model">复制模型名</button>
      </div>
      <div class="panel-body" style="padding-top:0;display:flex;flex-direction:column;gap:12px">
        ${callout}
        <div>
          <div class="section-title"><h3>${r.forward ? '路由去向' : '候选链'}</h3><span class="hint">${r.forward ? '交接中的模型按目标的候选顺序和自动换站走' : '从左到右依次尝试 · 拖动卡片或用 ◂ ▸ 调整顺序 · 首选只改起点，不改顺序'}</span></div>
          ${chain}
        </div>
      </div>
    </div>
    <div class="detail-grid">
      <section class="panel"><div class="panel-head"><h2>最近 60 分钟</h2><span class="grow"></span>
        <span class="legend"><span><i style="background:var(--accent)"></i>请求 ${total}</span><span><i style="background:var(--err)"></i>失败 ${fails}</span></span></div>
        <div class="panel-body">${seriesChart(r.series)}</div></section>
      <section class="panel"><div class="panel-head"><h2>最近请求</h2><span class="grow"></span><button class="link" data-act="log-for-model">在转发记录里查看 →</button></div>
        <div class="panel-body" style="padding-top:4px;padding-bottom:4px">${recentRows(mine)}</div></section>
    </div>`;
}

// ---------- 关系图：客户端 → 下游模型 → 上游分组，自动布局 ----------
function renderFlow() {
  const sec = $('.view[data-view="flow"]');
  const rs = routes.filter(r => ui.flowProto === 'all' || r.proto === ui.flowProto)
    .sort((a, b) => PROTO_KEYS.indexOf(a.proto) - PROTO_KEYS.indexOf(b.proto) || b.requests - a.requests);
  const clients = PROTO_KEYS.filter(p => rs.some(r => r.proto === p)).map(p => ({ id: 'cl:' + p, label: PROTOS[p].client, sub: PROTOS[p].label + ' 客户端', proto: p }));
  const usedGroups = new Set(rs.filter(r => !r.forward).flatMap(r => r.candidates.map(c => c.group)));
  const groups = [...GROUPS.keys()].filter(id => usedGroups.has(id));
  const W = 1000, rowH = 48, nodeH = 34, top = 34;
  const col = { cl: { x: 16, w: 150 }, md: { x: 390, w: 210 }, gp: { x: 774, w: 210 } };
  const n = Math.max(clients.length, rs.length, groups.length, 1);
  const H = top + n * rowH + 12;
  const yOf = (len, i) => top + (n - len) * rowH / 2 + i * rowH + (rowH - nodeH) / 2;
  const pos = new Map();
  clients.forEach((c, i) => pos.set(c.id, { ...col.cl, y: yOf(clients.length, i) }));
  rs.forEach((r, i) => pos.set('md:' + r.key, { ...col.md, y: yOf(rs.length, i) }));
  groups.forEach((g, i) => pos.set('gp:' + g, { ...col.gp, y: yOf(groups.length, i) }));

  const edges = [];
  const maxReq = Math.max(1, ...rs.map(r => r.requests));
  const width = w => (1.2 + 9 * Math.sqrt(Math.max(0, w) / maxReq)).toFixed(1);
  const curve = (a, b) => { const x1 = a.x + a.w, y1 = a.y + nodeH / 2, x2 = b.x, y2 = b.y + nodeH / 2, dx = (x2 - x1) * 0.5; return `M${x1},${y1} C${x1 + dx},${y1} ${x2 - dx},${y2} ${x2},${y2}`; };
  for (const r of rs) {
    const m = pos.get('md:' + r.key), protoOff = !settings.protocols[r.proto];
    edges.push({ d: curve(pos.get('cl:' + r.proto), m), cls: protoOff ? 'off' : '', p: r.proto, w: width(r.requests), a: 'cl:' + r.proto, b: 'md:' + r.key, title: `${PROTOS[r.proto].client} → ${r.name}：近 1 小时约 ${r.requests} 次` });
    if (r.forward) {
      const t = findRoute(r.proto, r.forward), tp = t && pos.get('md:' + t.key);
      const x = m.x + m.w, y1 = m.y + nodeH / 2;
      if (tp) {
        const y2 = tp.y + nodeH / 2;
        edges.push({ d: `M${x},${y1} C${x + 90},${y1} ${x + 90},${y2} ${x},${y2}`, cls: 'fwd', p: r.proto, w: 2.2, a: 'md:' + r.key, b: 'md:' + t.key, title: `${r.name} 整条链交给 ${t.name}`, marker: true });
      } else {
        edges.push({ d: `M${x},${y1} L${x + 70},${y1}`, cls: 'broken', p: r.proto, w: 2.2, a: 'md:' + r.key, b: 'md:' + r.key, title: `交接目标 ${r.forward} 不存在`, label: { x: x + 76, y: y1 + 4, text: `→ ${r.forward} ✕` } });
      }
      continue;
    }
    const start = nextStart(r);
    const others = r.candidates.length - 1;
    for (const c of r.candidates) {
      const st = candState(c), isStart = start && c.id === start.id;
      const share = isStart ? 0.85 : settings.failover[r.proto] && others ? 0.15 / others : 0;
      edges.push({ d: curve(m, pos.get('gp:' + c.group)), cls: protoOff || st === 'off' ? 'off' : st === 'cool' ? 'cool' : isStart ? '' : 'standby', p: r.proto, w: width(r.requests * share), a: 'md:' + r.key, b: 'gp:' + c.group, title: `${r.name} → ${groupLabel(c.group)} · ${c.remote}${isStart ? '（当前起点）' : st === 'cool' ? '（冷却中）' : '（备用）'}` });
    }
  }
  const node = (id, p, label, sub, cls = '', mono = false, stripe = null) => `<g class="node ${cls}" data-node="${esc(id)}" tabindex="0" role="button" aria-label="${esc(label)}">
      <rect x="${p.x}" y="${p.y}" width="${p.w}" height="${nodeH}" rx="8"/>
      ${stripe ? `<rect x="${p.x}" y="${p.y + 7}" width="3" height="${nodeH - 14}" rx="1.5" style="fill:${stripe};stroke:none"/>` : ''}
      <text x="${p.x + 12}" y="${p.y + 15}" class="${mono ? 'm' : ''}">${esc(label)}</text>
      <text x="${p.x + 12}" y="${p.y + 28}" class="sub">${esc(sub)}</text></g>`;
  const nodes = [
    ...clients.map(c => node(c.id, pos.get(c.id), c.label, c.sub, settings.protocols[c.proto] ? '' : 'off', true, `var(--p-${c.proto})`)),
    ...rs.map(r => { const st = routeStatus(r); return node('md:' + r.key, pos.get('md:' + r.key), r.name, `${PROTOS[r.proto].label} · ${r.forward ? '交接 → ' + r.forward : r.candidates.length + ' 条候选'} · ${fmtK(r.requests)} 次`, st.level === 'err' ? 'err' : st.level === 'off' ? 'off' : '', true, `var(--p-${r.proto})`); }),
    ...groups.map(id => { const x = G(id), cool = isCooling(id), off = !x.group.enabled; return node('gp:' + id, pos.get('gp:' + id), groupLabel(id), off ? '分组已停用' : cool ? '冷却中 · 自动换站跳过' : `${PROTOS[x.group.proto].label} · 成功率 ${(successRate(id) * 100).toFixed(0)}%`, off ? 'off' : cool ? 'cool' : '', false, `var(--p-${x.group.proto})`); }),
  ];
  sec.innerHTML = `
    <header class="view-head"><div><h1>流量关系图</h1><p class="sub">客户端 → 下游模型 → 上游分组。线越粗流量越大；虚线是冷却中，淡线是备用，蓝色虚线是整条链交接。布局自动计算，不需要手动摆放。</p></div></header>
    <div class="panel">
      <div class="panel-head"><div class="chips" id="flow-proto">${['all', ...PROTO_KEYS].map(p => `<button class="chip" data-flow-proto="${p}" aria-pressed="${ui.flowProto === p}">${p === 'all' ? '全部接口' : PROTOS[p].label}</button>`).join('')}</div>
        <span class="grow"></span><span class="dim" style="font-size:12px">悬停高亮相关连线 · 点模型打开候选链</span></div>
      <div class="flow-wrap"><svg class="flow" id="flow-svg" viewBox="0 0 ${W} ${H}" role="img" aria-label="流量关系图">
        <defs><marker id="fwd-ah" markerUnits="userSpaceOnUse" markerWidth="10" markerHeight="10" refX="9" refY="5" orient="auto"><path d="M0,0 L10,5 L0,10 z" style="fill:var(--accent)"/></marker></defs>
        <text class="col-title" x="${col.cl.x}" y="18">客户端</text><text class="col-title" x="${col.md.x}" y="18">下游模型（客户端填的名字）</text><text class="col-title" x="${col.gp.x}" y="18">上游分组</text>
        ${edges.map(e => `<path class="edge ${e.cls}" data-p="${e.p}" data-a="${esc(e.a)}" data-b="${esc(e.b)}" d="${e.d}" stroke-width="${e.w}"${e.marker ? ' marker-end="url(#fwd-ah)"' : ''}><title>${esc(e.title)}</title></path>${e.label ? `<text x="${e.label.x}" y="${e.label.y}" style="fill:var(--err);font-size:11px">${esc(e.label.text)}</text>` : ''}`).join('')}
        ${nodes.join('')}
      </svg></div>
    </div>`;
  const svg = $('#flow-svg');
  const focus = id => {
    svg.classList.toggle('has-focus', !!id);
    $$('.hl', svg).forEach(el => el.classList.remove('hl'));
    if (!id) return;
    const related = new Set([id]);
    $$('.edge', svg).forEach(e => { if (e.dataset.a === id || e.dataset.b === id) { e.classList.add('hl'); related.add(e.dataset.a); related.add(e.dataset.b); } });
    $$('.node', svg).forEach(nd => { if (related.has(nd.dataset.node)) nd.classList.add('hl'); });
  };
  svg.addEventListener('pointerover', e => { const nd = e.target.closest('.node'); focus(nd?.dataset.node ?? null); });
  svg.addEventListener('pointerleave', () => focus(null));
  svg.addEventListener('focusin', e => focus(e.target.closest('.node')?.dataset.node ?? null));
}

// ---------- 实时请求：瀑布时间轴 ----------
const PHASE_TEXT = { wait: '等响应头', ttft: '等首字', stream: '流式输出', stall: '无内容', fail: '失败' };
function liveSnapshot(req) {
  if (req.done) return req.record;
  const rec = planToRecord(req.plan, req.t0, now());
  rec.status = 'running';
  rec.code = null;
  const a = rec.attempts.at(-1);
  if (a) a.outcome = 'run';
  return rec;
}
function livePhase(req) {
  if (req.done) {
    const r = req.record;
    return r.status === 'ok' ? { text: '完成', color: 'var(--ok)' } : r.status === 'cancel' ? { text: '已中断', color: 'var(--warn)' } : { text: '失败', color: 'var(--err)' };
  }
  const el = now() - req.t0;
  let off = 0;
  for (const [ai, a] of req.plan.attempts.entries()) {
    for (const s of a.segs) {
      if (el < off + s.d) {
        if (a.outcome === 'fail' && s.k === 'wait') return { text: ai ? `第 ${ai + 1} 次尝试` : '等响应头', color: 'var(--muted)' };
        return { text: PHASE_TEXT[s.k] + (ai ? ` · 第 ${ai + 1} 次` : ''), color: s.k === 'stall' ? 'var(--warn)' : s.k === 'stream' ? 'var(--ok)' : 'var(--muted)' };
      }
      off += s.d;
    }
  }
  return { text: '收尾中', color: 'var(--muted)' };
}
function trackHTML(req) {
  const end = req.done ? (req.cancelledAt ?? req.t0 + req.plan.total) - req.t0 : now() - req.t0;
  const scale = Math.max(30000, end * 1.08);
  const pct = d => (d / scale * 100).toFixed(2) + '%';
  let off = 0, html = '', notes = [];
  req.plan.attempts.forEach((a, ai) => {
    if (off >= end) return;
    if (ai > 0) html += '<span class="seg hop" title="换下一个候选">›</span>';
    const site = G(a.group)?.site.name ?? a.group;
    const aDur = attemptDur(a);
    if (a.outcome === 'fail') {
      const vis = Math.min(aDur, end - off);
      const finished = off + aDur <= end;
      html += `<span class="seg ${finished ? 'fail' : 'wait'}" style="width:${pct(vis)}" title="${esc(groupLabel(a.group))}">${finished ? `${esc(site)} ${a.code}` : esc(site)}</span>`;
      if (finished) notes.push(`${groupLabel(a.group)} 返回 ${a.code}`);
      off += aDur;
      return;
    }
    let soff = off;
    for (const s of a.segs) {
      const vis = clamp(end - soff, 0, s.d);
      if (vis <= 0) break;
      const running = !req.done && soff + s.d > end;
      let label = s.k === 'wait' ? (ai ? site : '等响应头') : s.k === 'ttft' ? '首字' : s.k === 'stream' ? `${ai ? site + ' ' : ''}流式 ${a.tps} tok/s` : `无内容 ${(vis / 1000).toFixed(0)}s${settings.stallTimeout ? ' / ' + settings.stallTimeout + 's' : ''}`;
      if (s.k === 'stall' && a.outcome === 'stall' && !running) label = '无内容超时';
      html += `<span class="seg ${s.k === 'stall' && a.outcome === 'stall' && !running ? 'fail' : s.k}${running ? ' live-edge' : ''}" style="width:${pct(vis)}">${esc(label)}</span>`;
      soff += s.d;
    }
    if (req.plan.attempts.length > 1 && ai > 0) notes.push(`第 ${ai + 1} 次尝试 · ${groupLabel(a.group)} · ${a.remote}`);
    off += aDur;
  });
  if (req.cancelledAt) html += '<span class="seg cancel" style="width:3%" title="手动中断"></span>';
  return { html, note: req.done && req.record.status === 'cancel' ? '手动中断' : notes.join(' → ') };
}
function liveRowHTML(req) {
  const p = req.plan, ph = livePhase(req), tr = trackHTML(req);
  const el = (req.done ? req.record.dur : now() - req.t0);
  return `<div class="live-row live-grid${req.done ? ' done' : ''}${ui.drawer === p.id ? ' selected' : ''}" data-live="${p.id}">
    <div class="live-phase" style="color:${ph.color}">${esc(ph.text)}<small>${fmtClock(req.t0)} · ${fmtDur(el)}</small></div>
    <div class="live-who"><div class="model-name">${esc(p.model)}${p.via ? ` <span class="faint">→ ${esc(p.via)}</span>` : ''}</div>
      <div class="meta">${protoBadge(p.proto)}<span>${esc(p.client)}</span></div></div>
    <div class="track-wrap"><div class="track">${tr.html}</div><div class="attempt-note">${esc(tr.note)}</div></div>
    <div style="text-align:right">${req.done ? `<span class="dim num" style="font-size:12px">${req.record.tokens.out ? fmtK(req.record.tokens.out) + ' tok' : ''}</span>` : `<button class="btn btn-danger btn-sm" data-cancel="${p.id}">停止</button>`}</div>
  </div>`;
}
function liveBuckets() {
  const running = live.filter(x => !x.done);
  const isIssue = x => x.done ? x.record.status !== 'ok'
    : (x.plan.attempts.length > 1 && (now() - x.t0) > attemptDur(x.plan.attempts[0])) || livePhase(x).text.startsWith('无内容');
  const issues = live.filter(isIssue);
  const shown = ui.liveFilter === 'running' ? running : ui.liveFilter === 'issue' ? issues : live;
  return { running, issues, shown };
}
// 骨架只在进入页面时建一次；之后由 updateLive 原地更新
function renderLive() {
  const sec = $('.view[data-view="live"]');
  sec.innerHTML = `
    <header class="view-head"><div><h1>实时请求</h1><p class="sub">每条请求一行：等响应头 → 首字 → 流式输出；换站和无内容都画在同一条时间轴上。结束的请求保留 90 秒。</p></div>
      <span class="grow"></span>
      <div class="chips" id="live-chips">
        <button class="chip" data-live-filter="all">全部<span class="n"></span></button>
        <button class="chip" data-live-filter="running">进行中<span class="n"></span></button>
        <button class="chip" data-live-filter="issue">异常 / 换站<span class="n"></span></button>
      </div>
      <button class="btn btn-ghost btn-sm" data-act="toggle-sim" id="sim-btn"></button>
    </header>
    <div class="live-layout">
      <section class="panel" aria-label="请求时间轴">
        <div class="live-axis"><span>状态</span><span>模型 / 客户端</span><div class="axis-ticks"><span style="left:0">0s</span><span style="left:33.3%">10s</span><span style="left:66.6%">20s</span><span style="left:100%;transform:translateX(-100%)">30s+</span></div><span style="text-align:right">操作</span></div>
        <div id="live-rows"></div>
      </section>
      <aside style="display:flex;flex-direction:column;gap:14px">
        <section class="panel" id="live-health"></section>
        <section class="panel"><div class="panel-head"><h2>图例</h2></div><div class="panel-body" style="display:grid;grid-template-columns:auto 1fr;gap:8px 10px;align-items:center;font-size:12px">
          <span class="seg wait" style="width:28px;height:12px;border-radius:3px"></span><span>等响应头</span>
          <span class="seg ttft" style="width:28px;height:12px;border-radius:3px"></span><span>收到响应头，等第一个字</span>
          <span class="seg stream" style="width:28px;height:12px;border-radius:3px"></span><span>流式输出中</span>
          <span class="seg stall" style="width:28px;height:12px;border-radius:3px"></span><span>输出中途没有新内容</span>
          <span class="seg fail" style="width:28px;height:12px;border-radius:3px"></span><span>这一次尝试失败，› 后是换站</span></div></section>
      </aside>
    </div>`;
  updateLive();
}
function healthHTML() {
  const cooling = [...GROUPS.keys()].filter(isCooling);
  const streaky = [...health.entries()].filter(([id, h]) => h.streak > 0 && !isCooling(id));
  return `<div class="panel-head"><h2>分组健康</h2><span class="grow"></span><span class="tag ${cooling.length ? 'warn' : 'ok'}">${cooling.length ? cooling.length + ' 个冷却中' : '全部正常'}</span></div>
    <div class="panel-body" style="padding-top:2px;padding-bottom:2px">
      ${cooling.map(id => `<div class="breaker-row"><span class="dot cool"></span><span>${esc(groupLabel(id))}<br><span class="dim" style="font-size:11.5px">${esc(health.get(id).lastError)}</span></span><b class="num" style="color:var(--warn)" data-cd="${id}">${fmtCountdown(cooldowns.get(id) - now())}</b></div>`).join('')}
      ${streaky.map(([id, h]) => `<div class="breaker-row"><span class="dot warn"></span><span>${esc(groupLabel(id))}<br><span class="dim" style="font-size:11.5px">连续失败 ${h.streak} 次 · ${esc(h.lastError)}</span></span><span class="dim">再失败 1 次冷却</span></div>`).join('')}
      ${!cooling.length && !streaky.length ? '<p class="empty" style="padding:14px 0">没有连续失败的分组</p>' : ''}
    </div>
    <div class="panel-body dim" style="font-size:12px;border-top:1px solid var(--line)">同一分组连续失败 2 次冷却 90 秒；冷却期间自动换站会跳过它。手动设为首选会立刻生效。</div>`;
}
function patchLiveRow(row, req) {
  row.classList.toggle('selected', ui.drawer === req.plan.id);
  if (req.done) return;
  const ph = livePhase(req), tr = trackHTML(req);
  const phEl = $('.live-phase', row);
  phEl.style.color = ph.color;
  phEl.innerHTML = `${esc(ph.text)}<small>${fmtClock(req.t0)} · ${fmtDur(now() - req.t0)}</small>`;
  $('.track', row).innerHTML = tr.html;
  $('.attempt-note', row).textContent = tr.note;
}
// 按请求 id 复用行元素：只有新行或刚结束的行才重建，“停止”按钮不会在按下和松开之间被换掉
function updateLive() {
  const box = $('#live-rows');
  if (!box) return;
  const { running, issues, shown } = liveBuckets();
  const counts = { all: live.length, running: running.length, issue: issues.length };
  for (const chip of $$('#live-chips .chip')) {
    chip.setAttribute('aria-pressed', ui.liveFilter === chip.dataset.liveFilter);
    $('.n', chip).textContent = counts[chip.dataset.liveFilter];
  }
  $('#sim-btn').textContent = settings.simulate ? '⏸ 暂停模拟流量' : '▶ 继续模拟流量';
  const hh = healthHTML();
  const hb = $('#live-health');
  if (hb._html !== hh.replace(/data-cd="(\w+)">[^<]*/g, '$1')) { hb.innerHTML = hh; hb._html = hh.replace(/data-cd="(\w+)">[^<]*/g, '$1'); }

  if (!shown.length) {
    box.innerHTML = `<p class="empty">${settings.simulate ? '暂时没有请求，模拟流量马上就来…' : '模拟流量已暂停'}</p>`;
    return;
  }
  $('.empty', box)?.remove();
  const existing = new Map($$('.live-row', box).map(el => [el.dataset.live, el]));
  const make = req => { const t = document.createElement('template'); t.innerHTML = liveRowHTML(req).trim(); return t.content.firstElementChild; };
  const rowFor = req => {
    let el = existing.get(req.plan.id);
    if (!el || el.classList.contains('done') !== req.done) el = make(req);
    else patchLiveRow(el, req);
    return el;
  };
  const doneRows = shown.filter(x => x.done);
  const nodes = shown.filter(x => !x.done).map(rowFor);
  if (doneRows.length) {
    const sep = $('.live-sep', box) ?? Object.assign(document.createElement('div'), { className: 'live-sep' });
    sep.textContent = `最近结束 · ${doneRows.length} 条`;
    nodes.push(sep);
  }
  nodes.push(...doneRows.map(rowFor));
  nodes.forEach((n, i) => { if (box.children[i] !== n) box.insertBefore(n, box.children[i] ?? null); });
  while (box.children.length > nodes.length) box.lastElementChild.remove();
  for (const el of $$('[data-cd]')) el.textContent = fmtCountdown((cooldowns.get(el.dataset.cd) ?? 0) - now());
}

// ---------- 转发记录 ----------
function renderLog() {
  const sec = $('.view[data-view="log"]');
  if (!sec.dataset.ready) {
    sec.dataset.ready = '1';
    sec.innerHTML = `
      <header class="view-head"><div><h1>转发记录</h1><p class="sub">已经结束的请求。点一行看每次尝试、报错原文和 token；“复制排查信息”一次带上时间、客户端、模型、上游、状态码和报错。</p></div></header>
      <section class="panel">
        <div class="log-tools">
          <input type="search" class="search" id="log-q" placeholder="搜索模型、上游、客户端、报错…" style="width:280px" aria-label="搜索转发记录">
          <div class="chips" id="log-status"></div><span class="sb-sep"></span><div class="chips" id="log-proto"></div>
          <span class="grow"></span><span class="dim" id="log-count" style="font-size:12px"></span>
        </div>
        <div class="log-scroll"><table class="tbl"><thead><tr><th>时间</th><th>客户端</th><th>接口</th><th>模型</th><th>最终上游</th><th>结果</th><th class="r">尝试</th><th class="r">Token 入 / 出 / 缓存</th><th class="r">首字</th><th class="r">耗时</th></tr></thead>
          <tbody id="log-body"></tbody></table><p class="empty" id="log-empty" hidden>没有匹配的记录</p></div>
      </section>`;
    $('#log-q').addEventListener('input', e => { ui.logQuery = e.target.value; renderLogRows(); });
  }
  $('#log-q').value = ui.logQuery;
  renderLogRows();
}
function renderLogRows() {
  const q = ui.logQuery.trim().toLowerCase();
  const byStatus = s => s === 'all' ? records : records.filter(r => s === 'error' ? r.status === 'error' : s === 'retry' ? r.attempts.length > 1 : r.status === s);
  const st = [['all', '全部'], ['ok', '成功'], ['error', '失败'], ['retry', '换过站'], ['cancel', '中断']];
  $('#log-status').innerHTML = st.map(([k, l]) => `<button class="chip" data-log-status="${k}" aria-pressed="${ui.logStatus === k}">${l}<span class="n">${byStatus(k).length}</span></button>`).join('');
  $('#log-proto').innerHTML = ['all', ...PROTO_KEYS].map(p => `<button class="chip" data-log-proto="${p}" aria-pressed="${ui.logProto === p}">${p === 'all' ? '全部接口' : PROTOS[p].label}</button>`).join('');
  const list = byStatus(ui.logStatus).filter(r => (ui.logProto === 'all' || r.proto === ui.logProto) &&
    (!q || `${r.model} ${r.via ?? ''} ${r.client} ${r.code} ${r.attempts.map(a => groupLabel(a.group) + ' ' + a.remote + ' ' + a.error).join(' ')}`.toLowerCase().includes(q)));
  const shown = list.slice(0, 300);
  $('#log-count').textContent = `${list.length} 条${list.length > shown.length ? '，显示最近 300 条' : ''}`;
  $('#log-empty').hidden = list.length > 0;
  $('#log-body').innerHTML = shown.map(r => {
    const last = r.attempts.at(-1);
    return `<tr data-record="${r.id}" class="${ui.drawer === r.id ? 'selected' : ''}${newRecordIds.has(r.id) ? ' fresh' : ''}">
      <td class="dim num">${fmtClock(r.t)}</td><td>${esc(r.client)}</td><td>${protoBadge(r.proto)}</td>
      <td class="mono">${esc(r.model)}${r.via ? ` <span class="faint">→ ${esc(r.via)}</span>` : ''}</td>
      <td class="up">${esc(groupLabel(last?.group))} <span class="faint mono">${esc(last?.remote)}</span></td>
      <td>${codeCell(r)}</td>
      <td class="r">${r.attempts.length > 1 ? `<span class="tag warn">${r.attempts.length}</span>` : '<span class="faint">1</span>'}</td>
      <td class="r">${r.tokens.estimated ? '≈ ' : ''}${fmtK(r.tokens.in)} / ${fmtK(r.tokens.out)} / ${fmtK(r.tokens.cache)}</td>
      <td class="r dim">${fmtDur(r.ttft)}</td><td class="r">${fmtDur(r.dur)}</td></tr>`;
  }).join('');
  newRecordIds.clear();
}

// ---------- 上游站点 ----------
function renderUpstreams() {
  const sec = $('.view[data-view="upstreams"]');
  sec.innerHTML = `
    <header class="view-head"><div><h1>上游站点</h1><p class="sub">站点保存地址和出口；分组保存接口、Key 和模型目录。模型目录的勾选即时生效，与需要保存的表单分开。</p></div>
      <span class="grow"></span><button class="btn" data-act="demo-only">＋ 添加站点</button></header>
    <div class="site-grid">${sites.map(s => `
      <section class="panel">
        <div class="site-head"><span class="dot ${s.enabled ? 'ok' : 'off'}"></span><h2>${esc(s.name)}</h2><span class="dim mono" style="font-size:12px">${esc(s.base)}</span>
          <span class="grow"></span><button class="btn btn-ghost btn-sm" data-probe="${s.id}">测一下</button>
          <input type="checkbox" class="switch" data-site-toggle="${s.id}" ${s.enabled ? 'checked' : ''} aria-label="启用 ${esc(s.name)}"></div>
        <div class="site-meta"><span>出口：${esc(s.egress)}</span><span>${s.groups.length} 个分组</span><span>${s.groups.reduce((n, g) => n + g.models.length, 0)} 个上游模型</span></div>
        ${s.groups.map(g => {
          const cool = isCooling(g.id), h = health.get(g.id);
          return `<div class="group-row"><span class="dot ${!g.enabled ? 'off' : cool ? 'cool' : 'ok'}"></span>
            <span><b>${esc(g.name)}</b><br><span class="dim mono" style="font-size:11.5px">${g.key ? esc(g.key) : '透传客户端鉴权'}</span></span>
            ${protoBadge(g.proto)}
            <span class="models">${g.models.slice(0, 3).map(m => `<span>${esc(m)}</span>`).join('')}${g.models.length > 3 ? `<span>+${g.models.length - 3}</span>` : ''}</span>
            <span class="stat">${!g.enabled ? '<span class="dim">已停用</span>' : cool ? `<span style="color:var(--warn)">冷却 <span data-cd="${g.id}">${fmtCountdown(cooldowns.get(g.id) - now())}</span></span>` : `${(successRate(g.id) * 100).toFixed(0)}% · ${fmtDur(h.p95)}`}</span></div>`;
        }).join('')}
      </section>`).join('')}</div>`;
}

// ---------- 设置 ----------
function renderSettings() {
  const sec = $('.view[data-view="settings"]');
  sec.innerHTML = `
    <header class="view-head"><div><h1>网关设置</h1><p class="sub">全局行为集中在这里，改动即时生效（样稿内只影响本页面的演示数据）。</p></div></header>
    <div class="settings-grid">
      <section class="panel"><div class="panel-head"><h2>接口开关</h2><span class="tag ok">即时生效</span></div><div class="panel-body" style="padding-top:2px;padding-bottom:2px">
        ${PROTO_KEYS.map(p => `<div class="set-row"><span class="lbl">${protoBadge(p)} <span class="mono dim" style="font-size:12px;margin-left:6px">POST ${PROTOS[p].path}</span><small>关闭后拒绝新请求，配置保留。</small></span>
          <input type="checkbox" class="switch" data-proto-toggle="${p}" ${settings.protocols[p] ? 'checked' : ''} aria-label="${PROTOS[p].label} 接口"></div>`).join('')}</div></section>
      <section class="panel"><div class="panel-head"><h2>失败与超时</h2><span class="tag ok">即时生效</span></div><div class="panel-body" style="padding-top:2px;padding-bottom:2px">
        ${PROTO_KEYS.map(p => `<div class="set-row"><span class="lbl">${PROTOS[p].label} 失败自动换站<small>${settings.failover[p] ? '失败或冷却时按候选顺序往后试' : '只用起点，失败直接返回给客户端'}</small></span>
          <input type="checkbox" class="switch" data-fo-toggle="${p}" ${settings.failover[p] ? 'checked' : ''} aria-label="${PROTOS[p].label} 自动换站"></div>`).join('')}
        <div class="set-row"><span class="lbl">无内容超时（秒）<small>输出中途多久没有新内容就断开这一次尝试；0 表示关闭。试试改成 10，再去实时页看效果。</small></span>
          <input type="number" id="stall-input" min="0" max="600" value="${settings.stallTimeout}" style="width:84px" aria-label="无内容超时秒数"></div></div></section>
      <section class="panel"><div class="panel-head"><h2>下游接入</h2></div><div class="panel-body" style="padding-top:2px;padding-bottom:2px">
        ${[['OpenAI 兼容', 'http://127.0.0.1:8317/v1'], ['Claude Code', 'http://127.0.0.1:8317'], ['API Key', 'local-only（任意非空值）']].map(([k, v]) => `<div class="access-row"><span class="dim">${k}</span><code>${esc(v)}</code><button class="btn btn-ghost btn-sm" data-copy="${esc(v.split('（')[0])}">复制</button></div>`).join('')}
        <p class="dim" style="font-size:12px;margin:8px 0">Claude Code 的地址不要加 /v1。模型名填「模型路由」里起的名字。</p></div></section>
    </div>`;
  $('#stall-input').addEventListener('change', e => {
    settings.stallTimeout = clamp(Math.round(+e.target.value || 0), 0, 600);
    e.target.value = settings.stallTimeout;
    renderStatusbar();
    toast(settings.stallTimeout ? `无内容超时改为 ${settings.stallTimeout} 秒（之后的新请求生效）` : '已关闭无内容超时');
  });
}

// ---------- 请求详情抽屉 ----------
function findRecordOrLive(id) {
  const req = live.find(x => x.plan.id === id);
  if (req) return { rec: liveSnapshot(req), req };
  return { rec: records.find(x => x.id === id), req: null };
}
function diagText(rec) {
  const last = rec.attempts.at(-1);
  const lines = [
    `时间：${fmtDate(rec.t)}`,
    `客户端：${rec.client}`,
    `接口：${PROTOS[rec.proto].label} POST ${PROTOS[rec.proto].path}`,
    `模型：${rec.model}${rec.via ? `（整条链交给 ${rec.via}）` : ''}`,
    `最终上游：${groupLabel(last?.group)} · ${last?.remote ?? '—'}`,
    `结果：${rec.status === 'running' ? '进行中' : rec.status === 'cancel' ? '手动中断' : rec.code === 'stall' ? '无内容超时' : rec.code} · 耗时 ${fmtDur(rec.dur)} · 首字 ${fmtDur(rec.ttft)}`,
    '尝试：',
    ...rec.attempts.map((a, i) => `  ${i + 1}. ${groupLabel(a.group)} · ${a.remote} → ${a.outcome === 'ok' ? '200' : a.outcome === 'run' ? '进行中' : a.outcome === 'cancel' ? '中断' : a.outcome === 'stall' ? '无内容超时' : a.code}（${fmtDur(a.dur)}）${a.error ? ' ' + a.error : ''}`),
    `Token：入 ${rec.tokens.in} / 出 ${rec.tokens.out} / 缓存 ${rec.tokens.cache}${rec.tokens.estimated ? '（含估算）' : ''}`,
  ];
  return lines.join('\n');
}
function openDrawer(id) {
  ui.drawer = id;
  renderDrawer();
  const d = $('#drawer');
  d.classList.add('open');
  d.setAttribute('aria-hidden', 'false');
  $$('[data-record], [data-live]').forEach(el => el.classList.toggle('selected', (el.dataset.record ?? el.dataset.live) === id));
}
function closeDrawer() {
  ui.drawer = null;
  const d = $('#drawer');
  d.classList.remove('open');
  d.setAttribute('aria-hidden', 'true');
  $$('.selected').forEach(el => el.classList.remove('selected'));
}
function renderDrawer() {
  const { rec, req } = findRecordOrLive(ui.drawer);
  if (!rec) { closeDrawer(); return; }
  $('#drawer-title').textContent = rec.model;
  $('#drawer-status').innerHTML = `${protoBadge(rec.proto)} ${rec.status === 'running' ? '<span class="tag accent">进行中</span>' : rec.status === 'ok' ? '<span class="tag ok">完成</span>' : rec.status === 'cancel' ? '<span class="tag warn">已中断</span>' : '<span class="tag err">失败</span>'}`;
  const idxCls = a => a.outcome === 'ok' ? 'ok' : a.outcome === 'run' ? 'run' : a.outcome === 'cancel' ? '' : 'err';
  const res = a => a.outcome === 'ok' ? '<span class="code ok">200</span>' : a.outcome === 'run' ? '<span class="code" style="color:var(--accent)">进行中</span>' : a.outcome === 'cancel' ? '<span class="code warn">中断</span>' : a.outcome === 'stall' ? '<span class="code err">无内容超时</span>' : `<span class="code err">${a.code}</span>`;
  $('#drawer-body').innerHTML = `
    <dl class="kv">
      <dt>时间</dt><dd class="num">${fmtDate(rec.t)}</dd>
      <dt>客户端</dt><dd>${esc(rec.client)}</dd>
      <dt>接口</dt><dd class="mono">POST ${PROTOS[rec.proto].path}</dd>
      <dt>模型</dt><dd class="mono">${esc(rec.model)}${rec.via ? ` → ${esc(rec.via)}` : ''}</dd>
      <dt>耗时</dt><dd class="num">${fmtDur(rec.dur)} · 首字 ${fmtDur(rec.ttft)}</dd>
    </dl>
    <div class="d-section"><h3>尝试经过 · ${rec.attempts.length} 次</h3>
      ${rec.attempts.map((a, i) => `<div class="attempt"><span class="idx ${idxCls(a)}">${i + 1}</span>
        <span>${esc(groupLabel(a.group))}<br><span class="mono dim" style="font-size:12px">${esc(a.remote)}</span></span>
        <span style="text-align:right">${res(a)}<br><span class="dim num" style="font-size:12px">${fmtDur(a.dur)}${a.tps && (a.outcome === 'ok' || a.outcome === 'run') ? ` · ${a.tps} tok/s` : ''}</span></span>
        ${a.error ? `<div class="err-text">${esc(a.error)}</div>` : ''}</div>`).join('')}</div>
    <div class="d-section"><h3>Token ${rec.tokens.estimated ? '<span class="faint" style="text-transform:none">（≈ 含估算，不是账单）</span>' : ''}</h3>
      <div class="tokens"><div><b>${fmtK(rec.tokens.in)}</b><span>输入</span></div><div><b>${fmtK(rec.tokens.out)}</b><span>输出</span></div><div><b>${fmtK(rec.tokens.cache)}</b><span>缓存命中</span></div></div></div>
    ${rec.status === 'error' ? `<div class="callout warn"><span class="dot warn"></span><div>只能说明这次请求没有正常结束，不能据此认定是谁的问题。看报错原文和上游站点状态再判断。</div></div>` : ''}`;
  $('#drawer-foot').innerHTML = `<button class="btn" data-act="copy-diag">复制排查信息</button>
    <button class="btn btn-ghost" data-route="${esc(rec.proto + '|' + rec.model)}">打开模型路由</button><span class="grow"></span>
    ${req && !req.done ? `<button class="btn btn-danger" data-cancel="${req.plan.id}">停止</button>` : ''}`;
}

// ---------- 改配置（只改页面内存） ----------
const selectedRoute = () => routes.find(r => r.key === ui.selected);
function moveCand(r, cid, toIndex) {
  const from = r.candidates.findIndex(c => c.id === cid);
  if (from < 0) return;
  const [c] = r.candidates.splice(from, 1);
  r.candidates.splice(clamp(toIndex, 0, r.candidates.length), 0, c);
  onStateChange();
  toast(`候选顺序已更新：${groupLabel(c.group)} 现在排第 ${r.candidates.indexOf(c) + 1}`);
}
function prefer(r, cid) {
  const c = r.candidates.find(x => x.id === cid);
  if (!c) return;
  r.preferred = cid;
  cooldowns.delete(c.group); // 手动设为首选立刻生效并清除冷却
  onStateChange();
  toast(`首选改为 ${groupLabel(c.group)}，下一次请求从它开始`);
}
function removeCand(r, cid) {
  const c = r.candidates.find(x => x.id === cid);
  if (!c || !confirm(`从 ${r.name} 移除候选「${groupLabel(c.group)} · ${c.remote}」？`)) return;
  r.candidates = r.candidates.filter(x => x.id !== cid);
  if (r.preferred === cid) r.preferred = r.candidates[0]?.id ?? null;
  onStateChange();
  toast('已移除候选');
}

function fillGroupSelect(proto, selectedGroup) {
  const opts = [...GROUPS.values()].filter(x => x.group.proto === proto);
  $('#rd-group').innerHTML = opts.length ? opts.map(x => `<option value="${x.group.id}"${x.group.id === selectedGroup ? ' selected' : ''}>${esc(x.site.name)} · ${esc(x.group.name)}${x.group.enabled ? '' : '（已停用）'}</option>`).join('') : '<option value="">这个接口还没有分组</option>';
  fillRemoteList();
}
function fillRemoteList() {
  const g = G($('#rd-group').value)?.group;
  $('#rd-remote-list').innerHTML = (g?.models ?? []).map(m => `<option value="${esc(m)}">`).join('');
  if (g && !$('#rd-remote').value) $('#rd-remote').placeholder = g.models[0] ?? '';
}
function openRouteDialog(mode) {
  ui.dlgMode = mode;
  const r = selectedRoute();
  const isNew = mode === 'new';
  $('#route-dlg-title').textContent = isNew ? '新增模型' : `给 ${r.name} 添加候选`;
  $('#rd-save').textContent = isNew ? '添加模型' : '添加候选';
  $('#rd-name-wrap').hidden = !isNew;
  $('#rd-proto-wrap').hidden = !isNew;
  $('#rd-name').value = '';
  $('#rd-remote').value = '';
  $('#rd-proto').innerHTML = PROTO_KEYS.map(p => `<option value="${p}">${PROTOS[p].label}（${PROTOS[p].path}）</option>`).join('');
  $('#rd-proto').value = isNew ? (ui.routeProto === 'all' ? 'responses' : ui.routeProto) : r.proto;
  fillGroupSelect($('#rd-proto').value);
  $('#route-dlg').showModal();
  (isNew ? $('#rd-name') : $('#rd-group')).focus();
}
function submitRouteDialog() {
  const gid = $('#rd-group').value;
  const remote = $('#rd-remote').value.trim() || $('#rd-remote').placeholder;
  if (!gid) { toast('先选一个上游分组'); return false; }
  if (ui.dlgMode === 'new') {
    const name = $('#rd-name').value.trim();
    const proto = $('#rd-proto').value;
    if (!name) { toast('填一个给客户端用的模型名'); $('#rd-name').focus(); return false; }
    if (findRoute(proto, name)) { toast(`${PROTOS[proto].label} 下已经有 ${name}`); return false; }
    const r = { name, proto, candidates: [cand(gid, remote || name)], requests: 0 };
    initRoute(r);
    routes.push(r);
    ui.selected = r.key;
    toast(`已新增 ${name}，客户端现在可以用这个名字调用`);
  } else {
    const r = selectedRoute();
    if (r.candidates.some(c => c.group === gid && c.remote === remote)) { toast('这条候选已经有了'); return false; }
    r.candidates.push(cand(gid, remote));
    if (!r.preferred) r.preferred = r.candidates[0].id;
    toast(`已添加候选 ${groupLabel(gid)} · ${remote}`);
  }
  onStateChange();
  return true;
}
function openForwardDialog() {
  const r = selectedRoute();
  const targets = routes.filter(x => x.proto === r.proto && x.key !== r.key);
  $('#fwd-title').textContent = `把 ${r.name} 的整条链交给…`;
  $('#fwd-target').innerHTML = targets.map(t => `<option value="${esc(t.name)}"${t.name === r.forward ? ' selected' : ''}>${esc(t.name)}${t.forward ? `（→ ${esc(t.forward)}）` : `（${t.candidates.length} 条候选）`}</option>`).join('');
  $('#fwd-clear').hidden = !r.forward;
  $('#fwd-dlg').showModal();
}
function submitForward() {
  const r = selectedRoute();
  const target = $('#fwd-target').value;
  const old = r.forward;
  r.forward = target;
  const res = resolveRoute(r);
  if (res.broken === 'loop') { r.forward = old; toast('会绕成圈，已拒绝'); return false; }
  if (!res.route || !res.route.candidates.length) { r.forward = old; toast('目标在这个接口下没有可用候选，转过去也是 404'); return false; }
  onStateChange();
  toast(`${r.name} 现在按 ${target} 的候选链走`);
  return true;
}
function unforward() {
  const r = selectedRoute();
  if (!r?.forward) return;
  r.forward = null;
  $('#fwd-dlg').close();
  onStateChange();
  toast(`已取消交接，${r.name} 恢复使用自己的 ${r.candidates.length} 条候选`);
}

// ---------- 命令面板 ----------
let cmdkItems = [], cmdkIndex = 0;
function cmdkSource() {
  return [
    ...VIEWS.map(v => ({ group: '页面', label: v.title, hint: '跳转', ic: '↗', run: () => setView(v.id) })),
    ...routes.map(r => ({ group: '模型', label: r.name, hint: `${PROTOS[r.proto].label} · ${routeStatus(r).text}`, ic: '◆', mono: true, run: () => { ui.selected = r.key; setView('routes'); } })),
    ...sites.map(s => ({ group: '站点', label: s.name, hint: s.base, ic: '▦', run: () => setView('upstreams') })),
    { group: '操作', label: '新增模型', hint: '模型路由', ic: '＋', run: () => { setView('routes'); openRouteDialog('new'); } },
    { group: '操作', label: '切换深浅色', hint: '', ic: '◐', run: toggleTheme },
    { group: '操作', label: '复制 OpenAI 兼容接入地址', hint: 'http://127.0.0.1:8317/v1', ic: '⧉', run: () => copyText('http://127.0.0.1:8317/v1').then(() => toast('已复制接入地址')) },
    { group: '操作', label: settings.simulate ? '暂停模拟流量' : '继续模拟流量', hint: '样稿', ic: '⏯', run: toggleSim },
    ...PROTO_KEYS.map(p => ({ group: '操作', label: `${settings.failover[p] ? '关闭' : '开启'} ${PROTOS[p].label} 自动换站`, hint: '网关设置', ic: '⇄', run: () => { settings.failover[p] = !settings.failover[p]; onStateChange(); toast(`${PROTOS[p].label} 自动换站已${settings.failover[p] ? '开启' : '关闭'}`); } })),
  ];
}
function openCmdk() {
  const dlg = $('#cmdk');
  if (dlg.open) return;
  $('#cmdk-input').value = '';
  renderCmdk();
  dlg.showModal();
  $('#cmdk-input').focus();
}
function renderCmdk() {
  const q = $('#cmdk-input').value.trim().toLowerCase();
  const score = it => { const l = it.label.toLowerCase(); if (!q) return 1; if (l.startsWith(q)) return 3; if (l.includes(q)) return 2; return (it.hint + ' ' + it.group).toLowerCase().includes(q) ? 1 : 0; };
  cmdkItems = cmdkSource().map(it => ({ ...it, s: score(it) })).filter(it => it.s > 0);
  if (q) cmdkItems.sort((a, b) => b.s - a.s);
  cmdkIndex = 0;
  let lastGroup = '';
  $('#cmdk-list').innerHTML = cmdkItems.length ? cmdkItems.map((it, i) => {
    const head = !q && it.group !== lastGroup ? `<div class="cmdk-group">${it.group}</div>` : '';
    lastGroup = it.group;
    return `${head}<button class="cmdk-item" role="option" data-cmdk="${i}" aria-selected="${i === 0}"><span class="ic">${it.ic}</span><span class="${it.mono ? 'mono' : ''}">${esc(it.label)}</span><span class="hint">${esc(it.hint)}</span></button>`;
  }).join('') : '<p class="empty">没有匹配的项目</p>';
}
function cmdkMove(d) {
  if (!cmdkItems.length) return;
  cmdkIndex = (cmdkIndex + d + cmdkItems.length) % cmdkItems.length;
  $$('.cmdk-item').forEach((el, i) => el.setAttribute('aria-selected', i === cmdkIndex));
  $(`.cmdk-item[data-cmdk="${cmdkIndex}"]`)?.scrollIntoView({ block: 'nearest' });
}
function cmdkRun(i) {
  const it = cmdkItems[i];
  if (!it) return;
  $('#cmdk').close();
  it.run();
}

function toggleTheme() {
  const next = document.documentElement.dataset.theme === 'light' ? 'dark' : 'light';
  document.documentElement.dataset.theme = next;
  try { localStorage.setItem('mg-console-demo-theme', next); } catch {}
}
function toggleSim() {
  settings.simulate = !settings.simulate;
  toast(settings.simulate ? '模拟流量已继续' : '模拟流量已暂停，进行中的请求会照常结束');
  if (ui.view === 'live') updateLive();
}

// ---------- 事件 ----------
document.addEventListener('click', e => {
  const t = e.target.closest('button, [data-record], [data-live], .node, a');
  if (!t) return;
  const d = t.dataset;
  if (d.go) return setView(d.go);
  if (d.route) { ui.selected = d.route; if (ui.view !== 'routes') setView('routes'); else { renderModelList(); renderDetail(); } return; }
  if (d.routeProto) { ui.routeProto = d.routeProto; return renderModelList(), renderDetail(); }
  if (d.flowProto) { ui.flowProto = d.flowProto; return renderFlow(); }
  if (d.liveFilter) { ui.liveFilter = d.liveFilter; return updateLive(); }
  if (d.logStatus) { ui.logStatus = d.logStatus; return renderLogRows(); }
  if (d.logProto) { ui.logProto = d.logProto; return renderLogRows(); }
  if (d.cancel) { e.stopPropagation(); cancelLive(d.cancel); updateLive(); if (ui.drawer) renderDrawer(); return; }
  if (d.move) { const r = selectedRoute(); return moveCand(r, d.cid, r.candidates.findIndex(c => c.id === d.cid) + +d.move); }
  if (d.prefer) return prefer(selectedRoute(), d.prefer);
  if (d.remove) return removeCand(selectedRoute(), d.remove);
  if (d.copy) return copyText(d.copy).then(() => toast('已复制 ' + d.copy));
  if (d.probe) { const s = sites.find(x => x.id === d.probe); return toast(`${s.name}：连通，耗时 ${randInt(90, 480)}ms（样稿模拟）`); }
  if (d.cmdk) return cmdkRun(+d.cmdk);
  if (t.classList.contains('node')) {
    const id = t.dataset.node;
    if (id.startsWith('md:')) { ui.selected = id.slice(3); return setView('routes'); }
    if (id.startsWith('gp:')) return setView('upstreams');
    return;
  }
  if (d.record) return openDrawer(d.record);
  if (d.live) return openDrawer(d.live);
  switch (d.act) {
    case 'cmdk': return openCmdk();
    case 'theme': return toggleTheme();
    case 'go-flow': return setView('flow');
    case 'new-route': return openRouteDialog('new');
    case 'add-cand': return openRouteDialog('add');
    case 'forward': return openForwardDialog();
    case 'unforward': return unforward();
    case 'close-dlg': return t.closest('dialog').close();
    case 'close-drawer': return closeDrawer();
    case 'toggle-sim': return toggleSim();
    case 'copy-model': return copyText(selectedRoute().name).then(() => toast('已复制模型名 ' + selectedRoute().name));
    case 'copy-endpoint': return copyText('http://127.0.0.1:8317/v1').then(() => toast('已复制接入地址'));
    case 'copy-diag': { const { rec } = findRecordOrLive(ui.drawer); return rec && copyText(diagText(rec)).then(() => toast('排查信息已复制，可以直接贴给别人')); }
    case 'log-for-model': ui.logQuery = selectedRoute().name; ui.logStatus = 'all'; return setView('log');
    case 'demo-only': return toast('样稿只演示布局，这个入口在正式页面里使用');
  }
});
document.addEventListener('change', e => {
  const d = e.target.dataset;
  if (d.protoToggle) { settings.protocols[d.protoToggle] = e.target.checked; onStateChange(); toast(`${PROTOS[d.protoToggle].label} 接口已${e.target.checked ? '开启' : '关闭'}`); }
  if (d.foToggle) { settings.failover[d.foToggle] = e.target.checked; onStateChange(); }
  if (d.siteToggle) { const s = sites.find(x => x.id === d.siteToggle); s.enabled = e.target.checked; onStateChange(); toast(`${s.name} 已${s.enabled ? '启用' : '停用'}`); }
  if (e.target.id === 'rd-proto') fillGroupSelect(e.target.value);
  if (e.target.id === 'rd-group') { $('#rd-remote').value = ''; fillRemoteList(); }
});
$('#route-form').addEventListener('submit', e => { if (!submitRouteDialog()) e.preventDefault(); });
$('#fwd-form').addEventListener('submit', e => { if (!submitForward()) e.preventDefault(); });
$('#cmdk-input').addEventListener('input', renderCmdk);
$('#cmdk').addEventListener('click', e => { if (e.target === e.currentTarget) e.currentTarget.close(); });
$('#cmdk-input').addEventListener('keydown', e => {
  if (e.key === 'ArrowDown') { e.preventDefault(); cmdkMove(1); }
  else if (e.key === 'ArrowUp') { e.preventDefault(); cmdkMove(-1); }
  else if (e.key === 'Enter') { e.preventDefault(); cmdkRun(cmdkIndex); }
});
document.addEventListener('keydown', e => {
  if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 'k') { e.preventDefault(); return openCmdk(); }
  if (e.key === 'Escape' && ui.drawer && !document.querySelector('dialog[open]')) return closeDrawer();
  const inField = e.target.closest('input, textarea, select, dialog');
  if (inField || ui.view !== 'routes') return;
  if (e.key === 'ArrowDown' || e.key === 'ArrowUp') {
    const rows = $$('#model-list .model-row');
    const i = rows.findIndex(r => r.dataset.route === ui.selected);
    const next = rows[clamp(i + (e.key === 'ArrowDown' ? 1 : -1), 0, rows.length - 1)];
    if (next) { e.preventDefault(); ui.selected = next.dataset.route; renderModelList(); renderDetail(); $(`#model-list [data-route="${CSS.escape(ui.selected)}"]`)?.scrollIntoView({ block: 'nearest' }); }
  }
});
window.addEventListener('hashchange', () => setView(location.hash.slice(1), { push: false }));

// 候选卡片拖拽排序
let dragId = null;
document.addEventListener('dragstart', e => {
  const card = e.target.closest?.('.cand[data-cand]');
  if (!card) return;
  dragId = card.dataset.cand;
  ui.dragging = true;
  card.classList.add('dragging');
  e.dataTransfer.effectAllowed = 'move';
  e.dataTransfer.setData('text/plain', dragId);
});
document.addEventListener('dragover', e => {
  const card = e.target.closest?.('.cand[data-cand]');
  if (!dragId || !card) return;
  e.preventDefault();
  const rect = card.getBoundingClientRect();
  const after = e.clientX > rect.left + rect.width / 2;
  $$('.drop-before, .drop-after').forEach(el => el.classList.remove('drop-before', 'drop-after'));
  if (card.dataset.cand !== dragId) card.classList.add(after ? 'drop-after' : 'drop-before');
});
document.addEventListener('drop', e => {
  const card = e.target.closest?.('.cand[data-cand]');
  if (!dragId || !card) return;
  e.preventDefault();
  const r = selectedRoute();
  const after = card.classList.contains('drop-after');
  const targetId = card.dataset.cand;
  const id = dragId;
  endDrag();
  if (targetId === id) return;
  const without = r.candidates.filter(c => c.id !== id);
  const to = without.findIndex(c => c.id === targetId) + (after ? 1 : 0);
  moveCand(r, id, to);
});
document.addEventListener('dragend', endDrag);
function endDrag() {
  dragId = null;
  ui.dragging = false;
  $$('.dragging, .drop-before, .drop-after').forEach(el => el.classList.remove('dragging', 'drop-before', 'drop-after'));
}

// ---------- 定时器 ----------
let spawnAt = now() + 600;
let lastCooling = '';
setInterval(() => {
  if (document.hidden) return;
  if (settings.simulate && now() >= spawnAt && live.filter(x => !x.done).length < 9) {
    spawnRequest();
    spawnAt = now() + rand(900, 2600);
  }
  simTick();
  if (ui.view === 'live') updateLive();
  if (ui.drawer && live.some(x => x.plan.id === ui.drawer && !x.done)) renderDrawer();
}, 250);
setInterval(() => {
  if (document.hidden) return;
  // 冷却结束会改变下一次起点：集合变化时整页重算，否则只走倒计时
  const cooling = [...cooldowns.keys()].filter(isCooling).join(',');
  if (cooling !== lastCooling) { lastCooling = cooling; onStateChange(); }
  for (const el of $$('[data-cd]')) el.textContent = fmtCountdown((cooldowns.get(el.dataset.cd) ?? 0) - now());
  renderRail();
  renderStatusbar();
  if (ui.view === 'log' && newRecordIds.size && !ui.logQuery) renderLogRows();
}, 1000);

// ---------- 启动 ----------
seedHistory();
lastCooling = [...cooldowns.keys()].filter(isCooling).join(',');
renderStatusbar();
setView(location.hash.slice(1) || 'routes', { push: false });
