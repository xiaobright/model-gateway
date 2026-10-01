"""Learning observations are bounded and cannot influence forwarding decisions."""

from __future__ import annotations

import asyncio
import importlib.util
import json
import os
from pathlib import Path
import queue

import httpx
import pytest

from gateway import config, db, failover, inflight, learning, protocols, upstream
from gateway.app import create_app


def rows(collector):
    collector.stop()
    assert not collector.thread or not collector.thread.is_alive()
    return [
        json.loads(line)
        for path in sorted(collector.directory.glob("*.jsonl"))
        for line in path.read_text("utf-8").splitlines()
    ]


def queued(collector):
    result = []
    while True:
        try:
            result.append(collector.queue.get_nowait())
            collector.queue.task_done()
        except queue.Empty:
            return result


def start_attempt(trace):
    trace.start_attempt(
        route_id=1, upstream_id=2, group_id=3, remote_model="remote",
        candidate_attempt=1, same_retries=0, req_bytes=100,
    )


@pytest.fixture
def app(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "gateway.db")
    monkeypatch.delenv("MODEL_GATEWAY_LEARNING", raising=False)
    failover.reset()
    inflight.reset()
    return create_app()


def routes(protocol="openai", retry_rules=""):
    for name in ("primary", "backup"):
        provider = db.create_upstream(
            name, f"https://{name}.example", egress="direct",
            retry_rules=retry_rules if name == "primary" else "",
        )
        group = db.create_group(provider.id, "default", protocol)
        db.add_model_route("m", group.id, "m")


class Body(httpx.AsyncByteStream):
    def __init__(self, chunks):
        self.chunks = chunks

    def __aiter__(self):
        return self.chunks

    async def aclose(self):
        await self.chunks.aclose()


async def clients(app, monkeypatch, handler, operation):
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler), trust_env=False) as remote:
            async def get_client(egress=""):
                return remote
            monkeypatch.setattr(upstream, "get_client", get_client)
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app), base_url="http://127.0.0.1",
            ) as client:
                await operation(client)
    return rows(app.state.learning)


def test_timeline_is_bounded_and_heartbeats_do_not_reset_content_gap(tmp_path, monkeypatch):
    collector = learning.Collector(tmp_path)
    trace = learning.Trace(collector)
    clock = [0]
    monkeypatch.setattr(trace, "ms", lambda: clock[0])
    start_attempt(trace)
    trace.headers(200, True)
    trace.chunk(5)
    trace.event("content", 1)
    clock[0] = 5000
    trace.event("heartbeat")
    clock[0] = 10000
    trace.event("content", 1)
    for i in range(learning.MAX_WINDOWS + 20):
        clock[0] += 300
        trace.event("content", 1)
    clock[0] += 12000
    trace.end_attempt(status=200, note="manual_abort", action="stop")
    start, end = queued(collector)
    assert start["event_counts"] == {}, "queued start must not share mutable aggregate data"
    assert end["first_content_ms"] == 0
    assert end["max_content_gap_ms"] == 10000
    assert end["tail_silence_ms"] == 12000
    assert len(end["windows"]) == learning.MAX_WINDOWS
    assert end["windows_dropped"] == 23
    assert end["interrupted"] is True and end["manual_reason"] == "unknown"
    assert collector.active_groups == {}
    assert collector.errors == 0


@pytest.mark.parametrize("protocol,raw,expected", [
    ("openai", b'data: {"type":"response.output_text.delta","delta":"SECRET"}\n\n'
     b'data: {"type":"response.completed"}', ["content", "completion"]),
    ("anthropic", b': heartbeat\n\n'
     b'event: content_block_delta\ndata: {"delta":{"type":"input_json_delta","partial_json":"SECRET"}}\n\n'
     b'event: message_stop\ndata: {}', ["heartbeat", "tool", "completion"]),
    ("openai-chat", b'data: {"choices":[{"delta":{"reasoning_content":"SECRET"}}]}\n\n'
     b'data: [DONE]', ["reasoning", "completion"]),
    ("openai", b'event: response.failed\ndata: {"error":{"message":"SECRET"}}\n\n', ["protocol_error"]),
    ("openai", b'event: odd\ndata: not-json\n\n', ["malformed"]),
    ("openai", b'event: odd\ndata: {"delta":"SECRET",broken\n\n', ["malformed", "content"]),
    ("openai", b'event: ping\ndata: {}\n\n', ["heartbeat"]),
])
def test_event_hook_is_metadata_only_and_handles_split_frames(protocol, raw, expected):
    events = []
    observer = protocols.SSEObserver(
        protocols.by_name(protocol), on_event=lambda kind, size: events.append((kind, size)),
    )
    for value in raw:
        observer.feed(bytes([value]))
    observer.flush()
    assert [kind for kind, size in events] == expected
    assert "SECRET" not in json.dumps(events)


def test_broken_event_hook_cannot_break_completion_detection():
    def broken(*args):
        raise RuntimeError("observer failed")
    observer = protocols.SSEObserver(protocols.OPENAI, on_event=broken)
    observer.feed(b'data: {"type":"response.completed"}\n\n')
    assert observer.ended


def test_request_ids_are_unique_across_restarts(tmp_path):
    first, second = learning.Collector(tmp_path), learning.Collector(tmp_path)
    assert first.run_id != second.run_id
    assert learning.Trace(first).id != learning.Trace(second).id


@pytest.mark.parametrize("status,raw,content_type,error,completion", [
    (200, b'data: {"type":"response.output_text.delta","delta":"PRIVATE-OUTPUT"}\n\n'
     b'data: {"type":"response.completed"}', "text/event-stream", False, True),
    (500, b'{"error":{"message":"PRIVATE-ERROR"}}', "application/json", True, False),
    (200, b'data: {"type":"response.failed","error":{"message":"PRIVATE-ERROR"}}\n\n',
     "text/event-stream", True, False),
    (200, b'{"error":null,"status":"completed","output":[]}', "application/json", False, False),
])
def test_real_forward_metadata_labels_and_privacy(app, monkeypatch, status, raw, content_type, error, completion):
    routes()
    failover.set_enabled("openai", False)

    async def chunks():
        for i in range(0, len(raw), 7):
            yield raw[i:i + 7]

    def handler(request):
        assert "PRIVATE-INPUT" in request.content.decode()
        return httpx.Response(status, headers={"content-type": content_type}, stream=Body(chunks()))

    async def operation(client):
        response = await client.post(
            "/v1/responses",
            json={"model": "m", "stream": True, "input": "PRIVATE-INPUT"},
            headers={"authorization": "Bearer PRIVATE-KEY", "user-agent": "PRIVATE-UA"},
        )
        assert response.status_code == status and response.content == raw
        assert (await client.get("/admin/api/learning-status")).json()["enabled"]

    data = asyncio.run(clients(app, monkeypatch, handler, operation))
    attempts = [r for r in data if r["kind"] == "attempt_end"]
    assert len(attempts) == 1
    attempt = attempts[0]
    assert attempt["http_status"] == status
    assert attempt["protocol_error_seen"] == error
    assert attempt["completion_seen"] == completion
    assert attempt["resp_bytes"] == len(raw)
    assert attempt["start_ms"] <= attempt["headers_ms"] <= attempt["first_byte_ms"] <= attempt["end_ms"]
    request_end = next(r for r in data if r["kind"] == "request_end")
    assert request_end["downstream_bytes"] == len(raw)
    assert request_end["response_body_finished"]
    assert app.state.learning.errors == 0
    assert "PRIVATE-" not in json.dumps(data)
    assert "primary.example" not in json.dumps(data)


def test_same_retry_and_failover_have_unique_attempts_under_one_request(app, monkeypatch):
    routes("anthropic", '[{"status":503,"times":1,"delay_ms":0}]')
    seen = []
    def handler(request):
        seen.append(request.url.host)
        if request.url.host == "primary.example":
            return httpx.Response(503, json={"error": "retry"})
        return httpx.Response(200, json={"content": [{"type": "text", "text": "ok"}]})

    async def operation(client):
        assert (await client.post("/v1/messages", json={"model": "m"})).status_code == 200

    data = asyncio.run(clients(app, monkeypatch, handler, operation))
    attempts = [r for r in data if r["kind"] == "attempt_end"]
    assert seen == ["primary.example", "primary.example", "backup.example"]
    assert [r["attempt_seq"] for r in attempts] == [1, 2, 3]
    assert [r["candidate_attempt"] for r in attempts] == [1, 1, 2]
    assert [r["action"] for r in attempts] == ["same_retry", "failover", "return"]
    assert len({r["attempt_id"] for r in attempts}) == 3
    assert len({r["request_id"] for r in data if "request_id" in r}) == 1
    assert next(r for r in data if r["kind"] == "request_end")["attempt_count"] == 3
    assert app.state.learning.errors == 0


def test_connect_failure_is_an_attempt_not_a_success(app, monkeypatch):
    routes()
    failover.set_enabled("openai", False)
    def handler(request):
        raise httpx.ConnectError("PRIVATE-ERROR", request=request)
    async def operation(client):
        assert (await client.post("/v1/responses", json={"model": "m"})).status_code == 502
    data = asyncio.run(clients(app, monkeypatch, handler, operation))
    attempt = next(r for r in data if r["kind"] == "attempt_end")
    assert attempt["note"] == "connect_failed" and attempt["http_status"] == 502
    assert attempt["headers_ms"] is None and attempt["first_byte_ms"] is None
    assert "PRIVATE-" not in json.dumps(data)


def test_stall_timeout_is_intervention_not_a_ground_truth_failure(app, monkeypatch):
    routes("anthropic")
    db.set_setting("stall_timeout_s", "0.02")
    async def chunks():
        yield b'event: content_block_delta\ndata: {"delta":{"text":"hello"}}\n\n'
        await asyncio.Event().wait()
    async def operation(client):
        assert (await client.post("/v1/messages", json={"model": "m", "stream": True})).status_code == 200
    data = asyncio.run(clients(
        app, monkeypatch,
        lambda request: httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=Body(chunks())),
        operation,
    ))
    attempt = next(r for r in data if r["kind"] == "attempt_end")
    assert attempt["note"] == "stall_timeout" and attempt["interrupted"]
    assert attempt["content_events"] == 1 and not attempt["completion_seen"]
    assert next(r for r in data if r["kind"] == "request_start")["stall_timeout_s"] == 0.02


def test_lifecycle_records_an_unstarted_relay_and_does_not_invent_success(tmp_path):
    collector = learning.Collector(tmp_path)
    async def inner(scope, receive, send):
        trace = learning.Trace(collector)
        scope["learning_trace"] = trace
        start_attempt(trace)
        await send({"type": "http.response.start", "status": 200, "headers": []})
        raise asyncio.CancelledError
    async def run():
        async def no_op(*args):
            pass
        with pytest.raises(asyncio.CancelledError):
            await learning.Lifecycle(inner)({"type": "http"}, no_op, no_op)
    asyncio.run(run())
    data = queued(collector)
    assert data[-1]["kind"] == "request_end" and data[-1]["note"] == "client_abort"
    assert data[-1]["response_body_finished"] is False
    assert data[-1]["response_status"] == 200
    assert collector.active_groups == {}


@pytest.mark.parametrize("phase", ["headers", "stream", "retry"])
@pytest.mark.parametrize("cancel", ["manual", "task"])
def test_cancellation_records_partial_observation_without_phantom_attempt(app, monkeypatch, phase, cancel):
    routes("anthropic", '[{"status":400,"times":1,"delay_ms":30000}]' if phase == "retry" else "")

    async def run():
        ready = asyncio.Event()
        seen = []
        original = inflight.wait_for_upstream
        async def waiting(call, operation, timeout=None):
            if phase == "retry" and call.trail:
                ready.set()
            return await original(call, operation, timeout)
        monkeypatch.setattr(inflight, "wait_for_upstream", waiting)

        async def chunks():
            yield b'event: content_block_delta\ndata: {"delta":{"text":"hello"}}\n\n'
            ready.set()
            await asyncio.Event().wait()

        async def handler(request):
            seen.append(request.url.host)
            if phase == "retry":
                return httpx.Response(400, json={"error": "try again"})
            if phase == "headers":
                ready.set()
                await asyncio.Event().wait()
            return httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=Body(chunks()))

        async def operation(client):
            task = asyncio.create_task(client.post("/v1/messages", json={"model": "m", "stream": True}))
            try:
                await asyncio.wait_for(ready.wait(), 2)
                if cancel == "manual":
                    call = inflight.snapshot()["calls"][0]
                    await client.post(f'/admin/api/inflight/{call["id"]}/cancel')
                    await asyncio.wait_for(task, 2)
                else:
                    task.cancel()
                    with pytest.raises(asyncio.CancelledError):
                        await task
            finally:
                if not task.done():
                    task.cancel()
                await asyncio.gather(task, return_exceptions=True)
        data = await clients(app, monkeypatch, handler, operation)
        assert seen == ["primary.example"]
        return data

    data = asyncio.run(run())
    end = next(r for r in data if r["kind"] == "request_end")
    assert end["note"] == ("manual_abort" if cancel == "manual" else "client_abort")
    attempts = [r for r in data if r["kind"] == "attempt_end"]
    assert len(attempts) == 1 and end["attempt_count"] == 1
    assert attempts[0]["interrupted"] == (phase != "retry")
    assert app.state.learning.active_groups == {}
    assert app.state.learning.errors == 0


def test_queue_full_and_oversized_records_are_dropped(tmp_path, monkeypatch):
    monkeypatch.setattr(learning, "QUEUE_SIZE", 2)
    collector = learning.Collector(tmp_path)
    for _ in range(3):
        collector.submit({"kind": "test"})
    assert collector.queue.qsize() == 2 and collector.dropped == 1
    collector._write([{"oversized": "x" * learning.MAX_RECORD_BYTES}])
    assert collector.dropped == 2 and list(tmp_path.iterdir()) == []


def test_worker_start_failure_disables_collection_and_shutdown_remains_safe(tmp_path, monkeypatch):
    def fail_start(self):
        raise RuntimeError("thread unavailable")
    monkeypatch.setattr(learning.threading.Thread, "start", fail_start)
    collector = learning.Collector(tmp_path)
    collector.start()
    collector.stop()
    assert collector.status()["enabled"] is False
    assert collector.status()["last_error"] == "RuntimeError"
    assert list(tmp_path.iterdir()) == []


def test_rotation_and_retention_only_prune_owned_files(tmp_path, monkeypatch):
    monkeypatch.setattr(learning, "MAX_FILE_BYTES", 300)
    monkeypatch.setattr(learning, "MAX_TOTAL_BYTES", 650)
    collector = learning.Collector(tmp_path)
    for i in range(10):
        collector._write([{"n": i, "padding": "x" * 100}])
    files = collector._owned_files()
    assert len(files) >= 2
    assert sum(p.stat().st_size for p in files) <= 650
    assert all(p.stat().st_size <= 300 for p in files)
    keep = tmp_path / "notes.jsonl"
    keep.write_text("keep")
    old = tmp_path / ("events-20000101T000000000000Z-" + "a" * 32 + ".jsonl")
    old.write_text("{}\n")
    os.utime(old, (0, 0))
    collector._prune()
    assert not old.exists() and keep.read_text() == "keep"


def test_daily_rotation_and_peer_counts(tmp_path):
    collector = learning.Collector(tmp_path)
    collector._write([{"n": 1}])
    previous = collector._file
    collector._day = "20000101"
    collector._write([{"n": 2}])
    assert collector._file != previous
    a, b = learning.Trace(collector), learning.Trace(collector)
    start_attempt(a)
    start_attempt(b)
    a.end_attempt(status=200, note="ok")
    b.end_attempt(status=200, note="ok")
    ends = [r for r in queued(collector) if r["kind"] == "attempt_end"]
    assert [r["active_group_peers"] for r in ends] == [0, 1]
    assert collector.active_groups == {}


def test_writer_failure_cannot_fail_forwarding(app, monkeypatch):
    routes()
    def fail(*args):
        raise OSError("PRIVATE-PATH-OR-SECRET")
    monkeypatch.setattr(learning.Collector, "_write", fail)
    async def operation(client):
        response = await client.post("/v1/responses", json={"model": "m"})
        assert response.status_code == 200
    asyncio.run(clients(app, monkeypatch, lambda request: httpx.Response(200, json={"output": []}), operation))
    status = app.state.learning.status()
    assert status["errors"] > 0 and status["last_error"] == "OSError"
    assert status["dropped"] > 0 and "PRIVATE-" not in json.dumps(status)


def test_disabled_collector_and_metadata_requests_create_no_learning_records(app, monkeypatch):
    routes("anthropic")
    monkeypatch.setenv("MODEL_GATEWAY_LEARNING", "0")
    async def operation(client):
        assert (await client.post("/v1/messages", json={"model": "m"})).status_code == 200
    data = asyncio.run(clients(app, monkeypatch, lambda request: httpx.Response(200, json={}), operation))
    assert data == [] and not (config.DATA_DIR / "learning").exists()


def test_metadata_and_rejected_requests_are_excluded(app, monkeypatch):
    routes("anthropic")
    async def operation(client):
        assert (await client.post("/v1/messages/count_tokens", json={"model": "m"})).status_code == 200
        assert (await client.post("/v1/messages", content="bad")).status_code == 400
    data = asyncio.run(clients(app, monkeypatch, lambda request: httpx.Response(200, json={}), operation))
    assert all(r["kind"] == "collector_status" for r in data)


def test_report_handles_partial_lines_and_incomplete_requests(tmp_path):
    script = Path(__file__).resolve().parents[1] / "dev" / "learning-report.py"
    spec = importlib.util.spec_from_file_location("learning_report", script)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    path = tmp_path / ("events-20260929T000000000000Z-" + "a" * 32 + ".jsonl")
    path.write_text(
        '{"schema_version":1,"kind":"request_start","request_id":"one"}\n'
        '{"schema_version":99,"kind":"unknown"}\n{"partial":',
        encoding="utf-8",
    )
    result = module.report(tmp_path)
    assert result["request_starts_without_end"] == 1
    assert result["unknown_schema_lines"] == result["bad_lines"] == 1
