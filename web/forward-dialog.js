'use strict';
/* 模型转发弹窗：把一条模型的整条链交给同接口下的另一个模型。
   路由列表的详情面板用它设/改/取消；写入仍走 /admin/api/models/forward。 */

import { $, state, api, toast, esc, PROTO_LABEL } from './util.js';
import { modelKey, forwardTargets } from './route-model.js';
import { openDialog, closeDialog } from './motion.js';

let fwdEditing = null;

const hooks = { refreshConfig: async () => {} };

export function initForwardDialog(injected) {
  hooks.refreshConfig = injected.refreshConfig || hooks.refreshConfig;
}

const routeOf = (model, proto) =>
  state.routes.find((r) => modelKey(r.model_name, r.protocol) === modelKey(model, proto)) || null;

export async function setForward(model, proto, target) {
  await api('POST', '/admin/api/models/forward', {
    model_name: model, protocol: proto, target_model: target,
  });
  await hooks.refreshConfig();
  toast(`${model} 的整条链交给 ${target}`, 'ok');
}

export async function clearForward(model, proto) {
  await api('DELETE', '/admin/api/models/forward'
    + `?model_name=${encodeURIComponent(model)}&protocol=${encodeURIComponent(proto)}`);
  await hooks.refreshConfig();
  toast(`${model} 用回自己的候选链`, 'ok');
}

export function openForwardDialog(model, proto) {
  const row = routeOf(model, proto);
  if (!row) return;
  fwdEditing = { model, proto };
  const pool = forwardTargets(row, state.routes);
  if (!pool.length) {
    return toast('这个接口下没有可用的转发目标（目标须有完整候选链且不能成环）', 'err');
  }
  $('fwd-title').textContent = `「${model}」整条链交给…`;
  $('fwd-hint').innerHTML = `接口：<b>${esc(PROTO_LABEL[proto] || proto)}</b>。`
    + '交接后请求直接按目标的链走，包括它的候选顺序和自动降级。';
  $('fwd-target').innerHTML = pool.map((r) =>
    `<option value="${esc(r.model_name)}">${esc(r.model_name)}（${r.candidates.length} 个候选）</option>`).join('');
  if (row.forward_to) $('fwd-target').value = row.forward_to;
  $('fwd-clear').hidden = !row.forward_to;
  $('fwd-save').textContent = row.forward_to ? '改指' : '交接';
  openDialog($('fwd-dialog'));
}

export async function saveForward() {
  if (!fwdEditing) return;
  const target = $('fwd-target').value;
  if (!target) return toast('先选一个目标模型', 'err');
  const pending = fwdEditing;
  await setForward(pending.model, pending.proto, target);
  if (fwdEditing === pending) await closeDialog($('fwd-dialog'));
}

export async function clearForwardFromDialog() {
  if (!fwdEditing) return;
  const pending = fwdEditing;
  await clearForward(pending.model, pending.proto);
  if (fwdEditing === pending) await closeDialog($('fwd-dialog'));
}

/* 路由详情面板和弹窗里的按钮都走这两个名字；弹窗内那条不带 model，表示编辑中的那条。 */
export const forwardActions = {
  'fwd-open': (d) => openForwardDialog(d.model, d.proto),
  'fwd-clear': (d) => (d.model ? clearForward(d.model, d.proto) : clearForwardFromDialog()),
};
