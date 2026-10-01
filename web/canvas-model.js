/* 编排页的数据派生：不写配置，不把配置顺序与单次请求轨迹混在一起。 */
export const modelKey = (model, protocol) => JSON.stringify([model, protocol]);
export const orderedCandidates = (row) => [...row.candidates]
  .sort((a, b) => a.priority - b.priority || a.route_id - b.route_id);
export const usable = (c) => Boolean(c.upstream_enabled && c.group_enabled);

// 对齐 db._CHAIN_QUERY + failover.order_chain：首选在前、其余按 priority，
// 启用降级时冷却候选后移。请求内还可能因失败类别跳过同分组，不能称为实时轨迹。
export function attemptOrder(row, failover) {
  if (row.forward_to) return [];
  const chain = orderedCandidates(row).filter(usable);
  const preferred = chain.findIndex((c) => c.route_id === row.preferred_route_id);
  if (preferred > 0) chain.unshift(...chain.splice(preferred, 1));
  return failover
    ? [...chain.filter((c) => !c.cooling_ms), ...chain.filter((c) => c.cooling_ms)]
    : chain.slice(0, 1);
}

// 返回直接目标到最终目标；16 跳与后端一致，停用不等于配置断链。
export function forwardPath(row, rows) {
  const seen = new Set([modelKey(row.model_name, row.protocol)]);
  const path = [];
  let current = row;
  while (current.forward_to) {
    const key = modelKey(current.forward_to, row.protocol);
    if (seen.has(key) || path.length >= 16) return null;
    const next = rows.find((r) => modelKey(r.model_name, r.protocol) === key);
    if (!next) return null;
    seen.add(key);
    path.push(next);
    current = next;
  }
  return current.candidates.length ? path : null;
}

export function forwardTargets(source, rows) {
  return rows.filter((r) => r.protocol === source.protocol && !r.draft
    && modelKey(r.model_name, r.protocol) !== modelKey(source.model_name, source.protocol)
    && forwardPath({ ...source, forward_to: r.model_name }, rows) !== null);
}

export function moveCandidate(row, rid, delta) {
  const ids = orderedCandidates(row).map((c) => c.route_id);
  const from = ids.indexOf(Number(rid)), to = from + Number(delta);
  if (from < 0 || to < 0 || to >= ids.length || Math.abs(Number(delta)) !== 1) return null;
  ids.splice(to, 0, ids.splice(from, 1)[0]);
  return ids;
}
