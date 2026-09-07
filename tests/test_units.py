"""端到端测试 · 不依赖网关进程的纯函数：完成标记的跨块识别、自签代理的证书固定。"""

from __future__ import annotations

import asyncio

import httpx
import pytest

from helpers import PROXY_CERT, PROXY_KEY, MockProxy, MockUpstream


def test_has_end_marker_spans_chunk_boundary():
    """传输层常会把小块合并，端到端测不稳，所以直接测这个纯函数。"""
    from gateway.proxy import has_end_marker

    buf = bytearray()
    for piece in (b'data: {"type": "response.comp', b'leted", "usage": {}}\n\n'):
        buf.extend(piece)
        found = has_end_marker(buf, len(piece))
    assert found, "标记被切成两半时必须靠回看窗口认出来"

    # 单块内的两种标记都要认
    assert has_end_marker(bytearray(b'data: [DONE]\n\n'), 14)
    assert has_end_marker(bytearray(b'x' * 100 + b'response.completed'), 18)
    # 只在新数据窗口里找：老数据里的标记不该被反复命中
    assert not has_end_marker(bytearray(b'response.completed' + b'y' * 500), 100)
    assert not has_end_marker(bytearray(b'data: {"type": "response.in_progress"}'), 38)
    assert not has_end_marker(bytearray(b'anything'), 0)


def test_has_end_marker_recognizes_anthropic_completion_event():
    from gateway.protocols import ANTHROPIC
    from gateway.proxy import has_end_marker

    markers = ANTHROPIC.end_markers
    buf = bytearray()
    for piece in (b"event: message_st", b'op\ndata: {"type":"message_stop"}\n\n'):
        buf.extend(piece)
        found = has_end_marker(buf, len(piece), markers)
    assert found, "结束事件被切成两半时也要认出来"
    assert not has_end_marker(bytearray(b'event: message_delta\ndata: {}'), 29, markers)
    # Responses API 的标记不该被 Anthropic 认成结束，反过来也一样
    assert not has_end_marker(bytearray(b"data: response.completed"), 24, markers)
    assert not has_end_marker(bytearray(b'data: {"type":"message_stop"}'), 29)


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
