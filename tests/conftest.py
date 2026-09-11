"""gateway fixture：每个用例一个临时库、一个独立端口，跑完把进程内的状态清干净。

和 helpers.py 一起，是原来那个 2000 行的 test_e2e.py 里所有「不是断言」的部分。
helpers 能被直接 import，靠的是 pytest 会把测试文件所在目录放进 sys.path。
"""

from __future__ import annotations

import httpx
import pytest
from starlette.testclient import TestClient

import helpers


@pytest.fixture()
def gateway(tmp_path, monkeypatch, request):
    from gateway import config, failover, inflight
    from gateway import proxy as proxy_mod
    from gateway import stats as stats_mod
    from gateway.app import create_app
    from gateway.server import start_server_thread

    data_dir = tmp_path / "data"
    monkeypatch.setattr(config, "DATA_DIR", data_dir)
    monkeypatch.setattr(config, "DB_PATH", data_dir / "gateway.db")
    # 断路器和「进行中」登记表都是进程内的内存状态，测试跑在同一个进程里 —— 不清会串到下一个用例。
    # 按字节估 token 的那把标尺也一样：它是从库里量的，而每个用例一个临时库
    failover.reset()
    inflight.reset()
    stats_mod.reset()

    use_network = request.node.get_closest_marker("network") is not None
    helpers.IN_PROCESS_UPSTREAMS = not use_network

    if not use_network:
        async def get_client(egress=""):
            client = proxy_mod._clients.get(egress)
            if client is None or client.is_closed:
                client = httpx.AsyncClient(
                    transport=httpx.MockTransport(helpers.fake_upstream_request),
                    timeout=proxy_mod.PROXY_TIMEOUT,
                    trust_env=False,
                )
                proxy_mod._clients[egress] = client
            return client

        monkeypatch.setattr(proxy_mod, "get_client", get_client)

        async def fetch_remote_models(base_url, api_key, header_override="", protocol="openai", egress=""):
            # The production helper creates its own socket client.  Reuse the
            # same in-process transport as forwarding while keeping its URL,
            # headers, status, and JSON validation behavior intact.
            from gateway import upstream as upstream_mod

            client = await get_client(egress)
            url = upstream_mod.models_url(base_url)
            resp = await client.get(
                url,
                headers=upstream_mod.build_headers(api_key, header_override, protocol),
            )
            if resp.status_code != 200:
                raise RuntimeError(f"{url} 返回 {resp.status_code}: {resp.text[:300]}")
            try:
                payload = resp.json()
            except ValueError as exc:
                raise RuntimeError(f"{url} 返回的不是 JSON: {resp.text[:200]}") from exc
            data = payload.get("data") if isinstance(payload, dict) else None
            if not isinstance(data, list):
                raise RuntimeError(f"{url} 的响应里没有 data 数组")
            return tuple(str(m["id"]) for m in data if isinstance(m, dict) and "id" in m)

        monkeypatch.setattr(
            "gateway.upstream.fetch_remote_models", fetch_remote_models
        )
        with TestClient(
            create_app(),
            base_url="http://127.0.0.1",
            headers={"user-agent": "python-httpx"},
        ) as client:
            yield client
        helpers._FAKE_UPSTREAMS.clear()
        helpers.IN_PROCESS_UPSTREAMS = False
        return

    port = helpers.free_port()
    server, thread = start_server_thread(port)
    try:
        helpers.wait_server_started(server, thread)
        base_url = f"http://127.0.0.1:{port}"
        # trust_env=False：全程都是回环地址，绝不能让系统代理（Clash 之类）插一脚
        with httpx.Client(base_url=base_url, timeout=15.0, trust_env=False) as client:
            yield client
    finally:
        server.should_exit = True
        thread.join(timeout=5)
        helpers.IN_PROCESS_UPSTREAMS = False
