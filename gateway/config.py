from __future__ import annotations

import json
import socket
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT_ROOT / "data"
DB_PATH = DATA_DIR / "gateway.db"
SETTINGS_PATH = DATA_DIR / "settings.json"
WEB_DIR = PROJECT_ROOT / "web"

DEFAULT_PORT = 8317
DEFAULT_HOST = "127.0.0.1"

_LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1", "[::1]", ""}
_WILDCARD_HOSTS = {"0.0.0.0", "::", "[::]"}

# 服务实际绑在哪：启动时由 server / main 记一笔，管理页据此决定左下角显示哪个地址。
# 不在这里猜 —— 只听本机就显示 127.0.0.1，绑到局域网才显示本机的局域网 IP。
_bound_host = DEFAULT_HOST
_bound_port: int | None = None


def set_bind(host: str, port: int) -> None:
    global _bound_host, _bound_port
    _bound_host = (host or DEFAULT_HOST).strip()
    _bound_port = int(port)


def bind_host() -> str:
    return _bound_host


def bind_port() -> int:
    return _bound_port if _bound_port is not None else load_port()


def lan_ip() -> str | None:
    """本机在局域网里的地址。UDP connect 只让系统挑出口网卡，不会真的发包。"""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.settimeout(0.2)
            sock.connect(("192.0.2.1", 9))   # TEST-NET-1，保留给文档示例，不会真的连出去
            ip = sock.getsockname()[0]
    except OSError:
        return None
    return ip if ip and not ip.startswith("127.") else None


def access_host() -> str:
    """管理页里给客户端用的主机名：只听本机就是 127.0.0.1，绑到局域网就是局域网 IP。"""
    host = _bound_host
    if host in _LOOPBACK_HOSTS:
        return DEFAULT_HOST
    if host in _WILDCARD_HOSTS:
        return lan_ip() or DEFAULT_HOST
    return host


def load_port() -> int:
    try:
        raw = json.loads(SETTINGS_PATH.read_text("utf-8"))
        if not isinstance(raw, dict):
            return DEFAULT_PORT
        value = raw.get("port", DEFAULT_PORT)
        # JSON 里的 true 会被 int() 收成 1 并通过范围检查
        if isinstance(value, bool) or isinstance(value, float) and not value.is_integer():
            return DEFAULT_PORT
        port = int(value)
    except (OSError, ValueError, TypeError, OverflowError):
        return DEFAULT_PORT
    return port if 1 <= port <= 65535 else DEFAULT_PORT
