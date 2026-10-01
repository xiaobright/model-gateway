# 旧模型编排：已淘汰的那一版，以及现在的编排页

## 结论先说

**旧编排界面（液态画布）已废弃，不要恢复。** 它当初想做的是「拖拽复制 / 移动 / 拆分候选」，
交互太重，实际用下来不满意，已在 `5164fc2` 连同专用后端接口一起移除。

**现在的编排页是另一套实现**，见 `web/canvas.js` + `web/canvas.css` + `gateway/canvas.py`，
设计与维护要点写在[维护说明的「编排画布」一节](maintenance.md#编排画布)，本文只负责「旧的那版在哪、为什么不用它」。

两版的差别不在画得好不好看，而在**表达什么**：

| | 旧版（已废） | 现在 |
| --- | --- | --- |
| 画布核心 | WebGL 液体画布，模型之间没有卡片边界 | DOM 下游气泡 + 选中模型候选链 |
| 主要操作 | 在画布上拖拽复制/移动/拆分候选 | 加气泡、挂候选、调顺序、指交接；**不做**拖拽复制 |
| 顺序怎么表达 | 候选圆片从左到右 | 选中模型下方的箭头链，前移/后移调整顺序 |
| 当前生效 | 圆片高亮 | 指针表示保存的首选，另列当前可用起点 |
| 下游→下游 | 没有 | 有（整条链交给另一个模型） |

## 去哪里找旧版

| 内容 | 提交 | 文件 |
| --- | --- | --- |
| 完整旧编排 UI、液态画布、预览和验收工具 | `1cbb2e73d61d9085ae42d18c0c32d202e1b215e1` | `web/orbit.js`、`web/orbit-layout.js`、`web/liquid-surface.js`、`web/orbit.css`、`dev/preview_liquid.py`、`dev/check_liquid_ui.mjs`、`tests/web_orbit.test.mjs` |
| 移除旧 UI、拆开上游目录与下游路由的改动 | `5164fc27776b38998ba36e710f03f526f5035fcb` | 查看该提交的 diff，理解当前结构与旧版的区别。 |
| 本次清理前的候选复制/移动后端及回归测试 | `4d1b987` | `gateway/admin.py`、`gateway/db.py`、`tests/test_route_transfer.py` |

在项目目录用 PowerShell **只读查看**，不覆盖当前文件：

```powershell
git show '1cbb2e73d61d9085ae42d18c0c32d202e1b215e1:web/orbit.js'
git show '1cbb2e73d61d9085ae42d18c0c32d202e1b215e1:dev/check_liquid_ui.mjs'
git show '4d1b987:tests/test_route_transfer.py'
git show --stat 5164fc27776b38998ba36e710f03f526f5035fcb
```

## 从旧版借东西时的约束

- 旧 UI 早于目录拆分、同名多协议和后续弹窗竞态修复，**不能直接当现行实现用**。
- 模型链用「模型名 + 接口」定位，候选用 `route_id` 定位；旧拖拽快照必须重新校验。
- 如果哪天要把「拖拽复制 / 移动」加回来，得保留事务、重复映射处理、首选修复及对应测试，
  而且不能把旧异步回调带回新界面 —— 新画布的写操作全部收在 `canvasActions` 那几个入口里。
- 历史预览脚本先检查隔离方式和 API 契约，不要直接连日常使用的网关跑验收。
