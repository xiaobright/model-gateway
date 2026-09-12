# Codex 的模型名为什么会影响请求格式（版本相关记录）

**先说结论：**当时发现，同一个上游，客户端里使用某些模型名就会换一种请求格式，导致工具不兼容。
给网关模型起一个不匹配内置目录的别名，曾让那一版 Codex 使用更通用的格式，避免继续维护网关转换代码。

> 日期：2026-09-11。依据当时的 openai/codex main 源码（`%TEMP%\codex-src`）和 0.153.4 实测。
> **不是当前所有 Codex 版本的配置保证。** 本次整理没有重新检查客户端源码、配置或运行行为。
> 改名还可能改变客户端选择的工具模式、上下文等默认值，不能只验证聊天一句话成功就认为所有功能相同。

## 结论摘要

**当时没有查到配置里的一键开关。** 查看过的路径由 `ModelInfo.use_responses_lite` 决定是否使用 lite 格式，
它来自**客户端内置的模型目录**（编译进二进制的 `models.json`），
`config.toml` 的模型覆盖项里没有这个字段。目录外的任何模型名一律走 fallback，
`use_responses_lite: false` → 完整格式。

这里的 `slug` 就是模型名，`provider` 是客户端配置的服务提供方；`fallback` 指模型不在目录时使用的默认设置。
`responses-lite` 是当时的另一套请求组织方式，不是“更便宜的模型”。

## 当时源码怎样选择请求格式

请求组装，只有这一个开关（core/src/client.rs:795）：

```rust
let (instructions, tools) = if model_info.use_responses_lite {
    // lite：AdditionalTools + role:"developer" + 工具进 namespace 容器
} else {
    // 完整：顶层 instructions + 顶层 function tools
}
```

`model_info` 的构造（models-manager/src/manager.rs:728）：

```rust
construct_model_info_from_candidates(model, &remote_models, config):
    find_model_by_longest_prefix(model, candidates)   // 命中目录 → 用目录的字段值
      .or_else(namespaced_suffix)
    找不到 → model_info_from_slug(model)               // fallback: use_responses_lite=false
    → with_config_overrides(model_info, config)        // 只覆盖 context_window /
                                                       // auto_compact / tool_output /
                                                       // base_instructions —— 无 lite
```

内置目录（models-manager/models.json，`include_str!` 编译进二进制）：

| slug | lite | tool_mode |
|---|---|---|
| gpt-6-astra / gpt-5.6-sol / gpt-5.6-terra / gpt-5.6-luna | **true** | code_mode_only |
| gpt-daybreak-blue-latest / gpt-daybreak-red-latest / codex-auto-review | **true** | code_mode_only |
| gpt-5.5 / gpt-5.4 | false | — |

**前缀匹配的坑**（manager.rs:691 `find_model_by_longest_prefix`）：

```rust
if !model.starts_with(&candidate.slug) { continue; }
```

是 `starts_with`！`gpt-5.6-terra-custom` 会命中 `gpt-5.6-terra` 的 lite=true。
别名必须**不以任何内置 slug 为前缀**。

## 当时为什么使用内置模型目录

远端目录刷新条件（client.rs:977 `uses_codex_backend` +
model-provider/src/models_endpoint.rs:93）：

- 只有「ChatGPT 登录 + **官方 openai provider** + 无自定义 base_url」才走官方目录后端
  （`chatgpt.com/backend-api/codex/models`）；
- 配了自定义 provider（base_url=网关）→ `uses_codex_backend()=false`、
  `supports_api_key_discovery()=false`（`is_openai()`=provider 名必须是 "openai"）
  → `should_refresh_models()` 全 false → **根本不发目录请求**；
- 因此 `remote_models` = 内置 `models.json` 这 9 个 → 除非 slug 落在表里，
  否则都走 fallback（完整格式）。

在当时检查的这条路径里，网关的 `/v1/models` 返回内容不能改变这个判定。
模型名能出现在某个选择界面，不等于该界面能修改客户端的请求格式；本记录没有验证所有模型选择入口。

## 当时可用的办法

1. **用目录外的模型名**（当时的 deepseek-v4-flash 就是这么通过的）。
   想要 gpt-5.6 系的能力又不要 lite：在网关挂一个别名路由，
   别名要**不以内置 slug 为前缀**：`terra-nolite` ✓、`gpt-5.6-terra-x` ✗。
2. 模型名保持 gpt-5.6-terra 时，**当时没有找到只改配置就关闭 lite 的办法**。
   除非：上游兼容 lite 格式、或自编译 codex 改 models.json（不推荐）、
   或等待客户端提供相关配置（当时查看的 main 中没有找到）。
3. 网关照常只做透传：fallback 的模型名原样转发，网关按需把别名映射回上游真名。

## 备忘

- 当时内置的 `gpt-5.5` / `gpt-5.4` 使用完整格式（lite=false），不代表升级后仍然如此。
- 相关历史：[接口转换实验](protocol-bridge-plan-2026-09-10.md)第 10.13 节（消息角色差异）、
  第 10.11 节（工具调用被当成普通文本）。
