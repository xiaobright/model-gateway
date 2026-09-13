from __future__ import annotations

import asyncio
import json
import time
import urllib.parse
from collections import defaultdict
from typing import Annotated, Any, Iterable, Literal

import httpx
from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from . import capture, db, failover, inflight, protocols, proxy as proxy_mod, rewrite
from . import stats as stats_mod
from . import upstream as upstream_mod
from .reqlog import log

router = APIRouter(prefix="/admin/api")

# 只含空白的名字过得了 min_length=1，但落库后是空串 —— 在入口就 strip 再查长度
_NonBlank = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]


def _validate_base_url(raw: str) -> str:
    """站根必须是 http(s)://主机[:端口]；坏地址在保存时就拒绝，别攒到第一个请求。"""
    value = raw.strip()
    parts = urllib.parse.urlsplit(value)
    if parts.scheme not in ("http", "https") or not parts.netloc:
        raise HTTPException(400, f"站根要填 http(s)://主机[:端口]（收到 {value!r}）")
    return value


class UpstreamIn(BaseModel):
    """供应商只管「站在哪、怎么连」。key 和接口都在分组里。"""

    name: _NonBlank
    base_url: _NonBlank
    enabled: bool = True
    header_override: str = ""
    # 从哪扇门出去：'' 跟随系统代理 / 'direct' 直连 / 一个代理 URL
    # PUT 省略时保留已有出口，兼容列表快捷开关等只更新启用状态的调用方。
    egress: str | None = None
    # 同站重试：JSON 数组 [{"status":400,"times":2,"delay_ms":0}, ...]；空串 = 关
    retry_rules: str = ""


class GroupIn(BaseModel):
    name: _NonBlank
    protocol: str = Field(min_length=1)
    # 省略 / None = 保留库里已有的 key（列表接口只回脱敏形状，编辑弹窗取回原文后照常传）。
    api_key: str | None = None
    enabled: bool = True
    # PUT 时传了就是把这个分组搬到另一个供应商下（同一个站建成了两个供应商时用来合并）
    upstream_id: int | None = None


class EnabledIn(BaseModel):
    """列表上的快捷开关：只动启用状态，不拿内存里的整份快照覆盖别的字段。"""

    enabled: bool


class ModelRouteIn(BaseModel):
    """新增候选：把某个模型挂到某个分组上。

    同一个分组下可以挂同一个模型名的多条候选，只要各指一个不同的上游真名，
    所以「哪一条」在别的接口里用 route_id 指，不能再用 group_id。
    """

    model_name: _NonBlank
    group_id: int
    remote_model: str = ""


class RouteEditIn(BaseModel):
    route_id: int
    remote_model: str = ""


class RouteTransferIn(BaseModel):
    source_model_name: _NonBlank
    target_model_name: _NonBlank
    route_ids: tuple[int, ...] = Field(min_length=1)
    mode: Literal["copy", "move"] = "copy"


class BulkAddIn(BaseModel):
    group_id: int
    model_names: tuple[str, ...] = Field(min_length=1)


class GroupModelsIn(BaseModel):
    """登记上游模型（只进目录，不产生下游候选）。"""

    model_names: tuple[str, ...] = Field(min_length=1)


class SwitchIn(BaseModel):
    route_id: int


class OrderIn(BaseModel):
    model_name: _NonBlank
    # 自动降级依次尝试的顺序，从先到后。元素是候选 id
    order: tuple[int, ...] = Field(min_length=1)


class FailoverIn(BaseModel):
    protocol: str = Field(min_length=1)
    enabled: bool


class ProtocolSwitchIn(BaseModel):
    """全局停用 / 启用一种接口。停用后它像不存在一样：隐藏、拒绝转发，但配置都留着。"""

    protocol: str = Field(min_length=1)
    enabled: bool


class StandaloneSearchTargetIn(BaseModel):
    # None means restore the normal per-model candidate chain.
    group_id: int | None = Field(default=None, gt=0)
    # Optional model sent to the search-only upstream. This is useful when the
    # search provider exposes only a subset of the models exposed downstream.
    model: str | None = Field(default=None, min_length=1)


class CloneIn(BaseModel):
    # 兼容当前只有两种接口时的无请求体调用；有多个目标时必须明确指定。
    protocol: str | None = None


class RewriteRuleIn(BaseModel):
    """一条绕行规则。`from` 是 Python 关键字，所以字段名叫 from_、对外仍叫 from。"""

    model_config = ConfigDict(populate_by_name=True)

    from_: str = Field(alias="from", min_length=1)
    # 留空 = 把那段删掉
    to: str = ""


class RewriteRulesIn(BaseModel):
    rules: list[RewriteRuleIn] = Field(default_factory=list)


class CaptureStreamIn(BaseModel):
    """热开关抓包。max 是「抓满几条自动关」，不是字节上限。"""

    enabled: bool = True
    max: int = Field(default=1, ge=1, le=50)


def _validate_protocol(protocol: str) -> str:
    clean = protocol.strip().lower()
    if clean not in protocols.NAMES:
        raise HTTPException(400, f"接口只能是 {' / '.join(protocols.NAMES)}")
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


def _validate_retry_rules(raw: str) -> str:
    """同站重试规则：[{status, times, delay_ms}, ...]。空串 = 不配。"""
    if not raw.strip():
        return ""
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise HTTPException(400, f"同站重试不是合法 JSON: {exc}") from exc
    if not isinstance(parsed, list):
        raise HTTPException(400, '同站重试必须是数组，例如 [{"status":503,"times":2}]')
    out: list[dict[str, int]] = []
    for item in parsed:
        if not isinstance(item, dict) or "status" not in item:
            raise HTTPException(400, "每条规则都要有 status，例如 {\"status\":503,\"times\":2}")
        try:
            status = int(item["status"])
            times = int(item.get("times", 2))
            delay_ms = int(item.get("delay_ms", 0))
        except (TypeError, ValueError) as exc:
            raise HTTPException(400, "status / times / delay_ms 必须是整数") from exc
        if not (100 <= status <= 599):
            raise HTTPException(400, f"status 只能在 100–599（收到 {status}）")
        if not (1 <= times <= failover.SAME_RETRY_MAX_TIMES):
            raise HTTPException(
                400, f"times 只能在 1–{failover.SAME_RETRY_MAX_TIMES}（收到 {times}）"
            )
        if delay_ms < 0 or delay_ms > 30_000:
            raise HTTPException(400, "delay_ms 只能在 0–30000 之间")
        out.append({"status": status, "times": times, "delay_ms": delay_ms})
    return json.dumps(out, separators=(",", ":"))


def _validate_egress(raw: str) -> str:
    """出口：'' 跟随系统 / 'direct' 直连 / 一个代理 URL。

    只认 http(s) 和 socks5 —— 网关是拿 httpx 直接拨号的，别的协议（vless/ss 那种）
    得有个内核在中间翻译，填进来只会在转发时才炸。socks5 还要装 socksio，
    这里就先说清楚，免得攒到第一个请求失败才发现。

    https 代理还能带 `#ca=<pem 路径>`（自签证书的那扇门，比如 VPS 上的 gost）：
    片段当场查 —— 语法不对、socks5 没有证书可验、文件不在，都在保存时说清楚，
    别攒到第一个请求失败才发现。
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
    # 语法和文件在不在都交给 upstream.ca_context 查 —— 转发时撞上的是同一段代码，
    # 所以「保存时被拒」和「请求时降级」说的是同一件事，不会出现两套规则
    _, frag = upstream_mod.split_ca(egress)
    if frag:
        if scheme != "https":
            raise HTTPException(400, "#ca= 只有 https 代理用得上 —— socks5/http 的门没有要验的证书")
        try:
            upstream_mod.ca_context(frag)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
    return egress


def _mask_key(key: str) -> str:
    """列表里只给「前几位 + 长度」：能认出是哪把 key，又不用把凭据发给浏览器。"""
    key = (key or "").strip()
    if not key:
        return ""
    if len(key) <= 8:
        return f"… ({len(key)} 位)"
    return f"{key[:6]}… ({len(key)} 位)"


def _mask_userinfo(url: str) -> str:
    """代理 URL 里的 user:pass 换成 user:***；没凭据的原样返回（主机端口是排障要看的）。"""
    raw = (url or "").strip()
    if "://" not in raw or "@" not in raw:
        return raw
    scheme, rest = raw.split("://", 1)
    userinfo, _, host = rest.rpartition("@")
    user = userinfo.partition(":")[0]
    return f"{scheme}://{user}:***@{host}"


def _vps_preset_raw() -> str:
    return (db.get_setting("egress_vps", "") or "").strip()


def _egress_kind(value: str) -> str:
    """前端编辑弹窗靠它决定「跟随系统 / 直连 / VPS 预设 / 自填代理」。
    别让前端拿 raw 值去比对预设 —— 那等于把代理凭据又发下去了。"""
    raw = (value or "").strip()
    if not raw or raw == proxy_mod.EGRESS_SYSTEM:
        return "system"
    if raw == proxy_mod.EGRESS_DIRECT:
        return "direct"
    if raw and raw == _vps_preset_raw():
        return "vps"
    return "proxy"


def _serialize_group(g: db.Group, models: Iterable[str] = ()) -> dict[str, Any]:
    return {
        "id": g.id,
        "upstream_id": g.upstream_id,
        "name": g.name,
        "protocol": g.protocol,
        # key 不进列表接口：脱敏给一眼能认出的形状，「编辑」时再按需取回
        "key_masked": _mask_key(g.api_key),
        "has_key": bool(g.api_key.strip()),
        "enabled": g.enabled,
        # 上游模型目录：这个分组登记过、能调到的上游真名。和「下游暴露」是两回事，
        # 前端用它画「已录入模型」和分组弹窗里的勾选列表。
        "models": list(models),
    }


def _serialize_upstream(
    u: db.Upstream, groups: list[db.Group], models_by_group: dict[int, tuple[str, ...]] | None = None
) -> dict[str, Any]:
    """分组一起带出来：前端的可展开行和「供应商 → 分组」两级选择器都要用，省一次往返。
    supports 是分组接口的去重，前端拿它过滤「这个模型能选哪些供应商」。"""
    models_by_group = models_by_group or {}
    return {
        "id": u.id,
        "name": u.name,
        "base_url": u.base_url,
        "enabled": u.enabled,
        "header_override": u.header_override,
        # 出口同样脱敏：kind 给前端选控件，masked 只用于展示，编辑时按需取原文
        "egress_kind": _egress_kind(u.egress),
        "egress_masked": _mask_userinfo(u.egress),
        "retry_rules": u.retry_rules,
        "supports": [p for p in protocols.NAMES if any(g.protocol == p for g in groups)],
        "groups": [_serialize_group(g, models_by_group.get(g.id, ())) for g in groups],
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
    disabled = db.disabled_protocols()
    groups = [g for g in db.list_groups(upstream_id) if g.protocol not in disabled]
    return _serialize_upstream(_require_upstream(upstream_id), groups, db.all_group_models())


# ---------------------------------------------------------------- 供应商


@router.get("/upstreams")
def get_upstreams() -> list[dict[str, Any]]:
    disabled = db.disabled_protocols()
    by_upstream: dict[int, list[db.Group]] = defaultdict(list)
    for group in db.list_groups():
        by_upstream[group.upstream_id].append(group)
    models_by_group = db.all_group_models()
    out = []
    for u in db.list_upstreams():
        groups = by_upstream[u.id]
        visible = [g for g in groups if g.protocol not in disabled]
        # 只有被停用协议分组的站整个藏起来；没有分组的站照旧显示（还要建第一个分组）
        if groups and not visible:
            continue
        out.append(_serialize_upstream(u, visible, models_by_group))
    return out


@router.get("/protocols")
def get_protocols() -> dict[str, list[dict[str, object]]]:
    """前端用的只读协议投影，不暴露描述符里的函数、key 或上游配置。"""
    return {"protocols": protocols.public_metadata()}


def _protocol_switches() -> dict[str, dict[str, bool]]:
    disabled = db.disabled_protocols()
    return {"enabled": {p: p not in disabled for p in protocols.NAMES}}


@router.get("/protocol-switches")
def get_protocol_switches() -> dict[str, dict[str, bool]]:
    """每种接口的全局开关状态。停用只是隐藏 + 拒绝转发，描述符和配置都还在。"""
    return _protocol_switches()


@router.post("/protocol-switches")
def post_protocol_switch(payload: ProtocolSwitchIn) -> dict[str, dict[str, bool]]:
    protocol = _validate_protocol(payload.protocol)
    db.set_protocol_enabled(protocol, payload.enabled)
    log(f"PROTOCOL {protocol} -> {'enabled' if payload.enabled else 'disabled'}")
    return _protocol_switches()


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
            _validate_base_url(payload.base_url),
            _validate_override(payload.header_override),
            payload.enabled,
            _validate_egress(payload.egress or ""),
            _validate_retry_rules(payload.retry_rules),
        )
    except db.DuplicateName as exc:
        raise HTTPException(409, f"已有同名供应商「{name}」") from exc
    except db.DuplicateBaseUrl as exc:
        raise HTTPException(409, _DUP_BASE.format(exc.args[0])) from exc
    return _one_upstream(created.id)


@router.put("/upstreams/{upstream_id}")
def put_upstream(upstream_id: int, payload: UpstreamIn) -> dict[str, Any]:
    name = payload.name.strip()
    current = _require_upstream(upstream_id)
    egress = current.egress if payload.egress is None else _validate_egress(payload.egress)
    try:
        ok = db.update_upstream(
            upstream_id,
            name,
            _validate_base_url(payload.base_url),
            payload.enabled,
            _validate_override(payload.header_override),
            egress,
            _validate_retry_rules(payload.retry_rules),
        )
    except db.DuplicateName as exc:
        raise HTTPException(409, f"已有同名供应商「{name}」") from exc
    except db.DuplicateBaseUrl as exc:
        raise HTTPException(409, _DUP_BASE.format(exc.args[0])) from exc
    if not ok:
        raise HTTPException(404, f"供应商 {upstream_id} 不存在")
    return _one_upstream(upstream_id)


@router.get("/upstreams/{upstream_id}/egress")
def reveal_upstream_egress(upstream_id: int) -> dict[str, str]:
    """编辑供应商时取回完整出口（含代理凭据）：列表里只回脱敏形状。"""
    return {"egress": _require_upstream(upstream_id).egress}


@router.post("/upstreams/{upstream_id}/enabled")
def set_upstream_enabled(upstream_id: int, payload: EnabledIn) -> dict[str, bool]:
    if not db.set_upstream_enabled(upstream_id, payload.enabled):
        raise HTTPException(404, f"供应商 {upstream_id} 不存在")
    return {"ok": True}


@router.delete("/upstreams/{upstream_id}")
def remove_upstream(upstream_id: int) -> dict[str, bool]:
    # 分组随供应商级联删除；先把它们的断路器状态清掉，不然管理页会一直挂着幽灵条目
    for group in db.list_groups(upstream_id):
        failover.clear(group.id)
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
            "label": label, "ok": False, "status": 0,
            "ms": int((time.monotonic() - began) * 1000),
            "error": str(exc) or exc.__class__.__name__,
        }
    # 拿到任何状态码都算「这扇门能到这个站」。401 也算通 —— 我们问的是网络，不是 key
    return {
        "label": label, "ok": True, "status": resp.status_code,
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


# 「走 VPS」预设。门是部署在 VPS 上的 gost（systemd: gost-proxy.service），对网关来说
# 就是一个带自签证书的 https 代理 —— 值存设置表（egress_vps），代码里不落任何密钥：
# 换端口换密码改一遍设置就行，不用动代码。没配时前端不显示这个选项。
# 列表只给脱敏形状，前端应用这个预设时才调 /egress-presets/vps 取原文。
@router.get("/egress-presets")
def get_egress_presets() -> dict[str, Any]:
    raw = _vps_preset_raw()
    return {"has_vps": bool(raw), "vps_masked": _mask_userinfo(raw) if raw else ""}


@router.get("/egress-presets/vps")
def reveal_egress_preset() -> dict[str, str]:
    raw = _vps_preset_raw()
    if not raw:
        raise HTTPException(404, "还没有配置 VPS 出口")
    return {"vps": raw}


@router.get("/standalone-search-target")
def get_standalone_search_target() -> dict[str, int | str | None]:
    """Read the OpenAI group reserved for Codex standalone Alpha Search."""
    return {
        "group_id": db.standalone_search_target_group_id(),
        "model": db.standalone_search_target_model() or None,
    }


@router.put("/standalone-search-target")
def put_standalone_search_target(payload: StandaloneSearchTargetIn) -> dict[str, int | str | None]:
    """Set the search-only group/model, or clear them with null values."""
    if payload.group_id is not None:
        # Resolve against enabled OpenAI groups now, rather than accepting a
        # typo that would silently make Search return to a normal model route.
        if db.resolve_standalone_search_group(payload.group_id, "search-probe") is None:
            raise HTTPException(400, "搜索专用分组不存在、已停用，或不是 OpenAI 接口")
    db.set_standalone_search_target_group(payload.group_id)
    db.set_standalone_search_target_model(payload.model)
    return {"group_id": payload.group_id, "model": payload.model}


# ---------------------------------------------------------------- 分组


@router.get("/upstreams/{upstream_id}/groups")
def get_groups(upstream_id: int) -> list[dict[str, Any]]:
    _require_upstream(upstream_id)
    disabled = db.disabled_protocols()
    return [
        _serialize_group(g) for g in db.list_groups(upstream_id) if g.protocol not in disabled
    ]


@router.post("/upstreams/{upstream_id}/groups")
def post_group(upstream_id: int, payload: GroupIn) -> dict[str, Any]:
    _require_upstream(upstream_id)
    name = payload.name.strip()
    protocol = _validate_protocol(payload.protocol)
    try:
        created = db.create_group(
            upstream_id, name, protocol, (payload.api_key or "").strip(), payload.enabled
        )
    except db.DuplicateName as exc:
        raise HTTPException(409, f"这个供应商的 {protocol} 接口下已有分组「{name}」") from exc
    return _serialize_group(created)


@router.put("/groups/{group_id}")
def put_group(group_id: int, payload: GroupIn) -> dict[str, Any]:
    current = _require_group(group_id)
    name = payload.name.strip()
    protocol = _validate_protocol(payload.protocol)
    if payload.upstream_id is not None:
        _require_upstream(payload.upstream_id)
    api_key = current.api_key if payload.api_key is None else payload.api_key.strip()
    try:
        ok = db.update_group(
            group_id, name, protocol, api_key, payload.enabled, payload.upstream_id
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


@router.get("/groups/{group_id}/key")
def reveal_group_key(group_id: int) -> dict[str, str]:
    """编辑弹窗按需取回原文 —— 列表接口只回脱敏形状，key 不跟着每次轮询满天飞。"""
    return {"api_key": _require_group(group_id).api_key}


@router.post("/groups/{group_id}/enabled")
def set_group_enabled(group_id: int, payload: EnabledIn) -> dict[str, bool]:
    if not db.set_group_enabled(group_id, payload.enabled):
        raise HTTPException(404, f"分组 {group_id} 不存在")
    return {"ok": True}


@router.post("/groups/{group_id}/clone")
def post_clone_group(group_id: int, payload: CloneIn | None = None) -> dict[str, Any]:
    """把这把 key 复制到另一种接口上。有些站一把 key 两种接口都能用，而接口是分组的属性，
    手动再填一遍 key 很烦。"""
    source = _require_group(group_id)
    other = payload.protocol.strip().lower() if payload and payload.protocol else ""
    if not other:
        choices = [p for p in protocols.NAMES if p != source.protocol]
        if len(choices) != 1:
            raise HTTPException(400, "有多个可复制的目标接口，请明确指定 protocol")
        other = choices[0]
    else:
        other = _validate_protocol(other)
        if other == source.protocol:
            raise HTTPException(400, "复制目标接口必须与来源不同")
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
    failover.clear(group_id)
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
    # 三类失败处置不同，顺序也就不能随便排：ValueError 是**出口配坏了**（#ca 指的文件
    # 不在之类），属于自己这边的配置错，不是上游的锅，所以单独给 400 让它和「站连不上」分开。
    # 以前它和 HTTPError 写在同一条 except 里，结果下面那条 except 的 ValueError 永远走不到
    except ValueError as exc:
        raise HTTPException(400, f"出口配错了: {exc}") from exc
    except httpx.HTTPError as exc:
        # 连不上时 str(exc) 常常是空的（Windows 上 DNS 失败尤其如此），只写「拉取失败:」
        # 没法排查，所以补上异常类型和实际请求的那个地址
        why = str(exc) or exc.__class__.__name__
        raise HTTPException(502, f"拉取失败: {why}（{upstream_mod.models_url(parent.base_url)}）") from exc
    except RuntimeError as exc:
        # fetch_remote_models 自己抛的（非 200 / 不是 JSON / 没有 data 数组），消息里已经带了地址
        raise HTTPException(502, f"拉取失败: {exc}") from exc
    return {"models": list(models)}


# 上游模型目录：登记 / 移除这个分组能调到的上游真名。它和下面的「模型路由」分开 ——
# 拉一份模型列表只回答「这个站有什么」，要不要对下游暴露由模型路由决定。
@router.post("/groups/{group_id}/models")
def post_group_models(group_id: int, payload: GroupModelsIn) -> dict[str, Any]:
    _require_group(group_id)
    added = db.add_group_models(group_id, payload.model_names)
    return {"added": added, "models": list(db.list_group_models(group_id))}


@router.delete("/groups/{group_id}/models")
def remove_group_model(
    group_id: int, remote_model: str = Query(min_length=1, description="上游那边的真实模型名")
) -> dict[str, Any]:
    _require_group(group_id)
    removed, routes = db.delete_group_model(group_id, remote_model)
    if not removed:
        raise HTTPException(404, f"这个分组的目录里没有「{remote_model}」")
    if routes:
        log(f"CATALOG remove {remote_model!r} from group {group_id}: {routes} downstream route(s) dropped")
    return {"ok": True, "removed": removed, "routes_removed": routes}


# ---------------------------------------------------------------- 模型路由


@router.get("/models")
def get_model_routes() -> list[dict[str, Any]]:
    disabled = db.disabled_protocols()
    cooling = {b["group_id"]: b for b in failover.snapshot()}
    grouped: dict[tuple[str, str], dict[str, Any]] = {}
    for row in db.list_routes():
        if row["protocol"] in disabled:
            continue
        key = (row["model_name"], row["protocol"])
        group = grouped.setdefault(
            key,
            {
                "model_name": row["model_name"],
                # 同名模型可以在多种接口下各挂一条链（转发按「模型名 + 请求接口」选链，
                # 两条链互不可见），所以一行 = 一条链，接口是它自己的属性
                "protocol": row["protocol"],
                "candidates": [],
                # 保存的首选和当前实际起点分开：停用首选后，自动降级仍可能有可用候选。
                "preferred_route_id": None,
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
            group["preferred_route_id"] = row["route_id"]

    # 和 proxy.forward 使用同一条 resolve_chain + 断路器排序规则，返回当前真正会先
    # 尝试的候选。这里不能只看 is_active 那一行，否则首选停用时页面会误报无可用上游。
    for group in grouped.values():
        chain = db.resolve_chain(group["model_name"], group["protocol"])
        if chain:
            ordered = failover.order_chain(chain) if failover.enabled(group["protocol"]) else chain
            group["active_route_id"] = ordered[0].route_id
    return sorted(grouped.values(), key=lambda g: (g["model_name"], g["protocol"]))


@router.post("/models")
def post_model_route(payload: ModelRouteIn) -> dict[str, Any]:
    group = _require_group(payload.group_id)
    model_name = payload.model_name.strip()
    remote_model = payload.remote_model.strip() or model_name
    route_id = db.add_model_route(model_name, payload.group_id, remote_model)
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


@router.post("/models/transfer")
def transfer_model_routes(payload: RouteTransferIn) -> dict[str, Any]:
    try:
        result = db.transfer_model_routes(
            payload.source_model_name, payload.target_model_name, payload.route_ids, payload.mode
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    except db.RouteTransferConflict as exc:
        raise HTTPException(409, str(exc)) from exc
    log(
        f"TRANSFER mode={payload.mode} from={payload.source_model_name!r}"
        f" to={result['model_name']!r} added={result['added']} merged={result['merged']}"
    )
    return result


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


@router.post("/inflight/{call_id}/cancel")
async def cancel_inflight(call_id: int) -> dict[str, bool]:
    # 取消 Future 必须在转发所在的事件循环里执行，不能用同步端点的线程池。
    return {"ok": True, "cancelled": inflight.cancel(call_id)}


# ---------------------------------------------------------------- 抓包
# 「下游说截断、上游说正常」这类问题，光看转发记录永远差最后一步：上游到底吐了什么字节。
# 这里开一个热开关 —— 不用改代码、不用重启，开一下复现一次，原始 SSE 就躺在
# data/captured_stream/<时间戳>-<模型>/ 里。开关本质是 data/capture-stream.flag 文件，
# 所以手搓文件、直接删文件、走 API 三种方式等价，删了立刻停。


@router.get("/capture-stream")
def get_capture_stream() -> dict[str, Any]:
    return capture.status()


@router.put("/capture-stream")
def put_capture_stream(payload: CaptureStreamIn) -> dict[str, Any]:
    if not payload.enabled:
        return capture.disable()
    return capture.enable(payload.max)


@router.get("/capture-stream/list")
def get_capture_stream_list() -> dict[str, Any]:
    """列出已经抓到的目录，最新的在前 —— 抓完直接从这拿路径。"""
    root = capture.out_root()
    if not root.exists():
        return {"items": []}
    items = []
    for d in sorted(root.iterdir(), reverse=True):
        if not d.is_dir():
            continue
        meta: dict[str, Any] = {}
        try:
            meta = json.loads((d / "meta.json").read_text("utf-8"))
        except Exception:
            pass
        items.append(
            {
                "name": d.name,
                "path": str(d),
                "stream_bytes": meta.get("stream_bytes"),
                "status": meta.get("status"),
                "note": meta.get("note"),
                "model": meta.get("model"),
                "upstream": meta.get("upstream"),
            }
        )
    return {"items": items[:50], "root": str(root)}


# ---------------------------------------------------------------- 请求改写
# 上游的内容审核是黑盒，会误伤固定提示词（opencode 的标题生成器每轮必中
# sensitive_words_detected，而上游不会告诉你命中了哪个词）。网关猜不出来，所以只能
# 「踩到一条配一条」：转发前按规则表对请求体文本做字面替换。默认空表 = 一个字节都不改。


@router.get("/rewrite-rules")
def get_rewrite_rules() -> dict[str, Any]:
    return {"rules": rewrite.rules()}


@router.put("/rewrite-rules")
def put_rewrite_rules(payload: RewriteRulesIn) -> dict[str, Any]:
    if len(payload.rules) > rewrite.MAX_RULES:
        raise HTTPException(400, f"最多 {rewrite.MAX_RULES} 条规则")
    items = [{"from": r.from_, "to": r.to} for r in payload.rules]
    return {"rules": rewrite.save(items)}


# ---------------------------------------------------------------- 自动降级


@router.get("/failover")
def get_failover() -> dict[str, Any]:
    return {"enabled": failover.all_enabled(), "breakers": failover.snapshot()}


@router.post("/failover")
def post_failover(payload: FailoverIn) -> dict[str, Any]:
    protocol = _validate_protocol(payload.protocol)
    failover.set_enabled(protocol, payload.enabled)
    return {"enabled": failover.all_enabled()}


# 上游发呆超时：全局设置（不分接口）。卡住时按「手动打断」处理，让下游重发，
# 不走自动降级 —— 见 proxy.forward 里的收尾注释。
class StallTimeoutIn(BaseModel):
    seconds: float = Field(ge=0, le=3600)


@router.get("/stall-timeout")
def get_stall_timeout() -> dict[str, float]:
    return {"seconds": proxy_mod.stall_timeout()}


@router.put("/stall-timeout")
def put_stall_timeout(payload: StallTimeoutIn) -> dict[str, float]:
    return {"seconds": proxy_mod.set_stall_timeout(payload.seconds)}


@router.delete("/models")
def remove_model_route(
    model_name: str = Query(default="", description="模型名"),
    group_id: int | None = Query(default=None, description="只删这个模型在该分组下的候选（可能有多条）"),
    route_id: int | None = Query(default=None, description="只删这一条候选；给了它就不看前两个参数"),
    protocol: str = Query(default="", description="只删这个接口下的链；同名模型在其它接口下的候选保留"),
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
        removed = db.delete_model(model_name, protocol)
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
    # 停用的接口像不存在一样，历史记录也一起藏 —— 过滤放 SQL 里，别让它们占 limit 配额
    return list(db.recent_requests(max(1, min(limit, 200)), db.disabled_protocols()))


@router.delete("/requests")
def clear_requests() -> dict[str, Any]:
    removed = db.clear_request_log()
    log(f"CLEAR request_log ({removed} rows)")
    return {"ok": True, "removed": removed}


@router.get("/stats")
def get_stats() -> dict[str, Any]:
    stats = stats_mod.request_stats()
    return {
        **stats,
        "cache_hit_rate": stats_mod.cache_hit_rate(stats),
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
