'use strict';
/* 连续液态工作区：配置状态来自管理接口；拖拽状态独立于轮询。
   所有写入由同一个入口串行处理，成功后走现有刷新队列。 */
import {
  $, state, api, toast, esc, clamp, splitOneM, withOneM,
  PROTO_INFO, PROTO_LABEL, PROTOCOLS, remotesOfGroup,
} from './util.js';
import { reduceMotion } from './motion.js';
import { pullRemoteModels, remoteModels } from './group-editor.js';
import { createLiquidSurface } from './liquid-surface.js';
import { layoutModels, worldPoint, hitModel, transferCompatibility } from './orbit-layout.js';

let hooks = {};
let host, world, stage, inspector, workbench, surface, newPool, sourceBar, threads;
let renderer = null, rendererTried = false;
let layout = { items: [], height: 500, poolY: 320 };
const models = new Map();
const sources = new Map();
const cooling = new Map();
let liveCalls = [];
let activityReceived = 0;
let breakerSignature = '';
let selection = null, panel = null, panelSeq = 0;
let pending = null, drag = null, dragLabel = null;
let mode = 'copy';
let motion = true;
let query = '';
let busy = false, alive = false, frameId = 0, lastTime = 0, lastPaint = 0, lastStatus = 0;
let lastDragAt = -1000;
let visualTime = 0;
let pointer = { x: .5, y: .5 };
let hover = null;
let viewportWidth = 0, viewportHeight = 0;
let effects = [];

const findModel = (name) => state.routes.find((m) => m.model_name === name);
const findCandidate = (name, id) => findModel(name)?.candidates.find((c) => c.route_id === Number(id));
const available = (c) => Boolean(c.upstream_enabled && c.group_enabled);
const liveFor = (name) => liveCalls.filter((call) => call.model === name);
const coolLeft = (c) => Math.max(0, (cooling.get(c.group_id)?.until || 0) - performance.now());
const button = (text, attrs = '', primary = false) => '<button type="button" class="orbit-tool' + (primary ? ' orbit-tool-primary' : '') + '" ' + attrs + '>' + text + '</button>';
const closeButton = '<button type="button" class="oi-close" data-oi="close" aria-label="关闭详情">×</button>';
const hint = (text) => { if ($('orbit-hint').textContent !== text) $('orbit-hint').textContent = text; };
const defaultHint = () => hint(mode === 'copy'
  ? '拖动复制候选或整组路由；放到空白处可汇成新模型。按住 Alt 临时移动。'
  : '移动模式：融合后从原模型取走；移走最后一条候选后，原模型会消失。');

function makeNode(className) {
  const el = document.createElement('button');
  el.type = 'button';
  el.className = className;
  stage.append(el);
  return el;
}
function setHtml(entity, html) {
  if (entity.html !== html) { entity.el.innerHTML = html; entity.html = html; }
}
function captureSource(el) {
  if (el.dataset.uid) return { kind: 'site', uid: Number(el.dataset.uid) };
  const model = findModel(el.dataset.model);
  if (!model) return null;
  const ids = el.dataset.rid ? [Number(el.dataset.rid)] : model.candidates.map((c) => c.route_id);
  if (!ids.length || ids.some((id) => !model.candidates.some((c) => c.route_id === id))) return null;
  return { kind: el.dataset.rid ? 'candidate' : 'model', model: model.model_name, protocol: model.protocol, routeIds: ids };
}
function sourceTitle(source) {
  if (source.kind === 'site') return state.upstreams.find((u) => u.id === source.uid)?.name || '上游';
  const candidate = source.kind === 'candidate' ? findCandidate(source.model, source.routeIds[0]) : null;
  return candidate ? candidate.upstream_name + ' · ' + candidate.group_name : source.model;
}
function currentMode(event) { return event?.altKey ? 'move' : mode; }

function syncSources() {
  const seen = new Set();
  for (const up of state.upstreams) {
    seen.add(up.id);
    let item = sources.get(up.id);
    if (!item) {
      const el = document.createElement('button');
      el.type = 'button';
      el.className = 'orbit-source';
      el.dataset.uid = String(up.id);
      sourceBar.append(el);
      item = { el, html: '' };
      sources.set(up.id, item);
    }
    item.el.classList.toggle('is-off', !up.enabled);
    item.el.setAttribute('aria-label', up.name + '，拖动添加候选，点选查看');
    setHtml(item, '<span>' + esc(up.name) + '</span><small>' + (up.groups || []).length + '</small>');
  }
  for (const [id,item] of sources) if (!seen.has(id)) { item.el.remove(); sources.delete(id); }
}

function syncModels() {
  if (pending && !sourceExists(pending.source)) cleanupDrag();
  const seen = new Set();
  for (const data of state.routes) {
    seen.add(data.model_name);
    let item = models.get(data.model_name);
    if (!item) {
      const el = makeNode('orbit-model');
      el.dataset.orbitNode = 'model';
      el.dataset.model = data.model_name;
      const label = document.createElement('span');
      label.className = 'orbit-model-label';
      stage.append(label);
      const threadGroup = document.createElementNS('http://www.w3.org/2000/svg','g');
      threads.append(threadGroup);
      item = { el, label, threadGroup, sats: new Map(), x: NaN, y: NaN, vx: 0, vy: 0, html: '' };
      models.set(data.model_name,item);
    }
    item.data = data;
    item.el.dataset.dead = String(data.active_route_id === null);
    item.el.setAttribute('aria-label', data.model_name + '，' + (PROTO_LABEL[data.protocol] || data.protocol) + '，' + data.candidates.length + ' 条候选');
    const active = data.candidates.find((c) => c.route_id === data.active_route_id);
    setHtml(item, '<span class="orbit-model-protocol">' + esc(data.protocol.toUpperCase()) + '</span>'
      + '<span class="orbit-model-name">' + esc(data.model_name) + '</span>'
      + '<span class="orbit-model-count">' + data.candidates.length + ' 条候选</span>'
      + '<span class="orbit-model-meta">' + (active ? '当前 · ' + esc(active.upstream_name) : '暂无可用上游') + '</span>'
      + '<span class="orbit-model-traffic" hidden></span>');
    const candidatesSeen = new Set();
    for (const candidate of data.candidates) {
      const id = candidate.route_id;
      candidatesSeen.add(id);
      let sat = item.sats.get(id);
      if (!sat) {
        const el = makeNode('orbit-candidate');
        el.dataset.orbitNode = 'candidate';
        el.dataset.model = data.model_name;
        el.dataset.rid = String(id);
        const tether = document.createElementNS('http://www.w3.org/2000/svg','path');
        tether.classList.add('orbit-tether');
        item.threadGroup.append(tether);
        sat = { el, tether, x: item.x, y: item.y, vx: 0, vy: 0, html: '' };
        item.sats.set(id,sat);
      }
      sat.data = candidate;
      const previous = cooling.get(candidate.group_id);
      if (!previous || (previous.fromConfig !== candidate.cooling_ms && performance.now() - activityReceived > 2500)) {
        cooling.set(candidate.group_id, { until: performance.now() + (candidate.cooling_ms || 0), fromConfig: candidate.cooling_ms });
      }
      const remote = splitOneM(candidate.remote_model);
      setHtml(sat, '<span class="oc-where">' + esc(candidate.upstream_name) + '</span>'
        + '<span class="oc-group">' + esc(candidate.group_name || '默认') + (remote.onem ? ' · 1M' : '') + '</span>'
        + '<span class="oc-remote">' + esc(remote.bare) + '</span><span class="oc-state"></span>');
      sat.el.title = candidate.upstream_name + ' / ' + candidate.group_name + '\n' + candidate.remote_model;
      sat.el.setAttribute('aria-label', candidate.upstream_name + ' · ' + candidate.group_name + ' · ' + candidate.remote_model + '，属于 ' + data.model_name);
    }
    for (const [id,sat] of item.sats) if (!candidatesSeen.has(id)) { sat.el.remove(); sat.tether.remove(); item.sats.delete(id); }
  }
  for (const [name,item] of models) if (!seen.has(name)) {
    item.el.remove(); item.label.remove(); item.threadGroup.remove(); item.sats.forEach((sat) => sat.el.remove()); models.delete(name);
  }
}
function sourceExists(source) {
  if (source.kind === 'site') return state.upstreams.some((u) => u.id === source.uid);
  const model = findModel(source.model);
  return Boolean(model && source.routeIds.every((id) => model.candidates.some((c) => c.route_id === id)));
}
function matches(data) {
  return !query || [data.model_name, data.protocol, ...data.candidates.flatMap((c) => [c.upstream_name,c.group_name,c.remote_model])]
    .some((value) => String(value).toLowerCase().includes(query));
}
function measure() {
  if (!host || !host.clientWidth || state.view !== 'orbit') return;
  const width = host.clientWidth;
  const height = host.clientHeight;
  const resized = width !== viewportWidth;
  const shown = state.routes.filter(matches);
  layout = layoutModels(shown,width);
  world.style.height = Math.max(height,layout.height) + 'px';
  newPool.style.top = layout.poolY + 'px';
  for (const item of models.values()) {
    const show = matches(item.data);
    item.el.hidden = !show;
    item.label.hidden = !show;
    item.threadGroup.style.display = show ? '' : 'none';
    item.sats.forEach((sat) => { sat.el.hidden = !show; });
  }
  layout.items.forEach((geometry,index) => {
    const item = models.get(geometry.model.model_name);
    item.geometry = geometry;
    item.tx = geometry.core.x; item.ty = geometry.core.y;
    item.rx = geometry.core.rx; item.ry = geometry.core.ry;
    if (!Number.isFinite(item.x) || reduceMotion() || resized) { item.x = item.tx; item.y = item.ty; item.vx = 0; item.vy = 0; }
    item.el.style.width = item.rx * 2 + 'px';
    item.el.style.height = item.ry * 2 + 'px';
    item.label.textContent = String(index + 1).padStart(2,'0') + ' / ' + geometry.model.protocol.toUpperCase();
    item.label.style.left = geometry.x + 8 + 'px';
    item.label.style.top = geometry.y + 9 + 'px';
    geometry.candidates.forEach((geo) => {
      const sat = item.sats.get(geo.candidate.route_id);
      sat.tx = geo.x; sat.ty = geo.y; sat.rx = geo.rx; sat.ry = geo.ry;
      if (!Number.isFinite(sat.x) || reduceMotion() || resized) { sat.x = sat.tx; sat.y = sat.ty; sat.vx = 0; sat.vy = 0; }
      sat.el.style.width = geo.rx * 2 + 'px';
      sat.el.style.height = geo.ry * 2 + 'px';
    });
  });
  if (width !== viewportWidth || height !== viewportHeight) {
    viewportWidth = width; viewportHeight = height;
    surface.style.height = height + 'px';
    renderer?.resize(width,height);
  }
  const empty = $('orbit-empty');
  empty.hidden = shown.length > 0;
  empty.textContent = state.routes.length ? '没有找到匹配的模型或上游' : '从上游源拖入一枚气泡，开始建立模型路由。';
  paintPositions();
  schedule();
}
function paintPositions() {
  for (const geometry of layout.items) {
    const item = models.get(geometry.model.model_name);
    item.el.style.left = item.x + 'px'; item.el.style.top = item.y + 'px';
    for (const sat of item.sats.values()) {
      sat.el.style.left = sat.x + 'px'; sat.el.style.top = sat.y + 'px';
      const dx = item.x-sat.x;
      sat.tether.setAttribute('d',`M ${sat.x} ${sat.y} C ${sat.x+dx*.48} ${sat.y}, ${item.x-dx*.48} ${item.y}, ${item.x} ${item.y}`);
    }
  }
}
function updateStates() {
  for (const [name,item] of models) {
    item.el.classList.toggle('is-selected', selection?.model === name && !selection?.rid);
    const traffic = item.el.querySelector('.orbit-model-traffic');
    const count = liveFor(name).length;
    traffic.hidden = !count;
    traffic.textContent = count ? count + ' 条请求流转中' : '';
    item.el.dataset.live = String(count > 0);
    item.data.candidates.forEach((candidate,index) => {
      const sat = item.sats.get(candidate.route_id);
      if (!sat) return;
      const off = !available(candidate), cool = coolLeft(candidate);
      const active = candidate.route_id === item.data.active_route_id;
      const preferred = candidate.route_id === item.data.preferred_route_id;
      sat.el.dataset.off = String(off); sat.el.dataset.cooling = String(cool > 0); sat.el.dataset.current = String(active);
      sat.tether.classList.toggle('is-current',active);
      sat.tether.classList.toggle('is-off',off || cool>0);
      sat.el.classList.toggle('is-selected', selection?.rid === candidate.route_id);
      const left = Math.ceil(cool/1000);
      const text = String(index+1).padStart(2,'0') + ' · ' + (off ? '停用' : cool > 0 ? '冷却 ' + left + 's'
        : active ? (preferred ? '首选 · 当前' : '当前接替') : preferred ? '首选' : '备用');
      const node = sat.el.querySelector('.oc-state');
      if (node.textContent !== text) node.textContent = text;
    });
  }
}

function schedule() {
  if (!frameId && alive && document.visibilityState === 'visible') frameId = requestAnimationFrame(frame);
}
function easeEntity(entity, dt) {
  if (reduceMotion()) { entity.x = entity.tx; entity.y = entity.ty; return false; }
  const delta = Math.abs(entity.tx-entity.x) + Math.abs(entity.ty-entity.y);
  if (delta < .08) { entity.x = entity.tx; entity.y = entity.ty; entity.vx = 0; entity.vy = 0; return false; }
  entity.vx = (entity.vx + (entity.tx-entity.x)*190*dt)*Math.exp(-17*dt);
  entity.vy = (entity.vy + (entity.ty-entity.y)*190*dt)*Math.exp(-17*dt);
  entity.x += entity.vx*dt; entity.y += entity.vy*dt;
  return true;
}
function frame(now) {
  frameId = 0;
  if (!alive || state.view !== 'orbit' || document.visibilityState !== 'visible') return;
  const dt = Math.min(.04,(now-(lastTime || now-16))/1000);
  lastTime = now;
  const animate = motion && !reduceMotion();
  if (animate) visualTime += dt;
  let moving = false;
  for (const geometry of layout.items) {
    const item = models.get(geometry.model.model_name);
    const nearCore = !drag && hover?.model === item.data.model_name && !hover?.rid && animate;
    item.tx = geometry.core.x + (nearCore ? clamp(hover.x-geometry.core.x,-60,60)*.08 : 0);
    item.ty = geometry.core.y + (nearCore ? clamp(hover.y-geometry.core.y,-60,60)*.08 : 0);
    moving = easeEntity(item,dt) || moving;
    geometry.candidates.forEach((geo) => {
      const sat = item.sats.get(geo.candidate.route_id);
      const near = !drag && hover?.rid === sat.data.route_id && animate;
      sat.tx = geo.x + (near ? clamp(hover.x-geo.x,-60,60)*.10 : 0);
      sat.ty = geo.y + (near ? clamp(hover.y-geo.y,-30,30)*.16 : 0);
      moving = easeEntity(sat,dt) || moving;
    });
  }
  if (drag) {
    const box = host.getBoundingClientRect();
    if (drag.clientX >= box.left && drag.clientX <= box.right) {
      const top = box.top + 52, bottom = box.bottom - 52;
      const speed = drag.clientY < top ? -clamp((top-drag.clientY)/52,0,1)*680 : drag.clientY > bottom ? clamp((drag.clientY-bottom)/52,0,1)*680 : 0;
      if (speed) host.scrollTop += speed*dt;
    }
    const point = localPoint(drag.clientX,drag.clientY);
    const k = animate ? 1-Math.exp(-22*dt) : 1;
    drag.x += (point.x-drag.x)*k; drag.y += (point.y-drag.y)*k;
    drag.tx = point.x; drag.ty = point.y;
    aimDrag();
  }
  effects = effects.filter((e) => now-e.started < e.duration);
  if (now-lastStatus > 700) { lastStatus = now; updateStates(); }
  if (moving) paintPositions();
  if (renderer && (now-lastPaint > (drag || moving || effects.length ? 10 : 30) || !animate)) {
    lastPaint = now;
    const extra = drag ? dragShapes(drag) : [];
    const scenes = layout.items.map((geometry) => {
      const item = models.get(geometry.model.model_name);
      const core = { x: item.x, y: item.y, rx: item.rx, ry: item.ry };
      if (drag?.target?.model.model_name === item.data.model_name && drag.compatible === 'ok') {
        const distance = Math.hypot(drag.x-core.x,drag.y-core.y);
        const attraction = Math.max(0,1-distance/(core.rx+90));
        core.x += (drag.x-core.x)*attraction*.08; core.y += (drag.y-core.y)*attraction*.08;
        core.rx += attraction*8; core.ry += attraction*5;
      }
      const drops = [...item.sats.values()].map((sat) => ({ x:sat.x,y:sat.y,rx:sat.rx,ry:sat.ry,flag:!available(sat.data)?2:coolLeft(sat.data)>0?1:0 }));
      const activeSat = item.sats.get(item.data.active_route_id);
      const particles = [];
      let pulse = 0;
      for (const effect of effects.filter((e) => e.target === item.data.model_name)) {
        const t = clamp((now-effect.started)/effect.duration,0,1);
        const k = 1-Math.pow(1-t,3);
        const x = effect.from.x+(core.x-effect.from.x)*k;
        const y = effect.from.y+(core.y-effect.from.y)*k-Math.sin(t*Math.PI)*30;
        particles.push({x,y,rx:15*(1-t*.45),ry:17*(1-t*.45)});
        pulse = Math.sin(t*Math.PI);
      }
      if (animate) {
        liveFor(item.data.model_name).slice(0,3).forEach((call,index) => {
          const sat = [...item.sats.values()].find((s) => s.data.group_id === call.group_id
            && splitOneM(s.data.remote_model).bare === splitOneM(call.remote_model).bare);
          if (!sat) return;
          const t = (visualTime*.44+index*.31)%1;
          particles.push({x:sat.x+(core.x-sat.x)*t,y:sat.y+(core.y-sat.y)*t-Math.sin(t*Math.PI)*18,rx:4,ry:4});
        });
      }
      return {...geometry,core,drops,active:activeSat?{x:activeSat.x,y:activeSat.y}:null,particles,pulse,protocol:item.data.protocol};
    });
    renderer.draw(scenes,{scrollTop:host.scrollTop,time:visualTime,motion:animate?.65:0,pointer,extra});
  }
  if (animate || moving || drag || effects.length) schedule();
}
function dragShapes(d) {
  const r = d.source.kind === 'model' ? 44 : 29;
  const dx = clamp(d.tx-d.x,-90,90), dy = clamp(d.ty-d.y,-90,90);
  return [{x:d.x,y:d.y,rx:r+Math.abs(dx)*.12,ry:r*.84+Math.abs(dy)*.12},
    {x:d.x-dx*.42,y:d.y-dy*.42,rx:r*.64,ry:r*.59},
    {x:d.x-dx*.84,y:d.y-dy*.84,rx:r*.30,ry:r*.30}];
}
function pulseInto(target,from) {
  effects.push({target,from,started:performance.now(),duration:reduceMotion()?1:950});
  schedule();
}

async function write(label, operation, onSuccess) {
  if (busy) return;
  busy = true;
  workbench.classList.add('is-saving');
  let saved = false;
  try {
    const result = await operation();
    saved = true;
    await hooks.refreshConfig();
    if (alive) { onSuccess?.(result); hint(label); toast(label,'ok'); }
    return result;
  } catch (error) {
    toast((saved ? '配置已保存，但刷新失败：' : '') + error.message,'err');
    if (alive) hint(saved ? '配置已保存，等待重新同步。' : error.message);
  } finally {
    busy = false;
    workbench.classList.remove('is-saving');
  }
}
async function transfer(source,targetName,operation,from) {
  if (!source || source.kind === 'site') return;
  const target = findModel(targetName);
  if (target && transferCompatibility(source,target,state.upstreams) !== 'ok') throw new Error('只支持在相同协议的模型之间融合');
  const token = panel?.seq;
  return write((operation === 'move' ? '已移动融合到 ' : '已复制融合到 ') + targetName,
    () => api('POST','/admin/api/models/transfer',{
      source_model_name:source.model,target_model_name:targetName,route_ids:source.routeIds,mode:operation,
    }),
    (result) => {
      if (panel?.seq === token) closePanel();
      if (result.source_empty && operation === 'move') hint('已移动全部候选，原模型 ' + source.model + ' 已移除。');
      const targetItem = models.get(targetName);
      if (targetItem) {
        if (targetItem.y < host.scrollTop || targetItem.y > host.scrollTop+host.clientHeight) host.scrollTo({top:Math.max(0,targetItem.y-host.clientHeight*.4),behavior:reduceMotion()?'instant':'smooth'});
        pulseInto(targetName,from || {x:targetItem.x-150,y:targetItem.y-50});
      }
    });
}
function reorder(name,id,destination) {
  const model = findModel(name);
  const ids = model?.candidates.map((c) => c.route_id);
  if (!ids) return;
  const at = ids.indexOf(id);
  if (at < 0 || destination < 0 || destination >= ids.length || at === destination) return;
  ids.splice(destination,0,ids.splice(at,1)[0]);
  return write('候选顺序已更新',() => api('POST','/admin/api/models/order',{model_name:name,order:ids}));
}
function switchCandidate(name,id) {
  const candidate = findCandidate(name,id);
  if (!candidate) return;
  if (!available(candidate)) { toast('请先在上游站点启用该站点和分组','err'); return; }
  const sat = models.get(name)?.sats.get(id);
  return write(name + ' → ' + candidate.upstream_name,
    () => api('POST','/admin/api/models/switch',{route_id:id}),
    () => { if(sat) pulseInto(name,{x:sat.x,y:sat.y}); });
}
function closePanel() {
  panel = null; panelSeq++;
  inspector.hidden = true;
  selection = null;
  updateStates();
}
function panelHead(kicker,title) {
  return '<div class="oi-top"><div><small>' + esc(kicker) + '</small><strong>' + esc(title) + '</strong></div>' + closeButton + '</div>';
}
function showDetails(name,rid = null) {
  const model = findModel(name);
  if (!model) return;
  selection = {model:name,rid};
  panel = {kind:'details',model:name,rid,seq:++panelSeq};
  renderDetails();
  inspector.hidden = false;
  updateStates();
}
function renderDetails() {
  if (panel?.kind !== 'details') return;
  const model = findModel(panel.model);
  if (!model) return closePanel();
  const candidate = panel.rid ? findCandidate(panel.model,panel.rid) : null;
  if (panel.rid && !candidate) return closePanel();
  let html = panelHead(PROTO_LABEL[model.protocol] || model.protocol,model.model_name);
  if (candidate) {
    html += '<div class="oi-info">' + esc(candidate.upstream_name) + ' / ' + esc(candidate.group_name)
      + '<br><code>' + esc(candidate.remote_model) + '</code></div>'
      + '<div class="oi-actions">' + button('设为首选','data-oi="switch"',true) + button('编辑映射','data-oi="edit"') + '</div>'
      + '<div class="oi-actions">' + button('复制到…','data-oi="transfer" data-mode="copy"') + button('移动到…','data-oi="transfer" data-mode="move"') + '</div>'
      + '<div class="oi-actions">' + button('拆成新模型','data-oi="split"') + button('移除候选','data-oi="remove"') + '</div>';
  } else {
    html += '<div class="oi-info">首选优先尝试，其余候选按下面的顺序降级。冷却状态跟随分组。</div><div class="oi-list">';
    model.candidates.forEach((c,index) => {
      html += '<div class="oi-row' + (c.route_id === model.active_route_id ? ' is-on' : '') + '"><span>'
        + String(index+1).padStart(2,'0') + '</span><button type="button" class="oi-row-pick" data-oi="candidate" data-rid="' + c.route_id + '">'
        + '<b>' + esc(c.upstream_name) + ' · ' + esc(c.group_name) + '</b><small>' + esc(c.remote_model) + '</small></button><div class="oi-reorder">'
        + '<button type="button" data-oi="order" data-rid="' + c.route_id + '" data-direction="-1" aria-label="前移 ' + esc(c.upstream_name) + '"' + (!index?' disabled':'') + '>↑</button>'
        + '<button type="button" data-oi="order" data-rid="' + c.route_id + '" data-direction="1" aria-label="后移 ' + esc(c.upstream_name) + '"' + (index===model.candidates.length-1?' disabled':'') + '>↓</button></div></div>';
    });
    html += '</div><div class="oi-actions">' + button('复制整组','data-oi="transfer" data-mode="copy"')
      + button('移动整组','data-oi="transfer" data-mode="move"') + '</div>';
  }
  if (inspector.innerHTML !== html) inspector.innerHTML = html;
}
function selectedSource() {
  const model = findModel(selection?.model);
  if (!model) return null;
  return {kind:selection.rid?'candidate':'model',model:model.model_name,protocol:model.protocol,
    routeIds:selection.rid?[selection.rid]:model.candidates.map((c) => c.route_id)};
}
function suggestedName(base) {
  let candidate = base + '-copy', index = 2;
  while (findModel(candidate)) candidate = base + '-copy-' + index++;
  return candidate;
}
function openTransfer(source,targetName = '',operation = mode,from = null,forceNew = false) {
  if (!source || source.kind === 'site') return;
  const options = state.routes.filter((m) => m.protocol === source.protocol && m.model_name !== source.model);
  panel = {kind:'transfer',source,seq:++panelSeq,from};
  selection = {model:source.model,rid:source.kind==='candidate'?source.routeIds[0]:null};
  inspector.innerHTML = panelHead(operation==='move'?'移动 / 拆分':'复制 / 融合',sourceTitle(source))
    + '<div class="oi-info">' + source.routeIds.length + ' 条候选 · ' + esc(PROTO_LABEL[source.protocol] || source.protocol) + '<br>保留分组和上游真实模型；相同映射自动合并。</div>'
    + '<label class="oi-field">目标模型<select id="oi-target"><option value="">＋ 新建一个模型</option>'
    + options.map((m) => '<option value="' + esc(m.model_name) + '">' + esc(m.model_name) + '</option>').join('') + '</select></label>'
    + '<label class="oi-field" id="oi-name-field">下游模型名<input id="oi-name" autocomplete="off" value="' + esc(suggestedName(source.model)) + '" placeholder="输入新的下游模型名"></label>'
    + '<label class="oi-field">操作方式<select id="oi-operation"><option value="copy">复制 · 保留原候选</option><option value="move">移动 · 从原模型取走</option></select></label>'
    + '<p class="oi-help" id="oi-transfer-help"></p>'
    + '<div class="oi-actions">' + button('取消','data-oi="close"') + button('确认融合','data-oi="confirm-transfer"',true) + '</div>';
  $('oi-target').value = forceNew ? '' : targetName;
  $('oi-operation').value = operation;
  updateTransferFields();
  inspector.hidden = false;
  (forceNew ? $('oi-name') : $('oi-target')).focus();
  updateStates();
}
function updateTransferFields() {
  if (panel?.kind !== 'transfer') return;
  $('oi-name-field').hidden = Boolean($('oi-target').value);
  const source = panel.source;
  const whole = findModel(source.model)?.candidates.length === source.routeIds.length;
  $('oi-transfer-help').textContent = $('oi-operation').value === 'move' && whole
    ? '移走这组候选后，原模型 ' + source.model + ' 将不再对下游暴露。'
    : '复制和移动只改变候选归属，不改变接口格式。';
}
function openMapping(source,targetName = '',editing = null) {
  const target = findModel(targetName);
  const candidate = editing ? findCandidate(targetName,editing) : null;
  const uid = candidate?.upstream_id || source.uid;
  const up = state.upstreams.find((u) => u.id === uid);
  if (!up) return;
  const protocols = [...new Set((up.groups || []).map((g) => g.protocol))].filter((p) => PROTOCOLS.includes(p));
  if (!protocols.length) { toast('请先给这个上游添加分组','err'); return; }
  panel = {kind:'mapping',seq:++panelSeq,uid,model:targetName,rid:editing,protocol:target?.protocol || protocols[0]};
  selection = targetName ? {model:targetName,rid:editing} : null;
  const remote = splitOneM(candidate?.remote_model || '');
  inspector.innerHTML = panelHead(candidate?'编辑候选映射':'接入上游',up.name)
    + '<label class="oi-field">接口格式<select id="oi-protocol"' + (target?' disabled':'') + '>'
    + protocols.map((p) => '<option value="' + esc(p) + '">' + esc(PROTO_LABEL[p] || p) + '</option>').join('') + '</select></label>'
    + '<label class="oi-field">下游模型名<input id="oi-map-model" value="' + esc(targetName) + '"' + (target?' readonly':'') + ' placeholder="对客户端暴露的模型名" autocomplete="off"></label>'
    + '<label class="oi-field">分组<select id="oi-group"' + (candidate?' disabled':'') + '></select></label>'
    + '<label class="oi-field">上游真实模型<input id="oi-remote" list="oi-remotes" value="' + esc(remote.bare) + '" placeholder="选择或输入真实模型名" autocomplete="off"><datalist id="oi-remotes"></datalist></label>'
    + '<label class="oi-check"><input id="oi-onem" type="checkbox"' + (remote.onem?' checked':'') + '>1M 上下文</label>'
    + '<div class="oi-actions">' + button('拉取模型列表','data-oi="pull-models"') + '</div><p class="oi-help" id="oi-remote-status">可使用已知名称，也可以手动输入上游模型名。</p>'
    + '<div class="oi-actions">' + button('取消','data-oi="close"') + button(candidate?'保存映射':'融合候选','data-oi="save-mapping"',true) + '</div>';
  $('oi-protocol').value = panel.protocol;
  updateMappingGroups(candidate?.group_id);
  inspector.hidden = false;
  (target ? $('oi-remote') : $('oi-map-model')).focus();
  updateStates();
}
function updateMappingGroups(gid = null) {
  if (panel?.kind !== 'mapping') return;
  panel.protocol = $('oi-protocol').value;
  const up = state.upstreams.find((u) => u.id === panel.uid);
  const groups = (up?.groups || []).filter((g) => g.protocol === panel.protocol);
  $('oi-group').innerHTML = groups.map((g) => '<option value="' + g.id + '">' + esc(g.name) + (!g.enabled?' · 停用':'') + '</option>').join('');
  if (gid && groups.some((g) => g.id === gid)) $('oi-group').value = String(gid);
  const show1m = Boolean(PROTO_INFO[panel.protocol]?.supports_1m || $('oi-onem').checked);
  $('oi-onem').closest('label').hidden = !show1m;
  updateRemoteOptions();
}
function updateRemoteOptions() {
  if (panel?.kind !== 'mapping') return;
  inspector.querySelector('[data-oi="pull-models"]').disabled = false;
  const gid = Number($('oi-group').value);
  const values = remoteModels(gid) || remotesOfGroup(gid);
  $('oi-remotes').innerHTML = [...new Set(values)].map((v) => '<option value="' + esc(v) + '"></option>').join('');
  if (!panel.rid) {
    const target = $('oi-map-model').value;
    $('oi-remote').value = values.includes(target) ? target : '';
  }
  $('oi-remote-status').classList.remove('is-error');
  $('oi-remote-status').textContent = values.length ? '已知 ' + values.length + ' 个模型，也可以手动输入。' : '请选择分组并输入真实模型名，或拉取该分组的模型列表。';
}
function pullMappingModels() {
  if (panel?.kind !== 'mapping') return;
  const token = panel, gid = Number($('oi-group').value);
  const current = () => alive && panel === token && Number($('oi-group')?.value) === gid;
  if (!gid) return;
  const trigger = inspector.querySelector('[data-oi="pull-models"]');
  trigger.disabled = true;
  $('oi-remote-status').textContent = '正在拉取该分组的模型列表…';
  return pullRemoteModels(gid,'orbit:' + token.seq + ':' + gid,current,{
    onSuccess(values) {
      const typed = $('oi-remote').value;
      updateRemoteOptions();
      if (typed) $('oi-remote').value = typed;
      $('oi-remote-status').textContent = '已加载 ' + values.length + ' 个上游模型。';
    },
    onFailure(error) {
      $('oi-remote-status').classList.add('is-error');
      $('oi-remote-status').textContent = '拉取失败，可手动输入：' + error.message;
    },
    onFinally() { trigger.disabled = false; },
  });
}
async function saveMapping() {
  const token = panel;
  if (token?.kind !== 'mapping') return;
  const modelName = $('oi-map-model').value.trim(), remote = $('oi-remote').value.trim(), gid = Number($('oi-group').value);
  if (!modelName || !remote || !gid) throw new Error('请填写下游模型、分组和上游真实模型名');
  const existing = findModel(modelName);
  if (existing && existing.protocol !== token.protocol) throw new Error('该下游模型已属于另一个接口格式');
  const parsed = splitOneM(remote);
  const remoteModel = withOneM(parsed.bare,$('oi-onem').checked || parsed.onem);
  return write(token.rid?'映射已保存':modelName + ' 已接入新的候选',
    () => token.rid ? api('PUT','/admin/api/models',{route_id:token.rid,remote_model:remoteModel})
      : api('POST','/admin/api/models',{model_name:modelName,group_id:gid,remote_model:remoteModel}),
    () => {
      if (panel === token) closePanel();
      const item = models.get(modelName);
      if (item) pulseInto(modelName,{x:item.x,y:item.y-120});
    });
}

function localPoint(x,y) { return worldPoint(x,y,host.getBoundingClientRect(),host.scrollTop); }
function hitRenderedModel(point, excluded = '') {
  return hitModel(layout.items.map((geometry) => {
    const item = models.get(geometry.model.model_name);
    return {...geometry,core:{...geometry.core,x:item.x,y:item.y}};
  }),point,excluded);
}
function onDown(event) {
  if (event.button !== 0 || busy || pending || drag) return;
  const el = event.target.closest('[data-orbit-node],.orbit-source');
  if (!el || inspector.contains(el)) return;
  const source = captureSource(el);
  if (!source) return;
  pending = {source,el,id:event.pointerId,startX:event.clientX,startY:event.clientY};
}
function onMove(event) {
  if (pending && event.pointerId !== pending.id) return;
  const point = localPoint(event.clientX,event.clientY);
  pointer = {x:point.x/host.clientWidth,y:(point.y-host.scrollTop)/host.clientHeight};
  const node = event.target.closest('[data-orbit-node]');
  hover = node ? {model:node.dataset.model,rid:Number(node.dataset.rid)||null,...point} : null;
  if (pending && !drag && Math.hypot(event.clientX-pending.startX,event.clientY-pending.startY) > 6) {
    const origin = localPoint(pending.startX,pending.startY);
    drag = {...pending,clientX:event.clientX,clientY:event.clientY,x:origin.x,y:origin.y,tx:point.x,ty:point.y,mode:currentMode(event),target:null,compatible:'empty'};
    try { pending.el.setPointerCapture(event.pointerId); } catch { /* 已经松开或元素从别处被移除 */ }
    pending.el.classList.add('is-lifting');
    document.body.classList.add('orbit-is-dragging');
    closePanel();
    dragLabel = document.createElement('div');
    dragLabel.className = 'orbit-drag-label';
    document.body.append(dragLabel);
  }
  if (drag) {
    drag.clientX = event.clientX; drag.clientY = event.clientY;
    drag.tx = point.x; drag.ty = point.y; drag.mode = currentMode(event);
    aimDrag();
  }
  schedule();
}
function aimDrag() {
  if (!drag) return;
  const point = localPoint(drag.clientX,drag.clientY);
  drag.target = hitRenderedModel(point,drag.source.model || '');
  drag.compatible = transferCompatibility(drag.source,drag.target?.model,state.upstreams);
  drag.reorder = null;
  if (drag.source.kind === 'candidate') {
    const owner = models.get(drag.source.model);
    for (const [id,sat] of owner?.sats || []) {
      if (id !== drag.source.routeIds[0] && Math.pow((point.x-sat.x)/(sat.rx+8),2)+Math.pow((point.y-sat.y)/(sat.ry+10),2) <= 1) drag.reorder = id;
    }
  }
  for (const item of models.values()) {
    const hit = drag.target?.model.model_name === item.data.model_name;
    item.el.classList.toggle('is-target',hit && drag.compatible === 'ok');
    item.el.classList.toggle('is-incompatible',hit && drag.compatible === 'protocol');
    item.sats.forEach((sat,id) => sat.el.classList.toggle('is-reorder-target',id === drag.reorder));
  }
  const overPool = point.y >= layout.poolY && point.y <= layout.poolY+104;
  newPool.classList.toggle('is-target',overPool);
  let description = drag.source.kind === 'site' ? '接入 ' : drag.mode === 'move' ? '移动 ' : '复制 ';
  description += sourceTitle(drag.source);
  if (drag.reorder) description = '调整同一模型内的候选顺序';
  else if (drag.target) description += drag.compatible === 'ok' ? ' → ' + drag.target.model.model_name : ' · 接口格式不兼容';
  else description += ' · 放到空白处建立新模型';
  if (dragLabel) {
    dragLabel.textContent = description;
    dragLabel.style.left = Math.max(4,Math.min(drag.clientX,innerWidth-310)) + 'px';
    dragLabel.style.top = Math.min(drag.clientY,innerHeight-58) + 'px';
  }
}
function cleanupDrag() {
  const previous = pending;
  pending = null; drag = null;
  if (previous?.el?.hasPointerCapture?.(previous.id)) previous.el.releasePointerCapture(previous.id);
  previous?.el?.classList.remove('is-lifting');
  dragLabel?.remove(); dragLabel = null;
  document.body.classList.remove('orbit-is-dragging');
  newPool.classList.remove('is-target');
  for (const item of models.values()) {
    item.el.classList.remove('is-target','is-incompatible');
    item.sats.forEach((sat) => sat.el.classList.remove('is-reorder-target'));
  }
  schedule();
}
async function onUp(event,cancelled = false) {
  if (!pending || pending.id !== event.pointerId) return;
  const hadDrag = Boolean(drag);
  if (drag) {
    drag.clientX = event.clientX; drag.clientY = event.clientY;
    drag.mode = currentMode(event);
    aimDrag();
  }
  const d = drag;
  const point = localPoint(event.clientX,event.clientY);
  cleanupDrag();
  if (!hadDrag) return;
  lastDragAt = performance.now();
  if (cancelled || !d) return;
  try {
    const box = host.getBoundingClientRect();
    if (event.clientX < box.left || event.clientX > box.right || event.clientY < box.top || event.clientY > box.bottom) { defaultHint(); return; }
    if (d.reorder) {
      const model = findModel(d.source.model);
      return reorder(model.model_name,d.source.routeIds[0],model.candidates.findIndex((c) => c.route_id === d.reorder));
    }
    if (d.target) {
      if (d.compatible !== 'ok') throw new Error('接口格式不同，无法融合；原配置保持不变');
      if (d.source.kind === 'site') return openMapping(d.source,d.target.model.model_name);
      return await transfer(d.source,d.target.model.model_name,d.mode,point);
    }
    if (d.source.model && hitRenderedModel(point)?.model.model_name === d.source.model) return;
    if (d.source.kind === 'site') openMapping(d.source);
    else openTransfer(d.source,'',d.mode,point,true);
  } catch (error) { toast(error.message,'err'); hint(error.message); }
}
async function onClick(event) {
  const el = event.target.closest('button');
  if (!el || el.disabled) return;
  if (performance.now()-lastDragAt < 180 && (el.dataset.orbitNode || el.classList.contains('orbit-source'))) return;
  try {
    if (el.dataset.orbit === 'mode') {
      mode = el.dataset.mode;
      document.querySelectorAll('[data-orbit="mode"]').forEach((b) => b.setAttribute('aria-pressed',String(b.dataset.mode===mode)));
      defaultHint(); return;
    }
    if (el.dataset.orbit === 'motion') {
      motion = !motion;
      localStorage.setItem('mg-orbit-motion',String(motion));
      syncMotionToggle();
      schedule(); return;
    }
    if (el.dataset.orbit === 'new-model') { document.querySelector('[data-act="new-route"]').click(); return; }
    if (el.classList.contains('orbit-source')) return hooks.editUpstream?.(Number(el.dataset.uid));
    if (el.dataset.orbitNode) return showDetails(el.dataset.model,el.dataset.rid?Number(el.dataset.rid):null);
    const action = el.dataset.oi;
    if (!action) return;
    if (action === 'close') return closePanel();
    if (busy) return;
    if (action === 'candidate') return showDetails(panel.model,Number(el.dataset.rid));
    if (action === 'switch') return await switchCandidate(panel.model,panel.rid);
    if (action === 'order') {
      const id = Number(el.dataset.rid), model = findModel(panel.model);
      return await reorder(model.model_name,id,model.candidates.findIndex((c) => c.route_id===id)+Number(el.dataset.direction));
    }
    if (action === 'edit') return openMapping({},panel.model,panel.rid);
    if (action === 'transfer' || action === 'split') return openTransfer(selectedSource(),'',action==='split'?'move':el.dataset.mode || mode,null,action==='split');
    if (action === 'confirm-transfer') {
      const target = $('oi-target').value || $('oi-name').value.trim();
      if (!target) throw new Error('请填写目标模型名');
      return await transfer(panel.source,target,$('oi-operation').value,panel.from);
    }
    if (action === 'pull-models') return await pullMappingModels();
    if (action === 'save-mapping') return await saveMapping();
    if (action === 'remove') {
      const token = panel, candidate = findCandidate(token.model,token.rid);
      if (!candidate) return;
      const last = findModel(token.model).candidates.length===1;
      const ok = await hooks.confirmRemove({
        title:last?'移除最后一条候选':'移除候选',
        body:last?esc(token.model)+' 将不再对下游暴露。':esc(candidate.upstream_name)+' / '+esc(candidate.group_name)+' / '+esc(candidate.remote_model),
        ok:'移除',
      });
      if (ok) return await write('候选已移除',() => api('DELETE','/admin/api/models?route_id='+token.rid),() => {if(panel===token)closePanel();});
    }
  } catch (error) { toast(error.message,'err'); }
}

export function initOrbit(opts) {
  hooks = opts;
  host = $('orbit-canvas'); world = $('orbit-world'); stage = $('orbit-stage'); inspector = $('orbit-inspector');
  workbench = host.closest('.orbit-workbench'); surface = $('orbit-surface'); newPool = $('orbit-new-pool'); sourceBar = $('orbit-sources');
  threads = $('orbit-threads');
  motion = localStorage.getItem('mg-orbit-motion') !== 'false';
  syncMotionToggle();
  const view = host.closest('.liquid-view');
  view.addEventListener('pointerdown',onDown);
  document.addEventListener('pointermove',(event) => { if(alive&&(pending||host.contains(event.target)))onMove(event); });
  document.addEventListener('pointerup',(event) => { if(pending)void onUp(event); });
  document.addEventListener('pointercancel',(event) => { if(pending)void onUp(event,true); });
  view.addEventListener('lostpointercapture',(event) => { if(pending?.id===event.pointerId)cleanupDrag(); });
  view.addEventListener('click',(event) => { void onClick(event); });
  stage.addEventListener('dblclick',(event) => {
    const el = event.target.closest('.orbit-candidate');
    if (el && !busy && performance.now()-lastDragAt>180) { void switchCandidate(el.dataset.model,Number(el.dataset.rid)); }
  });
  inspector.addEventListener('change',(event) => {
    if (event.target.id==='oi-target'||event.target.id==='oi-operation') updateTransferFields();
    if (event.target.id==='oi-protocol') updateMappingGroups();
    if (event.target.id==='oi-group') updateRemoteOptions();
  });
  $('orbit-search').addEventListener('input',(event) => {
    if(drag)cleanupDrag();
    query = event.target.value.trim().toLowerCase();
    host.scrollTop = 0;
    measure();
  });
  host.addEventListener('scroll',schedule,{passive:true});
  host.addEventListener('pointerleave',() => {hover=null;schedule();});
  document.addEventListener('keydown',(event) => {
    if (!alive) return;
    if(event.key==='Escape') { cleanupDrag(); closePanel(); defaultHint(); }
    if(event.key==='Alt' && drag) {drag.mode='move';aimDrag();}
  });
  document.addEventListener('keyup',(event) => {if(event.key==='Alt' && drag){drag.mode=mode;aimDrag();}});
  new ResizeObserver(() => {if(alive)measure();}).observe(host);
  new MutationObserver(() => {
    if(alive){renderer?.theme();schedule();}
  }).observe(document.documentElement,{attributes:true,attributeFilter:['data-theme']});
  matchMedia('(prefers-color-scheme: dark)').addEventListener('change',() => {if(alive){renderer?.theme();schedule();}});
  matchMedia('(prefers-reduced-motion: reduce)').addEventListener('change',() => {syncMotionToggle();schedule();});
  document.addEventListener('visibilitychange',() => {
    if(document.visibilityState==='hidden'){
      if(frameId)cancelAnimationFrame(frameId);
      frameId=0;
      if(pending)cleanupDrag();
    }
    else if(alive){lastTime=0;schedule();}
  });
  setInterval(() => {if(alive&&document.visibilityState==='visible'){updateStates();if(!motion||reduceMotion())schedule();}},1000);
}
function syncMotionToggle() {
  const toggle = document.querySelector('[data-orbit="motion"]');
  const enabled = motion && !reduceMotion();
  toggle.setAttribute('aria-pressed',String(enabled));
  toggle.setAttribute('aria-label',reduceMotion()?'已跟随系统减少动态效果':enabled?'暂停液体动效':'开启液体动效');
  toggle.querySelector('span').textContent = enabled?'动效开启':'动效暂停';
}
export function renderOrbit() {
  if (!host || state.view !== 'orbit' || !host.clientWidth) return;
  alive = true;
  if (!rendererTried) {
    rendererTried = true;
    renderer = createLiquidSurface(surface,() => {
      renderer = null;
      workbench.classList.remove('has-liquid-gl');
      schedule();
    });
    workbench.classList.toggle('has-liquid-gl',Boolean(renderer));
    if (!renderer) surface.style.visibility = 'hidden';
  }
  renderer?.theme();
  syncSources(); syncModels(); measure(); updateStates();
  $('orbit-count').textContent = state.routes.length + ' 个模型 · ' + state.routes.reduce((n,m) => n+m.candidates.length,0) + ' 条候选';
  if (panel?.kind==='details') renderDetails();
  schedule();
}
export function renderOrbitActivity(data) {
  if (state.view!=='orbit') return;
  liveCalls = data.calls || [];
  activityReceived = performance.now();
  for(const item of data.breakers || []) {
    cooling.set(item.group_id,{until:performance.now()+(item.cooling_ms||0),fromConfig:cooling.get(item.group_id)?.fromConfig});
  }
  const ids = new Set((data.breakers || []).map((b) => b.group_id));
  for(const [id,value] of cooling) if(!ids.has(id)) value.until = 0;
  const signature = JSON.stringify([(data.breakers || []).filter((b) => b.cooling_ms>0).map((b) => b.group_id).sort(),data.failover]);
  if(breakerSignature && signature!==breakerSignature) hooks.refreshConfig().catch(() => {});
  breakerSignature = signature;
  updateStates();
  schedule();
}
export function leaveOrbit() {
  alive = false;
  if(frameId)cancelAnimationFrame(frameId);
  frameId = 0; lastTime = 0;
  cleanupDrag();
  closePanel();
  effects = [];
}
