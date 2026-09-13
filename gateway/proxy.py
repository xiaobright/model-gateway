from __future__ import annotations

import asyncio
import contextlib
import json
import time
import urllib.request
from collections.abc import Mapping
from typing import Any, AsyncIterator

import httpx
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, StreamingResponse
from starlette.requests import ClientDisconnect

from . import (
    capture,
    config,
    db,
    failover,
    inflight,
    naming,
    protocols,
    rewrite,
    upstream as upstream_mod,
)
from .reqlog import log
from .upstream import endpoint as upstream_endpoint

# connect 只给 8 秒：真库里 104 个 502 全是连不上，平均白等 16.6 秒（旧值是 15 秒的
# connect 超时在磨）。握手 8 秒都完不成的站，也扛不住几十万 token 的请求体。
PROXY_TIMEOUT = httpx.Timeout(connect=8.0, read=600.0, write=60.0, pool=600.0)
PROXY_LIMITS = httpx.Limits(max_connections=64, max_keepalive_connections=16)

# httpx 在 Windows 上会读注册表里的系统代理（Clash 之类），而注册表的 bypass 列表通常是空的，
# 于是连本机上游都会绕一趟代理。回环地址一律直连。
LOOPBACK = ("127.0.0.1", "localhost", "[::1]")

# 「出口」的两个特殊值，其余一律当代理 URL（http:// 或 socks5://）
EGRESS_SYSTEM = ""        # 跟随系统代理：httpx 自己去读环境变量和注册表
EGRESS_DIRECT = "direct"  # 直连：把系统代理也关掉

# 上游发呆超时：这么久没有任何新字节（含等响应头）就按「卡住」打断这条请求，
# 让下游的客户端自己重发。不触发自动降级 —— 真库里的偶发卡住常常只影响一条连接
# （并发其它请求都正常），换站会让同一条请求被处理两遍。0 = 关闭。
STALL_TIMEOUT_KEY = "stall_timeout_s"
DEFAULT_STALL_TIMEOUT_S = 20.0
MAX_STALL_TIMEOUT_S = 3600.0

# 请求体上限。一次正常 LLM 调用远到不了这个数（1M 上下文纯文本约几 MB，图片再翻几倍），
# 但客户端写错或恶意灌大时能保住进程 —— Starlette 的 request.body() 会把整包读进内存。
MAX_REQUEST_BYTES = 64 * 1024 * 1024


def stall_timeout() -> float:
    raw = db.get_setting(STALL_TIMEOUT_KEY, "")
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return DEFAULT_STALL_TIMEOUT_S
    return min(MAX_STALL_TIMEOUT_S, max(0.0, value))


def set_stall_timeout(seconds: float) -> float:
    value = min(MAX_STALL_TIMEOUT_S, max(0.0, float(seconds)))
    db.set_setting(STALL_TIMEOUT_KEY, f"{value:g}")
    return value


def loopback_mounts() -> dict[str, httpx.AsyncHTTPTransport]:
    # 每个 client 要有自己的 transport（transport 自带连接池，会随 client 一起关闭）
    return {f"all://{host}": httpx.AsyncHTTPTransport() for host in LOOPBACK}


def client_args(egress: str) -> dict:
    """按「出口」拼出建 client 要的那几个参数。

    这是整件事唯一的开关：网关自己就是发请求的那个客户端，socket 是它自己开的，
    所以按站换出口不需要任何代理内核 —— 内核的存在意义是替「不知道有代理」的进程
    做拦截。

    注意 `trust_env=False` 才是真的「直连」：httpx 不只看环境变量，在 Windows 上
    还会读注册表里的系统代理。

    回环 mounts 只在没指定代理时挂：它是用来抵消**隐式**的系统代理的（否则连本机
    上游都要绕一趟 Clash）。明确给某个站指了代理，就按说的走 —— 真实场景里没人会给
    127.0.0.1 的站配代理，而测试要的正是「字节真的从那扇门出去了」。

    代理 URL 允许带 `#ca=<pem 路径>` 的尾巴（自签证书的 https 代理，VPS 上 gost 那扇门）。
    怎么拆、怎么校验都在 upstream 里（split_ca / ca_context）—— 保存出口时和真正建 client
    时走的是同一段代码，别在两处各写一份规则。
    """
    egress = (egress or "").strip()
    proxy = None if egress in (EGRESS_SYSTEM, EGRESS_DIRECT) else egress
    args: dict = {
        "mounts": {} if proxy else loopback_mounts(),
        "trust_env": egress == EGRESS_SYSTEM,
        "proxy": proxy,
    }
    # 用 Windows 系统证书库验证上游 TLS。httpx 默认用自带的 CA 捆绑包，认不得
    # 卡巴斯基这类「加密连接扫描」的 MITM 根证书 —— 明明 curl 能通、浏览器能通，
    # 网关却 502 "self-signed certificate in certificate chain"（2026-09-11 实锤）。
    # truststore 把系统库注入 ssl：杀软的根是用户自己机器上受信的，跟着走。
    # 没装 truststore 就退回 httpx 默认行为。
    ctx = upstream_mod.system_ssl_context()
    if ctx is not None:
        args["verify"] = ctx
    if proxy:
        base, frag = upstream_mod.split_ca(proxy)
        if frag:
            # httpx 连代理这一跳用的是 Proxy 对象上单独的 ssl_context，client 的
            # verify 管不到它 —— 钉证书必须钉在这里
            args["proxy"] = httpx.Proxy(httpx.URL(base), ssl_context=upstream_mod.ca_context(frag))
    return args


# 逐跳头（RFC 7230 8.1.2.2）：只属于当前这一跳，代理必须剥掉；Connection 头里
# 点名的 token 同样算逐跳，见 _connection_tokens
_HOP_BY_HOP = {
    "connection", "keep-alive", "content-length", "host", "te", "trailer",
    "transfer-encoding", "upgrade", "proxy-authorization", "proxy-authenticate",
}
REQ_DROP = _HOP_BY_HOP
# content-encoding/content-length 必须去掉：relay 用 aiter_bytes 解压后转发，
# 客户端拿到的是未压缩字节流（上游的 usage/完成事件检测也依赖解压后的内容）。
# 多段编码的残余头由 _resp_encoding_to_keep 单独重写。
RESP_DROP = _HOP_BY_HOP | {"content-encoding"}


def _connection_tokens(headers: Any) -> set[str]:
    """Connection: keep-alive, x-foo 里点名的头也是逐跳的，必须一起剥。"""
    return {t.strip().lower() for t in headers.get("connection", "").split(",") if t.strip()}

def _resp_encoding_to_keep(upstream_resp: httpx.Response) -> str:
    """httpx 按它支持的解码器逐层解压；返回还要留给客户端的 content-encoding。

    RESP_DROP 默认摘掉 content-encoding，前提是 relay 用 aiter_bytes 已经解压过了。
    但响应头可能是逗号分隔的多层（`gzip, br`），httpx 只解自己支持的那几层：解过的
    层必须从头里去掉（不然客户端会拿明文再解一次），没解掉的留着让客户端自己解。
    老逻辑拿整串去和单个解码器名比相等，多层时就整段保留了 —— 和 opencode 那次
    「上游 200、下游没回复」同类，只是触发条件更窄。

    返回空串 = 全部已解压，调用方摘掉 content-encoding。
    """
    from httpx._decoders import SUPPORTED_DECODERS

    supported = {
        (key.decode() if isinstance(key, bytes) else str(key)).lower()
        for key in SUPPORTED_DECODERS
    }
    supported.add("identity")
    values = upstream_resp.headers.get_list("content-encoding", split_commas=True)
    remaining = [
        token.strip()
        for token in values
        if token.strip() and token.strip().lower() not in supported
    ]
    return ", ".join(remaining)


def accept_encoding() -> str:
    """我们能解压什么，就向上游报什么 —— 决不能照抄客户端的 accept-encoding。

    opencode 这类客户端会带 `accept-encoding: gzip, deflate, br, zstd`。原样透传时上游
    可能挑 br，而 httpx 没装 brotli 就解不开，转发给客户端的是一坨压缩原文：客户端一个
    SSE 事件都解析不出来（表现为「没回复」），网关这边因为等不到完成事件而记成 truncated
    （2026-09-12 实锤，抓包看到上游 content-encoding: br、转发的 421 字节全是二进制）。
    所以这个头必须由网关按自己真实的解码能力来报，而不是替客户端转达。
    """
    from httpx._decoders import SUPPORTED_DECODERS

    names = [
        (key.decode() if isinstance(key, bytes) else str(key))
        for key in SUPPORTED_DECODERS
        if (key.decode() if isinstance(key, bytes) else str(key)) != "identity"
    ]
    return ", ".join(sorted(names)) or "identity"


HEAD_KEEP = 8192   # 开头留这么多：Anthropic 的输入 token 只在流开头的 message_start 里报一次
TAIL_KEEP = 65536  # 末尾留这么多，用来抓 usage
# 非流式响应要收齐了才能解析 usage；大响应封顶，别在网关里额外留一份全量副本
MAX_JSON_OBSERVE_BYTES = 16 * 1024 * 1024

# 连上游都没连上时的 usage：一个数都没有
NO_USAGE: protocols.Usage = (None, None, None, None)

# /v1/models 的 Anthropic 形状要求每项带 created_at，值本身没有客户端会用
MODEL_CREATED_AT = "2025-01-01T00:00:00Z"

router = APIRouter()

_clients: dict[object, httpx.AsyncClient] = {}
_client_loop: asyncio.AbstractEventLoop | None = None


def _system_proxy_signature() -> tuple[tuple[str, str], ...]:
    """快照 httpx 会读取的系统代理配置，避免开关切换后继续复用旧 client。"""
    return tuple(sorted(
        (str(key).lower(), str(value))
        for key, value in urllib.request.getproxies().items()
    ))


async def get_client(egress: str = EGRESS_SYSTEM) -> httpx.AsyncClient:
    """按「出口」复用 client，省掉每个请求一次 TLS 握手（对远端公益站是几百 ms 的差别）。

    同一个出口和同一份系统代理配置复用一个 client。代理是建 client 时定的，没法按请求
    换；系统代理开关变化后用新的配置签名建新 client。出口最多也就三五种，池子小得可以忽略。
    """
    global _client_loop
    loop = asyncio.get_running_loop()
    if _client_loop is not loop:
        # 换 loop 了（托盘模式下服务跑在另一个线程里）。旧 client 的连接池绑着上一个
        # loop，留着就是泄漏一批连接和 fd —— 而且它们已经没人能用了。
        # 先占坑再清理：否则两个并发请求同时进这段，后完成者会把先完成者刚建的
        # client 一起清掉（那段连接池就没人关了）
        _client_loop = loop
        await aclose_client()
    cache_key: object = (
        (EGRESS_SYSTEM, _system_proxy_signature())
        if egress == EGRESS_SYSTEM else egress
    )
    client = _clients.get(cache_key)
    if client is None or client.is_closed:
        client = httpx.AsyncClient(
            timeout=PROXY_TIMEOUT, limits=PROXY_LIMITS, **client_args(egress)
        )
        _clients[cache_key] = client
    return client


async def aclose_client() -> None:
    # 先摘快照再关：并发 get_client 在等待期间新建的 client 不能被后到的清理扫掉。
    # 不在这里改 _client_loop —— 谁切换 loop 谁负责记，清理只负责关连接。
    snapshot = list(_clients.items())
    for key, client in snapshot:
        if _clients.get(key) is client:
            del _clients[key]
    for _, client in snapshot:
        if not client.is_closed:
            with contextlib.suppress(Exception):
                await client.aclose()


class ClientDisconnected(Exception):
    """下游在等待上游响应头时已经断开。"""


RESPONSE_POLL = 0.05


async def _send_until_headers(
    request: Request, client: httpx.AsyncClient, prepared: httpx.Request
) -> httpx.Response:
    """等待响应头，同时让下游断开能够取消尚未完成的发送。

    `httpx.AsyncClient.send()` 会一直等到响应头，relay 还没开始之前没有其它取消点。
    Request.is_disconnected() 是非阻塞检查，短暂让出事件循环即可避免把下游断开拖到
    上游 read/connect timeout 才收尾。等响应头不设发呆超时 —— 慢站的首字可以等一两分钟，
    那是模型在算，不是卡住；真卡死了还有 httpx 的 600 秒 read 超时兜底。
    """
    task = asyncio.create_task(client.send(prepared, stream=True))
    try:
        while True:
            # 可被完成唤醒的等待：响应头一到就返回，不能让 50ms 的轮询节拍变成
            # 每个请求的关键路径延迟（断开检测仍是每 RESPONSE_POLL 一次）
            done, _ = await asyncio.wait({task}, timeout=RESPONSE_POLL)
            if task in done:
                return await task
            if await request.is_disconnected():
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
                raise ClientDisconnected
        return await task
    except BaseException:
        # 取消恰好撞上响应头到达时，send 的任务可能已经完成，也要归还那条连接。
        if task.done() and not task.cancelled() and task.exception() is None:
            with contextlib.suppress(Exception):
                await task.result().aclose()
        raise
    finally:
        if not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task


def _error(proto: protocols.Protocol, status: int, message: str) -> JSONResponse:
    return JSONResponse(proto.error_body(status, message), status_code=status)

def _client_label(ua: str) -> str:
    if not ua:
        return "unknown"
    if ua.startswith("Codex"):
        return "Codex Desktop"
    if "codex_cli_rs" in ua:
        return "Codex CLI"
    if ua.startswith("claude-cli"):
        return "Claude Code"
    return ua.split("/")[0].strip()[:24] or "unknown"


def _capability_summary(payload: dict) -> str:
    """为排查工具/压缩兼容性记录脱敏摘要，不落请求参数或输入内容。"""
    parts: list[str] = []
    tools = payload.get("tools")
    if isinstance(tools, list):
        names = []
        for item in tools[:16]:
            if isinstance(item, dict):
                names.append(str(item.get("type") or item.get("name") or "?"))
            else:
                names.append("?")
        suffix = ",".join(names)
        if len(tools) > 16:
            suffix += ",..."
        parts.append(f"tools={len(tools)}[{suffix}]")
    if "context_management" in payload:
        parts.append("context_management=present")
    return (" " + " ".join(parts)) if parts else ""


def _input_item_census(payload: dict) -> tuple[dict[str, int], dict[str, int]]:
    """数一遍 input 里各 item 的 type 和「type:role」。

    为什么连 role 一起数：Codex Desktop 的 "responses lite" 线格式不发顶层 `instructions`，
    系统提示词是 `role: "developer"` 的 message item —— 只看 type 会以为「全是普通 message」，
    而 role 一旦原样透传就会被上游 422。抓形状时就该看见它。
    """
    input_value = payload.get("input")
    input_items = input_value if isinstance(input_value, list) else [input_value]
    types: dict[str, int] = {}
    roles: dict[str, int] = {}
    for item in input_items:
        if isinstance(item, dict):
            item_type = str(item.get("type", "?"))
            role = item.get("role")
            if isinstance(role, str) and role:
                key = f"{item_type}:{role}"
                roles[key] = roles.get(key, 0) + 1
        else:
            item_type = type(item).__name__
        types[item_type] = types.get(item_type, 0) + 1
    return types, roles


def _tool_brief(value: object, depth: int = 0) -> object:
    """工具形状摘要：递归保留结构，长字符串截断 —— 抓包是为了看形状，不是存内容。"""
    if isinstance(value, Mapping):
        if depth > 4:
            return "..."
        return {
            str(key): _tool_brief(item, depth + 1)
            for key, item in value.items()
            if key not in ("parameters",) or depth < 2
        }
    if isinstance(value, list):
        return [_tool_brief(item, depth + 1) for item in value[:8]] + (
            ["..."] if len(value) > 8 else []
        )
    if isinstance(value, str):
        return value[:160] + f"...({len(value)}B)" if len(value) > 160 else value
    return value


def _maybe_capture_headers(request: Request, payload: dict, body_len: int) -> None:
    """调试用：放一个 data/capture.flag，下一请求的头和形状会被脱敏记录。"""
    flag = config.DATA_DIR / "capture.flag"
    if not flag.exists():
        return
    dump = {
        k: ("<redacted>" if capture.is_secret_header(k) else v)
        for k, v in request.headers.items()
    }
    input_types, input_roles = _input_item_census(payload)
    # 工具连容器一起记：`namespace` 这个坑就是抓包只记个数才漏掉的
    tool_briefs = _tool_brief(payload.get("tools") or [])
    extra_tools = [
        item.get("tools")
        for item in (payload.get("input") if isinstance(payload.get("input"), list) else [])
        if isinstance(item, Mapping) and item.get("type") == "additional_tools"
    ]
    shape = {
        "path": request.url.path,
        "body_bytes": body_len,
        "top_level_keys": sorted(str(key) for key in payload),
        "input_item_types": input_types,
        # 连 role 一起记：`developer` 是从这里看出来的
        "input_item_roles": input_roles,
        "tools_count": len(payload.get("tools")) if isinstance(payload.get("tools"), list) else 0,
        "tools_shape": tool_briefs,
        "additional_tools_shape": [_tool_brief(tools) for tools in extra_tools if tools is not None],
        "has_context_management": "context_management" in payload,
        "content_encoding": request.headers.get("content-encoding", ""),
        "codex_beta_features": request.headers.get("x-codex-beta-features", ""),
    }
    try:
        (config.DATA_DIR / "captured_headers.json").write_text(
            json.dumps(dump, indent=2, ensure_ascii=False), encoding="utf-8"
        )
        (config.DATA_DIR / "captured_request_shape.json").write_text(
            json.dumps(shape, indent=2, ensure_ascii=False), encoding="utf-8"
        )
        flag.unlink()
        log("captured real client headers and request shape to data/captured_*.json")
    except OSError:
        pass


def _compaction_capture_requested(path: str) -> bool:
    """Whether the one-shot compaction observation flag is armed."""
    return path.endswith("/responses") or path.endswith("/responses/compact")


def _has_compaction_trigger(payload: dict) -> bool:
    input_items = payload.get("input")
    if not isinstance(input_items, list):
        return False
    return any(
        isinstance(item, dict) and item.get("type") == "compaction_trigger"
        for item in input_items
    )


def _probe_request_shape(payload: dict) -> dict[str, object]:
    """Return only the request shape needed to diagnose client-side compaction."""
    input_value = payload.get("input")
    input_items = input_value if isinstance(input_value, list) else [input_value]
    input_types: dict[str, int] = {}
    for item in input_items:
        item_type = item.get("type", "?") if isinstance(item, dict) else type(item).__name__
        key = str(item_type)
        input_types[key] = input_types.get(key, 0) + 1
    return {
        "top_level_keys": sorted(str(key) for key in payload),
        "input_item_types": input_types,
        "tools_count": len(payload.get("tools")) if isinstance(payload.get("tools"), list) else 0,
        "has_context_management": "context_management" in payload,
        "has_compaction_trigger": _has_compaction_trigger(payload),
    }


def _probe_response_types(value: object, counts: dict[str, int] | None = None) -> dict[str, int]:
    """Count JSON ``type`` fields without retaining response content."""
    result = counts if counts is not None else {}
    if isinstance(value, dict):
        kind = value.get("type")
        if isinstance(kind, str):
            result[kind] = result.get(kind, 0) + 1
        for child in value.values():
            _probe_response_types(child, result)
    elif isinstance(value, list):
        for child in value:
            _probe_response_types(child, result)
    return result


def _write_compaction_capture(
    *,
    request: Request,
    payload: dict,
    route: db.Route,
    remote_model: str,
    status: int,
    stream: bool,
    req_bytes: int,
    resp_bytes: int,
    elapsed: float,
    observations: list[dict[str, object]],
    response_event_types: dict[str, int] | None = None,
    response_payload_types: dict[str, int] | None = None,
) -> None:
    """Write a count-only probe record, including negative evidence.

    Keep the flag armed after an ordinary response so a later automatic
    compaction can still be captured. A positive record is never overwritten by
    subsequent ordinary turns.
    """
    flag = config.DATA_DIR / "compaction_capture.flag"
    if not flag.exists():
        return
    capture_path = config.DATA_DIR / "compaction_capture.json"
    if not observations and capture_path.exists():
        try:
            old = json.loads(capture_path.read_text(encoding="utf-8"))
            if isinstance(old, dict) and old.get("found") is True:
                return
        except (OSError, ValueError, TypeError):
            pass
    capture = {
        "captured_at_unix": time.time(),
        "path": request.url.path,
        "model": str(payload.get("model") or ""),
        "remote_model": remote_model,
        "upstream": route.upstream.name,
        "group": route.group_name,
        "status": status,
        "stream": stream,
        "request_bytes": req_bytes,
        "response_bytes": resp_bytes,
        "duration_ms": int(elapsed * 1000),
        "found": bool(observations),
        "x_codex_beta_features": request.headers.get("x-codex-beta-features", ""),
        "request_shape": _probe_request_shape(payload),
        "response_event_types": response_event_types or {},
        "response_payload_types": response_payload_types or {},
        "observations": observations,
    }
    try:
        capture_path.write_text(
            json.dumps(capture, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        if observations:
            flag.unlink(missing_ok=True)
            log(f"  compaction capture: {len(observations)} item event(s), encrypted lengths only; saved data/compaction_capture.json")
        else:
            log("  compaction probe: no compaction item in this response; flag remains armed")
    except OSError as exc:
        log(f"  compaction capture write failed: {exc.__class__.__name__}: {exc}")


def _build_headers(
    request: Request,
    upstream: db.Upstream,
    proto: protocols.Protocol,
    want_1m: bool = False,
) -> dict[str, str]:
    # ASGI 保证头名已经小写，所以下面用小写键既能覆盖客户端的同名头，也不会两份并存
    drop = REQ_DROP | _connection_tokens(request.headers)
    headers = {k: v for k, v in request.headers.items() if k.lower() not in drop}
    # 客户端报的压缩算法我们不一定解得了，一律换成网关自己这边的真实能力（见 accept_encoding）
    headers["accept-encoding"] = accept_encoding()
    for key, value in proto.defaults.items():
        headers.setdefault(key, value)
    if upstream.api_key:
        headers.update(proto.auth_headers(upstream.api_key))
    if want_1m and proto.beta_header:
        headers[proto.beta_header] = naming.add_beta(headers.get(proto.beta_header, ""), naming.BETA_1M)
    # 覆写放最后：它要能改掉上面自动加的任何头（值写 null 表示删掉那个头）
    for key, value in upstream_mod.parse_override(upstream.header_override).items():
        if value is None:
            headers.pop(key, None)
        else:
            headers[key] = value
    return headers

# ---------------------------------------------------------------- 转发


async def forward(
    request: Request,
    proto: protocols.Protocol,
    path: str,
    *,
    record: bool = True,
) -> StreamingResponse | JSONResponse:
    """把一个请求转到当前生效的上游；打不通就按优先级换下一个候选（见 failover.py）。

    唯一会改动请求体的地方是 model 字段（换成上游那边的真名）。两边名字一致时继续发
    原始字节，「字节级透传」这个特性就还在。**每个候选的真名和 key 都可能不一样**，
    所以请求体和请求头都在循环里按候选重建。

    record=False 给 count_tokens 这类元数据请求用：照样转发、照样记文本日志，但不进
    转发记录、不计入活跃流，否则它会把「累计转发」和模型热度冲得没法看。
    """
    endpoint = request.url.path
    # 全局停用的接口在转发入口就拒绝：不解析请求体、不碰上游、不写记录，客户端拿到的
    # 和「这个模型没配过」一样是 404。配置都还在，管理页打开开关即恢复。
    if not db.protocol_enabled(proto.name):
        log(f"POST {endpoint} -> 404 ({proto.name} 接口已全局停用)")
        return _error(proto, 404, f"{proto.label} 接口已全局停用，请在管理页的接口开关里启用")
    # content-length 先拦一道（大包不必读进内存）；分块传的边读边数
    declared = request.headers.get("content-length", "")
    if declared.isdigit() and int(declared) > MAX_REQUEST_BYTES:
        log(f"POST {endpoint} -> 413 (content-length {declared} 超上限)")
        return _error(proto, 413, f"请求体超过 {MAX_REQUEST_BYTES // (1024 * 1024)}MB 上限")
    try:
        buf = bytearray()
        async for part in request.stream():
            buf.extend(part)
            if len(buf) > MAX_REQUEST_BYTES:
                log(f"POST {endpoint} -> 413 (请求体超过上限，已读 {len(buf)}B)")
                return _error(proto, 413, f"请求体超过 {MAX_REQUEST_BYTES // (1024 * 1024)}MB 上限")
        body = bytes(buf)
    except ClientDisconnect:
        # 大请求体上传到一半客户端走了：连接已经没了，但别在日志里留一坨 ASGI 异常栈
        log(f"POST {endpoint} -> 499 (客户端在请求体上传中途断开)")
        return _error(proto, 499, "客户端已断开")
    try:
        payload = json.loads(body)
        if not isinstance(payload, dict):
            raise ValueError("not a dict")
    except (json.JSONDecodeError, UnicodeDecodeError, ValueError):
        return _error(proto, 400, "请求体不是合法 JSON")

    # 上游内容审核误报绕行：按规则表对请求体文本做字面替换（默认空表 = 什么都不做）
    scrubbed, rewrite_hits = rewrite.apply(payload)
    if scrubbed is not None:
        payload = scrubbed
        log(f"POST {endpoint} 敏感词绕行：命中 {rewrite_hits} 处")

    compaction_capture = (
        _compaction_capture_requested(endpoint)
        and (config.DATA_DIR / "compaction_capture.flag").exists()
    )
    requested = str(payload.get("model", ""))
    asked, flag = naming.split_model(requested)
    normal_chain = db.resolve_chain(asked, proto.name)
    search_endpoint = path == "/alpha/search"
    search_target = (
        db.resolve_standalone_search_target(asked)
        if search_endpoint and proto.name == protocols.OPENAI.name
        else None
    )
    # Standalone Codex search does not run on the model's Responses provider.
    # When a search-only group is configured, put it first even if that group
    # has no ordinary route for this model.  It receives the requested model
    # unchanged, allowing the upstream to select its own Alpha Search credential/alias.
    # Avoid retrying the same group through its normal candidate later.
    chain = (
        (search_target,) + tuple(route for route in normal_chain if route.group_id != search_target.group_id)
        if search_target is not None
        else normal_chain
    )
    if not chain:
        # 模型录在别的接口下时说清楚：这种 404 光看「未配置」会以为是没导入。
        # 本接口也在暴露名单里时不算「别处」——那只是候选全停用了，文案该走默认
        others = [p for p in db.protocol_of_model(asked) if p != proto.name]
        why = (
            f"模型 {requested!r} 是在 {'、'.join(others)} 接口下暴露的，不能从 {endpoint} 调用"
            if others
            else f"模型 {requested!r} 未配置或当前上游已停用"
        )
        log(f"POST {endpoint} model={requested!r} -> 404 ({why}) req={len(body)}B")
        return _error(proto, 404, why)
    if chain[0].model_name != asked:
        log(f"POST {endpoint} model={asked!r} 没配过，按档位关键字落到 {chain[0].model_name!r}")

    stream_flag = bool(payload.get("stream"))
    # 有副作用的 OpenAI 请求不降级：上游可能已经把它存下来了才失败，重试会留下两条
    stateful = payload.get("store") is not None or payload.get("previous_response_id") is not None
    # Standalone Codex search is side-effect free. It may be unavailable on an
    # otherwise healthy OpenAI-compatible upstream (404/405/501), so probe the
    # next configured candidate for this endpoint even when normal OpenAI
    # failover is disabled. Ordinary /responses keeps the existing policy.
    can_search_failover = search_endpoint and len(chain) > 1 and not stateful
    can_failover = (
        (failover.enabled(proto.name) and len(chain) > 1 and not stateful)
        or can_search_failover
    )
    if search_target is not None:
        # The explicit target is an operator decision, not a normal model
        # fallback candidate.  A prior Responses failure must not silently
        # send a search to another provider before it has been attempted.
        candidates = [search_target] + (
            failover.order_chain(chain[1:]) if can_failover else []
        )
    else:
        candidates = failover.order_chain(chain) if can_failover else [chain[0]]

    started = time.monotonic()
    stall_s = stall_timeout()
    resp: httpx.Response | None = None
    route = candidates[0]
    remote = ""
    sent_body = body
    attempt = 0
    index = 0
    fail: Exception | None = None
    # 最后一次真正发出去的候选（不是「下一个要试的」）：502 文案只能怪它
    last_route: db.Route | None = None
    # 这个请求里站级失败过的分组。整个跳掉（含它下面同模型的其它候选）：同一个站
    # 绝不在一次请求里立刻重试。候选级的失败（404 / 400 / 422）不进这里 ——
    # 那是「这个候选不认这条请求」，站本身不一定有毛病
    dead_groups: set[int] = set()
    # 「实时」那一页的数据来源。record=False 的元数据请求（count_tokens）照样登记 ——
    # 它也会走降级、也会踩断路器，「这个站为什么在被打」的答案有时就是它 —— 但打个
    # meta 标，不计入「进行中」的数字
    call = inflight.begin(
        client=_client_label(request.headers.get("user-agent", "")),
        protocol=proto.name, model=asked, stream=stream_flag,
        req_bytes=len(body), meta=not record,
    )

    def advance() -> int:
        """还该不该再打一个？返回下一个候选的下标，-1 = 到此为止。

        候选顺序就是用户排的，配了几个就试几个 —— 不设尝试次数上限。总时长由
        START_DEADLINE 和每次尝试前的客户端断开检查兜底。
        """
        return failover.next_index(candidates, index + 1, dead_groups)

    async def same_retry_pause(delay_s: float) -> bool:
        """同站重试前的间隔；客户端已经走了就返回 False。"""
        if delay_s > 0:
            end = time.monotonic() + delay_s
            while time.monotonic() < end:
                if await request.is_disconnected():
                    return False
                await asyncio.sleep(min(0.05, max(0.0, end - time.monotonic())))
        return True

    # 每个上游在本次请求里已经同站重试过几次（键是 upstream.id）
    same_counts: dict[int, int] = {}
    # True = 上一轮决定同站再发，本轮不把 attempt 加一（同站重试优先于换站降级）
    same_mode = False
    # 客户端在中途走了（重试间隙被检测到）。收尾要记 499/client_abort，不能冤枉成上游连不上
    client_left = False

    # 这里是「自动降级」唯一安全的落点：状态码已经拿到手，但还没往下游发过任何字节，
    # 换个上游重试客户端完全无感。第一个字节一旦发出去就不能再换了。
    # 同站重试也在这同一个落点：先吃供应商的 retry_rules，耗尽了才轮到换候选。
    while True:
        route = candidates[index]
        if same_mode:
            same_mode = False
        else:
            attempt += 1
        if attempt > 1 or same_counts.get(route.upstream.id, 0) > 0:
            if await request.is_disconnected():
                client_left = True
                if attempt > 1:
                    attempt -= 1
                log(f"  客户端已经走了，不再重试（试过 {attempt} 个）")
                break
            if time.monotonic() - started > failover.START_DEADLINE:
                if attempt > 1:
                    attempt -= 1
                log(f"  已经耗了 {time.monotonic() - started:.0f}s，不再开新尝试")
                break

        last_route = route
        # 上游只认它那边的真名。[1m] 是 Claude Code 自己的档位约定，请求侧和配置侧都可能带，一并摘掉
        remote, remote_flag = naming.split_model(route.remote_model)
        want_1m = naming.wants_1m(flag) or naming.wants_1m(remote_flag)
        sent_body = body
        if remote != requested or rewrite_hits:
            # ensure_ascii=False：否则中文请求体会涨三到六倍。
            sent_body = json.dumps(
                {**payload, "model": remote}, ensure_ascii=False, separators=(",", ":")
            ).encode()

        _maybe_capture_headers(request, payload, len(body))
        # base_url 存的是站根，/v1 由这里按接口补上（两种接口的路径都在 /v1 底下）
        url = upstream_endpoint(route.upstream.base_url, path)
        headers = _build_headers(request, route.upstream, proto, want_1m)
        label = f"{route.upstream.name}/{route.group_name}"
        inflight.set_route(
            call, attempt=attempt, upstream=route.upstream.name, group_name=route.group_name,
            group_id=route.group_id, remote_model=remote, req_bytes=len(sent_body),
        )
        began = time.monotonic()
        try:
            # 出口是**供应商**的属性，而每个候选可能属于不同的供应商，所以 client 在循环里取。
            # 放在 try 里：出口配坏了（比如 #ca 指的证书被删了）是「这扇门不通」，
            # 按连不上处理、降级换下一扇，而不是整个请求 500
            client = await get_client(route.upstream.egress)
            resp = await inflight.wait_for_upstream(
                call,
                _send_until_headers(
                    request,
                    client,
                    client.build_request("POST", url, content=sent_body, headers=headers),
                ),
            )
        except inflight.ManualAbort:
            elapsed = time.monotonic() - began
            log(f"POST {endpoint} model={requested!r} -> 499 (手动中断) after {elapsed:.1f}s")
            inflight.finish(call, status=499, note="manual_abort")
            if record:
                _record(
                    request=request, route=route, proto=proto, model=asked,
                    remote_model=remote, status=499, stream_flag=stream_flag,
                    req_bytes=len(sent_body), resp_bytes=0, elapsed=elapsed,
                    usage=NO_USAGE, note="manual_abort", attempt=attempt,
                )
            return _error(proto, 499, "请求已手动中断")
        except ClientDisconnected:
            elapsed = time.monotonic() - began
            log(
                f"POST {endpoint} model={requested!r} upstream={route.upstream.name} -> 499 "
                f"(客户端在等待响应头时断开) after {elapsed:.1f}s req={len(sent_body)}B"
            )
            inflight.finish(call, status=499, note="client_abort")
            if record:
                _record(
                    request=request, route=route, proto=proto, model=asked,
                    remote_model=remote, status=499, stream_flag=stream_flag,
                    req_bytes=len(sent_body), resp_bytes=0, elapsed=elapsed,
                    usage=NO_USAGE, note="client_abort", attempt=attempt,
                )
            return _error(proto, 499, "客户端已断开")
        except asyncio.CancelledError:
            elapsed = time.monotonic() - began
            inflight.finish(call, status=499, note="client_abort")
            if record:
                _record(
                    request=request, route=route, proto=proto, model=asked,
                    remote_model=remote, status=499, stream_flag=stream_flag,
                    req_bytes=len(sent_body), resp_bytes=0, elapsed=elapsed,
                    usage=NO_USAGE, note="client_abort", attempt=attempt,
                )
            raise
        except (httpx.HTTPError, httpx.InvalidURL, ImportError, ValueError) as exc:
            fail, resp = exc, None
            elapsed = time.monotonic() - began
            log(
                f"POST {endpoint} model={requested!r} upstream={route.upstream.name} -> 502 "
                f"({exc.__class__.__name__}: {exc}) after {elapsed:.1f}s req={len(sent_body)}B"
                f"{'' if attempt == 1 else f' [第 {attempt} 次尝试]'}"
            )
            # 连不上也算 502：供应商若把 502 写进了同站重试，就先原站再拨一把
            delay = failover.same_retry_delay(
                route.upstream.retry_rules, 502, same_counts.get(route.upstream.id, 0)
            )
            if delay is not None:
                same_counts[route.upstream.id] = same_counts.get(route.upstream.id, 0) + 1
                log(
                    f"  同站重试：{route.upstream.name} 连不上，{delay:.2f}s 后再试"
                    f"（第 {same_counts[route.upstream.id]} 次）"
                )
                inflight.failed(call, status=502, note="same_retry", ms=int(elapsed * 1000))
                if record:
                    _record(
                        request=request, route=route, proto=proto, model=asked,
                        remote_model=remote, status=502, stream_flag=stream_flag,
                        req_bytes=len(sent_body), resp_bytes=0, elapsed=elapsed,
                        usage=NO_USAGE, note="same_retry", attempt=attempt,
                    )
                if not await same_retry_pause(delay):
                    client_left = True
                    log("  客户端已断开，不再同站重试")
                    break
                fail = None
                same_mode = True
                continue
            failover.note_fail(route.group_id, 502, label)
            dead_groups.add(route.group_id)
            inflight.failed(call, status=502, note="connect_failed", ms=int(elapsed * 1000))
            if record:
                _record(
                    request=request, route=route, proto=proto, model=asked,
                    remote_model=remote, status=502, stream_flag=stream_flag,
                    req_bytes=len(sent_body), resp_bytes=0, elapsed=elapsed,
                    usage=NO_USAGE, note="connect_failed", attempt=attempt,
                )
            index = advance()
            if index < 0:
                break
            continue

        # 这次拿到响应头了，之前候选的连接异常不再算数
        fail = None
        inflight.phase(call, inflight.WAIT, status=resp.status_code)
        # 带了 stateful 字段的请求不降级，日志里标出来 —— 排查「为什么这条没换站」时靠它
        detail = f" store={payload.get('store')} prev_id={payload.get('previous_response_id')!r}" if stateful else ""
        log(
            f"POST {endpoint} model={requested!r} upstream={route.upstream.name} remote={remote!r} "
            f"-> {resp.status_code} stream={stream_flag}{' 1m' if want_1m else ''}{detail} "
            f"req={len(sent_body)}B ua={request.headers.get('user-agent', '')[:48]!r}"
            f"{_capability_summary(payload)}"
            f"{'' if attempt == 1 else f' [第 {attempt} 次尝试]'}"
        )

        site_bad = resp.status_code in failover.RETRY_STATUS
        candidate_bad = resp.status_code in failover.CANDIDATE_STATUS
        # 同站重试优先于换站降级：命中该供应商的规则就原站再发，耗尽了才走下面的
        # note_fail / advance。错误状态说明上游还没吐正文，重发对 store 类请求通常也安全
        delay = failover.same_retry_delay(
            route.upstream.retry_rules, resp.status_code, same_counts.get(route.upstream.id, 0)
        )
        if delay is not None:
            same_counts[route.upstream.id] = same_counts.get(route.upstream.id, 0) + 1
            log(
                f"  同站重试：{route.upstream.name} 返回 {resp.status_code}，"
                f"{delay:.2f}s 后再发（第 {same_counts[route.upstream.id]} 次）"
            )
            status_for_retry = resp.status_code
            with contextlib.suppress(Exception):
                await resp.aclose()
            elapsed = time.monotonic() - began
            inflight.failed(call, status=status_for_retry, note="same_retry", ms=int(elapsed * 1000))
            if record:
                _record(
                    request=request, route=route, proto=proto, model=asked,
                    remote_model=remote, status=status_for_retry, stream_flag=stream_flag,
                    req_bytes=len(sent_body), resp_bytes=0, elapsed=elapsed,
                    usage=NO_USAGE, note="same_retry", attempt=attempt,
                )
            resp = None
            if not await same_retry_pause(delay):
                client_left = True
                log("  客户端已断开，不再同站重试")
                break
            same_mode = True
            continue
        if search_endpoint and resp.status_code in {404, 405, 501}:
            # These status codes mean this upstream has no standalone search
            # route. Do not count it as a site outage or cool the group down;
            # just try the next candidate, if one is configured.
            nxt = advance()
            if nxt >= 0:
                log(
                    f"  alpha/search unsupported ({resp.status_code}) at {label}，"
                    "关闭响应后尝试下一个候选"
                )
                with contextlib.suppress(Exception):
                    await resp.aclose()
                elapsed = time.monotonic() - began
                inflight.failed(call, status=resp.status_code, note="search_failed_over", ms=int(elapsed * 1000))
                if record:
                    _record(
                        request=request, route=route, proto=proto, model=asked,
                        remote_model=remote, status=resp.status_code, stream_flag=stream_flag,
                        req_bytes=len(sent_body), resp_bytes=0, elapsed=elapsed,
                        usage=NO_USAGE, note="search_failed_over", attempt=attempt,
                    )
                resp = None
                index = nxt
                continue
        if not site_bad and not candidate_bad:
            failover.note_ok(route.group_id)
            break
        if site_bad:
            failover.note_fail(route.group_id, resp.status_code, label)
            dead_groups.add(route.group_id)
        # 候选级的失败（404 名字没了；400/422 这站不认请求里的某个参数）既不算失败也
        # 不算成功：站是通的、key 是好的。所以不碰断路器，让同组的其它真名和后面的
        # 候选都还有机会
        nxt = advance()
        if nxt < 0:
            # 没有退路了就把上游的响应原样透传下去，和没有降级时的行为一字不差
            break
        # 还有候选可试：错误体不能挡住降级。没有退路时上面的 response 会照旧原样透传，
        # 这里直接关掉响应，日志只记状态和候选，不等待可能永远不来的正文。
        log(f"  上游返回 {resp.status_code}，关闭响应后换下一个候选")
        with contextlib.suppress(Exception):
            await resp.aclose()
        elapsed = time.monotonic() - began
        inflight.failed(call, status=resp.status_code, note="failed_over", ms=int(elapsed * 1000))
        if record:
            _record(
                request=request, route=route, proto=proto, model=asked,
                remote_model=remote, status=resp.status_code, stream_flag=stream_flag,
                req_bytes=len(sent_body), resp_bytes=0, elapsed=elapsed,
                usage=NO_USAGE, note="failed_over", attempt=attempt,
            )
        resp = None
        index = nxt

    if resp is None:
        if client_left:
            # 客户端在重试间隙走了：这不是上游的锅，记 499 让排障一眼看清
            inflight.finish(call, status=499, note="client_abort")
            return _error(proto, 499, "客户端已断开")
        if call.cancel_requested:
            # 中断恰好落在「没有候选可试」的那一瞬：也按手动中断记账
            inflight.finish(call, status=499, note="manual_abort")
            return _error(proto, 499, "请求已手动中断")
        blamed = last_route or route
        if fail is not None:
            why = f"上游 {blamed.upstream.name} 请求失败: {fail}"
        else:
            why = f"上游 {blamed.upstream.name} 没能给出可用的响应"
        if attempt > 1:
            why += f"（试过 {attempt} 个候选）"
        inflight.finish(call, status=502, note="connect_failed")
        return _error(proto, 502, why)

    won = route
    won_attempt = attempt
    won_remote = remote
    won_bytes = len(sent_body)
    upstream_resp = resp

    async def relay() -> AsyncIterator[bytes]:
        # 抓包只在 data/capture-stream.flag 存在时开，没开时 begin() 返回 None、
        # 下面每块字节只是多一次空判断。写完 max 条自动删 flag，不用回来关。
        cap = capture.begin(
            {
                "path": endpoint,
                "model": asked,
                "upstream": route.upstream.name,
                "group": route.group_name,
                "remote_model": route.remote_model,
                "protocol": proto.name,
                "requested_stream": bool(payload.get("stream")),
                "req_headers": capture.sanitize_headers(request.headers),
            },
            body,
        )
        sent = 0
        text_bytes = 0
        thinking = False
        # 发呆计时只在「开始吐字」之后生效：等响应头、等第一个字可能一两分钟，
        # 那是模型在算，不是卡住。观察器的 content_events 往前走一次就续一次期；
        # 已经发出完成事件的流也不再计时（有的站发完结束事件却不收连接）。
        content_deadline: float | None = None
        seen_content_events = 0
        compaction_observations: list[dict[str, object]] = []
        response_event_types: dict[str, int] = {}
        response_payload_types: dict[str, int] = {}
        content_type = upstream_resp.headers.get("content-type", "").split(";", 1)[0].strip().lower()
        observer = protocols.SSEObserver(proto) if content_type == "text/event-stream" else None
        json_body = bytearray()
        json_capped = False
        head = bytearray()
        tail = bytearray()
        note = "ok"
        # 第一块字节开始往下走才算「正在返回」：在这之前是「等上游出字」，两件事的
        # 处置完全不同（卡在等待是模型在想，卡在连接是站连不上）
        chunks = upstream_resp.aiter_bytes()
        try:
            while True:
                timeout: float | None = None
                if content_deadline is not None and not (observer is not None and observer.ended):
                    timeout = max(0.0, content_deadline - time.monotonic())
                try:
                    chunk = await inflight.wait_for_upstream(call, anext(chunks), timeout=timeout)
                except StopAsyncIteration:
                    break
                inflight.phase(call, inflight.STREAM)
                sent += len(chunk)
                if len(head) < HEAD_KEEP:
                    head.extend(chunk[: HEAD_KEEP - len(head)])
                    # Anthropic 把输入 token 放在流开头的 message_start 里，也就是说这个数
                    # 往往在第一块字节里就到手了 —— 比按包大小估准得多，「实时」页上直接用它。
                    # 头填满之前每来一块都试一次：数字可能正好被切成两半，extract_usage
                    # 取最大值，所以多试几次一定会读到完整的那个
                    if b"input_tokens" in head:
                        got = proto.extract_usage(bytes(head), b"")
                        inflight.usage(call, tokens_in=proto.context_tokens(got))
                tail.extend(chunk)
                if cap is not None:
                    cap.feed(chunk)
                # 观察器按完整 SSE 帧统计内容和结束事件；它不参与实际转发，解析出错也不能
                # 影响下面的原始 chunk。
                if observer is not None:
                    try:
                        observer.feed(chunk)
                        text_bytes = observer.text_bytes
                        thinking = observer.thinking
                        if stall_s > 0 and observer.content_events > seen_content_events:
                            seen_content_events = observer.content_events
                            content_deadline = time.monotonic() + stall_s
                        if compaction_capture:
                            compaction_observations = list(observer.compaction_items)
                            response_event_types = dict(observer.event_types)
                            response_payload_types = dict(observer.payload_types)
                    except Exception as exc:
                        log(f"  SSE observe failed: {exc.__class__.__name__}: {exc}")
                else:
                    # 非 SSE 响应没有观察器可看：第一块字节就算「开始出正文」，每来一块
                    # 续一次期 —— 块与块之间同样受发呆超时约束（chunked JSON 卡住也打断）
                    if stall_s > 0:
                        content_deadline = time.monotonic() + stall_s
                    # JSON 要收齐后再统计，字符串和 UTF-8 字符也可能被网络切开。
                    room = MAX_JSON_OBSERVE_BYTES - len(json_body)
                    if room > 0:
                        json_body.extend(chunk[:room])
                    if len(chunk) > room:
                        json_capped = True
                if len(tail) > TAIL_KEEP:
                    del tail[: len(tail) - TAIL_KEEP]
                inflight.progress(call, sent, text_bytes=text_bytes, thinking=thinking)
                yield chunk
        except inflight.ManualAbort:
            note = "manual_abort"
            log(f"  manually stopped after {sent}B")
        except inflight.UpstreamStall:
            # 开始吐字之后突然长时间没有新内容（也没有完成事件）：和手动打断一样
            # 切断这条流，下游收不到完成事件就会自己重发；不换站、不影响并发的其它请求
            note = "stall_timeout"
            log(
                f"  WARN 吐字后 {stall_s:g}s 没有新内容，按卡住打断"
                f" status={upstream_resp.status_code} resp={sent}B（等下游重发）"
            )
        except httpx.HTTPError as exc:
            note = "upstream_abort"
            log(f"  stream aborted: {exc.__class__.__name__}: {exc} after {sent}B")
            raise
        except (GeneratorExit, asyncio.CancelledError):
            # 很多上游发完完成事件后并不主动收连接，客户端（codex 就是这样）拿到完成事件
            # 就走了，我们这边还卡在等下一块。这属于正常收尾，不是异常。
            completed = observer is not None and observer.ended
            note = "ok" if completed else "client_abort"
            log(f"  client left after {sent}B (completed={completed})")
            raise
        else:
            if observer is not None:
                # 上游 EOF：没有空行收尾的最后一个帧也要看，不能把完整流误记成 truncated
                observer.flush()
            # 只有「上游说 200 且是流式」时缺完成事件才算被截断，4xx/5xx 本来就没有完成事件
            if observer is not None and upstream_resp.status_code < 300 and not observer.ended:
                note = "truncated"
                log(f"  WARN stream ended WITHOUT completion event status={upstream_resp.status_code} resp={sent}B")
            else:
                log(f"  done status={upstream_resp.status_code} resp={sent}B {time.monotonic() - started:.1f}s")
        finally:
            if cap is not None:
                capture.finish(
                    cap,
                    status=upstream_resp.status_code,
                    note=note,
                    sent=sent,
                    observer_ended=(observer.ended if observer is not None else None),
                    event_types=(dict(observer.event_types) if observer is not None else {}),
                    resp_headers=capture.sanitize_headers(upstream_resp.headers),
                )
            if json_capped:
                log(f"  JSON observe skipped: 响应体超过 {MAX_JSON_OBSERVE_BYTES // 1048576}MB，不解析 usage")
            if json_body and not json_capped:
                try:
                    json_payload = json.loads(json_body)
                    if isinstance(json_payload, dict):
                        text_bytes, thinking = proto.count_json_content(json_payload)
                        if compaction_capture:
                            compaction_observations = protocols.compaction_observations(json_payload)
                            response_payload_types = _probe_response_types(json_payload)
                except Exception as exc:
                    log(f"  JSON observe failed: {exc.__class__.__name__}: {exc}")
                inflight.progress(call, sent, text_bytes=text_bytes, thinking=thinking)
            if compaction_capture:
                _write_compaction_capture(
                    request=request,
                    payload=payload,
                    route=won,
                    remote_model=won_remote,
                    status=upstream_resp.status_code,
                    stream=stream_flag,
                    req_bytes=won_bytes,
                    resp_bytes=sent,
                    elapsed=time.monotonic() - started,
                    observations=compaction_observations,
                    response_event_types=response_event_types,
                    response_payload_types=response_payload_types,
                )
            # usage 抽一次给两处用：「实时」页要拿真数替掉按字节估的，转发记录要落库
            usage = proto.extract_usage(bytes(head), bytes(tail))
            # 先落库（纯同步，即使外层在取消也能跑完），再还连接
            inflight.finish(
                call, status=upstream_resp.status_code, note=note, sent=sent,
                tokens_in=proto.context_tokens(usage), tokens_out=usage[1] or 0,
            )
            if record:
                _record(
                    request=request, route=won, proto=proto, model=asked,
                    remote_model=won_remote, status=upstream_resp.status_code,
                    stream_flag=stream_flag, req_bytes=won_bytes,
                    resp_bytes=sent, elapsed=time.monotonic() - started,
                    usage=usage, note=note, attempt=won_attempt,
                    text_bytes=text_bytes, thinking=thinking,
                )
            with contextlib.suppress(Exception):
                await upstream_resp.aclose()

    drop = RESP_DROP | _connection_tokens(upstream_resp.headers)
    passthrough = {k: v for k, v in upstream_resp.headers.items() if k.lower() not in drop}
    # 解过的层不能留在头里（客户端会拿明文再解一次），没解掉的必须写回去
    keep_encoding = _resp_encoding_to_keep(upstream_resp)
    if keep_encoding:
        passthrough["content-encoding"] = keep_encoding
    return StreamingResponse(
        relay(),
        status_code=upstream_resp.status_code,
        headers=passthrough,
        media_type=upstream_resp.headers.get("content-type", "application/json"),
    )

def _record(
    *,
    request: Request,
    route: db.Route,
    proto: protocols.Protocol,
    model: str,
    remote_model: str,
    status: int,
    stream_flag: bool,
    req_bytes: int,
    resp_bytes: int,
    elapsed: float,
    usage: protocols.Usage,
    note: str,
    attempt: int = 1,
    text_bytes: int = 0,
    thinking: bool = False,
) -> None:
    input_tokens, output_tokens, cached_tokens, cache_creation_tokens = usage
    try:
        db.insert_request(
            client=_client_label(request.headers.get("user-agent", "")),
            model=model,
            remote_model=remote_model if remote_model != model else "",
            protocol=proto.name,
            upstream=route.upstream.name,
            group_name=route.group_name,
            status=status,
            stream=stream_flag,
            req_bytes=req_bytes,
            resp_bytes=resp_bytes,
            resp_text_bytes=text_bytes,
            thinking=thinking,
            duration_ms=int(elapsed * 1000),
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cached_tokens=cached_tokens,
            cache_creation_tokens=cache_creation_tokens,
            note=note,
            attempt=attempt,
        )
    except Exception as exc:  # 记日志失败绝不能影响转发本身
        log(f"  request_log insert failed: {exc}")


# ---------------------------------------------------------------- 路由
#
# 下游打哪个路径，就转发到上游同名的路径，不做任何格式转换。


@router.post("/v1/responses", response_model=None)
@router.post("/responses", response_model=None)
async def responses_proxy(request: Request) -> StreamingResponse | JSONResponse:
    return await forward(request, protocols.OPENAI, "/responses")


@router.post("/v1/responses/compact", response_model=None)
@router.post("/responses/compact", response_model=None)
async def responses_compact_proxy(request: Request) -> StreamingResponse | JSONResponse:
    """透传 Responses 的独立 compaction endpoint。

    compaction item 是上游生成的 opaque/encrypted 状态，不能在网关里解析、裁剪或重建；
    复用普通 Responses 转发路径可以保持模型改名、鉴权、流式观察和记录行为一致。
    """
    return await forward(request, protocols.OPENAI, "/responses/compact")


@router.post("/v1/alpha/search", response_model=None)
@router.post("/alpha/search", response_model=None)
async def standalone_search_proxy(request: Request) -> StreamingResponse | JSONResponse:
    """透传 Codex custom provider 的 standalone web search endpoint。"""
    return await forward(request, protocols.OPENAI, "/alpha/search")


@router.post("/v1/messages", response_model=None)
@router.post("/messages", response_model=None)
async def messages_proxy(request: Request) -> StreamingResponse | JSONResponse:
    return await forward(request, protocols.ANTHROPIC, "/messages")


@router.post("/v1/messages/count_tokens", response_model=None)
@router.post("/messages/count_tokens", response_model=None)
async def count_tokens_proxy(request: Request) -> StreamingResponse | JSONResponse:
    # Claude Code 用它算上下文占用。不记进转发记录，见 forward 的 record 参数
    return await forward(request, protocols.ANTHROPIC, "/messages/count_tokens", record=False)


@router.post("/v1/chat/completions", response_model=None)
@router.post("/chat/completions", response_model=None)
async def chat_completions_proxy(request: Request) -> StreamingResponse | JSONResponse:
    return await forward(request, protocols.CHAT, "/chat/completions")


@router.get("/v1/models")
@router.get("/models")
async def models_list(request: Request) -> JSONResponse:
    """一份清单同时满足两种形状。

    OpenAI 要 `object`/`owned_by`，Anthropic 要 `type`/`display_name`/`created_at` 和外层的
    `has_more`。两边都会忽略自己不认识的字段，所以不必为 Anthropic 另开一个路径。

    带了 `anthropic-version` 的客户端（Claude Code 就带）只给 Anthropic 接口下的模型，
    省得它在 `/model` 里列出一堆自己调不了的名字。认不出客户端时给全部，宁可多给。
    """
    names = db.exposed_models(
        protocols.ANTHROPIC.name if request.headers.get("anthropic-version") else ""
    )
    data = [
        {
            "id": name,
            "object": "model",
            "owned_by": "model-gateway",
            "type": "model",
            "display_name": name,
            "created_at": MODEL_CREATED_AT,
        }
        for name in names
    ]
    return JSONResponse(
        {
            "object": "list",
            "data": data,
            "has_more": False,
            "first_id": names[0] if names else None,
            "last_id": names[-1] if names else None,
        }
    )
