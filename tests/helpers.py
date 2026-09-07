"""端到端测试的脚手架：mock 上游、网关进程的起停、以及各用例共用的那些断言帮手。

真起两个 mock 上游、真起一个网关线程，客户端一律 trust_env=False —— 否则系统代理会把
回环请求也接走，`connect_failed` 之类的断言会拿到代理返回的 502。

以前这些和一个 2000 行的测试文件堆在一起。拆开之后每个测试文件里只剩断言，找
「自动降级那条链是怎么验的」不用再翻半个文件。
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import socket
import ssl
import threading
import time
from pathlib import Path

import httpx
import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse

# 测试专用的自签证书（fixture 目录里那对，十年都不带过期的）：
# MockProxy 套上它就是 https 代理，#ca= 钉的就是这张
PROXY_CERT = str(Path(__file__).parent / "fixture" / "proxy-cert.pem")
PROXY_KEY = str(Path(__file__).parent / "fixture" / "proxy-key.pem")


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
            # bulk：撑爆 64KB 尾部窗口；slow：每块之间歇一下，
            # 好让「这条流还在跑」这件事在测试里抓得住
            deltas, pause = {"bulk": (BULK_DELTAS, 0.0), "slow": (6, 0.05)}.get(mode, (2, 0.0))

            async def gen():
                yield sse("message_start", {
                    "type": "message_start",
                    "message": {
                        "id": "msg_1", "model": body.get("model", ""),
                        "usage": {"input_tokens": 1234, "cache_creation_input_tokens": 0,
                                  "cache_read_input_tokens": 900, "output_tokens": 1},
                    },
                })
                for _ in range(deltas):
                    await asyncio.sleep(pause)
                    yield sse("content_block_delta", {
                        "type": "content_block_delta", "index": 0,
                        "delta": {"type": "text_delta", "text": DELTA_TEXT},
                    })
                if mode == "thinking":
                    # 思维链：发来的是总结过的，但计费按完整的算
                    yield sse("content_block_delta", {
                        "type": "content_block_delta", "index": 1,
                        "delta": {"type": "thinking_delta", "thinking": "想了很久" * 20},
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


class MockProxy:
    """一个最小的 HTTP 代理：读请求行里的绝对 URL，连过去，然后双向对拷。

    为什么要真写一个：「出口」这件事只有「字节真的从那扇门出去了」才算验过 ——
    光测「填个死端口会失败」证明不了流量走的是代理而不是直连。
    只支持明文 HTTP（mock 上游都是 http://），所以不用管 CONNECT。
    传入证书就是 https 代理（TLS 在门口，进去之后照旧明文），用来验 #ca 的证书固定。
    """

    def __init__(self, certfile: str | None = None, keyfile: str | None = None) -> None:
        self.port = free_port()
        self.url = f"http://127.0.0.1:{self.port}"
        self.seen: list[str] = []           # 经过它的那些绝对 URL
        self._tls: ssl.SSLContext | None = None
        if certfile and keyfile:
            self._tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            self._tls.load_cert_chain(certfile, keyfile)
        self._loop: asyncio.AbstractEventLoop | None = None
        self._stop: asyncio.Event | None = None
        self._ready = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)

    async def _pump(self, reader, writer) -> None:
        with contextlib.suppress(Exception):
            while data := await reader.read(65536):
                writer.write(data)
                await writer.drain()
        with contextlib.suppress(Exception):
            writer.close()

    async def _handle(self, reader, writer) -> None:
        try:
            head = await reader.readuntil(b"\r\n\r\n")
            line, rest = head.split(b"\r\n", 1)
            method, target, version = line.split(b" ")
            self.seen.append(target.decode())
            url = httpx.URL(target.decode())
            up_r, up_w = await asyncio.open_connection(url.host, url.port or 80)
            # 绝对形式改回起始行形式，其余头原样带过去
            up_w.write(b" ".join([method, url.raw_path or b"/", version]) + b"\r\n" + rest)
            await up_w.drain()
        except Exception:
            with contextlib.suppress(Exception):
                writer.close()
            return
        # 剩下的（请求体、响应、流）两边对拷就行，不用自己解 Content-Length
        await asyncio.gather(self._pump(reader, up_w), self._pump(up_r, writer))

    def _run(self) -> None:
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        self._stop = asyncio.Event()

        async def serve() -> None:
            server = await asyncio.start_server(self._handle, "127.0.0.1", self.port, ssl=self._tls)
            self._ready.set()
            await self._stop.wait()
            server.close()
            with contextlib.suppress(Exception):
                await server.wait_closed()

        try:
            self._loop.run_until_complete(serve())
        finally:
            self._loop.close()

    def __enter__(self) -> "MockProxy":
        self._thread.start()
        assert self._ready.wait(5), "代理没起来"
        return self

    def __exit__(self, *exc: object) -> None:
        if self._loop is not None and self._stop is not None:
            self._loop.call_soon_threadsafe(self._stop.set)
        self._thread.join(timeout=3)


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


def two_anthropic_sites(gateway, a, b, remote_a="claude-opus-4-1", remote_b="claude-opus-4-5"):
    """两个站各一个 anthropic 分组，同一个模型两条候选（a 是活跃的那条）。

    放在 helpers 而不是某个测试文件里：自动降级和「实时」页两组用例都要用它。
    """
    g_a = add_upstream(gateway, a, "siteA", "anthropic")
    g_b = add_upstream(gateway, b, "siteB", "anthropic")
    add_route(gateway, "opus", g_a, remote_a)
    add_route(gateway, "opus", g_b, remote_b)
    return g_a, g_b


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
