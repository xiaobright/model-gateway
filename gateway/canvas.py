"""编排画布的位置记忆：只存「哪个节点摆在哪儿」，不存任何路由配置。

画布上的东西全部由 ``/admin/api/models`` 和 ``/admin/api/upstreams`` 推出来 —— 位置是
纯展示状态。所以这里做成一个独立的 settings 条目，**不动 schema**：画布坏了、位置丢了，
只是节点回到自动摆放，转发照旧，绝不会因为一份坐标把路由配置带坏。

节点用字符串键标识，前端和后端各拼各的字符串太容易写歪，所以格式定在这里::

    m|<模型名>|<接口>       下游气泡（一个「模型名 + 接口」= 一条链）
    u|<分组 id>|<上游真名>   上游节点（一个分组上的一个可调模型）

存成 ``{"v": 1, "nodes": {键: [x, y]}, "view": {"x","y","z"}}``。

**写入时顺手剪掉已经不存在的节点**：模型删了、分组没了，它的坐标就是垃圾。剪在写的时候
而不是读的时候 —— 读路径每次开画布都要多查几张表，而写路径本来就在处理一次用户操作，
多一次查询无所谓。剪掉的键将来重新配出来会回到自动摆放，这是可接受的行为。
"""

from __future__ import annotations

import json
import math
import threading
from typing import Any

from . import db

SETTING_KEY = "canvas_layout"
LAYOUT_VERSION = 1

# 上限都按「手滑塞进来的东西」定，正常用量离得很远：几百个模型已经很多了
MAX_NODES = 800
MAX_KEY_LEN = 300
COORD_LIMIT = 200_000.0
ZOOM_MIN, ZOOM_MAX = 0.2, 3.0
# 一份坐标序列化后的体积上限。800 个节点 × 几十字节，离它还很远
MAX_BYTES = 320 * 1024

DOWNSTREAM_PREFIX = "m"
UPSTREAM_PREFIX = "u"

# 布局每次开画布都要读；sqlite 已经被每条请求查过，但没必要把同一份 JSON 反复 parse。
# 和 rewrite.py 一样按原始文本缓存，读在管理接口线程池、写在同一个池里，要加锁。
_cache: dict[str, Any] = {"raw": None, "layout": None}
_lock = threading.Lock()


def model_key(model_name: str, protocol: str) -> str:
    return f"{DOWNSTREAM_PREFIX}|{model_name}|{protocol}"


def upstream_key(group_id: int, remote_model: str) -> str:
    return f"{UPSTREAM_PREFIX}|{group_id}|{remote_model}"


def empty_layout() -> dict[str, Any]:
    return {"v": LAYOUT_VERSION, "nodes": {}, "view": {"x": 0.0, "y": 0.0, "z": 1.0}}


def _coord(value: Any) -> float | None:
    """坐标只收有限数。bool 是 int 的子类，得先挡掉，否则 True 会被当成 1 摆到角上。"""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if not math.isfinite(value) or abs(value) > COORD_LIMIT:
        return None
    return round(float(value), 1)


def _split_key(key: str) -> tuple[str, str, str] | None:
    """拆开节点键，返回 (前缀, 前段, 后段)。拆不出来就是坏键，直接丢。"""
    parts = key.split("|", 2)
    if len(parts) != 3:
        return None
    prefix, head, tail = parts
    if prefix not in (DOWNSTREAM_PREFIX, UPSTREAM_PREFIX) or not head or not tail:
        return None
    return prefix, head, tail


def _valid_keys() -> tuple[set[str], set[str]]:
    """当前库里真实存在的下游键和上游键，用来剪枝。

    按**库里有没有**算，不按「界面上看不看得见」算：某个接口被整体停用时，它的位置
    要留着，重新打开接口时节点还得回到原处。
    """
    models = {model_key(r["model_name"], r["protocol"]) for r in db.list_routes()}
    for row in db.list_forwards():
        models.add(model_key(row["model_name"], row["protocol"]))
    upstreams = {
        upstream_key(group_id, remote)
        for group_id, remotes in db.all_group_models().items()
        for remote in remotes
    }
    return models, upstreams


def _parse(text: str) -> dict[str, Any]:
    """把库里的原文解析成布局。任何解析不出来的形态都退化成空布局，不抛异常。"""
    if not text or not text.strip():
        return empty_layout()
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return empty_layout()
    if not isinstance(data, dict):
        return empty_layout()
    nodes = data.get("nodes")
    if not isinstance(nodes, dict):
        return empty_layout()
    clean: dict[str, list[float]] = {}
    for key, value in nodes.items():
        if not isinstance(key, str) or len(key) > MAX_KEY_LEN or _split_key(key) is None:
            continue
        if not isinstance(value, (list, tuple)) or len(value) != 2:
            continue
        x, y = _coord(value[0]), _coord(value[1])
        if x is None or y is None:
            continue
        clean[key] = [x, y]
        if len(clean) >= MAX_NODES:
            break
    raw_view = data.get("view")
    view = {"x": 0.0, "y": 0.0, "z": 1.0}
    if isinstance(raw_view, dict):
        vx, vy = _coord(raw_view.get("x")), _coord(raw_view.get("y"))
        if vx is not None:
            view["x"] = vx
        if vy is not None:
            view["y"] = vy
        z = raw_view.get("z")
        if (
            not isinstance(z, bool)
            and isinstance(z, (int, float))
            and math.isfinite(z)
            and ZOOM_MIN <= z <= ZOOM_MAX
        ):
            view["z"] = round(float(z), 3)
    return {"v": LAYOUT_VERSION, "nodes": clean, "view": view}


def load() -> dict[str, Any]:
    """当前布局。坏 JSON 按「还没摆过」处理 —— 配错了不能拖垮整个页面。"""
    text = db.get_setting(SETTING_KEY, "") or ""
    with _lock:
        if _cache["raw"] == text and _cache["layout"] is not None:
            return _cache["layout"]
    layout = _parse(text)
    with _lock:
        _cache["raw"] = text
        _cache["layout"] = layout
    return layout


def save(layout: dict[str, Any]) -> dict[str, Any]:
    """落盘一份布局，返回实际存下去的那份（已归一化并剪枝）。

    先按 ``_parse`` 过一遍（丢掉坏键、越界坐标、超量节点），再剪掉库里不存在的节点，
    最后才写。校验放在落盘前，坏数据进不了库。
    """
    cleaned = _parse(json.dumps({"v": LAYOUT_VERSION, "nodes": layout.get("nodes", {}),
                                 "view": layout.get("view", {})}))
    models, upstreams = _valid_keys()
    nodes: dict[str, list[float]] = {}
    for key, coord in cleaned["nodes"].items():
        parts = _split_key(key)
        if parts is None:
            continue
        prefix, head, tail = parts
        if prefix == DOWNSTREAM_PREFIX:
            keep = key in models
        else:
            keep = key in upstreams
        if keep:
            nodes[key] = coord
    stored = {"v": LAYOUT_VERSION, "nodes": nodes, "view": cleaned["view"]}
    text = json.dumps(stored, ensure_ascii=False, separators=(",", ":"))
    if len(text.encode("utf-8")) > MAX_BYTES:
        raise ValueError(f"布局太大（超过 {MAX_BYTES // 1024} KB）")
    db.set_setting(SETTING_KEY, text)
    with _lock:
        _cache["raw"] = text
        _cache["layout"] = stored
    return stored


def forget_cache() -> None:
    """丢掉内存缓存。测试里换库时用。"""
    with _lock:
        _cache["raw"] = None
        _cache["layout"] = None
