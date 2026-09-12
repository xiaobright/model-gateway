"""管理页「概览」视图的聚合查询。

request_log 有 2000 行上限（db.LOG_KEEP_ROWS），所以这里直接拉全量在
Python 里聚合。分桶和分位数用 SQL 写会更绕（SQLite 没有 percentile 函数），
而两千行的代价可以忽略，可读性优先。

这个模块只读，不参与转发。「进行中」那两个数来自 inflight 的登记表 ——
以前是这里的一对计数器，现在那张表是同一件事的唯一来源。
"""

from __future__ import annotations

import math
import time
from collections import defaultdict
from datetime import datetime
from typing import Any, Iterable, Sequence

from . import db, inflight, protocols

TS_FMT = "%Y-%m-%d %H:%M:%S"

# 时间窗 -> (跨度秒, 桶宽秒)。桶数 = 跨度/桶宽 + 1（多出来的一个是"当前"这一桶）
WINDOWS: dict[str, tuple[int, int]] = {
    "1h": (3600, 60),
    "24h": (24 * 3600, 3600),
    "7d": (7 * 24 * 3600, 24 * 3600),
}
DEFAULT_WINDOW = "1h"

# 这些收尾都不是一次成功完成的请求，用于健康度和失败计数。client_abort 的责任在下游，
# 但它同样不能在统计卡片里被算成成功；日志里的 note 仍保留了责任边界。
BAD_NOTES = frozenset({"connect_failed", "upstream_abort", "truncated", "client_abort", "manual_abort"})

# ---------------------------------------------------------------- 活跃流


def live() -> dict[str, int]:
    """几条在跑、其中几条流式。「在跑」现在包括还卡在连接和等首字节的 ——
    一个连不上的站要磨 8 秒，那 8 秒当然算进行中。"""
    return inflight.counts()


# ---------------------------------------------------------------- 工具


def _epoch(ts: str) -> int | None:
    """'2026-09-03 19:42:07'（localtime）-> epoch 秒。"""
    try:
        return int(datetime.strptime(ts, TS_FMT).timestamp())
    except (ValueError, TypeError):
        return None


def _pct(values: Sequence[float], q: float) -> float:
    """最近秩分位数：q=0.5 是中位数，q=0.95 是 P95。空列表返回 0。"""
    if not values:
        return 0
    ordered = sorted(values)
    rank = max(0, math.ceil(q * len(ordered)) - 1)
    return ordered[min(rank, len(ordered) - 1)]


def _failed(row: dict) -> bool:
    return int(row.get("status") or 0) >= 400 or row.get("note") in BAD_NOTES


def _saved(row: dict) -> bool:
    """只有第二次尝试最终正常收尾，才算一次真正的救回。"""
    return (
        int(row.get("attempt") or 1) > 1
        and 200 <= int(row.get("status") or 0) < 400
        and not _failed(row)
        and row.get("note") != "client_abort"
    )


def request_stats(rows: Iterable[dict] | None = None) -> dict[str, int]:
    """集中计算转发统计，所有协议相关的输入口径都从 Protocol 描述符派生。"""
    rows = tuple(_all_rows() if rows is None else rows)
    return {
        "requests": len(rows),
        "input_tokens": sum(int(r.get("input_tokens") or 0) for r in rows),
        "output_tokens": sum(int(r.get("output_tokens") or 0) for r in rows),
        "cached_tokens": sum(int(r.get("cached_tokens") or 0) for r in rows),
        "cache_creation_tokens": sum(int(r.get("cache_creation_tokens") or 0) for r in rows),
        "context_tokens": sum(context_tokens(r) for r in rows),
        "saved": sum(_saved(r) for r in rows),
        "failed_over": sum(r.get("note") == "failed_over" for r in rows),
    }


def cache_hit_rate(stats: dict[str, int]) -> float:
    """缓存读取 / 协议归一化后的总输入；Anthropic 的 cache_read 不在 input 内。

    夹到 100%：某行的 usage 只捞到缓存数、没捞到 input（流被截/上游字段缺失）时，
    分子会大于分母，旧口径能算出 900% 这种数。
    """
    total = stats.get("context_tokens") or 0
    if total <= 0:
        return 0.0
    return round(min(1.0, (stats.get("cached_tokens") or 0) / total), 4)


def _buckets(window: str) -> tuple[int, int, int]:
    """返回 (起始桶的 epoch, 桶宽秒, 桶数)。

    分/小时直接对 epoch 取整就行；跨天的桶必须按本地时区的日界对齐，
    否则 7 天视图的每一天是从 UTC 午夜开始的，东八区看日期标签会整体错位。
    """
    span, bucket = WINDOWS.get(window, WINDOWS[DEFAULT_WINDOW])
    now = int(time.time())
    if bucket >= 86400:
        midnight = datetime.fromtimestamp(now).replace(hour=0, minute=0, second=0, microsecond=0)
        start = int(midnight.timestamp()) - (span // bucket - 1) * bucket
        return start, bucket, span // bucket
    start = (now - span) // bucket * bucket
    return start, bucket, (now - start) // bucket + 1


# 三个聚合都允许传入已取好的行，overview 里拉一次复用给三个，省两次全表读。
# 全局停用的接口在 SQL 里就滤掉：它的历史记录也一起消失，管理页不会为死掉的站报健康度，
# 也不会让停用协议的记录占掉 2000 行配额。
def _all_rows() -> tuple[dict, ...]:
    return db.recent_requests(db.LOG_KEEP_ROWS, db.disabled_protocols())


# ---------------------------------------------------------------- 时间线


def series(window: str = DEFAULT_WINDOW, rows: Iterable[dict] | None = None) -> dict[str, Any]:
    """按时间分桶的请求量 / token 量，空桶补 0（前端不用自己补点）。"""
    start, bucket, count = _buckets(window)
    points = [
        {"t": start + i * bucket, "n": 0, "err": 0, "ti": 0, "to": 0, "tc": 0}
        for i in range(count)
    ]

    for row in _all_rows() if rows is None else rows:
        epoch = _epoch(row["ts"] or "")
        if epoch is None:
            continue
        # 必须拿 start 当原点：天桶是按本地日界对齐的，直接 epoch//bucket 会
        # 落到 UTC 日界上，东八区差 8 小时，7 天视图会一个点都匹配不上
        idx = (epoch - start) // bucket
        if idx < 0 or idx >= count:
            continue
        point = points[idx]
        point["n"] += 1
        if _failed(row):
            point["err"] += 1
        point["ti"] += row["input_tokens"] or 0
        point["to"] += row["output_tokens"] or 0
        point["tc"] += row["cached_tokens"] or 0

    return {"window": window, "bucket": bucket, "points": points}


# ---------------------------------------------------------------- 上游健康


def _protocol_split(group: list[dict]) -> dict[str, dict[str, Any]]:
    """按协议拆一遍。回答的是「这个站的 anthropic 接口到底能不能用」——公益站常常只有一种
    格式能通，而这件事没法静态探测，只能看实际跑过的请求。老记录没有这个字段，跳过。"""
    out: dict[str, dict[str, Any]] = {}
    for row in group:
        name = row.get("protocol") or ""
        if not name:
            continue
        slot = out.setdefault(name, {"n": 0, "bad": 0})
        slot["n"] += 1
        if _failed(row):
            slot["bad"] += 1
    for slot in out.values():
        slot["ok_rate"] = round((slot["n"] - slot["bad"]) / slot["n"], 4)
    return out


def upstream_health(rows: Iterable[dict] | None = None) -> list[dict[str, Any]]:
    grouped: dict[str, list[dict]] = defaultdict(list)
    for row in _all_rows() if rows is None else rows:
        grouped[row["upstream"]].append(row)

    out = []
    for name, group in grouped.items():
        durations = [r["duration_ms"] for r in group]
        bad = sum(1 for r in group if _failed(r))
        out.append(
            {
                "name": name,
                "n": len(group),
                "bad": bad,
                # 成功率按"没出问题的比例"算，比只看 status < 400 更贴合实际体感
                "ok_rate": round((len(group) - bad) / len(group), 4) if group else 0.0,
                "p50": _pct(durations, 0.5),
                "p95": _pct(durations, 0.95),
                "avg": round(sum(durations) / len(durations)) if durations else 0,
                "ti": sum(r["input_tokens"] or 0 for r in group),
                "to": sum(r["output_tokens"] or 0 for r in group),
                "by_protocol": _protocol_split(group),
            }
        )
    return sorted(out, key=lambda r: -r["n"])


# ---------------------------------------------------------------- 模型热度


def model_top(limit: int = 8, rows: Iterable[dict] | None = None) -> list[dict[str, Any]]:
    grouped: dict[str, list[dict]] = defaultdict(list)
    for row in _all_rows() if rows is None else rows:
        grouped[row["model"]].append(row)

    out = []
    for model, group in grouped.items():
        durations = [r["duration_ms"] for r in group]
        bad = sum(1 for r in group if _failed(r))
        out.append(
            {
                "model": model,
                "n": len(group),
                "bad": bad,
                "avg": round(sum(durations) / len(durations)) if durations else 0,
                "p95": _pct(durations, 0.95),
                "ti": sum(r["input_tokens"] or 0 for r in group),
                "to": sum(r["output_tokens"] or 0 for r in group),
            }
        )
    out.sort(key=lambda r: -r["n"])
    return out[: max(1, limit)]


# ---------------------------------------------------------------- 概览合批


def _rows_in_window(window: str, rows: Iterable[dict]) -> tuple[dict, ...]:
    """把记录裁到当前时间窗。时间线自己按桶过滤，但卡片 / 健康 / 热度也要用同一口径 ——
    否则切到 1 小时，卡片还显示 7 天的累计值，三档看起来一模一样。"""
    start, _bucket, _count = _buckets(window)
    return tuple(
        row for row in rows
        if (epoch := _epoch(row["ts"] or "")) is not None and epoch >= start
    )


def overview(window: str = DEFAULT_WINDOW, top: int = 8) -> dict[str, Any]:
    """概览视图一次拿全，省掉前端三次往返。所有聚合都只看当前时间窗。"""
    all_rows = _all_rows()
    rows = _rows_in_window(window, all_rows)
    stats = request_stats(rows)
    return {
        "series": series(window, all_rows),
        "upstreams": upstream_health(rows),
        "models": model_top(top, rows),
        "live": live(),
        "totals": {
            **stats,
            "cache_hit_rate": cache_hit_rate(stats),
            "p95": p95_overall(rows),
        },
    }


def p95_overall(rows: Iterable[dict]) -> int:
    return int(_pct([r["duration_ms"] for r in rows], 0.95))


# ---------------------------------------------------------------- 按包大小估 token
#
# 「实时」页上那个 `≈ N tok` 的标尺。转发记录里每一行都现成地放着字节数和上游报的
# token 数，所以这个比值是真的从过往经验里量出来的，不是拍的常数。
#
# 两个方向量的**不是同一件事**，这一点必须写清楚：
#
# - 上行量的是「计费口径」，而且它是准的：请求体每个字符都算进输入，字节数和 token 数
#   一一对应，所以上行的 `≈` 可以当计费量看
# - 下行量的是「**收到手的内容**」。计费口径这边估不出来 —— 思维链发下来的是总结过的，
#   而计费按完整的算，思维链越多差得越远。所以下行的标尺只认没有思维链的那些记录
#   （正文是完整的，那种记录里收到的和计费的对得上），而计费的输出量只从流末尾
#   上游自己报的那个数读，不猜
#
# 另外下行只数**内容**字节，不数整条响应：SSE 帧和 JSON 结构占了大头，
# 拿整条响应的字节数去折 token 差十倍（见 protocols.count_content）。

RATIO_MIN_ROWS = 20        # 样本少于这个数就用兜底常数，别拿三条记录去定标尺
RATIO_MAX_SPREAD = 6.0     # p90/p10 超过这个就是「字节数压根预测不了 token」，不给估值
RATIO_TTL = 60.0           # 学出来的标尺缓存这么久：那个接口 1 秒一刷，不该每次全表扫

# 兜底值在协议描述符里登记。上行取自真库两千条记录的中位数；下行是中英混排正文的
# 经验值，等攒够没有思维链的记录就会被真实测量顶掉。未配置的协议不擅自套用别人的值。
RATIO_FALLBACK: dict[str, tuple[float, float]] = {
    proto.name: proto.ratio_fallback
    for proto in protocols.ALL.values()
    if proto.ratio_fallback is not None
}

_ratio_cache: tuple[float, dict[str, dict[str, float]]] = (0.0, {})


def context_tokens(row: dict) -> int:
    """这条记录的上下文有多大。两种接口的 input_tokens 含不含缓存读取不一样，
    规则只在 protocols 里写一份。"""
    proto = protocols.by_name(row.get("protocol") or "")
    return proto.context_tokens(
        (
            row.get("input_tokens"),
            row.get("output_tokens"),
            row.get("cached_tokens"),
            row.get("cache_creation_tokens"),
        )
    )


def _ratio_of(samples: list[float], fallback: float) -> float:
    """一堆「多少字节摊一个 token」的样本 -> 一个能用的标尺，不可信则返回 0。

    取中位数而不是总量比：总量比会被几条巨大的请求整个带走。
    张幅（p90/p10）太大说明这个方向不是等比的 —— 有个跟 token 数无关的大常数项，
    再乘一个系数也救不回来，这时界面上少一段比多一个差十倍的数好。
    """
    if len(samples) < RATIO_MIN_ROWS:
        return fallback
    lo, hi = _pct(samples, 0.1), _pct(samples, 0.9)
    if lo <= 0 or hi / lo > RATIO_MAX_SPREAD:
        return 0.0
    return round(_pct(samples, 0.5), 2)


def token_ratio() -> dict[str, dict[str, float]]:
    """每种接口、每个方向「多少字节摊一个 token」。0 表示估不出来，界面上就不显示。"""
    global _ratio_cache
    at, cached = _ratio_cache
    now = time.monotonic()
    if cached and now - at < RATIO_TTL:
        return cached

    up: dict[str, list[float]] = defaultdict(list)
    down: dict[str, list[float]] = defaultdict(list)
    for row in _all_rows():
        name = row.get("protocol") or ""
        if name not in RATIO_FALLBACK or row["status"] >= 300:
            continue
        # 门槛甩掉小请求：那种请求里固定开销占大头，摊出来的比值和真正想看的大请求不是一回事
        ctx = context_tokens(row)
        if ctx > 200 and row["req_bytes"]:
            up[name].append(row["req_bytes"] / ctx)
        # 下行只认没有思维链的记录，而且只数内容字节，理由见本节开头
        got_out = row["output_tokens"] or 0
        content = row.get("resp_text_bytes") or 0
        if got_out > 20 and content and not row.get("thinking"):
            down[name].append(content / got_out)

    result = {
        name: {"up": _ratio_of(up[name], fb[0]), "down": _ratio_of(down[name], fb[1])}
        for name, fb in RATIO_FALLBACK.items()
    }
    _ratio_cache = (now, result)
    return result


def reset() -> None:
    """清掉标尺缓存。给测试用 —— 库是每个用例一个临时文件，而缓存是进程级的。"""
    global _ratio_cache
    _ratio_cache = (0.0, {})
