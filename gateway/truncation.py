"""200 截断守卫：同一个站的同一个模型连着拿回「没有正文的 200」时，下一次先把响应
扣在网关里，判定还是坏的就在原站原样重发；直到拿到真正能用的流，或者重发次数用完。

要解决的问题（真库里的形状）：anyrouter / AgentRouter 这类公益站排队排不进去时回
**200**，正文是一段和请求无关的错误信封（`rate limit exceeded: ... exceeded token
rate limit`），既没有完成事件、也没有一个字的正文。网关如实转发，客户端（codex）
只看到「流被截断」；它自己重试 5 次全是这一段，于是整轮停在半路要人手动继续。

为什么不能复用现成的两条路：

- **同站重试**（`failover.same_retry_delay`）认的是 HTTP 状态码，而这种情况状态码是
  200 —— 按状态码看它「成功了」，规则根本匹配不上。
- **自动降级换候选**只能在「还没往下游发过一个字节」时做，而 200 截断要等整条流读完
  才知道。所以这里换一个落点：**先扣住，判定它是坏的就不发下去**。

扣住的开销控制在一个字上：**一见到正文就放行**。正常流式第一个字一到就交给下游，
不额外增加等待；只有「一个正文字节都没有」的那种响应会被扣到读完。所以这条规则
拦的正好是「挤不进去 / 被限流」这一类空响应，而不是正常的慢回答。

## 为什么触发条件是「站 + 模型 + 端点」，而不是「同一份请求」

一开始按**整份请求体的摘要**认「这是同一条请求」，真跑起来不成立。2026-10-01 实测
（codex 在 anyrouter 上连着重试）：6 次重试的请求体字节数完全相同（1005868B），
**内容却每次都不一样** —— 8 次空响应留下 8 个不同的指纹、每个计数都是 1，守卫于是
永远不武装，功能形同不存在。响应那一侧同样：6 次都是 129881 字节，摘要却两两不同
（错误信封里显然带着每次变化的 request id / 时间戳）。

所以「连着坏」只认**站 + 模型 + 端点**这三样（站和模型正是上游限流报错里点名的
对象），不再要求请求体逐字节相同。响应摘要仍然记着，但它只服务于可选的 `same_body`
规则和排查 —— 在那两个站上那条规则**不会**命中，别开。

端点也算进键里是因为它有实际意义：Codex 的 standalone search、compaction 和主对话
都会打到同一个模型上，但成功一次搜索不代表主对话也能拿到东西；分开计数才不会
「被一次无关的成功悄悄解除拦截」。

## 状态

只在内存里（键是「分组 + 模型 + 端点」），重启即忘，和分组冷却一样：坏的是那一段
时间的那个站，不是这台机器上的什么持久事实。
"""

from __future__ import annotations

import json
import threading
from collections import OrderedDict
from dataclasses import dataclass

from .reqlog import log

# 一次扣住最多这么多字节。真出问题的那种错误信封只有一两百 KB；到顶就说明「这不是
# 我们要拦的那种短空响应」，直接交给下游继续实时转发，别把长回答整条憋在内存里。
MAX_HOLD_BYTES = 8 * 1024 * 1024

# 记住多少条（分组 + 模型 + 端点）。出问题的站就那么几个，给一个上限、超了淘汰最旧的。
MAX_ENTRIES = 256

# 上游配置里的默认值：连着坏 2 次开始扣；每次扣住最多在原站再发 3 把
DEFAULT_AFTER = 2
DEFAULT_TIMES = 3
MAX_AFTER = 10
MAX_TIMES = 10
MAX_DELAY_MS = 30_000

# 键：同一个站的同一把 key（分组）上，同一个模型、同一种端点
GuardKey = tuple[int, str, str]


def parse_rules(raw: str) -> dict[str, object] | None:
    """上游的 hold_retry 配置；空串 / 坏 JSON 一律当没配（None）。

    形状：``{"after":2,"times":3,"delay_ms":0}``

    - ``after``：连着几次「2xx 且没有完成事件、也没有正文」之后开始扣住
    - ``times``：每次扣住最多在原站再发几把
    - ``delay_ms``：重发前等多久
    - ``same_body``：是否要求这几次的**响应字节完全相同**才扣住。默认关，而且在这两个
      公益站上**必然不命中** —— 它们的错误信封每次内容都不一样（长度倒是固定），
      见模块开头的实测记录。

    转发路径上不能因为配置脏了就把请求打炸，所以这里从不抛异常；管理接口保存时校验。
    """
    if not raw or not raw.strip():
        return None
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError):
        return None
    if not isinstance(parsed, dict):
        return None
    try:
        after = int(parsed.get("after", DEFAULT_AFTER))
        times = int(parsed.get("times", DEFAULT_TIMES))
        delay_ms = int(parsed.get("delay_ms", 0))
    except (TypeError, ValueError):
        return None
    return {
        "after": min(MAX_AFTER, max(1, after)),
        "times": min(MAX_TIMES, max(1, times)),
        "delay_ms": min(MAX_DELAY_MS, max(0, delay_ms)),
        "same_body": parsed.get("same_body") is True,
    }


@dataclass
class _Entry:
    """一个「站 + 模型 + 端点」连着坏了几次、坏的是不是同一段响应。"""

    fails: int = 0
    digest: str = ""     # 这一串里**第一次**坏响应的摘要
    same: bool = True    # 后面几次的摘要是不是都和第一次一样
    held: int = 0        # 累计扣住重发过几次，只用来展示


_entries: OrderedDict[GuardKey, _Entry] = OrderedDict()
# 写发生在转发（事件循环线程），读发生在管理接口（FastAPI 同步端点用线程池）——
# 一边迭代一边增删会让 /inflight、/failover 随机 500，所以和 failover 一样加锁。
_lock = threading.RLock()


def note_bad(group_id: int, model: str, endpoint: str, digest: str) -> int:
    """记一次坏结果：2xx、没有完成事件、也没有一个正文字节。返回新的连续次数。

    计数的单位是「这个站的这个模型连着坏了几次」，**不要求请求体或响应体逐字节相同**：
    客户端重试时请求体并不一样，上游的错误信封也每次都不一样（模块开头有实测）。
    要求响应相同的是 `same_body` 那条可选规则，它看的是下面这个 same 标志。

    一次正常回复（`note_ok`）才把这串清零。
    """
    key = (group_id, model, endpoint)
    with _lock:
        st = _entries.get(key)
        if st is None:
            st = _Entry()
            _entries[key] = st
        if st.fails == 0:
            st.digest = digest      # 这一串的第一段，same_body 拿它比
            st.same = True
        elif st.digest != digest:
            # 连着坏，但不是同一段响应。默认口径照样算「连着」，只有配了
            # same_body 的站才因此不武装（见 armed）
            st.same = False
        st.fails += 1
        _entries.move_to_end(key)
        while len(_entries) > MAX_ENTRIES:
            _entries.popitem(last=False)
        return st.fails


def note_ok(group_id: int, model: str, endpoint: str) -> None:
    """这一发正常收尾了：忘掉这条记录，下次不再扣。"""
    with _lock:
        st = _entries.pop((group_id, model, endpoint), None)
        if st is not None and st.held:
            log(
                f"  truncation: g{group_id} {model} {endpoint} 的截断记录恢复正常"
                f"（之前扣住重发过 {st.held} 次）"
            )


def note_held(group_id: int, model: str, endpoint: str) -> None:
    """扣住并决定重发一次。"""
    with _lock:
        st = _entries.get((group_id, model, endpoint))
        if st is not None:
            st.held += 1


def armed(group_id: int, model: str, endpoint: str, rules: dict[str, object]) -> bool:
    """这个站的这个模型现在该不该扣住？"""
    with _lock:
        st = _entries.get((group_id, model, endpoint))
        if st is None or st.fails < int(rules["after"]):
            return False
        if rules["same_body"] and not st.same:
            return False
        return True


def fails(group_id: int, model: str, endpoint: str) -> int:
    """连着坏了几次（日志文案用）。"""
    with _lock:
        st = _entries.get((group_id, model, endpoint))
        return st.fails if st is not None else 0


def reset() -> None:
    with _lock:
        _entries.clear()


def snapshot() -> list[dict[str, object]]:
    """给管理接口/排查用：现在拦着哪几个「站 + 模型 + 端点」、坏了几次、扣过几次。"""
    with _lock:
        out = []
        for (group_id, model, endpoint), st in _entries.items():
            out.append(
                {
                    "group_id": group_id,
                    "model": model,
                    "endpoint": endpoint,
                    "fails": st.fails,
                    "same_body": st.same,
                    "digest": st.digest,
                    "held": st.held,
                }
            )
        return sorted(out, key=lambda r: (-int(r["fails"]), int(r["group_id"]), str(r["model"])))
