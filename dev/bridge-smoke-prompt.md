# 协议桥接 · 真机验收提示词（M5）

> 用法：在管理页给某个模型挂一条 **OpenAI Chat Completions** 分组的候选，勾上「转换为该接口
> 暴露」，然后让 Codex 用**那个模型名**跑下面这段提示词。全程只在一个临时目录里操作，
> 不碰任何仓库文件。

为什么是这几步：桥接最容易坏的地方是**工具调用**（`tools` 映射、`custom` 工具的参数互转、
参数跨 chunk 拼接）和**多轮历史回放**（`function_call` + `function_call_output` 还原成
`assistant.tool_calls` + `role:tool`）。所以下面每一步都盯着其中一个点。

---

## 一、提示词（直接粘给 Codex）

```
这是一次协议转换的连通性验证。只在一个临时目录里操作，不要碰 <仓库根>
里的任何文件，也不要提交任何东西。

工作目录：<临时目录>/bridge-smoke（不存在就自己建）

请按顺序做完下面 6 步，一步都不要合并、也不要跳过：

1. 建目录 <临时目录>/bridge-smoke，在里面新建 notes.md，内容只有一行：
   smoke-test-start
   建目录用 shell；写文件用 apply_patch，不要用 shell 重定向或 echo 去写文件。

2. 用 shell 跑一条**故意会失败**的命令，要求：先打印 before-fail，然后以退出码 3 结束。
   把它打印的内容和退出码原样告诉我。这一步失败是对的。

3. 用 apply_patch 改 notes.md：原来那一行留着不动，在它下面追加两行，最终正好三行：
   smoke-test-start
   中文也要能过桥：这一行里的非 ASCII 字符如果变了就是坏的
   BRIDGE-OK-7f3a

4. 用 shell 跑两条互不依赖的命令，都要跑，可以并行：一条打印两行 A 和 B，一条打印一行 C。
   这是同一轮里的两次独立工具调用。

5. 用 apply_patch 新建 big.txt，一共 60 行，第 i 行内容就是 line-<i>（i 从 1 到 60，不要补零）。
   这一次的补丁会比较长，请一口气写完，不要分几次追加。

6. 收尾核对，用 python 依次做三件事并把输出原样贴出来：
   - 数 notes.md 和 big.txt 各有多少行
   - 把 notes.md 完整打印出来
   - 算 notes.md 的 sha256

最后回答下面四个问题，一个都不要省：
a) 这 6 步里哪一步的工具调用报错了？报错原文是什么？
b) notes.md 最终几行？标记 BRIDGE-OK-7f3a 在不在？
c) big.txt 是 60 行吗？
d) 你有没有遇到这几种情况：工具返回的内容像是缺了一半、工具参数拼成了非法 JSON、
   同一个工具被重复调用、或者你自己觉得"刚才那步明明没返回结果"？有就贴原话，
   没有就说没有。

不要为了让我满意而说一切都好。哪一步报错、哪一步输出不完整，都照实说。
```

### 想先快速确认能不能用，只跑这 3 步

```
在临时目录 <临时目录>/bridge-smoke 里做两件事（目录不存在就建）：
1. 用 shell 列一下这个目录，然后把结果告诉我。
2. 用 apply_patch 在里面新建 hello.txt，内容是两行：第一行 hello，第二行 中文测试。
然后原样读出这个文件。哪一步的工具调用报错了，就把报错原文贴给我。
```

---

## 二、怎么判读

### 看 Codex 那一侧

| 现象 | 坏在哪 |
|---|---|
| 上游 422：`unknown variant 'developer'` | role 没映射。**2026-09-10 首次真机就挂在这，已修**（`developer`→`system`）—— 如果你又看到它，说明网关进程还是旧的，重启一下 |
| Codex 说工具名不认识 / 报 `unknown tool` | `tools` 映射。最可能是 `additional_tools` 没被提到顶层，或 `custom`→`function` 转换没生效 |
| 模型一直在聊天、压根不调工具 | 同上，或者 `tool_choice` 没传对 |
| 模型说一句话就结束、从不调工具，转发记录 note 里有 `tools=namespace/…` | **namespace 容器没展开**（0.153.x 把全部工具包进 `type:"namespace"` 容器）。**2026-09-10 第二跑挂在这，已修** —— 又看到就是网关进程还是旧的 |
| apply_patch 报 `invalid patch` / 补丁变成一坨带 `\n` 的转义字符串 | `custom_tool_call.input` ↔ `{"content": ...}` 的互转 |
| 第 5 步的长补丁报 JSON 解析错、内容缺一段 | 工具参数**跨 chunk 拼接**（网关按 10 字符切片下发） |
| 第 4 步两条命令只跑了一条 | 同一轮里多个 `tool_calls` 的合并（连续 `function_call` 要合成一条 assistant 消息） |
| 第 3 步中文变问号/乱码，或第三行被吃掉 | UTF-8 与 JSON 转义 |
| 第 6 步输出看着缺一半、或 Codex 卡住不结束 | 流式**收尾事件**（`output_item.done` / `response.completed` / 末尾 `[DONE]`） |
| 第二轮开始上下文错乱、模型重复问同一件事 | 多轮历史回放（`function_call` + `function_call_output` → `assistant.tool_calls` + `role:tool`） |
| Codex 报缺 usage / 计费是 0 | `stream_options.include_usage` 与 usage 映射 |

### 看网关那一侧

- `data/gateway.log`：这次请求那行应该带 `bridge=openai-chat->openai`。没有这个标记说明它压根
  没走桥接（多半是候选没勾，或者 Codex 请求里的模型名不在那条链上）。
- 「转发记录」页：协议列旁边会多一个 **转换** 标签（说明这条是经协议转换发出去的）；备注列里
  会写清这一轮丢掉了什么 —— 形如 `ok (tools=web_search parts=input_image)`。那是**故意丢的**，
  不是 bug；备注里出现 `bridge_error` 才是真出问题了。

---

## 三、已知的边界（不用当 bug 报）

- **带 `previous_response_id` 的请求不走桥接**：网关会跳过桥接候选。真 Codex 目前每轮都发全量
  历史、不用这个字段，所以正常用不到。
- **`web_search` 会被丢掉**：Chat 协议里没有服务端搜索的对应物。Codex 的联网搜索走的是
  `/v1/alpha/search` 那个独立端点，跟这条链无关。
- **图片输入会被丢**：v1 不做多模态。
- **只回摘要的推理**：上游 Chat 站给不了加密的完整思维链，网关只把 `reasoning_content`
  当摘要回放。多轮时 Codex 会把摘要传回来，网关按普通推理处理。
