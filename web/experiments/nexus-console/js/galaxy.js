/* NEXUS — route galaxy: downstream models on an inner orbit, upstream stations outside. */

import { esc, fmtInt, fmtMs } from "./core.js";

const W = 820;
const H = 520;
const CX = W / 2;
const CY = H / 2;

const PROTO_CLASS = { openai: "", "openai-chat": "is-chat", anthropic: "is-anthropic" };

function clamp(v, lo, hi) { return Math.max(lo, Math.min(hi, v)); }

function modelWidth(name) {
  return clamp(String(name).length * 6.1 + 30, 90, 162);
}

function upWidth(name) {
  return clamp(String(name).length * 6.1 + 26, 70, 138);
}

function orbit(index, count, rx, ry) {
  const angle = -Math.PI / 2 + (index / Math.max(1, count)) * Math.PI * 2;
  return { x: CX + Math.cos(angle) * rx, y: CY + Math.sin(angle) * ry, angle };
}

/**
 * 轨道上的标签会互相压住 —— 名字长的模型在椭圆顶端宽度超过相邻夹角对应的弦长。
 * 这里做几轮「推开 + 回弹」：节点仍被拉回轨道，但不会叠在一起，也不会掉进中心。
 */
function settle(items) {
  const minGap = 9;
  for (let iter = 0; iter < 140; iter++) {
    let moved = false;
    for (let i = 0; i < items.length; i++) {
      for (let j = i + 1; j < items.length; j++) {
        const a = items[i];
        const b = items[j];
        const ox = a.hw + b.hw + minGap - Math.abs(a.x - b.x);
        const oy = a.hh + b.hh + minGap - Math.abs(a.y - b.y);
        if (ox <= 0 || oy <= 0) continue;
        moved = true;
        if (oy < ox) {
          const dir = a.y <= b.y ? -1 : 1;
          const push = oy * 0.26;
          a.y += dir * push;
          b.y -= dir * push;
        } else {
          const dir = a.x <= b.x ? -1 : 1;
          const push = ox * 0.26;
          a.x += dir * push;
          b.x -= dir * push;
        }
      }
    }

    for (const n of items) {
      // 松开手以后回轨道，保持「星系」的形状
      n.x += (n.px - n.x) * 0.055;
      n.y += (n.py - n.y) * 0.055;
      // 模型不能压到中心核心
      if (n.ring === "model") {
        const dx = n.x - CX;
        const dy = n.y - CY;
        const dist = Math.hypot(dx, dy) || 1;
        if (dist < 122) {
          n.x = CX + (dx / dist) * 122;
          n.y = CY + (dy / dist) * 122;
        }
      }
      n.y = clamp(n.y, 22, H - 22);
      n.x = clamp(n.x, n.hw + 10, W - n.hw - 10);
    }
    if (!moved) break;
  }
}

export function renderGalaxy(stage, panel, models, selected, handlers) {
  if (!stage) return;
  const list = (models || []).filter((m) => (m.candidates && m.candidates.length) || m.forward_to);
  const drawn = list.slice(0, 14);

  const upMap = new Map();
  for (const m of drawn) {
    for (const c of m.candidates || []) {
      if (!upMap.has(c.upstream_id)) {
        upMap.set(c.upstream_id, { id: c.upstream_id, name: c.upstream_name, enabled: c.upstream_enabled });
      }
    }
  }
  const ups = [...upMap.values()].sort((a, b) => a.name.localeCompare(b.name)).slice(0, 16);

  const modelNodes = drawn.map((m, i) => {
    const p = orbit(i, drawn.length, 204, 152);
    const w = modelWidth(m.model_name);
    return {
      ring: "model",
      mi: i,
      x: p.x, y: p.y, px: p.x, py: p.y, hw: w / 2, hh: 15,
    };
  });
  const upNodes = ups.map((u, i) => {
    const p = orbit(i, ups.length, 338, 228);
    const w = upWidth(u.name);
    return { ring: "up", id: u.id, x: p.x, y: p.y, px: p.x, py: p.y, hw: w / 2, hh: 12.5 };
  });
  settle([...modelNodes, ...upNodes]);

  const modelPos = new Map(modelNodes.map((n) => [n.mi, { x: n.x, y: n.y }]));
  const upPos = new Map(upNodes.map((n) => [n.id, { x: n.x, y: n.y }]));

  if (!drawn.length) {
    stage.innerHTML = `<div class="empty" style="height:100%;justify-content:center">
      <span class="e-mark">◎</span>
      <b>还没有下游模型</b>
      <span>在「模型」页新增一条链之后，这里会画出它和上游站点的连接。</span>
    </div>`;
    if (panel) panel.innerHTML = "";
    return;
  }

  const links = [];
  drawn.forEach((m, mi) => {
    const from = modelPos.get(mi);
    for (const c of m.candidates || []) {
      const to = upPos.get(c.upstream_id);
      if (!to) continue;
      const mx = (from.x + to.x) / 2;
      const my = (from.y + to.y) / 2;
      const dx = to.x - from.x;
      const dy = to.y - from.y;
      const bend = 0.09 + (c.priority % 3) * 0.03;
      const cx = mx - dy * bend * 0.55;
      const cy = my + dx * bend * 0.55;
      const active = c.route_id === m.active_route_id;
      links.push({
        mi, to: c.upstream_id, active,
        proto: m.protocol,
        d: `M${from.x.toFixed(1)},${from.y.toFixed(1)} Q${cx.toFixed(1)},${cy.toFixed(1)} ${to.x.toFixed(1)},${to.y.toFixed(1)}`,
      });
    }
  });

  const linkSvg = links.map((l, i) => `
    <path id="gl-${i}" class="g-link ${PROTO_CLASS[l.proto] || ""} ${l.active ? "is-on" : ""}"
      data-link-model="${l.mi}" data-link-up="${l.to}" data-idx="${i}"
      d="${l.d}" />`).join("")
    + links.filter((l) => l.active).map((l, j) => {
      const idx = links.indexOf(l);
      return `<circle class="g-flow ${PROTO_CLASS[l.proto] || ""}" r="1.9">
        <animateMotion dur="${(2.8 + (j % 5) * 0.35).toFixed(2)}s" begin="${(j * 0.27).toFixed(2)}s" repeatCount="indefinite">
          <mpath href="#gl-${idx}"/>
        </animateMotion>
      </circle>`;
    }).join("");

  const upSvg = ups.map((u) => {
    const p = upPos.get(u.id);
    const w = upWidth(u.name);
    return `
    <g class="g-node is-up" data-ui="${u.id}" transform="translate(${p.x.toFixed(1)},${p.y.toFixed(1)})" role="button" tabindex="0" aria-label="上游 ${esc(u.name)}">
      <rect class="g-chip" x="${(-w / 2).toFixed(1)}" y="-12.5" width="${w}" height="25" rx="8"/>
      <circle cx="${(-w / 2 + 11).toFixed(1)}" cy="0" r="2.6" fill="${u.enabled ? "var(--ok)" : "var(--bad)"}" opacity=".9"/>
      <text class="g-label" x="${(-w / 2 + 19).toFixed(1)}" y="3.6" text-anchor="start">${esc(u.name.length > 15 ? u.name.slice(0, 14) + "…" : u.name)}</text>
    </g>`;
  }).join("");

  const modelSvg = drawn.map((m, i) => {
    const p = modelPos.get(i);
    const w = modelWidth(m.model_name);
    const isSel = selected && selected.model_name === m.model_name && selected.protocol === m.protocol;
    const isStart = m.active_route_id != null;
    const label = m.model_name.length > 20 ? `${m.model_name.slice(0, 19)}…` : m.model_name;
    const cand = (m.candidates || []).length;
    return `
    <g class="g-node is-model ${isSel ? "is-sel" : ""} ${isStart ? "is-active-start" : ""}"
       data-mi="${i}" transform="translate(${p.x.toFixed(1)},${p.y.toFixed(1)})"
       role="button" tabindex="0" aria-label="模型 ${esc(m.model_name)}，${cand} 条候选">
      <rect class="g-chip" x="${(-w / 2).toFixed(1)}" y="-15" width="${w}" height="30" rx="10"/>
      <circle cx="${(-w / 2 + 12).toFixed(1)}" cy="0" r="3" fill="${isStart ? "var(--ok)" : "var(--brand)"}" opacity="${isStart ? "1" : ".65"}"/>
      <text class="g-label" x="${(-w / 2 + 21).toFixed(1)}" y="3.7" text-anchor="start">${esc(label)}</text>
      <text class="g-sub" x="${(w / 2 - 9).toFixed(1)}" y="3.4" text-anchor="end">${m.forward_to ? "↦" : cand}</text>
    </g>`;
  }).join("");

  stage.innerHTML = `
  <svg viewBox="0 0 ${W} ${H}" preserveAspectRatio="xMidYMid meet" role="img" aria-label="模型到上游的路由图">
    <defs>
      <radialGradient id="core-glow" cx="50%" cy="50%" r="50%">
        <stop offset="0" stop-color="#8b7bff" stop-opacity=".55"/>
        <stop offset=".55" stop-color="#6d5cf0" stop-opacity=".16"/>
        <stop offset="1" stop-color="#8b7bff" stop-opacity="0"/>
      </radialGradient>
      <linearGradient id="core-fill" x1="0" y1="0" x2="0" y2="1">
        <stop offset="0" stop-color="#a78bfa" stop-opacity=".38"/>
        <stop offset="1" stop-color="#6d5cf0" stop-opacity=".12"/>
      </linearGradient>
    </defs>
    <ellipse class="g-ring" cx="${CX}" cy="${CY}" rx="204" ry="152"/>
    <ellipse class="g-ring" cx="${CX}" cy="${CY}" rx="338" ry="228" opacity=".75"/>
    <ellipse class="g-ring-2" cx="${CX}" cy="${CY}" rx="271" ry="190"/>
    <circle cx="${CX}" cy="${CY}" r="118" fill="url(#core-glow)" opacity=".55"/>

    <g class="g-links">${linkSvg}</g>

    <g class="g-core" transform="translate(${CX},${CY})">
      <circle class="g-pulse" cx="0" cy="0" r="30"/>
      <circle class="g-halo" cx="0" cy="0" r="46"/>
      <circle class="g-halo2" cx="0" cy="0" r="60"/>
      <path class="g-core-hex" d="M0,-30 26,-15 26,15 0,30 -26,15 -26,-15Z" fill="url(#core-fill)"/>
      <path d="M0,-15 13,-7.5 13,7.5 0,15 -13,7.5 -13,-7.5Z" fill="rgba(139,123,255,.22)" stroke="rgba(167,139,250,.7)" stroke-width=".9"/>
      <circle class="g-core-dot" cx="0" cy="0" r="2.8"/>
      <text class="g-core-label" x="0" y="-43" text-anchor="middle">GATEWAY</text>
      <text class="g-core-sub" x="0" y="47" text-anchor="middle">${fmtInt(list.length)} 模型 · ${fmtInt(ups.length)} 上游</text>
    </g>

    <g class="g-ups">${upSvg}</g>
    <g class="g-models">${modelSvg}</g>
  </svg>`;

  const svg = stage.querySelector("svg");
  if (!stage._scrollBound) {
    stage._scrollBound = true;
    stage.addEventListener("scroll", () => { stage._prevScroll = stage.scrollLeft; }, { passive: true });
  }
  requestAnimationFrame(() => {
    const max = stage.scrollWidth - stage.clientWidth;
    if (max > 4) {
      stage.scrollLeft = stage._prevScroll > 0 ? Math.min(stage._prevScroll, max) : max / 2;
    }
  });
  const selectedIndex = selected
    ? drawn.findIndex((m) => m.model_name === selected.model_name && m.protocol === selected.protocol)
    : -1;

  const focus = (mi, upId) => {
    svg.querySelectorAll(".g-link").forEach((el) => {
      const match = mi != null
        ? el.dataset.linkModel === String(mi)
        : upId != null ? el.dataset.linkUp === String(upId) : false;
      el.classList.toggle("is-on", match);
      el.classList.toggle("is-live", match);
    });
    svg.querySelectorAll(".g-node").forEach((el) => {
      if (el.classList.contains("is-up")) {
        const hit = upId != null
          ? el.dataset.ui === String(upId)
          : mi != null ? links.some((l) => l.mi === mi && l.to === Number(el.dataset.ui)) : false;
        el.classList.toggle("is-touch", hit);
        el.classList.toggle("is-dim", mi != null && !hit);
      } else {
        const idx = Number(el.dataset.mi);
        const isSel = mi != null ? idx === mi : idx === selectedIndex;
        el.classList.toggle("is-sel", isSel);
        el.classList.toggle("is-dim", mi != null && idx !== mi);
      }
    });
  };

  const restore = () => focus(selectedIndex >= 0 ? selectedIndex : null, null);
  restore();

  svg.addEventListener("pointerover", (e) => {
    const model = e.target.closest("[data-mi]");
    if (model) { focus(Number(model.dataset.mi), null); return; }
    const up = e.target.closest("[data-ui]");
    if (up) focus(null, up.dataset.ui);
  });
  svg.addEventListener("pointerleave", restore);
  svg.addEventListener("click", (e) => {
    const model = e.target.closest("[data-mi]");
    if (model) {
      const idx = Number(model.dataset.mi);
      const m = drawn[idx];
      if (m) handlers.onSelect?.(m);
      return;
    }
    const up = e.target.closest("[data-ui]");
    if (up && handlers.onUpstream) handlers.onUpstream(Number(up.dataset.ui));
  });
  svg.addEventListener("keydown", (e) => {
    if (e.key !== "Enter" && e.key !== " ") return;
    const model = e.target.closest("[data-mi]");
    if (model) {
      e.preventDefault();
      handlers.onSelect?.(drawn[Number(model.dataset.mi)]);
    }
  });

  renderPanel(panel, selected ? list.find((m) => m.model_name === selected.model_name && m.protocol === selected.protocol) : null, list, handlers);
}

function renderPanel(panel, model, models, handlers) {
  if (!panel) return;
  if (!model) {
    const totalCandidates = models.reduce((n, m) => n + (m.candidates?.length || 0), 0);
    const forward = models.filter((m) => m.forward_to).length;
    const upNames = new Set();
    for (const m of models) for (const c of m.candidates || []) upNames.add(c.upstream_name);
    panel.innerHTML = `
      <div class="gp-summary">
        <div class="gp-eyebrow">SELECT A MODEL</div>
        <div class="gp-name" style="font-size:16px">选一个气泡，展开它的链</div>
        <div class="gp-meta">
          <span class="tag">${fmtInt(models.length)} 条下游链</span>
          <span class="tag">${fmtInt(totalCandidates)} 个候选</span>
          <span class="tag">${fmtInt(upNames.size)} 个上游</span>
          ${forward ? `<span class="tag brand">${forward} 条交接中</span>` : ""}
        </div>
      </div>
      <div class="gp-chain">
        ${models.slice(0, 4).map((m) => `
          <button type="button" class="gp-hop" data-pick="${esc(m.model_name)}" data-proto="${esc(m.protocol)}" style="text-align:left;cursor:pointer">
            <span class="n">→</span>
            <span class="u">${esc(m.model_name)}</span>
            <span class="r">${m.candidates?.length || 0} 候选</span>
          </button>`).join("")}
      </div>
      <p class="gp-note">有绿边的气泡表示当前有可用起点。连线指向实际的调用站点，实线是保存的首选，虚线动画是这次请求的起点。</p>`;
    panel.querySelectorAll("[data-pick]").forEach((btn) => {
      btn.addEventListener("click", () => {
        const found = models.find((m) => m.model_name === btn.dataset.pick && m.protocol === btn.dataset.proto);
        if (found) handlers.onSelect?.(found);
      });
    });
    return;
  }

  const start = (model.candidates || []).find((c) => c.route_id === model.active_route_id);
  const preferred = (model.candidates || []).find((c) => c.route_id === model.preferred_route_id);
  const hops = (model.candidates || []).map((c) => {
    const isStart = c.route_id === model.active_route_id;
    const tags = [];
    if (c.route_id === model.preferred_route_id) tags.push('<span class="tag brand">首选</span>');
    if (isStart) tags.push('<span class="tag ok">起点</span>');
    if (c.cooling_ms > 0) tags.push(`<span class="tag bad">冷却 ${fmtMs(c.cooling_ms)}</span>`);
    if (!c.group_enabled || !c.upstream_enabled) tags.push('<span class="tag warn">停用</span>');
    return `
      <div class="gp-hop ${isStart ? "is-start" : ""} ${c.cooling_ms > 0 ? "is-cool" : ""}">
        <span class="n">${c.priority + 1}</span>
        <span class="u">${esc(c.upstream_name)}</span>
        <span class="r">${esc(c.remote_model || "同名")}</span>
      </div>`;
  }).join("");

  const protoLabel = handlers.protocolLabel
    ? handlers.protocolLabel(model.protocol)
    : model.protocol;

  panel.innerHTML = `
    <div class="gp-summary">
      <div class="gp-eyebrow">${esc(protoLabel)}</div>
      <div class="gp-name">${esc(model.model_name)}</div>
      <div class="gp-meta">
        ${start ? `<span class="tag ok"><i class="st-dot"></i>起点 ${esc(start.upstream_name)}</span>` : `<span class="tag bad">无可用起点</span>`}
        ${model.forward_to ? `<span class="tag brand">↦ ${esc(model.forward_to)}</span>` : `<span class="tag">${(model.candidates || []).length} 个候选</span>`}
      </div>
    </div>
    <div class="gp-chain">${hops || '<div class="gp-note">这条链没有自己的候选，请求按交接目标走。</div>'}</div>
    <div class="gp-actions">
      <button type="button" class="btn btn-sm btn-primary" data-gp-action="detail">查看详情</button>
      ${preferred && preferred.route_id !== model.active_route_id ? `<button type="button" class="btn btn-sm" data-gp-action="switch" data-route="${preferred.route_id}">启用首选</button>` : ""}
      ${start ? `<button type="button" class="btn btn-sm btn-ghost" data-gp-action="live" data-model="${esc(model.model_name)}">看实时</button>` : ""}
    </div>
    <p class="gp-note">${model.forward_to ? "整条链已交给目标模型，自己的候选暂时不参与路由。" : "顺序就是自动降级的尝试顺序；换首选不会重排候选。"}</p>`;

  panel.querySelectorAll("[data-gp-action]").forEach((btn) => {
    btn.addEventListener("click", () => {
      handlers.onAction?.(btn.dataset.gpAction, {
        model,
        routeId: Number(btn.dataset.route || 0),
        modelName: btn.dataset.model,
      });
    });
  });
}
