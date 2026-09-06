from __future__ import annotations

import asyncio
import json
import socket
import threading
import time

import httpx
import pytest
import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


# 撑过 64KB 尾部窗口用：40 × ~2.1KB ≈ 85KB，足以把 message_start 挤出尾巴
BULK_DELTAS = 40
DELTA_TEXT = "x" * 2000


def sse(event: str, payload: dict) -> bytes:
    return f"event: {event}\ndata: {json.dumps(payload)}\n\n".encode()


def wait_server_started(server: uvicorn.Server, thread: threading.Thread, timeout: float = 10.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if server.started:
            return
        if not thread.is_alive():
            raise RuntimeError("server thread died during startup")
        time.sleep(0.02)
    raise RuntimeError("server did not start in time")


def build_upstream_app(name: str, sick: dict | None = None) -> FastAPI:
    app = FastAPI()
    # sick["status"] 一被设上，两个转发端点就一律回那个状态码 —— 用来演「这个站坏了」。
    # 放在字典里是为了能在运行中翻转（测冷却期满之后自己恢复）
    sick = sick if sick is not None else {"status": None, "missing": set()}

    def sick_now() -> JSONResponse | None:
        code = sick.get("status")
        if not code:
            return None
        return JSONResponse({"error": {"message": f"{name} is sick", "code": code}}, status_code=code)

    def unknown(model: str) -> JSONResponse | None:
        """这个站没有这个模型 id。站级故障是 5xx，这个是模型级的 404，两回事。"""
        if not model or model not in sick.get("missing", ()):
            return None
        return JSONResponse(
            {"type": "error", "error": {"type": "not_found_error", "message": f"no model {model}"}},
            status_code=404,
        )

    @app.get("/v1/models")
    def models(request: Request) -> dict:
        # 按 key / 按接口返回不同的模型列表 —— 同一个站的两把 key 能看到的东西常常不一样，
        # 而 Anthropic 那边认的是 x-api-key + anthropic-version，两件事都得能断言
        ids = ["gpt-test", "claude-test"]
        if request.headers.get("anthropic-version"):
            ids = ["claude-test", "claude-haiku-test"] if request.headers.get("x-api-key") \
                else ["missing-x-api-key"]
        elif request.headers.get("authorization", "").endswith("-vip"):
            ids = ["gpt-test", "vip-only"]
        return {"object": "list", "data": [{"id": i, "object": "model"} for i in ids]}

    @app.post("/v1/responses")
    async def responses(request: Request) -> object:
        if (bad := sick_now()) is not None:
            return bad
        body = json.loads((await request.body()) or b"{}")
        if (gone := unknown(body.get("model", ""))) is not None:
            return gone
        if body.get("stream"):
            mode = body.get("mode", "")

            async def gen():
                for i in range(6):
                    await asyncio.sleep(0.04)
                    yield f'data: {json.dumps({"upstream": name, "i": i})}\n\n'.encode()
                if mode == "split_marker":
                    # 把完成事件的标记切在两块之间，模拟真实的 TCP 分片
                    yield b'data: {"type": "response.comp'
                    yield b'leted", "response": {"usage": {"input_tokens": 7, "output_tokens": 2}}}\n\n'
                else:
                    yield b"data: [DONE]\n\n"
                if mode == "lingering":
                    # 发完完成事件却不收连接，等客户端自己走（站A 这类站的行为）
                    await asyncio.sleep(20)

            return StreamingResponse(gen(), media_type="text/event-stream")
        if body.get("fail"):
            return JSONResponse({"error": {"message": "quota exhausted"}}, status_code=429)
        return JSONResponse(
            {
                "id": "resp_1",
                "upstream": name,
                "output": [],
                "ua": request.headers.get("user-agent", ""),
                "x_probe": request.headers.get("x-probe", ""),
                "auth": request.headers.get("authorization", ""),
                "originator": request.headers.get("originator", ""),
                "x_drop": request.headers.get("x-drop-me", ""),
                "usage": {
                    "input_tokens": 120,
                    "output_tokens": 30,
                    "input_token_details": {"cached_tokens": 80},
                },
            }
        )

    @app.post("/v1/messages")
    async def messages(request: Request) -> object:
        if (bad := sick_now()) is not None:
            return bad
        raw = await request.body()
        body = json.loads(raw or b"{}")
        if (gone := unknown(body.get("model", ""))) is not None:
            return gone
        echo = {
            "upstream": name,
            "model": body.get("model", ""),
            # 原始字节原样回显：没配改名时网关必须一个字节都不动
            "raw": raw.decode(),
            "auth": request.headers.get("authorization", ""),
            "x_api_key": request.headers.get("x-api-key", ""),
            "version": request.headers.get("anthropic-version", ""),
            "beta": request.headers.get("anthropic-beta", ""),
        }
        if body.get("fail"):
            return JSONResponse(
                {"type": "error", "error": {"type": "rate_limit_error", "message": "quota exhausted"}},
                status_code=429,
            )
        if body.get("stream"):
            mode = body.get("mode", "")

            async def gen():
                yield sse("message_start", {
                    "type": "message_start",
                    "message": {
                        "id": "msg_1", "model": body.get("model", ""),
                        "usage": {"input_tokens": 1234, "cache_creation_input_tokens": 0,
                                  "cache_read_input_tokens": 900, "output_tokens": 1},
                    },
                })
                for _ in range(BULK_DELTAS if mode == "bulk" else 2):
                    await asyncio.sleep(0)
                    yield sse("content_block_delta", {
                        "type": "content_block_delta", "index": 0,
                        "delta": {"type": "text_delta", "text": DELTA_TEXT},
                    })
                if mode == "no_end":
                    return          # 劣质上游：一句结束事件都不发
                yield sse("message_delta", {
                    "type": "message_delta", "delta": {"stop_reason": "end_turn"},
                    "usage": {"output_tokens": 777},
                })
                yield sse("message_stop", {"type": "message_stop"})

            return StreamingResponse(gen(), media_type="text/event-stream")
        return JSONResponse({
            **echo,
            "content": [{"type": "text", "text": "hi"}],
            "usage": {"input_tokens": 11, "output_tokens": 22, "cache_read_input_tokens": 5},
        })

    @app.post("/v1/messages/count_tokens")
    async def count_tokens(request: Request) -> object:
        body = json.loads((await request.body()) or b"{}")
        return JSONResponse({"input_tokens": 42, "model": body.get("model", ""), "upstream": name})

    return app


def wait_for_row(client: httpx.Client, timeout: float = 6.0) -> dict:
    """转发记录是在流收尾时才落库的，所以要等一下。"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        rows = client.get("/admin/api/requests").json()
        if rows:
            return rows[0]
        time.sleep(0.05)
    raise AssertionError("等不到转发记录")


class MockUpstream:
    def __init__(self, name: str) -> None:
        self.name = name
        self.port = free_port()
        # 站根：/v1 由网关按接口自己补，两种接口的路径都在它底下
        self.base_url = f"http://127.0.0.1:{self.port}"
        # 运行中可翻转的「病历」：设了 status 就一律回那个码，用来演故障与恢复；
        # missing 里的模型名一律回 404，用来演「这个站把某个模型 id 下掉了」
        self.sick: dict = {"status": None, "missing": set()}
        self._server = uvicorn.Server(
            uvicorn.Config(
                build_upstream_app(name, self.sick), host="127.0.0.1", port=self.port, log_level="error"
            )
        )
        self._thread = threading.Thread(target=self._server.run, daemon=True)

    def fail_with(self, status: int) -> None:
        self.sick["status"] = status

    def drop_model(self, *names: str) -> None:
        """这个站不再认这几个模型 id（带日期后缀的那种最常被下掉）。"""
        self.sick["missing"].update(names)

    def heal(self) -> None:
        self.sick["status"] = None
        self.sick["missing"] = set()

    def __enter__(self) -> "MockUpstream":
        self._thread.start()
        wait_server_started(self._server, self._thread)
        return self

    def __exit__(self, *exc: object) -> None:
        self._server.should_exit = True
        self._thread.join(timeout=5)


@pytest.fixture()
def gateway(tmp_path, monkeypatch):
    from gateway import config, failover, inflight
    from gateway.server import start_server_thread

    data_dir = tmp_path / "data"
    monkeypatch.setattr(config, "DATA_DIR", data_dir)
    monkeypatch.setattr(config, "DB_PATH", data_dir / "gateway.db")
    # 断路器和「进行中」登记表都是进程内的内存状态，测试跑在同一个进程里 —— 不清会串到下一个用例
    failover.reset()
    inflight.reset()

    port = free_port()
    server, thread = start_server_thread(port)
    try:
        wait_server_started(server, thread)
        base_url = f"http://127.0.0.1:{port}"
        # trust_env=False：全程都是回环地址，绝不能让系统代理（Clash 之类）插一脚
        with httpx.Client(base_url=base_url, timeout=15.0, trust_env=False) as client:
            yield client
    finally:
        server.should_exit = True
        thread.join(timeout=5)


DEFAULT_GROUP = "默认"


def add_upstream(
    client: httpx.Client,
    mock: MockUpstream,
    name: str,
    protocol: str = "openai",
    api_key: str | None = None,
) -> int:
    """建一个供应商 + 一个分组，返回**分组** id。

    接口是分组的属性（那把 key 走哪种格式），所以建站的时候就得说清楚。候选、批量导入、
    拉模型列表也都以分组为单位 —— 同一个站的两把 key 能看到的模型不一样。
    需要供应商 id 的地方单独调 provider_id()。
    """
    resp = client.post("/admin/api/upstreams", json={"name": name, "base_url": mock.base_url})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["groups"] == [], "新建的供应商还没有分组：接口和 key 都得自己选"
    return add_group(
        client, int(body["id"]), protocol, api_key=f"key-{name}" if api_key is None else api_key
    )


def add_group(
    client: httpx.Client,
    upstream_id: int,
    protocol: str,
    name: str = DEFAULT_GROUP,
    api_key: str = "",
) -> int:
    resp = client.post(
        f"/admin/api/upstreams/{upstream_id}/groups",
        json={"name": name, "protocol": protocol, "api_key": api_key},
    )
    assert resp.status_code == 200, resp.text
    return int(resp.json()["id"])


def provider_id(client: httpx.Client, name: str) -> int:
    row = next(u for u in client.get("/admin/api/upstreams").json() if u["name"] == name)
    return int(row["id"])


def add_route(client: httpx.Client, model_name: str, group_id: int, remote_model: str = "") -> int:
    """加一个候选，返回它的 route_id —— 切换 / 改 / 删单条都按这个 id 指。"""
    resp = client.post(
        "/admin/api/models",
        json={"model_name": model_name, "group_id": group_id, "remote_model": remote_model},
    )
    assert resp.status_code == 200, resp.text
    return int(resp.json()["route_id"])


def cands(client: httpx.Client, model_name: str) -> list[dict]:
    """某个模型的候选，按链上的顺序。"""
    row = next(
        (r for r in client.get("/admin/api/models").json() if r["model_name"] == model_name), None
    )
    return list(row["candidates"]) if row else []


def route_id(client: httpx.Client, model_name: str, group_id: int) -> int:
    """这个模型在某个分组下的候选 id（bulk-add 建出来的拿不到返回值，从列表里找）。"""
    hit = [c for c in cands(client, model_name) if c["group_id"] == group_id]
    assert len(hit) == 1, f"{model_name} 在 g{group_id} 下有 {len(hit)} 条候选"
    return int(hit[0]["route_id"])


def msg(model: str, **extra) -> dict:
    return {"model": model, "max_tokens": 16, "messages": [{"role": "user", "content": "hi"}], **extra}


def parse_sse_events(raw: str) -> list[dict]:
    return [
        json.loads(line[len("data: "):])
        for line in raw.splitlines()
        if line.startswith("data: ") and line != "data: [DONE]"
    ]


def wait_rows(client: httpx.Client, n: int, timeout: float = 8.0) -> list[dict]:
    """等到至少 n 条转发记录，返回**时间升序**的列表。

    失败的那几次尝试是同步落库的，但胜出那次要等流收尾才写，所以得等。
    """
    deadline = time.monotonic() + timeout
    rows: list[dict] = []
    while time.monotonic() < deadline:
        rows = client.get("/admin/api/requests?limit=50").json()
        if len(rows) >= n:
            return list(reversed(rows))
        time.sleep(0.05)
    raise AssertionError(f"等不到 {n} 条转发记录，只有 {len(rows)} 条: {rows}")


def rows_on(client: httpx.Client, upstream: str) -> int:
    return sum(1 for r in client.get("/admin/api/requests?limit=50").json() if r["upstream"] == upstream)


def wait_inflight(client: httpx.Client, ready, timeout: float = 6.0) -> dict:
    """等 /admin/api/inflight 到某个状态。注销发生在流收尾时，所以要等一下。"""
    deadline = time.monotonic() + timeout
    data: dict = {}
    while time.monotonic() < deadline:
        data = client.get("/admin/api/inflight").json()
        if ready(data):
            return data
        time.sleep(0.05)
    raise AssertionError(f"inflight 没等到期望的状态: {data}")


def test_import_models_and_switch_without_interrupting_stream(gateway):
    with MockUpstream("siteA") as a, MockUpstream("siteB") as b:
        g_a = add_upstream(gateway, a, "siteA")
        g_b = add_upstream(gateway, b, "siteB")

        pulled = gateway.get(f"/admin/api/groups/{g_a}/remote-models").json()["models"]
        assert set(pulled) == {"gpt-test", "claude-test"}

        added = gateway.post(
            "/admin/api/models/bulk-add", json={"group_id": g_a, "model_names": pulled}
        ).json()
        assert added == {"added": 2, "skipped": []}

        r_b = add_route(gateway, "gpt-test", g_b, "gpt-test")

        exposed = gateway.get("/v1/models").json()
        assert {m["id"] for m in exposed["data"]} == {"gpt-test", "claude-test"}

        with gateway.stream("POST", "/v1/responses", json={"model": "gpt-test", "stream": True}) as stream:
            assert stream.status_code == 200
            chunks: list[str] = []
            for chunk in stream.iter_text():
                if len(chunks) == 0 and chunk.strip():
                    switched = gateway.post(
                        "/admin/api/models/switch", json={"route_id": r_b}
                    )
                    assert switched.json() == {"ok": True}
                chunks.append(chunk)
            raw = "".join(chunks)

        events = parse_sse_events(raw)
        assert [e["i"] for e in events] == list(range(6))
        assert all(e["upstream"] == "siteA" for e in events), "切换后进行中的流必须完整走完旧上游"

        follow_up = gateway.post("/v1/responses", json={"model": "gpt-test"}).json()
        assert follow_up["upstream"] == "siteB", "新请求必须路由到新上游"


def test_upstream_error_is_passed_through(gateway):
    with MockUpstream("siteA") as a:
        g_a = add_upstream(gateway, a, "siteA")
        gateway.post("/admin/api/models/bulk-add", json={"group_id": g_a, "model_names": ["gpt-test"]})

        resp = gateway.post("/v1/responses", json={"model": "gpt-test", "fail": True})
        assert resp.status_code == 429
        assert resp.json()["error"]["message"] == "quota exhausted"


def test_unknown_model_returns_404(gateway):
    resp = gateway.post("/v1/responses", json={"model": "nope"})
    assert resp.status_code == 404


def test_delete_active_candidate_reattaches_remaining(gateway):
    with MockUpstream("siteA") as a, MockUpstream("siteB") as b:
        g_a = add_upstream(gateway, a, "siteA")
        g_b = add_upstream(gateway, b, "siteB")
        rids = {gid: add_route(gateway, "shared", gid, "gpt-test") for gid in (g_a, g_b)}

        routes = gateway.get("/admin/api/models").json()
        shared = next(g for g in routes if g["model_name"] == "shared")
        assert shared["active_route_id"] == rids[g_a]

        assert gateway.delete(
            "/admin/api/models", params={"model_name": "shared", "group_id": g_a}
        ).status_code == 200
        routes = gateway.get("/admin/api/models").json()
        shared = next(g for g in routes if g["model_name"] == "shared")
        assert shared["active_route_id"] == rids[g_b]

        follow_up = gateway.post("/v1/responses", json={"model": "shared"}).json()
        assert follow_up["upstream"] == "siteB"


def test_client_headers_pass_through_and_auth_override(gateway):
    with MockUpstream("siteA") as a, MockUpstream("siteB") as b:
        g_a = add_upstream(gateway, a, "siteA")
        g_b = add_upstream(gateway, b, "nokey", api_key="")
        rids = {gid: add_route(gateway, "hdr-test", gid, "gpt-test") for gid in (g_a, g_b)}

        client_headers = {"User-Agent": "codex_cli_rs/1.0", "X-Probe": "abc", "Authorization": "Bearer client-token"}
        resp = gateway.post("/v1/responses", json={"model": "hdr-test"}, headers=client_headers).json()
        assert resp["ua"] == "codex_cli_rs/1.0", "客户端 UA 必须原样到达上游"
        assert resp["x_probe"] == "abc", "自定义头必须原样到达上游"
        assert resp["auth"] == "Bearer key-siteA", "分组存有 key 时覆盖客户端 Authorization"

        gateway.post("/admin/api/models/switch", json={"route_id": rids[g_b]})
        resp = gateway.post("/v1/responses", json={"model": "hdr-test"}, headers=client_headers).json()
        assert resp["auth"] == "Bearer client-token", "分组 key 为空时必须透传客户端 Authorization"


def test_header_override_applied_per_upstream(gateway):
    with MockUpstream("siteA") as a:
        g_a = add_upstream(gateway, a, "siteA")
        uid = provider_id(gateway, "siteA")
        override = '{"user-agent": "codex_cli_rs", "originator": "codex_cli_rs", "x-drop-me": null}'
        detail = gateway.get("/admin/api/upstreams").json()[0]
        r = gateway.put(
            f"/admin/api/upstreams/{uid}",
            json={
                "name": detail["name"],
                "base_url": detail["base_url"],
                "enabled": True,
                "header_override": override,
            },
        )
        assert r.status_code == 200, r.text
        assert r.json()["header_override"] == override

        gateway.post("/admin/api/models/bulk-add", json={"group_id": g_a, "model_names": ["gpt-test"]})
        resp = gateway.post(
            "/v1/responses",
            json={"model": "gpt-test"},
            headers={"User-Agent": "some-other-agent/2.0", "X-Drop-Me": "bye"},
        ).json()
        assert resp["ua"] == "codex_cli_rs", "覆写必须替换客户端 UA"
        assert resp["originator"] == "codex_cli_rs", "覆写新增的头必须生效"
        assert resp["x_drop"] == "", "值为 null 的头必须被删除"
        assert resp["x_probe"] == "", "未覆写的头仍原样透传（此处为空）"
        assert resp["auth"] == "Bearer key-siteA"


def test_request_log_records_usage_and_stats(gateway):
    with MockUpstream("siteA") as a:
        g_a = add_upstream(gateway, a, "siteA")
        gateway.post("/admin/api/models/bulk-add", json={"group_id": g_a, "model_names": ["gpt-test"]})
        assert gateway.post("/v1/responses", json={"model": "gpt-test"}).status_code == 200

        rows = gateway.get("/admin/api/requests").json()
        assert rows, "至少有一条转发记录"
        row = rows[0]
        assert row["model"] == "gpt-test"
        assert row["upstream"] == "siteA"
        assert row["group_name"] == "默认"
        assert row["status"] == 200
        assert row["input_tokens"] == 120
        assert row["output_tokens"] == 30
        assert row["cached_tokens"] == 80
        assert row["client"] == "python-httpx"

        stats = gateway.get("/admin/api/stats").json()
        assert stats["requests"] >= 1
        assert stats["input_tokens"] >= 120
        assert stats["cached_tokens"] >= 80
        assert 0 < stats["cache_hit_rate"] < 1


def test_disabled_upstream_is_not_routed(gateway):
    with MockUpstream("siteA") as a:
        g_a = add_upstream(gateway, a, "siteA")
        uid = provider_id(gateway, "siteA")
        gateway.post("/admin/api/models/bulk-add", json={"group_id": g_a, "model_names": ["gpt-test"]})

        detail = gateway.get("/admin/api/upstreams").json()[0]
        gateway.put(
            f"/admin/api/upstreams/{uid}",
            json={"name": detail["name"], "base_url": detail["base_url"], "enabled": False},
        )

        assert gateway.post("/v1/responses", json={"model": "gpt-test"}).status_code == 404
        exposed = {m["id"] for m in gateway.get("/v1/models").json()["data"]}
        assert "gpt-test" in exposed, "停用供应商只影响路由，不改变对下游暴露的模型清单"


def test_disabled_group_is_not_routed(gateway):
    """分组也能单独停用：同一个站的某把 key 额度用完了，先停这一组而不是整个站。"""
    with MockUpstream("siteA") as a:
        g_a = add_upstream(gateway, a, "siteA")
        add_route(gateway, "gpt-test", g_a)
        assert gateway.post("/v1/responses", json={"model": "gpt-test"}).status_code == 200

        r = gateway.put(
            f"/admin/api/groups/{g_a}",
            json={"name": "默认", "protocol": "openai", "api_key": "key-siteA", "enabled": False},
        )
        assert r.status_code == 200, r.text
        assert gateway.post("/v1/responses", json={"model": "gpt-test"}).status_code == 404
        assert "gpt-test" in {m["id"] for m in gateway.get("/v1/models").json()["data"]}


def test_model_name_with_slash_can_be_deleted(gateway):
    """公益站上很多模型名带 '/'，放在 URL 路径里会被当成多段，所以删除走 query 参数。"""
    with MockUpstream("siteA") as a:
        g_a = add_upstream(gateway, a, "siteA")
        name = "deepseek-ai/DeepSeek-V3"
        add_route(gateway, name, g_a, name)
        assert name in {m["id"] for m in gateway.get("/v1/models").json()["data"]}

        resp = gateway.delete("/admin/api/models", params={"model_name": name, "group_id": g_a})
        assert resp.status_code == 200, resp.text
        assert name not in {m["id"] for m in gateway.get("/v1/models").json()["data"]}


def test_delete_whole_model_removes_every_candidate(gateway):
    with MockUpstream("siteA") as a, MockUpstream("siteB") as b:
        g_a = add_upstream(gateway, a, "siteA")
        g_b = add_upstream(gateway, b, "siteB")
        for gid in (g_a, g_b):
            add_route(gateway, "shared", gid, "gpt-test")

        resp = gateway.delete("/admin/api/models", params={"model_name": "shared"})
        assert resp.json() == {"ok": True, "removed": 2}
        assert gateway.get("/admin/api/models").json() == []
        assert gateway.delete("/admin/api/models", params={"model_name": "shared"}).status_code == 404


def test_duplicate_upstream_name_is_409_not_500(gateway):
    with MockUpstream("siteA") as a:
        add_upstream(gateway, a, "siteA")
        dup = gateway.post("/admin/api/upstreams", json={"name": "siteA", "base_url": a.base_url + "/x"})
        assert dup.status_code == 409
        assert "同名" in dup.json()["detail"]


def test_duplicate_base_url_is_rejected_with_a_hint_about_groups(gateway):
    """站D / DDD2 那种「同一个站建成两个供应商」正是分组要解决的问题，别让它再发生。"""
    with MockUpstream("siteA") as a:
        add_upstream(gateway, a, "siteA")
        dup = gateway.post("/admin/api/upstreams", json={"name": "siteA-2", "base_url": a.base_url})
        assert dup.status_code == 409
        assert "分组" in dup.json()["detail"] and "siteA" in dup.json()["detail"]


def test_bulk_add_to_missing_group_is_404(gateway):
    resp = gateway.post("/admin/api/models/bulk-add", json={"group_id": 9999, "model_names": ["x"]})
    assert resp.status_code == 404


def test_remote_model_pull_and_manual_add_default_remote_name(gateway):
    with MockUpstream("siteA") as a:
        g_a = add_upstream(gateway, a, "siteA")
        # remote_model 留空时应回落成 model_name
        r = gateway.post("/admin/api/models", json={"model_name": "gpt-test", "group_id": g_a})
        assert r.status_code == 200
        assert r.json()["remote_model"] == "gpt-test"
        assert gateway.post(
            "/admin/api/models", json={"model_name": "gpt-test", "group_id": g_a}
        ).status_code == 409


def test_request_log_can_be_cleared(gateway):
    with MockUpstream("siteA") as a:
        g_a = add_upstream(gateway, a, "siteA")
        gateway.post("/admin/api/models/bulk-add", json={"group_id": g_a, "model_names": ["gpt-test"]})
        gateway.post("/v1/responses", json={"model": "gpt-test"})
        assert gateway.get("/admin/api/requests").json()

        assert gateway.delete("/admin/api/requests").json()["ok"] is True
        assert gateway.get("/admin/api/requests").json() == []
        assert gateway.get("/admin/api/stats").json()["requests"] == 0


def test_admin_api_rejects_cross_site_and_foreign_host(gateway):
    """管理接口没鉴权，只能靠拒绝跨站请求兜底，否则任何网页都能改配置或关掉进程。"""
    assert gateway.get("/admin/api/upstreams").status_code == 200

    blocked = gateway.post(
        "/admin/api/shutdown", headers={"Origin": "https://evil.example", "Sec-Fetch-Site": "cross-site"}
    )
    assert blocked.status_code == 403

    rebound = gateway.get("/admin/api/upstreams", headers={"Host": "evil.example"})
    assert rebound.status_code == 403

    # 同源的浏览器请求必须放行
    same_origin = gateway.get(
        "/admin/api/upstreams",
        headers={"Origin": str(gateway.base_url), "Sec-Fetch-Site": "same-origin"},
    )
    assert same_origin.status_code == 200


def test_connect_failure_is_logged_as_502(gateway):
    created = gateway.post(
        "/admin/api/upstreams", json={"name": "dead", "base_url": "http://127.0.0.1:1"}
    ).json()
    gid = add_group(gateway, int(created["id"]), "openai")
    gateway.post("/admin/api/models/bulk-add", json={"group_id": gid, "model_names": ["ghost"]})

    assert gateway.post("/v1/responses", json={"model": "ghost"}).status_code == 502
    row = gateway.get("/admin/api/requests").json()[0]
    assert row["status"] == 502 and row["note"] == "connect_failed"


def test_client_disconnect_mid_stream_is_recorded_and_gateway_survives(gateway):
    """客户端中途断开不能让转发协程炸掉，也不能漏掉这条记录。"""
    with MockUpstream("siteA") as a:
        g_a = add_upstream(gateway, a, "siteA")
        gateway.post("/admin/api/models/bulk-add", json={"group_id": g_a, "model_names": ["gpt-test"]})

        with gateway.stream("POST", "/v1/responses", json={"model": "gpt-test", "stream": True}) as stream:
            assert stream.status_code == 200
            next(stream.iter_bytes())  # 只读第一块就走，此时还没收到完成事件

        row = wait_for_row(gateway)
        assert row["note"] == "client_abort", f"真的中途断开要标出来，实际是 {row['note']}"

        # 断流之后网关仍然正常工作
        assert gateway.post("/v1/responses", json={"model": "gpt-test"}).status_code == 200


def test_client_leaving_after_completion_event_is_not_flagged(gateway):
    """上游发完完成事件却不收连接、客户端拿到就走 —— 这是正常收尾，不能记成客户端断开。"""
    with MockUpstream("siteA") as a:
        g_a = add_upstream(gateway, a, "siteA")
        gateway.post("/admin/api/models/bulk-add", json={"group_id": g_a, "model_names": ["gpt-test"]})

        with gateway.stream(
            "POST", "/v1/responses", json={"model": "gpt-test", "stream": True, "mode": "lingering"}
        ) as stream:
            assert stream.status_code == 200
            for chunk in stream.iter_bytes():
                if b"[DONE]" in chunk:
                    break  # 跟 codex 一样：看到完成事件就不等 TCP 关闭了

        row = wait_for_row(gateway)
        assert row["note"] == "ok", f"流已经走完了，不该报异常，实际是 {row['note']}"


def test_completion_marker_split_across_chunks_is_detected(gateway):
    """完成标记被切在两个 chunk 之间时也要认出来，否则会误报截断。"""
    with MockUpstream("siteA") as a:
        g_a = add_upstream(gateway, a, "siteA")
        gateway.post("/admin/api/models/bulk-add", json={"group_id": g_a, "model_names": ["gpt-test"]})

        resp = gateway.post("/v1/responses", json={"model": "gpt-test", "stream": True, "mode": "split_marker"})
        assert resp.status_code == 200

        row = wait_for_row(gateway)
        assert row["note"] == "ok", f"标记跨块也必须认出来，实际是 {row['note']}"
        assert row["input_tokens"] == 7


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


# ================================================================ Anthropic 格式


def test_anthropic_messages_rewrites_model_and_injects_both_auth_headers(gateway):
    """档位名 -> 上游真名的改写，以及 x-api-key 必须被覆盖（客户端会带占位 key）。"""
    with MockUpstream("siteA") as a:
        g_a = add_upstream(gateway, a, "siteA", "anthropic")
        add_route(gateway, "opus", g_a, "claude-opus-4-1")

        resp = gateway.post(
            "/v1/messages", json=msg("opus"), headers={"x-api-key": "placeholder-from-client"}
        )
        assert resp.status_code == 200, resp.text
        seen = resp.json()
        assert seen["model"] == "claude-opus-4-1", "上游必须收到它自己那边的真名"
        assert seen["auth"] == "Bearer key-siteA"
        assert seen["x_api_key"] == "key-siteA", "客户端的占位 key 不能把配好的真 key 压掉"
        assert seen["version"] == "2023-06-01", "客户端没带 anthropic-version 时要补上"

        row = wait_for_row(gateway)
        assert row["model"] == "opus"
        assert row["remote_model"] == "claude-opus-4-1"
        assert (row["input_tokens"], row["output_tokens"], row["cached_tokens"]) == (11, 22, 5)
        assert row["client"] == "python-httpx"


def test_anthropic_stream_usage_survives_message_start_falling_out_of_tail(gateway):
    """Anthropic 把输入 token 放在流开头的 message_start，只留尾巴窗口会丢掉它。"""
    with MockUpstream("siteA") as a:
        g_a = add_upstream(gateway, a, "siteA", "anthropic")
        add_route(gateway, "opus", g_a, "claude-opus-4-1")

        resp = gateway.post("/v1/messages", json=msg("opus", stream=True, mode="bulk"))
        assert resp.status_code == 200
        assert len(resp.content) > 65536, "这个用例的前提是流长过 TAIL_KEEP"
        assert b"message_start" not in resp.content[-65536:]

        row = wait_for_row(gateway)
        assert row["note"] == "ok", f"message_stop 就是结束事件，不该报截断，实际 {row['note']}"
        assert row["input_tokens"] == 1234, "输入 token 只在流开头出现过一次"
        assert row["output_tokens"] == 777, "输出 token 取末尾 message_delta 里的终值"
        assert row["cached_tokens"] == 900, "cache_read_input_tokens 要算进缓存命中"


def test_anthropic_stream_without_message_stop_is_flagged_truncated(gateway):
    with MockUpstream("siteA") as a:
        g_a = add_upstream(gateway, a, "siteA", "anthropic")
        add_route(gateway, "opus", g_a, "claude-opus-4-1")

        assert gateway.post("/v1/messages", json=msg("opus", stream=True, mode="no_end")).status_code == 200
        row = wait_for_row(gateway)
        assert row["note"] == "truncated"


def test_one_million_suffix_is_stripped_and_beta_header_injected(gateway):
    """[1m] 是 Claude Code 自己的档位约定，上游不认；它只该变成一个 beta 头。"""
    with MockUpstream("siteA") as a:
        g_a = add_upstream(gateway, a, "siteA", "anthropic")
        add_route(gateway, "claude-opus-5", g_a, "claude-opus-4-1")
        # 后缀也可能被写在配置的 remote 名里
        add_route(gateway, "sonnet", g_a, "claude-sonnet-4-5[1m]")

        seen = gateway.post("/v1/messages", json=msg("claude-opus-5[1m]")).json()
        assert seen["model"] == "claude-opus-4-1", "方括号后缀绝不能传给上游"
        assert "context-1m-2025-08-07" in seen["beta"]

        seen = gateway.post("/v1/messages", json=msg("sonnet")).json()
        assert seen["model"] == "claude-sonnet-4-5", "remote 名里的后缀同样要摘掉"
        assert "context-1m-2025-08-07" in seen["beta"]

        # 客户端自己带了别的 beta 时要追加而不是覆盖
        seen = gateway.post(
            "/v1/messages", json=msg("claude-opus-5[1m]"), headers={"anthropic-beta": "oauth-2025-04-20"}
        ).json()
        assert "oauth-2025-04-20" in seen["beta"] and "context-1m-2025-08-07" in seen["beta"]

        # 没有 1M 标记时不该凭空加头
        seen = gateway.post("/v1/messages", json=msg("claude-opus-5")).json()
        assert "context-1m" not in seen["beta"]


def test_tier_keyword_fallback_catches_unconfigured_model_ids(gateway):
    """只配了档位名时，Claude Code 发来的具体 id 也要能落到同档位那条配置上。"""
    with MockUpstream("siteA") as a:
        g_a = add_upstream(gateway, a, "siteA", "anthropic")
        add_route(gateway, "opus", g_a, "real-opus")

        seen = gateway.post("/v1/messages", json=msg("claude-opus-4-1-20250805")).json()
        assert seen["model"] == "real-opus"
        row = wait_for_row(gateway)
        assert row["model"] == "claude-opus-4-1-20250805", "记录里留客户端问的那个名字"
        assert row["remote_model"] == "real-opus"

        # 认不出档位的名字仍然是 404，不能瞎猜
        assert gateway.post("/v1/messages", json=msg("gpt-5-turbo")).status_code == 404


def test_gateway_errors_use_the_shape_of_the_endpoint(gateway):
    """网关自己产生的错误也要按下游期望的形状返回，否则客户端解析不出来。"""
    an = gateway.post("/v1/messages", json=msg("nope"))
    assert an.status_code == 404
    assert an.json() == {
        "type": "error",
        "error": {"type": "not_found_error", "message": "模型 'nope' 未配置或当前上游已停用"},
    }

    oa = gateway.post("/v1/responses", json={"model": "nope"})
    assert oa.status_code == 404
    assert oa.json()["error"]["type"] == "gateway_error"

    bad = gateway.post("/v1/messages", content=b"{not json", headers={"content-type": "application/json"})
    assert bad.status_code == 400
    assert bad.json()["error"]["type"] == "invalid_request_error"


def test_upstream_error_body_is_passed_through_untouched(gateway):
    with MockUpstream("siteA") as a:
        g_a = add_upstream(gateway, a, "siteA", "anthropic")
        add_route(gateway, "opus", g_a, "claude-opus-4-1")

        resp = gateway.post("/v1/messages", json=msg("opus", fail=True))
        assert resp.status_code == 429
        assert resp.json()["error"]["type"] == "rate_limit_error"


def test_body_is_byte_exact_when_no_rename_configured(gateway):
    """名字两边一致时继续发原始字节，「透明中转」这个特性不能因为改写机制丢掉。"""
    with MockUpstream("siteA") as a:
        g_a = add_upstream(gateway, a, "siteA", "anthropic")
        add_route(gateway, "same", g_a, "same")
        add_route(gateway, "renamed", g_a, "other")

        raw = b'{"model": "same",   "note" : "  \xe7\x95\x99\xe7\x9d\x80  "}'
        seen = gateway.post(
            "/v1/messages", content=raw, headers={"content-type": "application/json"}
        ).json()
        assert seen["raw"] == raw.decode(), "没改名就一个字节都不该动"

        seen = gateway.post(
            "/v1/messages",
            content=b'{"model": "renamed", "note": "\xe4\xb8\xad\xe6\x96\x87"}',
            headers={"content-type": "application/json"},
        ).json()
        assert seen["model"] == "other"
        assert "中文" in seen["raw"], "重新序列化必须 ensure_ascii=False，否则中文体积暴涨"


def test_count_tokens_is_forwarded_but_kept_out_of_the_stats(gateway):
    """Claude Code 会频繁调它，记进转发记录会把累计次数和模型热度冲得没法看。"""
    with MockUpstream("siteA") as a:
        g_a = add_upstream(gateway, a, "siteA", "anthropic")
        add_route(gateway, "opus", g_a, "claude-opus-4-1")

        resp = gateway.post("/v1/messages/count_tokens", json=msg("opus"))
        assert resp.status_code == 200, resp.text
        assert resp.json() == {"input_tokens": 42, "model": "claude-opus-4-1", "upstream": "siteA"}

        assert gateway.get("/admin/api/requests").json() == []
        assert gateway.get("/admin/api/stats").json()["live"] == {"requests": 0, "streams": 0}


def test_models_list_satisfies_both_openai_and_anthropic_shapes(gateway):
    with MockUpstream("siteA") as a:
        g_a = add_upstream(gateway, a, "siteA", "anthropic")
        add_route(gateway, "opus", g_a, "claude-opus-4-1")

        listing = gateway.get("/v1/models").json()
        assert listing["object"] == "list" and listing["has_more"] is False
        assert listing["first_id"] == "opus" and listing["last_id"] == "opus"
        item = listing["data"][0]
        # OpenAI 侧要的字段
        assert (item["id"], item["object"], item["owned_by"]) == ("opus", "model", "model-gateway")
        # Anthropic 侧要的字段
        assert (item["type"], item["display_name"]) == ("model", "opus")
        assert item["created_at"]


def test_anthropic_switch_and_disable_reuse_the_same_routing(gateway):
    """热切换、停用兜底这些是路由层的能力，两种协议共用一套。"""
    with MockUpstream("siteA") as a, MockUpstream("siteB") as b:
        g_a = add_upstream(gateway, a, "siteA", "anthropic")
        g_b = add_upstream(gateway, b, "siteB", "anthropic")
        add_route(gateway, "opus", g_a, "on-a")
        r_b = add_route(gateway, "opus", g_b, "on-b")

        assert gateway.post("/v1/messages", json=msg("opus")).json()["upstream"] == "siteA"
        gateway.post("/admin/api/models/switch", json={"route_id": r_b})
        seen = gateway.post("/v1/messages", json=msg("opus")).json()
        assert (seen["upstream"], seen["model"]) == ("siteB", "on-b")


def test_request_log_records_protocol_and_health_splits_by_it(gateway):
    """管理页要能回答「这条是哪种格式来的」和「这个站的哪种格式在用」。

    一个站两种接口 = 两个分组，这也是那些「既有 GPT 又有 Claude」的公益站的正常形态。
    """
    with MockUpstream("siteA") as a:
        g_an = add_upstream(gateway, a, "siteA", "anthropic")
        g_oa = add_group(gateway, provider_id(gateway, "siteA"), "openai", name="gpt", api_key="key-siteA")
        add_route(gateway, "opus", g_an, "claude-opus-4-1")
        gateway.post("/admin/api/models/bulk-add", json={"group_id": g_oa, "model_names": ["gpt-test"]})

        assert gateway.post("/v1/messages", json=msg("opus")).status_code == 200
        assert gateway.post("/v1/responses", json={"model": "gpt-test"}).status_code == 200

        rows = gateway.get("/admin/api/requests").json()
        assert {r["model"]: r["protocol"] for r in rows} == {"opus": "anthropic", "gpt-test": "openai"}

        health = next(h for h in gateway.get("/admin/api/stats/upstreams").json() if h["name"] == "siteA")
        assert health["by_protocol"] == {
            "anthropic": {"n": 1, "bad": 0, "ok_rate": 1.0},
            "openai": {"n": 1, "bad": 0, "ok_rate": 1.0},
        }


def test_protocol_split_exposes_a_dead_endpoint(gateway):
    """「这个站的 anthropic 接口通不通」没法静态探测（分组只是声明），只能靠实际跑过的请求。"""
    created = gateway.post(
        "/admin/api/upstreams", json={"name": "dead", "base_url": "http://127.0.0.1:1"}
    ).json()
    add_route(gateway, "opus", add_group(gateway, int(created["id"]), "anthropic"), "claude-opus-4-1")

    assert gateway.post("/v1/messages", json=msg("opus")).status_code == 502
    health = next(h for h in gateway.get("/admin/api/stats/upstreams").json() if h["name"] == "dead")
    assert health["by_protocol"] == {"anthropic": {"n": 1, "bad": 1, "ok_rate": 0.0}}


def test_static_assets_are_not_cached(gateway):
    """ES 模块的 import 是裸路径挂不了版本号，只能靠 no-store 保证改完刷新就生效。"""
    for path in ("/", "/static/app.js", "/static/views.js", "/static/style.css"):
        resp = gateway.get(path)
        assert resp.status_code == 200, path
        assert resp.headers.get("cache-control") == "no-store", path


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


# ================================================================ 供应商 / 分组


def test_groups_keep_their_own_key_and_model_list(gateway):
    """同一个站两把 key：拉到的模型不一样，转发时也各用各的 key。"""
    with MockUpstream("siteA") as a:
        g_default = add_upstream(gateway, a, "siteA")
        uid = provider_id(gateway, "siteA")
        g_vip = add_group(gateway, uid, "openai", name="vip", api_key="key-siteA-vip")

        plain = gateway.get(f"/admin/api/groups/{g_default}/remote-models").json()["models"]
        vip = gateway.get(f"/admin/api/groups/{g_vip}/remote-models").json()["models"]
        assert set(plain) == {"gpt-test", "claude-test"}
        assert set(vip) == {"gpt-test", "vip-only"}, "分组的模型列表要用它自己的 key 去拉"

        add_route(gateway, "only-vip", g_vip, "gpt-test")
        seen = gateway.post("/v1/responses", json={"model": "only-vip"}).json()
        assert seen["auth"] == "Bearer key-siteA-vip", "转发时必须用命中分组的 key"

        row = wait_for_row(gateway)
        assert (row["upstream"], row["group_name"]) == ("siteA", "vip")


def test_switching_between_two_groups_of_one_provider(gateway):
    """两个分组是平级候选，切换和跨供应商切换没有区别。"""
    with MockUpstream("siteA") as a:
        g_default = add_upstream(gateway, a, "siteA")
        uid = provider_id(gateway, "siteA")
        g_vip = add_group(gateway, uid, "openai", name="vip", api_key="key-siteA-vip")
        add_route(gateway, "shared", g_default, "gpt-test")
        r_vip = add_route(gateway, "shared", g_vip, "gpt-test")

        assert gateway.post("/v1/responses", json={"model": "shared"}).json()["auth"] == "Bearer key-siteA"
        gateway.post("/admin/api/models/switch", json={"route_id": r_vip})
        assert gateway.post("/v1/responses", json={"model": "shared"}).json()["auth"] == "Bearer key-siteA-vip"

        group = next(g for g in gateway.get("/admin/api/models").json() if g["model_name"] == "shared")
        assert group["active_route_id"] == r_vip
        assert {c["group_name"] for c in group["candidates"]} == {"默认", "vip"}


def test_moving_a_group_merges_two_providers(gateway):
    """一开始把同一个站建成了两个供应商，事后把分组搬过去就能合并，候选跟着走。"""
    with MockUpstream("siteA") as a, MockUpstream("siteB") as b:
        add_upstream(gateway, a, "keep")
        g_stray = add_upstream(gateway, b, "stray")
        add_route(gateway, "gpt-test", g_stray)

        keep_id = provider_id(gateway, "keep")
        moved = gateway.put(
            f"/admin/api/groups/{g_stray}",
            json={
                "name": "luna", "protocol": "openai", "api_key": "key-stray",
                "enabled": True, "upstream_id": keep_id,
            },
        )
        assert moved.status_code == 200, moved.text

        upstreams = {u["name"]: u for u in gateway.get("/admin/api/upstreams").json()}
        assert {g["name"] for g in upstreams["keep"]["groups"]} == {"默认", "luna"}
        assert upstreams["stray"]["groups"] == [], "分组搬走后原供应商就空了，可以删掉"

        cand = next(g for g in gateway.get("/admin/api/models").json() if g["model_name"] == "gpt-test")
        assert cand["candidates"][0]["upstream_name"] == "keep"
        assert cand["candidates"][0]["group_name"] == "luna"
        # base_url 在供应商上，所以搬完之后这把 key 就走 keep 的地址了 —— 这正是合并想要的效果
        # （站D / DDD2 两个域名本来就是同一个后端）。反过来说，两边地址不等价就别合。
        assert gateway.post("/v1/responses", json={"model": "gpt-test"}).json()["upstream"] == "siteA"

        assert gateway.delete(f"/admin/api/upstreams/{provider_id(gateway, 'stray')}").status_code == 200


def test_group_can_be_deleted_even_when_it_is_the_last_one(gateway):
    """接口挂在分组上，所以「只剩一个分组」不是特殊状态：删完这个站就是没 key、用不了而已。"""
    with MockUpstream("siteA") as a:
        g_a = add_upstream(gateway, a, "siteA")
        add_route(gateway, "gpt-test", g_a)
        assert gateway.delete(f"/admin/api/groups/{g_a}").status_code == 200

        detail = gateway.get("/admin/api/upstreams").json()[0]
        assert detail["groups"] == [] and detail["supports"] == []
        assert gateway.get("/admin/api/models").json() == [], "候选跟着分组一起走"
        assert gateway.post("/v1/responses", json={"model": "gpt-test"}).status_code == 404


def test_base_url_is_stored_as_a_root_and_v1_is_added_per_protocol(gateway):
    """两种接口的路径都在 /v1 底下，而 Anthropic 客户端给的地址是站根、OpenAI 给的是 …/v1。
    库里统一存站根：粘进来的 /v1 剥掉，转发时按接口补回去。"""
    with MockUpstream("siteA") as a:
        created = gateway.post(
            "/admin/api/upstreams", json={"name": "siteA", "base_url": a.base_url + "/v1/"}
        ).json()
        assert created["base_url"] == a.base_url, "尾部的 /v1 要剥掉，存的是站根"

        uid = int(created["id"])
        # 同名不同接口是允许的：UNIQUE 是 (供应商, 接口, 组名)
        g_oa = add_group(gateway, uid, "openai", api_key="key-siteA")
        g_an = add_group(gateway, uid, "anthropic", api_key="key-siteA")
        add_route(gateway, "gpt-test", g_oa)
        add_route(gateway, "opus", g_an, "claude-opus-4-1")

        assert gateway.post("/v1/responses", json={"model": "gpt-test"}).json()["upstream"] == "siteA"
        assert gateway.post("/v1/messages", json=msg("opus")).json()["upstream"] == "siteA"


def test_model_is_bound_to_one_interface(gateway):
    """模型的接口 = 它候选所在分组的接口。跨接口调是 404 —— 拿 Anthropic 的请求体去打人家的
    /v1/responses 只会得到垃圾。跨接口挂候选是 409，否则「这个名字在哪个接口下」就没答案了。"""
    with MockUpstream("siteA") as a:
        g_an = add_upstream(gateway, a, "siteA", "anthropic")
        uid = provider_id(gateway, "siteA")
        g_oa = add_group(gateway, uid, "openai", name="gpt", api_key="key-siteA")
        add_route(gateway, "opus", g_an, "claude-opus-4-1")

        assert {g["model_name"]: g["protocol"] for g in gateway.get("/admin/api/models").json()} \
            == {"opus": "anthropic"}
        assert gateway.post("/v1/messages", json=msg("opus")).status_code == 200

        wrong = gateway.post("/v1/responses", json={"model": "opus"})
        assert wrong.status_code == 404
        assert "anthropic" in wrong.json()["error"]["message"]

        dup = gateway.post("/admin/api/models", json={"model_name": "opus", "group_id": g_oa})
        assert dup.status_code == 409 and "接口" in dup.json()["detail"]
        assert "opus" in dup.json()["detail"], "得说清是哪个模型名撞了，一批几十个时才找得到"

        bad = gateway.post(f"/admin/api/upstreams/{uid}/groups", json={"name": "x", "protocol": "nope"})
        assert bad.status_code == 400


def test_bulk_add_skips_cross_interface_names_instead_of_failing(gateway):
    """拉一个站的模型列表动辄几十上百个，里面撞上一两个「已经在另一种接口下暴露」的名字，
    整批退回去、一个都不落库是最难用的结果。跳过它们，并且说清跳了哪些。"""
    with MockUpstream("siteA") as a:
        g_an = add_upstream(gateway, a, "siteA", "anthropic")
        g_oa = add_group(gateway, provider_id(gateway, "siteA"), "openai", name="gpt", api_key="k")
        add_route(gateway, "claude-test", g_an, "claude-test")

        # claude-test 已经在 anthropic 下了，gpt-test 是干净的
        resp = gateway.post(
            "/admin/api/models/bulk-add",
            json={"group_id": g_oa, "model_names": ["gpt-test", "claude-test"]},
        )
        assert resp.status_code == 200, resp.text
        assert resp.json() == {"added": 1, "skipped": ["claude-test"]}

        routes = {g["model_name"]: g["protocol"] for g in gateway.get("/admin/api/models").json()}
        assert routes == {"claude-test": "anthropic", "gpt-test": "openai"}


def test_group_protocol_decides_the_pull_auth_headers(gateway):
    """Anthropic 站的 /v1/models 认 x-api-key + anthropic-version，只发 Bearer 多半是 401。"""
    with MockUpstream("siteA") as a:
        g_oa = add_upstream(gateway, a, "siteA", "openai")
        g_an = add_group(gateway, provider_id(gateway, "siteA"), "anthropic", api_key="key-siteA")

        plain = gateway.get(f"/admin/api/groups/{g_oa}/remote-models").json()["models"]
        claude = gateway.get(f"/admin/api/groups/{g_an}/remote-models").json()["models"]
        assert set(plain) == {"gpt-test", "claude-test"}
        assert set(claude) == {"claude-test", "claude-haiku-test"}, "mock 只在两个头都带上时才回这个"


def test_models_list_can_be_filtered_by_the_anthropic_version_header(gateway):
    """一个 /v1/models 服务两种客户端。带 anthropic-version 的（Claude Code 就带）
    只该看到它调得动的那些，认不出来的给全部。"""
    with MockUpstream("siteA") as a:
        g_an = add_upstream(gateway, a, "siteA", "anthropic")
        g_oa = add_group(gateway, provider_id(gateway, "siteA"), "openai", name="gpt", api_key="key-siteA")
        add_route(gateway, "opus", g_an, "claude-opus-4-1")
        add_route(gateway, "gpt-test", g_oa)

        every = {m["id"] for m in gateway.get("/v1/models").json()["data"]}
        assert every == {"opus", "gpt-test"}
        claude = gateway.get("/v1/models", headers={"anthropic-version": "2023-06-01"}).json()
        assert {m["id"] for m in claude["data"]} == {"opus"}


def test_candidate_remote_name_and_1m_can_be_edited(gateway):
    """1M 开关就是 remote_model 上的 [1m] 后缀，改候选走 PUT（原来只有档位弹窗能设）。"""
    with MockUpstream("siteA") as a:
        g_a = add_upstream(gateway, a, "siteA", "anthropic")
        rid = add_route(gateway, "opus", g_a, "claude-opus-4-1")
        assert "context-1m" not in gateway.post("/v1/messages", json=msg("opus")).json()["beta"]

        r = gateway.put(
            "/admin/api/models",
            json={"route_id": rid, "remote_model": "claude-opus-4-5[1m]"},
        )
        assert r.status_code == 200, r.text

        seen = gateway.post("/v1/messages", json=msg("opus")).json()
        assert seen["model"] == "claude-opus-4-5", "后缀只用来推断意图，绝不传给上游"
        assert "context-1m-2025-08-07" in seen["beta"]

        assert gateway.put(
            "/admin/api/models", json={"route_id": 9999, "remote_model": "x"}
        ).status_code == 404


def test_pull_failure_says_which_url_it_tried(gateway):
    """公益站三天两头连不上，而 httpx 的 DNS / 连接错误 str() 常常是空的 ——
    只回一句「拉取失败:」没法排查，至少得说清打的是哪个地址。"""
    created = gateway.post(
        "/admin/api/upstreams", json={"name": "dead", "base_url": "http://127.0.0.1:1"}
    ).json()
    gid = add_group(gateway, int(created["id"]), "anthropic")

    resp = gateway.get(f"/admin/api/groups/{gid}/remote-models")
    assert resp.status_code == 502
    detail = resp.json()["detail"]
    assert "http://127.0.0.1:1/v1/models" in detail, detail
    assert detail.strip() != "拉取失败:", "异常消息为空时也得留点线索"


def test_group_protocol_is_locked_once_it_has_candidates(gateway):
    """改接口等于把已录入的模型悄悄换成另一种线格式，有候选就不给改。"""
    with MockUpstream("siteA") as a:
        g_a = add_upstream(gateway, a, "siteA", "openai")
        add_route(gateway, "gpt-test", g_a)

        locked = gateway.put(f"/admin/api/groups/{g_a}", json={
            "name": "默认", "protocol": "anthropic", "api_key": "key-siteA", "enabled": True})
        assert locked.status_code == 409 and "接口" in locked.json()["detail"]

        # 名字和 key 照样能改
        assert gateway.put(f"/admin/api/groups/{g_a}", json={
            "name": "renamed", "protocol": "openai", "api_key": "k2", "enabled": True}).status_code == 200


def test_cloning_a_group_copies_the_key_to_the_other_interface(gateway):
    """一把 key 两种接口都能用的站不少，而接口是分组的属性，手动再填一遍 key 很烦。"""
    with MockUpstream("siteA") as a:
        g_a = add_upstream(gateway, a, "siteA", "openai")
        clone = gateway.post(f"/admin/api/groups/{g_a}/clone")
        assert clone.status_code == 200, clone.text
        assert (clone.json()["protocol"], clone.json()["api_key"]) == ("anthropic", "key-siteA")

        detail = gateway.get("/admin/api/upstreams").json()[0]
        assert detail["supports"] == ["anthropic", "openai"]
        assert {g["name"] for g in detail["groups"]} == {"默认"}, "同名不同接口"


_PRE_GROUP_SCHEMA = """
CREATE TABLE upstreams(
  id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL UNIQUE, base_url TEXT NOT NULL,
  api_key TEXT NOT NULL DEFAULT '', enabled INTEGER NOT NULL DEFAULT 1,
  created_at TEXT NOT NULL DEFAULT (datetime('now','localtime')),
  header_override TEXT NOT NULL DEFAULT '');
CREATE TABLE model_routes(
  model_name TEXT NOT NULL,
  upstream_id INTEGER NOT NULL REFERENCES upstreams(id) ON DELETE CASCADE,
  remote_model TEXT NOT NULL, is_active INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY(model_name, upstream_id));
CREATE TABLE request_log(
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, client TEXT NOT NULL, model TEXT NOT NULL,
  upstream TEXT NOT NULL, status INTEGER NOT NULL, stream INTEGER NOT NULL,
  req_bytes INTEGER NOT NULL, resp_bytes INTEGER NOT NULL, duration_ms INTEGER NOT NULL,
  input_tokens INTEGER, output_tokens INTEGER, cached_tokens INTEGER,
  note TEXT NOT NULL DEFAULT '');
INSERT INTO upstreams(name, base_url, api_key) VALUES
  ('siteA', 'https://a.example/v1', 'sk-aaa'), ('siteB', 'https://b.example/v1', 'sk-bbb');
INSERT INTO model_routes(model_name, upstream_id, remote_model, is_active) VALUES
  ('m1', 1, 'remote-1', 1), ('m1', 2, 'remote-1b', 0), ('m2', 2, 'remote-2', 1);
INSERT INTO request_log(client, model, upstream, status, stream, req_bytes, resp_bytes, duration_ms, note)
  VALUES ('codex', 'm1', 'siteA', 200, 0, 10, 20, 30, 'ok');
"""


def test_migration_from_pre_group_schema(tmp_path, monkeypatch):
    """老库（api_key 挂在上游行上）原地升级成供应商 / 分组结构，候选和历史记录一条不少。"""
    import sqlite3

    from gateway import config, db

    data_dir = tmp_path / "data"
    data_dir.mkdir()
    db_path = data_dir / "gateway.db"
    monkeypatch.setattr(config, "DATA_DIR", data_dir)
    monkeypatch.setattr(config, "DB_PATH", db_path)

    old = sqlite3.connect(db_path)
    old.executescript(_PRE_GROUP_SCHEMA)
    old.commit()
    old.close()

    assert db._is_pre_group_shape(db_path)
    db.init_db()
    assert not db._is_pre_group_shape(db_path)
    assert list(data_dir.glob("gateway.db.bak-*")), "迁移前必须留一份备份"

    groups = {(g.upstream_id, g.name): g for g in db.list_groups()}
    assert set(groups) == {(1, "默认"), (2, "默认")}
    assert groups[(1, "默认")].api_key == "sk-aaa"
    assert groups[(2, "默认")].api_key == "sk-bbb"
    for group in groups.values():
        assert group.protocol == "openai", "今天之前只有 /v1/responses，历史配置就是 openai 接口"
    for upstream in db.list_upstreams():
        assert not upstream.base_url.endswith("/v1"), "base_url 统一存站根"

    rows = db.list_routes()
    assert {(r["model_name"], r["upstream_name"], r["remote_model"], r["protocol"]) for r in rows} == {
        ("m1", "siteA", "remote-1", "openai"),
        ("m1", "siteB", "remote-1b", "openai"),
        ("m2", "siteB", "remote-2", "openai"),
    }

    route = db.resolve_route("m1", "openai")
    assert route.upstream.name == "siteA" and route.upstream.api_key == "sk-aaa"
    assert (route.group_name, route.remote_model) == ("默认", "remote-1")
    assert route.upstream.base_url == "https://a.example"
    assert db.resolve_route("m1", "anthropic") is None, "接口参与匹配"

    assert db.request_stats()["requests"] == 1, "历史转发记录不能丢"
    assert db.recent_requests(1)[0]["protocol"] == "openai", "老记录的协议列要回填"

    db.init_db()   # 再跑一遍不能出事，也不能又建一遍分组
    assert len(db.list_groups()) == 2


_PRE_PROTOCOL_SCHEMA = """
CREATE TABLE upstreams(
  id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL UNIQUE, base_url TEXT NOT NULL,
  enabled INTEGER NOT NULL DEFAULT 1, header_override TEXT NOT NULL DEFAULT '',
  protocols TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL DEFAULT (datetime('now','localtime')));
CREATE TABLE upstream_groups(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  upstream_id INTEGER NOT NULL REFERENCES upstreams(id) ON DELETE CASCADE,
  name TEXT NOT NULL, api_key TEXT NOT NULL DEFAULT '', enabled INTEGER NOT NULL DEFAULT 1,
  created_at TEXT NOT NULL DEFAULT (datetime('now','localtime')),
  UNIQUE(upstream_id, name));
CREATE TABLE model_routes(
  model_name TEXT NOT NULL,
  group_id INTEGER NOT NULL REFERENCES upstream_groups(id) ON DELETE CASCADE,
  remote_model TEXT NOT NULL, is_active INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY(model_name, group_id));
CREATE TABLE model_meta(model_name TEXT PRIMARY KEY, side TEXT NOT NULL DEFAULT '');
CREATE TABLE request_log(
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, client TEXT NOT NULL, model TEXT NOT NULL,
  remote_model TEXT NOT NULL DEFAULT '', protocol TEXT NOT NULL DEFAULT '',
  upstream TEXT NOT NULL, group_name TEXT NOT NULL DEFAULT '', status INTEGER NOT NULL,
  stream INTEGER NOT NULL, req_bytes INTEGER NOT NULL, resp_bytes INTEGER NOT NULL,
  duration_ms INTEGER NOT NULL, input_tokens INTEGER, output_tokens INTEGER,
  cached_tokens INTEGER, note TEXT NOT NULL DEFAULT '');
INSERT INTO upstreams(name, base_url, protocols) VALUES
  ('gpt-site', 'https://a.example/v1', 'openai'),
  ('claude-site', 'https://b.example', 'anthropic'),
  ('both', 'https://c.example/v1', 'openai,anthropic');
INSERT INTO upstream_groups(upstream_id, name, api_key) VALUES
  (1, '默认', 'sk-aaa'), (1, 'luna', 'sk-luna'), (2, '默认', 'sk-bbb'), (3, '默认', 'sk-ccc');
INSERT INTO model_routes(model_name, group_id, remote_model, is_active) VALUES
  ('m1', 1, 'remote-1', 1), ('m1', 2, 'remote-1b', 0), ('m2', 4, 'remote-2', 1);
INSERT INTO model_meta(model_name, side) VALUES ('m1', 'openai'), ('m2', 'openai');
INSERT INTO request_log(client, model, upstream, status, stream, req_bytes, resp_bytes, duration_ms, note)
  VALUES ('codex', 'm1', 'gpt-site', 200, 0, 10, 20, 30, 'ok');
"""


def test_migration_moves_the_protocol_mark_onto_groups(tmp_path, monkeypatch):
    """上一版结构：接口标记挂在 upstreams.protocols 上、base_url 填到 /v1。
    迁移要把接口搬到分组上、把 base_url 收成站根，候选和历史记录一条不少。"""
    import sqlite3

    from gateway import config, db

    data_dir = tmp_path / "data"
    data_dir.mkdir()
    db_path = data_dir / "gateway.db"
    monkeypatch.setattr(config, "DATA_DIR", data_dir)
    monkeypatch.setattr(config, "DB_PATH", db_path)

    old = sqlite3.connect(db_path)
    old.executescript(_PRE_PROTOCOL_SCHEMA)
    old.commit()
    old.close()

    assert db._is_pre_protocol_shape(db_path)
    db.init_db()
    assert not db._is_pre_protocol_shape(db_path)
    assert list(data_dir.glob("gateway.db.bak-*")), "迁移前必须留一份备份"

    assert {u.name: u.base_url for u in db.list_upstreams()} == {
        "gpt-site": "https://a.example",
        "claude-site": "https://b.example",
        "both": "https://c.example",
    }
    assert "protocols" not in db._columns(db_path, "upstreams"), "接口标记已经挪到分组上了"
    assert not db._columns(db_path, "model_meta"), "模型的接口由候选推出来，不再单独存"

    by_upstream = {u.id: u.name for u in db.list_upstreams()}
    got = {(by_upstream[g.upstream_id], g.name, g.protocol, g.api_key) for g in db.list_groups()}
    assert got == {
        ("gpt-site", "默认", "openai", "sk-aaa"),
        ("gpt-site", "luna", "openai", "sk-luna"),
        ("claude-site", "默认", "anthropic", "sk-bbb"),
        # 两种格式都标了的站：候选留在 openai 那个分组上（历史流量就是 /v1/responses），
        # 另一种接口留一个同 key 的空分组，别把填过的信息弄丢
        ("both", "默认", "openai", "sk-ccc"),
        ("both", "默认", "anthropic", "sk-ccc"),
    }

    rows = db.list_routes()
    assert {(r["model_name"], r["group_name"], r["remote_model"], r["protocol"]) for r in rows} == {
        ("m1", "默认", "remote-1", "openai"),
        ("m1", "luna", "remote-1b", "openai"),
        ("m2", "默认", "remote-2", "openai"),
    }
    route = db.resolve_route("m1", "openai")
    assert (route.upstream.name, route.group_name, route.upstream.api_key) == ("gpt-site", "默认", "sk-aaa")
    assert db.request_stats()["requests"] == 1
    assert db.recent_requests(1)[0]["protocol"] == "openai", "老记录的协议列要回填"

    # 老库的候选是复合主键，顺带升级成自增 id：一个分组下才塞得下第二条映射
    assert not db._is_pre_routeid_shape(db_path)
    assert all(r["route_id"] for r in rows), "每条候选都该有自己的 id"
    first = next(r for r in rows if r["remote_model"] == "remote-1")
    assert db.add_model_route("m1", first["group_id"], "remote-1-alt"), "同分组换个真名能再加一条"
    assert db.add_model_route("m1", first["group_id"], "remote-1") == 0, "一模一样的还是重复"

    db.init_db()   # 幂等
    assert len(db.list_groups()) == 5
    assert len(list(data_dir.glob("gateway.db.bak-*"))) == 1


# ================================================================ 自动降级
#
# 只在 Anthropic 接口默认开着：那边全是坏得勤的中转站。OpenAI 那边除了一个公益站
# 都是花钱买稳定的，换站要人点头，所以默认关闭 —— 见 test_failover_is_off_on_openai。


def two_anthropic_sites(gateway, a, b, remote_a="claude-opus-4-1", remote_b="claude-opus-4-5"):
    """两个站各一个 anthropic 分组，同一个模型两条候选（a 是活跃的那条）。"""
    g_a = add_upstream(gateway, a, "siteA", "anthropic")
    g_b = add_upstream(gateway, b, "siteB", "anthropic")
    add_route(gateway, "opus", g_a, remote_a)
    add_route(gateway, "opus", g_b, remote_b)
    return g_a, g_b


def test_failover_moves_to_the_next_candidate(gateway):
    """第一个候选打不通就换下一个，客户端不该看见这件事。

    每个候选的**上游真名和 key 都可能不一样**（真库里 claude-sonnet-5 在一个站叫
    claude-sonnet-5、在另一个站映射到 claude-opus-5），所以请求体和请求头都得按候选重建。
    """
    with MockUpstream("siteA") as a, MockUpstream("siteB") as b:
        two_anthropic_sites(gateway, a, b)
        a.fail_with(503)

        resp = gateway.post("/v1/messages", json=msg("opus"))
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["upstream"] == "siteB"
        assert body["model"] == "claude-opus-4-5", "换站之后请求体里的 model 要跟着换"
        assert body["x_api_key"] == "key-siteB", "key 也得是新候选那把"

        rows = wait_rows(gateway, 2)
        assert [(r["upstream"], r["status"], r["attempt"], r["note"]) for r in rows] == [
            ("siteA", 503, 1, "failed_over"),
            ("siteB", 200, 2, "ok"),
        ], "被降级掉的那次失败也要留痕：客户端没看见，但这次调用是真花了钱的"
        assert gateway.get("/admin/api/stats").json()["saved"] == 1

def test_failover_works_for_streams(gateway):
    """流式也能降级：状态码已经拿到、还没往下游发过任何字节，这是唯一安全的落点。"""
    with MockUpstream("siteA") as a, MockUpstream("siteB") as b:
        two_anthropic_sites(gateway, a, b)
        a.fail_with(503)

        with gateway.stream("POST", "/v1/messages", json=msg("opus", stream=True)) as stream:
            assert stream.status_code == 200
            raw = "".join(stream.iter_text())
        events = parse_sse_events(raw)
        assert events[0]["type"] == "message_start", "不能把坏站的半截响应混进来"
        assert events[-1]["type"] == "message_stop"
        assert events[0]["message"]["model"] == "claude-opus-4-5"


def test_failover_is_off_on_openai(gateway):
    """GPT 那边默认不自动换站，行为和加这个功能之前一字不差；打开开关才降级。"""
    with MockUpstream("siteA") as a, MockUpstream("siteB") as b:
        g_a = add_upstream(gateway, a, "siteA")
        g_b = add_upstream(gateway, b, "siteB")
        add_route(gateway, "gpt-test", g_a)
        add_route(gateway, "gpt-test", g_b)
        a.fail_with(503)

        assert gateway.get("/admin/api/failover").json()["enabled"] == {
            "anthropic": True, "openai": False,
        }
        resp = gateway.post("/v1/responses", json={"model": "gpt-test"})
        assert resp.status_code == 503, "没开降级就该把上游的 503 原样透传下去"
        rows = wait_rows(gateway, 1)
        assert len(rows) == 1 and rows[0]["upstream"] == "siteA"

        gateway.post("/admin/api/failover", json={"protocol": "openai", "enabled": True})
        again = gateway.post("/v1/responses", json={"model": "gpt-test"})
        assert again.status_code == 200 and again.json()["upstream"] == "siteB"


def test_failover_skips_a_bad_request(gateway):
    """400 换个站也是同样的答案，重试只是白花一次调用。"""
    with MockUpstream("siteA") as a, MockUpstream("siteB") as b:
        two_anthropic_sites(gateway, a, b)
        a.fail_with(400)

        resp = gateway.post("/v1/messages", json=msg("opus"))
        assert resp.status_code == 400
        rows = wait_rows(gateway, 1)
        assert len(rows) == 1 and rows[0]["upstream"] == "siteA"

def test_failover_gives_up_after_three_attempts(gateway):
    """全都坏的时候要有个头：打三个就把最后那个的响应还给客户端，别把 40 万 token 重发五遍。"""
    from gateway import failover

    with MockUpstream("s1") as s1, MockUpstream("s2") as s2, \
            MockUpstream("s3") as s3, MockUpstream("s4") as s4:
        for i, site in enumerate((s1, s2, s3, s4), start=1):
            gid = add_upstream(gateway, site, site.name, "anthropic")
            add_route(gateway, "opus", gid, f"remote-{i}")
            site.fail_with(503)

        resp = gateway.post("/v1/messages", json=msg("opus"))
        assert resp.status_code == 503
        rows = wait_rows(gateway, failover.MAX_ATTEMPTS)
        assert [r["upstream"] for r in rows] == ["s1", "s2", "s3"]
        assert [r["attempt"] for r in rows] == [1, 2, 3]
        # 最后那次是原样透传下去的，不算「被降级接住」
        assert rows[-1]["note"] != "failed_over"


def test_breaker_stops_paying_for_a_dead_site(gateway):
    """真库里 站A 连续失败过 60 次、平均每次白等 16.6 秒 —— 连着坏就得躲开它，
    否则每个请求都要重新交一遍学费。"""
    with MockUpstream("siteA") as a, MockUpstream("siteB") as b:
        g_a, _ = two_anthropic_sites(gateway, a, b)
        a.fail_with(503)

        for _ in range(2):
            assert gateway.post("/v1/messages", json=msg("opus")).json()["upstream"] == "siteB"

        breakers = gateway.get("/admin/api/failover").json()["breakers"]
        assert [(x["group_id"], x["fails"], x["cooling_ms"] > 0) for x in breakers] == [(g_a, 2, True)]

        hits = rows_on(gateway, "siteA")
        assert gateway.post("/v1/messages", json=msg("opus")).json()["upstream"] == "siteB"
        assert rows_on(gateway, "siteA") == hits, "冷却期内根本不该再打它"


def test_manual_switch_clears_the_cooldown(gateway):
    """手动点了那个圆片就是明确指定，之前的连续失败不该继续把它挡在外面。"""
    with MockUpstream("siteA") as a, MockUpstream("siteB") as b:
        g_a, _ = two_anthropic_sites(gateway, a, b)
        a.fail_with(503)
        for _ in range(2):
            gateway.post("/v1/messages", json=msg("opus"))
        assert gateway.get("/admin/api/failover").json()["breakers"], "先让它进冷却"

        assert gateway.post(
            "/admin/api/models/switch", json={"route_id": route_id(gateway, "opus", g_a)}
        ).json() == {"ok": True}
        assert gateway.get("/admin/api/failover").json()["breakers"] == []

        hits = rows_on(gateway, "siteA")
        assert gateway.post("/v1/messages", json=msg("opus")).json()["upstream"] == "siteB"
        assert rows_on(gateway, "siteA") == hits + 1, "清了冷却就该重新试它一次"

def test_route_order_decides_who_is_tried_next(gateway):
    """圆片从左到右就是尝试顺序，站H 这种最稳的排最后当保底。"""
    with MockUpstream("siteA") as a, MockUpstream("siteB") as b, MockUpstream("siteC") as c:
        g_a, g_b = two_anthropic_sites(gateway, a, b)
        g_c = add_upstream(gateway, c, "siteC", "anthropic")
        add_route(gateway, "opus", g_c, "claude-opus-4-9")

        order = [route_id(gateway, "opus", gid) for gid in (g_a, g_c, g_b)]
        ordered = gateway.post(
            "/admin/api/models/order", json={"model_name": "opus", "order": order}
        )
        assert ordered.json() == {"ok": True, "ordered": 3}
        row = next(r for r in gateway.get("/admin/api/models").json() if r["model_name"] == "opus")
        assert [cand["group_id"] for cand in row["candidates"]] == [g_a, g_c, g_b]

        a.fail_with(503)
        b.fail_with(503)
        resp = gateway.post("/v1/messages", json=msg("opus"))
        assert resp.status_code == 200 and resp.json()["upstream"] == "siteC"
        rows = wait_rows(gateway, 2)
        assert [r["upstream"] for r in rows] == ["siteA", "siteC"], "第二个该按顺序轮到 C 而不是 B"


def test_stateful_openai_request_is_not_failed_over(gateway):
    """带 store / previous_response_id 的请求，上游可能已经存下来了才失败，重试会留下两条。"""
    with MockUpstream("siteA") as a, MockUpstream("siteB") as b:
        g_a = add_upstream(gateway, a, "siteA")
        g_b = add_upstream(gateway, b, "siteB")
        add_route(gateway, "gpt-test", g_a)
        add_route(gateway, "gpt-test", g_b)
        gateway.post("/admin/api/failover", json={"protocol": "openai", "enabled": True})
        a.fail_with(503)

        resp = gateway.post("/v1/responses", json={"model": "gpt-test", "store": True})
        assert resp.status_code == 503
        rows = wait_rows(gateway, 1)
        assert len(rows) == 1 and rows[0]["upstream"] == "siteA"


# ================================================================ 同一分组下的多条映射
#
# 一个分组 = 一个站的一把 key，它下面常常有好几个能用的模型 id。以前候选的主键是
# (模型名, 分组)，一个分组只塞得下一条；现在按 route_id 指，同分组可以挂好几条，
# 各指一个不同的上游真名。


def test_one_group_can_host_several_remote_names(gateway):
    """同一把 key 下的两个模型 id 是两条平级候选，只有「连真名都一样」才算重复。"""
    with MockUpstream("siteA") as a:
        g_a = add_upstream(gateway, a, "siteA", "anthropic")
        r1 = add_route(gateway, "opus", g_a, "claude-opus-4-1")
        r2 = add_route(gateway, "opus", g_a, "claude-opus-4-5")
        assert r1 != r2

        row = next(r for r in gateway.get("/admin/api/models").json() if r["model_name"] == "opus")
        assert [(c["group_id"], c["remote_model"]) for c in row["candidates"]] == [
            (g_a, "claude-opus-4-1"), (g_a, "claude-opus-4-5"),
        ]
        assert row["active_route_id"] == r1

        dup = gateway.post(
            "/admin/api/models",
            json={"model_name": "opus", "group_id": g_a, "remote_model": "claude-opus-4-1"},
        )
        assert dup.status_code == 409
        assert "claude-opus-4-1" in dup.json()["detail"], "得说清是跟哪条重复了"

        # 手动切到同分组的第二条：这是以前根本表达不出来的操作
        gateway.post("/admin/api/models/switch", json={"route_id": r2})
        assert gateway.post("/v1/messages", json=msg("opus")).json()["model"] == "claude-opus-4-5"


def test_model_level_404_tries_the_sibling_in_the_same_group(gateway):
    """上游把某个模型 id 下掉了（404）时，同一把 key 的另一个真名还有机会。

    404 不算这个分组的锅：站是通的、key 是好的，只是这个名字没了，所以不进冷却。
    """
    with MockUpstream("siteA") as a:
        g_a = add_upstream(gateway, a, "siteA", "anthropic")
        add_route(gateway, "opus", g_a, "claude-opus-4-1-20250805")
        add_route(gateway, "opus", g_a, "claude-opus-4-1")
        a.drop_model("claude-opus-4-1-20250805")

        resp = gateway.post("/v1/messages", json=msg("opus"))
        assert resp.status_code == 200, resp.text
        assert resp.json()["model"] == "claude-opus-4-1"

        rows = wait_rows(gateway, 2)
        assert [(r["remote_model"], r["status"], r["attempt"]) for r in rows] == [
            ("claude-opus-4-1-20250805", 404, 1),
            ("claude-opus-4-1", 200, 2),
        ]
        assert gateway.get("/admin/api/failover").json()["breakers"] == [], \
            "模型名没了不该让整把 key 进冷却"


def test_site_level_failure_skips_the_rest_of_that_group(gateway):
    """站级失败（503）时同分组的兄弟候选一起跳掉 —— 站都连不上，换个模型名没用。"""
    with MockUpstream("siteA") as a, MockUpstream("siteB") as b:
        g_a = add_upstream(gateway, a, "siteA", "anthropic")
        g_b = add_upstream(gateway, b, "siteB", "anthropic")
        add_route(gateway, "opus", g_a, "opus-a1")
        add_route(gateway, "opus", g_a, "opus-a2")
        add_route(gateway, "opus", g_b, "opus-b")
        a.fail_with(503)

        resp = gateway.post("/v1/messages", json=msg("opus"))
        assert resp.status_code == 200 and resp.json()["upstream"] == "siteB"
        rows = wait_rows(gateway, 2)
        assert [(r["upstream"], r["attempt"]) for r in rows] == [("siteA", 1), ("siteB", 2)]
        assert rows_on(gateway, "siteA") == 1, "同一个站不该在一次请求里被打两遍"


def test_editing_a_candidate_into_a_duplicate_is_409(gateway):
    """改真名改成同分组里另一条已经用着的名字，那两条就完全一样了。"""
    with MockUpstream("siteA") as a:
        g_a = add_upstream(gateway, a, "siteA", "anthropic")
        add_route(gateway, "opus", g_a, "claude-opus-4-1")
        r2 = add_route(gateway, "opus", g_a, "claude-opus-4-5")

        clash = gateway.put(
            "/admin/api/models", json={"route_id": r2, "remote_model": "claude-opus-4-1"}
        )
        assert clash.status_code == 409 and "claude-opus-4-1" in clash.json()["detail"]
        assert len(cands(gateway, "opus")) == 2, "冲突的改动不能落库"


def test_unchecking_a_model_removes_every_mapping_in_that_group(gateway):
    """分组弹窗里的勾选框答的是「这个模型在这个分组里有没有」，所以取消勾选
    （group_id 那种删法）要把同分组的几条映射一起去掉。"""
    with MockUpstream("siteA") as a, MockUpstream("siteB") as b:
        g_a = add_upstream(gateway, a, "siteA", "anthropic")
        g_b = add_upstream(gateway, b, "siteB", "anthropic")
        add_route(gateway, "opus", g_a, "opus-a1")
        add_route(gateway, "opus", g_a, "opus-a2")
        r_b = add_route(gateway, "opus", g_b, "opus-b")

        resp = gateway.delete("/admin/api/models", params={"model_name": "opus", "group_id": g_a})
        assert resp.json() == {"ok": True, "removed": 2}
        assert [c["route_id"] for c in cands(gateway, "opus")] == [r_b]
        # 活跃的那条被删了，流量自动落到剩下的候选上
        assert gateway.post("/v1/messages", json=msg("opus")).json()["upstream"] == "siteB"


# ================================================================ 实时请求
#
# /admin/api/inflight 是「实时」那一页的全部数据来源：谁在跑、打的是哪个上游、
# 什么阶段、前面被谁拒过。纯内存，不碰数据库。


def test_inflight_lists_the_running_request(gateway):
    """这页要能回答：现在在打哪个上游、什么阶段、等了多久、下游是谁。"""
    with MockUpstream("siteA") as a:
        g_a = add_upstream(gateway, a, "siteA")
        add_route(gateway, "gpt-test", g_a, "gpt-remote")

        seen = None
        with gateway.stream(
            "POST", "/v1/responses", json={"model": "gpt-test", "stream": True},
            headers={"User-Agent": "codex_cli_rs/1.0"},
        ) as stream:
            for chunk in stream.iter_text():
                if chunk.strip() and seen is None:
                    seen = gateway.get("/admin/api/inflight").json()

        assert seen is not None and len(seen["calls"]) == 1, seen
        call = seen["calls"][0]
        assert (call["model"], call["remote_model"]) == ("gpt-test", "gpt-remote")
        assert (call["upstream"], call["group_name"]) == ("siteA", "默认")
        assert call["phase"] == "stream", "第一块字节已经在往下游走了"
        assert call["client"] == "Codex CLI" and call["stream"] is True
        assert call["meta"] is False and call["trail"] == []
        assert seen["counts"] == {"requests": 1, "streams": 1}
        assert seen["failover"] == {"anthropic": True, "openai": False}

        # 流走完就转进「刚结束」，再留 90 秒 —— 不然降级轨迹只在活着的那几秒里存在
        after = wait_inflight(gateway, lambda d: not d["calls"])
        assert [c["model"] for c in after["recent"]] == ["gpt-test"]
        assert after["recent"][0]["note"] == "ok"
        assert after["counts"] == {"requests": 0, "streams": 0}


def test_inflight_keeps_the_failover_trail(gateway):
    """降级最怕的是把问题藏起来：胜出的那条要带着「前面被谁拒了」。"""
    with MockUpstream("siteA") as a, MockUpstream("siteB") as b:
        g_a, _ = two_anthropic_sites(gateway, a, b)
        a.fail_with(503)

        assert gateway.post("/v1/messages", json=msg("opus")).status_code == 200
        data = wait_inflight(gateway, lambda d: d["recent"] and not d["calls"])
        call = data["recent"][0]
        assert (call["upstream"], call["attempt"], call["status"]) == ("siteB", 2, 200)
        assert [(t["upstream"], t["status"], t["note"]) for t in call["trail"]] == [
            ("siteA", 503, "failed_over")
        ]
        # 分组健康板的数据也是这个接口给的
        assert [(b["group_id"], b["fails"]) for b in data["breakers"]] == [(g_a, 1)]


def test_count_tokens_is_listed_but_not_counted(gateway):
    """count_tokens 会走降级、会踩断路器，所以列出来；但它又多又快，
    计进「进行中」就没法当忙闲指示看了。"""
    with MockUpstream("siteA") as a:
        g_a = add_upstream(gateway, a, "siteA", "anthropic")
        add_route(gateway, "opus", g_a, "claude-opus-4-1")

        assert gateway.post("/v1/messages/count_tokens", json=msg("opus")).status_code == 200
        data = wait_inflight(gateway, lambda d: bool(d["recent"]))
        assert [c["meta"] for c in data["recent"]] == [True]
        assert data["counts"]["requests"] == 0
        assert gateway.get("/admin/api/requests").json() == [], "照旧不进转发记录"


def test_inflight_registers_nothing_for_an_unconfigured_model(gateway):
    """连上游都没碰过的 404 不该出现在这页上 —— 它没在打任何站。"""
    assert gateway.post("/v1/responses", json={"model": "nope"}).status_code == 404
    data = gateway.get("/admin/api/inflight").json()
    assert (data["calls"], data["recent"]) == ([], [])




