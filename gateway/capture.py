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

import itertools
import json
import re
import threading
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any, Mapping

from fastapi import Request

from . import config
from .reqlog import log

if TYPE_CHECKING:
    from . import db

FLAG_NAME = "capture-stream.flag"
OUT_DIRNAME = "captured_stream"
MAX_STREAM_BYTES = 16 * 1024 * 1024  # 单条流封顶 16MB，超了截断但继续转发
DEFAULT_MAX_REQUESTS = 1

# 进程内剩余条数。flag 文件是权威来源：外部直接删文件也能立刻生效，
# 这里的计数只是为了避免同一次运行里反复读盘。begin 时就占名额，抓满不再补发。
# 转发线程（begin/finish）和管理接口线程池（status/enable/disable）都会碰它，加把锁。
_state: dict[str, int] = {"remaining": 0, "active": 0}
_lock = threading.Lock()
_seq = itertools.count(1)  # 目录序号：同一秒、同一个模型也不会互相覆盖


def flag_path() -> Path:
    return config.DATA_DIR / FLAG_NAME


def out_root() -> Path:
    """抓包产物根目录（管理接口列目录时用）。"""
    return config.DATA_DIR / OUT_DIRNAME


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


def is_secret_header(name: str) -> bool:
    """这个名字的头是不是密钥。抓包落盘的每一处都必须用这同一份判定。"""
    name = str(name).lower()
    return name in _SECRET_HEADERS or name.endswith(_SECRET_SUFFIXES)


def sanitize_headers(headers: Any) -> dict[str, str]:
    """转成普通 dict 并丢掉鉴权头。传什么都行，坏了返回 {}。"""
    out: dict[str, str] = {}
    try:
        items = headers.items()
    except Exception:
        return out
    for raw_name, value in items:
        try:
            if is_secret_header(raw_name):
                continue
            out[str(raw_name).lower()] = str(value)
        except Exception:
            continue
    return out


def enabled() -> bool:
    return flag_path().exists()


def status() -> dict[str, Any]:
    spec = _read_spec() if enabled() else None
    with _lock:
        remaining = _state["remaining"]
    return {
        "enabled": spec is not None,
        "remaining": remaining,
        "max": (spec or {}).get("max", DEFAULT_MAX_REQUESTS),
        "out_dir": str(out_root()),
    }


def enable(max_requests: int = DEFAULT_MAX_REQUESTS) -> dict[str, Any]:
    max_requests = max(1, int(max_requests or DEFAULT_MAX_REQUESTS))
    flag_path().write_text(json.dumps({"max": max_requests}), encoding="utf-8")
    with _lock:
        _state["remaining"] = max_requests
    return status()


def disable() -> dict[str, Any]:
    try:
        flag_path().unlink()
    except FileNotFoundError:
        pass
    with _lock:
        _state["remaining"] = 0
    return status()


class StreamCapture:
    """一条请求的抓包句柄。begin() 返回 None 时表示没开抓包。"""

    def __init__(self, meta: Mapping[str, Any], request_body: bytes | None) -> None:
        stamp = time.strftime("%Y%m%d-%H%M%S")
        # 模型名来自客户端请求体，会进目录名 —— 只留一组安全字符，`\`、`:`、`..`
        # 这类在 Windows 上是路径分隔/保留字符，原样拼进去能逃出 captured_stream。
        model = re.sub(r"[^A-Za-z0-9._-]+", "_", str(meta.get("model") or "unknown"))[:40]
        model = model.strip("._") or "unknown"
        root = out_root().resolve()
        target = (root / f"{stamp}-{next(_seq):03d}-{model}").resolve()
        if target.parent != root:
            raise ValueError(f"抓包目录名不合法：{meta.get('model')!r}")
        self.dir = target
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
        with _lock:
            _state["remaining"] = 0
        return None
    # 名额在 begin 时占住：并发开抓不能各自都看到「还有余额」然后一起超发。
    # active == 0 才补额 —— 否则「全部在飞、还没 finish」会被误当成计数过期。
    with _lock:
        if _state["remaining"] <= 0 and _state["active"] == 0:
            _state["remaining"] = _read_spec()["max"]
        if _state["remaining"] <= 0:
            return None
        _state["remaining"] -= 1
        _state["active"] += 1
    try:
        return StreamCapture(meta, request_body)
    except Exception:
        with _lock:  # 建句柄失败（磁盘满之类）把名额还回去
            _state["active"] = max(0, _state["active"] - 1)
            _state["remaining"] += 1
        return None


def finish(cap: StreamCapture | None, **extra: Any) -> None:
    """收尾并检查是否抓满；抓满自动关（删 flag）。"""
    if cap is None:
        return
    try:
        cap.finish(**extra)
    finally:
        with _lock:
            _state["active"] = max(0, _state["active"] - 1)
            full = _state["remaining"] <= 0
        if full:
            try:
                flag_path().unlink()
            except FileNotFoundError:
                pass


# ---------------------------------------------------------------- 请求形状 / 压缩诊断


def capability_summary(payload: dict) -> str:
    """为排查工具/压缩兼容性记录脱敏摘要，不落请求参数或输入内容。"""
    parts: list[str] = []
    tools = payload.get("tools")
    if isinstance(tools, list):
        names = []
        for item in tools[:16]:
            if isinstance(item, dict):
                names.append(str(item.get("type") or item.get("name") or "?"))
            else:
                names.append("?")
        suffix = ",".join(names)
        if len(tools) > 16:
            suffix += ",..."
        parts.append(f"tools={len(tools)}[{suffix}]")
    if "context_management" in payload:
        parts.append("context_management=present")
    return (" " + " ".join(parts)) if parts else ""


def _input_item_census(payload: dict) -> tuple[dict[str, int], dict[str, int]]:
    """数一遍 input 里各 item 的 type 和「type:role」。

    为什么连 role 一起数：Codex Desktop 的 "responses lite" 线格式不发顶层 `instructions`，
    系统提示词是 `role: "developer"` 的 message item —— 只看 type 会以为「全是普通 message」，
    而 role 一旦原样透传就会被上游 422。抓形状时就该看见它。
    """
    input_value = payload.get("input")
    input_items = input_value if isinstance(input_value, list) else [input_value]
    types: dict[str, int] = {}
    roles: dict[str, int] = {}
    for item in input_items:
        if isinstance(item, dict):
            item_type = str(item.get("type", "?"))
            role = item.get("role")
            if isinstance(role, str) and role:
                key = f"{item_type}:{role}"
                roles[key] = roles.get(key, 0) + 1
        else:
            item_type = type(item).__name__
        types[item_type] = types.get(item_type, 0) + 1
    return types, roles


def _tool_brief(value: object, depth: int = 0) -> object:
    """工具形状摘要：递归保留结构，长字符串截断 —— 抓包是为了看形状，不是存内容。"""
    if isinstance(value, Mapping):
        if depth > 4:
            return "..."
        return {
            str(key): _tool_brief(item, depth + 1)
            for key, item in value.items()
            if key not in ("parameters",) or depth < 2
        }
    if isinstance(value, list):
        return [_tool_brief(item, depth + 1) for item in value[:8]] + (
            ["..."] if len(value) > 8 else []
        )
    if isinstance(value, str):
        return value[:160] + f"...({len(value)}B)" if len(value) > 160 else value
    return value


def maybe_capture_headers(request: Request, payload: dict, body_len: int) -> None:
    """调试用：放一个 data/capture.flag，下一请求的头和形状会被脱敏记录。"""
    flag = config.DATA_DIR / "capture.flag"
    if not flag.exists():
        return
    dump = {
        k: ("<redacted>" if is_secret_header(k) else v)
        for k, v in request.headers.items()
    }
    input_types, input_roles = _input_item_census(payload)
    # 工具连容器一起记：`namespace` 这个坑就是抓包只记个数才漏掉的
    tool_briefs = _tool_brief(payload.get("tools") or [])
    extra_tools = [
        item.get("tools")
        for item in (payload.get("input") if isinstance(payload.get("input"), list) else [])
        if isinstance(item, Mapping) and item.get("type") == "additional_tools"
    ]
    shape = {
        "path": request.url.path,
        "body_bytes": body_len,
        "top_level_keys": sorted(str(key) for key in payload),
        "input_item_types": input_types,
        # 连 role 一起记：`developer` 是从这里看出来的
        "input_item_roles": input_roles,
        "tools_count": len(payload.get("tools")) if isinstance(payload.get("tools"), list) else 0,
        "tools_shape": tool_briefs,
        "additional_tools_shape": [_tool_brief(tools) for tools in extra_tools if tools is not None],
        "has_context_management": "context_management" in payload,
        "content_encoding": request.headers.get("content-encoding", ""),
        "codex_beta_features": request.headers.get("x-codex-beta-features", ""),
    }
    try:
        (config.DATA_DIR / "captured_headers.json").write_text(
            json.dumps(dump, indent=2, ensure_ascii=False), encoding="utf-8"
        )
        (config.DATA_DIR / "captured_request_shape.json").write_text(
            json.dumps(shape, indent=2, ensure_ascii=False), encoding="utf-8"
        )
        flag.unlink()
        log("captured real client headers and request shape to data/captured_*.json")
    except OSError:
        pass


def compaction_requested(path: str) -> bool:
    """Whether the one-shot compaction observation flag is armed."""
    return (path.endswith("/responses") or path.endswith("/responses/compact")) and (
        config.DATA_DIR / "compaction_capture.flag"
    ).exists()


def _has_compaction_trigger(payload: dict) -> bool:
    input_items = payload.get("input")
    if not isinstance(input_items, list):
        return False
    return any(
        isinstance(item, dict) and item.get("type") == "compaction_trigger"
        for item in input_items
    )


def _probe_request_shape(payload: dict) -> dict[str, object]:
    """Return only the request shape needed to diagnose client-side compaction."""
    input_types, _ = _input_item_census(payload)
    return {
        "top_level_keys": sorted(str(key) for key in payload),
        "input_item_types": input_types,
        "tools_count": len(payload.get("tools")) if isinstance(payload.get("tools"), list) else 0,
        "has_context_management": "context_management" in payload,
        "has_compaction_trigger": _has_compaction_trigger(payload),
    }


def response_types(value: object, counts: dict[str, int] | None = None) -> dict[str, int]:
    """Count JSON ``type`` fields without retaining response content."""
    result = counts if counts is not None else {}
    if isinstance(value, dict):
        kind = value.get("type")
        if isinstance(kind, str):
            result[kind] = result.get(kind, 0) + 1
        for child in value.values():
            response_types(child, result)
    elif isinstance(value, list):
        for child in value:
            response_types(child, result)
    return result


def write_compaction_capture(
    *,
    request: Request,
    payload: dict,
    route: db.Route,
    remote_model: str,
    status: int,
    stream: bool,
    req_bytes: int,
    resp_bytes: int,
    elapsed: float,
    observations: list[dict[str, object]],
    response_event_types: dict[str, int] | None = None,
    response_payload_types: dict[str, int] | None = None,
) -> None:
    """Write a count-only probe record, including negative evidence.

    Keep the flag armed after an ordinary response so a later automatic
    compaction can still be captured. A positive record is never overwritten by
    subsequent ordinary turns.
    """
    flag = config.DATA_DIR / "compaction_capture.flag"
    if not flag.exists():
        return
    capture_path = config.DATA_DIR / "compaction_capture.json"
    if not observations and capture_path.exists():
        try:
            old = json.loads(capture_path.read_text(encoding="utf-8"))
            if isinstance(old, dict) and old.get("found") is True:
                return
        except (OSError, ValueError, TypeError):
            pass
    capture = {
        "captured_at_unix": time.time(),
        "path": request.url.path,
        "model": str(payload.get("model") or ""),
        "remote_model": remote_model,
        "upstream": route.upstream.name,
        "group": route.group_name,
        "status": status,
        "stream": stream,
        "request_bytes": req_bytes,
        "response_bytes": resp_bytes,
        "duration_ms": int(elapsed * 1000),
        "found": bool(observations),
        "x_codex_beta_features": request.headers.get("x-codex-beta-features", ""),
        "request_shape": _probe_request_shape(payload),
        "response_event_types": response_event_types or {},
        "response_payload_types": response_payload_types or {},
        "observations": observations,
    }
    try:
        capture_path.write_text(
            json.dumps(capture, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        if observations:
            flag.unlink(missing_ok=True)
            log(f"  compaction capture: {len(observations)} item event(s), encrypted lengths only; saved data/compaction_capture.json")
        else:
            log("  compaction probe: no compaction item in this response; flag remains armed")
    except OSError as exc:
        log(f"  compaction capture write failed: {exc.__class__.__name__}: {exc}")
