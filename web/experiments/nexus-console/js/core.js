/* NEXUS — shared primitives: API, formatting, tiny DOM helpers, toasts, field. */

export const API = "/admin/api";

export const $ = (sel, root = document) => root.querySelector(sel);
export const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));

export function esc(value) {
  return String(value ?? "").replace(/[&<>"']/g, (c) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  }[c]));
}

export async function api(path, options = {}) {
  const opts = { ...options, headers: { ...(options.headers || {}) } };
  if (opts.body && typeof opts.body !== "string") {
    opts.headers["content-type"] = "application/json";
    opts.body = JSON.stringify(opts.body);
  }
  const res = await fetch(API + path, opts);
  const text = await res.text();
  let data = null;
  if (text) {
    try { data = JSON.parse(text); } catch { data = text; }
  }
  if (!res.ok) {
    const detail = data && typeof data === "object" ? data.detail || data.message : data;
    throw new Error(typeof detail === "string" && detail ? detail : `HTTP ${res.status}`);
  }
  return data;
}

/* ------------------------------------------------------------------ 数字 */

const NF = new Intl.NumberFormat("zh-CN");

export function fmtInt(n) {
  return NF.format(Math.round(Number(n) || 0));
}

export function fmtCompact(n) {
  const v = Number(n) || 0;
  if (Math.abs(v) < 1000) return String(Math.round(v));
  if (Math.abs(v) < 1e8) return `${(v / 1e4).toFixed(v < 1e5 ? 1 : 0)} 万`;
  return `${(v / 1e8).toFixed(2)} 亿`;
}

export function fmtBytes(n) {
  const v = Number(n) || 0;
  if (v < 1024) return `${Math.round(v)} B`;
  if (v < 1024 ** 2) return `${(v / 1024).toFixed(1)} KB`;
  if (v < 1024 ** 3) return `${(v / 1024 ** 2).toFixed(1)} MB`;
  return `${(v / 1024 ** 3).toFixed(2)} GB`;
}

export function fmtMs(ms) {
  const v = Number(ms) || 0;
  if (v < 1000) return `${Math.round(v)}ms`;
  if (v < 60000) return `${(v / 1000).toFixed(v < 10000 ? 2 : 1)}s`;
  const m = Math.floor(v / 60000);
  const s = Math.round((v % 60000) / 1000);
  return `${m}m ${String(s).padStart(2, "0")}s`;
}

export function fmtPct(v, digits = 1) {
  return `${((Number(v) || 0) * 100).toFixed(digits)}%`;
}

export function parseTime(ts) {
  if (!ts) return null;
  const d = new Date(String(ts).replace(" ", "T"));
  return Number.isNaN(d.getTime()) ? null : d;
}

export function fmtClock(ts) {
  const d = parseTime(ts);
  if (!d) return "--:--:--";
  return d.toLocaleTimeString("zh-CN", { hour12: false });
}

export function fmtWhen(ts) {
  const d = parseTime(ts);
  if (!d) return "—";
  const now = Date.now();
  const diff = Math.floor((now - d.getTime()) / 1000);
  if (diff < 10) return "刚刚";
  if (diff < 60) return `${diff} 秒前`;
  if (diff < 3600) return `${Math.floor(diff / 60)} 分钟前`;
  if (diff < 86400) return `${Math.floor(diff / 3600)} 小时前`;
  return d.toLocaleDateString("zh-CN", { month: "2-digit", day: "2-digit" });
}

export function bucketLabel(epoch, window) {
  const d = new Date(epoch * 1000);
  if (window === "7d") return `${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
  return `${String(d.getHours()).padStart(2, "0")}:${String(d.getMinutes()).padStart(2, "0")}`;
}

/* ------------------------------------------------------------------ 计数动画 */

const lastValues = new Map();

export function animNum(el, key, to, format = fmtInt, duration = 900) {
  if (!el) return;
  const target = Number(to) || 0;
  const from = lastValues.has(key) ? lastValues.get(key) : 0;
  lastValues.set(key, target);
  const reduced = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  if (reduced || Math.abs(target - from) < 0.0001) {
    el.textContent = format(target);
    return;
  }
  cancelAnimationFrame(el._raf || 0);
  const t0 = performance.now();
  const step = (now) => {
    const p = Math.min(1, (now - t0) / duration);
    const eased = 1 - Math.pow(1 - p, 3);
    el.textContent = format(from + (target - from) * eased);
    if (p < 1) el._raf = requestAnimationFrame(step);
  };
  el._raf = requestAnimationFrame(step);
}

/* ------------------------------------------------------------------ 迷你图 */

export function sparkPaths(values, width = 100, height = 30) {
  const nums = values && values.length ? values.map((v) => Number(v) || 0) : [0, 0];
  const max = Math.max(...nums, 1);
  const min = 0;
  const dx = nums.length > 1 ? width / (nums.length - 1) : width;
  const pts = nums.map((v, i) => [i * dx, height - 2 - ((v - min) / (max - min || 1)) * (height - 5)]);
  let line = `M${pts[0][0].toFixed(2)},${pts[0][1].toFixed(2)}`;
  for (let i = 0; i < pts.length - 1; i++) {
    const p0 = pts[i - 1] || pts[i];
    const p1 = pts[i];
    const p2 = pts[i + 1];
    const p3 = pts[i + 2] || p2;
    const t = 0.16;
    const c1x = p1[0] + (p2[0] - p0[0]) * t;
    const c1y = p1[1] + (p2[1] - p0[1]) * t;
    const c2x = p2[0] - (p3[0] - p1[0]) * t;
    const c2y = p2[1] - (p3[1] - p1[1]) * t;
    line += ` C${c1x.toFixed(2)},${c1y.toFixed(2)} ${c2x.toFixed(2)},${c2y.toFixed(2)} ${p2[0].toFixed(2)},${p2[1].toFixed(2)}`;
  }
  const area = `${line} L${width},${height} L0,${height} Z`;
  return { line, area };
}

/* ------------------------------------------------------------------ 提示条 */

export function toast(title, detail = "", kind = "ok") {
  const host = $("#toasts");
  if (!host) return;
  const node = document.createElement("div");
  node.className = `toast ${kind === "ok" ? "" : kind}`.trim();
  node.innerHTML = `<b>${esc(title)}</b>${detail ? `<p>${esc(detail)}</p>` : ""}<span class="toast-bar"></span>`;
  host.appendChild(node);
  const kill = () => {
    node.classList.add("is-out");
    setTimeout(() => node.remove(), 260);
  };
  node.addEventListener("click", kill);
  setTimeout(kill, 4200);
}

export async function copyText(text) {
  try {
    await navigator.clipboard.writeText(text);
    toast("已复制", text);
  } catch {
    toast("复制失败", "浏览器拒绝了剪贴板访问", "err");
  }
}

/* ------------------------------------------------------------------ 确认框 */

let modalResolve = null;

export function confirmDialog(title, text, okLabel = "确认") {
  const wrap = $("#modal");
  $("#modal-title").textContent = title;
  $("#modal-text").textContent = text;
  $("#modal-ok").textContent = okLabel;
  wrap.hidden = false;
  return new Promise((resolve) => {
    modalResolve = resolve;
  });
}

export function initModal() {
  const wrap = $("#modal");
  const finish = (value) => {
    if (wrap.hidden) return;
    wrap.hidden = true;
    if (modalResolve) modalResolve(value);
    modalResolve = null;
  };
  $("#modal-ok").addEventListener("click", () => finish(true));
  $("#modal-cancel").addEventListener("click", () => finish(false));
  wrap.querySelector("[data-close-modal]").addEventListener("click", () => finish(false));
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape" && !wrap.hidden) finish(false);
  });
}

/* ------------------------------------------------------------------ 背景场 */

export function initField() {
  const canvas = $("#field");
  if (!canvas) return;
  const ctx = canvas.getContext("2d");
  const reduced = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  let w = 0, h = 0, dpr = 1, dots = [], raf = 0;
  const mouse = { x: -9999, y: -9999 };

  const theme = () => (document.documentElement.dataset.theme === "light" ? "light" : "dark");
  const palette = () => (theme() === "light"
    ? { dot: "rgba(84,72,190,", line: "rgba(84,72,190,", mouse: "rgba(14,159,196," }
    : { dot: "rgba(170,160,255,", line: "rgba(139,123,255,", mouse: "rgba(52,214,240," });

  function seed() {
    const count = Math.max(26, Math.min(78, Math.round((w * h) / 26000)));
    dots = Array.from({ length: count }, () => ({
      x: Math.random() * w,
      y: Math.random() * h,
      vx: (Math.random() - 0.5) * 0.16,
      vy: (Math.random() - 0.5) * 0.16,
      r: Math.random() * 1.25 + 0.35,
      p: Math.random() * Math.PI * 2,
    }));
  }

  function resize() {
    dpr = Math.min(2, window.devicePixelRatio || 1);
    w = window.innerWidth;
    h = window.innerHeight;
    canvas.width = Math.round(w * dpr);
    canvas.height = Math.round(h * dpr);
    canvas.style.width = `${w}px`;
    canvas.style.height = `${h}px`;
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    seed();
    if (reduced) draw(0);
  }

  function draw(t) {
    ctx.clearRect(0, 0, w, h);
    const c = palette();
    const light = theme() === "light";
    const baseAlpha = light ? 0.34 : 0.55;

    for (const d of dots) {
      if (!reduced) {
        d.x += d.vx;
        d.y += d.vy;
        if (d.x < -20) d.x = w + 20;
        if (d.x > w + 20) d.x = -20;
        if (d.y < -20) d.y = h + 20;
        if (d.y > h + 20) d.y = -20;
      }
      const tw = 0.55 + 0.45 * Math.sin(t / 1600 + d.p);
      ctx.beginPath();
      ctx.arc(d.x, d.y, d.r, 0, Math.PI * 2);
      ctx.fillStyle = `${c.dot}${(baseAlpha * tw * 0.5).toFixed(3)})`;
      ctx.fill();
    }

    for (let i = 0; i < dots.length; i++) {
      for (let j = i + 1; j < dots.length; j++) {
        const a = dots[i], b = dots[j];
        const dx = a.x - b.x, dy = a.y - b.y;
        const dist = Math.hypot(dx, dy);
        if (dist > 132) continue;
        ctx.beginPath();
        ctx.moveTo(a.x, a.y);
        ctx.lineTo(b.x, b.y);
        ctx.strokeStyle = `${c.line}${((1 - dist / 132) * (light ? 0.06 : 0.1)).toFixed(3)})`;
        ctx.lineWidth = 0.7;
        ctx.stroke();
      }
    }

    for (const d of dots) {
      const dx = d.x - mouse.x, dy = d.y - mouse.y;
      const dist = Math.hypot(dx, dy);
      if (dist > 170) continue;
      ctx.beginPath();
      ctx.moveTo(d.x, d.y);
      ctx.lineTo(mouse.x, mouse.y);
      ctx.strokeStyle = `${c.mouse}${((1 - dist / 170) * 0.22).toFixed(3)})`;
      ctx.lineWidth = 0.8;
      ctx.stroke();
      d.x += dx * 0.0016;
      d.y += dy * 0.0016;
    }

    if (!reduced) raf = requestAnimationFrame(draw);
  }

  function start() {
    cancelAnimationFrame(raf);
    if (reduced) { draw(0); return; }
    raf = requestAnimationFrame(draw);
  }

  window.addEventListener("resize", () => { resize(); start(); });
  window.addEventListener("pointermove", (e) => { mouse.x = e.clientX; mouse.y = e.clientY; }, { passive: true });
  window.addEventListener("pointerleave", () => { mouse.x = -9999; mouse.y = -9999; });
  document.addEventListener("visibilitychange", () => {
    if (document.hidden) cancelAnimationFrame(raf);
    else start();
  });
  resize();
  start();
}

/* ------------------------------------------------------------------ 主题 */

export function applyTheme(theme) {
  document.documentElement.dataset.theme = theme;
  try { localStorage.setItem("nexus-theme", theme); } catch { /* 私密模式 */ }
  window.dispatchEvent(new CustomEvent("nexus-theme"));
}

export function initTheme() {
  let stored = null;
  try { stored = localStorage.getItem("nexus-theme"); } catch { /* 忽略 */ }
  // 控制台以深色为首要形态；只有用户主动选过浅色才跟随。
  if (stored !== "light" && stored !== "dark") stored = "dark";
  applyTheme(stored);
}
