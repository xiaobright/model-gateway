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

import json
import re
from dataclasses import dataclass, field
from typing import Callable

# 上游的 usage 是从字节流里正则捞的，不做完整 SSE 解析：只要认出数字就够记账，
# 而任何一次解析失败都不该影响转发本身。
Usage = tuple[int | None, int | None, int | None, int | None]
# (输入, 输出, 缓存读取, 缓存创建)

_IN = re.compile(rb'"input_tokens":\s*(\d+)')
_OUT = re.compile(rb'"output_tokens":\s*(\d+)')
_CACHED = re.compile(rb'"cached_tokens":\s*(\d+)')
_CACHE_READ = re.compile(rb'"cache_read_input_tokens":\s*(\d+)')
_CACHE_CREATE = re.compile(rb'"cache_creation_input_tokens":\s*(\d+)')

# 「真正拿到手的内容有多少字节」。SSE 帧和 JSON 结构不算 —— 那些字节不是内容，
# Responses API 的流里光事件框架就几十 KB，按整条响应的字节数去折 token 会差十倍。
# 冒号后面允许空格：紧凑的 SSE 里没有，但 json.dumps 的默认输出有。
def _string_field(name: str) -> re.Pattern[bytes]:
    return re.compile(rb'"' + name.encode() + rb'":\s*"((?:[^"\\]|\\.)*)"')


_ANTHROPIC_TEXT = _string_field("text")
_ANTHROPIC_THINK = _string_field("thinking")
# Responses API 的正文、推理摘要、工具参数都走 "delta":"…"（对象形式的 delta 不匹配）
_OPENAI_TEXT = _string_field("delta")
_OPENAI_THINK = re.compile(rb"reasoning_summary")


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
    return _last(_IN, tail), _last(_OUT, tail), _last(_CACHED, tail), None


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
        _largest(_CACHE_CREATE, head, tail),
    )


def openai_context(usage: Usage) -> int:
    """整个上下文有多少 token。Responses API 的 input_tokens 已经含了 cached_tokens。"""
    return usage[0] or 0


def anthropic_context(usage: Usage) -> int:
    """Messages API 的 input_tokens **不含**缓存读取（cache_read 是另一个字段），
    所以上下文得把缓存读取和缓存创建都加上 —— 少加一边，缓存命中率就会超过 100%。"""
    return (usage[0] or 0) + (usage[2] or 0) + (usage[3] or 0)


def _total(pattern: re.Pattern[bytes], chunk: bytes) -> int:
    return sum(len(m) for m in pattern.findall(chunk))


def anthropic_content(chunk: bytes) -> tuple[int, bool]:
    """这一块里有多少字节是内容，以及里面有没有思维链。

    思维链要单独认出来是因为**它是总结过的，而计费按完整的算** —— 有思维链的那些流，
    「收到多少」和「被计多少 token」根本不是一回事，不能拿来定标尺。正文（`text_delta`）
    是完整的，所以没有思维链的流就是干净样本。
    """
    think = _total(_ANTHROPIC_THINK, chunk)
    return _total(_ANTHROPIC_TEXT, chunk) + think, think > 0


def openai_content(chunk: bytes) -> tuple[int, bool]:
    """Responses API 同理。推理摘要也是「发来的是摘要、计费按完整的算」。"""
    return _total(_OPENAI_TEXT, chunk), bool(_OPENAI_THINK.search(chunk))


def _text_size(value: object) -> int:
    return len(value.encode("utf-8")) if isinstance(value, str) else 0


def anthropic_json_content(payload: dict) -> tuple[int, bool]:
    """非流式响应只数输出 content，不能把回显的请求或其它元数据也算进去。"""
    size, thinking = 0, False
    for block in payload.get("content", []):
        kind = block.get("type")
        if kind == "text":
            size += _text_size(block.get("text"))
        elif kind in ("thinking", "redacted_thinking"):
            thinking = True
            size += _text_size(block.get("thinking"))
    return size, thinking


def openai_json_content(payload: dict) -> tuple[int, bool]:
    """Responses 的完整 JSON 用 output 数组，流式的 delta 正则在这里匹配不到。"""
    size, thinking = 0, False
    for item in payload.get("output", []):
        kind = item.get("type")
        if kind == "message":
            for part in item.get("content", []):
                size += _text_size(part.get("text")) + _text_size(part.get("refusal"))
        elif kind == "reasoning":
            thinking = True
            size += sum(_text_size(part.get("text")) for part in item.get("summary", []))
        elif kind == "function_call":
            size += _text_size(item.get("arguments"))
    return size, thinking


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
    # SSE 的事件名和独立 data 行。不能把它们当普通字符串在正文里搜索。
    end_event_types: tuple[str, ...]
    end_data_markers: tuple[str, ...]
    extract_usage: Callable[[bytes, bytes], Usage]
    # usage -> 整个上下文的 token 数。两种接口的 input_tokens 含不含缓存不一样
    context_tokens: Callable[[Usage], int]
    # 一块字节 -> (里面有多少字节是内容, 有没有思维链)
    count_content: Callable[[bytes], tuple[int, bool]]
    # 完整非流式 JSON -> 同样的内容统计；不把 JSON 当成没有帧边界的 SSE
    count_json_content: Callable[[dict], tuple[int, bool]]
    error_body: Callable[[int, str], dict]
    auth_headers: Callable[[str], dict[str, str]]
    # 有些站按客户端指纹拦截，默认就伪装成这个接口对应的官方客户端。
    # 供应商自己的「请求头覆写」能改掉或删掉这里的任何一个头。
    # 放在描述符里而不是单独一张表：它和「这个接口怎么鉴权」是同一类知识，
    # 加一种协议时不该有人记得去别处补一份
    fingerprint: dict[str, str] = field(default_factory=dict)
    # 客户端没带时补上的头
    defaults: dict[str, str] = field(default_factory=dict)
    # 非空表示这个协议用这个头传 beta 开关（1M 上下文就走它）
    beta_header: str = ""


OPENAI = Protocol(
    name="openai",
    end_event_types=("response.completed",),
    end_data_markers=("[DONE]",),
    extract_usage=openai_usage,
    context_tokens=openai_context,
    count_content=openai_content,
    count_json_content=openai_json_content,
    error_body=openai_error,
    auth_headers=openai_auth,
    fingerprint={"user-agent": "codex_cli_rs", "originator": "codex_cli_rs"},
)

ANTHROPIC = Protocol(
    name="anthropic",
    end_event_types=("message_stop",),
    # [DONE] 是给「OpenAI 转 Anthropic」那类中转站留的，它们有时会在末尾多发一行
    end_data_markers=("[DONE]",),
    extract_usage=anthropic_usage,
    context_tokens=anthropic_context,
    count_content=anthropic_content,
    count_json_content=anthropic_json_content,
    error_body=anthropic_error,
    auth_headers=anthropic_auth,
    fingerprint={"user-agent": "claude-cli/2.0.0 (external, cli)", "x-app": "cli"},
    defaults={"anthropic-version": "2023-06-01"},
    beta_header="anthropic-beta",
)

# 协议名只有这两个地方之一在登记：描述符自己。别处一律从 NAMES 派生，
# 漏改一处就会「新协议在转发侧存在、在下拉里没有」这种半吊子状态
ALL: dict[str, Protocol] = {p.name: p for p in (ANTHROPIC, OPENAI)}
NAMES: tuple[str, ...] = tuple(ALL)


def by_name(name: str) -> Protocol:
    """按名字取描述符。认不出来时退回 OPENAI，调用方全都是「拿它拼个头」的场景，
    没有哪个值得为此抛异常。"""
    return ALL.get(name, OPENAI)


class SSEObserver:
    """按完整 SSE 事件观察流，不改变也不缓存要转发的原始字节。

    网络 chunk 只是传输层分片，不能拿它当事件边界。未结束的帧留在自己的 buffer 里，
    直到下一次 feed 补齐；观察失败最多少一条统计，不应影响 relay 原样转发。
    """

    def __init__(self, proto: Protocol) -> None:
        self.proto = proto
        self._buffer = bytearray()
        self.ended = False
        self.text_bytes = 0
        self.thinking = False

    def feed(self, chunk: bytes) -> None:
        self._buffer.extend(chunk)
        while True:
            match = re.search(rb"\r?\n\r?\n", self._buffer)
            if match is None:
                return
            frame = bytes(self._buffer[: match.start()])
            del self._buffer[: match.end()]
            self._observe_frame(frame)

    def _observe_frame(self, frame: bytes) -> None:
        event = ""
        data: list[bytes] = []
        for line in frame.splitlines():
            if line.startswith(b":"):
                continue
            if line.startswith(b"event:"):
                event = line[6:].lstrip().decode("utf-8", "ignore")
            elif line.startswith(b"data:"):
                data.append(line[5:].lstrip())

        if not data and not event:
            return
        data_bytes = b"\n".join(data)
        try:
            got, think = self.proto.count_content(frame)
            self.text_bytes += got
            self.thinking = self.thinking or think
        except Exception:
            # 统计只是观察，坏 JSON / 奇怪编码不能让下游断流。
            pass

        if event in self.proto.end_event_types:
            self.ended = True
            return
        if event:
            # 有 event 头时以它为准；否则一个正文里的 type 字段可能把非结束事件
            # 错当成结束。没有 event 头的上游才使用 data JSON 的 type 兜底。
            return
        data_text = data_bytes.decode("utf-8", "ignore").strip()
        if data_text in self.proto.end_data_markers:
            self.ended = True
            return
        try:
            payload = json.loads(data_text)
        except (TypeError, ValueError):
            return
        if isinstance(payload, dict) and payload.get("type") in self.proto.end_event_types:
            self.ended = True
