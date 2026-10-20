/* NEXUS — traffic area chart. Hand-rolled SVG: no chart library, no runtime deps. */

import { bucketLabel, fmtInt } from "./core.js";

const PAD = { l: 42, r: 14, t: 16, b: 26 };

function niceMax(value) {
  if (value <= 4) return 4;
  const exp = Math.floor(Math.log10(value));
  const base = 10 ** exp;
  const n = value / base;
  const step = n <= 1 ? 1 : n <= 2 ? 2 : n <= 4 ? 4 : n <= 5 ? 5 : 10;
  return step * base;
}

function smooth(points) {
  if (!points.length) return "";
  let d = `M${points[0][0].toFixed(2)},${points[0][1].toFixed(2)}`;
  for (let i = 0; i < points.length - 1; i++) {
    const p0 = points[i - 1] || points[i];
    const p1 = points[i];
    const p2 = points[i + 1];
    const p3 = points[i + 2] || p2;
    const t = 0.16;
    const c1x = p1[0] + (p2[0] - p0[0]) * t;
    const c1y = p1[1] + (p2[1] - p0[1]) * t;
    const c2x = p2[0] - (p3[0] - p1[0]) * t;
    const c2y = p2[1] - (p3[1] - p1[1]) * t;
    d += ` C${c1x.toFixed(2)},${c1y.toFixed(2)} ${c2x.toFixed(2)},${c2y.toFixed(2)} ${p2[0].toFixed(2)},${p2[1].toFixed(2)}`;
  }
  return d;
}

export function mountChart(svg, tip, payload, opts = {}) {
  if (!svg || !payload) return;
  if (tip) tip.hidden = true;
  const points = payload.points || [];
  const win = payload.window || "24h";
  const wrap = svg.parentElement;
  const W = Math.max(320, Math.round(wrap.clientWidth - 20));
  const H = Math.max(240, Math.round(wrap.clientHeight || svg.clientHeight || 268));
  const plotW = W - PAD.l - PAD.r;
  const plotH = H - PAD.t - PAD.b;
  const base = PAD.t + plotH;

  svg.setAttribute("viewBox", `0 0 ${W} ${H}`);
  svg.setAttribute("width", W);
  svg.setAttribute("height", H);

  const maxN = niceMax(Math.max(1, ...points.map((p) => p.n || 0)));
  const stepX = points.length > 1 ? plotW / (points.length - 1) : plotW;

  const pos = points.map((p, i) => [PAD.l + i * stepX, base - ((p.n || 0) / maxN) * plotH]);
  const posErr = points.map((p, i) => [PAD.l + i * stepX, base - ((p.err || 0) / maxN) * plotH]);

  const line = smooth(pos);
  const area = `${line} L${(PAD.l + plotW).toFixed(2)},${base} L${PAD.l},${base} Z`;
  const errLine = smooth(posErr);
  const errArea = `${errLine} L${(PAD.l + plotW).toFixed(2)},${base} L${PAD.l},${base} Z`;

  const grid = [0, 0.25, 0.5, 0.75, 1].map((f) => {
    const y = PAD.t + plotH * f;
    const value = maxN * (1 - f);
    return `
      <line class="chart-grid-line" x1="${PAD.l}" y1="${y.toFixed(2)}" x2="${PAD.l + plotW}" y2="${y.toFixed(2)}" ${f === 1 ? 'opacity=".9"' : 'opacity=".55"'} />
      <text class="chart-axis-text" x="${PAD.l - 9}" y="${(y + 3).toFixed(2)}" text-anchor="end">${fmtInt(value)}</text>`;
  }).join("");

  const ticks = [0, 0.25, 0.5, 0.75, 1].map((f) => {
    const idx = Math.min(points.length - 1, Math.round(f * (points.length - 1)));
    const x = PAD.l + idx * stepX;
    const anchor = f === 0 ? "start" : f === 1 ? "end" : "middle";
    return `<text class="chart-axis-text" x="${x.toFixed(2)}" y="${H - 7}" text-anchor="${anchor}">${bucketLabel(points[idx]?.t || 0, win)}</text>`;
  }).join("");

  const total = points.reduce((sum, p) => sum + (p.n || 0), 0);
  const errors = points.reduce((sum, p) => sum + (p.err || 0), 0);
  const peak = points.reduce((best, p) => Math.max(best, p.n || 0), 0);

  svg.innerHTML = `
    <defs>
      <linearGradient id="req-grad" x1="0" y1="0" x2="0" y2="1">
        <stop offset="0" stop-color="#8b7bff" stop-opacity=".46"/>
        <stop offset=".55" stop-color="#7c6cff" stop-opacity=".14"/>
        <stop offset="1" stop-color="#7c6cff" stop-opacity="0"/>
      </linearGradient>
      <linearGradient id="err-grad" x1="0" y1="0" x2="0" y2="1">
        <stop offset="0" stop-color="#ff647d" stop-opacity=".24"/>
        <stop offset="1" stop-color="#ff647d" stop-opacity="0"/>
      </linearGradient>
    </defs>
    ${grid}
    <path class="chart-area-err" d="${errArea}"/>
    <path class="chart-area-req" d="${area}"/>
    <path class="chart-line-err" d="${errLine}"/>
    <path class="chart-line-req" d="${line}"/>
    ${total === 0 ? `<text class="chart-empty" x="${(PAD.l + plotW / 2).toFixed(1)}" y="${(PAD.t + plotH / 2).toFixed(1)}" text-anchor="middle">这个时间窗还没有请求</text>` : ""}
    ${ticks}
    <g class="chart-cursor-layer" style="opacity:0">
      <line class="chart-cursor" y1="${PAD.t}" y2="${base}"/>
      <circle class="chart-dot" r="3.6" data-dot="req"/>
      <circle class="chart-dot" r="2.8" data-dot="err" style="stroke:#ff647d"/>
    </g>
    <rect class="chart-hit" x="${PAD.l}" y="${PAD.t}" width="${plotW}" height="${plotH}"/>
  `;

  const lineEl = svg.querySelector(".chart-line-req");
  if (opts.animate !== false && lineEl && typeof lineEl.getTotalLength === "function") {
    const len = lineEl.getTotalLength();
    lineEl.style.strokeDasharray = `${len}`;
    lineEl.style.strokeDashoffset = `${len}`;
    lineEl.getBoundingClientRect();
    lineEl.style.transition = "stroke-dashoffset .95s cubic-bezier(.22,.9,.28,1)";
    lineEl.style.strokeDashoffset = "0";
    const areaEl = svg.querySelector(".chart-area-req");
    if (areaEl) {
      areaEl.style.opacity = "0";
      areaEl.style.transition = "opacity .8s ease .15s";
      areaEl.getBoundingClientRect();
      areaEl.style.opacity = "1";
    }
  }

  const cursor = svg.querySelector(".chart-cursor-layer");
  const cursorLine = svg.querySelector(".chart-cursor");
  const dotReq = svg.querySelector('[data-dot="req"]');
  const dotErr = svg.querySelector('[data-dot="err"]');
  const hit = svg.querySelector(".chart-hit");

  const hide = () => {
    if (cursor) cursor.style.opacity = "0";
    if (tip) tip.hidden = true;
  };

  if (hit) {
    hit.addEventListener("pointermove", (e) => {
      const rect = svg.getBoundingClientRect();
      const x = ((e.clientX - rect.left) / rect.width) * W;
      const idx = Math.max(0, Math.min(points.length - 1, Math.round((x - PAD.l) / (stepX || 1))));
      const p = points[idx];
      if (!p) return;
      const px = pos[idx][0];
      const py = pos[idx][1];
      const ey = posErr[idx][1];

      if (cursor) cursor.style.opacity = "1";
      if (cursorLine) { cursorLine.setAttribute("x1", px); cursorLine.setAttribute("x2", px); }
      if (dotReq) { dotReq.setAttribute("cx", px); dotReq.setAttribute("cy", py); }
      if (dotErr) { dotErr.setAttribute("cx", px); dotErr.setAttribute("cy", ey); }

      if (tip) {
        const wrapRect = wrap.getBoundingClientRect();
        const left = ((px / W) * rect.width) + (rect.left - wrapRect.left);
        const top = ((py / H) * rect.height) + (rect.top - wrapRect.top);
        tip.hidden = false;
        tip.style.left = `${Math.max(86, Math.min(wrapRect.width - 86, left))}px`;
        tip.style.top = `${Math.max(92, top)}px`;
        tip.innerHTML = `
          <div class="tip-time">${bucketLabel(p.t, win)}</div>
          <div class="tip-row"><span>请求</span><b>${fmtInt(p.n)}</b></div>
          <div class="tip-row bad"><span>失败</span><b>${fmtInt(p.err)}</b></div>
          <div class="tip-row"><span>输入</span><b>${fmtInt(p.ti)}</b></div>
          <div class="tip-row"><span>输出</span><b>${fmtInt(p.to)}</b></div>`;
      }
    });
    hit.addEventListener("pointerleave", hide);
  }

  const note = document.querySelector("#chart-note");
  if (note) {
    note.textContent = `共 ${fmtInt(total)} 次 · 失败 ${fmtInt(errors)} · 峰值 ${fmtInt(peak)}/桶`;
  }
  return { W, H, total, errors };
}
