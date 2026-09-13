from __future__ import annotations

from contextlib import asynccontextmanager
from typing import AsyncIterator, Callable

from fastapi import FastAPI
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from . import config, db, proxy
from .admin import router as admin_router
from .proxy import router as proxy_router

LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1", "[::1]"}


def _parts(value: str) -> tuple[str, int]:
    """把 Host / Origin 拆成 (小写主机名, 端口)。IPv6 保留方括号。

    没有端口时按 scheme 的缺省端口算（http 80 / https 443），这样
    「Origin 带不带端口」和「Host 带不带端口」比较的是同一个东西。
    """
    raw = value.strip()
    scheme = ""
    if "://" in raw:
        scheme, raw = raw.split("://", 1)
        scheme = scheme.lower()
    raw = raw.split("/", 1)[0]
    if raw.startswith("["):
        host, _, rest = raw.partition("]")
        host += "]"
        port = rest[1:] if rest.startswith(":") else ""
    else:
        host, _, port = raw.partition(":")
    if port:
        try:
            return host.lower(), int(port)
        except ValueError:
            return host.lower(), -1  # 畸形端口：保证和谁都对不上
    return host.lower(), 443 if scheme == "https" else 80


def _reject_reason(scope: dict) -> str | None:
    headers = {k.decode("latin-1"): v.decode("latin-1") for k, v in scope.get("headers", [])}

    # 只监听回环地址，但仍要挡 DNS rebinding：攻击者的域名解析到回环地址时 Host 会是他的域名。
    # 没有 Host 的 HTTP/1.1 请求（curl --http1.0 之类）同样拒绝 —— 浏览器永远会带。
    host_value = headers.get("host", "").strip()
    if not host_value:
        return "缺少 Host 头"
    host, host_port = _parts(host_value)
    if host not in LOOPBACK_HOSTS:
        return f"只接受本机访问，非法 Host: {host_value}"

    # 任何路径都拒绝浏览器发起的跨站请求：管理接口没鉴权（关进程、改配置），
    # /v1 转发会烧钱（拿用户的 key 替任意网页跑模型）。curl / Codex / Claude Code
    # 这类客户端不带这些头，不受影响。
    if headers.get("sec-fetch-site", "").strip().lower() == "cross-site":
        return "不接受跨站请求"
    origin = headers.get("origin", "").strip()
    if not origin:
        return None
    # Origin: null（sandboxed iframe / file:// 页面）不可信，和跨站同等处理
    if origin.lower() == "null":
        return "不接受跨站请求，Origin: null"
    origin_host, origin_port = _parts(origin)
    # 主机名和端口都要和 Host 完全一致：别的本机端口/地址是 same-site，
    # 浏览器不会拿 Sec-Fetch-Site 拦；只比「是不是回环」不够。
    if origin_host != host or origin_port != host_port:
        return f"Origin 与网关地址不一致: {origin}"
    return None


class LocalOnly:
    """纯 ASGI 中间件：只看 scope 里的头，不包装响应，避免干扰 SSE 流式转发。"""

    def __init__(self, app: Callable) -> None:
        self.app = app

    async def __call__(self, scope: dict, receive: Callable, send: Callable) -> None:
        if scope["type"] == "http":
            reason = _reject_reason(scope)
            if reason is not None:
                await JSONResponse({"detail": reason}, status_code=403)(scope, receive, send)
                return
        await self.app(scope, receive, send)


class NoCacheStatic(StaticFiles):
    """管理页的静态文件一律不缓存。

    ES 模块的 `import './views.js'` 是裸路径，没法像 index.html 里那样挂 `?v=` 版本号；
    只给入口挂版本号更糟 —— 新的 app.js 配上缓存里的旧 views.js，页面会半坏不坏。
    localhost 上这点带宽无所谓，直接让浏览器每次都拿新的，省掉「改了没生效」这类事故。
    """

    def is_not_modified(self, response_headers, request_headers) -> bool:
        return False

    async def get_response(self, path: str, scope: dict):
        response = await super().get_response(path, scope)
        response.headers["cache-control"] = "no-store"
        return response


@asynccontextmanager
async def _lifespan(app: FastAPI) -> AsyncIterator[None]:
    yield
    await proxy.aclose_client()


def create_app() -> FastAPI:
    db.init_db()
    app = FastAPI(title="model-gateway", docs_url=None, redoc_url=None, lifespan=_lifespan)
    app.include_router(proxy_router)
    app.include_router(admin_router)
    app.mount("/static", NoCacheStatic(directory=config.WEB_DIR), name="static")

    @app.get("/")
    def index() -> FileResponse:
        return FileResponse(config.WEB_DIR / "index.html", headers={"cache-control": "no-store"})

    @app.get("/health")
    def health() -> dict[str, bool]:
        return {"ok": True}

    app.add_middleware(LocalOnly)
    return app
