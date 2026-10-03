"""启动失败分流与诊断；全部使用虚拟时钟、临时目录或本机临时端口。"""

from __future__ import annotations

import asyncio
import errno
import logging
import socket
import sys
import threading
from contextlib import asynccontextmanager
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI

from gateway import config, server as server_mod, startup


@pytest.fixture(autouse=True)
def isolated_startup(tmp_path, monkeypatch):
    data_dir = tmp_path / "data"
    monkeypatch.setattr(config, "DATA_DIR", data_dir)
    monkeypatch.setattr(config, "DB_PATH", data_dir / "gateway.db")
    monkeypatch.setenv("MODEL_GATEWAY_LEARNING", "0")
    monkeypatch.setattr(server_mod, "_registry", {"server": None, "tray": None})
    previous = list(startup.logger.handlers)
    try:
        yield
    finally:
        for handler in list(startup.logger.handlers):
            if handler not in previous:
                startup.logger.removeHandler(handler)
                handler.close()


def fake_wait(monkeypatch, *, ready_after=None, alive=True):
    clock = SimpleNamespace(now=0.0)
    server = SimpleNamespace(
        startup_error=None, started=False, should_exit=False,
        config=SimpleNamespace(host="127.0.0.1", port=8317),
    )

    def sleep(seconds):
        clock.now += seconds
        if ready_after is not None and clock.now >= ready_after:
            server.started = True

    monkeypatch.setattr(server_mod, "time", SimpleNamespace(monotonic=lambda: clock.now, sleep=sleep))
    return server, SimpleNamespace(is_alive=lambda: alive), clock


def test_slow_startup_can_exceed_the_old_ten_second_limit(monkeypatch):
    server, thread, clock = fake_wait(monkeypatch, ready_after=12)
    server_mod.wait_for_startup(server, thread)
    assert 12 <= clock.now < 13
    assert not server.should_exit


def test_dead_thread_fails_immediately_even_if_it_once_started(monkeypatch):
    server, thread, clock = fake_wait(monkeypatch, alive=False)
    server.started = True
    with pytest.raises(startup.StartupError) as caught:
        server_mod.wait_for_startup(server, thread)
    assert caught.value.kind == "thread_failed"
    assert clock.now == 0


def test_timeout_requests_stop_and_does_not_claim_a_port_conflict(monkeypatch):
    server, thread, clock = fake_wait(monkeypatch)
    with pytest.raises(startup.StartupError) as caught:
        server_mod.wait_for_startup(server, thread)
    assert clock.now == pytest.approx(60)
    assert server.should_exit
    assert caught.value.kind == "timeout"
    assert "启动等待超时" in caught.value.message(8317)
    assert "已被占用" not in caught.value.message(8317)


@pytest.mark.parametrize("failure", [RuntimeError("worker-start-broken"), SystemExit(3)])
def test_worker_exception_is_preserved_even_without_console(monkeypatch, failure):
    log_path = startup.configure_logging()
    monkeypatch.setattr(server_mod, "create_app", FastAPI)

    def fail_run(self):
        raise failure

    monkeypatch.setattr(server_mod.GatewayServer, "run", fail_run)
    server, thread = server_mod.start_server_thread(0)
    thread.join(timeout=5)
    assert not thread.is_alive()
    with pytest.raises(startup.StartupError) as caught:
        server_mod.wait_for_startup(server, thread)
    assert caught.value.kind == "thread_failed"
    assert type(failure).__name__ in str(caught.value)
    log = log_path.read_text(encoding="utf-8")
    assert "Traceback" in log and type(failure).__name__ in log
    assert not any(isinstance(h, server_mod._StartupErrors) for h in logging.getLogger("uvicorn.error").handlers)


def test_initialization_failure_is_reported_before_a_thread_starts(monkeypatch):
    log_path = startup.configure_logging()

    def broken_app():
        raise ValueError("invalid-local-config")

    monkeypatch.setattr(server_mod, "create_app", broken_app)
    with pytest.raises(startup.StartupError) as caught:
        server_mod.start_server_thread(0)
    assert caught.value.kind == "initialization"
    assert server_mod._registry["server"] is None
    assert "ValueError: invalid-local-config" in log_path.read_text(encoding="utf-8")


def test_uvicorn_lifespan_failure_keeps_the_original_traceback(monkeypatch):
    log_path = startup.configure_logging()

    @asynccontextmanager
    async def lifespan(app):
        raise RuntimeError("lifespan-start-broken")
        yield  # pragma: no cover

    monkeypatch.setattr(server_mod, "create_app", lambda: FastAPI(lifespan=lifespan))
    server, thread = server_mod.start_server_thread(0)
    try:
        with pytest.raises(startup.StartupError) as caught:
            server_mod.wait_for_startup(server, thread, timeout=5)
        assert caught.value.kind == "thread_failed"
        assert "lifespan-start-broken" in str(caught.value)
    finally:
        server.should_exit = True
        thread.join(timeout=5)
    assert not thread.is_alive()
    assert "RuntimeError: lifespan-start-broken" in log_path.read_text(encoding="utf-8")


@pytest.mark.parametrize("code, kind", [(errno.EACCES, "bind_denied"), (errno.EADDRNOTAVAIL, "bind_failed")])
def test_bind_errors_retain_their_specific_reason(monkeypatch, code, kind):
    monkeypatch.setattr(server_mod, "create_app", FastAPI)

    def fail_run(self):
        logging.getLogger("uvicorn.error").error(OSError(code, "test bind error"))
        raise SystemExit(3)

    monkeypatch.setattr(server_mod.GatewayServer, "run", fail_run)
    server, thread = server_mod.start_server_thread(0)
    thread.join(timeout=5)
    assert not thread.is_alive()
    with pytest.raises(startup.StartupError) as caught:
        server_mod.wait_for_startup(server, thread)
    assert caught.value.kind == kind
    assert "test bind error" in str(caught.value)


def test_logging_is_bounded_and_reconfiguration_does_not_duplicate_records():
    log_path = startup.configure_logging()
    startup.logger.info("first-start")
    assert startup.configure_logging() == log_path
    startup.logger.info("second-start")
    handlers = [h for h in startup.logger.handlers if isinstance(h, logging.handlers.RotatingFileHandler)]
    assert len(handlers) == 1
    assert handlers[0].maxBytes == 1024 * 1024 and handlers[0].backupCount == 2
    text = log_path.read_text(encoding="utf-8")
    assert text.count("first-start") == text.count("second-start") == 1
    assert "pid=" in text


def test_unwritable_logging_does_not_prevent_startup(monkeypatch):
    def denied(*args, **kwargs):
        raise PermissionError("denied-for-test")

    monkeypatch.setattr(startup, "RotatingFileHandler", denied)
    assert startup.configure_logging() is None


def test_tray_failure_requests_cleanup_before_showing_specific_error(monkeypatch):
    import gateway
    import main

    calls = []
    server = SimpleNamespace(should_exit=False)
    thread = SimpleNamespace(join=lambda timeout: calls.append(("join", timeout, server.should_exit)))
    tray = SimpleNamespace(
        acquire_single_instance=lambda: True,
        msgbox=lambda message: calls.append(("message", message)),
    )
    monkeypatch.setattr(gateway, "tray", tray, raising=False)
    monkeypatch.setitem(sys.modules, "gateway.tray", tray)
    monkeypatch.setattr(main, "parse_args", lambda: SimpleNamespace(tray=True, no_tray=False, port=8317, open_ui=False))
    monkeypatch.setattr(server_mod, "start_server_thread", lambda port: (server, thread))

    def failed_wait(*args):
        raise startup.StartupError("port_in_use", "occupied-for-test")

    monkeypatch.setattr(server_mod, "wait_for_startup", failed_wait)
    assert main.main() == 1
    assert calls[0] == ("join", 5, True)
    assert "8317 已被占用" in calls[1][1]
    assert str(config.DATA_DIR / "startup.log") in calls[1][1]


@pytest.mark.network
def test_occupied_socket_is_distinguished_from_slow_startup(monkeypatch):
    log_path = startup.configure_logging()
    monkeypatch.setattr(server_mod, "create_app", FastAPI)
    with socket.socket() as occupied:
        occupied.bind(("127.0.0.1", 0))
        occupied.listen()
        port = occupied.getsockname()[1]
        server, thread = server_mod.start_server_thread(port)
        try:
            with pytest.raises(startup.StartupError) as caught:
                server_mod.wait_for_startup(server, thread, timeout=5)
            assert caught.value.kind == "port_in_use"
            assert str(port) in str(caught.value)
        finally:
            server.should_exit = True
            thread.join(timeout=5)
        assert not thread.is_alive()
    log = log_path.read_text(encoding="utf-8")
    assert str(port) in log and "SystemExit" in log


@pytest.mark.network
def test_real_gateway_starts_and_stops_using_a_temporary_database():
    server, thread = server_mod.start_server_thread(0)
    try:
        server_mod.wait_for_startup(server, thread, timeout=5)
        port = server.servers[0].sockets[0].getsockname()[1]
        with httpx.Client(trust_env=False, timeout=5) as client:
            response = client.get(f"http://127.0.0.1:{port}/health")
        assert response.status_code == 200 and response.json() == {"ok": True}
        assert config.DB_PATH.is_file()
    finally:
        server.should_exit = True
        thread.join(timeout=5)
    assert not thread.is_alive()


@pytest.mark.network
def test_late_startup_shuts_down_after_timeout(monkeypatch):
    entered = threading.Event()
    release = threading.Event()
    stopped = threading.Event()

    @asynccontextmanager
    async def lifespan(app):
        entered.set()
        await asyncio.to_thread(release.wait)
        try:
            yield
        finally:
            stopped.set()

    monkeypatch.setattr(server_mod, "create_app", lambda: FastAPI(lifespan=lifespan))
    server, thread = server_mod.start_server_thread(0)
    try:
        assert entered.wait(timeout=5)
        with pytest.raises(startup.StartupError) as caught:
            server_mod.wait_for_startup(server, thread, timeout=0.02)
        assert caught.value.kind == "timeout"
    finally:
        server.should_exit = True
        release.set()
        thread.join(timeout=5)
    assert not thread.is_alive() and stopped.is_set()
    assert all(not listener.is_serving() for listener in server.servers)
