"""gateway fixture：每个用例一个临时库、一个独立端口，跑完把进程内的状态清干净。

和 helpers.py 一起，是原来那个 2000 行的 test_e2e.py 里所有「不是断言」的部分。
helpers 能被直接 import，靠的是 pytest 会把测试文件所在目录放进 sys.path。
"""

from __future__ import annotations

import httpx
import pytest

from helpers import free_port, wait_server_started


@pytest.fixture()
def gateway(tmp_path, monkeypatch):
    from gateway import config, failover, inflight
    from gateway import stats as stats_mod
    from gateway.server import start_server_thread

    data_dir = tmp_path / "data"
    monkeypatch.setattr(config, "DATA_DIR", data_dir)
    monkeypatch.setattr(config, "DB_PATH", data_dir / "gateway.db")
    # 断路器和「进行中」登记表都是进程内的内存状态，测试跑在同一个进程里 —— 不清会串到下一个用例。
    # 按字节估 token 的那把标尺也一样：它是从库里量的，而每个用例一个临时库
    failover.reset()
    inflight.reset()
    stats_mod.reset()

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
