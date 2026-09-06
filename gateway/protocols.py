"""按协议不同的那几处细节，集中在这里。

网关本身不做格式转换：下游打哪个路径，就原样转发到上游对应的路径。所以一条请求
走哪种协议，是由**它打进来的路径**决定的，跟站点无关。站点那一侧的对应物是分组
（`upstream_groups.protocol`）：那把 key 走哪种接口。两者在 `db.resolve_route()`
里汇合 —— 模型名 + 请求协议，找出该用哪个分组。

真正随协议变的只有四件事，全在下面的描述符里：结束标记、usage 字段位置、鉴权头、
以及网关自己产生错误时的错误体形状。要再加一种协议（比如 chat-completions），
写一个描述符 + 一条路由即可。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Callable

# 上游的 usage 是从字节流里正则捞的，不做完整 SSE 解析：只要认出数字就够记账，
# 而任何一次解析失败都不该影响转发本身。
Usage = tuple[int | None, int | None, int | None]   # (输入, 输出, 缓存读取)

_IN = re.compile(rb'"input_tokens":\s*(\d+)')
_OUT = re.compile(rb'"output_tokens":\s*(\d+)')
_CACHED = re.compile(rb'"cached_tokens":\s*(\d+)')
_CACHE_READ = re.compile(rb'"cache_read_input_tokens":\s*(\d+)')


def _last(pattern: re.Pattern[bytes], *bufs: bytes) -> int | None:
    """最后一次出现的值：流式 usage 会被多次改写，最后那次才是终值。"""
    for buf in reversed(bufs):
        found = pattern.findall(buf)
        if found:
            return int(found[-1])
    return None


def _largest(pattern: re.Pattern[bytes], *bufs: bytes) -> int | None:
    """所有出现里最大的那个。用于只报一次终值、但可能被别处写成占位 0/1 的字段。"""
    values = [int(v) for buf in bufs for v in pattern.findall(buf)]
    return max(values) if values else None


def openai_usage(head: bytes, tail: bytes) -> Usage:
    """Responses API 只在最后的 response.completed 里报一次 usage，看尾巴就够。"""
    return _last(_IN, tail), _last(_OUT, tail), _last(_CACHED, tail)


def anthropic_usage(head: bytes, tail: bytes) -> Usage:
    """Messages API 的 usage 被拆在流的两头。

    `input_tokens` 和 `cache_read_input_tokens` 只出现在**开头**的 message_start 里，
    终值 `output_tokens` 在**末尾**的 message_delta 里。SSE 每个 delta 事件一百多字节
    只带几个字，几百 token 的回复就能把 message_start 挤出尾部窗口 —— 所以必须头尾都留。
    """
    return (
        _largest(_IN, head, tail),
        _last(_OUT, head, tail),
        _largest(_CACHE_READ, head, tail),
    )


def openai_context(usage: Usage) -> int:
    """整个上下文有多少 token。Responses API 的 input_tokens 已经含了 cached_tokens。"""
    return usage[0] or 0


def anthropic_context(usage: Usage) -> int:
    """Messages API 的 input_tokens **不含**缓存读取（cache_read 是另一个字段），
    所以上下文得两个加起来 —— 少加一边，少掉的正好是缓存那一大半。"""
    return (usage[0] or 0) + (usage[2] or 0)


def openai_error(status: int, message: str) -> dict:
    return {"error": {"message": message, "type": "gateway_error", "code": status}}


# Anthropic 的错误体形状和 OpenAI 不一样，type 还得按状态码给对应的名字
_ANTHROPIC_ERROR_TYPE = {
    400: "invalid_request_error",
    401: "authentication_error",
    403: "permission_error",
    404: "not_found_error",
    413: "request_too_large",
    429: "rate_limit_error",
    529: "overloaded_error",
}


def anthropic_error(status: int, message: str) -> dict:
    kind = _ANTHROPIC_ERROR_TYPE.get(status, "api_error")
    return {"type": "error", "error": {"type": kind, "message": message}}


def openai_auth(api_key: str) -> dict[str, str]:
    return {"authorization": f"Bearer {api_key}"}


def anthropic_auth(api_key: str) -> dict[str, str]:
    """两种鉴权头都发。

    Anthropic 官方认 `x-api-key`，中转站大多两种都认。关键是 `x-api-key` 必须**覆盖**：
    Claude Code 自己会带一个占位 key，只设 authorization 的话那个占位值会把真 key 压掉。
    某个站只吃一种头时，用该上游的「请求头覆写」把另一个写成 null 删掉。
    """
    return {"authorization": f"Bearer {api_key}", "x-api-key": api_key}


@dataclass(frozen=True, slots=True)
class Protocol:
    name: str
    # 流结束的标记。少一个就会把正常结束的流误判成「被截断」
    end_markers: tuple[bytes, ...]
    extract_usage: Callable[[bytes, bytes], Usage]
    # usage -> 整个上下文的 token 数。两种接口的 input_tokens 含不含缓存不一样
    context_tokens: Callable[[Usage], int]
    error_body: Callable[[int, str], dict]
    auth_headers: Callable[[str], dict[str, str]]
    # 客户端没带时补上的头
    defaults: dict[str, str] = field(default_factory=dict)
    # 非空表示这个协议用这个头传 beta 开关（1M 上下文就走它）
    beta_header: str = ""


OPENAI = Protocol(
    name="openai",
    end_markers=(b"response.completed", b"[DONE]"),
    extract_usage=openai_usage,
    context_tokens=openai_context,
    error_body=openai_error,
    auth_headers=openai_auth,
)

ANTHROPIC = Protocol(
    name="anthropic",
    # message_stop 是 Messages API 的结束事件；[DONE] 是给「OpenAI 转 Anthropic」
    # 那类中转站留的，它们有时会在末尾多发一行
    end_markers=(b"message_stop", b"[DONE]"),
    extract_usage=anthropic_usage,
    context_tokens=anthropic_context,
    error_body=anthropic_error,
    auth_headers=anthropic_auth,
    defaults={"anthropic-version": "2023-06-01"},
    beta_header="anthropic-beta",
)

ALL: dict[str, Protocol] = {p.name: p for p in (ANTHROPIC, OPENAI)}


def by_name(name: str) -> Protocol:
    """按名字取描述符。认不出来时退回 OPENAI，调用方全都是「拿它拼个头」的场景，
    没有哪个值得为此抛异常。"""
    return ALL.get(name, OPENAI)
