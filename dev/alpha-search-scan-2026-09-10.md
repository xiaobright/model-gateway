# Alpha Search 扫描与 DeepSeek 搜索能力记录

日期：2026-09-10。只做调查，未改动网关配置、未改动任何代码。

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

**结论：只有 站B / gpt-5.6-luna 稳定返回 200，没有第二个可降级候选。**
维持搜索目标 `standalone_search_target_group_id=7` + `standalone_search_target_model=gpt-5.6-luna`。

补充：站B 的搜索也不是永远稳。日志里最后一次 200 是 09-10 09:04，10:51 那次挂了
344s 后客户端断开（499）。频率升高时再考虑独立 search sidecar。

## 2. DeepSeek 官方 API 的搜索能力

- `POST https://api.deepseek.com/v1/alpha/search` → **404**，没有这个端点。
- `POST /v1/responses` 带 `web_search` 工具（试过 `web_search`、`web_search_20250305`、
  `web_search_preview`）→ 200 但工具被**静默忽略**，output 只有 `reasoning` + `message`，
  无 `web_search_call`。官方文档 Responses API 的 Tools 表明确写 `web_search` 为 Ignored。
- `POST /v1/models` → `deepseek-flash`、`deepseek-v4-pro`。
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

## 3. 当前决定

保留现状：搜索继续用 站B / gpt-5.6-luna，DeepSeek 只作为普通模型上游。不建 sidecar，
不做格式转换。

## 附：同日远程压缩调查结论

- 09-09～09-10 共 16 次 Codex `compacted` 事件（含 00:17:24、16:34:44、17:18:05 手动压缩），
  全部是**客户端模型总结**路径：可读摘要 + 纯 `message` 的 replacement_history，
  零 `encrypted_content`、零 `cmp_*` 项。
- 网关从未收到 `/v1/responses/compact` 请求（只有 09-09 的探测，上游全 404），
  compaction 探针也始终 `found=false`。**远程 compact 从未真正发生。**
- 手动压缩后下一轮请求（17:22:19）已确认**复用了压缩结果**：43 条 message、无 reasoning 项，
  携带压缩生成的 37 条 replacement_history + 摘要；「测试连接」得到正常回复。
