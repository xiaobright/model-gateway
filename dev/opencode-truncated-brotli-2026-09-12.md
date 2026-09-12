# opencode 没回复：一次压缩格式处理故障（2026-09-12）

**一句话：上游有回复，但用了旧网关处理不正确的压缩格式，客户端因此读不出来。** 这次不是“模型没有回答”。

当时补齐了解码依赖，并修了压缩请求头和响应头处理；还发现标题生成提示词有两处固定误报，后来用可配置替换规则处理。
下文保留原始现象、样本和当时的验证输出，**本次文档整理没有重新发请求或重跑测试**。

现在遇到类似问题请先看[抓包步骤与隐私提醒](maintenance.md#抓包排查)，不要仅凭 `truncated` 就认定同一个原因。

## 0. 当时确认的两个问题

| 现象 | 真因 | 处置 |
| --- | --- | --- |
| opencode 收不到任何回复；网关记 `note=truncated`，上游 站A 记 200 | 上游用 **Brotli** 压缩（`content-encoding: br`），网关 venv 没装 `brotli`，httpx 解不开，**把 421 字节压缩原文原样转发** | 装 `brotli` + `zstandard`；并让网关按自己真实能力重写 `accept-encoding`（不再照抄客户端） |
| 每轮开头两条 `500`（167B） | 站A 误报 opencode 标题生成提示词；最初找到一行，后续确认有两处 | 与主回复的压缩故障分开处理，后来配置了两条最小文本替换，见第 6 节 |

在这次排查中，没有证据指向并发、超时、包大小或工具数量。之前的合成测试请求（9KB / 21KB / 41KB / 72KB，
1~15 个工具）全部正常，正因为它们都没踩到压缩这条线。

## 1. 症状

转发记录（`GET /admin/api/requests`）：

```
6363 opencode 499 client_abort  req=60397B resp=0B
6362 opencode 200 truncated     req=60397B resp=3738B  text=0B  2512ms
6361 opencode 200 truncated     req=60397B resp=2694B  text=0B  2082ms
6357 opencode 500 ok            req=2782B  resp=167B
6352 python-httpx 200 ok        req=72285B resp=5420B  text=66B   ← 我自己的探针，正常
```

两个反常点：

1. `resp_text_bytes` 全是 **0** —— SSE 观察器一个内容字节都没认出来；
2. 同样 6 万字节的请求，我的探针（ua `python-httpx`）正常，opencode 的一律截断。

## 2. 抓包

当时开启抓包记录 20 条，用 `opencode run` 复现后取得了转发时的响应字节。这是当时操作记录；日常排查优先只开一到三条，见[维护说明](maintenance.md#抓包排查)。

`meta.json` 的响应头一眼看穿：

```json
"status": 200,
"note": "truncated",
"sent": 421,
"observer_ended": false,
"event_types": {},
"resp_headers": {
  "content-type": "text/event-stream",
  "transfer-encoding": "chunked",
  "content-encoding": "br",        ← 就是它
  "vary": "Accept-Encoding"
}
```

请求头：

```json
"accept-encoding": "gzip, deflate, br, zstd"
```

`stream.sse` 那 421 字节 `od -c` 一看全是二进制：

```
0000000 205 245  \0  \0 304 377   w 316 367   3   j 347 352 224   ( 312
```

只在末尾能看到明文 `data: [DONE]\n\n` —— 那是 Brotli 的**未压缩元块**把原始字节直接
嵌进流里了，不是「解了一半」。

为什么观察器没认出这个 `[DONE]`：它前面是 `\x08`，整行变成 `\x08data: [DONE]`，
data 字段不等于 `[DONE]`，所以 `observer.ended` 一直是 False，网关就记 `truncated`。

## 3. 根因

```text
当时执行：.venv/Scripts/python.exe -c "from httpx._decoders import SUPPORTED_DECODERS; print(SUPPORTED_DECODERS)"
{'identity': IdentityDecoder, 'gzip': GZipDecoder, 'deflate': DeflateDecoder}
```

装 `brotli` 之前，httpx 0.28.1 **只认 identity / gzip / deflate**。
而网关 `_build_headers()` 是把客户端的请求头原样转给上游的（`REQ_DROP` 里没有
`accept-encoding`），于是：

```
opencode 报 "gzip, deflate, br, zstd"
   → 上游挑了 br
   → httpx 解不开，aiter_bytes() 吐出压缩原文
   → 网关原样转发（RESP_DROP 还会把 content-encoding 摘掉，客户端更无从判断）
   → opencode 一个 SSE 事件都解析不出来 → 表现为「没回复」
   → 网关等不到完成事件 → 记 truncated
```

我的探针之所以没事：httpx 自己发的 `accept-encoding` 只列它解得开的编码，上游就选了
identity/gzip。curl 默认也不带 `accept-encoding`。**只有 opencode（bun）把 br/zstd 都报上了。**

## 4. 改了什么

1. `gateway/proxy.py`
   - 新增 `accept_encoding()`：从 `httpx._decoders.SUPPORTED_DECODERS` 读真实能力。
   - `_build_headers()` 里 `headers["accept-encoding"] = accept_encoding()`，
      不再照抄客户端。放在 auth / beta 之后、`header_override` 之前，所以供应商的
     「请求头覆写」仍能盖掉它。
2. `requirements.txt`：加 `brotli>=1.1`、`zstandard>=0.23`（httpx 有这两个包才会启用
    对应解码器）。当时安装的版本：brotli 1.2.0、zstandard 0.25.0，安装后
   `SUPPORTED_DECODERS = {identity, gzip, deflate, br, zstd}`。
3. `tests/test_units.py::test_accept_encoding_is_rewritten_not_copied_from_the_client`
   —— 断言报出去的每一种编码 httpx 都真的解得了。

## 5. 验证

重启网关后：

```
$ opencode run "只回复两个字：你好"
> build · deepseek-v4.1-flash
你好
```

转发记录：`200 note=ok req=58627B resp=1203B text=6B 1220ms`。

## 6. 附带发现：站A 敏感词误报（及网关侧的绕行）

每轮开头的两条 500（req≈2.5KB）是 opencode 的**会话标题生成**请求，上游回：

```json
{"error":{"message":"sensitive words detected (...)" ,"code":"sensitive_words_detected"}}
```

对照实验确认是上游词表误报，与用户消息无关：

| 改动 | 结果 |
| --- | --- |
| 原文照发 | 500 |
| 只换用户消息为 `hello` | 500 |
| 去掉 system 提示词 | **200** |

**坑：二分不能只做一趟。** 我先用二分缩到一行（去掉开头的 `- ` 就 200），照这条配了
规则 —— 请求体确实少了 2 字节，但**照样 500**。触发点不止一处，一趟二分只会找到其中
一条。改成迭代（洗掉 → 整段重测 → 再二分）或逐行扫才找全，实际是**两条**：

```
✗ 行21: 〈触发行 A〉  —— 一个 markdown 列表项，去掉开头的 "- " 即过审
✗ 行34: 〈触发行 B〉  —— 一个示例对，把中间的 → 换成 -> 即过审
```

（触发原文不写进文档，只保留在规则配置里，避免以后把仓库内容发给同类上游时再次触发。）

两条都换掉后整段提示词 200：

| from | to |
| --- | --- |
| 〈触发行 A〉 | 去掉列表符号的同一句 |
| 〈触发行 B〉 | `→` 换成 `->` 的同一句 |

### 网关侧的绕行层

新增 `gateway/rewrite.py` + `GET/PUT /admin/api/rewrite-rules` + 管理页「上游站点 →
敏感词绕行」卡片：转发前按规则表对请求体文本做字面替换，默认空表 = 一个字节都不改。

配好上面两条后实测：标题生成请求从 500 变 200（resp 从 167B → 73KB，text=1075B），
每轮那两次白跑消失。

这不是自动审核或隐私过滤：网关不知道上游的完整规则，只对用户明确配置的固定文本做替换。

## 7. 下次再遇到「上游说正常、下游说截断」

1. 按[维护说明](maintenance.md#抓包排查)确认隐私风险，开启一条或少量抓包。
2. 复现一次，记下时间。
3. 查看对应目录 `meta.json` 的 `resp_headers`，再与 `stream.sse` 的实际内容对照。
4. 确认开关已关闭；已生成的文件不会自动删除，是否保留另行决定，不要直接公开。
