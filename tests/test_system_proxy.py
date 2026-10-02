"""系统代理快照、实际 transport 选择与切换时的连接生命周期。"""

from __future__ import annotations

import asyncio
import socket
import ssl
import urllib.request
from types import SimpleNamespace

import httpx
import pytest

from gateway import upstream
from helpers import MockProxy, MockUpstream, add_route, add_upstream, provider_id


@pytest.fixture
def windows_proxy(monkeypatch):
    state = {}
    monkeypatch.setattr(upstream, "sys", SimpleNamespace(platform="win32"))
    monkeypatch.setattr(urllib.request, "getproxies_registry", lambda: dict(state), raising=False)
    # 与注册表冲突，确认系统开关关闭时不会被继承的环境变量重新带进代理。
    for name in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY"):
        monkeypatch.setenv(name, "http://127.0.0.1:9")
    return state


@pytest.fixture
def lightweight_tls(monkeypatch):
    # 纯 transport 选择测试不连接网络，不需要加载本机根证书。
    monkeypatch.setattr(upstream, "system_ssl_context", lambda: ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT))


def selected_proxy(client, url="https://model.example/v1/responses"):
    pool = client._transport_for_url(httpx.URL(url))._pool
    proxy = getattr(pool, "_proxy_url", None)
    return None if proxy is None else (proxy.host.decode(), proxy.port)


def test_windows_off_on_change_off_ignores_inherited_environment(windows_proxy, lightweight_tls):
    async def run():
        try:
            off = await upstream.get_client()
            assert selected_proxy(off) is None
            windows_proxy.update(http="http://127.0.0.1:7890", https="http://127.0.0.1:7890")
            on = await upstream.get_client()
            assert selected_proxy(on) == ("127.0.0.1", 7890)
            assert await upstream.get_client() is on
            assert off.is_closed
            windows_proxy["https"] = "http://127.0.0.1:7891"
            changed = await upstream.get_client()
            assert selected_proxy(changed) == ("127.0.0.1", 7891)
            assert on.is_closed
            windows_proxy.clear()
            off_again = await upstream.get_client()
            assert selected_proxy(off_again) is None
            assert changed.is_closed
            assert len(upstream._clients) == 1
            assert not upstream._retired_clients
        finally:
            await upstream.aclose_client()
    asyncio.run(run())


def test_switch_during_client_construction_uses_one_snapshot(monkeypatch, windows_proxy, lightweight_tls):
    reads = []

    def read_then_switch():
        snapshot = dict(windows_proxy)
        reads.append(snapshot)
        windows_proxy.update(http="http://127.0.0.1:7890", https="http://127.0.0.1:7890")
        return snapshot

    monkeypatch.setattr(urllib.request, "getproxies_registry", read_then_switch)

    async def run():
        try:
            off = await upstream.get_client()
            assert reads == [{}], "建 client 时不允许 httpx 再读一次代理"
            assert selected_proxy(off) is None
            on = await upstream.get_client()
            assert len(reads) == 2
            assert selected_proxy(on) == ("127.0.0.1", 7890)
            windows_proxy.clear()
            off_again = await upstream.get_client()
            assert len(reads) == 3
            assert selected_proxy(off_again) is None, "关闭状态不能命中被代理污染的缓存"
        finally:
            await upstream.aclose_client()
    asyncio.run(run())


def test_other_platforms_keep_environment_proxy_and_bypass(monkeypatch, lightweight_tls):
    monkeypatch.setattr(upstream, "sys", SimpleNamespace(platform="linux"))
    monkeypatch.setattr(urllib.request, "getproxies", urllib.request.getproxies_environment)
    for name in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY"):
        monkeypatch.delenv(name, raising=False)
        monkeypatch.delenv(name.lower(), raising=False)
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:7890")
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:7891")
    monkeypatch.setenv("ALL_PROXY", "http://127.0.0.1:7892")
    monkeypatch.setenv("NO_PROXY", "example.com,.internal,10.0.0.1,::1,localhost,*.wild.test")

    async def run():
        try:
            client = await upstream.get_client()
            assert selected_proxy(client, "http://outside.test") == ("127.0.0.1", 7890)
            assert selected_proxy(client, "https://outside.test") == ("127.0.0.1", 7891)
            for host in ("example.com", "sub.example.com", "sub.internal", "10.0.0.1", "[::1]", "localhost", "127.0.0.1", "sub.wild.test"):
                assert selected_proxy(client, f"https://{host}") is None, host
            assert selected_proxy(client, "https://notexample.com") == ("127.0.0.1", 7891)
            assert selected_proxy(client, "https://internal") == ("127.0.0.1", 7891)
            monkeypatch.setenv("NO_PROXY", "*")
            direct = await upstream.get_client()
            assert selected_proxy(direct, "https://outside.test") is None
        finally:
            await upstream.aclose_client()
    asyncio.run(run())


@pytest.mark.parametrize("finish", ["consume", "close", "cancel_headers"])
def test_retired_client_waits_for_headers_and_stream(monkeypatch, windows_proxy, finish):
    async def run():
        entered = asyncio.Event()
        headers = asyncio.Event()
        body = asyncio.Event()

        class Stream(httpx.AsyncByteStream):
            async def __aiter__(self):
                yield b"first"
                await body.wait()
                yield b"last"

        async def handle(request):
            entered.set()
            await headers.wait()
            return httpx.Response(200, stream=Stream())

        monkeypatch.setattr(upstream, "client_args", lambda egress, **kwargs: {
            "transport": httpx.MockTransport(handle), "trust_env": False,
        })
        task = None
        response = None
        try:
            old = await upstream.get_client()
            task = asyncio.create_task(old.send(old.build_request("GET", "https://example.test"), stream=True))
            await asyncio.wait_for(entered.wait(), 3)
            windows_proxy["https"] = "http://127.0.0.1:7890"
            new = await upstream.get_client()
            assert old.retired and not old.is_closed
            assert old in upstream._retired_clients
            if finish == "cancel_headers":
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task
            else:
                headers.set()
                response = await asyncio.wait_for(task, 3)
                chunks = response.aiter_bytes()
                assert await anext(chunks) == b"first"
                assert not old.is_closed, "响应头已返回也不能关闭旧池"
                if finish == "consume":
                    body.set()
                    assert [chunk async for chunk in chunks] == [b"last"]
                else:
                    await response.aclose()
                await response.aclose()  # 重复关闭不重复释放占用。
            assert old.is_closed and old.active == 0
            assert not upstream._retired_clients
            assert not new.is_closed
        finally:
            headers.set()
            body.set()
            if task is not None and not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
            if response is not None:
                await response.aclose()
            await upstream.aclose_client()
    asyncio.run(run())


def test_unsent_old_client_uses_current_settings(monkeypatch, windows_proxy):
    def args(egress, *, system_proxy):
        return {"transport": httpx.MockTransport(lambda request: httpx.Response(200, json=dict(system_proxy))), "trust_env": False}
    monkeypatch.setattr(upstream, "client_args", args)

    async def run():
        try:
            old = await upstream.get_client()
            request = old.build_request("GET", "https://example.test")
            windows_proxy["https"] = "http://127.0.0.1:7890"
            current = await upstream.get_client()
            assert old.is_closed
            response = await old.send(request)
            assert response.json() == windows_proxy
            assert current.active == 0
        finally:
            await upstream.aclose_client()
    asyncio.run(run())


@pytest.mark.network
def test_system_toggle_changes_real_route_without_cutting_stream(monkeypatch, windows_proxy, gateway):
    # 保持正式回环绕过规则；只给测试用的非回环主机名提供本机 DNS。
    real_resolve = socket.getaddrinfo
    def resolve(host, *args, **kwargs):
        if host in ("system-proxy.test", b"system-proxy.test"):
            host = "127.0.0.1"
        return real_resolve(host, *args, **kwargs)
    monkeypatch.setattr(socket, "getaddrinfo", resolve)

    with MockProxy() as proxy, MockUpstream("system-site") as remote:
        remote.base_url = f"http://system-proxy.test:{remote.port}"
        group = add_upstream(gateway, remote, remote.name)
        add_route(gateway, "system-model", group, "remote")
        windows_proxy.update(http=proxy.url, https=proxy.url)
        with gateway.stream("POST", "/v1/responses", json={"model": "system-model", "stream": True, "mode": "stalled"}) as response:
            assert response.status_code == 200
            lines = response.iter_lines()
            assert '"i": 0' in next(lines)
            assert len(proxy.seen) == 1
            windows_proxy.clear()
            assert gateway.post("/v1/responses", json={"model": "system-model"}).json()["upstream"] == remote.name
            assert len(proxy.seen) == 1, "关闭后新请求必须直连，即使代理仍在监听"
            remote.app.state.loop.call_soon_threadsafe(remote.app.state.release.set)
            assert "data: [DONE]" in list(lines), "出口切换不能截断旧流"
        windows_proxy.update(http=proxy.url, https=proxy.url)
        assert gateway.post("/v1/responses", json={"model": "system-model"}).status_code == 200
        assert len(proxy.seen) == 2
        assert gateway.get(f"/admin/api/groups/{group}/remote-models").status_code == 200
        assert len(proxy.seen) == 3
        windows_proxy.clear()
        assert gateway.get(f"/admin/api/groups/{group}/remote-models").status_code == 200
        result = gateway.post(f"/admin/api/upstreams/{provider_id(gateway, remote.name)}/probe").json()
        assert all(row["ok"] for row in result["results"])
        assert len(proxy.seen) == 3, "拉模型与探测也必须遵循系统开关"
