"""200 截断守卫：同一份请求连着拿回「同一个坏 200」时，下一次先把响应扣在网关里，
判定还是坏的就在原站原样重发；直到拿到真正能用的流，或者重发次数用完。

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

状态只在内存里（键是「分组 + 请求体指纹」），重启即忘，和分组冷却一样：坏的是那
一段时间的那个站，不是这台机器上的什么持久事实。
"""

from __future__ import annotations

import hashlib
import json
import threading
from collections import OrderedDict
from dataclasses import dataclass

from .reqlog import log

# 一次扣住最多这么多字节。真出问题的那种错误信封只有一两百 KB；到顶就说明「这不是
# 我们要拦的那种短空响应」，直接交给下游继续实时转发，别把长回答整条憋在内存里。
MAX_HOLD_BYTES = 8 * 1024 * 1024

# 记住多少个（分组 + 请求指纹）。指纹是完整请求体的摘要，同一个模型每轮请求体都不一样，
# 这里只需要留住最近出问题的那几条，所以给一个上限、超了淘汰最旧的。
MAX_ENTRIES = 256

# 上游配置里的默认值：连着坏 2 次开始扣；每次扣住最多在原站再发 3 把
DEFAULT_AFTER = 2
DEFAULT_TIMES = 3
MAX_AFTER = 10
MAX_TIMES = 10
MAX_DELAY_MS = 30_000


def fingerprint(body: bytes) -> str:
    """请求体指纹。客户端自己重试时是原样重发，所以会得到同一个值。"""
    return hashlib.sha1(body).hexdigest()[:16]


def parse_rules(raw: str) -> dict[str, object] | None:
    """上游的 hold_retry 配置；空串 / 坏 JSON 一律当没配（None）。

    形状：``{"after":2,"times":3,"delay_ms":0,"same_body":false}``

    - ``after``：连着几次「200 且没有完成事件、也没有正文」之后开始扣住
    - ``times``：每次扣住最多在原站再发几把
    - ``delay_ms``：重发前等多久
    - ``same_body``：是否要求这几次的**响应字节也完全相同**才扣住。默认关 ——
      错误信封里常带 request id / 时间戳，逐字节相同这个条件在真站上很容易永远不成立，
      而「连着几次 200 且一个字都没有」本身已经是很窄的签名了。

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
    """一份请求体连着坏了几次、坏的是不是同一段响应。"""

    fails: int = 0
    digest: str = ""     # 这一串里**第一次**坏响应的摘要
    same: bool = True    # 后面几次的摘要是不是都和第一次一样
    held: int = 0        # 累计扣住重发过几次，只用来展示
    model: str = ""      # 下游模型名，只为了让日志和管理接口看得懂这串哈希是什么


_entries: OrderedDict[tuple[int, str], _Entry] = OrderedDict()
# 写发生在转发（事件循环线程），读发生在管理接口（FastAPI 同步端点用线程池）——
# 一边迭代一边增删会让 /inflight、/failover 随机 500，所以和 failover 一样加锁。
_lock = threading.RLock()


def note_bad(group_id: int, request_fp: str, digest: str, model: str = "") -> int:
    """记一次坏结果：状态码 2xx、没有完成事件、也没有一个正文字节。返回新的连续次数。

    计数的单位是「这份请求连着坏了几次」，**不要求每次的响应字节一样**：错误信封里
    常带 request id / 时间戳，真站上逐字节相同很容易永远不成立。要求逐字节相同的是
    `same_body` 那条可选规则，它看的是下面这个 same 标志。

    一次成功（`note_ok`）才把这串清零；出了正文的回答走的是 `note_ok`，见 proxy.relay。
    """
    key = (group_id, request_fp)
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
        if model:
            st.model = model
        _entries.move_to_end(key)
        while len(_entries) > MAX_ENTRIES:
            _entries.popitem(last=False)
        return st.fails


def note_ok(group_id: int, request_fp: str) -> None:
    """这一发正常收尾了：忘掉这份请求的坏记录，下次不再扣。"""
    with _lock:
        st = _entries.pop((group_id, request_fp), None)
        if st is not None and st.held:
            log(f"  truncation: g{group_id} 的截断记录恢复正常（之前扣住重发过 {st.held} 次）")


def note_held(group_id: int, request_fp: str) -> None:
    """扣住并决定重发一次。"""
    with _lock:
        st = _entries.get((group_id, request_fp))
        if st is not None:
            st.held += 1


def armed(group_id: int, request_fp: str, rules: dict[str, object]) -> bool:
    """这一份请求现在该不该扣住？"""
    with _lock:
        st = _entries.get((group_id, request_fp))
        if st is None or st.fails < int(rules["after"]):
            return False
        if rules["same_body"] and not st.same:
            return False
        return True


def fails(group_id: int, request_fp: str) -> int:
    """连着坏了几次（日志文案用）。"""
    with _lock:
        st = _entries.get((group_id, request_fp))
        return st.fails if st is not None else 0


def reset() -> None:
    with _lock:
        _entries.clear()


def snapshot() -> list[dict[str, object]]:
    """给管理接口/排查用：当前记住了哪些请求体、坏了几次、扣住过几次。"""
    with _lock:
        out = []
        for (group_id, request_fp), st in _entries.items():
            out.append(
                {
                    "group_id": group_id,
                    "model": st.model,
                    "fingerprint": request_fp,
                    "fails": st.fails,
                    "same_body": st.same,
                    "digest": st.digest,
                    "held": st.held,
                }
            )
        return sorted(out, key=lambda r: (-int(r["fails"]), r["group_id"]))
