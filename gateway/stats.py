"""管理页「概览」视图的聚合查询。

request_log 有 2000 行上限（db.LOG_KEEP_ROWS），所以这里直接拉全量在
Python 里聚合。分桶和分位数用 SQL 写会更绕（SQLite 没有 percentile 函数），
而两千行的代价可以忽略，可读性优先。

这个模块只读，不参与转发；唯一的可变状态是活跃流计数器，由 proxy 调用。
"""

from __future__ import annotations

import math
import time
from collections import defaultdict
from datetime import datetime
from typing import Any, Iterable

from . import db

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

_live = {"requests": 0, "streams": 0}


def live_enter(stream: bool) -> None:
    _live["requests"] += 1
    if stream:
        _live["streams"] += 1


def live_exit(stream: bool) -> None:
    _live["requests"] = max(0, _live["requests"] - 1)
    if stream:
        _live["streams"] = max(0, _live["streams"] - 1)


def live() -> dict[str, int]:
    return dict(_live)


# ---------------------------------------------------------------- 工具


def _epoch(ts: str) -> int | None:
    """'2026-09-03 19:42:07'（localtime）-> epoch 秒。"""
    try:
        return int(datetime.strptime(ts, TS_FMT).timestamp())
    except (ValueError, TypeError):
        return None


def _pct(values: list[int], q: float) -> int:
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
    return _pct([r["duration_ms"] for r in rows], 0.95)
