"""桥接转换失败。

单独一个模块是为了让 `request`/`response`/`stream` 都能 import 它，而 `__init__`
再把它转出去 —— 放在 `__init__` 里会造成循环导入。
"""

from __future__ import annotations


class BridgeError(Exception):
    """请求体没法转换成上游接口能吃的形状。

    这是**请求问题**，不是站点问题：proxy 收到它要回 400，且绝不能记成站点失败
    （否则一条 Codex 发来的怪请求会把整个分组打进冷却）。
    """

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message
