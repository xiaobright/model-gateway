/* 主题风格（皮肤）机制。
   明暗与风格是两个正交的轴：明暗走 data-theme（auto / light / dark），风格走
   data-style。风格只重新声明 style.css :root 里的同一批 token，所以每种风格都
   要自己用 light-dark() 给出两套明暗值；两个属性同在 <html> 上，color-scheme
   仍由 data-theme 决定，互不干扰。
   新增风格 = 在 STYLES 登记 + 在 style.css 加一段 :root[data-style="id"]，
   JS 不用动。 */
'use strict';

export const STYLES = [
  { id: 'glass', name: '轻盈玻璃', hint: '默认风格：通透的工作台，环境色缓慢流动，玻璃只做在侧栏、面板、指标卡和弹窗上。' },
];

export const DEFAULT_STYLE = 'glass';
const STORAGE_KEY = 'mg-style';

export const isValidStyle = (id) => STYLES.some((s) => s.id === id);

export const styleHint = (id) => STYLES.find((s) => s.id === id)?.hint || '';

/* 存档里可能是旧版本的值、手改坏的值，或隐私模式下根本读不到：一律回落到默认 */
export function normalizeStyle(saved) {
  return isValidStyle(saved) ? saved : DEFAULT_STYLE;
}

export function loadStyle() {
  try { return normalizeStyle(localStorage.getItem(STORAGE_KEY)); }
  catch { return DEFAULT_STYLE; }   // 读不了本地存储的会话仍然可用
}

export function applyStyle(id) {
  const style = normalizeStyle(id);
  document.documentElement.dataset.style = style;
  try { localStorage.setItem(STORAGE_KEY, style); }
  catch { /* 隐私模式：当前会话仍然生效 */ }
  return style;
}
