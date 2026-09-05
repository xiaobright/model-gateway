# Model Gateway

多公益站上游聚合网关：把多个上游站点统一成一个入口，每个模型可单独指定当前使用的上游，切换时进行中的流不中断，新请求立即路由到新上游。

透传 OpenAI 和 Anthropic 两种格式，**不做格式转换** —— 下游打哪个路径，就原样转发到上游同名的路径：

| 下游路径 | 上游路径 | 谁在用 |
| --- | --- | --- |
| `/v1/responses` | `<base>/responses` | Codex（OpenAI Responses API） |
| `/v1/messages` | `<base>/messages` | Claude Code（Anthropic Messages API） |
| `/v1/messages/count_tokens` | `<base>/messages/count_tokens` | Claude Code 用它算上下文占用 |
| `/v1/models` | — | 聚合清单，一份 JSON 同时满足两种形状 |

所以「协议」是请求的属性，不是站点的属性：一个站点同时挂着两种接口时只注册一次就够了，
不用为了两种格式把同一个 key 存两份。要再加一种协议（比如 chat-completions），
在 `gateway/protocols.py` 写一个描述符 + 一条路由即可。

## 快速开始

```
python -m venv .venv
.venv\Scripts\python -m pip install -r requirements.txt
```

启动（Windows 默认进入托盘模式，Linux/Mac 需加 `--no-tray`）：

```
.venv\Scripts\python main.py
```

或直接双击 `start-gateway.bat`。托盘图标右键菜单：打开管理页 / 开机自启开关 / 退出。

前台调试模式：

```
.venv\Scripts\python main.py --no-tray --open
```

## 供应商 / 分组 / 侧

三个概念，管理页和数据库都按这个分层：

- **供应商**（一个站）：一个 Base URL，**一个供应商一个地址**，重复的地址会被拒掉。请求头覆写和启用开关也在这一层
- **分组**（一把 key）：同一个站常常给你两把 key，各自能拉到的模型还不一样（比如专门给某个模型开的那把）。
  分组各自维护自己的 `api_key` 和模型列表，**模型候选是挂在分组上的**，切换、停用都到分组一级
- **侧**：每个模型标成 Claude 侧或 GPT 侧；每个供应商标出它有哪几种格式。给某个 Claude 模型挑供应商时，
  只列标了 Claude 的那些。侧只用于管理页的分侧和过滤，**不影响转发** —— 转发只看下游打的是哪个路径

内部一律用 `anthropic` / `openai`（和协议名对齐），界面上显示成 Claude / GPT。转发记录里的「协议」列
仍然写 `Anthropic / OpenAI`：那是「这条请求实际走的线格式」，和「这个模型属于哪一侧」是两件事。

老库（`api_key` 挂在上游行上）会在启动时自动升级：每个老上游变成一个供应商 + 一个「默认」分组，
候选跟着改指到分组，历史模型和站点全部标成 GPT 侧（今天之前网关只有 `/v1/responses`，这是事实不是猜测）。
**迁移前会先备份成 `data/gateway.db.bak-<时间戳>`**，用的是 sqlite 自己的 backup API 而不是复制文件
（WAL 模式下未落盘的内容还在 `-wal` 里，直接 cp 会拿到一个缺尾巴的库）。

同一个站不小心建成了两个供应商（两把 key 各建一个）时，展开那一行编辑分组、把「所属供应商」改成
另一个，分组连着候选一起搬过去，然后删掉空掉的那个供应商即可。

## 使用流程

1. 浏览器打开 `http://127.0.0.1:8317/`（托盘模式下自动打开）
2. 「上游站点」点「添加供应商」：Base URL 填到 `/v1` 为止，如 `https://xxx.com/v1`；这里填的 API Key
   会落到自动创建的「默认」分组上，同时勾一下这个站有 Claude / GPT 哪几种格式
3. 保存后自动接上分组弹窗 → 「拉取模型列表」（用这个分组的 key 拉）→ 勾选、选好导入为哪一侧 → 导入
4. 同一个站的另一把 key：在列表里展开那一行点「＋ 添加分组」
5. 同一个模型名在多个分组都有候选后，在「模型路由」点圆片即完成切换（高亮的是当前生效的分组）
6. 要接 Claude Code 就点「模型路由」右上角的**Claude 档位**：选供应商（只列标了 Claude 的）→ 选分组 →
   拉模型列表（档位关键字唯一命中的会自动填好）→ 一次建出四档。侧栏底部的**下游接入**里有两种客户端
   各自要填的地址和环境变量，可一键复制

管理页的分工：**模型路由**管「哪个模型走哪个分组」（分侧查看 / 切换 / 加候选 / 删候选 / 删模型 / 建档位），
**上游站点**管「站点怎么连」（地址、标记、请求头覆写，以及每个分组的 key 和模型导入），**转发记录**只读。

对下游暴露的模型清单 = 已录入的所有模型名，与上游启用状态无关。

## 下游接入

| 配置项 | 值 |
| --- | --- |
| API 地址 | `http://127.0.0.1:8317/v1`（管理页右上角可一键复制） |
| API Key | 任意值（本地服务未做鉴权） |

```
curl http://127.0.0.1:8317/v1/responses \
  -H "Content-Type: application/json" \
  -d '{"model": "gpt-test", "input": "你好"}'
```

上游返回的非 200 状态码与错误体会原样透传（比如额度用尽的 429）。网关自己产生的错误
（404 没配这个模型、502 连不上上游）按下游打的那个路径给对应形状：`/v1/responses` 给
OpenAI 的 `{"error": {...}}`，`/v1/messages` 给 Anthropic 的 `{"type": "error", "error": {...}}`。

## 接 Claude Code

Claude Code 按 haiku / sonnet / opus / fable 四个档位选模型。把这四个档位当成四个模型名录进来，
每个档位指向某个上游的真实模型 id，就能在网关里随时改档位落到哪个站、哪个模型，不用重启客户端。
管理页「模型路由」右上角的**Claude 档位**按钮就是干这个的，一次建齐四档：

| 模型名（对下游暴露） | 上游 | 上游那边的真实模型名 |
| --- | --- | --- |
| `claude-opus-5` | 公益站甲 | `claude-opus-4-1` |
| `claude-sonnet-5` | 公益站甲 | `claude-sonnet-4-5` |
| `claude-haiku-4-5` | 公益站乙 | `claude-haiku-4-5` |

客户端侧（侧栏「下游接入」里可以直接复制，模型名会按你实际录入的填）：

```
setx ANTHROPIC_BASE_URL http://127.0.0.1:8317
setx ANTHROPIC_AUTH_TOKEN whatever
setx ANTHROPIC_DEFAULT_OPUS_MODEL claude-opus-5
setx ANTHROPIC_DEFAULT_SONNET_MODEL claude-sonnet-5
setx ANTHROPIC_DEFAULT_HAIKU_MODEL claude-haiku-4-5
```

注意 `ANTHROPIC_BASE_URL` 填到端口为止，Claude Code 自己会拼 `/v1/messages`。

几个坑：

- **鉴权头**：Anthropic 官方认 `x-api-key`，中转站大多两种都认，所以分组存了 key 时网关会
  同时写 `x-api-key` 和 `Authorization: Bearer`。`x-api-key` 必须**覆盖**——Claude Code
  自己会带一个占位 key，只设 Authorization 的话那个占位值会把真 key 压掉。某个站只吃一种头时，
  用该上游的「请求头覆写」把另一个写成 `null` 删掉。
- **haiku 档很费**：Claude Code 用 haiku 档跑很多琐碎后台活（会话标题、各种小判断），量不小。
  全指到只有 opus 的站，等于用 opus 的价格干这些活；有便宜档的话让 haiku 单独指过去。
- **1M 上下文**：Claude Code 把它编码成模型名的方括号后缀（`claude-opus-5[1m]`），正常情况下
  它自己会摘掉后缀、换成 `anthropic-beta: context-1m-2025-08-07` 头。但这条路径有已知的漏网
  （auto 模式的分类器会把带后缀的名字原样发出去，anthropics/claude-code#81142）。所以网关自己
  兜了一层：**后缀只用来推断意图，绝不往上游传**——摘掉后缀再查路由，客户端没带那个 beta 头时
  补上，已经带了别的 beta 就追加而不是覆盖。配置里的「真实模型名」写成带后缀的形式也一样处理。
- **档位关键字兜底**：上面那几个环境变量漏设一个、或者以后模型改名，客户端就会发来一个没配过的
  具体 id。这时网关会按名字里的档位关键字（haiku / sonnet / opus / fable）找同档位的那条配置，
  唯一命中就落过去并记一行日志；同档位有多个名字、或者认不出档位，仍然返回 404，不瞎猜。
- **想让某一档固定按 1M 请求**：在「Claude 档位」里勾上那一行的 1M，等于把真实模型名存成
  `名字[1m]`，之后这一档每个请求都会带上 beta 头。上游不支持时会返回 4xx，别盲开。

## 设计要点

- 路由决策发生在请求进入时，切换只改 SQLite 里的一行记录，已建立的 SSE 连接不受影响，因此无需重启进程即可热切换
- **模型名改写**：请求体里唯一会被动的字段是 `model`，换成该候选配的「上游那边的真实模型名」。
  两边名字一致时继续发原始字节，所以没配改名的请求仍然是字节级透传。重新序列化用
  `ensure_ascii=False`，否则中文请求体会涨三到六倍
- **完全透明中转**：客户端请求头原样转发（仅重写 host/content-length 等必须由代理处理的），命中的分组存了 key 才覆盖鉴权头，key 留空则连鉴权头都透传
- **请求头覆写**：每个供应商可配 JSON 覆写（编辑弹窗），用于通过有客户端指纹检测的站点；值 null 表示删除该头。它在最后应用，能改掉网关自动加的任何头
- 供应商停用、分组停用都只影响新请求的路由，不改变对下游暴露的模型清单；删除当前活跃候选会自动把流量切到剩余候选之一
- 路由解析是三表 join（候选 → 分组 → 供应商），`api_key` 取自命中的**分组**再填进转发用的上游描述里，
  所以 `proxy.py` 完全不需要知道分组的存在
- 转发用一个全局复用的 httpx client，省掉每个请求一次 TLS 握手
- **将来要做自动降级的话，唯一安全的落点在 `proxy.forward` 里 `client.send()` 刚返回那几行**：
  状态码已经拿到手，但还没往下游发过任何字节，换个上游重试客户端完全无感。第一个字节一旦发出去
  就不能再换了 —— 下游会看到两段拼接的回复，而上游那边的 token 已经计费了
- 数据存于 `data/gateway.db`，端口可通过 `data/settings.json` 的 `{"port": 8317}` 覆盖
- 模型名允许带 `/`（如 `deepseek-ai/DeepSeek-V3`），所以删除走 query 参数而不是 URL 路径

## 只对本机开放

- 仅监听 `127.0.0.1`，不对局域网开放
- 另外会校验 `Host` 头必须是回环地址，挡掉 DNS rebinding（攻击者把自己的域名解析到 127.0.0.1）
- `/admin/api/*` 拒绝带跨站 `Origin` / `Sec-Fetch-Site: cross-site` 的请求：管理接口没有鉴权，
  否则任何网页都能悄悄改你的上游配置或调 `/admin/api/shutdown` 关掉进程
- 如果你把网关配到了别的主机名下（改 hosts 之类），会收到 403，属预期行为

## 转发记录与统计

管理页底部「转发记录」：最近 50 条请求的客户端（按 UA 识别）、**协议**、模型、上游、状态码、token 用量
（输入/输出/缓存命中）、耗时、备注，以及顶部的累计统计与缓存命中率。右上角的分段选择器可以只看
Anthropic 或只看 OpenAI 的记录（纯前端筛选，只把行藏起来，不重新拉数据也不打断增量渲染）。
模型那一列在配了改名时显示 `档位名 → 上游真名`，上游那一列在分组名不是「默认」时显示 `供应商 · 分组`，
否则从记录里看不出实际跑的是哪个模型、用的哪把 key。
数据持久化在 `request_log` 表，只保留最近 2000 条。`count_tokens` 不计入记录也不计入活跃流，
否则它会把累计次数和模型热度冲得没法看。备注列会标出异常结束的请求：

| 备注 | 含义 |
| --- | --- |
| 流被截断 | 上游 200 但整条流都没出现完成事件，通常是劣质上游 |
| 上游断流 | 传输中上游连接断了 |
| 客户端断开 | 下游客户端在收到完成事件**之前**就挂断了（真的中断） |
| 连不上 | 连上游都没连上 |

完成事件按协议不同：Responses API 是 `response.completed` / `[DONE]`，Anthropic Messages 是
`message_stop`。检测走滑动窗口，标记被 TCP 切成两半也能认出来。
很多上游发完完成事件后并不主动收连接，而 codex 拿到完成事件就走了，这种情况算正常收尾（记 `ok`），
不标「客户端断开」。

token 用量也是按协议分开抓的。Anthropic 把 usage 拆在流的两头：`input_tokens` 和
`cache_read_input_tokens` 只出现在**开头**的 `message_start` 里，终值 `output_tokens` 在**末尾**的
`message_delta` 里。SSE 每个 delta 事件一百多字节只带几个字，几百 token 的回复就能把 `message_start`
挤出尾部窗口，所以头尾各留一段（8KB / 64KB）。

「上游站点」表的**实测格式**列回答的是「这个站的 anthropic 接口到底能不能用」。协议是请求的属性、
不是站点的属性，所以这件事没法静态探测，只能看实际跑过的请求：每种格式显示跑过多少次，按该格式
自己的成功率上色。一个只有 claude 的站被 Codex 打过、或者反过来，在这一列一眼就能看出来是红的。

文本日志在 `data/gateway.log`（**只追加，不轮转，长了自己删**）。调试时可放一个 `data/capture.flag`，
下一个请求的请求头会被 dump 到 `data/captured_headers.json`（Authorization、Cookie、x-api-key 等敏感头
写成 `<redacted>`），dump 完 flag 自己删掉。

## 和系统代理的关系

httpx 在 Windows 上会读**注册表里的系统代理**（Clash / v2ray 那种），即使环境变量里没有
`HTTP_PROXY` 也会生效，而注册表的 bypass 列表通常是空的。也就是说：

- 转发到公益站的请求会经过你的系统代理——多数时候这正是需要的
- 但回环地址（`127.0.0.1` / `localhost` / `[::1]`）已被强制直连，否则连本机上游都要绕一圈代理
- 「连不上」类记录里的 `getaddrinfo failed` / `ConnectTimeout` 有可能是代理侧的问题，不一定是上游挂了；
  排查时先看 `data/gateway.log` 里的异常类型

## 开机自启

托盘菜单勾选「开机自启」即在 Startup 目录创建指向 `pythonw.exe main.py --tray` 的快捷方式，再次点击取消。

## 运行与退出

- 单实例：重复启动会弹窗提示，不会开出第二个托盘图标
- 正常退出用托盘菜单的「退出」，或 `curl -X POST http://127.0.0.1:8317/admin/api/shutdown`
- 注意：venv 的 pythonw 启动器在任务管理器里表现为一对父子进程（同秒创建），这是正常现象；若必须用任务管理器强杀，请右键「结束任务树」而不是只结束单个 PID
- 启动失败（如端口被占用）会以弹窗提示，不会无声消失

## 测试

```
.venv\Scripts\python -m pytest tests/ -v
```

测试起两个真实 mock 上游验证：批量导入、流式透传、**流进行中切换不断流且后续请求走新上游**、错误透传、
删除/停用候选后的路由兜底、带 `/` 的模型名可删除、重名上游返回 409、管理接口拒绝跨站，以及三种流收尾：
客户端真的中途断开（记 `client_abort`）、上游发完不收连接而客户端先走（记 `ok`）、完成标记跨块（记 `ok`）。

Anthropic 侧另外验证：模型名改写到上游真名、`x-api-key` 覆盖掉客户端的占位 key、`anthropic-version`
补默认值、`[1m]` 后缀被摘掉且 beta 头被注入（客户端已有别的 beta 时追加）、档位关键字兜底命中与不瞎猜、
`message_stop` 认成正常结束而缺失时记 `truncated`、**流长过 64KB 时输入 token 仍能从流开头抓到**、
没配改名时请求体字节级不变、`count_tokens` 转发但不进记录、`/v1/models` 同时满足两种形状、
转发记录里落下协议且上游健康按协议拆分（含「某个格式全失败」这种情况）、静态资源带 `no-store`。

供应商 / 分组另外验证：老库自动升级成新结构且候选与历史记录一条不少（含幂等重跑）、同一个供应商的
两个分组各用自己的 key 拉到不同的模型列表、两个分组之间热切换、把分组搬到另一个供应商下完成合并、
供应商停用与分组停用都能挡掉路由、唯一的分组不许删、重复 Base URL 被拒并提示去加分组、
`side` 只是元数据不影响转发。

测试客户端一律 `trust_env=False`，否则系统代理会把回环请求也接走，`connect_failed` 之类的断言会拿到
代理返回的 502。

## 前端

`web/` 没有构建步骤，浏览器直接吃原生 ES 模块：`index.html`（结构）、`style.css`（设计令牌 + 组件，
跟随系统深浅色，右上角可手动切换）、`app.js`（视图路由 + 一张 `data-act` 动作表，事件走委托）、
`views.js`（各视图的渲染）、`charts.js`（SVG 图表）、`motion.js`（动效与全局光照）、`util.js`（纯工具）。

`/static` 一律带 `cache-control: no-store`。ES 模块的 `import './views.js'` 是裸路径，没法挂
`?v=` 版本号，而只给入口挂版本号更糟：新的 `app.js` 配上缓存里的旧 `views.js`，页面会半坏不坏。
localhost 上这点带宽无所谓，代价换来「改完刷新就生效」。

弹窗 `showModal()` 后会进浏览器的 top layer，它的 `::backdrop` 带模糊，会盖住所有普通页面内容
（`z-index` 开多高都没用）。所以这两样东西也必须进 top layer 才不会被糊掉：

- **确认框**是页面内第二个 `<dialog>`，后 `showModal()` 的在上，于是它清晰、下层弹窗被模糊
- **提示条**用 `popover="manual"`，每次新提示都重新 `showPopover()` 一次，靠「后进的在上」压在弹窗之上
