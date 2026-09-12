# 协议桥接方案：Responses 客户端 → Chat Completions 上游

> 交接文档（2026-09-10）。**方向已定**：客户端发 Responses API（Codex），上游只有 OpenAI
> Chat Completions 站。转换是双向的：请求 responses→chat，响应 chat→responses（含流式）。
> 接手方先读第 0 节，再把第 8 节的待确认决策拍板，然后按第 7 节里程碑开工。

## 0. 接手须知

- 网关仓库：`<仓库根>`（纯 Python，依赖只有 fastapi/uvicorn/httpx/pystray/pillow/pytest，venv 是 Python 3.14.0）。
- 参考实现：`<参考仓库>/litellm`，v1.102.0，commit `e5da59336d`。**只读参考，不装进网关**。
  许可证 MIT（`enterprise/` 目录除外，我们不碰那里）。移植代码要注明出处。
- 基线（动手前先跑一遍）：
  - `pytest -q -m "not network"` → 应 **120 passed**（10 个 network 用例不跑）
  - `node tests/web_protocols.test.mjs` 等 `tests/web_*.test.mjs` 全过
- 网关当前有 3 种协议：`anthropic`、`openai`（Responses）、`openai-chat`（Chat Completions），
  见 `gateway/protocols.py:337`。后端改完必须重启网关（`start-gateway.bat`），
  DB 会自动迁移并备份（`data/gateway.db`，勿动根目录同名文件）。
- 参考库不可用时：本方案第 3 节已把关键语义写全，按它实现即可；能读源码时优先以源码为准。
- 已拍板：方向（本页）、转换实现放**进程内纯函数**（不要 sidecar、不要 litellm 当依赖）。
- 未拍板：第 8 节，共 8 条。

## 1. 目标与范围

**做**：

1. 客户端 `POST /v1/responses`（Codex，通常 `stream: true`）→ 转成 Chat Completions 请求发上游。
2. 上游 chat 的非流式 JSON / 流式 SSE → 转回 Responses API 的 JSON / SSE 事件流。
3. 只在**候选显式开启转换**时生效，默认关；没开的行为与现在一字不差。
4. 统计、转发记录、降级、断路器照旧工作，并标记这条请求是转换来的。

**不做（v1）**：

- 反向（chat 客户端 → Responses 上游）；参考实现在 `litellm/completion_extras/litellm_responses_transformation/`，以后再说。
- Anthropic 任何方向的桥接。
- `previous_response_id` 的会话状态模拟（litellm 用 `session_handler.py` + InMemoryCache 做，
  我们不引入；见第 8 节决策 3）。
- 图片输入、annotation、code interpreter、image generation 等 Codex 不用的能力（litellm 有，
  先不移植；架构留好位置即可）。

## 2. 参考实现清单（litellm 1.102.0）

主模块：`litellm/responses/litellm_completion_transformation/`

| 文件 | 行数 | 用途 |
|---|---|---|
| `transformation.py` | 2775 | 请求/非流式响应映射（核心） |
| `streaming_iterator.py` | 1198 | chat chunk → Responses SSE 事件状态机 |
| `custom_tools.py` | 188 | apply_patch 等 `type:"custom"` 工具的双向转换 |
| `handler.py` | 134 | 编排（调 `litellm.completion`）——**不移植** |
| `session_handler.py` | 320 | `previous_response_id` 状态——**不移植** |

### 请求侧要移植的函数（`transformation.py`）

| 位置 | 函数 | 说明 |
|---|---|---|
| :312 | `transform_responses_api_request_to_chat_completion_request` | 总入口，输出 chat 请求 dict |
| :405 | `transform_responses_api_input_to_messages` | `instructions` + input items → messages |
| :521 | `_transform_response_input_param_to_chat_completion_message` | 单条 input item 分派 |
| :1236 | `_transform_responses_api_input_item_to_chat_completion_message` | 同上（细分派） |
| :1592 | `_transform_responses_api_function_call_to_chat_completion_message` | 历史 function_call → assistant.tool_calls |
| :1456 | `_transform_responses_api_tool_call_output_to_chat_completion_message` | function_call_output/custom_tool_call_output → tool 消息 |
| :834 | `_deduplicate_tool_call_output_messages` | 去重 |
| :1113 | `_ensure_tool_results_have_corresponding_tool_calls` | 工具结果与调用配对修复 |
| :1383 | `_decode_thinking_blocks_from_input_item` | reasoning item 的 `encrypted_content` 回放 |
| :1781 | `transform_instructions_to_system_message` | instructions → system |
| :1945 | `transform_responses_api_tools_to_chat_completion_tools` | 工具映射（含 custom→function、web_search） |
| :213 | `_transform_tool_choice` | tool_choice 映射 |
| :2727 | `_transform_text_format_to_response_format` | `text.format` → `response_format` |

### 响应侧要移植的函数

| 位置 | 函数 |
|---|---|
| `transformation.py:2259` | `transform_chat_completion_response_to_responses_api_response`（**支持 dict 入参**：`ModelResponse(**dict)`，我们直接吃上游 JSON） |
| `transformation.py:2319` | `_transform_chat_completion_choices_to_responses_output` |
| `transformation.py` 内 | `_extract_reasoning_output_items`、`_extract_message_output_items`、`transform_chat_completion_tools_to_responses_tools`、`_transform_chat_completion_usage_to_responses_usage`、`_map_chat_completion_finish_reason_to_responses_status` |
| `streaming_iterator.py:73` | `LiteLLMCompletionStreamingIterator` 整个状态机 |
| `streaming_iterator.py:793` | `common_done_event_logic`（收尾事件 + completed） |
| `streaming_iterator.py:825` | `_ensure_output_item_for_chunk`（output item 的懒创建） |
| `streaming_iterator.py:1057` | `_transform_chat_completion_chunk_to_response_api_chunk`（文本/推理/annotation 增量） |
| `streaming_iterator.py:175/:261` | 工具调用的 delta/done 事件队列 |
| `streaming_iterator.py:1173` | `_emit_response_completed_event`（带 usage） |
| `custom_tools.py` 全部 | `extract_custom_tool_names`:42、`is_custom_tool_call`:53、`serialize_tool_call_arguments`:58、`unwrap_custom_tool_arguments`:73、`build_tool_call_item_kwargs`:95、`convert_custom_tool_to_function_tool`:156 |

### 黄金测试（用来生成我们的单测语料）

`tests/test_litellm/responses/litellm_completion_transformation/`：

- `test_litellm_completion_responses.py`（4248 行）——请求/响应映射的大全
- `test_streaming_iterator_transformation.py`（707 行）——事件序列
- `test_reasoning_input_item_preservation.py`（384 行）——reasoning 回放
- `test_function_call_output_normalization.py`（42 行）、`test_tool_output_order_preserved_for_gemini.py`（159 行）
- **跳过**：`test_session_handler*.py`、`test_image_generation_output.py`（v1 不需要）

### 关键坑（litellm 代码里能看到的）

- litellm 同时支持 pydantic 对象和 dict，到处是 `getattr`/`_get_mapping_or_attr_value` 双通道防御。
  我们只吃 raw dict，**不要 1:1 抄**，按 dict-only 重写能砍掉近一半代码。
- Responses 流式必须带 `sequence_number`；`output_index` 的分配（message=0、reasoning、每个工具调用）
  要严格照抄状态机。
- 流式请求必须加 `stream_options.include_usage=true`，否则 `response.completed` 里没 usage，Codex 会不认。
- `ModelResponse(**dict)` 对字段很宽容，但 `usage` 细节字段（`prompt_tokens_details.cached_tokens`）和
  `choices[].message.reasoning_content` 是 litellm 扩展字段，直接来自上游 JSON，转换时要保留。
- 事件类型全名见 `litellm/types/llms/openai.py:1464` `ResponsesAPIStreamEvents`。

## 3. 转换规格

### 3.1 请求（Responses → Chat）

输入：客户端原始 JSON（dict）；输出：chat 请求 dict。

- `instructions` → 头一条 `{"role":"system","content":...}`
- `input` 数组逐条映射：
  - `message`（`input_text`/`output_text` 等 part）→ `{"role":..., "content":...}`；
    assistant 的 `output_text` → assistant content；`reasoning` item → assistant 的
    `reasoning_content` +（有 `encrypted_content` 时解码回 thinking blocks，见 litellm :1383）
  - `function_call` → assistant 消息的 `tool_calls[{id,name,arguments}]`
  - `function_call_output` → `{"role":"tool","tool_call_id":...,"content":...}`
  - `custom_tool_call` / `custom_tool_call_output` → 同上，但参数按 custom 工具规则处理
  - 工具结果与调用不配对时按 litellm :1113 修复（补空 assistant tool_call 或去重）
- `tools`：
  - `{"type":"function"}` 原样透传（name/description/parameters/strict）
  - `{"type":"custom"}`（Codex 的 `apply_patch`）→ function 工具：参数固定为
    `{"content": string}`，grammar（`format.syntax`/`format.definition`）拼进 description
    （`custom_tools.py:156`）。同时记下 custom 工具名集合，响应侧要还原
  - `{"type":"web_search"}` 等内置工具：v1 丢弃（见决策 5）
- `tool_choice`：`auto|none|required|{type:"function",name}` 映射（:213）
- `max_output_tokens` → `max_tokens`；`temperature`/`top_p`/`user`/`parallel_tool_calls` 直传
- `reasoning.effort` → `reasoning_effort`（`summary` 字段对 chat 无意义，丢弃）
- `text.format` → `response_format`（json_schema/json_object/text；:2727）
- `stream: true` 时加 `stream_options: {"include_usage": true}`
- 丢弃：`store`、`previous_response_id`（决策 3）、`include`、`truncation`、
  `prompt_cache_key`、`safety_identifier`、`context_management`、`text.verbosity`
- `model` 换成候选的 `remote_model`（proxy 层做，桥接函数不碰）

### 3.2 非流式响应（Chat → Responses）

上游 JSON → Responses JSON（litellm :2259 的 dict-only 版）：

- `id`/`created`/`model` 沿用上游；`object: "response"`
- `status`：finish_reason `stop`→`completed`，`length`→`incomplete`，`content_filter`→`incomplete`（:map 函数）
- `output` 数组，顺序：reasoning item（有 `reasoning_content` 时）→ message item
  （`content:[{type:"output_text",text,annotations:[]}]`）→ 每个 tool call 一个 item：
  - 普通函数 → `{"type":"function_call","call_id","name","arguments","status":"completed"}`
  - custom 工具（apply_patch）→ `{"type":"custom_tool_call","call_id","name","input",...}`，
    `input` 是把 arguments JSON 解包后的 content 字符串（`custom_tools.py:73`）
- `usage`：`{input_tokens, input_tokens_details:{cached_tokens}, output_tokens,
  output_tokens_details:{reasoning_tokens}, total_tokens}`
- `tools`/`tool_choice`/`temperature`/`top_p`/`max_output_tokens`/`parallel_tool_calls` 回填
  （没有就缺省，Codex 不强求）

### 3.3 流式响应（Chat SSE → Responses SSE）

上游帧：`data: {...}`（chat chunk）与 `data: [DONE]`。要产出的事件（按顺序）：

1. `response.created` → `response.in_progress`
2. 首个内容块：`response.output_item.added`（message item）→ `response.content_part.added`
3. 文本：`response.output_text.delta`（`delta` 字符串）
4. 推理：`response.reasoning_summary_text.delta`（单独 reasoning item，`rs_` 前缀 id）；
   收尾 `response.reasoning_summary_text.done` → `response.reasoning_summary_part.done`
5. 工具调用：`response.output_item.added`（function_call / custom_tool_call item，
   `output_index` 从 1 起递增）→ `response.function_call_arguments.delta` → `...done`
   （custom 工具发 `custom_tool_call_input.delta/done` 之类要按 litellm 实现核对）
6. 收尾：`response.output_text.done` → `response.content_part.done` → `response.output_item.done`
7. `response.completed`（**必须带完整 usage**；随后不再发 `[DONE]`，Responses 协议没有这个标记）

事件统一带 `sequence_number`（自增）和正确的 `item_id`/`output_index`/`content_index`。
状态机细节以 `streaming_iterator.py` 为准，逐函数对照移植。

### 3.4 Codex 专属注意

- 先用现有抓取机制抓一份真实 Codex 请求：在 `data/` 放 `capture.flag`，发一次请求，
  看 `data/captured_request_shape.json` / `captured_headers.json`（`proxy.py:210`）。
  用这份 shape 做第一个 fixture，别凭记忆造。
- 预期 input item 类型：`message`、`reasoning`、`function_call`、`function_call_output`、
  `custom_tool_call`、`custom_tool_call_output`（apply_patch）。
- 预期 tools：`shell`（function）、`apply_patch`（custom）、`web_search`（内置）、可能还有
  `local_shell`/`view_image`/`update_plan`。**`local_shell` litellm 没覆盖**（决策 6）。
- Codex 要求流式（假定 `stream:true`，以抓包为准）；`include` 里可能有
  `reasoning.encrypted_content`——chat 上游给不了加密推理，我们只回摘要，多轮时 Codex 把
  摘要回传，我们按 litellm :1383 的逻辑当普通 reasoning 处理（M5 真机验证）。

## 4. 网关接入设计

### 4.1 数据模型（schema v6）

`SCHEMA_VERSION` 5→6（`gateway/db.py:109`），加两列 + 一个 stage：

- `model_routes` 加 `expose_protocol TEXT NOT NULL DEFAULT ''`：
  **空 = 原生**（走分组自己的接口）；非空 = 这条候选经桥接暴露成这个协议。
  v1 只允许一种组合：分组 `openai-chat` + `expose_protocol='openai'`。
  （比布尔 `converted` 好：方向显式，以后加 anthropic 桥接不用改语义。）
- `request_log` 加 `converted INTEGER NOT NULL DEFAULT 0`。
- 迁移函数 `_migrate_*` 照 `_migrate_group_models`（`db.py:327`）的写法补列。

`Route`（`db.py:163`）加字段：`group_protocol: str = ""`、`expose_protocol: str = ""`。
`_CHAIN_QUERY`（`db.py:1087`）的 SELECT 带上 `m.expose_protocol`、`g.protocol AS group_protocol`。

### 4.2 resolve_chain / 暴露规则

- `resolve_chain(model, client_proto)`（`db.py:1172`）改为：
  `WHERE m.model_name=? AND ((g.protocol=? AND m.expose_protocol='') OR m.expose_protocol=?)`
  即：原生候选 + 暴露成该协议的桥接候选，统一按 `is_active DESC, priority, id` 排。
- 「模型在哪个接口下暴露」`_model_protocol`：取所有候选的
  `COALESCE(NULLIF(m.expose_protocol,''), g.protocol)`，要求**全相同**（维持现有不变式：
  一个模型名只在一个接口下暴露；允许原生 + 桥接候选混排，见决策 2）。
- `add_model_route`（`db.py:857`）加参数 `expose_protocol=""`，校验：
  `expose_protocol` 为空 → 现有逻辑；非空 → 必须是 `openai-chat → openai` 且与该模型
  已暴露的协议一致。`ProtocolMismatch` 文案同步更新。
- `transfer_model_routes`（`db.py:899`）同步带上 `expose_protocol` 校验。
- `admin.get_model_routes`（`admin.py:546`）返回候选时带 `expose_protocol`、`group_protocol`、
  派生 `bridged` 布尔；`POST /admin/api/models`（`admin.py:614`）和 `PUT /admin/api/models`
  接受 `expose_protocol`（默认空）。

### 4.3 forward() 分支（`proxy.py:385`）

循环内按候选算：

```python
bridged = bool(route.expose_protocol) and route.expose_protocol != route.group_protocol
upstream_proto = protocols.by_name(route.group_protocol) if bridged else proto
```

- URL：`upstream_endpoint(route.upstream.base_url, upstream_proto.path)`（不是客户端 path）
- 头：`_build_headers(request, route.upstream, upstream_proto, want_1m)`
  （chat 的 auth/defaults；Codex 指纹头 v1 原样透传，决策 4）
- 体：`bridged` 时先 `bridge.responses_to_chat(payload, model=remote)` 再 `json.dumps`；
  否则走现有改名逻辑。转换失败 → 400（用 `proto.error_body`）。
- 响应：
  - 非流式（上游 JSON）→ `await upstream_resp.aread()`，`bridge.chat_to_responses(...)`，
    返回 `JSONResponse`，照常 `_record`。
  - 流式（上游 SSE）→ 在 `relay()` 里把原始字节喂给 `bridge.StreamBridge`，
    `yield` 生成的事件字节。**统计观察器用 `upstream_proto`（chat）跑**（文本/思维/usage
    都按上游 chat 帧算），`resp_bytes` 记实际发给客户端的字节。
- `_record`（`proxy.py:819`）/`db.insert_request`（`db.py:1241`）加 `converted` 参数落库；
  日志行加 `bridge=openai-chat->openai` 标记。
- 断路器/降级不变：转换按候选发生，站级失败照常换下一个候选；转换失败是请求问题，
  不该记站点失败。

### 4.4 管理端与前端

- 接口：见 4.2。桥接开关只挂在**候选**上，默认关。
- 前端（`web/views.js`/`app.js`/`util.js`）：
  - 模型卡片里 chat 分组的候选显示「桥接」小标签；
  - 「添加上游」弹窗里，当模型暴露协议是 `openai` 时，`openai-chat` 分组可勾选
    「转换为 Responses 暴露」；
  - 同步更新 `tests/web_*.test.mjs`。
- README 补一节「协议转换」；`protocols.py:3` 与 `proxy.py:866` 的「网关不做格式转换」
  注释要改。

## 5. 建议模块结构

```
gateway/bridge/__init__.py   # 公开 API：responses_to_chat / chat_to_responses / StreamBridge / BridgeError
gateway/bridge/request.py    # 请求映射
gateway/bridge/response.py   # 非流式响应映射
gateway/bridge/stream.py     # 流式状态机 + SSE 解析
gateway/bridge/tools.py      # 工具映射（custom/apply_patch、tool_choice）
```

- 纯 dict 进、纯 dict 出；不 import pydantic/openai/litellm。
- 每个文件头部注明「移植自 litellm v1.102.0（MIT），原文件路径 + 函数名」。
- `StreamBridge` 接口建议：`feed(chunk: bytes) -> list[bytes]`、`finish() -> list[bytes]`，
  内部维护未完整帧的 buffer（参考 `protocols.SSEObserver.feed` 的帧切分，`protocols.py:388`）。

## 6. 测试计划

1. **单测**（新 `tests/test_bridge_*.py`）：
   - 请求：Codex 真实 shape（第 3.4 节抓的）、instructions、历史 function_call/output、
     apply_patch custom、tool_choice、reasoning 回放。断言与 litellm 输出等价的 dict。
   - 非流式响应：文本、文本+推理、function_call、custom_tool_call、usage。
   - 流式：把 litellm 的 `test_streaming_iterator_transformation.py` 用例改造成
     「喂 chat chunk 列表 → 断言事件列表（类型、顺序、关键字段、sequence_number）」。
2. **集成**（新 `tests/test_bridge_proxy.py`，用 `gateway` fixture）：
   - `MockUpstream`（`tests/helpers.py:432`，内部 app 在 `build_upstream_app`:93）已有
     `/v1/chat/completions`（:224），扩展成
     可脚本化：返回工具调用、reasoning、可控 usage、`[DONE]`、中途断流。
   - 建 `openai-chat` 分组 + 候选 `expose_protocol='openai'`，`POST /v1/responses`：
     - 默认（不开桥接）→ 仍然 404，行为不变
     - 非流式 → output/usage 正确
     - 流式 → 事件序列完整、`response.completed` 带 usage
     - 降级：原生 Responses 候选 500 → 桥接候选 200；`request_log` 的 `converted`/`attempt`/`note` 正确
     - 转换失败 → 400，不误伤断路器
3. **回归**：`pytest -q -m "not network"`、`node tests/web_*.test.mjs`。
4. **真机验收（M5）**：配一个真 chat-only 站当桥接候选，Codex 走一轮带 apply_patch 的
   多轮会话；确认工具调用、reasoning、usage、断流恢复。

## 7. 里程碑

| # | 内容 | 完成标志 |
|---|---|---|
| M0 | 抓 Codex 真实请求、第 8 节拍板、基线跑绿 | 有 fixture、决策有结论 |
| M1 | `bridge/request.py` + `bridge/response.py` + 单测 | 单测过，非流式全绿 |
| M2 | `bridge/stream.py` + 单测 | 事件序列与 litellm 黄金用例一致 |
| M3 | 网关接入：v6 迁移、Route 字段、resolve_chain、forward 分支、记录 | 集成测试过，默认关行为不变 |
| M4 | 管理端 API + 前端 + web 测试 | 页面上能开/关桥接 |
| M5 | 真机 Codex 验收 | 一轮真实工具会话成功 |
| M6 | README/注释更新，整理提交 | `pytest -m "not network"` 全绿 |

## 8. 已拍板（2026-09-10，接手方定）

全部采用本文档给出的推荐项。下面每条后面括号里是「不这么做会怎样」，方便以后回头改。

1. **开关字段**：`model_routes.expose_protocol`，候选级，空 = 原生。不做全局 kill-switch。
   （布尔 `converted` 表达不了方向，以后加 anthropic 桥接就得改语义。）
2. **混合链**：同一个模型名允许「原生候选 + 桥接候选」按 priority 混排。
   （不允许的话，灰度只能靠改名；允许之后一个模型名就能先挂一条桥接候选试水。）
3. **有状态请求**：`previous_response_id` 非空时**桥接候选直接不参与**，客户端拿到的是
   「原生候选的失败/404」而不是转换错误。`store` 忽略，请求体里丢掉。
   （litellm 用 session_handler + 内存缓存模拟，我们不引入 —— 那是另一套状态。）
4. **上游请求头**：Codex 的指纹头原样透传（网关本来就按请求头转发，只有 key 和
   协议默认头会被覆盖），需要改的站在「请求头覆写」里处理。
   （改成 chat 默认头会让按客户端指纹放行的站把我们拦掉。）
5. **`web_search` 内置工具**：丢弃，不转 `web_search_options`。丢弃的类型名记进
   `_bridge_dropped_tools`，日志与转发记录里能看到。
6. **litellm 未覆盖的 item/tool**：**input item 明确 400**（`item_reference`、`local_shell_call`
   之类认不出来的会话状态不能猜形状）；**tools 里认不出的类型丢弃**（`local_shell`、
   `computer_use`、`mcp`…）。两者分开是因为：item 是会话状态，猜错会让模型基于错乱的
   上下文说胡话；tool 是能力，丢一个工具只是这一轮少一种手段，硬透传只会让站 400 掉整个请求。
7. **响应 id**：沿用上游 chat 的 id（`chatcmpl-...`）。
   （伪装成 `resp_...` 会丢掉「这轮到底打到了哪个站」的唯一线索。）
8. **reasoning**：上游 `reasoning_content` / `reasoning` 两个字段名都认；上游完全没有
   推理内容时正文照旧当纯文本走，不编造 reasoning item。

## 9. 风险清单

- 流式事件状态机是最容易踩坑的部分：事件顺序、`output_index`、`sequence_number`、
  item id 前缀，全部照 litellm 源码和黄金测试逐条对齐。
- 不要照抄 litellm 的 pydantic/dict 双通道防御代码，按 dict-only 重写并保留语义。
- 上游 chat 站行为差异大（工具参数增量拼接、reasoning 字段名、usage 缺失）；集成测试的
  mock 要能覆盖这些分支。
- 桥接请求体的 `model` 必须是候选的 `remote_model`；客户端看到的仍是请求里的模型名。
- 转换后的流式观察：统计一律以上游 chat 字节为准，别拿生成的事件流当上游统计源。
- 迁移 v6 会备份真实库；开发时先用临时库验证迁移，再碰 `data/gateway.db`。

## 10. 接手记录（2026-09-10 晚）

### 10.1 M0 的真实抓包推翻了两处前提

`data/captured_request_shape.json`（Codex Desktop 0.153.4，请求头
`x-openai-internal-codex-responses-lite: true`）显示：

```
top_level_keys: client_metadata include input model parallel_tool_calls
                prompt_cache_key reasoning store stream text tool_choice
input_item_types: additional_tools=1 message=20 reasoning=10
                  custom_tool_call=9 custom_tool_call_output=9
tools_count: 0
```

**两条都会让每一个真实请求挂掉**：

1. **顶层 `tools` 是空的**，工具定义在 `input` 里一个
   `{"type":"additional_tools","role":"developer","tools":[...]}` 的 item 上。litellm 只在
   `llms/bedrock_mantle/responses/transformation.py` 里做了这个 hoist，通用的
   `litellm_completion_transformation` **没有** —— 照抄 litellm 会得到一条没有任何工具的
   请求，模型永远不调 `apply_patch`，现象是「Codex 里工具凭空消失」。
2. **顶层没有 `instructions`**，系统提示词是 `input` 里 `role:"developer"` 的 message item
   发来的。而兼容站基本只认 system/user/assistant/tool 四个经典角色，原样透传 `developer`
   得到的是 422 `unknown variant 'developer'`（真机 M5 首跑就是这个错，见 §10.7）。
   litellm 的 `_input_item_role` 原样透传、连校验都没有，所以这条也得自己修 ——
   `bridge/request.py` 里 `_ROLE_ALIASES = {"developer": "system"}`，其余 role 一律原样过
   （有些站本来就认 `latest_reminder` 这类变体，悄悄改掉反而把能用的请求改坏）。

所以 `bridge/request.py` 自己实现了 `_split_additional_tools`，并且
`all_tools_of()` 是**唯一**取工具清单的入口（请求侧转换和响应侧认 custom 工具名都用它）；
role 的映射只收在 `_chat_role()` 一个函数里。

顺带把抓形状的探针补上了：`data/captured_request_shape.json` 现在会多记一份
`input_item_roles`（`type:role` 计数）。上一份抓包只记 type，所以「全是普通 message」
什么都看不出来 —— 这个坑本该在 M0 就暴露。

### 10.2 与 litellm 的有意差异（都写在代码注释里）

| 位置 | litellm | 我们 | 为什么 |
|---|---|---|---|
| SSE 编码 | proxy 层补 `data: [DONE]` | 桥接自己补 `data: [DONE]` | 客户端与网关自己的 `SSEObserver` 两种都认，补上兼容面更宽 |
| `sequence_number` | 有几处漏赋值、`output_item.done` 硬编码成 1 | 全部自增、单调且唯一 | 协议要求它单调；乱序到达时客户端按它排序 |
| 流里的 error 帧 | 抛异常 | 补发 `response.failed` | 不补的话客户端一直等收尾事件 |
| 孤立 tool 结果 | 只并进**前面那条** assistant | 前面没有 assistant 时插一条独立的 | 历史被压缩后那条 assistant 往往也没了，`role:tool` 紧跟 user 是非法历史 |
| 事件名 | — | 与 litellm 一致 | custom 工具也发 `response.function_call_arguments.*`（litellm 从不发 `custom_tool_call_input.*`），Codex 认这个 |

**没有**改动的部分（照抄，别自作聪明去「修」）：`role`-only 首帧会让 message item 占掉
那次唯一的 `output_item.added`，后面的推理只以 delta 出现、不宣告 reasoning item。
两个 item 都用 `output_index 0`，宣告两个会让客户端按 index 归并时错乱。

### 10.3 里程碑进度

- M0 ✅ 决策拍板（第 8 节）、真实形状 fixture（`tests/fixture/codex_responses_request.json`）、
  基线 120 passed + web 测试绿。
- M1 ✅ `bridge/{errors,tools,request,response}.py`，单测 50 个。
- M2 ✅ `bridge/stream.py`，单测 22 个。
- M3 ✅ schema v6（`expose_protocol` + `converted`）、`resolve_chain` 认桥接候选、
  `_model_protocol`/`_tier_match`/`exposed_models` 全部改成比**有效协议**、forward 分支、
  `_relay_bridged_json`、流式 relay、`_record(converted=)`。集成测试 13 个。
- M4 ✅ `admin` 的 `expose_protocol` 入参 / `bridged` 出参、前端桥接标签与勾选行、
  `BRIDGE_FROM` 名单进 `util.js`。web 测试补了 12 条断言。
- M6 ✅ README 补「协议转换（桥接）」一节；`protocols.py:3`、`proxy.py` 路由段、
  README 末尾那句「不做协议转换」都改掉了。

回归：`pytest -q -m "not network"` **206 passed**（原 120，新增 86）；
`node tests/web_protocols.test.mjs` 过；三个前端文件 `node --check` 过。

### 10.4 这一轮踩到的三个坑（都已有测试钉住）

1. **URL 拼两次 `/v1`**：描述符里的 `path` 是 `/v1/chat/completions`，而
   `upstream.endpoint()` 自己会补 `/v1` —— 直接拿 `proto.path` 拼出 `/v1/v1/…`，
   上游一律 404。修法是 `proxy._forward_path()` 剥前缀；纯单测碰不到 URL 拼接，
   只有集成测试能发现，所以专门留了一条 `test_upstream_path_is_not_double_prefixed`。
2. **改候选自己的暴露方式会和自己撞 409**：`_model_protocol()` 会把这条候选自己算进去，
   于是「关掉桥接」时它拿旧的有效协议去比新算出来的有效协议。修法是加
   `exclude_route_id`。
3. **`stateful` 把 `store: false` 也算成有副作用**（既有行为，这次没改）：`proxy.forward`
   里是 `payload.get("store") is not None`，而真 Codex 每轮都带 `store: false` ——
   等于 Codex 的请求从来不走自动降级。要改就是 `is True`，但那会改掉现有降级策略，
   不在这轮范围内，先记在这里。集成测试里的降级用例因此特意不带 `store`。

### 10.5 验证方式

- 真实库副本跑 v6 迁移：v5 → v6，两列都补上，43 条候选 / 2000 条记录一条不少，
  4 个暴露模型仍在、`resolve_chain` 正常，备份留了一份。
- 新代码起在 8321 上（`--no-tray`），无头 Chrome 加载管理页：4 张模型卡都渲染出来，
  控制台无报错，`rt-bridge-wrap` 结构在位（当时的截图已在开源清理时移除）。
  真实配置里没有 openai-chat 分组，所以**桥接标签没能在真机上看到**
  （标签本身由 `util.bridgeTag` 的单测覆盖）。

### 10.6 还没做

- **真机 Codex 验收（M5）**：见 §10.7–§10.9 —— 前两跑各修掉一个真 bug（role、namespace），
  第 3 跑（工具调用 / 长参数 / 多轮回放）还没验过，**这是当前唯一的未完事项**。
- 图片输入按 v1 范围丢弃（会记进 `_bridge_dropped_parts`），没有做多模态。

### 10.7 M5 真机首跑（2026-09-10 21:00）

用户配好候选跑了，第一发就报：

```
Failed to deserialize the JSON body into the target type:
messages[0].role: unknown variant `developer`,
expected one of `system`, `user`, `assistant`, `tool`, `latest_reminder`
```

两个信息都在这一句里：

- `unknown variant 'developer'` → 就是 §10.1 第 2 条，已修（`developer` → `system`）。
- expected 列表里有 **`latest_reminder`** → 这个上游是较新的严格实现，认我们没见过的
  role 变体。这正是不做「未知 role 一律改成 system」的理由：原样透传对它反而才是对的。

修完 fixture 也改了（不再伪造 `instructions`，改成 `developer` 消息），补了 5 条测试
（role 映射 3 条、抓形状的 `input_item_roles` 2 条）。回归 **212 passed**。

**M5 还没算过**：`apply_patch`、参数跨 chunk 拼接、多轮回放都还没在真机验过。
用 `dev/bridge-smoke-prompt.md` 里那段提示词重跑，那才是 M5 的完成标志。



### 10.8 M5 第二跑：`namespace` 容器（2026-09-10 21:15，已修）

role 修完后真机能跑通了，但模型说完「我先读一下……」就**直接结束**，工具一次都没调。
转发记录给出了线索：

```
id=5857 status=200 converted=1 note='ok (tools=namespace/namespace)'
```

`tools_for_chat` 把不认识的工具类型按 **type 名**记进 dropped —— 也就是说这条请求里有
**两个 `type: "namespace"` 的工具被整体丢掉了**。上游一个工具都没收到，模型自然
只能说句话就收尾。

查 Codex 源码（0.153.x，openai/codex）确认了新线格式：

- `tools/src/tool_spec.rs` `create_tools_json_for_responses_lite`：**所有**普通函数和
  custom 工具都塞进名为 `functions` 的默认 namespace 容器；其它 namespace 原样另发。
  容器形状：`{"type":"namespace","name":...,"description":...,"tools":[function|custom...]}`
- `protocol/src/models.rs:1061` `ResponseItem::FunctionCall` 有可选 **`namespace` 字段**
  —— 客户端靠它把调用路由回非默认命名空间的工具
- `tools/src/code_mode.rs:181` 模型可见名规则：默认命名空间裸名；否则 `{ns}__{name}`，
  ns 以 `_` 结尾或名字以 `_` 开头时直接拼接

**桥接的实现**（`bridge/tools.py`）：

- `tools_for_chat` 展开容器：默认容器（`functions`）裸名，其它容器按模型可见名规则
  限定；容器 `description` 拼进叶子工具描述。容器里的 **custom 工具照转** ——
  litellm 的 `_build_ns_chat_tool` 只认 function、会把嵌套的 custom 丢掉，这里比它全
- `namespace_name_map`（对应 litellm `namespace_tool_name_map` :2003）：模型可见名 →
  (命名空间, 裸名) 的往返表；歧义裸名不猜
- 响应侧（流式 + 非流式）：限定名拆回「裸名 + `namespace` 字段」；默认命名空间
  不带字段（与旧线格式一致，行为不变）
- 请求侧历史回放：`function_call` 带的 `namespace` 字段重新限定成 chat 名

fixture 也改成真实形状（工具包在 `functions` 容器里）。新增 5 条测试，回归 **217 passed**。

**教训**：抓包探针第一版只记 `tools_count`，连容器 existence 都看不见。探针已加厚
（`tools_shape` / `additional_tools_shape`，递归展开、长串截断）—— 下一个形状变化
应该能在第一跑就看出来，而不是靠转发记录里的 dropped 列表反推。

### 10.9 交接状态（2026-09-10 21:40，额度用尽前快照）

**一句话**：桥接本体（M0–M4）全部完成且 217 passed；M5 真机验收进行到第 2/3 关——
两个真 bug（`developer` role、`namespace` 容器）都已修复，**第 3 跑还没人跑过**。

**接手后第一件事**：确认用户已用 `start-gateway.bat` 重启过网关（21:35 之后的修复
只有重启才生效），然后让用户在 Codex 里跑 `dev/bridge-smoke-prompt.md` 那段提示词。

**M5 判定表**（跑完对照）：

| 关卡 | 现象 | 状态 |
|---|---|---|
| 1. role | 上游 422 `unknown variant 'developer'` | ✅ 第 1 跑修掉（§10.7） |
| 2. 工具下发 | 模型说一句话就结束，note 记 `tools=namespace/…` | ✅ 第 2 跑修掉（§10.8） |
| 3. 工具调用 | `shell` 能跑、`apply_patch` 能落盘 | ⬜ 第 3 跑验 |
| 4. 长参数 | 第 5 步 60 行补丁不缺段 | ⬜ 同上 |
| 5. 多轮回放 | 第 6 步之后模型不丢上下文 | ⬜ 同上 |

**改动清单**（全部未提交，工作区是干净起点，`git status` 看一遍即可）：

- 新增 `gateway/bridge/`（errors/tools/request/response/stream）、`tests/test_bridge_*.py` 4 个、
  `tests/fixture/codex_responses_request.json`、`dev/bridge-smoke-prompt.md`
- 改动：`db.py`（schema v6 + 有效协议路由）、`proxy.py`（forward 桥接分支 + 探针加厚）、
  `admin.py` + `web/`（桥接开关）、`README.md`、`protocols.py` 注释
- 真实库已迁 v6（备份在 `data/gateway.db.bak-20260910-195351`），用户配置零改动、零删除

**已知风险 / 未决**：

1. `store: false` 被当作「有副作用」→ Codex 的请求从不自动降级。改法一行
   （`payload.get("store") is True`），但会改现有降级策略，**等用户点头**。
2. 上游是较新的严格实现（认 `latest_reminder` role）。后续再报「不认某字段」的错，
   先怀疑形状差异，贴报错原文对照 Codex 源码（`openai/codex`，codex-rs）。
3. `data/capture.flag` 机制已加厚（`tools_shape`/`additional_tools_shape`）：
   再出形状问题，先放 flag 抓一份再动手。
4. 8317 上可能还有旧进程在跑；冒烟端口 8321 已停。查活：`curl 127.0.0.1:8317/health`。

**验证命令**（venv 在 `.venv/`，别用系统 python）：

```bash
.venv/Scripts/python.exe -m pytest -q -m "not network"   # 期望 217 passed
node tests/web_protocols.test.mjs                        # 期望 ok
```

### 10.10 M5 第三跑：**通过**（2026-09-10 21:45）

Codex 按冒烟提示词跑完了全部 6 步，**桥接的关键路径全部验证通过**：

| 验证点 | 结果 |
|---|---|
| 工具下发（custom→function） | ✅ `apply_patch` / `shell` 都被模型正常调用 |
| `apply_patch` 参数往返（`{"content":...}` 互转） | ✅ 大补丁一次调用提交、落盘正确 |
| 60 行长补丁跨 chunk 拼接 | ✅ 一次 apply_patch，60 行一字不差 |
| UTF-8 / 中文 | ✅ notes.md 中文行原样，sha256 交叉核对一致 |
| 工具输出回传（非零退出码、大输出） | ✅ 步骤 2/6 正常 |
| 多轮历史回放 | ✅ 6 步跨多轮，模型上下文不丢 |

Codex 报告里的问题逐条判读（结论：**没有一个是桥接的**）：

- `apply_patch verification failed: invalid hunk ... not a valid hunk header`
  —— **模型自己的错**：补丁内容行没加 `+` 前缀。模型自查后修正并成功，说明工具
  往返本身没问题。`aborted` 输出是 Codex 自己对 apply_patch 失败的呈现，不是网关截断
  （同一会话里后续的 exec 报错、sha256 输出都完整回传了）。
- `Remove-Item ... rejected: blocked by policy` + 引号乱码 —— **Codex Desktop 的
  exec 策略**拦了删除命令，错误文本是它 Rust 侧的 Debug 格式化。发生在 Codex 进程内，
  与网关无关（工具输出方向是 Codex→网关→上游，这里还没出 Codex 的门）。
- 重复调用 / 顺序调整 —— 模型自己的行为，报告里也如实说了。

**M5 剩余事项**：Codex UI 渲染观感的两件事——①最终返回「感觉没有流式」、②完成后
中间过程没有被折叠。事件类型本身是官方形状（`reasoning_summary_text.delta` +
`output_text.delta`，转发循环确认逐块 yield），所以问题大概率出在**事件序列的细节
与真上游有差**。网关已加一次性抓包（`data/capture-sse.flag`：下一条流式请求的上游
原始 SSE 和发给客户端的转换事件流各存 `data/captured_sse_{upstream,client}.txt`，
各封顶 2MB，抓完即焚），对照跑「同提示词 × 原生候选 / 桥接候选」即可定位差异。

回归 **218 passed**（+1：抓包机制）。

### 10.11 DSML 之谜破案：透传层无罪，规范化补上（2026-09-10 22:45）

**现象**：Codex 走网关接 DeepSeek Responses 上游（第三方的和官方的都试了），模型把
工具调用用 `<｜DSML｜…>` 标记吐在正文里，工具一次都调不出来。

**实验定案**（直连官方 `api.deepseek.com/v1/responses`，**完全不经网关**）：

| 请求形状 | DeepSeek 返回 |
|---|---|
| Codex 的 responses-lite（工具在 `additional_tools` item 里包 namespace 容器） | HTTP 200，但模型把 `shell` 调用用 DSML 标记写在 message 正文里 |
| 同样工具放顶层 `tools` | 规范的 `function_call`，参数干净 |
| 顶层 `type:"custom"` 工具 | **原生支持**：返回规范的 `custom_tool_call`，裸 `input` |

结论：**网关透传层没有破坏任何东西**——它忠实转发了 Codex 的私有线格式，是上游
（官方 DeepSeek 都这样，第三方更不用说）不认 `additional_tools`/namespace 扩展。
用户最初的判断「需要转换」是对的，只是需要的转换比 chat 桥接小得多。

**修复**：新增 `gateway/normalize.py` —— responses-lite → 标准 Responses 的**请求侧
最小规范化**：只把 `additional_tools` 里的**默认 namespace 容器**（`functions`）展开
合并进顶层 `tools`（function/custom 类型原样保留）；item 里剩下的（非默认 namespace、
web_search）原样留在 input（原生扩展上游不受影响、不认的上游本来就会忽略）。
`forward()` 入口处自动触发（`needs_normalization` 有 additional_tools 才动手），
日志加 `lite=+N` 标记；响应侧**零改动**，继续字节级透传。

**端到端验证**：Codex 线格式 → 网关（8322 临时实例）→ 官方 DeepSeek →
返回规范的 `function_call`（`{"command": ["ls", "-la"]}`），全文无 DSML。
回归 **223 passed**（+5：`tests/test_normalize.py`）。

**注意**：重启网关后生效。第三方 DeepSeek Responses 上游按同样原理被治好——
它们和官方一样只差这一个规范化。

### 10.12 流式感与折叠破案：litellm 的 item 宣告策略在 Codex 上是错的（2026-09-10 23:50）

**现象**：桥接流的回复「感觉不是流式」「思考过程不折叠」。

**证据链**（三方互证）：

1. **官方抓包**（本机 `codex-stream-recorder` 记录，5 月 21 日 Codex 直连 GPT-5.5，
   4 个会话）：reasoning item 的 added 形状是
   `{id, type, encrypted_content, summary: []}` —— **summary 是空数组**；每个 item
   （reasoning/web_search/message/工具）都有**独立递增的 output_index**；message item
   必有 `output_item.added` + `content_part.added` 才开始发 `output_text.delta`。
   另：时间节奏上 delta 每 80–200ms 一片，官方是真流式。
2. **moon-bridge 实证**（`\wsl.localhost\Ubuntu-24.04\home\xiaoming\moon-bridge`
   工作区未提交改动 = 当年修好的版本）：同样的发射序列，`summary` 恒为 `[]`，
   reasoning 是 added → part.added → delta×N → text.done → part.done → item.done。
3. **codex-rs 源码**（main，2026-09-10 拉取）：

   - `protocol/src/models.rs`：`ReasoningItem.summary: Vec<ReasoningItemReasoningSummary>`
     —— **无 `#[serde(default)]`、非 Option**，发 `null` 整个 item 反序列化失败；
   - `codex-api/src/sse/responses.rs`：`output_item.added/done` 解析失败只是
     `debug!` + 丢事件；`output_text.delta` 只取 `delta` 字段（**不读
     item_id/output_index**）；`ResponseCompleted` **只解析 id/usage/end_turn**，
     output 数组根本不碰（这解释了为什么 M5 带 content 的 reasoning 快照没炸）；
   - `core/src/session/turn.rs`：`OutputTextDelta` / `ReasoningSummaryDelta` 处理
     **要求存在 active_item**，而 active_item **只能**由解析成功的
     `output_item.added` 建立（`active_item = Some(turn_item)` 只有这一处赋值）；
     无主 delta 走 `error_or_panic` 被丢。

**因果链**（DeepSeek 走桥接，reasoning 先行）：

- reasoning added 带 `summary: null` → item 解析失败被丢 → 无 active_item →
  思考 delta 全丢 → **不折叠**（思考文本只靠 `output_item.done` 一次性出现）；
- litellm 行为「推理先行就不给正文发 added」→ 正文 delta 到达时 active_item 为
  None → **全部静默丢弃** → 正文只在 `output_item.done` 一次性出现 →
  **没流式感**。

**修复**（`gateway/bridge/stream.py`，对照 litellm 第 4 处有意差异）：

1. reasoning `output_item.added` 的 `summary` 恒为 `[]`；
2. 首个 reasoning delta 前补 `response.reasoning_summary_part.added`；
3. message item 在**首个正文增量**时独立宣告 added + part.added（不再「每流一次」）；
4. `output_index` 全局递增（宣告顺序 = 分配顺序），流里 index 与 completed
   快照的 output 数组下标在所有组合下一致（codex 不读它，但严格客户端会）。

顺带确认不用管的两点：`phase` 字段（codex 对 None 按 legacy 兼容处理）；`ContentItem`
的 `annotations`（serde 默认忽略未知字段）。

**回归**：233 passed（stream 层改 4 个旧断言 + proxy 集成改 1 个，新增 codex-compat
断言：summary==[]、双 item 宣告顺序、正文 delta 挂 message item）。
**待真机**：Codex 跑一轮看流式感与折叠是否恢复。

### 10.13 次日清晨双案：杀软 MITM 与 code-mode 的 exec（2026-09-11 09:30）

**案一：502 SSL self-signed certificate（DeepSeek 官方）**

卡巴斯基「加密连接扫描」对 TLS 做 MITM，根证书 `Kaspersky Anti-Virus Personal Root
Certificate` 装在 Windows 系统库里 —— curl/浏览器（走系统库）能通，httpx（自带 CA
捆绑包）不认 → 网关 502。与 TUN、Codex 无关。

**修复**：`upstream.system_ssl_context()` 用 `truststore` 建系统证书库 context，
`proxy.client_args()` 统一挂 `verify`（代理出口同样生效；truststore 未装则原样）。
requirements.txt 已加。**重启网关生效**。

**案二：400 "Unsupported custom tool: 'exec'. Only 'apply_patch' is supported"（站A）**

源码实锤（codex-rs 0.153.4）：

- `code-mode-protocol/src/lib.rs:51`：`pub const PUBLIC_TOOL_NAME: &str = "exec"` ——
  code mode 开启时，Codex 暴露一个叫 `exec` 的 **Freeform（custom）工具**作为委托入口；
- 模型目录 `tool_mode: "code_mode_only"` 是触发器（`protocol::openai_models::ToolMode`，
  core 里 `effective_tool_mode(model_info)` 消费）；
- gpt-5.6-terra 的请求带了它（站A 拒收），gpt-5.6-sol 的没带 —— 网关路由表
  两者完全同构，差异只能来自 Codex 侧的**运行时模型目录**（ChatGPT auth 会从后端
  拉活目录并按 ETag 刷新，与 0.153.4 内置的 models.json 未必一致，per-model 开关
  在灰度）。内置目录里 sol/terra 都标了 code_mode_only，但运行时以活目录为准。

**§10.12 补遗 —— developer-role 之谜彻底闭环**（client.rs:936-955）：

- `use_responses_lite=true`（gpt-5.6-* 等 catalog 模型）：顶层无 instructions、无
  tools；系统提示词 = `role:"developer"` 的 message item；工具装进 `AdditionalTools`
  namespace 容器；`parallel_tool_calls` 强制关。
- `use_responses_lite=false`：标准 Responses —— 顶层 `instructions` + 顶层 `tools`。

**未知 slug 走 fallback**（models-manager/manager.rs `construct_model_info_from_candidates`）：
最长前缀匹配不到 → `model_info_from_slug`：`use_responses_lite: false`、
`shell_type: unified_exec`（→ `exec_command`/`write_stdin` **函数**工具）、
`apply_patch_tool_type: None`（无 apply_patch 工具，模型经 shell 用 apply_patch）、
`tool_mode: None`（**无 custom exec**）。

**结论**：Codex 客户端直接配 DeepSeek 模型 id（如 `deepseek-v4-flash`）→ 自动回退
兼容线格式 → 顶层 function 工具、无 namespace、无 developer、无 exec → 任何兼容
Responses 的上游**零改动透传**即可用。这验证了当时的设想（2026-09-11）。

### 10.14 退役（2026-09-11 10:15）

真机冒烟确认：Codex 配第三方模型 slug（deepseek-v4-flash）走 fallback 兼容格式，
纯透传 6 步全过 —— 桥接与 normalize 失去触发路径，从工作区移除（commit c676ab0）。

完整代码永久可回捞：
- 桥接层 + 全部集成：`git checkout c3e9c6f -- gateway/bridge/`（或整库看该提交）
- normalize：b0784dc
- 桥接的流式/折叠修复（stream.py）：c3e9c6f 里的版本已含

若哪天要再接「gpt-5.6-* 模型名 + 仅 Chat 上游」的组合，从 c3e9c6f 捞回即可。
