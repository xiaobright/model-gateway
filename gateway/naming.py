"""模型名的解析：方括号后缀、以及档位关键字兜底。

Claude Code 把上下文档位编码成模型名的方括号后缀（`claude-opus-5[1m]`），正常路径下
它自己会摘掉后缀、换成 `anthropic-beta: context-1m-…` 头再发出来。但这条路径有已知的
漏网（auto 模式的分类器就会把带后缀的名字原样发出去，见 anthropics/claude-code#81142），
而自建网关的模型名也常被写成带后缀的形式。所以网关自己兜一层：

- 后缀只用来推断意图（要不要 1M），绝不往上游传
- 后缀摘掉之后再查路由，`claude-opus-5[1m]` 和 `claude-opus-5` 命中同一条配置
"""

from __future__ import annotations

import re

# 结尾的 [xxx]。限长是为了不把模型名里正常的方括号（如果真有）当成档位标记
_SUFFIX = re.compile(r"\[([^\[\]]{1,32})\]\s*$")

# 1M 上下文的 beta 标记；客户端没带这个头时由网关补上
BETA_1M = "context-1m-2025-08-07"
BETA_HEADER = "anthropic-beta"
_ONE_M = frozenset({"1m", "1000k", "1024k", "1048k"})

# Claude Code 的四个档位。用于「客户端发来的具体 id 没配过」时的关键字兜底
TIERS = ("haiku", "sonnet", "opus", "fable")


def split_model(raw: str) -> tuple[str, str]:
    """`'claude-opus-5[1m]'` -> `('claude-opus-5', '1m')`；没后缀时第二项是空串。"""
    text = raw.strip()
    found = _SUFFIX.search(text)
    if found is None:
        return text, ""
    return text[: found.start()].strip(), found.group(1).strip().lower()


def wants_1m(flag: str) -> bool:
    return flag in _ONE_M


def tier_of(name: str) -> str:
    """从模型名里认出档位关键字；认不出、或同时命中多个（含义不明）都返回空串。"""
    low = name.lower()
    hit = [tier for tier in TIERS if tier in low]
    return hit[0] if len(hit) == 1 else ""


def add_beta(existing: str, token: str) -> str:
    """往 anthropic-beta 头里追加一个标记，已经有了就原样返回。"""
    if not existing.strip():
        return token
    if token in existing:
        return existing
    return f"{existing},{token}"
