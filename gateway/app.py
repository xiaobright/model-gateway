from __future__ import annotations

from contextlib import asynccontextmanager
from typing import AsyncIterator, Callable

from fastapi import FastAPI
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from . import config, db, proxy
from .admin import router as admin_router
from .proxy import router as proxy_router

LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1", "[::1]", ""}


def _hostname(value: str) -> str:
    """从 Host / Origin 里取出主机名：去掉 scheme 和端口，IPv6 保留方括号。"""
    host = value.strip().split("://", 1)[-1].split("/", 1)[0]
    if host.startswith("["):
        return host.split("]", 1)[0] + "]"
    return host.split(":", 1)[0]


def _reject_reason(scope: dict) -> str | None:
    headers = {k.decode("latin-1"): v.decode("latin-1") for k, v in scope.get("headers", [])}

    # 只监听 127.0.0.1，但仍要挡 DNS rebinding：攻击者的域名解析到回环地址时 Host 会是他的域名
    host = headers.get("host", "")
    if host and _hostname(host).lower() not in LOOPBACK_HOSTS:
        return f"只接受本机访问，非法 Host: {host}"

    # 管理接口没有鉴权，所以必须挡住其它网页发来的跨站请求（CSRF），否则任意页面都能改配置/关进程
    if scope.get("path", "").startswith("/admin/api"):
        if headers.get("sec-fetch-site") == "cross-site":
            return "管理接口不接受跨站请求"
        origin = headers.get("origin", "")
        # Origin: null（sandboxed iframe / file:// 页面）同样不可信，一律按跨站处理
        if origin and _hostname(origin).lower() not in LOOPBACK_HOSTS:
            return f"管理接口不接受跨站请求，Origin: {origin}"
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
