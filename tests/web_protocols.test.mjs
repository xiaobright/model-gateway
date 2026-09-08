import assert from 'node:assert/strict';

import {
  PROTO_CLIENT,
  PROTO_INFO,
  PROTO_LABEL,
  PROTO_PATH,
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
} from '../web/group-editor.js';

const names = setProtocolMetadata([
  { name: 'anthropic', label: 'Anthropic Messages', path: '/v1/messages', client: 'Claude Code', supports_1m: true },
  { name: 'openai', label: 'OpenAI Responses', path: '/v1/responses', client: 'Codex', supports_1m: false },
  { name: 'test-third', label: 'Test Third', path: '/test/third', client: 'Test Client', supports_1m: false },
]);

assert.deepEqual(names, ['anthropic', 'openai', 'test-third']);
assert.deepEqual(PROTOCOLS, names);
assert.equal(PROTO_INFO['test-third'].label, 'Test Third');
assert.equal(PROTO_LABEL['test-third'], 'Test Third');
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
await tick();
loads[0].resolve('old');
while (loads.length < 2) await tick();
loads[1].resolve('new');
await Promise.all([oldRefresh, newRefresh]);
assert.deepEqual(applied, ['new']);

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
globalThis.fetch = originalFetch;

console.log('web protocol metadata behavior: ok');
