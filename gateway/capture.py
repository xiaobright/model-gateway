"""常驻流式抓包：热开关，抓「请求体 + 上游原始响应字节」。

设计目标：以后再遇到「下游说截断、上游说正常」这类问题，**不用改代码、不用重启**，
开一下开关复现一次就能拿到原始字节。

三种开关方式（都是热的）：

1. 放 flag 文件（最省事，跟 capture.flag 一个路子）::

       echo {} > data/capture-stream.flag          # 抓 1 条后自动停
       echo {"max":3} > data/capture-stream.flag   # 连抓 3 条

2. 删 flag 文件 = 立刻停。

3. 管理 API（前端/脚本用）::

       GET  /admin/api/capture-stream                     看状态
       PUT  /admin/api/capture-stream {"enabled":true,"max":1}
       PUT  /admin/api/capture-stream {"enabled":false}

产物目录 ``data/captured_stream/<时间戳>-<模型>/``：

- ``request.json``  客户端发来的原始请求体（脱敏只去掉 header，body 原样）
- ``stream.sse``    上游回来的**每一个字节**（也是转发给客户端的字节，透传时两者相同）
- ``meta.json``     路径 / 模型 / 上游 / 状态码 / note / 字节数 / 观察器是否看到结束事件

抓包是观察用的：写盘失败、磁盘满、超 MAX_STREAM_BYTES 都只丢抓包，不影响转发。
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Mapping

from gateway import config

FLAG_NAME = "capture-stream.flag"
OUT_DIRNAME = "captured_stream"
MAX_STREAM_BYTES = 16 * 1024 * 1024  # 单条流封顶 16MB，超了截断但继续转发
DEFAULT_MAX_REQUESTS = 1

# 进程内剩余条数。flag 文件是权威来源：外部直接删文件也能立刻生效，
# 这里的计数只是为了避免同一次运行里反复读盘。
_state: dict[str, int] = {"remaining": 0}


def flag_path() -> Path:
    return config.DATA_DIR / FLAG_NAME


def out_root() -> Path:
    """抓包产物根目录（管理接口列目录时用）。"""
    return config.DATA_DIR / OUT_DIRNAME


# 内部都走 out_root()，别再单独拼路径。
_out_root = out_root


def _read_spec() -> dict[str, Any]:
    """读 flag 内容；空文件或坏 JSON 都按「抓 1 条」处理。"""
    try:
        raw = flag_path().read_text("utf-8").strip()
        if not raw:
            return {"max": DEFAULT_MAX_REQUESTS}
        spec = json.loads(raw)
        if isinstance(spec, dict):
            return {"max": max(1, int(spec.get("max") or DEFAULT_MAX_REQUESTS))}
    except Exception:
        pass
    return {"max": DEFAULT_MAX_REQUESTS}


# 抓包会写请求头/响应头，但密钥一律不落盘：名字命中这些就整条丢掉，
# 不确定的（比如各家自造的 x-*-token）按后缀再兜一道。
_SECRET_HEADERS = {
    "authorization",
    "proxy-authorization",
    "cookie",
    "set-cookie",
    "api-key",
    "x-api-key",
    "x-goog-api-key",
    "x-auth-token",
}
_SECRET_SUFFIXES = ("-key", "-token", "-secret", "-auth")


def sanitize_headers(headers: Any) -> dict[str, str]:
    """转成普通 dict 并丢掉鉴权头。传什么都行，坏了返回 {}。"""
    out: dict[str, str] = {}
    try:
        items = headers.items()
    except Exception:
        return out
    for raw_name, value in items:
        try:
            name = str(raw_name).lower()
            if name in _SECRET_HEADERS or name.endswith(_SECRET_SUFFIXES):
                continue
            out[name] = str(value)
        except Exception:
            continue
    return out


def enabled() -> bool:
    return flag_path().exists()


def status() -> dict[str, Any]:
    spec = _read_spec() if enabled() else None
    return {
        "enabled": spec is not None,
        "remaining": _state["remaining"],
        "max": (spec or {}).get("max", DEFAULT_MAX_REQUESTS),
        "out_dir": str(_out_root()),
    }


def enable(max_requests: int = DEFAULT_MAX_REQUESTS) -> dict[str, Any]:
    max_requests = max(1, int(max_requests or DEFAULT_MAX_REQUESTS))
    flag_path().write_text(json.dumps({"max": max_requests}), encoding="utf-8")
    _state["remaining"] = max_requests
    return status()


def disable() -> dict[str, Any]:
    try:
        flag_path().unlink()
    except FileNotFoundError:
        pass
    _state["remaining"] = 0
    return status()


class StreamCapture:
    """一条请求的抓包句柄。begin() 返回 None 时表示没开抓包。"""

    def __init__(self, meta: Mapping[str, Any], request_body: bytes | None) -> None:
        stamp = time.strftime("%Y%m%d-%H%M%S")
        model = str(meta.get("model") or "unknown")[:40].replace("/", "_")
        self.dir = _out_root() / f"{stamp}-{model}"
        self.dir.mkdir(parents=True, exist_ok=True)
        self.meta: dict[str, Any] = dict(meta)
        self._bytes = 0
        self._capped = False
        self._fh = (self.dir / "stream.sse").open("wb")
        if request_body:
            try:
                (self.dir / "request.json").write_bytes(request_body)
            except Exception:
                pass  # 抓不到请求体也要继续抓响应

    def feed(self, chunk: bytes) -> None:
        """喂一块上游字节。超封顶后只记长度，不再写盘。

        _bytes 记的永远是真实字节数（不是写盘数），否则 meta 里看不出「这条是被
        截过的」—— 排查时真正要的就是这个数跟 sent 对不上的那一眼。
        """
        if self._fh.closed:
            return
        real = len(chunk)
        already = self._bytes  # 本块进来之前已经记了多少，用来算还剩多少可写
        self._bytes += real
        if self._capped:
            return
        room = MAX_STREAM_BYTES - already
        if room <= 0:
            self._capped = True
            return
        if real > room:
            chunk = chunk[:room]
            self._capped = True
        try:
            self._fh.write(chunk)
        except Exception:
            self._capped = True

    def finish(self, **extra: Any) -> None:
        self.meta.update(extra)
        self.meta["stream_bytes"] = self._bytes
        self.meta["stream_capped"] = self._capped
        try:
            if not self._fh.closed:
                self._fh.close()
        except Exception:
            pass
        try:
            (self.dir / "meta.json").write_text(
                json.dumps(self.meta, indent=2, ensure_ascii=False), encoding="utf-8"
            )
        except Exception:
            pass


def begin(meta: Mapping[str, Any], request_body: bytes | None = None) -> StreamCapture | None:
    """开抓包时返回一个句柄，否则 None（调用点零成本）。"""
    if not enabled():
        _state["remaining"] = 0
        return None
    if _state["remaining"] <= 0:
        _state["remaining"] = _read_spec()["max"]
    try:
        return StreamCapture(meta, request_body)
    except Exception:
        return None


def finish(cap: StreamCapture | None, **extra: Any) -> None:
    """收尾并递减计数；抓满自动关（删 flag）。"""
    if cap is None:
        return
    try:
        cap.finish(**extra)
    finally:
        _state["remaining"] = max(0, _state["remaining"] - 1)
        if _state["remaining"] <= 0:
            try:
                flag_path().unlink()
            except FileNotFoundError:
                pass
