import assert from 'node:assert/strict';
import { layoutModels, worldPoint, hitModel, transferCompatibility } from '../web/orbit-layout.js';

const models = Array.from({ length: 8 }, (_, i) => ({
  model_name: 'model-' + i, protocol: i % 2 ? 'anthropic' : 'openai',
  candidates: Array.from({ length: 4 + i }, (_, j) => ({ route_id: i * 20 + j })),
}));
for (const width of [300, 390, 600, 1100, 1240, 1650]) {
  const layout = layoutModels(models, width);
  assert.equal(layout.items.length, models.length);
  for (const item of layout.items) {
    const all = [item.core, ...item.candidates];
    for (const shape of all) {
      assert.ok(shape.x - shape.rx >= 0, 'left edge at ' + width);
      assert.ok(shape.x + shape.rx <= width, 'right edge at ' + width);
      assert.ok(shape.y - shape.ry >= item.y, 'top edge at ' + width);
      assert.ok(shape.y + shape.ry + 21 <= item.y + item.height, 'bottom labels at ' + width);
    }
    for (let a = 0; a < item.candidates.length; a++) {
      for (let b = a + 1; b < item.candidates.length; b++) {
        const left = item.candidates[a], right = item.candidates[b];
        assert.ok(Math.abs(left.x - right.x) >= left.rx + right.rx
          || Math.abs(left.y - right.y) >= left.ry + right.ry + 21, 'candidate labels overlap');
      }
    }
  }
}
const layout = layoutModels(models, 1400);
const target = layout.items[5];
const rect = { left: 200, top: 170 };
const scrollTop = target.core.y - 150;
const point = worldPoint(target.core.x + rect.left, 150 + rect.top, rect, scrollTop);
assert.equal(hitModel(layout.items, point).model.model_name, target.model.model_name);
assert.equal(hitModel(layout.items, point, target.model.model_name), null);
const source = { kind: 'candidate', model: 'model-0', protocol: 'openai' };
assert.equal(transferCompatibility(source, models[0]), 'same-model');
assert.equal(transferCompatibility(source, models[1]), 'protocol');
assert.equal(transferCompatibility(source, models[2]), 'ok');
assert.equal(transferCompatibility({ kind: 'site', uid: 5 }, models[2], [{ id: 5, groups: [{ protocol: 'openai' }] }]), 'ok');
assert.equal(transferCompatibility({ kind: 'site', uid: 5 }, models[1], [{ id: 5, groups: [{ protocol: 'openai' }] }]), 'protocol');
console.log('liquid routing geometry and drag boundaries: ok');
