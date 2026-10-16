// AI生成
import assert from 'node:assert/strict';
import test from 'node:test';
import { readFileSync } from 'node:fs';
import { STYLES, DEFAULT_STYLE, isValidStyle, normalizeStyle, styleHint, loadStyle } from '../web/theme.js';

test('风格登记表：id 唯一、能直接用作属性选择器、默认风格在表内', () => {
  const ids = STYLES.map((s) => s.id);
  assert.equal(new Set(ids).size, ids.length, 'id 重复会让 data-style 选择器互相串');
  for (const s of STYLES) {
    // id 会拼进 :root[data-style="..."]，只放小写字母数字和连字符
    assert.match(s.id, /^[a-z][a-z0-9-]*$/);
    assert.ok(s.name, '分段按钮上要显示名字');
    assert.ok(s.hint, '外观卡片要显示一句话说明');
  }
  assert.ok(ids.includes(DEFAULT_STYLE));
});

test('normalizeStyle：登记内的值原样返回，旧值/坏值/空值一律回落默认', () => {
  for (const s of STYLES) assert.equal(normalizeStyle(s.id), s.id);
  for (const bad of ['', 'glass ', 'Glass', 'GRAPHITE', 'liquid-glass', 'graphite', null, undefined, 0, 7, {}, []]) {
    assert.equal(normalizeStyle(bad), DEFAULT_STYLE, `存档里的 ${JSON.stringify(bad)} 不能选出一个不存在的风格`);
  }
});

test('isValidStyle 与 styleHint 只认登记表', () => {
  assert.equal(isValidStyle(DEFAULT_STYLE), true);
  assert.equal(isValidStyle('不存在的风格'), false);
  for (const s of STYLES) assert.equal(styleHint(s.id), s.hint);
  assert.equal(styleHint('nope'), '');
});

test('loadStyle：读不到本地存储时回落默认，不向外抛', () => {
  // node 里没有 localStorage，走的正是读失败那条路
  assert.equal(loadStyle(), DEFAULT_STYLE);
});

test('app.js 的外观卡片接线：分段控件、动作入口和启动顺序都在', () => {
  // 仓库按 LF 存、Windows 工作区按 CRLF 检出的，先归一化再比对
  const read = (p) => readFileSync(new URL(p, import.meta.url), 'utf8').replace(/\r\n/g, '\n');
  const html = read('../web/index.html');
  const js = read('../web/app.js');
  for (const id of ['seg-theme', 'seg-style', 'style-hint']) {
    assert.ok(html.includes(`id="${id}"`), `设置页缺少 #${id}`);
  }
  for (const act of ["'set-theme'", "'set-style'"]) {
    assert.ok(js.includes(act), `ACTIONS 缺少 ${act}`);
  }
  // 启动时先落风格再渲染控件，否则第一屏的分段没有选中项
  assert.ok(js.includes('applyStyle(loadStyle());\nrenderStyleControls();'), '启动顺序应为 applyStyle → renderStyleControls');
  // 明暗和风格切换必须共用同一个圆形揭开，且保留与视图过渡的互斥判断
  assert.ok(js.includes('function withCircleReveal(mutate, el)'));
  assert.ok(js.includes('withCircleReveal(() => applyTheme(name), el)'));
  assert.ok(js.includes('withCircleReveal(() => applyStyle(next), el)'));
  assert.ok(js.includes('document.documentElement.dataset.vtDir'), '圆形揭开仍要避开视图过渡');
});
