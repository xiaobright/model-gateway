"""端到端测试 · 不依赖网关进程的纯函数：完成标记的跨块识别、自签代理的证书固定。"""

from __future__ import annotations

import asyncio
import json
import urllib.request

import httpx
import pytest

from helpers import PROXY_CERT, PROXY_KEY, MockProxy, MockUpstream


def test_sse_observer_spans_chunks_and_counts_content_once():
    """事件边界和内容统计都不应受传输层 chunk 分片影响。"""
    from gateway.protocols import OPENAI, SSEObserver

    observer = SSEObserver(OPENAI)
    for piece in (
        b'data: {"delta":"hello ',
        b'world"}\n\n',
        b'data: {"delta":"literal [DONE]"}\n\n',
    ):
        observer.feed(piece)
    assert observer.text_bytes == len("hello world") + len("literal [DONE]")
    assert not observer.ended, "正文里的 [DONE] 不是完成事件"

    observer.feed(b'data: {"type":"response.comp')
    observer.feed(b'leted", "usage": {}}\n\n')
    assert observer.ended, "完成事件跨 chunk 也必须识别"


def test_sse_observer_requires_the_protocol_specific_event():
    from gateway.protocols import ANTHROPIC, SSEObserver

    observer = SSEObserver(ANTHROPIC)
    observer.feed(b"event: message_st")
    observer.feed(b"op\ndata: {}\n\n")
    assert observer.ended, "Anthropic 的真实事件类型跨 chunk 也能结束"

    data_only = SSEObserver(ANTHROPIC)
    data_only.feed(b'data: {"type":"message_stop"}\n\n')
    assert data_only.ended, "没有 event 头时使用 data JSON 的 type"

    wrong = SSEObserver(ANTHROPIC)
    wrong.feed(b"data: response.completed\n\n")
    assert not wrong.ended


def test_sse_observer_keeps_an_incomplete_frame_for_the_next_chunk():
    from gateway.protocols import OPENAI, SSEObserver

    observer = SSEObserver(OPENAI)
    observer.feed(b'data: {"delta":"hello')
    assert observer.text_bytes == 0 and not observer.ended
    observer.feed(b' world"}\n\n')
    assert observer.text_bytes == len("hello world")


def test_compaction_probe_recognizes_nested_summary_and_cmp_encrypted_items():
    from gateway.protocols import compaction_observations

    payload = {
        "type": "response.completed",
        "response": {
            "object": "response.compaction",
            "output": [
                {"type": "compaction_summary", "id": "cmp_summary", "encrypted_content": "opaque"},
                {"id": "cmp_adapter", "encrypted_content": "opaque-2"},
            ],
        },
    }
    got = compaction_observations(payload)
    assert [item["item_id"] for item in got] == ["cmp_summary", "cmp_adapter"]
    assert got[0]["encrypted_content_bytes"] == len("opaque")
    assert all("encrypted_content" not in item for item in got)


def test_sse_observer_records_event_and_payload_type_inventory():
    from gateway.protocols import OPENAI, SSEObserver

    observer = SSEObserver(OPENAI)
    observer.feed(
        b'event: response.output_item.done\ndata: {"type":"response.output_item.done",'
        b'"item":{"type":"compaction","id":"cmp_1","encrypted_content":"x"}}\n\n'
    )
    assert observer.event_types == {"response.output_item.done": 1}
    assert observer.payload_types == {"response.output_item.done": 1}
    assert observer.compaction_items[0]["item_id"] == "cmp_1"


def test_openai_json_content_accepts_codex_alpha_search_string_output():
    from gateway.protocols import openai_json_content

    size, thinking = openai_json_content({"output": "source-backed search text"})
    assert size == len("source-backed search text")
    assert not thinking


def test_waiting_for_headers_can_be_cancelled_when_client_disconnects():
    from gateway.proxy import ClientDisconnected, _send_until_headers

    started = asyncio.Event()
    cancelled = asyncio.Event()

    class Request:
        checks = 0

        async def is_disconnected(self):
            self.checks += 1
            return self.checks > 1

    class Client:
        async def send(self, prepared, *, stream):
            started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                cancelled.set()
                raise

    async def run():
        task = asyncio.create_task(_send_until_headers(Request(), Client(), object()))
        await started.wait()
        with pytest.raises(ClientDisconnected):
            await task

    asyncio.run(run())
    assert cancelled.is_set(), "下游断开后必须取消等待响应头的上游任务"


def test_cancelling_as_headers_arrive_closes_the_response():
    """响应头刚到、轮询还没把 response 交出去时取消，也不能泄漏连接。"""
    from gateway.proxy import _send_until_headers

    async def run():
        headers_arrived = asyncio.Event()
        response = httpx.Response(200, stream=httpx.ByteStream(b"unused"))

        class Request:
            async def is_disconnected(self):
                return False

        class Client:
            async def send(self, prepared, *, stream):
                headers_arrived.set()
                return response

        task = asyncio.create_task(_send_until_headers(Request(), Client(), object()))
        await headers_arrived.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert response.is_closed

    asyncio.run(run())


def test_system_proxy_change_rebuilds_the_cached_client(monkeypatch):
    """系统代理开关改变后，跟随系统的出口不能继续沿用旧直连 client。"""
    from gateway import proxy

    current = {}
    monkeypatch.setattr(urllib.request, "getproxies", lambda: dict(current))

    async def run():
        await proxy.aclose_client()
        direct = await proxy.get_client()
        current.update({"http": "http://127.0.0.1:7890", "https": "http://127.0.0.1:7890"})
        proxied = await proxy.get_client()
        assert proxied is not direct
        await proxy.aclose_client()

    asyncio.run(run())


@pytest.mark.network
def test_ca_pin_trusts_a_self_signed_proxy_and_nothing_else_does():
    """#ca= 把自签代理的证书钉进信任列表；不钉就过不了 TLS —— 这正是它的用处。

    自签的 https 代理（VPS 上 gost 那扇门）系统根证书验不了：钉了签发的那张就能过，
    而且系统根证书原样保留，不影响别的站。
    """
    from gateway import proxy as proxy_mod

    with MockProxy(certfile=PROXY_CERT, keyfile=PROXY_KEY) as px, MockUpstream("siteA") as a:
        egress = f"https://me:pw@127.0.0.1:{px.port}#ca={PROXY_CERT}"
        args = proxy_mod.client_args(egress)
        # 片段是给网关看的，httpx 不认识，传之前剥掉；钉的证书挂在 Proxy 对象上
        # （httpx 连代理那一跳认的是它自己的 ssl_context，不是 client 的 verify）
        assert isinstance(args["proxy"], httpx.Proxy)
        assert args["proxy"].url.host == "127.0.0.1" and args["proxy"].url.port == px.port
        assert not args["proxy"].url.fragment, "片段剥掉了，httpx 不认识"
        assert args["proxy"].auth == ("me", "pw"), "凭据还在，只是挪进了 auth"

        async def through(via: dict) -> None:
            async with httpx.AsyncClient(**via) as c:
                await c.get(f"http://127.0.0.1:{a.port}/v1/models")

        asyncio.run(through(args))
        assert px.seen, "字节没从 TLS 门过"

        # 同一扇门，不钉证书：TLS 握手就过不去（自签的不在系统信任列表里）
        bare = proxy_mod.client_args(f"https://me:pw@127.0.0.1:{px.port}")
        with pytest.raises(httpx.HTTPError) as ei:
            asyncio.run(through(bare))
        assert "certificate" in str(ei.value).lower()


def test_client_args_verifies_upstream_tls_against_the_system_trust_store():
    """上游 TLS 用系统证书库验：卡巴斯基这类杀软 MITM 的根也能过（2026-09-11 实锤）。

    httpx 自带的 CA 捆绑包不认杀软装进系统库的根证书，curl/浏览器能通、网关 502。
    truststore 装了就带上 verify，没装则不加、行为原样（可选依赖）。
    """
    from gateway import proxy as proxy_mod

    try:
        import truststore  # noqa: F401
        installed = True
    except ImportError:
        installed = False

    args = proxy_mod.client_args("")
    if installed:
        import ssl as _ssl
        assert isinstance(args.get("verify"), _ssl.SSLContext)
    else:
        assert "verify" not in args


def test_input_item_census_records_the_role_alongside_the_type():
    """抓形状时连 role 一起记 —— `role: "developer"` 这个坑就是这么被看出来的。

    Codex Desktop 的 "responses lite" 线格式不发顶层 `instructions`，系统提示词是
    `role: "developer"` 的 message item。只数 type 会看到「一堆普通 message」，
    什么都看不出来；把 role 记下来，下次抓包一眼就知道该映射哪一个。
    """
    from gateway.proxy import _input_item_census

    types, roles = _input_item_census({
        "input": [
            {"type": "additional_tools", "role": "developer", "tools": []},
            {"type": "message", "role": "developer", "content": [{"type": "input_text", "text": "sys"}]},
            {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "hi"}]},
            {"type": "reasoning", "id": "rs_1", "summary": []},
            "just a string",
        ],
    })

    assert types == {"additional_tools": 1, "message": 2, "reasoning": 1, "str": 1}
    assert roles == {"additional_tools:developer": 1, "message:developer": 1, "message:user": 1}


def test_input_item_census_handles_a_scalar_input():
    from gateway.proxy import _input_item_census

    types, roles = _input_item_census({"input": "hello"})
    assert types == {"str": 1} and roles == {}


# ------------------------------------------------------------------ 抓包
# 抓包是「以后再出问题不用改代码」的兜底手段，所以它自己的开关语义必须准：
# 关着的时候一个字节都不该落盘、一条都不该消耗；抓满要自己停。


def test_capture_is_off_without_the_flag_file(tmp_path, monkeypatch):
    from gateway import capture, config

    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    capture.disable()

    assert not capture.enabled()
    assert capture.begin({"model": "m"}) is None, "没开抓包时 begin 必须返回 None"
    assert not (tmp_path / capture.OUT_DIRNAME).exists(), "关着的时候不该建目录"


def test_capture_writes_request_body_and_raw_stream(tmp_path, monkeypatch):
    from gateway import capture, config

    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    capture.enable(1)

    cap = capture.begin({"model": "deepseek-chat", "path": "/v1/chat/completions"}, b'{"stream":true}')
    assert cap is not None
    cap.feed(b'data: {"delta":"a"}\n\n')
    cap.feed(b"data: [DONE]\n\n")
    capture.finish(cap, status=200, note="ok", sent=35)

    out = cap.dir
    assert (out / "request.json").read_bytes() == b'{"stream":true}'
    assert (out / "stream.sse").read_bytes() == b'data: {"delta":"a"}\n\ndata: [DONE]\n\n'
    meta = json.loads((out / "meta.json").read_text("utf-8"))
    assert meta["status"] == 200 and meta["note"] == "ok"
    assert meta["stream_bytes"] == 35 and meta["stream_capped"] is False
    assert meta["model"] == "deepseek-chat"


def test_capture_stops_itself_after_the_requested_number(tmp_path, monkeypatch):
    """抓满 max 条自动删 flag —— 开了就忘了关也不会把磁盘写满。"""
    from gateway import capture, config

    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    capture.enable(2)

    first = capture.begin({"model": "a"})
    capture.finish(first)
    assert capture.enabled(), "第二条还没抓，flag 不该没"

    second = capture.begin({"model": "b"})
    capture.finish(second)
    assert not capture.enabled(), "抓满两条后必须自动停"
    assert not (tmp_path / capture.FLAG_NAME).exists()
    assert capture.begin({"model": "c"}) is None


def test_capture_honours_the_flag_file_deleted_from_outside(tmp_path, monkeypatch):
    """flag 文件是权威来源：外部直接删掉，进程内计数还剩下的也算停。"""
    from gateway import capture, config

    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    capture.enable(5)
    (tmp_path / capture.FLAG_NAME).unlink()

    assert not capture.enabled()
    assert capture.begin({"model": "a"}) is None


def test_capture_tolerates_a_garbage_flag_file(tmp_path, monkeypatch):
    from gateway import capture, config

    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    (tmp_path / capture.FLAG_NAME).write_text("not json at all", encoding="utf-8")

    assert capture.enabled()
    assert capture.status()["max"] == capture.DEFAULT_MAX_REQUESTS, "坏 JSON 按「抓 1 条」兜底"


def test_capture_caps_a_huge_stream_without_breaking(tmp_path, monkeypatch):
    from gateway import capture, config

    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(capture, "MAX_STREAM_BYTES", 100)
    capture.enable(1)

    cap = capture.begin({"model": "big"})
    cap.feed(b"x" * 250)
    capture.finish(cap, status=200)

    meta = json.loads((cap.dir / "meta.json").read_text("utf-8"))
    assert len((cap.dir / "stream.sse").read_bytes()) == 100, "写盘封顶"
    assert meta["stream_bytes"] == 250, "但真实字节数要照实记，不然看不出是被截的"
    assert meta["stream_capped"] is True


def test_capture_never_writes_secrets_into_meta(tmp_path, monkeypatch):
    from gateway import capture

    headers = {
        "authorization": "Bearer sk-xxx",
        "x-api-key": "secret",
        "x-goog-api-key": "secret",
        "x-some-token": "secret",
        "content-type": "application/json",
        "user-agent": "opencode/1.2.3",
    }

    clean = capture.sanitize_headers(headers)

    assert clean == {"content-type": "application/json", "user-agent": "opencode/1.2.3"}
    assert capture.sanitize_headers(None) == {}


def test_accept_encoding_is_rewritten_not_copied_from_the_client():
    """客户端报的压缩算法我们不一定解得了 —— 必须按网关自己的解码能力重写。

    opencode 带 `accept-encoding: gzip, deflate, br, zstd`，上游挑了 br，而 httpx
    没装 brotli 时解不开，转发给客户端的就是一坨压缩原文：客户端一个 SSE 事件都
    解析不出来（表现为「没回复」），网关这边等不到完成事件，于是记 truncated。
    """
    from starlette.requests import Request

    from gateway import protocols
    from gateway import proxy as proxy_mod

    class _Upstream:
        api_key = "sk-test"
        header_override = ""

    request = Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/v1/chat/completions",
            "headers": [
                (b"accept-encoding", b"gzip, deflate, br, zstd"),
                (b"content-type", b"application/json"),
            ],
            "query_string": b"",
        }
    )

    sent = proxy_mod._build_headers(request, _Upstream(), protocols.OPENAI)

    assert sent["accept-encoding"] == proxy_mod.accept_encoding()
    # 报出去的每一种都得真的解得了，否则又是一坨压缩原文发给客户端
    from httpx._decoders import SUPPORTED_DECODERS

    for name in sent["accept-encoding"].split(","):
        token = name.strip()
        assert token in SUPPORTED_DECODERS or token.encode() in SUPPORTED_DECODERS, (
            f"{token} 报了却解不开"
        )


def test_undecodable_content_encoding_is_left_for_the_client():
    """解不开的压缩不能摘 content-encoding —— 摘了客户端就不知道该怎么解了。

    httpx 解压后响应头里的 content-encoding 还留着，所以「摘不摘」得看我们到底
    解没解开：解开了才摘（我们转的就是明文），解不开就留着让客户端自己解。
    """
    import httpx

    from gateway import proxy as proxy_mod

    decodable = httpx.Response(200, headers={"content-encoding": "gzip"})
    assert "content-encoding" in proxy_mod.RESP_DROP
    assert proxy_mod._resp_drop(decodable) is proxy_mod.RESP_DROP, "解得开：照旧摘掉"

    unknown = httpx.Response(200, headers={"content-encoding": "snappy"})
    assert "content-encoding" not in proxy_mod._resp_drop(unknown), "解不开：必须留给客户端"

    plain = httpx.Response(200, headers={"content-encoding": "identity"})
    assert proxy_mod._resp_drop(plain) is proxy_mod.RESP_DROP
    assert proxy_mod._resp_drop(httpx.Response(200)) is proxy_mod.RESP_DROP, "没压缩也照旧"
