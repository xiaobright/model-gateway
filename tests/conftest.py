"""每例独立临时库；默认进程内运行，network 用例才启动本机端口。

helpers 能被直接 import，靠的是 pytest 会把测试文件所在目录放进 sys.path。
"""

from __future__ import annotations

import httpx
import pytest
from starlette.testclient import TestClient

import helpers


def pytest_addoption(parser):
    group = parser.getgroup("gateway", "网关测试范围")
    group.addoption("--full", action="store_true", help="运行全部测试，包括本机网络组")
    group.addoption("--network", action="store_true", help="只运行本机网络组")


def _test_scope(config):
    # 保留原文档的 -m network 入口；其他 -m / -k 在选定范围内继续筛选。
    network = config.getoption("network") or config.getoption("markexpr").strip() == "network"
    if config.getoption("full"):
        if network:
            raise pytest.UsageError("--full 与 --network / -m network 不能同时使用")
        return "full"
    return "network" if network else "fast"


def pytest_configure(config):
    _test_scope(config)  # 冲突在收集前报错，不能静默少跑。


def pytest_collection_modifyitems(config, items):
    scope = _test_scope(config)
    if scope == "full":
        return
    selected, deselected = [], []
    for item in items:
        is_network = item.get_closest_marker("network") is not None
        (selected if is_network == (scope == "network") else deselected).append(item)
    items[:] = selected
    config.hook.pytest_deselected(items=deselected)


def pytest_terminal_summary(terminalreporter, config):
    scope = _test_scope(config)
    labels = {"fast": "非网络组（默认）", "network": "本机网络组", "full": "全部组"}
    terminalreporter.write_line(
        f"测试范围：{labels[scope]}；文件 / -k / -m 仍生效。"
        "网络组用 --network，完整检查用 --full。"
    )


@pytest.fixture()
def gateway(tmp_path, monkeypatch, request):
    from gateway import config, failover, inflight
    from gateway import canvas as canvas_mod
    from gateway import upstream as upstream_mod
    from gateway import stats as stats_mod
    from gateway.app import create_app
    from gateway.server import start_server_thread

    data_dir = tmp_path / "data"
    monkeypatch.setattr(config, "DATA_DIR", data_dir)
    monkeypatch.setattr(config, "DB_PATH", data_dir / "gateway.db")
    # 通用用例不启动无关的后台写盘线程；test_learning 独立开启真实采集器。
    monkeypatch.setenv("MODEL_GATEWAY_LEARNING", "0")
    # 断路器和「进行中」登记表都是进程内的内存状态，测试跑在同一个进程里 —— 不清会串到下一个用例。
    # 按字节估 token 的那把标尺也一样：它是从库里量的，而每个用例一个临时库
    failover.reset()
    inflight.reset()
    stats_mod.reset()
    # 画布布局也按原始文本缓存，换库必须一起清，否则上一个用例的坐标会漏过来
    canvas_mod.forget_cache()

    use_network = request.node.get_closest_marker("network") is not None
    helpers.IN_PROCESS_UPSTREAMS = not use_network

    if not use_network:
        async def get_client(egress=""):
            client = upstream_mod._clients.get(egress)
            if client is None or client.is_closed:
                client = httpx.AsyncClient(
                    transport=httpx.MockTransport(helpers.fake_upstream_request),
                    timeout=upstream_mod.PROXY_TIMEOUT,
                    trust_env=False,
                )
                upstream_mod._clients[egress] = client
            return client

        monkeypatch.setattr(upstream_mod, "get_client", get_client)

        async def fetch_remote_models(base_url, api_key, header_override="", protocol="openai", egress=""):
            # The production helper creates its own socket client.  Reuse the
            # same in-process transport as forwarding while keeping its URL,
            # headers, status, and JSON validation behavior intact.
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
        try:
            with TestClient(
                create_app(),
                base_url="http://127.0.0.1",
                headers={"user-agent": "python-httpx"},
            ) as client:
                yield client
        finally:
            # lifespan 收尾抛异常也要复位，否则后面的 network 用例会把 mock 上游
            # 当成进程内假上游注册，形成级联怪错
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
