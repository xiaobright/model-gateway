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

        async def wait(call, operation, timeout=None):
            if call.trail and call.trail[-1]["note"] == "same_retry":
                paused.set()
            return await original_wait(call, operation, timeout)

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
                            await task
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
