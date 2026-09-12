# 维护整理结果（2026-09-08）

## 先看这几句

- 普通测试改为在进程里模拟请求，不再每个用例都启动服务器；确实需要连接、代理或证书的测试仍保留。
- 接口选项改为从同一处读取；分组编辑和页面刷新增加保护，避免迟到的请求改错表单或覆盖新数据。
- 当时完整测试 **103 项通过，用时 96.79 秒**；快速组仍需 **47.79 秒**，没有达到最初期望的速度。
- 部分浏览器操作检查过，但完整交互清单**没有全部验证**，具体缺项在后文列明。

> 这是当时的结果，不是本次文档整理重新跑出的成绩。下面的“本轮”、提交号、版本和工作树状态都指那次开发。
> 当前测试方法见[维护说明](maintenance.md#测试怎么跑)，原要求见[任务书](task-maintainability-2026-09-07.md)。

日期：2026-09-08
目录：`<仓库根>`
执行环境：Windows、Python 3.14.0、pytest 9.1.1、Node.js v24.15.0；使用现有 `.venv`，没有新增依赖。

## 结论

任务书 P1–P4 的代码与文档工作已完成；浏览器验证有未覆盖项，见后文。应用代码没有推倒重写，没有加入协议转换、新的模型调用 API、跨接口同名模型规则或生产配置迁移。

原验收代码提交为 `4b6929e`。独立复审又修了刷新、路由编辑和模型写入中因请求先后顺序造成的问题，当次复审后提交为 `cd55fb2`。
完整测试仍通过 103 项；快速组与真实网络组不重复，合起来覆盖全部 103 项。快速组最终实测为 47.79 秒，主要耗时已定位，没有靠删检查、改正式运行参数或用更多模拟替代真实连接来凑时间。

## 提交与工作树

任务书指定的应用基线是 `02ae12d`。开始本次增量收尾时 HEAD 为 `e56362e`，工作树含 README、前端三处收尾改动和未跟踪任务书；之前三批提交如下：

| 批次 | 提交 | 内容 |
| --- | --- | --- |
| P1 | `f3b9932` | 将默认测试改为进程内 TestClient/MockTransport，保留真实网络测试并建立 `network` 标记 |
| P2 | `289d5f2` | 集中协议登记，增加只读协议元数据接口，复制分组明确目标 |
| P3 | `e56362e` | 分组编辑会话隔离、迟到响应保护、模型写入局部防重复、刷新请求合并 |
| P4 收尾 | `4b6929e` | 协议初始化失败重试、首次筛选恢复、默认协议兼容、README 维护说明 |

报告本身另行提交，不把报告提交 SHA 写回报告，避免自引用。任务书 `dev/task-maintainability-2026-09-07.md` 按要求保留为未跟踪输入文件。结束时除该任务书外无应用代码未提交改动；没有修改 `data/`、真实配置、密钥或运行数据。

## 实际改了什么，哪些没有做

- 测试 fixture 按是否需要真实 socket 分组；进程内路径仍经过 FastAPI 应用和管理接口，远程上游 I/O 使用 `httpx.MockTransport`。
- `gateway/protocols.py` 继续统一登记协议；`/admin/api/protocols` 只返回页面需要的展示信息，前端筛选、下拉和复制目标使用同一份列表。
- 复制分组在多于两个协议时要求明确目标；**当次开发结束时**只登记并运行 Anthropic 与 OpenAI Responses 两种协议。
- `web/async-state.js` 负责同一数据域的轻量刷新合并；`web/group-editor.js` 负责分组编辑会话、模型拉取和局部写入锁。
- `web/app.js` 保留显式的端点、Claude Code/Codex 提示和现有模型单接口规则；协议首次加载失败可重试，后续失败保留最后有效列表。
- README 增加了快速、network、完整测试命令以及职责边界说明。

有意未做：协议转换、Chat Completions/Completions 新入口、同名模型跨接口路由、新数据库结构、历史重算、框架迁移、通用 Repository/Service、全局状态框架和大规模目录重组。这些均被任务书排除，且本轮没有证据要求改变。

## 测试集合与迁移对应

最终收集到 103 项参数化测试。任务书编写时的 101 项基线增加的 2 项是协议元数据安全投影和多协议复制目标行为检查，属于 P2 新增行为证据。

| 原测试范围 | 最终执行方式 | 主要保留契约 |
| --- | --- | --- |
| `test_admin` 28 项 | 快速、临时 SQLite、进程内 TestClient；远程模型列表走 MockTransport | 管理状态码、落库、协议锁定、复制、出口字段、同源限制、迁移与 `/admin/api/protocols` |
| `test_routing` 10 项 | 快速、临时 SQLite/进程内 API | 首选/备用、禁用、排序、删除兜底、斜杠模型名、单接口模型规则 |
| `test_stats` 6 项 | 快速、临时 SQLite | 缓存口径、HTTP 错误、思维链过滤、token 比例与健康统计 |
| `test_failover` 11 项 | 快速、MockTransport 和可控测试状态 | 尝试顺序、站级/模型级失败、冷却、次数、状态请求限制、手动清冷却 |
| `test_proxy` 21 项 | 18 项快速，3 项 network | 字节透传、头和鉴权、模型改写、usage、错误、SSE；network 保留真实流切换、断流和完成后挂连接 |
| `test_inflight` 6 项 | 3 项快速，3 项 network | 实时登记、取消、请求轨迹、count_tokens 不计入；network 保留真实取消/流行为 |
| `test_lifecycle` 7 项 | 快速、ASGI/MockTransport 和事件控制 | JSON/SSE 分块、三阶段取消、并发隔离 |
| `test_egress` 8 项 | 5 项快速，3 项 network | 出口配置校验、直连不走系统代理和保存时 CA 校验快速验证；代理、探测和自签 TLS 端到端经过真实 socket |
| `test_units` 6 项 | 5 项快速，1 项 network | SSE 观察器、取消边界；自签 TLS 信任/拒绝保留真实连接 |

与 K1–K12 的对应证据集中在上述 `test_proxy`、`test_lifecycle`、`test_failover`、`test_stats`、`test_egress`、`test_units`、`test_admin` 和 `test_routing`；本轮没有删除行为断言。最近三次既有修复（故障切换/统计、迁移/取消）均由完整套件回归覆盖。

真实网络场景保留位置：

1. `test_proxy.py`：流中切换、客户端中途断开、完成事件后客户端离开；拆分完成标记在快速组用 MockTransport 验证。
2. `test_inflight.py`：真实流登记、实时取消和后续网关可用性。
3. `test_egress.py`：真实代理流量、直连不走系统代理、探测和自签 TLS CA。
4. `test_units.py`：真实 TLS CA pin 通过/不信任失败。

## 实测结果

命令均在同一工作区和现有 `.venv` 下执行，使用 `-p no:cacheprovider`；没有使用公网。

```powershell
$env:PYTHONUTF8 = '1'
$env:PYTHONDONTWRITEBYTECODE = '1'
& '.\.venv\Scripts\python.exe' -m pytest tests -q -p no:cacheprovider -m 'not network' --durations=10
& '.\.venv\Scripts\python.exe' -m pytest tests -q -p no:cacheprovider -m network --durations=10
& '.\.venv\Scripts\python.exe' -m pytest tests -q -p no:cacheprovider --durations=15
node --check web/app.js
node --check web/views.js
node --check web/util.js
node --check web/async-state.js
node --check web/group-editor.js
node tests/web_protocols.test.mjs
```

| 集合 | 结果 | 实际耗时 | 最慢项目 |
| --- | --- | ---: | --- |
| 快速 `not network` | 93 passed，10 deselected | 47.79s | `test_token_ratio_is_learned_from_the_log` 3.76s；`test_direct_really_turns_the_system_proxy_off` 3.69s |
| network | 10 passed，93 deselected | 60.99s | `test_probe_reports_which_door_works` 12.79s；完成事件后挂连接 8.19s；代理流量 8.19s |
| 完整 | 103 passed，0 failed | 96.79s | 探测 12.68s；完成事件后挂连接 8.36s；代理流量 7.26s |

集合核对结果为：总数 103、快速 93、network 10、并集 103、交集 0、遗漏 0、额外 0。完整套件只有一个既有 `StarletteDeprecationWarning`：当前 Starlette 的 TestClient 提示未来应安装 `httpx2`；本轮没有升级依赖。

Node 行为测试输出 `web protocol metadata behavior: ok`，JavaScript 语法检查通过，`git diff --check` 通过。

快速组仍超过 30 秒，主要是 token 比例测试的真实数据库/统计准备和系统代理关闭隔离测试；继续优化需要重新评估这些测试的准备边界，本轮停止在明确分组和不改变验证语义的位置。

## 页面操作检查，以及尚未验证的地方

使用临时库 `%TEMP%\model-gateway-ui-check` 和本机模拟上游完成了以下实际检查：

- 协议筛选动态显示 `Anthropic Messages` / `OpenAI Responses`。
- 新建供应商后可以直接进入分组编辑。
- 分组接口下拉和模型导入界面正常显示。
- 临时实例曾监听 `127.0.0.1:18317`，验证后已停止；端口无监听，临时库文件已删除。

受当前浏览器验证时间和工具边界限制，任务书列出的完整浏览器交互清单没有全部逐项复现。以下项目未在本轮浏览器中独立验证，不能用代码阅读或语法检查冒充通过：可控延迟 A/B 分组切换、关闭后重开同组、保存与轮询交错、筛选后的批量勾选/恢复、移动分组、候选删除确认、实时页多流交错、重复初始化轮询以及主题/键盘回归。相关异步核心仍有 Node 行为检查，管理/后端行为由 Python 测试覆盖。

## 后续复审重点

建议复审以下提交和文件：

- `f3b9932`、`tests/conftest.py`、`tests/helpers.py`：确认快速组没有错误地替代真实 socket 行为。
- `289d5f2`、`gateway/protocols.py`、`gateway/admin.py`、`web/util.js`、`web/views.js`：确认给页面的协议信息不泄露配置，复制分组时不会猜错目标接口。
- `e56362e`、`web/async-state.js`、`web/group-editor.js`、`web/app.js`：重点检查写入完成后的补读、旧会话 finally、共享 failover/live 状态序号和局部写入锁。
- `4b6929e`、README：确认初始化失败不以空协议提交，默认 OpenAI 选择保持既有行为，维护命令可直接运行。
- 继续关注现有 TestClient 弃用警告；它不是本轮失败，但未来依赖升级时需要单独处理。
