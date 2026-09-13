# 旧模型编排：Git 历史查阅指引

旧编排界面已废弃，目前没有确定的新方案。当前版本不保留隐藏界面或专用后端：
`POST /admin/api/models/transfer`、旧 `/models/bulk-add` 及对应实现/专用测试已移除。
上游模型目录和下游候选路由继续分开管理。

以后重做时，可以从 Git 历史借用布局、交互和事务处理思路，减少从零摸索，不必现在维护两套实现。

## 去哪里找

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

## 将来重做时保留的约束

- 先确定想改善的实际操作，再选旧代码片段；不为了复用旧画布恢复全部接口和状态。
- 旧 UI 早于目录拆分、同名多协议和后续弹窗竞态修复，不能直接当作现行实现。
- 模型链用「模型名 + 接口」定位，候选用 `route_id` 定位；旧拖拽快照必须重新校验。
- 复制/移动如果重新加入，应保留事务、重复映射处理、首选修复及对应测试，且不能把旧异步回调带回新界面。

历史预览脚本先检查隔离方式和 API 契约，不要直接连日常使用的网关运行验收。
