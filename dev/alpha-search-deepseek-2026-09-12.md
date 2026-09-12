# 给独立搜索换服务的调查（2026-09-12，方案未采用）

## 先看最后的决定

**当时决定不在网关增加搜索转换，改用客户端已有的 Tavily MCP 工具。** MCP 在这里就是客户端直接调用搜索服务的工具连接，不需要网关帮它改格式。

原因是：DeepSeek 虽然当时能通过另一种接口搜索，但不能直接替换 Codex 的 `/v1/alpha/search`；搜索结果格式和加密状态的兼容性没有验证完整，继续做转换不划算。
后续用户还补充了上游账号 401 的情况，最终决定见第 8 节。

> 下面保留当日实测、估算和讨论，**不是正在实施的计划**。站点状态、价格、免费额度和客户端行为都没有在本次文档整理中重新验证。
> 第 8 节记录了当时的客户端配置改动，不代表今天仍是该配置。网关当前规则见[维护说明](maintenance.md#搜索与压缩)。

本次原调查未改网关配置或代码；文末另记了客户端侧的调整。

起因：站B 的 `/v1/alpha/search` 不稳（09-10 10:51 那次挂了 344s 才被客户端断开），
问能不能换成 DeepSeek 官方的 Anthropic 端点做搜索，或者用 Tavily 做兼容层。

## 0. 结论摘要

| 问题 | 答案 |
| --- | --- |
| DeepSeek 官方有 `/v1/alpha/search` 吗 | **没有**，404 |
| DeepSeek 官方 Responses 支持 `web_search` 吗 | **不支持**，官方文档写明「忽略」 |
| DeepSeek 官方能搜索吗 | **能，但只在 Anthropic 端点**（`web_search_20250305` / `web_search_20260209`） |
| 官方搜索免费吗 | 不能按免费处理。当次返回 45,738 input tokens；费用估算见第 1.4 节，不是账单核验 |
| 换成 DeepSeek 要多少转换 | 第 4 节有当时的粗估，尚未实现，不是工期承诺 |
| 最大障碍 | 无法生成可被原服务认可的 `encrypted_output`；样本里也没有可直接填入 `snippet` 的正文摘要 |
| 最终有没有做 | **没有。** 第 8 节记录了改用 Tavily MCP 的决定 |

## 1. DeepSeek 官方实测（用库里 DeepSeek 分组的 key）

### 1.1 基础

```
GET  /v1/models              -> deepseek-flash, deepseek-v4-pro
POST /v1/alpha/search        -> 404
```

当时模型列表列出的是 **`deepseek-flash`**；原记录说旧名 `deepseek-v4-flash` 仍可调用。
列表没列旧名不等于旧名已彻底下线，当前名称和计费方式需要重新确认。

### 1.2 Anthropic 端点的搜索工具名

`POST https://api.deepseek.com/anthropic/v1/messages`

| tools[0].type | 结果 |
| --- | --- |
| `web_search` | **400**，`unknown variant \`web_search\`, expected \`web_search_20250305\` or \`web_search_20260209\`` |
| `web_search_20250305` | **200**，8.13s，234,706 B |
| `web_search_20260209` | **200**，8.93s，194,098 B |

头：`x-api-key` + `anthropic-version: 2023-06-01`（`anthropic-beta` 在 `/messages` 上被忽略）。

### 1.3 响应块结构（`web_search_20250305`，max_tokens=1024）

```
content:
  [0] thinking              thinking=200字, signature
  [1] text                  82字（"I'll look up the latest documentation..."）
  [2] server_tool_use       id=call_00_*, name=web_search, input={query}, caller
  [3] server_tool_use       id=call_01_*, name=web_search, input={query}, caller
  [4] web_search_tool_result tool_use_id=call_00_*  content[10]
  [5] web_search_tool_result tool_use_id=call_01_*  content[10]
  [6] thinking              2747字
  [7] text                  1539字（最终综述）
stop_reason: max_tokens（被 1024 截断，不是自然结束）
```

`web_search_tool_result.content[]` 的元素：

```json
{"type":"web_search_result","title":"...","url":"...","encrypted_content":"...","page_age":"..."}
```

**没有 snippet / 文本摘要** —— 可读内容只有 title/url，正文是 DeepSeek 自己的密文，
我们解不开。所以搜索到的正文只能靠模型写在 `text` 里的综述间接拿到。

### 1.4 返回的用量和当时的费用估算

```json
{"input_tokens": 45738, "cache_creation_input_tokens": 0, "cache_read_input_tokens": 512,
 "output_tokens": 1160, "service_tier": "standard",
 "server_tool_use": {"web_search_requests": 2}}
```

- 返回中有 `server_tool_use.web_search_requests` 计数；原调查没有在当时定价页找到独立搜索条目。
- 这一次请求包含 2 次查询，每次 10 条结果，上游报告约 **4.5 万 input tokens**。这不是每次搜索固定的用量。
- 原记录按当时 `deepseek-flash` 价格估算：空闲时段约 **0.05 元/次**，高峰时段约 **0.10 元/次**；并估计 v4-pro 更贵。
  这些没有在本次整理中复核，也没有对照实际账单，不能当作当前报价或免费承诺。

## 2. 当时站B的搜索接口返回了什么

探针（`gpt-5.6-luna`）：200，7.48s，75,879 B。顶层只有三个字段：

| 字段 | 值 |
| --- | --- |
| `output` | 字符串，**25,532 字符** —— 模型写的研究综述，内含 `citeturn30search0` 引用标记 |
| `results` | 数组，**33 项**，`{type:"text_result", domain, ref_id, snippet, title, url}`，`ref_id` 形如 `turn30search0` |
| `encrypted_output` | 字符串，**35,108 字符**，`gAAAAAB` 前缀，外观与 Fernet 令牌结构相符；未解密 |

请求侧形状（与 09-10 那份一致）：

```json
{"id":"...","model":"...","input":"...",
 "commands":{"search_query":[{"q":"..."}]},
 "settings":{"external_web_access":"live"}}
```

**关键点：`output` 是带引用的综述，不是搜索结果列表。**
`results` 里的 `ref_id` 和 `output` 里的 `cite...` 一一对应，Codex 拿它渲染引用角标。

## 3. 加密字段：看到了什么，还不知道什么

解 base64 后 26,329 字节，version byte `0x80`，符合 Fernet 结构
（`1 + 8(ts) + 16(IV) + ciphertext + 32(HMAC)`）。

对 `output`（UTF-8 25,783 字节）做同样计算：

```
预测 Fernet 长度 = 73 + 25783 + 9 = 25,865 字节
实际                = 26,329 字节
差                  = 464 字节
```

上面只能说明长度接近，**不能据此知道加密前是什么，也不能证明它没有携带额外内容**。
原文把“可能是综述加元数据”进一步写成“省略不丢内容、不影响本轮回答”，证据不足，不能沿用为结论。

当时能明确区分的是：

- 没有对应服务的密钥和实现，不能自己生成一个等价、能被该服务认可的加密字段。
- 客户端是否允许省略、下一轮会怎样使用、是否影响引用或继续搜索，都没有完整验证。
- 测试模拟响应里使用 `encrypted_output: None`，只能说明那个测试接受这种输入，不能证明真实 Codex 或上游接受。
- 所以“可不可以不带它”是待验证项，不是已经成立的转换方案。

## 4. 当时设想的转换方式（未实现）

### 4.1 请求侧 `/alpha/search` → Anthropic messages

```
commands.search_query[].q   ->  拼进 user 消息（可能多组）
input                       ->  user 消息正文
settings.external_web_access->  丢弃
                                system: "写一份带来源的调研综述…"
model                       ->  deepseek-flash（固定，不跟 Codex 的模型走）
max_tokens                  ->  4096
tools                       ->  [{"type":"web_search_20250305","name":"web_search","max_uses":5}]
```
注意：`top_p` 非思考模式恒 1.0、`temperature` 别设、`thinking` 关不掉（只能事后丢掉 thinking 块）。

### 4.2 响应侧 Anthropic → `/alpha/search`

```
output   <-  最后一个 text 块（或全部 text 块拼接）
results  <-  flatten(web_search_tool_result[].content[])
             -> {type:"text_result", title, url, domain: host,
                 ref_id: "turn0search{i}", snippet: ""}   # snippet 拿不到，只能空
encrypted_output <- null                                   # 造不出来
```
引用标记 `citeturn0search0`（U+E200 私有区）要不要往 `output` 里插，是可选项：
插得对 Codex 才有角标，插错了更糟，v1 建议**不插**。

### 4.3 当时粗估的工作量

以下保留原估算供回顾。没有完整解决加密字段、引用和多轮兼容问题之前，代码行数不能代表实际工作量，时间也不是承诺。

| 模块 | 行数 | 说明 |
| --- | --- | --- |
| `gateway/alpha_search.py`（新） | ~250 | 请求映射、响应映射、错误映射 |
| `proxy.py` 分支 | ~60 | `/alpha/search` 命中「转换型后端」时改走转换而非透传；挂 inflight/记录/断路器 |
| `db.py` + `admin.py` | ~150 | 现在搜索目标只有 `group_id`，要加后端类型 |
| `web/` 前端 | ~80 | 搜索目标卡片加后端选择 |
| `protocols.py` | ~40 | deepseek-anthropic 描述符（base_url/path/auth 头/usage 解析） |
| 测试 | ~200 | 照现有 4 个 search 用例的写法 |
| **合计** | **~780** | **约 1.5~2 个工作日** |

## 5. 如果改成 Tavily，需要做什么

- Tavily 只给搜索结果（title/url/content/score），**不给综述**。要凑出 站B 那种
  25 KB 带引用综述，得**再调一次 LLM 写** —— 两段式，多一轮延迟和成本。
- 好处：结果**可读**（有 content），`results[].snippet` 能填上，引用能对齐。
  这正是 DeepSeek 路线填不出来的那一格。
- 代价：多一个账号；如果仍要伪装成同一搜索接口，还需额外调用模型写综述。原记录提到的免费额度见后记，以服务当前条款为准，不把 credits 等同于固定请求次数。
- `<参考目录>\moon-bridge-cf\internal\extension\websearch\` 里有现成实现
  （`tavily.go` 74 行客户端 + `orchestrator.go`）。**但方向不对**：它的 Orchestrator 是
  包住 **Anthropic client**、拦截模型吐出的 `web_search` tool_use 后本地执行，服务的是
  Claude Code 那条链路；它不接 `/alpha/search`。能抄的只有那 74 行 HTTP 客户端。

## 6. 当时可见日志里，没有最近两天的独立搜索请求

当时对可见的 `data/gateway.log` 做了统计（不是完整历史或全天审计）：

```
09-09   18 次
09-10   45 次
09-11    0 次
09-12    0 次
```

今天（09-12 12:48）的实际流量是：

```
POST /v1/responses  model='deepseek-v4.1-flash'  upstream=站A
                    remote='deepseek-v4-flash'   -> 200  stream=True
                    req=556KB  input=163810  cached=163328
                    tools=16[function×10, custom, namespace×2, tool_search, web_search]
```

这份样本说明，请求里有 `web_search` 工具定义，但**有定义不等于搜索执行过**。

原调查中，DeepSeek 官方 Responses 的测试没有执行这个内置搜索工具。对于第三方站A，还不能仅凭模型名称推断它的全部行为。
当时没有找到搜索调用事件，但也不能由此断言所有请求都不会搜索。

同样，某段日志里没有 `/alpha/search`，不能保证“以后需要时一定会自动恢复”。是否发请求取决于客户端版本、模型配置和工具选择；后记中还记录了随后关闭独立搜索声明的操作。

## 7. 最终决定前讨论过的建议（不是待办）

按性价比排序：

1. **先不做 DeepSeek 转换。** `encrypted_output` 等兼容性尚未验证，不能只按预估代码量决定投入。
2. 当时讨论过改善等待和错误提示：
    - 给 `/alpha/search` 设置等待上限（当时有等待 344s 的记录）；
   - 支持**多个搜索候选**而不只是单个 `standalone_search_target_group_id`；
   - 超时/失败时**明确的错误响应**而不是空等。
    这类改动只能限制等待或提供备用，不保证解决账号问题。当前代码已有统一的“发呆超时”，不能把这里的旧建议当成现版本完全没有超时；详见[维护说明](maintenance.md#等待时间)。
3. 先确认实际使用的 Responses 上游是否执行内置搜索。若不能，可换支持的上游，或直接使用客户端搜索工具；不必首先在网关重做一套搜索。
4. 如果以后仍想研究转换，应先用本地模拟服务验证：只返回
   `output` + `results`、**不带** `encrypted_output`，把它设为搜索目标，看 Codex 认不认。
    客户端接受只能证明这一项检查通过，引用、多轮请求和失败处理仍要验证，不能直接宣布整个方案成立。

## 8. 同日下午的最终决定：不改网关，改用搜索工具

用户补充了关键信息并做了决定：

- 用户补充：主要遇到的是上游账号 401；普通模型请求能换账号，但该上游的独立搜索路径直接返回 401，需要手动切号或重新认证。
  这是用户补充的具体情况，不足以排除所有网络或等待问题。更换搜索后端可能绕过当时的账号问题，但也要付出兼容和维护成本，因此当时决定搁置转换。
- **改用 Tavily MCP**：原记录称当时免费档为 1000 credits/月，用户判断够用；网关无需因此改代码。额度和计费没有在本次整理中复核。
  Tavily MCP 其实早就配好了 —— dsh 在 `~/.dsh/cordis.patch.yml:92`，Codex 在
  `~/.codex/config.toml` 的 `[mcp_servers.tavily]`。
- **已关闭 Codex 的 standalone search**：`~/.codex/config.toml` 的
  `[model_providers.custom] supports_standalone_web_search` 由 `true` 改为 `false`。
  这是客户端对 provider 能力的声明，不是网关开关，见[搜索与压缩说明](maintenance.md#搜索与压缩)。
  当时预期重启客户端后不再发送独立搜索请求；本记录没有补充重启后的完整复测。

**网关侧：当次调查没有改代码或配置。** 原记录另报告过 129 passed；这是当时的测试数字。

当时的使用建议是：需要联网时明确让客户端使用 Tavily MCP，并确认它真的返回了搜索结果。
后续是否还发送其他形式的搜索请求，要看实际客户端记录，不能只凭这一项配置推断。

## 附：以后要复测时

先确认确有需要、允许向外部服务发送什么内容，以及愿意承担的调用费用。不要为了检查文档顺便复测。

- 模型列表使用 `GET /v1/models`；当时搜索测试使用 `POST /anthropic/v1/messages`。
- 请求参数见第 1 节，测试文本使用不含秘密的固定内容，并限制输出和调用次数。
- API Key 只用于鉴权，不要像旧示例那样从数据库读出后直接打印到终端，也不要照抄历史组号。
- PowerShell 下含中文的 JSON 可先保存成 UTF-8 文件再发送，避免多层命令行转义。不要把真实请求正文或密钥写回这份文档。
