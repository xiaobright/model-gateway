import assert from 'node:assert/strict';

import {
  PROTO_CLIENT,
  PROTO_INFO,
  PROTO_LABEL,
  PROTO_PATH,
  PROTO_SHORT,
  PROTOCOLS,
  setProtocolMetadata,
} from '../web/util.js';
import { createRefreshQueue } from '../web/async-state.js';
import {
  beginGroupEdit,
  beginModelWrites,
  endModelWrites,
  isCurrentGroupEdit,
  pullRemoteModels,
  remoteModels,
  updateGroupEdit,
} from '../web/group-editor.js';

const names = setProtocolMetadata([
  { name: 'anthropic', label: 'Anthropic Messages', short: 'Anthropic', path: '/v1/messages', client: 'Claude Code', supports_1m: true },
  { name: 'openai', label: 'OpenAI Responses', short: 'OpenAI', path: '/v1/responses', client: 'Codex', supports_1m: false },
  { name: 'test-third', label: 'Test Third', path: '/test/third', client: 'Test Client', supports_1m: false },
]);

assert.deepEqual(names, ['anthropic', 'openai', 'test-third']);
assert.deepEqual(PROTOCOLS, names);
assert.equal(PROTO_INFO['test-third'].label, 'Test Third');
assert.equal(PROTO_LABEL['test-third'], 'Test Third');
assert.equal(PROTO_SHORT['anthropic'], 'Anthropic');
assert.equal(PROTO_SHORT['test-third'], 'Test Third', '没给 short 就退回完整标签');
assert.equal(PROTO_PATH['test-third'], '/test/third');
assert.equal(PROTO_CLIENT['test-third'], 'Test Client');

assert.throws(
  () => setProtocolMetadata([{ name: 'test-third', label: '', path: '/x', client: 'Test' }]),
  /不完整/,
);

const applied = [];
const loads = [];
const refresh = createRefreshQueue(
  async (args) => new Promise((resolve) => loads.push({ args, resolve })),
  (value) => applied.push(value),
);
const tick = () => new Promise((resolve) => setTimeout(resolve, 0));
const oldRefresh = refresh({ name: 'old' });
const newRefresh = refresh({ name: 'new' });
let oldResolved = false;
oldRefresh.then(() => { oldResolved = true; });
await tick();
loads[0].resolve('old');
while (loads.length < 2) await tick();
await tick();
assert.equal(oldResolved, false);
assert.deepEqual(applied, []);
loads[1].resolve('new');
await Promise.all([oldRefresh, newRefresh]);
assert.deepEqual(applied, ['new']);

const pollApplied = [];
const pollLoads = [];
const pollRefresh = createRefreshQueue(
  async (args) => new Promise((resolve) => pollLoads.push({ args, resolve })),
  (value) => pollApplied.push(value),
);
const firstPoll = pollRefresh({ name: 'poll-1' }, { allowIntermediate: true });
const secondPoll = pollRefresh({ name: 'poll-2' }, { allowIntermediate: true });
await tick();
pollLoads[0].resolve('poll-1');
while (pollLoads.length < 2) await tick();
await tick();
assert.deepEqual(pollApplied, ['poll-1']);
pollLoads[1].resolve('poll-2');
await Promise.all([firstPoll, secondPoll]);
assert.deepEqual(pollApplied, ['poll-1', 'poll-2']);

const originalFetch = globalThis.fetch;
let resolveFetch;
globalThis.fetch = () => new Promise((resolve) => { resolveFetch = resolve; });
const token = beginGroupEdit(1, 7);
const late = pullRemoteModels(
  7,
  `group:${token.seq}:7`,
  () => isCurrentGroupEdit(token),
  { onSuccess: () => { throw new Error('stale response painted'); } },
);
beginGroupEdit(1, 8);
resolveFetch(new Response(JSON.stringify({ models: ['late-model'] }), { status: 200 }));
await late;
assert.equal(remoteModels(7), undefined);

const writeToken = beginGroupEdit(1, 9);
assert.equal(beginModelWrites(writeToken, ['m']), true);
assert.equal(beginModelWrites(writeToken, ['m']), false);
endModelWrites(writeToken, ['m']);
assert.equal(beginModelWrites(writeToken, ['m']), true);
endModelWrites(writeToken, ['m']);

const movingToken = beginGroupEdit(1, 10);
assert.equal(beginModelWrites(movingToken, ['moving']), true);
assert.equal(updateGroupEdit(movingToken, 2, 11), true);
endModelWrites(movingToken, ['moving']);
assert.equal(beginModelWrites(movingToken, ['moving']), true);
endModelWrites(movingToken, ['moving']);
globalThis.fetch = originalFetch;

console.log('web protocol metadata behavior: ok');
