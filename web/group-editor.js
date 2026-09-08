/* 分组编辑相关的异步边界。
   这里不导入 app.js，只保存会话序号和按分组隔离的模型列表请求；DOM 渲染、提示和刷新
   由入口传入回调。这样旧弹窗/旧分组的响应即使晚到，也没有机会改当前弹窗。 */

import { api, state } from './util.js';

let sessionSeq = 0;
let activeSession = null;
const remoteCache = new Map();
const remoteDead = new Set();
const remoteBusy = new Set();
const remoteEpoch = new Map();
const remoteRequests = new Map();
const modelWrites = new Set();

export function beginGroupEdit(upstreamId, groupId) {
  const token = {
    seq: ++sessionSeq,
    upstreamId: Number(upstreamId),
    groupId: groupId === null ? null : Number(groupId),
  };
  activeSession = token;
  state.editingUp = token.upstreamId;
  state.editingGroup = token.groupId;
  return token;
}

export function currentGroupEdit() {
  return activeSession;
}

export function isCurrentGroupEdit(token) {
  return Boolean(
    token && activeSession && activeSession.seq === token.seq
      && state.editingUp === token.upstreamId && state.editingGroup === token.groupId
  );
}

export function updateGroupEdit(token, upstreamId, groupId) {
  if (!isCurrentGroupEdit(token)) return false;
  token.upstreamId = Number(upstreamId);
  token.groupId = groupId === null ? null : Number(groupId);
  state.editingUp = token.upstreamId;
  state.editingGroup = token.groupId;
  return true;
}

export function closeGroupEdit(seq) {
  if (!activeSession || Number(seq) !== activeSession.seq) return false;
  activeSession = null;
  sessionSeq += 1;
  return true;
}

export function invalidateGroupEdit() {
  activeSession = null;
  sessionSeq += 1;
}

export function remoteModels(gid) {
  return remoteCache.get(Number(gid));
}

export { remoteBusy, remoteDead };

export function invalidateRemoteModels(gid) {
  const id = Number(gid);
  remoteCache.delete(id);
  remoteDead.delete(id);
  remoteEpoch.set(id, (remoteEpoch.get(id) || 0) + 1);
}

export function pullRemoteModels(gid, requestKey, isCurrent, { onSuccess, onFailure, onFinally } = {}) {
  const id = Number(gid);
  if (!id || !isCurrent()) return Promise.resolve({ stale: true });
  const existing = remoteRequests.get(requestKey);
  if (existing) return existing.promise;

  const epoch = remoteEpoch.get(id) || 0;
  remoteBusy.add(id);
  const promise = (async () => {
    try {
      const data = await api('GET', `/admin/api/groups/${id}/remote-models`);
      const current = isCurrent() && (remoteEpoch.get(id) || 0) === epoch;
      if (!current) return { stale: true };
      remoteCache.set(id, data.models);
      remoteDead.delete(id);
      onSuccess?.(data.models);
      return { stale: false, models: data.models };
    } catch (error) {
      if (isCurrent() && (remoteEpoch.get(id) || 0) === epoch) {
        remoteDead.add(id);
        onFailure?.(error);
      }
      return { stale: !isCurrent(), error };
    } finally {
      remoteRequests.delete(requestKey);
      if (![...remoteRequests.values()].some((item) => item.gid === id)) {
        remoteBusy.delete(id);
      }
      if (isCurrent()) onFinally?.();
    }
  })();
  remoteRequests.set(requestKey, { gid: id, promise });
  return promise;
}

function writeKey(token, name) {
  return `${token?.seq || 0}:${token?.groupId || 0}:${name}`;
}

export function beginModelWrites(token, names) {
  if (!isCurrentGroupEdit(token) || token.groupId === null) return false;
  const keys = names.map((name) => writeKey(token, name));
  if (keys.some((key) => modelWrites.has(key))) return false;
  keys.forEach((key) => modelWrites.add(key));
  return true;
}

export function endModelWrites(token, names) {
  names.forEach((name) => modelWrites.delete(writeKey(token, name)));
}
