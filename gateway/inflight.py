"""正在跑的请求：一张内存登记表，「实时」那一页的全部数据来源。

以前这里只有两个计数器（几条在跑、其中几条流式），除了数字什么都没留下 ——
而想知道的恰恰是那些数字答不了的问题：现在在打哪个站、卡在连接还是在等模型出字、
这条请求前面已经被哪几个站拒过。所以改成登记每条请求本身。

生命周期由 proxy.forward 驱动：
  begin()     解析出模型之后（400/404 那种连上游都没碰的不登记）
  set_route() 每次换候选时一次 —— 真名和 key 都是按候选重建的
  phase()     拿到状态码、开始往下游发字节时各一次
  failed()    这次尝试没成、要换下一个候选：记进 trail，客户端看不见但钱花了
  finish()    relay 的 finally 里，转成「刚结束」再留一会儿

「刚结束」要留一会儿是有意的：不留的话空闲时这页整个是空的（Claude Code 两轮请求
之间隔几十秒），而降级轨迹恰恰只在请求活着的那几秒存在，最想看的东西最看不到。

只在内存里，重启即清零；这里没有一样东西值得写库。
"""

from __future__ import annotations

import asyncio
import itertools
import threading
import time
from collections.abc import Awaitable
from dataclasses import dataclass, field
from typing import Any

from .reqlog import log

# 结束的请求再留这么久 / 最多留几条
KEEP_SECONDS = 90.0
KEEP_ROWS = 24

# 活跃条目超过这个岁数就当漏了扫掉。正常情况 relay 的 finally 一定会注销，但
# 「响应刚返回、生成器还没被迭代过就被取消」那一瞬够不到 finally（没启动过的生成器
# close() 不执行函数体），漏一条就会在页面上挂个永不消失的幽灵。
# 阈值取读超时（600s）再宽一倍。
STALE_SECONDS = 720.0

# 阶段。connect 和 wait 必须分开：卡在 connect 是站连不上（或代理问题），
# 卡在 wait 是模型在想 —— 同样的「12 秒没动静」，处置完全不同
CONNECT = "connect"
WAIT = "wait"
STREAM = "stream"
DONE = "done"


@dataclass
class Call:
    id: int
    started: float                  # monotonic
    client: str                     # 按 UA 认出来的下游客户端
    protocol: str
    model: str                      # 下游要的那个名字
    stream: bool
    req_bytes: int
    meta: bool = False              # count_tokens 这类元数据请求，不计入「进行中」
    # 当前这次尝试打的是谁
    attempt: int = 1
    upstream: str = ""
    group_name: str = ""
    group_id: int = 0
    remote_model: str = ""
    phase: str = CONNECT
    status: int = 0
    sent: int = 0                   # 已经转给下游多少字节
    # 这些字节里有多少是**内容**（SSE 帧和 JSON 结构不算），以及里面有没有思维链。
    # 思维链发来的是总结、计费按完整的算，所以「收到多少」和「计费多少」是两个数
    text_bytes: int = 0
    thinking: bool = False
    # 上游自己报的 token 数（0 = 还没报）。有真数就不用按字节估了 —— Anthropic 把输入
    # token 放在流开头的 message_start 里，所以它往往在第一块字节里就到手了
    tokens_in: int = 0
    tokens_out: int = 0
    trail: list[dict[str, Any]] = field(default_factory=list)   # 前面失败掉的那几次
    note: str = ""
    done_at: float = 0.0
    cancel_requested: bool = False
    # 只取消这条请求正在等的上游操作，不碰服务任务或其它请求。
    pending: asyncio.Future | None = field(default=None, repr=False)

    @property
    def elapsed_ms(self) -> int:
        end = self.done_at or time.monotonic()
        return int((end - self.started) * 1000)


_calls: dict[int, Call] = {}
_ids = itertools.count(1)
# 写发生在转发（事件循环线程），读发生在管理接口（FastAPI 对同步端点用线程池）——
# 一边迭代、一边增删会让 /inflight、/stats 随机 500。RLock 允许 snapshot/_sweep 嵌套。
_lock = threading.RLock()


class ManualAbort(Exception):
    """用户从实时页中断了这条请求。"""


class UpstreamStall(Exception):
    """上游在发呆超时内没有给出响应头或下一个字节。"""


async def wait_for_upstream(
    call: Call, operation: Awaitable[Any], timeout: float | None = None
) -> Any:
    """登记当前上游等待，让管理接口能立即取消卡住的 send/read。

    timeout 给了就是发呆超时：这段时间内没等到结果就取消等待并抛 UpstreamStall，
    由调用方决定是换候选还是切断这条流；None = 不限时（等响应头、等首字都用它）。
    """
    pending = asyncio.ensure_future(operation)
    call.pending = pending
    if call.cancel_requested:
        pending.cancel()
    try:
        if timeout is not None:
            return await asyncio.wait_for(pending, timeout)
        return await pending
    except asyncio.CancelledError:
        if call.cancel_requested:
            raise ManualAbort from None
        raise
    except (asyncio.TimeoutError, TimeoutError):
        raise UpstreamStall from None
    finally:
        call.pending = None


def cancel(call_id: int) -> bool:
    """由 async 管理接口在网关的事件循环里调用；重复中断无副作用。"""
    with _lock:
        call = _calls.get(call_id)
        if call is None or call.done_at:
            return False
        if not call.cancel_requested:
            call.cancel_requested = True
            if call.pending is not None:
                call.pending.cancel()
        return True


# ---------------------------------------------------------------- 写（只有 proxy 调）


def begin(
    *,
    client: str,
    protocol: str,
    model: str,
    stream: bool,
    req_bytes: int,
    meta: bool = False,
) -> Call:
    with _lock:
        _sweep()
        call = Call(
            id=next(_ids), started=time.monotonic(), client=client, protocol=protocol,
            model=model, stream=stream, req_bytes=req_bytes, meta=meta,
        )
        _calls[call.id] = call
    return call


def set_route(
    call: Call | None,
    *,
    attempt: int,
    upstream: str,
    group_name: str,
    group_id: int,
    remote_model: str,
    req_bytes: int,
) -> None:
    """现在开始打这个候选。"""
    if call is None:
        return
    call.attempt = attempt
    call.upstream = upstream
    call.group_name = group_name
    call.group_id = group_id
    call.remote_model = remote_model
    call.req_bytes = req_bytes
    call.phase = CONNECT
    call.status = 0
    # 换了候选就是换了一个站在算账，上一个报的数不作数
    call.tokens_in = 0
    call.tokens_out = 0
    call.text_bytes = 0
    call.thinking = False


def phase(call: Call | None, name: str, *, status: int = 0) -> None:
    if call is None:
        return
    call.phase = name
    if status:
        call.status = status


def usage(call: Call | None, *, tokens_in: int = 0, tokens_out: int = 0) -> None:
    """上游自己报的 token 数。

    取大的那个：Anthropic 在流开头的 message_start 里就报了输入 token，而那串数字
    可能正好被切在两块字节之间，先读到的是残缺的前几位。头填满之前会反复来试，
    完整的那个一定更大。
    """
    if call is None:
        return
    call.tokens_in = max(call.tokens_in, tokens_in)
    call.tokens_out = max(call.tokens_out, tokens_out)


def progress(call: Call | None, sent: int, *, text_bytes: int = 0, thinking: bool = False) -> None:
    if call is None:
        return
    call.sent = sent
    call.text_bytes = text_bytes
    call.thinking = call.thinking or thinking


def failed(call: Call | None, *, status: int, note: str, ms: int) -> None:
    """这次尝试没成，要换下一个候选了。客户端看不见它，但它真花了钱，所以留痕。"""
    if call is None:
        return
    call.trail.append(
        {
            "attempt": call.attempt,
            "upstream": call.upstream,
            "group_name": call.group_name,
            "remote_model": call.remote_model,
            "status": status,
            "note": note,
            "ms": ms,
        }
    )


def finish(
    call: Call | None,
    *,
    status: int = 0,
    note: str = "",
    sent: int = 0,
    tokens_in: int = 0,
    tokens_out: int = 0,
) -> None:
    if call is None:
        return
    call.done_at = time.monotonic()
    call.phase = DONE
    call.note = note
    call.sent = sent or call.sent
    if status:
        call.status = status
    usage(call, tokens_in=tokens_in, tokens_out=tokens_out)
    _trim()


def reset() -> None:
    with _lock:
        _calls.clear()


# ---------------------------------------------------------------- 清理


def _sweep() -> None:
    """漏掉的活跃条目扫走。真漏了得看得见，所以记一行日志而不是默默删。"""
    with _lock:
        now = time.monotonic()
        stale = [c for c in _calls.values() if not c.done_at and now - c.started > STALE_SECONDS]
        for call in stale:
            _calls.pop(call.id, None)
        for call in stale:
            log(
                f"  inflight: 清掉一条挂了 {int(now - call.started)}s 的登记 "
                f"model={call.model!r} upstream={call.upstream!r} phase={call.phase}"
            )


def _trim() -> None:
    """结束的条目按时间和条数各裁一刀。"""
    with _lock:
        now = time.monotonic()
        done = sorted(
            (c for c in _calls.values() if c.done_at), key=lambda c: c.done_at, reverse=True
        )
        for i, call in enumerate(done):
            if i >= KEEP_ROWS or now - call.done_at > KEEP_SECONDS:
                _calls.pop(call.id, None)


# ---------------------------------------------------------------- 读


def counts() -> dict[str, int]:
    """给侧栏和 KPI 用的那两个数。

    元数据请求（count_tokens）不计入：它是 Claude Code 自己算上下文占用用的，
    又多又快，混进来「进行中」就没法当忙闲指示看了。
    """
    with _lock:
        live = [c for c in _calls.values() if not c.done_at and not c.meta]
        return {"requests": len(live), "streams": sum(1 for c in live if c.stream)}


def _as_dict(call: Call) -> dict[str, Any]:
    return {
        "id": call.id,
        "client": call.client,
        "protocol": call.protocol,
        "model": call.model,
        "remote_model": call.remote_model,
        "stream": call.stream,
        "meta": call.meta,
        "attempt": call.attempt,
        "upstream": call.upstream,
        "group_name": call.group_name,
        "group_id": call.group_id,
        "phase": call.phase,
        "status": call.status,
        "req_bytes": call.req_bytes,
        "sent": call.sent,
        "text_bytes": call.text_bytes,
        "thinking": call.thinking,
        "tokens_in": call.tokens_in,
        "tokens_out": call.tokens_out,
        "elapsed_ms": call.elapsed_ms,
        "trail": list(call.trail),
        "note": call.note,
        "cancel_requested": call.cancel_requested,
    }


def snapshot() -> dict[str, Any]:
    """在跑的 + 刚结束的。

    在跑的按开始时间正序：最先来的排最上，位置稳定，不会因为来了新请求就把
    正在看的那张卡片挤下去。刚结束的反过来，最近的在前。
    """
    with _lock:
        _sweep()
        _trim()
        live = sorted((c for c in _calls.values() if not c.done_at), key=lambda c: c.started)
        done = sorted((c for c in _calls.values() if c.done_at), key=lambda c: -c.done_at)
        return {
            "calls": [_as_dict(c) for c in live],
            "recent": [_as_dict(c) for c in done],
            "counts": counts(),
        }
