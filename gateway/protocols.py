"""按协议不同的那几处细节，集中在这里。

网关本身不做格式转换：下游打哪个路径，就原样转发到上游对应的路径。所以一条请求
走哪种协议，是由**它打进来的路径**决定的，跟站点无关。站点那一侧的对应物是分组
（`upstream_groups.protocol`）：那把 key 走哪种接口。两者在 `db.resolve_route()`
里汇合 —— 模型名 + 请求协议，找出该用哪个分组。

真正随协议变的细节，以及管理页需要展示的元数据，都在下面的描述符里：结束标记、usage
字段位置、鉴权头、错误体形状、客户端提示和能力标记。要再加一种直通格式，仍需写描述符、
显式路由和对应行为测试；本模块不负责格式转换。
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
# Chat Completions 用另一套字段名（prompt/completion），缓存数仍叫 cached_tokens
_PROMPT = re.compile(rb'"prompt_tokens":\s*(\d+)')
_COMPLETION = re.compile(rb'"completion_tokens":\s*(\d+)')
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
# Chat Completions 的正文在 delta.content；思维链字段各站不统一（DeepSeek 用
# reasoning_content，有的站叫 reasoning），两个都认
_CHAT_TEXT = _string_field("content")
_CHAT_THINK = re.compile(rb'"(?:reasoning_content|reasoning)":\s*"((?:[^"\\]|\\.)*)"')


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


def chat_usage(head: bytes, tail: bytes) -> Usage:
    """Chat Completions 的 usage 在流最后一块（或非流式整个 JSON）里。

    OpenAI 官方流式要请求带 `stream_options.include_usage` 才报 usage；兼容站大多默认
    也报。网关不替客户端改请求体，没报就退回按字节估。
    """
    return _last(_PROMPT, head, tail), _last(_COMPLETION, head, tail), _last(_CACHED, head, tail), None


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


def chat_context(usage: Usage) -> int:
    """Chat Completions 的 prompt_tokens 同样已经含了 cached_tokens（后者是它的明细）。"""
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


def chat_content(chunk: bytes) -> tuple[int, bool]:
    """Chat Completions 的正文是 delta.content；思维链单独算并打标。"""
    think = _total(_CHAT_THINK, chunk)
    return _total(_CHAT_TEXT, chunk) + think, think > 0


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
    output = payload.get("output", [])
    # Codex standalone alpha/search returns output text as a string, whereas
    # Responses returns output items as an array. It shares the transport
    # observer but is not a malformed Responses body.
    if not isinstance(output, list):
        return _text_size(output), thinking
    for item in output:
        if not isinstance(item, dict):
            continue
        kind = item.get("type")
        if kind == "message":
            content = item.get("content", [])
            if not isinstance(content, list):
                continue
            for part in content:
                if not isinstance(part, dict):
                    continue
                size += _text_size(part.get("text")) + _text_size(part.get("refusal"))
        elif kind == "reasoning":
            thinking = True
            summary = item.get("summary", [])
            if isinstance(summary, list):
                size += sum(
                    _text_size(part.get("text")) for part in summary if isinstance(part, dict)
                )
        elif kind == "function_call":
            size += _text_size(item.get("arguments"))
    return size, thinking


def chat_json_content(payload: dict) -> tuple[int, bool]:
    """非流式 Chat Completions：正文在 choices[].message.content，思维链在它的兄弟字段。"""
    size, thinking = 0, False
    choices = payload.get("choices", [])
    if not isinstance(choices, list):
        return size, thinking
    for choice in choices:
        if not isinstance(choice, dict):
            continue
        message = choice.get("message")
        if not isinstance(message, dict):
            continue
        size += _text_size(message.get("content"))
        think = message.get("reasoning_content") or message.get("reasoning")
        if isinstance(think, str) and think:
            thinking = True
            size += _text_size(think)
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
    label: str
    path: str
    client: str
    # 管理页接口开关上用的一行短名（"Anthropic" / "OpenAI"）
    short: str
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
    # 与协议登记放在一起，供 failover/stats 派生默认设置，避免再维护平行名单。
    default_failover: bool = False
    ratio_fallback: tuple[float, float] | None = None


OPENAI = Protocol(
    name="openai",
    label="OpenAI Responses",
    path="/v1/responses",
    client="Codex",
    short="OpenAI",
    end_event_types=("response.completed",),
    end_data_markers=("[DONE]",),
    extract_usage=openai_usage,
    context_tokens=openai_context,
    count_content=openai_content,
    count_json_content=openai_json_content,
    error_body=openai_error,
    auth_headers=openai_auth,
    fingerprint={"user-agent": "codex_cli_rs", "originator": "codex_cli_rs"},
    ratio_fallback=(4.9, 3.0),
)

ANTHROPIC = Protocol(
    name="anthropic",
    label="Anthropic Messages",
    path="/v1/messages",
    client="Claude Code",
    short="Anthropic",
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
    default_failover=True,
    ratio_fallback=(6.7, 3.0),
)

CHAT = Protocol(
    name="openai-chat",
    label="OpenAI Chat Completions",
    path="/v1/chat/completions",
    client="OpenAI SDK",
    short="OpenAI Chat",
    # 流式的结束标记是 data: [DONE]，没有独立的 event 名
    end_event_types=(),
    end_data_markers=("[DONE]",),
    extract_usage=chat_usage,
    context_tokens=chat_context,
    count_content=chat_content,
    count_json_content=chat_json_content,
    error_body=openai_error,
    auth_headers=openai_auth,
    ratio_fallback=(4.9, 3.0),
)

# 协议名只在描述符这里登记。别处一律从 NAMES 派生，
# 漏改一处就会「新协议在转发侧存在、在下拉里没有」这种半吊子状态
ALL: dict[str, Protocol] = {p.name: p for p in (ANTHROPIC, OPENAI, CHAT)}
NAMES: tuple[str, ...] = tuple(ALL)


def public_metadata() -> list[dict[str, object]]:
    """Return only the stable display projection used by the admin UI.

    Keep keys, callables, and protocol-specific implementation details out of
    this response.  ``supports_1m`` is deliberately derived from the same
    beta-header setting used by forwarding.
    """
    return [
        {
            "name": proto.name,
            "label": proto.label,
            "short": proto.short or proto.label,
            "path": proto.path,
            "client": proto.client,
            "supports_1m": bool(proto.beta_header),
        }
        for proto in ALL.values()
    ]


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
        # 没有事件边界的超大帧：只丢观察缓冲，绝不拖慢或拖垮转发
        self.oversized_frames = 0
        # Compaction is opaque state. Keep only event metadata and ciphertext
        # length so diagnostics can prove its presence without persisting it.
        self.compaction_items: list[dict[str, object]] = []
        # Count-only inventory for capability probes. Some adapters use an
        # event name not known to the protocol descriptor.
        self.event_types: dict[str, int] = {}
        self.payload_types: dict[str, int] = {}

    MAX_BUFFER_BYTES = 4 * 1024 * 1024

    def feed(self, chunk: bytes) -> None:
        self._buffer.extend(chunk)
        while True:
            match = re.search(rb"\r?\n\r?\n", self._buffer)
            if match is None:
                if len(self._buffer) > self.MAX_BUFFER_BYTES:
                    # 很久等不到空行（超大单帧，或上游挂的是伪 SSE）：丢掉重来。
                    # 观察器只做统计，不能在这里无上限吃内存。
                    self._buffer.clear()
                    self.oversized_frames += 1
                return
            frame = bytes(self._buffer[: match.start()])
            del self._buffer[: match.end()]
            self._observe_frame(frame)

    def flush(self) -> None:
        """上游 EOF：把最后一个没以空行收尾的帧也看掉，别把完整流误记成 truncated。"""
        if not self._buffer:
            return
        frame = bytes(self._buffer).rstrip(b"\r\n")
        self._buffer.clear()
        if frame:
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
        if event:
            self.event_types[event] = self.event_types.get(event, 0) + 1
        data_bytes = b"\n".join(data)
        try:
            got, think = self.proto.count_content(frame)
            self.text_bytes += got
            self.thinking = self.thinking or think
        except Exception:
            # 统计只是观察，坏 JSON / 奇怪编码不能让下游断流。
            pass

        data_text = data_bytes.decode("utf-8", "ignore").strip()
        payload: object = None
        if data_text:
            try:
                payload = json.loads(data_text)
            except (TypeError, ValueError):
                pass
        if isinstance(payload, dict) and isinstance(payload.get("type"), str):
            payload_type = payload["type"]
            self.payload_types[payload_type] = self.payload_types.get(payload_type, 0) + 1
        self.compaction_items.extend(compaction_observations(payload, event=event))

        if event in self.proto.end_event_types:
            self.ended = True
            return
        # data 里的结束标记要独立于 event 头判断：中转站可能给 [DONE] 套一个
        # 未知的 event 名，只认 event 就会漏掉这段兜底
        if data_text in self.proto.end_data_markers:
            self.ended = True
            return
        if event:
            # 有 event 头时以它为准；否则一个正文里的 type 字段可能把非结束事件
            # 错当成结束。没有 event 头的上游才使用 data JSON 的 type 兜底。
            return
        if isinstance(payload, dict) and payload.get("type") in self.proto.end_event_types:
            self.ended = True


def compaction_observations(payload: object, *, event: str = "") -> list[dict[str, object]]:
    """Return redacted metadata for every likely Responses compaction item.

    Official Responses uses ``compaction``. ``compaction_summary`` and nested
    ``cmp_*`` encrypted items are also accepted because compatible gateways
    have emitted those shapes. Opaque values are never retained.
    """
    observations: list[dict[str, object]] = []
    seen: set[tuple[str, str, int, str]] = set()
    root_type = payload.get("type") if isinstance(payload, dict) else None

    def walk(value: object, path: str) -> None:
        if isinstance(value, dict):
            kind = value.get("type")
            item_id = value.get("id") if isinstance(value.get("id"), str) else ""
            encrypted = value.get("encrypted_content")
            is_compaction = (
                isinstance(kind, str)
                and kind in {"compaction", "compaction_summary", "response.compaction"}
            ) or (item_id.startswith("cmp_") and isinstance(encrypted, str))
            if is_compaction:
                kind_text = str(kind or "compaction")
                encrypted_len = len(encrypted.encode("utf-8")) if isinstance(encrypted, str) else 0
                marker = (path, item_id, encrypted_len, kind_text)
                if marker not in seen:
                    seen.add(marker)
                    observations.append({
                        "event": event or str(root_type or "json"),
                        "path": path,
                        "item_type": kind_text,
                        "item_id": item_id,
                        "encrypted_content_bytes": encrypted_len,
                        "has_encrypted_content": isinstance(encrypted, str),
                    })
            for key, child in value.items():
                walk(child, f"{path}.{key}")
        elif isinstance(value, list):
            for index, child in enumerate(value):
                walk(child, f"{path}[{index}]")

    walk(payload, "$")
    return observations
