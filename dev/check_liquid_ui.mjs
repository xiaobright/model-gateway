// Real browser acceptance checks against a disposable, locally seeded gateway.
// NODE_PATH may point at an existing Playwright installation; no app build needed.
import assert from 'node:assert/strict';
import { spawn } from 'node:child_process';
import { createRequire } from 'node:module';
import { fileURLToPath } from 'node:url';
import path from 'node:path';
import fs from 'node:fs/promises';

const require = createRequire(import.meta.url);
const { chromium } = require('playwright');
const root = fileURLToPath(new URL('../', import.meta.url));
const output = process.env.LIQUID_QA_DIR || path.join(root, 'dev', 'liquid-qa');
await fs.mkdir(output, { recursive: true });
const report = { started: new Date().toISOString(), passed: [], screenshots: [], pageErrors: [] };
const persist = () => fs.writeFile(path.join(output, 'report.json'), JSON.stringify(report, null, 2));
let browser, page, base;
const server = spawn(path.join(root, '.venv', 'Scripts', 'python.exe'), ['-u', 'dev/preview_liquid.py', '--port', '0'], {
  cwd: root, windowsHide: true, stdio: ['ignore', 'pipe', 'pipe'],
});
let serverLog = '';
server.stdout.on('data', (chunk) => { serverLog += chunk; });
const deadline = setTimeout(() => { server.kill(); process.exitCode = 1; void browser?.close(); }, 240_000);

async function step(name, run) {
  await run();
  report.passed.push(name);
  await persist();
  console.log('PASS ' + name);
}
async function screenshot(name, options = {}) {
  const filename = name + '.png';
  await page.screenshot({ path: path.join(output, filename), animations: 'disabled', ...options });
  report.screenshots.push(filename);
  await persist();
}
const modelNode = (name) => page.locator('.orbit-model[data-model=' + JSON.stringify(name) + ']');
const candidateNode = (id) => page.locator('.orbit-candidate[data-rid="' + id + '"]');
const routes = async () => {
  const response = await page.request.get(base + '/admin/api/models');
  assert.equal(response.status(), 200);
  return response.json();
};
const find = (all, name) => all.find((model) => model.model_name === name);
const center = (box) => ({ x: box.x + box.width / 2, y: box.y + box.height / 2 });
async function saved(action, endpoint) {
  const pending = page.waitForResponse((response) => response.url().endsWith(endpoint)
    && ['POST', 'PUT', 'DELETE'].includes(response.request().method()));
  await action();
  const response = await pending;
  assert.equal(response.status(), 200, await response.text());
  await page.waitForFunction(() => !document.querySelector('.orbit-workbench').classList.contains('is-saving'));
}
async function beginDrag(from, to) {
  const origin = center(await from.boundingBox());
  await page.mouse.move(origin.x, origin.y);
  await page.mouse.down();
  await page.mouse.move(to.x, to.y, { steps: 16 });
  await page.waitForSelector('.orbit-drag-label', { state: 'visible' });
}
async function dragTo(from, target) {
  await beginDrag(from, center(await target.boundingBox()));
  await page.mouse.up();
}
async function closePanel() { await page.keyboard.press('Escape'); }

try {
  base = await new Promise((resolve, reject) => {
    const timeout = setTimeout(() => reject(new Error('Preview server did not start')), 30_000);
    server.on('error', reject);
    server.on('exit', (code) => reject(new Error('Preview exited: ' + code + '\n' + serverLog)));
    server.stderr.on('data', (chunk) => {
      serverLog += chunk;
      const ready = serverLog.match(/Uvicorn running on (http:\/\/127\.0\.0\.1:\d+)/);
      if (ready) { clearTimeout(timeout); resolve(ready[1]); }
    });
  });
  browser = await chromium.launch({ headless: true, executablePath: process.env.LIQUID_BROWSER || undefined });
  page = await browser.newPage({ viewport: { width: 1680, height: 1050 }, colorScheme: 'dark' });
  page.setDefaultTimeout(15_000);
  page.on('pageerror', (error) => report.pageErrors.push(error.message));
  await page.goto(base + '/#orbit');
  await page.waitForSelector('.has-liquid-gl');
  await page.waitForFunction(() => document.querySelectorAll('.orbit-model').length === 8);
  await step('WebGL surface and two-column continuous layout', async () => {
    const first = await modelNode('claude-fable-5').boundingBox();
    const second = await modelNode('claude-opus-4-8').boundingBox();
    assert.ok(Math.abs(first.y - second.y) < 5 && second.x > first.x + 250);
    await screenshot('01-desktop-dark');
  });

  let snapshot = await routes();
  const sourceName = 'claude-fable-5', targetName = 'claude-opus-4-8';
  const firstId = find(snapshot, sourceName).candidates[0].route_id;
  await step('Candidate copy through pointer drag; source preserved', async () => {
    await beginDrag(candidateNode(firstId), center(await modelNode(targetName).boundingBox()));
    await page.waitForSelector('.orbit-model.is-target');
    await screenshot('02-drop-fusion');
    await saved(() => page.mouse.up(), '/admin/api/models/transfer');
    snapshot = await routes();
    assert.equal(find(snapshot, sourceName).candidates.length, 6);
    assert.equal(find(snapshot, targetName).candidates.length, 5);
    assert.equal(find(snapshot, targetName).candidates.at(-1).remote_model, 'claude-fable-5');
  });
  await step('Move merges duplicate and repairs source preferred candidate', async () => {
    await page.locator('[data-orbit="mode"][data-mode="move"]').click();
    await saved(() => dragTo(candidateNode(firstId), modelNode(targetName)), '/admin/api/models/transfer');
    snapshot = await routes();
    const source = find(snapshot, sourceName);
    assert.equal(source.candidates.length, 5);
    assert.equal(source.preferred_route_id, source.candidates[0].route_id);
    assert.equal(find(snapshot, targetName).candidates.length, 5);
    await page.locator('[data-orbit="mode"][data-mode="copy"]').click();
  });
  await step('Whole model copy and repeated merge are idempotent', async () => {
    await saved(() => dragTo(modelNode(sourceName), modelNode(targetName)), '/admin/api/models/transfer');
    assert.equal(find(await routes(), targetName).candidates.length, 10);
    await saved(() => dragTo(modelNode(sourceName), modelNode(targetName)), '/admin/api/models/transfer');
    assert.equal(find(await routes(), targetName).candidates.length, 10);
  });
  await step('Reorder candidates by dragging and switch preferred by double-click', async () => {
    snapshot = await routes();
    const source = find(snapshot, sourceName);
    const first = source.candidates[0].route_id, second = source.candidates[1].route_id;
    await saved(() => dragTo(candidateNode(second), candidateNode(first)), '/admin/api/models/order');
    assert.equal(find(await routes(), sourceName).candidates[0].route_id, second);
    await saved(() => candidateNode(second).dblclick(), '/admin/api/models/switch');
    assert.equal(find(await routes(), sourceName).preferred_route_id, second);
    await closePanel();
  });
  await step('Drop on shared empty space to split into a new model', async () => {
    const source = find(await routes(), sourceName);
    const id = source.candidates.at(-1).route_id;
    const board = await page.locator('#orbit-canvas').boundingBox();
    await beginDrag(candidateNode(id), { x: board.x + board.width / 2, y: board.y + 340 });
    await page.mouse.up();
    await page.locator('#oi-name').fill('claude-split-ui');
    await page.locator('#oi-operation').selectOption('move');
    await saved(() => page.locator('[data-oi="confirm-transfer"]').click(), '/admin/api/models/transfer');
    snapshot = await routes();
    assert.equal(find(snapshot, sourceName).candidates.length, 4);
    assert.equal(find(snapshot, 'claude-split-ui').candidates[0].remote_model, source.candidates.at(-1).remote_model);
  });
  await step('Auto-scroll during a long drag; incompatible protocol is rejected', async () => {
    const before = await routes();
    await modelNode(sourceName).scrollIntoViewIfNeeded();
    const board = await page.locator('#orbit-canvas').boundingBox();
    const startScroll = await page.locator('#orbit-canvas').evaluate((el) => el.scrollTop);
    await beginDrag(modelNode(sourceName), { x: board.x + board.width * .52, y: board.y + board.height - 9 });
    await page.waitForFunction(() => {
      const target = document.querySelector('.orbit-model[data-model="gpt-5.6-luna"]').getBoundingClientRect();
      const board = document.querySelector('#orbit-canvas').getBoundingClientRect();
      return target.top > board.top + 25 && target.bottom < board.bottom - 40;
    }, null, { timeout: 25_000 });
    assert.ok(await page.locator('#orbit-canvas').evaluate((el) => el.scrollTop) > startScroll + 200);
    const destination = center(await modelNode('gpt-5.6-luna').boundingBox());
    await page.mouse.move(destination.x, destination.y, { steps: 12 });
    await page.waitForSelector('.orbit-model.is-incompatible');
    await page.mouse.up();
    assert.deepEqual(await routes(), before);
    assert.match(await page.locator('#orbit-hint').textContent(), /接口格式不同/);
  });
  await step('Escape cancels a pending drag without changes', async () => {
    const before = await routes();
    const core = modelNode('gpt-5.6-luna');
    const box = center(await core.boundingBox());
    await beginDrag(core, { x: box.x + 50, y: box.y + 35 });
    await page.keyboard.press('Escape');
    await page.mouse.up();
    assert.equal(await page.locator('.orbit-drag-label').count(), 0);
    assert.deepEqual(await routes(), before);
  });
  await step('Source drop opens a real mapping editor; late group response is ignored', async () => {
    const board = await page.locator('#orbit-canvas').boundingBox();
    await beginDrag(page.locator('.orbit-source').filter({ hasText: /^站E/ }), { x: board.x + board.width / 2, y: board.y + 320 });
    await page.mouse.up();
    await page.locator('#oi-protocol').selectOption('openai');
    const initialGroup = await page.locator('#oi-group').inputValue();
    let releaseOld;
    const oldGate = new Promise((resolve) => { releaseOld = resolve; });
    await page.route('**/admin/api/groups/*/remote-models', async (route) => {
      if (route.request().url().includes('/groups/' + initialGroup + '/')) {
        await oldGate;
        await route.fulfill({ status: 200, json: { models: ['wrong-old-model'] } });
      } else await route.fulfill({ status: 502, json: { detail: 'Preview fetch failure' } });
    });
    const started = page.waitForRequest((request) => request.url().includes('/groups/' + initialGroup + '/remote-models'));
    await page.locator('[data-oi="pull-models"]').click();
    await started;
    assert.equal(await page.locator('[data-oi="pull-models"]').isDisabled(), true);
    await page.locator('#oi-protocol').selectOption('anthropic');
    assert.equal(await page.locator('[data-oi="pull-models"]').isDisabled(), false);
    await page.locator('#oi-map-model').fill('claude-mapping-ui');
    await page.locator('#oi-remote').fill('claude-real[1m]');
    const oldResponse = page.waitForResponse((response) => response.url().includes('/groups/' + initialGroup + '/remote-models'));
    releaseOld();
    await oldResponse;
    assert.equal(await page.locator('#oi-remote').inputValue(), 'claude-real[1m]');
    assert.equal(await page.locator('#oi-remotes option[value="wrong-old-model"]').count(), 0);
    await page.locator('[data-oi="pull-models"]').click();
    await page.waitForSelector('#oi-remote-status.is-error');
    assert.equal(await page.locator('#oi-remote').inputValue(), 'claude-real[1m]');
    await page.locator('#oi-onem').check();
    await saved(() => page.locator('[data-oi="save-mapping"]').click(), '/admin/api/models');
    assert.equal(find(await routes(), 'claude-mapping-ui').candidates[0].remote_model, 'claude-real[1m]');
    await page.unroute('**/admin/api/groups/*/remote-models');
  });
  await step('Reload keeps transferred, split, reordered and edited mappings', async () => {
    const before = await routes();
    await page.reload();
    await page.waitForSelector('.has-liquid-gl');
    await page.waitForFunction(() => document.querySelectorAll('.orbit-model').length === 10);
    assert.deepEqual(await routes(), before);
    assert.equal(await page.locator('#orbit-count').textContent(), '10 个模型 · 48 条候选');
  });
  await step('Live activity badge follows inflight data, then clears', async () => {
    const response = await page.request.get(base + '/admin/api/inflight');
    const payload = await response.json();
    const candidate = find(await routes(), sourceName).candidates[0];
    payload.calls = [{ id: 99, model: sourceName, protocol: 'anthropic', group_id: candidate.group_id,
      remote_model: candidate.remote_model, upstream: candidate.upstream_name, phase: 'streaming', elapsed_ms: 500 }];
    await page.route('**/admin/api/inflight', (route) => route.fulfill({ json: payload }));
    await modelNode(sourceName).locator('.orbit-model-traffic').waitFor({ state: 'visible' });
    assert.equal(await modelNode(sourceName).locator('.orbit-model-traffic').textContent(), '1 条请求流转中');
    payload.calls = [];
    await modelNode(sourceName).locator('.orbit-model-traffic').waitFor({ state: 'hidden' });
    await page.unroute('**/admin/api/inflight');
  });
  await step('Light theme, pause control, and system reduced motion', async () => {
    await page.emulateMedia({ colorScheme: 'light' });
    await screenshot('03-desktop-light');
    await page.locator('[data-orbit="motion"]').click();
    assert.equal(await page.locator('[data-orbit="motion"]').getAttribute('aria-label'), '开启液体动效');
    await page.locator('[data-orbit="motion"]').click();
    await page.emulateMedia({ reducedMotion: 'reduce' });
    await page.waitForFunction(() => document.querySelector('[data-orbit="motion"]').getAttribute('aria-pressed') === 'false');
    await page.emulateMedia({ reducedMotion: 'no-preference' });
  });
  await step('Narrow viewport stays readable without horizontal overflow', async () => {
    await page.setViewportSize({ width: 930, height: 950 });
    await page.waitForFunction(() => document.documentElement.scrollWidth <= innerWidth);
    assert.equal(await page.locator('.sidebar').evaluate((el) => getComputedStyle(el).flexDirection), 'column');
    await page.setViewportSize({ width: 390, height: 844 });
    await page.locator('#orbit-search').fill('claude-fable');
    await page.waitForFunction(() => document.documentElement.scrollWidth <= innerWidth);
    await page.waitForFunction(() => {
      const board = document.querySelector('#orbit-canvas').getBoundingClientRect();
      return [...document.querySelectorAll('.orbit-candidate:not([hidden])')].every((el) => {
        const box = el.getBoundingClientRect();
        return box.left >= board.left + 4 && box.right <= board.right - 4;
      });
    });
    const model = await modelNode(sourceName).boundingBox();
    assert.ok(model.x >= 0 && model.x + model.width <= 390);
    await screenshot('04-mobile-light', { fullPage: true });
    await modelNode(sourceName).click();
    await page.locator('[data-oi="transfer"][data-mode="copy"]').waitFor({ state: 'visible' });
    await closePanel();
  });
  await step('Empty search state keeps the new-model drop area unobstructed', async () => {
    await page.locator('#orbit-search').fill('no-matching-model-in-this-preview');
    await page.locator('#orbit-empty').waitFor({ state: 'visible' });
    const message = await page.locator('#orbit-empty').boundingBox();
    const pool = await page.locator('#orbit-new-pool').boundingBox();
    assert.ok(message.y + message.height < pool.y);
    await page.locator('#orbit-search').fill('claude-fable');
  });
  await step('WebGL context loss preserves usable glass fallback', async () => {
    await page.setViewportSize({ width: 1680, height: 1050 });
    await page.evaluate(() => document.querySelector('#orbit-surface').getContext('webgl').getExtension('WEBGL_lose_context').loseContext());
    await page.waitForFunction(() => !document.querySelector('.orbit-workbench').classList.contains('has-liquid-gl'));
    await modelNode(sourceName).click();
    await page.locator('[data-oi="transfer"][data-mode="copy"]').waitFor({ state: 'visible' });
    await screenshot('05-glass-fallback');
  });
  assert.deepEqual(report.pageErrors, []);
  report.status = 'passed';
} catch (error) {
  report.status = 'failed';
  report.error = error.stack;
  if (page) await screenshot('failure').catch(() => {});
  console.error(error.stack);
  process.exitCode = 1;
} finally {
  clearTimeout(deadline);
  report.finished = new Date().toISOString();
  await persist();
  await fs.writeFile(path.join(output, 'server.log'), serverLog);
  await browser?.close();
  server.kill();
  console.log('Report: ' + path.join(output, 'report.json'));
}
