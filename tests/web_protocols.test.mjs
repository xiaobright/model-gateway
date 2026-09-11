import assert from 'node:assert/strict';

import {
  BRIDGE_FROM,
  PROTO_CLIENT,
  PROTO_INFO,
  PROTO_LABEL,
  PROTO_PATH,
  PROTO_SHORT,
  PROTOCOLS,
  bridgeSourceProtocols,
  bridgeTag,
  groupsForExpose,
  isBridgedPair,
  setProtocolMetadata,
  supportsExpose,
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
  { name: 'openai-chat', label: 'OpenAI Chat Completions', short: 'OpenAI Chat', path: '/v1/chat/completions', client: 'OpenAI SDK', supports_1m: false },
  { name: 'test-third', label: 'Test Third', path: '/test/third', client: 'Test Client', supports_1m: false },
]);

assert.deepEqual(names, ['anthropic', 'openai', 'openai-chat', 'test-third']);
assert.deepEqual(PROTOCOLS, names);
assert.equal(PROTO_INFO['test-third'].label, 'Test Third');
assert.equal(PROTO_LABEL['test-third'], 'Test Third');
assert.equal(PROTO_SHORT['anthropic'], 'Anthropic');
assert.equal(PROTO_SHORT['test-third'], 'Test Third', '没给 short 就退回完整标签');
assert.equal(PROTO_PATH['test-third'], '/test/third');
assert.equal(PROTO_CLIENT['test-third'], 'Test Client');

// 桥接名单是 gateway/protocols.py 的 BRIDGES 在前端的影子：候选弹窗靠它决定
// 「这个站的分组能不能挂在这个接口下」。两边对不上，界面就会把能用的站藏起来、
// 或者把一个后端会 409 的组合摆出来
assert.deepEqual(BRIDGE_FROM, { openai: ['openai-chat'] });
assert.deepEqual(bridgeSourceProtocols('openai'), ['openai-chat']);
assert.deepEqual(bridgeSourceProtocols('anthropic'), []);
assert.deepEqual(bridgeSourceProtocols('test-third'), []);
assert.equal(isBridgedPair('openai-chat', 'openai'), true);
assert.equal(isBridgedPair('openai', 'openai'), false, '同一个协议是原生，不是桥接');
assert.equal(isBridgedPair('anthropic', 'openai'), false, '没实现的组合不算桥接');
assert.equal(isBridgedPair('openai-chat', 'openai-chat'), false);
assert.equal(isBridgedPair('', 'openai'), false);

const mixedSite = {
  name: 'mixed',
  supports: ['openai', 'openai-chat'],
  groups: [
    { id: 1, name: 'chat', protocol: 'openai-chat' },
    { id: 2, name: 'native', protocol: 'openai' },
    { id: 3, name: 'claude', protocol: 'anthropic' },
  ],
};
assert.deepEqual(groupsForExpose(mixedSite, 'openai').map((g) => g.id), [1, 2],
  '原生 openai 分组 + 能转换过来的 openai-chat 分组');
assert.deepEqual(groupsForExpose(mixedSite, 'anthropic').map((g) => g.id), [3]);
assert.deepEqual(groupsForExpose(mixedSite, '').map((g) => g.id), [1, 2, 3]);
assert.equal(supportsExpose(mixedSite, 'openai'), true);
assert.equal(supportsExpose({ groups: [{ id: 9, protocol: 'openai-chat' }] }, 'openai'), true);
assert.equal(supportsExpose({ groups: [{ id: 9, protocol: 'anthropic' }] }, 'openai'), false);

// 候选圆片上的「桥接」标签
assert.equal(bridgeTag(null), '');
assert.equal(bridgeTag({ bridged: false }), '');
assert.equal(bridgeTag({ bridged: undefined }), '', '老后端没有这个字段时不该炸');
const tag = bridgeTag({ bridged: true, group_protocol: 'openai-chat', expose_protocol: 'openai' });
assert.match(tag, /^ <span class="tag tag-accent"/);
assert.match(tag, />桥接<\/span>$/);
assert.match(tag, /OpenAI Chat/, 'title 里要说清从哪来');
assert.match(tag, /OpenAI\b(?! Chat)/, 'title 里要说清到哪去');
assert.equal(
  bridgeTag({ bridged: true, group_protocol: 'x<y', expose_protocol: 'openai' }),
  bridgeTag({ bridged: true, group_protocol: 'x<y', expose_protocol: 'openai' }),
);
assert.ok(!bridgeTag({ bridged: true, group_protocol: 'x<y', expose_protocol: 'openai' }).includes('<y'),
  '协议名要转义，别把 HTML 注进去');

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
