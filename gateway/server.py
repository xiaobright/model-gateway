from __future__ import annotations

import threading

import uvicorn

from .app import create_app

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


def start_server_thread(port: int, host: str = "127.0.0.1") -> tuple[uvicorn.Server, threading.Thread]:
    server = uvicorn.Server(uvicorn.Config(create_app(), host=host, port=port, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    _registry["server"] = server
    return server, thread
