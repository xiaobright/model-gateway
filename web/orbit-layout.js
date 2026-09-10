/* 几何与拖放意图不依赖 DOM，页面滚动和操作方式共用一套坐标规则。 */
export function layoutModels(models, width) {
  const padding = width < 600 ? 16 : 30;
  const columns = width >= 1240 ? 2 : 1;
  const gap = 28;
  const cellWidth = (width - padding * 2 - gap * (columns - 1)) / columns;
  const compact = cellWidth < 560;
  const items = [];
  if (!models.length) return { items, height: 480, poolY: 210, columns };
  let y = 10;
  for (let at = 0; at < models.length; at += columns) {
    const row = models.slice(at, at + columns);
    const heights = row.map((m) => compact
      ? 280 + Math.ceil(m.candidates.length / 2) * 112
      : Math.max(433, Math.ceil(m.candidates.length / 2) * 116 + 85));
    const height = Math.max(...heights);
    row.forEach((model, col) => {
      const x = padding + col * (cellWidth + gap);
      const core = { x: x + cellWidth / 2, y: y + (compact ? 102 : height * .46), rx: Math.min(91, cellWidth * .27), ry: 84 };
      const candidates = model.candidates.map((candidate, index) => {
        if (compact) {
          const rx = Math.min(70, (cellWidth - 32) / 4);
          return { candidate, x: x + cellWidth * (index % 2 ? .75 : .25), y: y + 284 + Math.floor(index / 2) * 112, rx, ry: 43 };
        }
        const side = index % 2;
        const count = Math.ceil((model.candidates.length - side) / 2);
        const slot = Math.floor(index / 2);
        return { candidate, x: x + cellWidth * (side ? .81 : .19), y: y + (count === 1 ? height * .46 : 78 + slot * (height - 164) / (count - 1)), rx: 70, ry: 43 };
      });
      items.push({ model, x, y, width: cellWidth, height, core, candidates });
    });
    y += height + 25;
  }
  return { items, height: y + 180, poolY: y + 20, columns };
}

export function worldPoint(clientX, clientY, rect, scrollTop) {
  return { x: clientX - rect.left, y: clientY - rect.top + scrollTop };
}

export function hitModel(items, point, excludedModel = '') {
  return items.find((item) => item.model.model_name !== excludedModel
    && Math.pow((point.x - item.core.x) / (item.core.rx + 34), 2)
      + Math.pow((point.y - item.core.y) / (item.core.ry + 30), 2) <= 1) || null;
}

export function transferCompatibility(source, target, upstreams = []) {
  if (!target) return 'empty';
  if (source.kind === 'site') {
    const site = upstreams.find((u) => u.id === source.uid);
    return site?.groups?.some((g) => g.protocol === target.protocol) ? 'ok' : 'protocol';
  }
  if (source.model === target.model_name) return 'same-model';
  return source.protocol === target.protocol ? 'ok' : 'protocol';
}
