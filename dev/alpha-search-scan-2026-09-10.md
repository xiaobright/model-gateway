# 哪些站点能接独立搜索（2026-09-10 的测试记录）

日期：2026-09-10。只做调查，未改动网关配置、未改动任何代码。

## 先看结论和后续决定

当次扫描中，只有站B的一组请求成功返回搜索结果。DeepSeek 当时能通过另一种接口搜索，但不能直接替换这个搜索接口。
**这不是站点当前可用性的保证，一次返回 200 也不等于长期稳定。**

当时选择保留原搜索目标。9 月 12 日后续又决定不在网关做搜索转换，改用客户端的 Tavily MCP 工具，见[后续记录](alpha-search-deepseek-2026-09-12.md)。
下文保留测试结果，组号、模型名和本机配置都只指当时。

## 1. Alpha Search 扫描（启用的 OpenAI 分组）

方法：只读直连。读 `data/gateway.db` 里启用且上游启用的 OpenAI 分组，按
`base_url` / `api_key` / `header_override` / `egress` 直接 `POST {base}/v1/alpha/search`，
请求体是脱敏固定探针。不经过网关、不写库。

| 分组 | 上游 | 探测模型 | 结果 |
| --- | --- | --- | --- |
| g7 | **站B** | gpt-5.6-luna | **200**（65KB，含 `encrypted_output` / `output` / `results`） |
| g1 | 站C | gpt-5.6-sol | 404 Invalid URL |
| g3 | 站A | gpt-5.6-sol | 404 Invalid URL |
| g15 | 站I | gpt-6-astra | 404 Invalid URL |
| g18 | deepseek | deepseek-flash | 404（空体） |
| g2 / g8 | 站D | gpt-5.6-sol / luna | 500 Request processing failed |
| g4 | 站E | gpt-5.6-luna | 503 Service temporarily unavailable |
| g5 | 站F | gpt-5.6-sol | 500 `channel does not support /v1/alpha/search` |
| g17 | 站H | gpt-5.6-sol | 500 `channel does not support /v1/alpha/search` |
| g6 | 站G | gpt-5.6-sol | 500 `分组 codex 下模型…可用渠道不存在` |

**这轮结果：只有站B / gpt-5.6-luna 返回了 200，没有找到第二个成功候选。**
当时维持搜索目标 `standalone_search_target_group_id=7` + `standalone_search_target_model=gpt-5.6-luna`，不要把组号 `7` 直接抄到其他配置里。

补充：站B 的搜索也不是永远稳。日志里最后一次 200 是 09-10 09:04，10:51 那次挂了
344s 后客户端断开（499）。当时讨论过另建一个独立搜索服务，但没有采用；现在的超时规则见[维护说明](maintenance.md#等待时间)。

## 2. DeepSeek 官方 API 的搜索能力

- `POST https://api.deepseek.com/v1/alpha/search` → **404**，没有这个端点。
- `POST /v1/responses` 带 `web_search` 工具（试过 `web_search`、`web_search_20250305`、
  `web_search_preview`）→ 200 但工具被**静默忽略**，output 只有 `reasoning` + `message`，
  无 `web_search_call`。官方文档 Responses API 的 Tools 表明确写 `web_search` 为 Ignored。
- 当时取得的模型列表包含 `deepseek-flash`、`deepseek-v4-pro`。
- **Anthropic 兼容端点支持搜索**：
  `POST https://api.deepseek.com/anthropic/v1/messages`
  + `tools:[{"type":"web_search_20250305","name":"web_search"}]` → 200，返回
  `server_tool_use` + `web_search_tool_result` 块，搜索真实执行。支持
  `web_search_20250305` / `web_search_20260209`（传裸 `web_search` 会 400）。

**结论：DeepSeek 官方 API 有搜索能力，但只在 Anthropic 格式端点上；它没有 OpenAI 的
`/v1/alpha/search`，也不执行 Responses 的 `web_search` 工具，所以不能直接给 Codex 的
Alpha Search 当上游。** 要让 Codex 用上需要新增一层 alpha/search ↔ Anthropic messages
的格式转换，且 站B 的 `encrypted_output` 不透明状态 DeepSeek 产不出来，兼容性未知，
超出当前「不做协议转换」的范围。

可选但未采用：把 DeepSeek 配成 anthropic 协议上游，供 Claude Code 用服务端搜索
（网关已有 `/v1/messages` 透传，理论上现成）。

## 3. 当天的决定（后续已更新）

当时保留现状：搜索继续用站B / gpt-5.6-luna，DeepSeek 只作为普通模型上游。不另建搜索服务，
不做格式转换。

## 附：同日远程压缩调查结论

- 09-09～09-10 共 16 次 Codex `compacted` 事件（含 00:17:24、16:34:44、17:18:05 手动压缩），
  全部是**客户端模型总结**路径：可读摘要 + 纯 `message` 的 replacement_history，
  零 `encrypted_content`、零 `cmp_*` 项。
- 在当时检查的记录中，没有找到真正的 `/v1/responses/compact` 压缩调用（只有 09-09 的探测，上游全 404），
  compaction 探针也是 `found=false`。**这些样本没有显示上游执行过独立远程压缩。**
- 手动压缩后下一轮请求（17:22:19）已确认**复用了压缩结果**：43 条 message、无 reasoning 项，
  携带压缩生成的 37 条 replacement_history + 摘要；「测试连接」得到正常回复。
