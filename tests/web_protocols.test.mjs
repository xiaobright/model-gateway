import assert from 'node:assert/strict';

import {
  PROTO_CLIENT,
  PROTO_INFO,
  PROTO_LABEL,
  PROTO_PATH,
  PROTOCOLS,
  setProtocolMetadata,
} from '../web/util.js';

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

console.log('web protocol metadata behavior: ok');
