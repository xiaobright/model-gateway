from __future__ import annotations

import errno
import logging
import threading
import time

import uvicorn

from . import config
from .app import create_app
from .startup import StartupError, logger

STARTUP_TIMEOUT = 60.0
# 10013 多发生在端口正被系统当作临时端口借用：本机动态端口段被压到 1024 起时，
# 8317 这类服务端口会偶发撞车，冲突窗口通常只有毫秒级。短暂退避后重试即可，
# 总重试窗口（BIND_ATTEMPTS - 1）× BIND_RETRY_DELAY 远小于 STARTUP_TIMEOUT。
BIND_ATTEMPTS = 4
BIND_RETRY_DELAY = 0.4


class GatewayServer(uvicorn.Server):
    startup_error: StartupError | None = None


class _StartupErrors(logging.Handler):
    def __init__(self, server: GatewayServer):
        super().__init__(logging.WARNING)
        self.server = server
        self.thread_id = threading.get_ident()
        # 先记在本地：退避重试期间不能让等待方误以为已经失败。
        self.error: StartupError | None = None

    def reset(self) -> None:
        self.error = None

    def emit(self, record: logging.LogRecord) -> None:
        if record.thread != self.thread_id or self.server.started:
            return
        detail = self.format(record)
        logger.log(record.levelno, "%s", detail)
        if record.levelno < logging.ERROR or self.error is not None:
            return
        kind = "thread_failed"
        if isinstance(record.msg, OSError):
            code = getattr(record.msg, "winerror", None) or record.msg.errno
            if code in (errno.EADDRINUSE, 10048):
                kind = "port_in_use"
            elif code in (errno.EACCES, errno.EPERM, 10013):
                kind = "bind_denied"
            else:
                kind = "bind_failed"
        self.error = StartupError(kind, detail)


def _should_retry_bind(server: GatewayServer, error: StartupError | None, attempt: int) -> bool:
    """只有 10013 值得重试：它多半是端口被临时借用，退避一下就能拿到。"""
    if server.started or server.should_exit or attempt >= BIND_ATTEMPTS:
        return False
    # 10048 说明确实有进程在监听，重试只会把明确结论拖成慢失败。
    return error is not None and error.kind == "bind_denied"


def wait_for_startup(
    server: GatewayServer, thread: threading.Thread, timeout: float = STARTUP_TIMEOUT,
) -> None:
    deadline = time.monotonic() + timeout
    while True:
        if server.startup_error is not None:
            raise server.startup_error
        if not thread.is_alive():
            raise StartupError("thread_failed", "服务线程已退出，未保持监听；详见启动日志。")
        if server.started:
            logger.info("监听就绪 host=%s port=%s", server.config.host, server.config.port)
            return
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            # 不让迟到的启动在错误弹窗后继续作为正常服务运行。
            server.should_exit = True
            raise StartupError("timeout", f"服务在 {timeout:g} 秒内未完成启动。")
        time.sleep(min(0.1, remaining))


_registry: dict = {"server": None, "tray": None}


def register_server(srv: uvicorn.Server) -> None:
    _registry["server"] = srv


def attach_tray(tray_app) -> None:
    _registry["tray"] = tray_app


def request_shutdown() -> bool:
    tray = _registry["tray"]
    if tray is not None:
        tray.begin_shutdown()
        return True
    server: uvicorn.Server | None = _registry["server"]
    if server is not None:
        server.should_exit = True
        return True
    return False


def start_server_thread(port: int, host: str = "127.0.0.1") -> tuple[GatewayServer, threading.Thread]:
    logger.info("初始化服务 host=%s port=%s", host, port)
    config.set_bind(host, port)
    try:
        server = GatewayServer(uvicorn.Config(create_app(), host=host, port=port, log_level="warning"))
    except Exception as exc:
        logger.exception("服务初始化失败")
        raise StartupError("initialization", f"{type(exc).__name__}: {exc}") from exc

    def run() -> None:
        errors = _StartupErrors(server)
        uvicorn_logger = logging.getLogger("uvicorn.error")
        uvicorn_logger.addHandler(errors)
        try:
            attempt = 0
            while True:
                attempt += 1
                errors.reset()
                try:
                    server.run()
                except BaseException as exc:
                    # Uvicorn 的监听/生命周期失败会在后台线程中抛 SystemExit。
                    failure = errors.error or StartupError("thread_failed", f"{type(exc).__name__}: {exc}")
                    if _should_retry_bind(server, failure, attempt):
                        logger.warning(
                            "绑定 %s:%s 被瞬时占用，%.1f 秒后重试（第 %d/%d 次）",
                            host, port, BIND_RETRY_DELAY, attempt, BIND_ATTEMPTS - 1,
                        )
                        time.sleep(BIND_RETRY_DELAY)
                        continue
                    # 确定不再重试才对外公布，否则等待方会在退避期间提前弹窗。
                    server.startup_error = failure
                    if failure.kind == "bind_denied":
                        logger.error(
                            "已尝试绑定 %d 次仍被拒绝；可查端口保留："
                            "netsh int ipv4 show excludedportrange protocol=tcp，"
                            "以及 netsh int ipv4 show dynamicport tcp",
                            attempt,
                        )
                    logger.exception("服务线程退出 started=%s", server.started)
                    break
                else:
                    break
        finally:
            uvicorn_logger.removeHandler(errors)
            errors.close()
            logger.info("服务线程结束 started=%s", server.started)

    thread = threading.Thread(target=run, name="gateway-server", daemon=True)
    _registry["server"] = server
    thread.start()
    return server, thread
