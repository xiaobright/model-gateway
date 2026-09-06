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

# 这几种收尾算"上游没把事办好"，用于健康度；client_abort 是下游自己走的，不算上游的锅
BAD_NOTES = frozenset({"connect_failed", "upstream_abort", "truncated"})

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
    return row["status"] >= 500 or row["note"] in BAD_NOTES


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


# 三个聚合都允许传入已取好的行，overview 里拉一次复用给三个，省两次全表读
def _all_rows() -> tuple[dict, ...]:
    return db.recent_requests(db.LOG_KEEP_ROWS)


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


def overview(window: str = DEFAULT_WINDOW, top: int = 8) -> dict[str, Any]:
    """概览视图一次拿全，省掉前端三次往返。"""
    rows = _all_rows()
    stats = db.request_stats()
    total_in = stats["input_tokens"]
    return {
        "series": series(window, rows),
        "upstreams": upstream_health(rows),
        "models": model_top(top, rows),
        "live": live(),
        "totals": {
            **stats,
            "cache_hit_rate": round(stats["cached_tokens"] / total_in, 4) if total_in else 0.0,
            "p95": p95_overall(rows),
        },
    }


def p95_overall(rows: Iterable[dict]) -> int:
    return int(_pct([r["duration_ms"] for r in rows], 0.95))


# ---------------------------------------------------------------- 按包大小估 token
#
# 「实时」页上那个 `≈ N tok` 的标尺。转发记录里每一行都现成地放着「这条请求多少字节」
# 和「上游报了多少 token」，所以这个比值是真的从过往经验里量出来的，不是拍的常数。
#
# 两个方向差两个数量级，必须分开量：上行是 JSON 正文（六七个字节一个 token），
# 下行是 SSE 帧（每个 delta 事件一百多字节只带几个字，五十多个字节才摊到一个 token）。

RATIO_MIN_ROWS = 20        # 样本少于这个数就用兜底常数，别拿三条记录去定标尺
RATIO_MAX_SPREAD = 6.0     # p90/p10 超过这个就是「字节数压根预测不了 token」，不给估值
RATIO_TTL = 60.0           # 学出来的标尺缓存这么久：那个接口 1 秒一刷，不该每次全表扫

# 兜底值取自真库两千条记录的中位数。openai 的下行是 0 = 不估：Responses API 的流里
# 光事件框架就几十 KB，跟输出长度基本无关（实测 p90/p10 差十倍），给数字比不给更糟
RATIO_FALLBACK: dict[str, tuple[float, float]] = {
    "anthropic": (6.7, 55.8),
    "openai": (4.9, 0.0),
}

_ratio_cache: tuple[float, dict[str, dict[str, float]]] = (0.0, {})


def context_tokens(row: dict) -> int:
    """这条记录的上下文有多大。两种接口的 input_tokens 含不含缓存读取不一样，
    规则只在 protocols 里写一份。"""
    proto = protocols.by_name(row.get("protocol") or "")
    return proto.context_tokens(
        (row["input_tokens"], row["output_tokens"], row["cached_tokens"])
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
        got_out = row["output_tokens"] or 0
        if got_out > 20 and row["resp_bytes"]:
            down[name].append(row["resp_bytes"] / got_out)

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
