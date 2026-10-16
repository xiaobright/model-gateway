"""进程内回归：分块 JSON 统计和手动中断，不靠真实超时或额外上游进程。"""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from gateway import config, db, failover, inflight, proxy, stats, upstream as upstream_mod
from gateway.app import create_app


@pytest.fixture
def app(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "gateway.db")
    failover.reset()
    inflight.reset()
    stats.reset()
    yield create_app()
    failover.reset()
    inflight.reset()
    stats.reset()


def configure_routes(protocol):
    for name in ("primary", "backup"):
        upstream = db.create_upstream(name, f"https://{name}.example", egress="direct")
        group = db.create_group(upstream.id, "default", protocol)
        db.add_model_route("m", group.id, "m")


class Body(httpx.AsyncByteStream):
    def __init__(self, chunks):
        self.chunks = chunks
        self.closed = False

    def __aiter__(self):
        return self.chunks

    async def aclose(self):
        self.closed = True
        await self.chunks.aclose()


def use_upstream(monkeypatch, client):
    async def get_client(egress=""):
        return client

    monkeypatch.setattr(upstream_mod, "get_client", get_client)


@pytest.mark.parametrize("kind", ["timeout", "task", "manual", "scope"])
def test_upstream_wait_cancels_once_and_joins_cleanup(app, kind):
    import anyio

    async def run():
        call = inflight.begin(client="test", protocol="openai", model="m", stream=True, req_bytes=0)
        entered, cleaned = asyncio.Event(), asyncio.Event()
        operations = []

        async def operation():
            operations.append(asyncio.current_task())
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                assert call.pending is asyncio.current_task(), "收尾前不能遗失 pending"
                await asyncio.sleep(0.01)
                cleaned.set()

        async def wait():
            return await inflight.wait_for_upstream(call, operation(), timeout=0.03 if kind == "timeout" else None)

        try:
            if kind == "scope":
                async with anyio.create_task_group() as group:
                    group.start_soon(wait)
                    await entered.wait()
                    group.cancel_scope.cancel()
            else:
                task = asyncio.create_task(wait())
                await entered.wait()
                expected = inflight.UpstreamStall
                if kind == "manual":
                    inflight.cancel(call.id)
                    inflight.cancel(call.id)
                    expected = inflight.ManualAbort
                elif kind == "task":
                    task.cancel()
                    expected = asyncio.CancelledError
                with pytest.raises(expected):
                    await asyncio.wait_for(task, 2)
            assert cleaned.is_set(), "不能把仍在运行的操作留给事件循环退出时清理"
            assert operations[0].done() and operations[0].cancelling() == 1
            assert call.pending is None
        finally:
            inflight.finish(call, status=499, note="test")

    asyncio.run(run())


def test_completed_response_is_closed_when_waiter_loses_cancellation_race(app):
    async def run():
        call = inflight.begin(client="test", protocol="openai", model="m", stream=True, req_bytes=0)
        response = httpx.Response(200, stream=httpx.ByteStream(b"test"))
        parent = None

        async def operation():
            # 结果已生成，shield 唤醒等待者之前取消它。
            asyncio.get_running_loop().call_soon(parent.cancel)
            return response

        try:
            parent = asyncio.create_task(inflight.wait_for_upstream(call, operation(), on_cancel=proxy._close_response))
            with pytest.raises(asyncio.CancelledError):
                await parent
            assert response.is_closed
            assert call.pending is None
        finally:
            inflight.finish(call, status=499, note="test")

    asyncio.run(run())


@pytest.mark.parametrize("path", ["inflight", "headers", "held_read", "close_response"])
@pytest.mark.parametrize("late_result", [False, True])
def test_repeated_task_cancellation_waits_for_cleanup_and_disposes_late_response(app, path, late_result):
    async def run():
        entered, closing, release, disposed = (asyncio.Event() for _ in range(4))
        tasks = []
        call = inflight.begin(client="test", protocol="openai", model="m", stream=True, req_bytes=0)

        class Stream(httpx.AsyncByteStream):
            async def __aiter__(self):
                yield b"unused"

            async def aclose(self):
                if path == "close_response":
                    entered.set()
                    closing.set()
                    await release.wait()
                disposed.set()

        response = httpx.Response(200, stream=Stream())

        async def operation():
            task = asyncio.current_task()
            tasks.append(task)
            entered.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                closing.set()
                await release.wait()
                assert task.cancelling() == 1, "第二次取消不能进入子任务的收尾"
                if late_result:
                    return response
                raise

        class Request:
            async def is_disconnected(self):
                return False

        class Client:
            async def send(self, *args, **kwargs):
                return await operation()

        class Iterator:
            def __anext__(self):
                return operation()

        if path == "inflight":
            wait = inflight.wait_for_upstream(call, operation(), on_cancel=proxy._close_response)
        elif path == "headers":
            wait = proxy._send_until_headers(Request(), Client(), object())
        elif path == "held_read":
            wait = proxy._next_while_connected(Request(), Iterator())
        else:
            wait = proxy._close_response(response)
        parent = asyncio.create_task(wait)
        try:
            await asyncio.wait_for(entered.wait(), 2)
            parent.cancel()
            await asyncio.wait_for(closing.wait(), 2)
            if path == "inflight":
                assert inflight.cancel(call.id)
                assert inflight.cancel(call.id)
            for _ in range(3):
                parent.cancel()
                await asyncio.sleep(0)
                assert not parent.done(), "资源没释放时不能提前退出"
            if path == "inflight":
                assert call.pending is tasks[0]
            release.set()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(parent, 2)
            assert all(task.done() for task in tasks)
            if path == "close_response" or late_result and path in {"inflight", "headers"}:
                assert response.is_closed and disposed.is_set()
            assert call.pending is None
        finally:
            release.set()
            await asyncio.gather(parent, *tasks, return_exceptions=True)
            await response.aclose()
            inflight.finish(call, status=499, note="test")

    asyncio.run(run())


@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("details_first", [False, True])
def test_large_structured_responses_usage_survives_chunks_and_tail_eviction(app, monkeypatch, stream, details_first):
    configure_routes("openai")
    totals = {"input_tokens": 4388, "output_tokens": 6, "input_tokens_details": {"cached_tokens": 4224}}
    details = {"attribution": {"items": [{"input_tokens": 0, "output_tokens": 0, "cached_tokens": 0,
                                         "echo": 'code { \\" braces }'}] * 1300,
                               "usage": {"input_tokens": 0, "output_tokens": 0, "cached_tokens": 0}}}
    usage = {**details, **totals} if details_first else {**totals, **details}
    raw = json.dumps({"type": "response.completed", "response": {"usage": usage},
                      "tool_usage": {"input_tokens": 0, "output_tokens": 0}}).encode()
    if stream:
        raw = b"event: response.completed\ndata: " + raw + b"\n\ndata: [DONE]\n\n"
    assert len(raw) > proxy.TAIL_KEEP

    async def run():
        async def chunks():
            for start in range(0, len(raw), 4093):
                yield raw[start:start + 4093]

        body = Body(chunks())
        content_type = "text/event-stream" if stream else "application/json"
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(
            200, headers={"content-type": content_type}, stream=body)), trust_env=False) as upstream:
            use_upstream(monkeypatch, upstream)
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://127.0.0.1") as client:
                response = await client.post("/v1/responses", json={"model": "m", "stream": stream})
                assert response.content == raw
        assert body.closed
        row = db.recent_requests(1)[0]
        assert (row["input_tokens"], row["output_tokens"], row["cached_tokens"]) == (4388, 6, 4224)
        recent = inflight.snapshot()["recent"][0]
        assert (recent["tokens_in"], recent["tokens_out"]) == (4388, 6)

    asyncio.run(run())


@pytest.mark.parametrize("spec", ["2.0", "2.4"])
@pytest.mark.parametrize("stage", ["start", "body"])
@pytest.mark.parametrize("failure", ["broken_pipe", "cancel"])
def test_response_send_failure_closes_even_an_unstarted_relay(app, monkeypatch, spec, stage, failure):
    from starlette.requests import Request, ClientDisconnect
    from gateway import protocols

    configure_routes("openai")

    async def run():
        entered = asyncio.Event()

        async def chunks():
            yield b'data: {"type":"response.output_text.delta","delta":"hello"}\n\n'
            await asyncio.Event().wait()

        body = Body(chunks())
        response = httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=body)
        scope = {"type": "http", "app": app, "asgi": {"version": "3.0", "spec_version": spec},
                 "method": "POST", "path": "/v1/responses", "headers": [], "query_string": b""}

        async def receive():
            await asyncio.Event().wait()

        async def send(message):
            if message["type"] == f"http.response.{stage}":
                entered.set()
                if failure == "broken_pipe":
                    raise BrokenPipeError("test downstream gone")
                await asyncio.Event().wait()

        request = Request(scope, receive)
        request._body = b'{"model":"m","stream":true}'
        async with upstream_mod._SystemClient(transport=httpx.MockTransport(lambda r: response), trust_env=False) as upstream:
            use_upstream(monkeypatch, upstream)
            streaming = await proxy.forward(request, protocols.OPENAI, "/responses")
            task = asyncio.create_task(streaming(scope, receive, send))
            await asyncio.wait_for(entered.wait(), 2)
            if failure == "cancel":
                task.cancel()
            with pytest.raises((asyncio.CancelledError, BrokenPipeError, ClientDisconnect)):
                await asyncio.wait_for(task, 2)
            assert response.is_closed and body.closed and upstream.active == 0
            assert inflight.counts()["requests"] == 0
            assert len(db.recent_requests()) == 1
            assert db.recent_requests(1)[0]["note"] == "client_abort"

    asyncio.run(run())


@pytest.mark.parametrize("old_mode", ["", "direct", "http://127.0.0.1:7890"])
def test_supplier_egress_update_retires_pool_without_cutting_active_stream(app, monkeypatch, old_mode):
    provider = db.create_upstream("primary", "https://primary.example", egress=old_mode)

    async def run():
        async def chunks():
            yield b"first"
            yield b"last"

        bodies = []

        def handle(request):
            body = Body(chunks())
            bodies.append(body)
            return httpx.Response(200, stream=body)

        monkeypatch.setattr(upstream_mod, "_system_proxy_signature", lambda: ())
        monkeypatch.setattr(upstream_mod, "client_args", lambda *args, **kwargs: {
            "transport": httpx.MockTransport(handle), "trust_env": False,
        })
        try:
            old = await upstream_mod.get_client(old_mode)
            response = await old.send(old.build_request("GET", "https://primary.example"), stream=True)
            chunks_iter = response.aiter_bytes()
            assert await anext(chunks_iter) == b"first"
            other_mode = "direct" if old_mode != "direct" else ""
            other = await upstream_mod.get_client(other_mode)
            assert not old.retired, "同时使用另一个出口不能淘汰此池"
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://127.0.0.1") as admin:
                for mode in (old_mode, other_mode):
                    result = await admin.put(f"/admin/api/upstreams/{provider.id}", json={
                        "name": provider.name, "base_url": provider.base_url, "egress": mode,
                    })
                    assert result.status_code == 200
                    assert old.retired is (mode != old_mode), "未改出口的保存不能退役池"
            assert old.active == 1 and not old.is_closed
            replacement = await upstream_mod.get_client(old_mode)
            assert replacement is not old
            assert await upstream_mod.get_client(other_mode) is other
            # get_client 先取到旧对象、配置随后变化的竞态，也必须发到该出口的新池。
            again = await old.send(old.build_request("GET", "https://primary.example"), stream=True)
            await again.aclose()
            assert replacement.active == 0 and old.active == 1
            assert [chunk async for chunk in chunks_iter] == [b"last"]
            await response.aclose()
            assert bodies[0].closed and old.active == 0 and old.is_closed
            assert old not in upstream_mod._retired_clients and not other.is_closed
        finally:
            await upstream_mod.aclose_client()

    asyncio.run(run())


def test_local_pool_timeout_stops_without_retry_or_breaker_failure(app, monkeypatch):
    configure_routes("anthropic")
    primary = db.list_upstreams()[0]
    db.update_upstream(primary.id, primary.name, primary.base_url, True, egress="direct",
                       retry_rules='[{"status":502,"times":3,"delay_ms":0}]')

    async def run():
        seen = []

        async def handle(request):
            seen.append(request.url.host)
            raise httpx.PoolTimeout("test saturated pool", request=request)

        async with httpx.AsyncClient(transport=httpx.MockTransport(handle), trust_env=False) as upstream:
            use_upstream(monkeypatch, upstream)
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://127.0.0.1") as client:
                response = await client.post("/v1/messages", json={"model": "m"})
                assert response.status_code == 502 and "连接池" in response.text
                assert seen == ["primary.example"]
        assert failover.snapshot() == []
        assert inflight.counts()["requests"] == 0
        assert [(r["status"], r["note"]) for r in db.recent_requests()] == [(502, "pool_timeout")]

    asyncio.run(run())


@pytest.mark.parametrize("retry", [False, True])
def test_cancel_during_retry_response_close_finishes_releasing_connection(app, monkeypatch, retry):
    from starlette.requests import Request
    from gateway import protocols

    configure_routes("anthropic")
    if retry:
        primary = db.list_upstreams()[0]
        db.update_upstream(primary.id, primary.name, primary.base_url, True, egress="direct",
                           retry_rules='[{"status":400,"times":1,"delay_ms":0}]')

    async def run():
        entered, release, closed = asyncio.Event(), asyncio.Event(), asyncio.Event()

        class ClosingBody(httpx.AsyncByteStream):
            async def __aiter__(self):
                yield b"error"

            async def aclose(self):
                entered.set()
                await release.wait()
                closed.set()

        response = httpx.Response(400, stream=ClosingBody())
        scope = {"type": "http", "app": app, "method": "POST", "path": "/v1/messages",
                 "headers": [], "query_string": b""}
        request = Request(scope)
        request._body = b'{"model":"m"}'
        async with upstream_mod._SystemClient(transport=httpx.MockTransport(lambda r: response), trust_env=False) as upstream:
            use_upstream(monkeypatch, upstream)
            task = asyncio.create_task(proxy.forward(request, protocols.ANTHROPIC, "/messages"))
            await asyncio.wait_for(entered.wait(), 2)
            task.cancel()
            await asyncio.sleep(0)
            assert not task.done(), "关流尚未完成，取消不能直接离开"
            release.set()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(task, 2)
            assert closed.is_set() and upstream.active == 0
            assert inflight.counts()["requests"] == 0
            assert db.recent_requests(1)[0]["note"] == "client_abort"

    asyncio.run(run())


@pytest.mark.network
def test_cancel_before_real_headers_clears_operation_and_pool(app, monkeypatch):
    import contextlib
    from starlette.requests import Request
    from gateway import protocols

    async def run():
        arrived = asyncio.Event()
        handlers = set()

        async def handle(reader, writer):
            handlers.add(asyncio.current_task())
            try:
                head = await reader.readuntil(b"\r\n\r\n")
                length = next(int(line.split(b":", 1)[1]) for line in head.split(b"\r\n")
                              if line.lower().startswith(b"content-length:"))
                await reader.readexactly(length)
                arrived.set()
                await reader.read()  # 不发响应头；等网关取消并真正断开 socket。
            finally:
                writer.close()
                with contextlib.suppress(ConnectionError):
                    await writer.wait_closed()
                handlers.discard(asyncio.current_task())

        server = await asyncio.start_server(handle, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        provider = db.create_upstream("headers", f"http://127.0.0.1:{port}", egress="direct")
        group = db.create_group(provider.id, "g", "openai")
        db.add_model_route("m", group.id, "m")
        scope = {"type": "http", "app": app, "method": "POST", "path": "/v1/responses",
                 "headers": [], "query_string": b""}
        request = Request(scope)
        request._body = b'{"model":"m","stream":true}'

        async def connected():
            return False

        monkeypatch.setattr(request, "is_disconnected", connected)
        try:
            async with upstream_mod._SystemClient(trust_env=False) as upstream:
                use_upstream(monkeypatch, upstream)
                task = asyncio.create_task(proxy.forward(request, protocols.OPENAI, "/responses"))
                await asyncio.wait_for(arrived.wait(), 2)
                pending = inflight._calls[inflight.snapshot()["calls"][0]["id"]].pending
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await asyncio.wait_for(task, 2)
                assert pending.done() and pending.cancelled()
                assert not upstream._transport._pool._requests
                assert not upstream._transport._pool.connections
                assert upstream.active == 0 and inflight.counts()["requests"] == 0
                assert db.recent_requests(1)[0]["note"] == "client_abort"
        finally:
            server.close()
            await server.wait_closed()
            for handler in list(handlers):
                handler.cancel()
            await asyncio.gather(*handlers, return_exceptions=True)

    asyncio.run(run())


@pytest.mark.parametrize("cancel_kind", ["disconnect", "task", "manual"])
def test_hold_wait_before_first_chunk_releases_request_and_system_pool(app, monkeypatch, cancel_kind):
    from starlette.requests import Request
    from gateway import protocols, truncation

    provider = db.create_upstream("hold", "https://hold.example", hold_retry='{"after":1,"times":1,"delay_ms":0}')
    group = db.create_group(provider.id, "g", "openai")
    db.add_model_route("m", group.id, "m")
    truncation.reset()
    truncation.note_bad(group.id, "m", "/responses", "test")

    async def run():
        entered = asyncio.Event()
        disconnected = False

        async def chunks():
            entered.set()
            await asyncio.Event().wait()
            yield b"unreachable"

        body = Body(chunks())
        response = httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=body)
        request = Request({"type": "http", "app": app, "method": "POST", "path": "/v1/responses", "headers": [], "query_string": b""})
        request._body = b'{"model":"m","stream":true}'

        async def is_disconnected():
            return disconnected

        monkeypatch.setattr(request, "is_disconnected", is_disconnected)
        async with upstream_mod._SystemClient(transport=httpx.MockTransport(lambda r: response), trust_env=False) as upstream:
            use_upstream(monkeypatch, upstream)
            task = asyncio.create_task(proxy.forward(request, protocols.OPENAI, "/responses"))
            try:
                await asyncio.wait_for(entered.wait(), 2)
                assert upstream.active == 1
                if cancel_kind == "task":
                    task.cancel()
                    with pytest.raises(asyncio.CancelledError):
                        await task
                else:
                    if cancel_kind == "disconnect":
                        disconnected = True
                    else:
                        inflight.cancel(inflight.snapshot()["calls"][0]["id"])
                    result = await asyncio.wait_for(task, 2)
                    assert result.status_code == 499
                assert response.is_closed and body.closed and upstream.active == 0
                assert inflight.counts()["requests"] == 0
                note = "manual_abort" if cancel_kind == "manual" else "client_abort"
                assert db.recent_requests(1)[0]["note"] == note
            finally:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
                await response.aclose()

    try:
        asyncio.run(run())
    finally:
        truncation.reset()


@pytest.mark.network
def test_real_disconnect_before_held_first_chunk_is_cleaned_up(gateway, monkeypatch):
    import socket
    import threading
    from gateway import truncation
    from helpers import wait_for_row

    entered, closed = threading.Event(), threading.Event()

    class HangingBody(httpx.AsyncByteStream):
        async def __aiter__(self):
            entered.set()
            await asyncio.Event().wait()
            yield b"unreachable"

        async def aclose(self):
            closed.set()

    monkeypatch.setattr(upstream_mod, "client_args", lambda *args, **kwargs: {
        "transport": httpx.MockTransport(lambda r: httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=HangingBody())),
        "trust_env": False,
    })
    provider = db.create_upstream("hold", "https://hold.example", hold_retry='{"after":1,"times":1,"delay_ms":0}')
    group = db.create_group(provider.id, "g", "openai")
    db.add_model_route("m", group.id, "m")
    truncation.note_bad(group.id, "m", "/responses", "test")
    body = b'{"model":"m","stream":true}'
    with socket.create_connection(("127.0.0.1", gateway.base_url.port), timeout=3) as client:
        client.sendall(
            f"POST /v1/responses HTTP/1.1\r\nHost: 127.0.0.1:{gateway.base_url.port}\r\nContent-Type: application/json\r\nContent-Length: {len(body)}\r\n\r\n".encode() + body
        )
        assert entered.wait(3)
    assert closed.wait(3), "真实客户端已断开，守卫不能继续占用上游"
    assert wait_for_row(gateway)["note"] == "client_abort"
    assert inflight.counts()["requests"] == 0


@pytest.mark.parametrize("raw,content_type", [
    (b'{"error":{"message":"upstream failure"}}', "application/json"),
    (b'{"status":"failed","output":[]}', "application/json"),
    (b'event: error\ndata: {"error":"failed"}\n\ndata: [DONE]\n\n', "text/event-stream"),
    (b'event: response.failed\ndata: {}\n\n', "text/event-stream"),
])
def test_protocol_failure_preserves_bytes_but_changes_all_result_views(app, monkeypatch, raw, content_type):
    from collections import OrderedDict
    from gateway import truncation

    configure_routes("openai")
    monkeypatch.setattr(truncation, "_entries", OrderedDict())
    primary = next(u for u in db.list_upstreams() if u.name == "primary")
    db.update_upstream(primary.id, primary.name, primary.base_url, True, egress="direct", hold_retry='{"after":1,"times":1,"delay_ms":0}')

    async def run():
        async def chunks():
            yield raw
        body = Body(chunks())
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200, headers={"content-type": content_type}, stream=body)), trust_env=False) as upstream:
            use_upstream(monkeypatch, upstream)
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://127.0.0.1") as client:
                response = await client.post("/v1/responses", json={"model": "m", "stream": True})
                assert response.status_code == 200 and response.content == raw
                health = (await client.get("/admin/api/overview")).json()["upstreams"]
                assert next(h for h in health if h["name"] == "primary")["bad"] == 1
        assert body.closed

    asyncio.run(run())
    assert db.recent_requests(1)[0]["note"] == "protocol_error"
    assert inflight.snapshot()["recent"][0]["note"] == "protocol_error"
    assert inflight.counts()["requests"] == 0
    group = db.list_groups(primary.id)[0]
    assert truncation.fails(group.id, "m", "/responses") == int(content_type == "text/event-stream")


@pytest.mark.parametrize("failure", ["flag_permission", "capture_finish", "usage_parser", "huge_usage", "blob_scan"])
def test_observation_failures_do_not_break_response_or_cleanup(app, monkeypatch, failure):
    from pathlib import Path
    from gateway import capture, protocols

    configure_routes("openai")
    # 即使 usage 的 JSON 数字异常，明确的 event 头仍能确认流正常结束。
    raw = b'event: response.completed\ndata: {"type":"response.completed","usage":{"input_tokens":5,"output_tokens":7}}\n\n'
    if failure == "huge_usage":
        raw = raw.replace(b'"input_tokens":5', b'"input_tokens":' + b"9" * 4400)
    elif failure == "usage_parser":
        def broken_usage(*args):
            raise ValueError("injected usage failure")
        monkeypatch.setattr(protocols, "openai_payload_usage", lambda *args, **kwargs: broken_usage())
    elif failure == "blob_scan":
        def broken_blob(*args):
            raise RuntimeError("injected request observation failure")
        monkeypatch.setattr(protocols, "blob_bytes", broken_blob)
    else:
        monkeypatch.setattr(capture, "_state", {"remaining": 0, "active": 0})
        capture.enable(1)
        if failure == "flag_permission":
            original = Path.unlink
            def unlink(path, *args, **kwargs):
                if path == capture.flag_path():
                    raise PermissionError("read-only flag")
                return original(path, *args, **kwargs)
            monkeypatch.setattr(Path, "unlink", unlink)
        else:
            original = capture.finish
            def finish(*args, **kwargs):
                original(*args, **kwargs)
                raise RuntimeError("injected capture failure")
            monkeypatch.setattr(capture, "finish", finish)

    async def run():
        async def chunks():
            yield raw
        body = Body(chunks())
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=body)), trust_env=False) as upstream:
            use_upstream(monkeypatch, upstream)
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://127.0.0.1") as client:
                response = await client.post("/v1/responses", json={"model": "m", "stream": True})
                assert response.status_code == 200 and response.content == raw
        assert body.closed

    asyncio.run(run())
    row = db.recent_requests(1)[0]
    assert row["note"] == "ok"
    assert row["input_tokens"] == (None if failure in ("huge_usage", "usage_parser") else 5)
    assert inflight.counts()["requests"] == 0


@pytest.mark.parametrize("protocol", ["anthropic", "openai"])
@pytest.mark.parametrize("thinking", [False, True])
def test_json_content_survives_chunk_boundaries(app, monkeypatch, protocol, thinking):
    configure_routes(protocol)
    # 一组逐字节切（含 UTF-8 / JSON 转义），另一组超过头尾窗口，不能只数保留的片段。
    text = 'hello "世界"\n' + ("x" * (proxy.HEAD_KEEP + proxy.TAIL_KEEP) if not thinking else "")
    thought = "推理摘要" if thinking else ""
    arguments = '{"city":"香港"}'
    if protocol == "anthropic":
        output = {"content": [{"type": "text", "text": text}]}
        if thinking:
            output["content"].append({"type": "thinking", "thinking": thought})
        expected = len((text + thought).encode())
        path = "/v1/messages"
    else:
        output = {"output": [
            {"type": "message", "content": [{"type": "output_text", "text": text}]},
            {"type": "function_call", "arguments": arguments},
        ]}
        if thinking:
            output["output"].append({"type": "reasoning", "summary": [{"type": "summary_text", "text": thought}]})
        expected = len((text + thought + arguments).encode())
        path = "/v1/responses"
    raw = json.dumps({
        "metadata": {"text": "not output"}, **output,
        "usage": {"input_tokens": 100, "output_tokens": 10},
    }, ensure_ascii=False).encode()

    async def chunks():
        size = 1 if thinking else 4093
        for i in range(0, len(raw), size):
            yield raw[i:i + size]

    body = Body(chunks())

    async def run():
        transport = httpx.MockTransport(lambda request: httpx.Response(
            200, headers={"content-type": "application/json; charset=utf-8"}, stream=body,
        ))
        async with httpx.AsyncClient(transport=transport, trust_env=False) as upstream:
            use_upstream(monkeypatch, upstream)
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://127.0.0.1") as client:
                response = await client.post(path, json={"model": "m", "stream": False})
                assert response.status_code == 200 and response.content == raw

    asyncio.run(run())
    assert body.closed
    row = db.recent_requests(1)[0]
    assert (row["resp_text_bytes"], bool(row["thinking"]), row["note"]) == (expected, thinking, "ok")
    assert (row["input_tokens"], row["output_tokens"]) == (100, 10)
    recent = inflight.snapshot()["recent"][0]
    assert (recent["text_bytes"], recent["thinking"]) == (expected, thinking)


@pytest.mark.parametrize("phase", ["connect", "wait", "stream"])
def test_manual_cancel_stops_only_the_selected_request(app, monkeypatch, phase):
    configure_routes("anthropic")

    async def run():
        blocked, stopped, other_blocked, release_other = (asyncio.Event() for _ in range(4))
        seen, bodies = [], {}
        chunk = b'event: content_block_delta\ndata: {"delta":{"text":"hello"}}\n\n'
        end = b'event: message_stop\ndata: {"type":"message_stop"}\n\n'

        async def chunks(other):
            try:
                if other or phase == "stream":
                    yield chunk
                if other:
                    other_blocked.set()
                    await release_other.wait()
                    yield end
                else:
                    blocked.set()
                    await asyncio.Event().wait()
            finally:
                if not other:
                    stopped.set()

        async def handle(request):
            seen.append(request.url.host)
            other = json.loads(request.content).get("other", False)
            if not other and phase == "connect":
                blocked.set()
                try:
                    await asyncio.Event().wait()
                finally:
                    stopped.set()
            body = bodies[other] = Body(chunks(other))
            return httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=body)

        async with httpx.AsyncClient(transport=httpx.MockTransport(handle), trust_env=False) as upstream:
            use_upstream(monkeypatch, upstream)
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://127.0.0.1") as client:
                victim = asyncio.create_task(client.post(
                    "/v1/messages", json={"model": "m", "stream": True}, headers={"user-agent": "victim/1"},
                ))
                other = asyncio.create_task(client.post(
                    "/v1/messages", json={"model": "m", "stream": True, "other": True}, headers={"user-agent": "other/1"},
                ))
                try:
                    await asyncio.wait_for(asyncio.gather(blocked.wait(), other_blocked.wait()), 2)
                    live = (await client.get("/admin/api/inflight")).json()
                    target = next(c for c in live["calls"] if c["client"] == "victim")
                    assert target["phase"] == phase
                    url = f'/admin/api/inflight/{target["id"]}/cancel'
                    assert (await client.post(url)).json() == {"ok": True, "cancelled": True}
                    response = await asyncio.wait_for(victim, 2)
                    assert response.status_code == (499 if phase == "connect" else 200)
                    if phase != "connect":
                        assert response.content == (chunk if phase == "stream" else b"")
                        assert bodies[False].closed
                    assert stopped.is_set(), "取消必须传到正在等待的上游操作"
                    assert not other.done(), "中断一条不能影响其它请求"
                    assert (await client.post(url)).json()["cancelled"] is False
                    assert (await client.post("/admin/api/inflight/999999/cancel")).json()["cancelled"] is False
                    live = (await client.get("/admin/api/inflight")).json()
                    assert [c["client"] for c in live["calls"]] == ["other"]
                    assert live["recent"][0]["note"] == "manual_abort"

                    release_other.set()
                    assert (await asyncio.wait_for(other, 2)).content == chunk + end
                    assert seen == ["primary.example", "primary.example"], "手动中断不能触发降级"
                    assert failover.snapshot() == []
                    rows = {r["client"]: r for r in db.recent_requests()}
                    assert rows["victim"]["note"] == "manual_abort" and rows["other"]["note"] == "ok"
                    assert stats.upstream_health()[0]["bad"] == 1, "手动中断不能计作成功"
                    assert inflight.counts() == {"requests": 0, "streams": 0}
                finally:
                    for task in (victim, other):
                        if not task.done():
                            task.cancel()
                    await asyncio.gather(victim, other, return_exceptions=True)

    asyncio.run(run())


@pytest.mark.parametrize("failure", ["connect", "status"])
@pytest.mark.parametrize("cancel_kind", ["manual", "task"])
def test_retry_delay_is_cancellable_without_another_attempt(app, monkeypatch, failure, cancel_kind):
    """30 秒重试间隔不实际等待：确认到达间隔后取消，两条错误分支都必须立即收尾。"""
    for name in ("primary", "backup"):
        provider = db.create_upstream(
            name, f"https://{name}.example", egress="direct",
            retry_rules='[{"status":400,"times":1,"delay_ms":30000},'
                        '{"status":502,"times":1,"delay_ms":30000}]',
        )
        group = db.create_group(provider.id, "default", "anthropic")
        db.add_model_route("m", group.id, "m")

    async def run():
        paused = asyncio.Event()
        calls = []
        original_wait = inflight.wait_for_upstream

        async def wait(call, operation, timeout=None, **kwargs):
            if call.trail and call.trail[-1]["note"] == "same_retry":
                paused.set()
            return await original_wait(call, operation, timeout, **kwargs)

        monkeypatch.setattr(inflight, "wait_for_upstream", wait)

        async def handle(request):
            calls.append(request.url.host)
            if failure == "connect":
                raise httpx.ConnectError("test connection failure", request=request)
            return httpx.Response(400, json={"error": "retry me"})

        async with httpx.AsyncClient(transport=httpx.MockTransport(handle), trust_env=False) as upstream:
            use_upstream(monkeypatch, upstream)
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://127.0.0.1") as client:
                task = asyncio.create_task(client.post("/v1/messages", json={"model": "m"}))
                try:
                    await asyncio.wait_for(paused.wait(), 2)
                    target = inflight.snapshot()["calls"][0]
                    if cancel_kind == "manual":
                        result = await client.post(f'/admin/api/inflight/{target["id"]}/cancel')
                        assert result.json()["cancelled"] is True
                        response = await asyncio.wait_for(task, 2)
                        assert response.status_code == 499
                    else:
                        task.cancel()
                        with pytest.raises(asyncio.CancelledError):
                            await asyncio.wait_for(task, 2)
                    note = "manual_abort" if cancel_kind == "manual" else "client_abort"
                    assert calls == ["primary.example"], "取消间隔不能再同站重试或换站"
                    assert failover.snapshot() == [], "取消不是站级故障"
                    assert inflight.counts()["requests"] == 0
                    assert inflight.snapshot()["recent"][0]["note"] == note
                    assert [(r["status"], r["note"]) for r in reversed(db.recent_requests())] == [
                        (502 if failure == "connect" else 400, "same_retry"),
                    ], "间隔中取消没有新发送，不能虚增一条转发记录"
                finally:
                    if not task.done():
                        task.cancel()
                    await asyncio.gather(task, return_exceptions=True)

    asyncio.run(run())


@pytest.mark.parametrize("mode", ["off", "stream", "compaction"])
def test_forward_diagnostics_are_opt_in_and_include_the_final_frame(app, monkeypatch, mode):
    from gateway import capture, protocols

    configure_routes("openai")
    if mode == "stream":
        capture.enable(1)
    elif mode == "compaction":
        (config.DATA_DIR / "compaction_capture.flag").touch()
    scans = []
    observe = protocols.compaction_observations

    def tracked(payload, **kwargs):
        scans.append(payload)
        return observe(payload, **kwargs)

    monkeypatch.setattr(protocols, "compaction_observations", tracked)
    raw = (
        b'event: response.output_item.done\ndata: {"type":"response.output_item.done",'
        b'"item":{"type":"compaction","id":"cmp_1","encrypted_content":"opaque-test"}}\n\n'
        b'data: {"type":"response.completed"}'  # EOF 没有空行，必须 flush 后再写诊断
    )

    async def chunks():
        yield raw

    async def run():
        transport = httpx.MockTransport(lambda request: httpx.Response(
            200, headers={"content-type": "text/event-stream"}, stream=Body(chunks()),
        ))
        async with httpx.AsyncClient(transport=transport, trust_env=False) as upstream:
            use_upstream(monkeypatch, upstream)
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://127.0.0.1") as client:
                response = await client.post("/v1/responses", json={"model": "m", "stream": True})
                assert response.content == raw

    asyncio.run(run())
    assert db.recent_requests(1)[0]["note"] == "ok"
    assert len(scans) == (2 if mode == "compaction" else 0)
    if mode == "compaction":
        result = json.loads((config.DATA_DIR / "compaction_capture.json").read_text("utf-8"))
        assert result["found"]
        assert result["response_payload_types"]["response.completed"] == 1
        assert result["observations"][0]["encrypted_content_bytes"] == len("opaque-test")
        assert "opaque-test" not in json.dumps(result)
        assert not (config.DATA_DIR / "compaction_capture.flag").exists()
    elif mode == "stream":
        result = json.loads(next(capture.out_root().glob("*/meta.json")).read_text("utf-8"))
        assert result["event_types"] == {"response.output_item.done": 1}
        assert result["observer_ended"] is True
