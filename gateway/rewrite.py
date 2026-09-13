"""上游敏感词绕行：转发前按规则替换请求体里的文本。

上游（站A 就是）的内容审核是黑盒，会误伤固定提示词 —— opencode 的会话标题
生成器里就有两行每轮必中 500，而上游不会告诉你命中了哪个词，网关也猜不出来。所以这里
做的事很朴素：**踩到一条就配一条**。

规则是纯字面替换（``from`` → ``to``），只动请求体里的字符串，不动 JSON 结构::

    [{"from": "<上游误报的原文>", "to": "<改成什么样子>"}]

**触发词原文一律不写进代码/文档**：它唯一的合法存放地是规则本身（管理页「上游站点 →
敏感词绕行」，落在库的 settings 表里）。写在这里的话，拿这份仓库去做总结、检索、上下文
都会被它污染，等于把雷又种回去。定位方法见 ``dev/find-sensitive-line.py``。

``to`` 给空串就是删掉那段。默认空表 = 什么都不做。

两条硬边界：

- **会改你的请求内容**，所以必须显式配置才生效，默认一行规则都没有。
- 替换对请求体里**所有字符串**生效（messages、system、工具描述都在内），规则得写得
  足够具体；写个 ``"an"`` 进去会把请求毁掉，这是配规则的人自己的责任。

不是「过滤层」：网关不可能预知上游的黑名单，这是**已知误报的绕行层**。
"""

from __future__ import annotations

import json
import threading
from typing import Any

from . import db

SETTING_KEY = "rewrite_rules"
MAX_RULES = 200

# 规则表每次转发都要读一次；sqlite 已经在每条请求上被查过（resolve_chain），
# 不差这一下，但没必要每条请求都重新 parse 一遍 JSON，所以按内容缓存解析结果。
# 读在转发线程、保存在管理接口线程池，加锁免得一边写缓存一边被读到半成品。
_cache: dict[str, Any] = {"raw": None, "rules": []}
_lock = threading.Lock()


def raw() -> str:
    return db.get_setting(SETTING_KEY, "") or ""


def rules() -> list[dict[str, str]]:
    """当前规则表。坏 JSON / 空表都按「没有规则」处理 —— 配错了不能拖垮转发。"""
    text = raw()
    with _lock:
        if _cache["raw"] == text:
            return _cache["rules"]
    parsed: list[dict[str, str]] = []
    if text.strip():
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            data = None
        if isinstance(data, list):
            for item in data:
                if not isinstance(item, dict):
                    continue
                old = item.get("from")
                if not isinstance(old, str) or not old:
                    continue
                new = item.get("to", "")
                parsed.append({"from": old, "to": new if isinstance(new, str) else ""})
    with _lock:
        _cache["raw"] = text
        _cache["rules"] = parsed
    return parsed


def save(items: list[dict[str, str]]) -> list[dict[str, str]]:
    db.set_setting(SETTING_KEY, json.dumps(items, ensure_ascii=False, indent=2))
    with _lock:
        _cache["raw"] = None  # 下次 rules() 重新读
    return rules()


def _pairs() -> list[tuple[str, str]]:
    return [(r["from"], r["to"]) for r in rules() if r["from"] != r["to"]]


def _rewrite_text(text: str, pairs: list[tuple[str, str]]) -> tuple[str, int]:
    hits = 0
    for old, new in pairs:
        if old in text:
            text = text.replace(old, new)
            hits += 1
    return text, hits


def _walk(node: Any, pairs: list[tuple[str, str]], hits: list[int]) -> Any:
    """递归重建；没有命中就原样返回，避免为 6 万字节的请求体做无谓的拷贝。"""
    if isinstance(node, str):
        new, n = _rewrite_text(node, pairs)
        if n:
            hits[0] += n
            return new
        return node
    if isinstance(node, dict):
        out = {}
        changed = False
        for key, value in node.items():
            new_value = _walk(value, pairs, hits)
            if new_value is not value:
                changed = True
            out[key] = new_value
        return out if changed else node
    if isinstance(node, list):
        out = []
        changed = False
        for value in node:
            new_value = _walk(value, pairs, hits)
            if new_value is not value:
                changed = True
            out.append(new_value)
        return out if changed else node
    return node


def apply(payload: dict[str, Any]) -> tuple[dict[str, Any] | None, int]:
    """返回 (新 payload 或 None, 命中次数)。没配规则 / 没命中就返回 (None, 0)。"""
    pairs = _pairs()
    if not pairs:
        return None, 0
    hits = [0]
    new_payload = _walk(payload, pairs, hits)
    if hits[0] == 0:
        return None, 0
    return new_payload, hits[0]
