"""启动诊断：独立的小日志，不记录请求正文或供应商配置。"""

from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path

from . import config

logger = logging.getLogger("model-gateway.startup")
logger.setLevel(logging.INFO)
logger.propagate = False
logger.addHandler(logging.NullHandler())


def configure_logging() -> Path | None:
    path = config.DATA_DIR / "startup.log"
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        handler = RotatingFileHandler(path, maxBytes=1024 * 1024, backupCount=2, encoding="utf-8")
    except OSError:
        return None
    handler.setFormatter(logging.Formatter("%(asctime)s pid=%(process)d %(levelname)s %(message)s"))
    for old in list(logger.handlers):
        if isinstance(old, RotatingFileHandler):
            logger.removeHandler(old)
            old.close()
    logger.addHandler(handler)
    return path


class StartupError(RuntimeError):
    def __init__(self, kind: str, detail: str):
        super().__init__(detail)
        self.kind = kind

    def message(self, port: int) -> str:
        heading = {
            "port_in_use": f"启动失败：127.0.0.1:{port} 已被占用。",
            "bind_denied": f"启动失败：系统拒绝绑定 127.0.0.1:{port}，请检查端口保留或访问限制。",
            "bind_failed": f"启动失败：无法监听 127.0.0.1:{port}。",
            "timeout": "启动等待超时，已请求停止本次启动；这不代表端口被占用。",
            "initialization": "服务初始化失败。",
            "thread_failed": "服务线程启动失败。",
        }[self.kind]
        return f"{heading}\n\n{str(self)[-800:]}"
