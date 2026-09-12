# Alpha Search 换后端调查：DeepSeek Anthropic / Tavily（2026-09-12）

只做调查与实测，未改网关配置、未改任何代码。

起因：站B 的 `/v1/alpha/search` 不稳（09-10 10:51 那次挂了 344s 才被客户端断开），
问能不能换成 DeepSeek 官方的 Anthropic 端点做搜索，或者用 Tavily 做兼容层。

## 0. 结论摘要

| 问题 | 答案 |
| --- | --- |
| DeepSeek 官方有 `/v1/alpha/search` 吗 | **没有**，404 |
| DeepSeek 官方 Responses 支持 `web_search` 吗 | **不支持**，官方文档写明「忽略」 |
| DeepSeek 官方能搜索吗 | **能，但只在 Anthropic 端点**（`web_search_20250305` / `web_search_20260209`） |
| 官方搜索免费吗 | **不免费**。无独立条目，但结果按 input token 计费，实测一次 45,738 input tokens |
| 换成 DeepSeek 要多少转换 | 见第 4 节，约 **750~850 行**（含配置/前端/测试） |
| 最大障碍 | `encrypted_output` 造不出来（第 3 节）；`results[].snippet` 填不出来 |
| 当前还值得做吗 | **先别做**。见第 6 节：`/alpha/search` 已两天没被调用 |

## 1. DeepSeek 官方实测（用库里 DeepSeek 分组的 key）

### 1.1 基础

```
GET  /v1/models              -> deepseek-flash, deepseek-v4-pro
POST /v1/alpha/search        -> 404
```

注意模型名：`deepseek-v4-flash` 已下线，现在叫 **`deepseek-flash`**（旧名仍可调，按 Flash 计费）。

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

### 1.4 usage（这是"免不免费"的答案）

```json
{"input_tokens": 45738, "cache_creation_input_tokens": 0, "cache_read_input_tokens": 512,
 "output_tokens": 1160, "service_tier": "standard",
 "server_tool_use": {"web_search_requests": 2}}
```

- 有独立的 `server_tool_use.web_search_requests` 计数，但**定价页没有搜索这一项**。
- 搜索结果正文会被拼进上下文算 input token：一次搜索（2 个 query × 10 条）吃掉 **4.5 万 input**。
- 按 `deepseek-flash` 折算：空闲时段 ≈ **0.05 元/次**，高峰时段 ≈ **0.10 元/次**。
  便宜，但**不是免费**。（用 v4-pro 会贵 4.5~9 倍，别用。）

## 2. 站B `/v1/alpha/search` 的真实响应形状

探针（`gpt-5.6-luna`）：200，7.48s，75,879 B。顶层只有三个字段：

| 字段 | 值 |
| --- | --- |
| `output` | 字符串，**25,532 字符** —— 模型写的研究综述，内含 `citeturn30search0` 引用标记 |
| `results` | 数组，**33 项**，`{type:"text_result", domain, ref_id, snippet, title, url}`，`ref_id` 形如 `turn30search0` |
| `encrypted_output` | 字符串，**35,108 字符**，Fernet 令牌（`gAAAAAB` 前缀） |

请求侧形状（与 09-10 那份一致）：

```json
{"id":"...","model":"...","input":"...",
 "commands":{"search_query":[{"q":"..."}]},
 "settings":{"external_web_access":"live"}}
```

**关键点：`output` 不是搜索结果列表，是服务端搜索 agent 写的带引用综述。**
`results` 里的 `ref_id` 和 `output` 里的 `cite...` 一一对应，Codex 拿它渲染引用角标。

## 3. `encrypted_output` 是什么（能不能造）

解 base64 后 26,329 字节，version byte `0x80`，符合 Fernet 结构
（`1 + 8(ts) + 16(IV) + ciphertext + 32(HMAC)`）。

对 `output`（UTF-8 25,783 字节）做同样计算：

```
预测 Fernet 长度 = 73 + 25783 + 9 = 25,865 字节
实际                = 26,329 字节
差                  = 464 字节
```

→ **明文 ≈ `output` 加约 460 字节的信封（大概是 turn / search id 之类的元数据）。**
它**不额外携带搜索正文**（33 篇全文绝不可能压进 26 KB）。

推论：

- 语义上**省略它不丢内容** —— `output` 明文已经把实质给了客户端。
- 但它是**服务端对称密钥**签出来的，我们造不出来。用途是下一轮 Codex 把它原样回传，
  服务端能解密并**验证这段搜索结果确实是服务端签发的**（防客户端伪造搜索内容）。
- 所以省略它 = 下一轮丢失「可验证的服务端搜索上下文」，但**不影响本轮回答**。
- **Codex 到底是不是强依赖它（反序列化非 Option 就报错）—— 没验，这是唯一的未知项。**
  仓库里 `tests/helpers.py:226` 的 mock 给的是 `encrypted_output: None`，说明当初默认可以为空。

## 4. 换成 DeepSeek Anthropic 的转换规格与工作量

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

### 4.3 工作量

| 模块 | 行数 | 说明 |
| --- | --- | --- |
| `gateway/alpha_search.py`（新） | ~250 | 请求映射、响应映射、错误映射 |
| `proxy.py` 分支 | ~60 | `/alpha/search` 命中「转换型后端」时改走转换而非透传；挂 inflight/记录/断路器 |
| `db.py` + `admin.py` | ~150 | 现在搜索目标只有 `group_id`，要加后端类型 |
| `web/` 前端 | ~80 | 搜索目标卡片加后端选择 |
| `protocols.py` | ~40 | deepseek-anthropic 描述符（base_url/path/auth 头/usage 解析） |
| 测试 | ~200 | 照现有 4 个 search 用例的写法 |
| **合计** | **~780** | **约 1.5~2 个工作日** |

## 5. Tavily 路线

- Tavily 只给搜索结果（title/url/content/score），**不给综述**。要凑出 站B 那种
  25 KB 带引用综述，得**再调一次 LLM 写** —— 两段式，多一轮延迟和成本。
- 好处：结果**可读**（有 content），`results[].snippet` 能填上，引用能对齐。
  这正是 DeepSeek 路线填不出来的那一格。
- 坏处：多一个账号、多一次 LLM 调用、免费额度 1000 次/月。
- `<参考目录>\moon-bridge-cf\internal\extension\websearch\` 里有现成实现
  （`tavily.go` 74 行客户端 + `orchestrator.go`）。**但方向不对**：它的 Orchestrator 是
  包住 **Anthropic client**、拦截模型吐出的 `web_search` tool_use 后本地执行，服务的是
  Claude Code 那条链路；它不接 `/alpha/search`。能抄的只有那 74 行 HTTP 客户端。

## 6. 现状：`/alpha/search` 已经两天没人调用了

按天统计（`data/gateway.log`）：

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

两点：

1. 用户已经把 Codex 切到非目录模型的 slug（`deepseek-v4.1-flash`），走的是 fallback
   线格式；`/alpha/search` 只在 Codex 判定要联网时才打，**不是死了，是休眠**。
   一旦需要搜索它还会回来，而且仍然打到 站B（目标仍是 `gpt-5.6-luna`）。
2. **`web_search` 现在被塞在 `/v1/responses` 的 tools 里发给上游，而 DeepSeek 官方
   Responses 明确「忽略」内置工具**（官方文档 Tools 表 + 09-10 §2 实测都是这个结论）。
   站A 转的就是 DeepSeek —— 也就是说**这条路上的搜索现在是静默失效的**，
   模型根本不会产出 `web_search_call`（全日志 0 次）。

## 7. 建议

按性价比排序：

1. **先别做 DeepSeek 转换。** 最大收益（换掉不稳定的 站B）被 `encrypted_output`
   卡住，而这个函数两天才用几十次、一次才几十毫秒到几秒。780 行换这个不划算。
2. **先做便宜的（~80 行，2 小时）**：
   - `/alpha/search` 加**单请求超时**（现在能挂 344s，客户端自己断才结束）；
   - 支持**多个搜索候选**而不只是单个 `standalone_search_target_group_id`；
   - 超时/失败时**明确的错误响应**而不是空等。
   这直接治「不稳」，且不影响现有透传语义。
3. **真正值得投入的是第 6 节第 2 点**：`/v1/responses` 里的 `web_search` 被上游忽略，
   这可是每一轮都在发生的。要治它只有两条路 —— 换一个真做服务端搜索的 Responses
   上游，或者网关自己把 `web_search` 拦下来代执行（moon-bridge Orchestrator 那个模式，
   但要做成 Responses 版）。这是另一个量级的工程，得先拍板要不要。
4. 如果哪天真要动 DeepSeek 转换，**先花 30 分钟验一件事**：起一个本地 mock 只返回
   `output` + `results`、**不带** `encrypted_output`，把它设为搜索目标，看 Codex 认不认。
   认 → 方案成立；不认 → 直接放弃这条路线。

## 8. 后记（同日下午）：这条路已经不走了

用户补充了关键信息并做了决定：

- **不稳定的真因是上游账号 401**，不是网络/并发。站B 部署在自建的上游服务上，
  号被封/掉线时模型请求能路由到其它号，**但 `/alpha/search` 的 401 是直接透传回来的**，
  得手动切号重认证。→ 换搜索后端（含 DeepSeek 转换）解决不了这一类问题，
  DeepSeek key 一样会 401 / 余额耗尽。**转换方案的成本收益比因此进一步下降，已搁置。**
- **改走 Tavily MCP**：免费档 1000 credits/月，够用；网关侧零成本、零改动。
  Tavily MCP 其实早就配好了 —— dsh 在 `~/.dsh/cordis.patch.yml:92`，Codex 在
  `~/.codex/config.toml` 的 `[mcp_servers.tavily]`。
- **已关闭 Codex 的 standalone search**：`~/.codex/config.toml` 的
  `[model_providers.custom] supports_standalone_web_search` 由 `true` 改为 `false`。
  这是 Codex 侧对「这个 provider 支持 /alpha/search」的能力声明（对应 README 第 224 行
  那句「是上游侧的能力声明，不是本地网关开关」）。改完 Codex 不再打 `/alpha/search`，
  需要重启 Codex 生效。

**网关侧：本轮零改动。** 129 passed 的基线也确认过了。

预期后续现象（下次用 Codex 时可对照）：`/alpha/search` 请求归零；Codex 可能改为把
`web_search` 塞进 `/v1/responses` 的 tools —— 那条路仍然是被上游忽略的（见第 6 节第 2 点），
所以表现为「Codex 不会自动联网，但可以让它用 Tavily MCP 搜」。

## 附：本次探测的用法（可复现）

```bash
# 从库里取 key（只读）
python -c "import sqlite3;c=sqlite3.connect('file:data/gateway.db?mode=ro',uri=True);\
print(c.execute('select api_key from upstream_groups where id=18').fetchone()[0])"

curl -X POST https://api.deepseek.com/anthropic/v1/messages \
  -H "x-api-key: $KEY" -H "anthropic-version: 2023-06-01" -H "Content-Type: application/json" \
  --data-binary @req.json     # 中文必须用文件传，Windows 下 -d 内联会破坏 UTF-8
```
