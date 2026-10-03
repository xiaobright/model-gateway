# 后端审查（2026-10-03）

审查基线：`97c4d1d2484c19158e536063be1e0b51f8e33efe`。范围包括转发、代理连接池、取消与收尾、重试、数据库及迁移、管理接口、批量操作、协议观察、抓包、统计和学习数据采集。

本报告的 **10 项问题均已通过隔离复现确认，并已完成代码修复**。下文“确认问题”保留修复前的触发条件与证据，行号对应审查基线；修复及验证结果见下节。没有把它们认定为今早自启失败的原因：当次真实启动错误没有留下可定位记录，当前已经迁移的数据库也不受第 8 项旧库问题影响。

## 本次已修复的启动流程

主任务已完成启动流程修复及定向验证：

- 启动等待上限由约 10 秒放宽到 60 秒；后台线程已经退出时立即报告失败。
- 区分等待超时、绑定失败和后台异常；超时会请求停止本次启动，不再统一提示端口可能被占用。
- 保存初始化、后台线程及 lifespan 异常到 `data/startup.log`；单文件上限 1 MiB，保留当前文件及 2 份轮转文件。
- BAT 启动检查调整为最多 65 轮本机直连健康检查。

`tests/test_startup.py` 默认组为 **12 passed / 3 deselected**，本机网络组为 **3 passed / 12 deselected**。前者覆盖虚拟时钟下 12 秒启动成功、默认 60 秒超时、死线程即时失败、后台 `RuntimeError/SystemExit` 持久化、初始化和 lifespan 异常、绑定拒绝分类、日志上限及不可写、托盘报错前收尾。三个 TCP 用例分别验证实际占用端口、完整网关使用临时数据库起停与 health、超时后迟到启动的关闭。

以上修复改善误判及错误留证，不能追溯确认今早的准确根因。没有重启正式网关或修改正式数据，启动修复将在下次启动时生效；下面 10 项审查发现也已按用户后续授权完成修复。

## 后续授权的后端修复

| 原问题 | 修复后的行为 | 回归覆盖 |
| --- | --- | --- |
| 1 首块前断开与取消 | 守卫等待可及时感知断开，取消会停止读取、释放响应并注销在途请求；关闭连接免受 ASGI 取消作用域打断。 | `test_lifecycle` 的三种取消及真实 TCP 断开用例 |
| 2 抓包错误破坏收尾 | flag 删除失败只记录诊断，抓包异常被隔离；请求注销、连接释放另有 finally 兜底。 | `test_lifecycle` 的 flag 权限错误和抓包收尾异常 |
| 3 分组协议检查竞态 | 写事务在读取协议和候选数之前开始，候选插入无法穿过校验与更新之间。 | `test_db_consistency` 的实际双线程交错 |
| 4 批量保存部分提交 | 当前分组检查、目录登记、候选排序与首选补齐在一个事务完成；意外异常整批回滚，失效项仍准确列入 skipped。 | 触发器制造第二条写入失败、并发删除、原有批量接口用例 |
| 5 协议失败误记成功 | 保留原 HTTP 状态与字节，以 protocol_error 统一记录在途结果、历史、健康统计及网页标签；保留原有空流守卫规则。 | JSON / SSE 四种错误形状、健康统计和网页结果用例 |
| 6 配置形状异常 | 非对象 JSON、无穷大、非整数端口等无效内容回退默认端口。 | `test_backend_boundaries` 与原有端口配置用例 |
| 7 供应商地址竞态 | 创建及更新均在地址查重前取得写事务。 | 创建/更新与同地址新增的双线程交错 |
| 8 旧库迁移 ID 碰撞 | 先复制全部原 ID，再分配额外协议分组 ID；原候选和密钥保持对应关系。 | 扩展原迁移用例，覆盖前两组均需拆协议及重复初始化 |
| 9 冷却溢出 | 先限制指数，再做乘法，原 90/180/360/600 秒规则不变。 | 连续 2000 次失败及成功后复位 |
| 10 畸形用量 | 单字段超过 10^12 或解析异常时记为缺失；计量异常不影响响应透传和收尾。 | 三协议极长数字、解析器抛异常及完整转发用例 |

启动修复的原验证记录保留在上节。本轮定向检查先取得 170 项通过，两个新用例的预期需要与其输入对齐：迁移新增两个协议分组后的数量应为 7；异常大整数会阻止 data JSON 的结束类型解析，因此正常结束样本改用明确的 SSE event 头。修正后相关 10 项定向回归通过。网页结果相关 Node 行为测试 5 项通过，改动的 JavaScript 语法检查通过。

最终执行一次 `uv run .venv\Scripts\python.exe -m pytest --full -q -p no:cacheprovider --durations=10`：**345 passed / 2 failed，111.30 秒**，包含真实本机 TCP、代理/TLS 及新加的首块前断开用例。

两处失败随后处理：协议错误标签收窄到成功 HTTP 状态，普通 400 仍沿用原记录语义；旧 TLS 测试的替身改为接受传输器关键字参数，显式覆盖系统代理有/无两种快照，保留真实 SSLContext 校验并补验证代理使用同一证书上下文。生产代理实现未修改。

最后定向复测 **16 passed / 108 deselected**：

```powershell
uv run .venv\Scripts\python.exe -m pytest tests/test_same_retry.py tests/test_lifecycle.py tests/test_learning.py tests/test_stats.py tests/test_units.py -k 'without_rule_400 or protocol_failure or observation_failures or real_forward_metadata or stats_normalize or client_args_verifies' -q -p no:cacheprovider
```

这轮覆盖两个失败点及结果标记、计量/抓包隔离、学习记录和统计相关调用。没有再重复全量，因此上述全量结果与最终定向复测分开记录；现有 Starlette TestClient 弃用警告保留。

没有更改正式数据、访问真实供应商或重启正式网关；代码需下次启动才能生效。

## 确认问题

### 1. [P2] 截断守卫等待首块时不能及时感知断开，任务取消后未完整释放

- **位置：**[gateway/proxy.py](../gateway/proxy.py)，264 行的读取等待及 743–751 行的异常处理。
- **触发：**已武装 200 截断守卫，上游返回 SSE 响应头后迟迟不给首块；此时下游断开，或转发任务被取消。
- **隔离证据：**临时 SQLite + MockTransport。将下游设为断开后等待 200 ms，仍为 `forward_completed=False`、`disconnect_checks=0`、`_SystemClient.active=1`、`inflight.requests=1`。再取消转发任务，观察到 `resp.is_closed=False`、`body.closed=False`，活动计数仍为 1。
- **影响与建议：**断开检查在读取返回后才执行；仅断开时可能等到上游读取超时，任务取消还会跳过响应关闭及在途注销。让守卫等待同时感知下游断开，并在包含取消异常的兜底收尾中关闭响应、完成 inflight 登记。不要因此更改正式读取超时。

### 2. [P2] 抓包自动停止的文件错误会打断正常请求收尾

- **位置：**[gateway/capture.py](../gateway/capture.py)，257–260 行；[gateway/proxy.py](../gateway/proxy.py)，998 行调用及其后的收尾。
- **触发：**抓满后自动删除 flag，但文件只读、权限或占用引发 `PermissionError/OSError`。
- **隔离证据：**临时抓包 flag 设置 Windows 只读属性，模拟正常 JSON 响应；返回结尾抛出 `PermissionError`，没有新增请求记录，`inflight.requests=1`。
- **影响与建议：**代码只捕获 `FileNotFoundError`，异常会从 relay 的 `finally` 逃出，跳过后面的注销、记录及显式关闭。抓包关闭失败应只记诊断，核心注销及连接释放要有独立兜底。此复现已读到完整 EOF，HTTPX 响应已自动关闭，**不能把这次结果报告成已证实的连接泄漏**。

### 3. [P2] 并发新增候选能穿透分组接口锁定

- **位置：**[gateway/db.py](../gateway/db.py)，747–764 行 `update_group()`。
- **触发：**一个请求查到分组没有候选、准备更改协议；另一个请求在真正 UPDATE 前新增该分组的候选。
- **隔离证据：**临时 SQLite + 两线程定点交错。先新增的候选协议为 `openai`，随后分组更新仍成功，最终候选协议成为 `anthropic`，`openai_chain_size=0`。
- **影响与建议：**SQLite 默认在首次写入时才启动事务，前面的 COUNT 没有与 UPDATE 串行，导致刚新增的路由静默换协议。应在检查前 `BEGIN IMMEDIATE`，将接口检查和更新放在同一个事务中。

### 4. [P2] 批量添加可能整体报错但已经保存部分候选

- **位置：**[gateway/model_batch.py](../gateway/model_batch.py)，279–285 行；管理入口为 [gateway/admin.py](../gateway/admin.py) 的 `POST /models/batch`。
- **触发：**批量提交计划已生成，目录已登记，另一个管理请求删掉计划中的后续分组。
- **隔离证据：**临时 SQLite，在目录提交后删掉第二分组。第一个候选成功落库，第二个抛出 `IntegrityError: FOREIGN KEY constraint failed`；读取库仍能看到第一个 `batch-review` 候选。
- **影响与建议：**目录和各候选分别提交，异常又未转换成 `skipped`，接口整体 500 与实际部分成功不一致。建议在 DB 层一次事务中重新检查分组状态、登记目录并添加候选，跳过失效项并返回真实结果。

### 5. [P2] HTTP 200 中的协议失败被记录为成功

- **位置：**[gateway/proxy.py](../gateway/proxy.py)，1012–1016、1041–1042 行；[gateway/stats.py](../gateway/stats.py)，67 行的失败判断。
- **触发：**上游返回 `HTTP 200 {"error":{...}}`、`HTTP 200 {"status":"failed","output":[]}`，或 SSE `event:error` 后接 `[DONE]`。
- **隔离证据：**临时 SQLite + ASGITransport + MockTransport，三种响应均原样透传，但最终 `request_log.note="ok"`、`stats._failed=False`。上游句柄正常关闭，在途计数归零。
- **影响与建议：**协议错误目前只进入 learning 观察，不改变常规请求结果及健康统计，故障会被计入成功。保留原 HTTP 状态和响应，以独立 note 表示协议失败，让实时、历史和健康统计采用同一口径；不因已经透传后的观察结果自动重试。

### 6. [P2] 合法 JSON 的错误形状会让端口读取异常退出

- **位置：**[gateway/config.py](../gateway/config.py)，18–26 行 `load_port()`。
- **触发：**`settings.json` 是合法 JSON，但顶层为数组、空值或字符串，或者端口为过大数值。
- **隔离证据：**临时 settings 内容 `[]`、`null`、`"text"` 均抛 `AttributeError`；`{"port":1e999}` 抛 `OverflowError`。
- **影响与建议：**顶层未经类型检查便调用 `.get()`，`int(inf)` 也不在现有异常兜底中，绕过原本无效配置回退默认端口的逻辑。应检查顶层对象和端口值，再选择一致的回退或明确报错。本次没有证据表明正式配置出现过这些内容；此项随后随本次后端修复一并处理，异常配置回退默认端口。

### 7. [P2] 同地址供应商的唯一性检查存在竞态

- **位置：**[gateway/db.py](../gateway/db.py)，631–638 行 `create_upstream()`；`update_upstream()` 的地址检查也采用相同模式。
- **触发：**两个不同名称、相同站根的创建请求并发，均在另一方提交前通过地址检查。
- **隔离证据：**临时 SQLite + 两线程，同步两次查重完成后继续写入；两个请求均成功，得到 `duplicate-0`、`duplicate-1`，站根完全相同。
- **影响与建议：**数据库只约束供应商名称唯一，无法兜住地址查重与写入之间的空隙，违反一个地址一个供应商的现有约定。创建和更新都应在查重前开启写事务；若改用唯一索引，需先处理可能已经存在的重复地址。

### 8. [P2，旧库限定] 多协议分组迁移会发生主键冲突

- **位置：**[gateway/db.py](../gateway/db.py)，397–411 行 `_migrate_group_protocols()`。
- **触发：**仍受支持的旧库将协议挂在供应商上；原分组 ID 为连续的 `1,2`，供应商协议为 `openai,anthropic`。
- **隔离证据：**构造独立旧库调用迁移，抛出 `IntegrityError: UNIQUE constraint failed: upstream_groups_new.id`。第一原分组保留 ID 1，其额外协议分组自动占用 ID 2，下一原分组再插入 ID 2 时冲突。
- **影响与建议：**这类旧库升级无法完成，会阻止使用旧备份恢复后的启动；**当前已迁移库不走此路径**。应先保留 ID 复制所有原分组，再创建额外协议分组，补连续 ID 的迁移覆盖。

### 9. [P2，低频边界] 长期连续失败会使冷却指数计算溢出

- **位置：**[gateway/failover.py](../gateway/failover.py)，112 行 `note_fail()`。
- **触发：**同分组连续失败累计到 1026 次，期间没有成功或手动清除状态。没有可用暖候选时，冷却分组仍可能被选中，计数可继续累加。
- **隔离证据：**独立进程只操作内存状态，将日志函数替换为无操作；第 1026 次调用抛出 `OverflowError: int too large to convert to float`。
- **影响与建议：**`min(COOL_MAX, COOL_SECONDS * 2**n)` 先做乘法再封顶，封顶不能阻止溢出，后续同组失败会继续抛错。应在达到最大冷却跨度后直接取上限，或先限制指数。

### 10. [P3] 畸形 usage 数字能让观察逻辑中断请求收尾

- **位置：**[gateway/protocols.py](../gateway/protocols.py)，56–69 行 `_last()` / `_largest()`；[gateway/proxy.py](../gateway/proxy.py)，1040 行调用。
- **触发：**上游 usage 中包含异常长的十进制整数。
- **隔离证据：**模拟 Responses 流含 4400 位 `input_tokens`，触发 Python 整数解析长度限制并抛 `ValueError`；没有请求记录，在途计数保留 1。该 EOF 案例响应已自动关闭。
- **影响与建议：**纯计量异常逃出收尾路径，影响核心注销和记录。数字解析失败应返回缺失值，并限制合理数值范围；观察失败不能影响核心收尾。未验证真实供应商会产生这种数据，按低优先级边界处理。

## 修复前审查阶段的验证与限制

下列为不同审查组的实际结果，分别列出，不将测试数混加为一次完整验收：

| 审查组 | 实际范围 | 结果 |
| --- | --- | --- |
| 转发与连接生命周期 | `test_system_proxy.py`、`test_lifecycle.py`、`test_truncation.py` 中选定的代理快照、连接退休、取消与截断守卫用例 | 11 passed，25 deselected |
| 数据库与管理接口 | `test_admin.py`、`test_model_batch.py` 中地址重复、接口锁定、批量登记、幂等及跳过未知分组用例 | 5 passed，68 deselected |
| 观察、抓包与统计 | `test_units.py`、`test_learning.py`、`test_stats.py`、`test_admin.py` 中协议观察、抓包、配置、访问限制、学习及统计用例 | 38 passed，100 deselected |

可复核命令（项目根目录 PowerShell）：

```powershell
uv run .venv\Scripts\python.exe -m pytest tests/test_system_proxy.py tests/test_lifecycle.py tests/test_truncation.py -k 'test_windows_off_on_change_off_ignores_inherited_environment or test_switch_during_client_construction_uses_one_snapshot or test_retired_client_waits_for_headers_and_stream or test_unsent_old_client_uses_current_settings or test_manual_cancel_stops_only_the_selected_request or test_empty_200_is_held_and_resent_until_good or test_hold_retry_gives_up_after_times' -q -p no:cacheprovider

uv run .venv\Scripts\python.exe -m pytest tests/test_admin.py tests/test_model_batch.py -k 'duplicate_base_url or group_protocol_is_locked or batch_skips_unknown_groups or batch_registers_catalog or batch_is_idempotent' -q -p no:cacheprovider

uv run python -m pytest tests/test_units.py tests/test_learning.py tests/test_stats.py tests/test_admin.py -k 'sse_observer or capture or settings_port or local_only or cross_site or foreign_host or static_assets or metadata_only or collector or token_ratio or cache_hit or overview' -q
```

上述现有测试通过不代表已覆盖本报告的问题；10 项审查发现的额外复现使用临时库、模拟传输、内存状态和受控交错，尚未为这些发现新增永久回归用例。启动修复已新增独立的 `tests/test_startup.py`。

观察组首次使用 `uv run pytest` 时因模块搜索路径收集失败，改用项目惯用的 `uv run python -m pytest` 后同范围通过。测试有既有 Starlette TestClient 弃用警告。

数据库组四项复现均输出了证据，但脚本末尾临时目录清理由于构造旧库的 SQLite 连接未显式关闭而报 `WinError 32`，命令最终退出码为 **1**，不能记成该脚本整轮通过。进程已退出，残留目录为 `C:\Users\y2278\AppData\Local\Temp\mg-admin-review-i2wjs6oo`，未作额外清理。

审查未运行全量测试，未访问真实供应商；隔离复现未读取或更改正式配置数据，未启动或重启正式网关。没有进行真实跨浏览器攻击、长期磁盘压力或全协议真实供应商兼容性验证。静态检查中的 Host/Origin 限制、学习队列及轮转、画布布局隔离等未发现足够确定的新缺陷；这不构成无缺陷保证。
