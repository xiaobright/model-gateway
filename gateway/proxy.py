from __future__ import annotations

import asyncio
import contextlib
import json
import ssl
import time
from pathlib import Path
from typing import AsyncIterator

import httpx
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, StreamingResponse

from . import config, db, failover, inflight, naming, protocols
from .reqlog import log
from .upstream import endpoint as upstream_endpoint, parse_override

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

    代理 URL 允许带 `#ca=<pem 路径>` 的尾巴：自签证书的 https 代理（VPS 上 gost 那扇门）
    用系统根证书验不过，把签发的那张钉进 client 的信任列表 —— 系统根证书原样保留，
    不影响别的站。片段传给 httpx 前剥掉，它不认识这东西。相对路径按仓库根解析，
    网关从哪个目录启动都一样。
    """
    egress = (egress or "").strip()
    proxy = None if egress in (EGRESS_SYSTEM, EGRESS_DIRECT) else egress
    args: dict = {
        "mounts": {} if proxy else loopback_mounts(),
        "trust_env": egress == EGRESS_SYSTEM,
        "proxy": proxy,
    }
    if proxy:
        base, _, frag = proxy.partition("#")
        if frag:
            args["proxy"] = base
            if not frag.startswith("ca=") or len(frag) == 3:
                raise ValueError(f"代理 URL 的 # 片段只认 ca=<证书路径>（收到 {frag!r}）")
            ca = Path(frag[3:])
            if not ca.is_absolute():
                ca = config.PROJECT_ROOT / ca
            if not ca.is_file():
                raise ValueError(f"#ca 指的证书文件不存在：{ca}")
            ctx = ssl.create_default_context()
            ctx.load_verify_locations(cafile=str(ca))
            # httpx 连代理这一跳用的是 Proxy 对象上单独的 ssl_context，client 的
            # verify 管不到它 —— 钉证书必须钉在这里
            args["proxy"] = httpx.Proxy(httpx.URL(base), ssl_context=ctx)
    return args


REQ_DROP = {"host", "content-length", "transfer-encoding", "connection", "keep-alive"}
# content-encoding/content-length 必须去掉：relay 用 aiter_bytes 解压后转发，
# 客户端拿到的是未压缩字节流（上游的 usage/完成事件检测也依赖解压后的内容）
RESP_DROP = {"transfer-encoding", "connection", "keep-alive", "content-encoding", "content-length"}
REDACT_ON_CAPTURE = {"authorization", "cookie", "proxy-authorization", "x-api-key"}

HEAD_KEEP = 8192   # 开头留这么多：Anthropic 的输入 token 只在流开头的 message_start 里报一次
TAIL_KEEP = 65536  # 末尾留这么多，用来抓 usage 和完成标记

# 连上游都没连上时的 usage：一个数都没有
NO_USAGE: protocols.Usage = (None, None, None)

# /v1/models 的 Anthropic 形状要求每项带 created_at，值本身没有客户端会用
MODEL_CREATED_AT = "2025-01-01T00:00:00Z"

def has_end_marker(
    buf: bytes | bytearray,
    fresh: int,
    markers: tuple[bytes, ...] = protocols.OPENAI.end_markers,
) -> bool:
    """在缓冲区末尾 fresh 个新字节里找完成标记。

    多带 overlap 字节回看，否则标记正好被 TCP 切成两半时会漏掉，
    整条流就会被误判成 truncated。
    """
    if fresh <= 0:
        return False
    overlap = max(len(m) for m in markers) - 1
    window = bytes(buf[-(fresh + overlap):])
    return any(m in window for m in markers)


router = APIRouter()

_clients: dict[str, httpx.AsyncClient] = {}
_client_loop: asyncio.AbstractEventLoop | None = None


def get_client(egress: str = EGRESS_SYSTEM) -> httpx.AsyncClient:
    """按「出口」复用 client，省掉每个请求一次 TLS 握手（对远端公益站是几百 ms 的差别）。

    一个出口一个 client：代理是建 client 时定的，没法按请求换。出口最多也就三五种，
    池子小得可以忽略。
    """
    global _client_loop
    loop = asyncio.get_running_loop()
    if _client_loop is not loop:
        _clients.clear()
        _client_loop = loop
    client = _clients.get(egress)
    if client is None or client.is_closed:
        client = httpx.AsyncClient(
            timeout=PROXY_TIMEOUT, limits=PROXY_LIMITS, **client_args(egress)
        )
        _clients[egress] = client
    return client


async def aclose_client() -> None:
    global _client_loop
    for client in list(_clients.values()):
        if not client.is_closed:
            with contextlib.suppress(Exception):
                await client.aclose()
    _clients.clear()
    _client_loop = None


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


def _maybe_capture_headers(request: Request) -> None:
    """调试用：放一个 data/capture.flag，下一个请求的头会被 dump 出来（敏感头打码）。"""
    flag = config.DATA_DIR / "capture.flag"
    if not flag.exists():
        return
    dump = {
        k: ("<redacted>" if k.lower() in REDACT_ON_CAPTURE else v)
        for k, v in request.headers.items()
    }
    try:
        (config.DATA_DIR / "captured_headers.json").write_text(
            json.dumps(dump, indent=2, ensure_ascii=False), encoding="utf-8"
        )
        flag.unlink()
        log("captured real client headers to data/captured_headers.json")
    except OSError:
        pass


def _build_headers(
    request: Request,
    upstream: db.Upstream,
    proto: protocols.Protocol,
    want_1m: bool = False,
) -> dict[str, str]:
    # ASGI 保证头名已经小写，所以下面用小写键既能覆盖客户端的同名头，也不会两份并存
    headers = {k: v for k, v in request.headers.items() if k.lower() not in REQ_DROP}
    for key, value in proto.defaults.items():
        headers.setdefault(key, value)
    if upstream.api_key:
        headers.update(proto.auth_headers(upstream.api_key))
    if want_1m and proto.beta_header:
        headers[proto.beta_header] = naming.add_beta(headers.get(proto.beta_header, ""), naming.BETA_1M)
    # 覆写放最后：它要能改掉上面自动加的任何头（值写 null 表示删掉那个头）
    for key, value in parse_override(upstream.header_override).items():
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
    body = await request.body()
    try:
        payload = json.loads(body)
        if not isinstance(payload, dict):
            raise ValueError("not a dict")
    except (json.JSONDecodeError, UnicodeDecodeError, ValueError):
        return _error(proto, 400, "请求体不是合法 JSON")

    requested = str(payload.get("model", ""))
    asked, flag = naming.split_model(requested)
    chain = db.resolve_chain(asked, proto.name)
    if not chain:
        # 模型录在另一个接口下时说清楚：这种 404 光看「未配置」会以为是没导入
        elsewhere = db.protocol_of_model(asked)
        why = (
            f"模型 {requested!r} 是在 {elsewhere} 接口下暴露的，不能从 {endpoint} 调用"
            if elsewhere and elsewhere != proto.name
            else f"模型 {requested!r} 未配置或当前上游已停用"
        )
        log(f"POST {endpoint} model={requested!r} -> 404 ({why}) req={len(body)}B")
        return _error(proto, 404, why)
    if chain[0].model_name != asked:
        log(f"POST {endpoint} model={asked!r} 没配过，按档位关键字落到 {chain[0].model_name!r}")

    stream_flag = bool(payload.get("stream"))
    # 有副作用的 OpenAI 请求不降级：上游可能已经把它存下来了才失败，重试会留下两条
    stateful = payload.get("store") is not None or payload.get("previous_response_id") is not None
    can_failover = failover.enabled(proto.name) and len(chain) > 1 and not stateful
    candidates = failover.order_chain(chain) if can_failover else [chain[0]]

    started = time.monotonic()
    resp: httpx.Response | None = None
    route = candidates[0]
    remote = ""
    sent_body = body
    attempt = 0
    index = 0
    fail: Exception | None = None
    # 这个请求里站级失败过的分组。整个跳掉（含它下面同模型的其它候选）：同一个站
    # 绝不在一次请求里立刻重试。模型级的 404 不进这里 —— 那是名字的问题不是站的问题
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
        """还该不该再打一个？返回下一个候选的下标，-1 = 到此为止。"""
        if attempt >= failover.MAX_ATTEMPTS:
            return -1
        return failover.next_index(candidates, index + 1, dead_groups)

    # 这里是「自动降级」唯一安全的落点：状态码已经拿到手，但还没往下游发过任何字节，
    # 换个上游重试客户端完全无感。第一个字节一旦发出去就不能再换了。
    while True:
        route = candidates[index]
        attempt += 1
        if attempt > 1:
            if await request.is_disconnected():
                attempt -= 1
                log(f"  客户端已经走了，不再降级（试过 {attempt} 个）")
                break
            if time.monotonic() - started > failover.START_DEADLINE:
                attempt -= 1
                log(f"  已经耗了 {time.monotonic() - started:.0f}s，不再开新尝试")
                break

        # 上游只认它那边的真名。[1m] 是 Claude Code 自己的档位约定，请求侧和配置侧都可能带，一并摘掉
        remote, remote_flag = naming.split_model(route.remote_model)
        want_1m = naming.wants_1m(flag) or naming.wants_1m(remote_flag)
        sent_body = body
        if remote != requested:
            # ensure_ascii=False：否则中文请求体会涨三到六倍
            sent_body = json.dumps(
                {**payload, "model": remote}, ensure_ascii=False, separators=(",", ":")
            ).encode()

        _maybe_capture_headers(request)
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
            client = get_client(route.upstream.egress)
            resp = await client.send(
                client.build_request("POST", url, content=sent_body, headers=headers), stream=True
            )
        except (httpx.HTTPError, ValueError) as exc:
            fail, resp = exc, None
            elapsed = time.monotonic() - began
            log(
                f"POST {endpoint} model={requested!r} upstream={route.upstream.name} -> 502 "
                f"({exc.__class__.__name__}: {exc}) after {elapsed:.1f}s req={len(sent_body)}B"
                f"{'' if attempt == 1 else f' [第 {attempt} 次尝试]'}"
            )
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

        inflight.phase(call, inflight.WAIT, status=resp.status_code)
        detail = ""
        if payload.get("store") is not None or payload.get("previous_response_id") is not None:
            detail = f" store={payload.get('store')} prev_id={payload.get('previous_response_id')!r}"
        log(
            f"POST {endpoint} model={requested!r} upstream={route.upstream.name} remote={remote!r} "
            f"-> {resp.status_code} stream={stream_flag}{' 1m' if want_1m else ''}{detail} "
            f"req={len(sent_body)}B ua={request.headers.get('user-agent', '')[:48]!r}"
            f"{'' if attempt == 1 else f' [第 {attempt} 次尝试]'}"
        )

        site_bad = resp.status_code in failover.RETRY_STATUS
        model_bad = resp.status_code in failover.MODEL_STATUS
        if not site_bad and not model_bad:
            failover.note_ok(route.group_id)
            break
        if site_bad:
            failover.note_fail(route.group_id, resp.status_code, label)
            dead_groups.add(route.group_id)
        # 模型级的 404 既不算失败也不算成功：站是通的、key 是好的，只是这个名字没了。
        # 所以不碰断路器，让同一个分组里的下一条真名还有机会
        nxt = advance()
        if nxt < 0:
            # 没有退路了就把上游的响应原样透传下去，和没有降级时的行为一字不差
            break
        # 还有候选可试：把这次的错误体读出来记一行（错误体都很小），然后换下一个
        with contextlib.suppress(Exception):
            raw = await resp.aread()
            log(f"  上游返回 {resp.status_code}，换下一个候选。响应开头: {raw[:180]!r}")
        elapsed = time.monotonic() - began
        inflight.failed(call, status=resp.status_code, note="failed_over", ms=int(elapsed * 1000))
        if record:
            _record(
                request=request, route=route, proto=proto, model=asked,
                remote_model=remote, status=resp.status_code, stream_flag=stream_flag,
                req_bytes=len(sent_body), resp_bytes=0, elapsed=elapsed,
                usage=NO_USAGE, note="failed_over", attempt=attempt,
            )
        with contextlib.suppress(Exception):
            await resp.aclose()
        resp = None
        index = nxt

    if resp is None:
        if fail is not None:
            why = f"上游 {route.upstream.name} 请求失败: {fail}"
        else:
            why = f"上游 {route.upstream.name} 没能给出可用的响应"
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
        sent = 0
        text_bytes = 0
        thinking = False
        seen_end = False
        head = bytearray()
        tail = bytearray()
        note = "ok"
        # 第一块字节开始往下走才算「正在返回」：在这之前是「等上游出字」，两件事的
        # 处置完全不同（卡在等待是模型在想，卡在连接是站连不上）
        inflight.phase(call, inflight.STREAM)
        try:
            async for chunk in upstream_resp.aiter_bytes():
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
                # 这一块里有多少字节是真内容、有没有思维链。整条响应的字节数里 SSE 帧
                # 占了大头，拿它折 token 会差十倍；而思维链发来的是总结、计费按完整的算，
                # 所以这两件事都得单独记，见 protocols.count_content
                got, think = proto.count_content(chunk)
                text_bytes += got
                thinking = thinking or think
                if not seen_end and has_end_marker(tail, len(chunk), proto.end_markers):
                    seen_end = True
                if len(tail) > TAIL_KEEP:
                    del tail[: len(tail) - TAIL_KEEP]
                inflight.progress(call, sent, text_bytes=text_bytes, thinking=thinking)
                yield chunk
        except httpx.HTTPError as exc:
            note = "upstream_abort"
            log(f"  stream aborted: {exc.__class__.__name__}: {exc} after {sent}B")
            raise
        except (GeneratorExit, asyncio.CancelledError):
            # 很多上游发完完成事件后并不主动收连接，客户端（codex 就是这样）拿到完成事件
            # 就走了，我们这边还卡在等下一块。这属于正常收尾，不是异常。
            note = "ok" if seen_end else "client_abort"
            log(f"  client left after {sent}B (completed={seen_end})")
            raise
        else:
            # 只有「上游说 200 且是流式」时缺完成事件才算被截断，4xx/5xx 本来就没有完成事件
            if stream_flag and upstream_resp.status_code < 300 and not seen_end:
                note = "truncated"
                log(f"  WARN stream ended WITHOUT completion event status={upstream_resp.status_code} resp={sent}B")
            else:
                log(f"  done status={upstream_resp.status_code} resp={sent}B {time.monotonic() - started:.1f}s")
        finally:
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

    passthrough = {k: v for k, v in upstream_resp.headers.items() if k.lower() not in RESP_DROP}
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
    input_tokens, output_tokens, cached_tokens = usage
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


@router.post("/v1/messages", response_model=None)
@router.post("/messages", response_model=None)
async def messages_proxy(request: Request) -> StreamingResponse | JSONResponse:
    return await forward(request, protocols.ANTHROPIC, "/messages")


@router.post("/v1/messages/count_tokens", response_model=None)
@router.post("/messages/count_tokens", response_model=None)
async def count_tokens_proxy(request: Request) -> StreamingResponse | JSONResponse:
    # Claude Code 用它算上下文占用。不记进转发记录，见 forward 的 record 参数
    return await forward(request, protocols.ANTHROPIC, "/messages/count_tokens", record=False)


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

