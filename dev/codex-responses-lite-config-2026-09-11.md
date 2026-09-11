# 让 Codex 不发 responses-lite 请求：配置侧研究

2026-09-11。基于 openai/codex main 分支源码（`%TEMP%\codex-src`），与 0.153.4 实测行为一致。

## 结论摘要

**没有一键开关。** lite 与否只由 `ModelInfo.use_responses_lite` 一个字段决定，
它来自**客户端内置的模型目录**（编译进二进制的 `models.json`），
`config.toml` 的模型覆盖项里没有这个字段。目录外的任何模型名一律走 fallback，
`use_responses_lite: false` → 完整格式。

## 判定链（源码位置）

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

## 为什么自定义 provider 下目录就是内置的 9 个

远端目录刷新条件（client.rs:977 `uses_codex_backend` +
model-provider/src/models_endpoint.rs:93）：

- 只有「ChatGPT 登录 + **官方 openai provider** + 无自定义 base_url」才走官方目录后端
  （`chatgpt.com/backend-api/codex/models`）；
- 配了自定义 provider（base_url=网关）→ `uses_codex_backend()=false`、
  `supports_api_key_discovery()=false`（`is_openai()`=provider 名必须是 "openai"）
  → `should_refresh_models()` 全 false → **根本不发目录请求**；
- 因此 `remote_models` = 内置 `models.json` 这 9 个 → 除非 slug 落在表里，
  否则都走 fallback（完整格式）。

即网关的 `/v1/models` 返回什么都影响不了这个判定（目录请求压根不发）。
`/v1/models` 只影响 Codex UI 的模型列表展示，不影响线格式。

## 实操

1. **用目录外的 slug**（推荐，当前 deepseek-v4-flash 就是这么过的）。
   想要 gpt-5.6 系的能力又不要 lite：在网关挂一个别名路由，
   别名要**不以内置 slug 为前缀**：`terra-nolite` ✓、`gpt-5.6-terra-x` ✗。
2. 把模型真的命名成 gpt-5.6-terra 时，**无解**（配置层面）。
   除非：上游兼容 lite 格式、或自编译 codex 改 models.json（不推荐）、
   或未来官方加开关（目前 main 没有）。
3. 网关照常只做透传：fallback 的模型名原样转发，网关按需把别名映射回上游真名。

## 备忘

- `gpt-5.5` / `gpt-5.4` 内置就是完整格式（lite=false），当档位名用没问题。
- 相关历史：`dev/protocol-bridge-plan-2026-09-10.md` §10.13（developer-role 根源）、
  §10.11（DSML 之谜 = terra 的 lite 工具格式被 DeepSeek 拒绝）。
