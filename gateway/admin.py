from __future__ import annotations

import asyncio
import json
import time
from collections import defaultdict
from typing import Any

import httpx
from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from . import db, failover, inflight, proxy as proxy_mod, stats as stats_mod, upstream as upstream_mod
from .reqlog import log

router = APIRouter(prefix="/admin/api")


class UpstreamIn(BaseModel):
    """供应商只管「站在哪、怎么连」。key 和接口都在分组里。"""

    name: str = Field(min_length=1)
    base_url: str = Field(min_length=1)
    enabled: bool = True
    header_override: str = ""
    # 从哪扇门出去：'' 跟随系统代理 / 'direct' 直连 / 一个代理 URL
    egress: str = ""


class GroupIn(BaseModel):
    name: str = Field(min_length=1)
    protocol: str = Field(min_length=1)
    api_key: str = ""
    enabled: bool = True
    # PUT 时传了就是把这个分组搬到另一个供应商下（同一个站建成了两个供应商时用来合并）
    upstream_id: int | None = None


class ModelRouteIn(BaseModel):
    """新增候选：把某个模型挂到某个分组上。

    同一个分组下可以挂同一个模型名的多条候选，只要各指一个不同的上游真名，
    所以「哪一条」在别的接口里用 route_id 指，不能再用 group_id。
    """

    model_name: str = Field(min_length=1)
    group_id: int
    remote_model: str = ""


class RouteEditIn(BaseModel):
    route_id: int
    remote_model: str = ""


class BulkAddIn(BaseModel):
    group_id: int
    model_names: tuple[str, ...] = Field(min_length=1)


class SwitchIn(BaseModel):
    route_id: int


class OrderIn(BaseModel):
    model_name: str = Field(min_length=1)
    # 自动降级依次尝试的顺序，从先到后。元素是候选 id
    order: tuple[int, ...] = Field(min_length=1)


class FailoverIn(BaseModel):
    protocol: str = Field(min_length=1)
    enabled: bool


def _validate_protocol(protocol: str) -> str:
    clean = protocol.strip().lower()
    if clean not in db.PROTOCOLS:
        raise HTTPException(400, f"接口只能是 {' / '.join(db.PROTOCOLS)}")
    return clean


def _validate_override(raw: str) -> str:
    if not raw.strip():
        return ""
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise HTTPException(400, f"请求头覆写不是合法 JSON: {exc}") from exc
    if not isinstance(parsed, dict) or any(not isinstance(v, (str, type(None))) for v in parsed.values()):
        raise HTTPException(400, '请求头覆写必须是 {"头名": "值"} 形式，值为 null 表示删除该头')
    return raw.strip()


def _validate_egress(raw: str) -> str:
    """出口：'' 跟随系统 / 'direct' 直连 / 一个代理 URL。

    只认 http(s) 和 socks5 —— 网关是拿 httpx 直接拨号的，别的协议（vless/ss 那种）
    得有个内核在中间翻译，填进来只会在转发时才炸。socks5 还要装 socksio，
    这里就先说清楚，免得攒到第一个请求失败才发现。
    """
    egress = (raw or "").strip()
    if egress in (proxy_mod.EGRESS_SYSTEM, proxy_mod.EGRESS_DIRECT):
        return egress
    scheme = egress.split("://", 1)[0].lower() if "://" in egress else ""
    if scheme not in ("http", "https", "socks5", "socks5h"):
        raise HTTPException(
            400,
            f"出口只能填 http:// 或 socks5:// 的代理地址（收到 {egress!r}）。"
            "vless / shadowsocks 这类得先由本机的代理内核落成一个 http/socks 端口",
        )
    if scheme.startswith("socks5"):
        try:
            import socksio  # noqa: F401
        except ImportError as exc:
            raise HTTPException(
                400, "要用 socks5 代理得先装 socksio：.venv\\Scripts\\python -m pip install socksio"
            ) from exc
    return egress


def _serialize_group(g: db.Group) -> dict[str, Any]:
    return {
        "id": g.id,
        "upstream_id": g.upstream_id,
        "name": g.name,
        "protocol": g.protocol,
        "api_key": g.api_key,
        "enabled": g.enabled,
    }


def _serialize_upstream(u: db.Upstream, groups: list[db.Group]) -> dict[str, Any]:
    """分组一起带出来：前端的可展开行和「供应商 → 分组」两级选择器都要用，省一次往返。
    supports 是分组接口的去重，前端拿它过滤「这个模型能选哪些供应商」。"""
    return {
        "id": u.id,
        "name": u.name,
        "base_url": u.base_url,
        "enabled": u.enabled,
        "header_override": u.header_override,
        "egress": u.egress,
        "supports": [p for p in db.PROTOCOLS if any(g.protocol == p for g in groups)],
        "groups": [_serialize_group(g) for g in groups],
    }


def _require_upstream(upstream_id: int) -> db.Upstream:
    found = db.get_upstream(upstream_id)
    if found is None:
        raise HTTPException(404, f"供应商 {upstream_id} 不存在")
    return found


def _require_group(group_id: int) -> db.Group:
    found = db.get_group(group_id)
    if found is None:
        raise HTTPException(404, f"分组 {group_id} 不存在")
    return found


def _one_upstream(upstream_id: int) -> dict[str, Any]:
    return _serialize_upstream(_require_upstream(upstream_id), list(db.list_groups(upstream_id)))


# ---------------------------------------------------------------- 供应商


@router.get("/upstreams")
def get_upstreams() -> list[dict[str, Any]]:
    by_upstream: dict[int, list[db.Group]] = defaultdict(list)
    for group in db.list_groups():
        by_upstream[group.upstream_id].append(group)
    return [_serialize_upstream(u, by_upstream[u.id]) for u in db.list_upstreams()]


_DUP_BASE = (
    "这个地址已经属于供应商「{0}」了。同一个站的另一把 key、或者另一种接口，"
    "请给它加一个分组"
)


@router.post("/upstreams")
def post_upstream(payload: UpstreamIn) -> dict[str, Any]:
    name = payload.name.strip()
    try:
        created = db.create_upstream(
            name,
            payload.base_url.strip(),
            _validate_override(payload.header_override),
            payload.enabled,
            _validate_egress(payload.egress),
        )
    except db.DuplicateName as exc:
        raise HTTPException(409, f"已有同名供应商「{name}」") from exc
    except db.DuplicateBaseUrl as exc:
        raise HTTPException(409, _DUP_BASE.format(exc.args[0])) from exc
    return _one_upstream(created.id)


@router.put("/upstreams/{upstream_id}")
def put_upstream(upstream_id: int, payload: UpstreamIn) -> dict[str, Any]:
    name = payload.name.strip()
    try:
        ok = db.update_upstream(
            upstream_id,
            name,
            payload.base_url.strip(),
            payload.enabled,
            _validate_override(payload.header_override),
            _validate_egress(payload.egress),
        )
    except db.DuplicateName as exc:
        raise HTTPException(409, f"已有同名供应商「{name}」") from exc
    except db.DuplicateBaseUrl as exc:
        raise HTTPException(409, _DUP_BASE.format(exc.args[0])) from exc
    if not ok:
        raise HTTPException(404, f"供应商 {upstream_id} 不存在")
    return _one_upstream(upstream_id)


@router.delete("/upstreams/{upstream_id}")
def remove_upstream(upstream_id: int) -> dict[str, bool]:
    if not db.delete_upstream(upstream_id):
        raise HTTPException(404, f"供应商 {upstream_id} 不存在")
    return {"ok": True}


# 「测一下」：同一个站从每扇门各打一次，看哪扇能到。
# 这正是出口这套东西要回答的问题 —— 公益站按 IP 屏蔽，而校园网 IP 和机房 IP
# 各自被不同的站拉黑，光靠猜要试很久。它是「实测格式」那一列在网络层的兄弟。
PROBE_TIMEOUT = httpx.Timeout(connect=6.0, read=8.0, write=6.0, pool=8.0)


async def _probe_one(base_url: str, egress: str, label: str, headers: dict[str, str]) -> dict[str, Any]:
    url = upstream_mod.models_url(base_url)
    began = time.monotonic()
    try:
        async with httpx.AsyncClient(
            timeout=PROBE_TIMEOUT, **proxy_mod.client_args(egress)
        ) as client:
            resp = await client.get(url, headers=headers)
    except Exception as exc:  # 探测什么都不该抛：代理地址填错是 ValueError，缺 socksio 是 ImportError
        return {
            "egress": egress, "label": label, "ok": False, "status": 0,
            "ms": int((time.monotonic() - began) * 1000),
            "error": str(exc) or exc.__class__.__name__,
        }
    # 拿到任何状态码都算「这扇门能到这个站」。401 也算通 —— 我们问的是网络，不是 key
    return {
        "egress": egress, "label": label, "ok": True, "status": resp.status_code,
        "ms": int((time.monotonic() - began) * 1000), "error": "",
    }


@router.post("/upstreams/{upstream_id}/probe")
async def probe_upstream(upstream_id: int) -> dict[str, Any]:
    up = _require_upstream(upstream_id)
    groups = list(db.list_groups(upstream_id))
    # 带上第一个分组的 key 和接口：401 也算通，但带上 key 能顺手看出这把 key 还活着
    group = groups[0] if groups else None
    headers = upstream_mod.build_headers(
        group.api_key if group else "",
        up.header_override,
        group.protocol if group else "openai",
    )
    doors = [(proxy_mod.EGRESS_SYSTEM, "跟随系统"), (proxy_mod.EGRESS_DIRECT, "直连")]
    if up.egress not in (proxy_mod.EGRESS_SYSTEM, proxy_mod.EGRESS_DIRECT):
        doors.append((up.egress, "这个代理"))
    # 并发打：一扇被挡住的门要磨满 connect 超时，串行的话三扇门要等三倍
    results = await asyncio.gather(
        *(_probe_one(up.base_url, door, label, headers) for door, label in doors)
    )
    log(
        f"PROBE {up.name}: "
        + "; ".join(
            f"{r['label']}="
            f"{'通 ' + str(r['status']) if r['ok'] else '不通 ' + r['error'][:48]} {r['ms']}ms"
            for r in results
        )
    )
    return {"current": up.egress, "results": list(results)}


# ---------------------------------------------------------------- 分组


@router.get("/upstreams/{upstream_id}/groups")
def get_groups(upstream_id: int) -> list[dict[str, Any]]:
    _require_upstream(upstream_id)
    return [_serialize_group(g) for g in db.list_groups(upstream_id)]


@router.post("/upstreams/{upstream_id}/groups")
def post_group(upstream_id: int, payload: GroupIn) -> dict[str, Any]:
    _require_upstream(upstream_id)
    name = payload.name.strip()
    protocol = _validate_protocol(payload.protocol)
    try:
        created = db.create_group(upstream_id, name, protocol, payload.api_key.strip(), payload.enabled)
    except db.DuplicateName as exc:
        raise HTTPException(409, f"这个供应商的 {protocol} 接口下已有分组「{name}」") from exc
    return _serialize_group(created)


@router.put("/groups/{group_id}")
def put_group(group_id: int, payload: GroupIn) -> dict[str, Any]:
    _require_group(group_id)
    name = payload.name.strip()
    protocol = _validate_protocol(payload.protocol)
    if payload.upstream_id is not None:
        _require_upstream(payload.upstream_id)
    try:
        ok = db.update_group(
            group_id, name, protocol, payload.api_key.strip(), payload.enabled, payload.upstream_id
        )
    except db.DuplicateName as exc:
        raise HTTPException(409, f"目标供应商的 {protocol} 接口下已有分组「{name}」") from exc
    except db.ProtocolLocked as exc:
        raise HTTPException(
            409,
            f"这个分组下已经有 {exc.args[0]} 条模型候选了，不能再改接口 —— "
            "先把候选删掉，或者给另一种接口新建一个分组",
        ) from exc
    if not ok:
        raise HTTPException(404, f"分组 {group_id} 不存在")
    return _serialize_group(_require_group(group_id))


@router.post("/groups/{group_id}/clone")
def post_clone_group(group_id: int) -> dict[str, Any]:
    """把这把 key 复制到另一种接口上。有些站一把 key 两种接口都能用，而接口是分组的属性，
    手动再填一遍 key 很烦。"""
    source = _require_group(group_id)
    other = next(p for p in db.PROTOCOLS if p != source.protocol)
    try:
        created = db.create_group(
            source.upstream_id, source.name, other, source.api_key, source.enabled
        )
    except db.DuplicateName:
        try:
            created = db.create_group(
                source.upstream_id, f"{source.name}-{other}", other, source.api_key, source.enabled
            )
        except db.DuplicateName as exc:
            raise HTTPException(409, f"这个供应商的 {other} 接口下已经有同名分组了") from exc
    return _serialize_group(created)


@router.delete("/groups/{group_id}")
def remove_group(group_id: int) -> dict[str, bool]:
    _require_group(group_id)
    if not db.delete_group(group_id):
        raise HTTPException(404, f"分组 {group_id} 不存在")
    return {"ok": True}


@router.get("/groups/{group_id}/remote-models")
async def get_remote_models(group_id: int) -> dict[str, Any]:
    """模型列表是分组一级的东西：同一个站的两把 key 能看到的模型常常不一样，
    而且鉴权头要按这个分组的接口来发（Anthropic 站认 x-api-key）。"""
    group = _require_group(group_id)
    parent = _require_upstream(group.upstream_id)
    try:
        models = await upstream_mod.fetch_remote_models(
            parent.base_url, group.api_key, parent.header_override, group.protocol, parent.egress
        )
    except httpx.HTTPError as exc:
        # 连不上时 str(exc) 常常是空的（Windows 上 DNS 失败尤其如此），只写「拉取失败:」
        # 没法排查，所以补上异常类型和实际请求的那个地址
        why = str(exc) or exc.__class__.__name__
        raise HTTPException(502, f"拉取失败: {why}（{upstream_mod.models_url(parent.base_url)}）") from exc
    except (ValueError, RuntimeError) as exc:
        # 这一类是 fetch_remote_models 自己抛的，消息里已经带了地址
        raise HTTPException(502, f"拉取失败: {exc}") from exc
    return {"models": list(models)}


# ---------------------------------------------------------------- 模型路由


@router.get("/models")
def get_model_routes() -> list[dict[str, Any]]:
    cooling = {b["group_id"]: b for b in failover.snapshot()}
    grouped: dict[str, dict[str, Any]] = {}
    for row in db.list_routes():
        group = grouped.setdefault(
            row["model_name"],
            {
                "model_name": row["model_name"],
                # 模型在哪个接口下暴露 = 它候选所在分组的接口。理论上所有候选都一样
                # （add_model_route 守着），活跃的那条优先，免得手改过的老库看起来乱跳
                "protocol": row["protocol"],
                "candidates": [],
                "active_route_id": None,
            },
        )
        is_active = bool(row["is_active"])
        upstream_on = bool(row["upstream_enabled"])
        group_on = bool(row["group_enabled"])
        breaker = cooling.get(row["group_id"])
        group["candidates"].append(
            {
                "route_id": row["route_id"],
                "group_id": row["group_id"],
                "group_name": row["group_name"],
                "group_enabled": group_on,
                "protocol": row["protocol"],
                "upstream_id": row["upstream_id"],
                "upstream_name": row["upstream_name"],
                "upstream_enabled": upstream_on,
                "remote_model": row["remote_model"],
                "is_active": is_active,
                # 候选按 priority 排好了；圆片从左到右就是自动降级的尝试顺序
                "priority": row["priority"],
                # 断路器是按**分组**记的（坏的是那个站和那把 key），所以同分组的
                # 几条候选会显示同一个冷却
                "cooling_ms": breaker["cooling_ms"] if breaker else 0,
                "fails": breaker["fails"] if breaker else 0,
            }
        )
        if is_active:
            group["protocol"] = row["protocol"]
            # 供应商和分组都启用才算真的在生效
            if upstream_on and group_on:
                group["active_route_id"] = row["route_id"]
    return sorted(grouped.values(), key=lambda g: g["model_name"])


def _mismatch(model_name: str, exc: db.ProtocolMismatch) -> HTTPException:
    mine, theirs = exc.args
    return HTTPException(
        409,
        f"「{model_name}」已经在 {mine} 接口下暴露了，不能再挂一个 {theirs} 接口的分组 —— "
        "同一个模型名的候选必须都在同一种接口上",
    )


@router.post("/models")
def post_model_route(payload: ModelRouteIn) -> dict[str, Any]:
    group = _require_group(payload.group_id)
    model_name = payload.model_name.strip()
    remote_model = payload.remote_model.strip() or model_name
    try:
        route_id = db.add_model_route(model_name, payload.group_id, remote_model)
    except db.ProtocolMismatch as exc:
        raise _mismatch(model_name, exc) from exc
    if not route_id:
        raise HTTPException(
            409,
            f"这个分组下已经有一条「{model_name}」→「{remote_model}」的候选了 —— "
            "换个上游真名可以再加一条",
        )
    return {
        "route_id": route_id,
        "model_name": model_name,
        "group_id": payload.group_id,
        "remote_model": remote_model,
        "protocol": group.protocol,
    }


@router.put("/models")
def put_model_route(payload: RouteEditIn) -> dict[str, Any]:
    """改一个已有候选的「上游那边的真实模型名」。1M 开关也走这里（存成 `名字[1m]`）。"""
    route = db.get_route(payload.route_id)
    if route is None:
        raise HTTPException(404, "该候选不存在")
    remote_model = payload.remote_model.strip() or route["model_name"]
    try:
        ok = db.update_model_route(payload.route_id, remote_model)
    except db.DuplicateRemote as exc:
        raise HTTPException(
            409,
            f"这个分组下已经有一条映射到「{exc.args[0]}」的候选了 —— 改成它就跟那条重复了",
        ) from exc
    if not ok:
        raise HTTPException(404, "该候选不存在")
    return {
        "route_id": payload.route_id,
        "model_name": route["model_name"],
        "group_id": route["group_id"],
        "remote_model": remote_model,
    }


@router.post("/models/bulk-add")
def post_bulk_add(payload: BulkAddIn) -> dict[str, Any]:
    """批量导入。撞上「已经在另一种接口下暴露」的名字只跳过它，别把整批退回去。"""
    _require_group(payload.group_id)
    added, skipped = db.add_routes_for_group(payload.group_id, payload.model_names)
    return {"added": added, "skipped": list(skipped)}


@router.post("/models/switch")
def post_switch(payload: SwitchIn) -> dict[str, bool]:
    route = db.get_route(payload.route_id)
    if route is None or not db.switch_route(payload.route_id):
        raise HTTPException(404, "切换目标不存在")
    # 手动指定了就立刻给它机会：之前的连续失败不该继续把它挡在外面
    failover.clear(route["group_id"])
    log(
        f"SWITCH model={route['model_name']!r} -> {route['upstream_name']}/{route['group_name']}"
        f" remote={route['remote_model']!r}"
    )
    return {"ok": True}


@router.post("/models/order")
def post_order(payload: OrderIn) -> dict[str, Any]:
    """重排一个模型的候选顺序 = 自动降级依次尝试的顺序。"""
    n = db.set_route_order(payload.model_name, payload.order)
    if n == 0:
        raise HTTPException(404, f"「{payload.model_name}」没有这些候选")
    log(f"ORDER model={payload.model_name!r} -> {list(payload.order)}")
    return {"ok": True, "ordered": n}


# ---------------------------------------------------------------- 实时


@router.get("/inflight")
def get_inflight() -> dict[str, Any]:
    """「实时」那一页要的全部东西，一次拿完 —— 这页 1 秒一刷，不该开三个连接。

    登记表和断路器都是纯内存的。只有 tokens 那份标尺来自数据库（从转发记录里量
    「多少字节摊一个 token」），它在 stats 里按分钟缓存，扫不到每次刷新头上。
    """
    return {
        **inflight.snapshot(),
        "breakers": failover.snapshot(),
        "failover": failover.all_enabled(),
        "tokens": stats_mod.token_ratio(),
    }


# ---------------------------------------------------------------- 自动降级


@router.get("/failover")
def get_failover() -> dict[str, Any]:
    return {"enabled": failover.all_enabled(), "breakers": failover.snapshot()}


@router.post("/failover")
def post_failover(payload: FailoverIn) -> dict[str, Any]:
    protocol = _validate_protocol(payload.protocol)
    failover.set_enabled(protocol, payload.enabled)
    return {"enabled": failover.all_enabled()}


@router.delete("/models")
def remove_model_route(
    model_name: str = Query(default="", description="模型名"),
    group_id: int | None = Query(default=None, description="只删这个模型在该分组下的候选（可能有多条）"),
    route_id: int | None = Query(default=None, description="只删这一条候选；给了它就不看前两个参数"),
) -> dict[str, Any]:
    # 走 query 而不是路径参数：模型名常带 '/'（如 deepseek-ai/DeepSeek-V3），放路径里会被当成多段
    if route_id is not None:
        name = db.delete_model_route(route_id)
        if not name:
            raise HTTPException(404, "该候选不存在")
        return {"ok": True, "removed": 1, "model_name": name}
    if not model_name:
        raise HTTPException(400, "要么给 route_id，要么给 model_name")
    if group_id is None:
        removed = db.delete_model(model_name)
        if removed == 0:
            raise HTTPException(404, f"模型「{model_name}」不存在")
        return {"ok": True, "removed": removed}
    # 分组级：同一个分组下可能挂了这个模型的好几条真名，一起去掉 ——
    # 分组弹窗里那个勾选框答的就是「这个模型在这个分组里有没有」
    removed = db.delete_routes_in_group(model_name, group_id)
    if removed == 0:
        raise HTTPException(404, "该候选不存在")
    return {"ok": True, "removed": removed}


# ---------------------------------------------------------------- 转发记录 / 运维


@router.get("/requests")
def get_requests(limit: int = 50) -> list[dict[str, Any]]:
    return list(db.recent_requests(max(1, min(limit, 200))))


@router.delete("/requests")
def clear_requests() -> dict[str, Any]:
    removed = db.clear_request_log()
    log(f"CLEAR request_log ({removed} rows)")
    return {"ok": True, "removed": removed}


@router.get("/stats")
def get_stats() -> dict[str, Any]:
    stats = db.request_stats()
    total_in = stats["input_tokens"]
    return {
        **stats,
        "cache_hit_rate": round(stats["cached_tokens"] / total_in, 4) if total_in else 0.0,
        "live": stats_mod.live(),
    }


@router.get("/overview")
def get_overview(window: str = "1h", top: int = 8) -> dict[str, Any]:
    """概览视图一次拿全：时间线 + 上游健康 + 模型热度 + 活跃流 + 累计值。"""
    return stats_mod.overview(window, max(1, min(top, 20)))


@router.get("/stats/series")
def get_series(window: str = "1h") -> dict[str, Any]:
    return stats_mod.series(window)


@router.get("/stats/upstreams")
def get_upstream_health() -> list[dict[str, Any]]:
    return stats_mod.upstream_health()


@router.get("/stats/models")
def get_model_top(limit: int = 8) -> list[dict[str, Any]]:
    return stats_mod.model_top(max(1, min(limit, 20)))


@router.post("/shutdown")
def post_shutdown() -> dict[str, bool]:
    from .server import request_shutdown  # 延迟导入：server -> app -> admin 是个环

    if not request_shutdown():
        raise HTTPException(409, "当前模式不支持远程关闭")
    return {"ok": True}
