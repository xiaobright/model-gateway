"""自动降级：一个请求打不通就换下一个候选，连着坏的站进冷却。

策略参数全在这个文件顶部 —— 要调「冷却多久、哪些状态码算站级可重试」，改这里。
每个模型的**尝试顺序**不在这儿，那是配置（model_routes.priority），在界面上排。

为什么是「冷却」而不是「成功了就把生效指针挪过去」：真库里同一个上游最长连续失败
60 次（站A）、28 次（站B）、10~11 次（站J / 站K）—— 故障是「坏一阵子」，
不是「坏一下」。冷却天然覆盖这一段，而且期满自动回到优先级第一（OpenAI 侧 站A
累计命中过 1.5 亿缓存 token，那边必须能自己回去），也不会悄悄改写用户手动选的那个候选。
"""

from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass
from typing import Sequence

from . import db, protocols
from .reqlog import log

# ---------------------------------------------------------------- 策略参数

START_DEADLINE = 180.0  # 已经耗了这么久就不再开新尝试（另外每次重试前还会查客户端还在不在）
COOL_AFTER = 2          # 同一个分组连续失败几次进冷却。1 次可能只是抖动
COOL_SECONDS = 90.0     # 首次冷却时长
COOL_MAX = 600.0        # 每多失败一次翻倍，封顶。60 连击那种会稳定在 10 分钟

# 可重试 = 「换一个站有希望拿到不同结果」。400 / 422 不在这里，但也不是没救：
# 那是候选级的失败，见下面的 CANDIDATE_STATUS。
RETRY_STATUS = frozenset(
    {
        408, 425, 429,                          # 超时 / 太早 / 限流
        500, 502, 503, 504,                     # 上游炸了
        520, 521, 522, 523, 524, 525, 526, 529,  # Cloudflare 那一串
        401, 403,                               # key 死了 —— 换个分组就是换一把 key
        413,                                    # 体积上限每个站不一样
    }
)

# 候选级失败：这个候选不认这次请求，换候选有希望 —— 可以是同一个分组里的另一个
# 真名，也可以是下一个站。但这**不算这个分组的锅**：key 是好的、站是通的，所以
# 不记失败、不进冷却，见 note_fail 的调用点。
# 404 = 模型名没了；400 / 422 = 这个站不支持请求里的某个参数或模型映射不对 ——
# 各家实现不一样，真库里同一个请求在别家往往能过。
CANDIDATE_STATUS = frozenset({400, 404, 422})

# 开关按接口分开存。默认值属于协议描述符；漏写的一种按「关」处理 —— 新的线格式不该
# 在没人点过头之前就开始自动换站。
_KEY = "failover.{0}"
_DEFAULT_ON = {
    proto.name: ("on" if proto.default_failover else "off")
    for proto in protocols.ALL.values()
}


def enabled(protocol: str) -> bool:
    return db.get_setting(_KEY.format(protocol), _DEFAULT_ON.get(protocol, "off")) == "on"


def set_enabled(protocol: str, on: bool) -> None:
    db.set_setting(_KEY.format(protocol), "on" if on else "off")
    log(f"failover[{protocol}] -> {'on' if on else 'off'}")


def all_enabled() -> dict[str, bool]:
    return {p: enabled(p) for p in protocols.NAMES}


# ---------------------------------------------------------------- 断路器

# 只放内存：重启该忘掉（干净起步是对的默认），也省掉每个请求一次写库。
# 键是 group_id —— 坏的是那个站和那把 key，不是某个模型，所以一个模型踩到的坑
# 能顺带保护其它模型。


@dataclass
class _State:
    fails: int = 0
    until: float = 0.0          # monotonic 时间；<= now 就是没在冷却
    last_status: int = 0
    cooled: int = 0             # 一共进过几次冷却，只用来展示


_states: dict[int, _State] = {}
# 写发生在转发（事件循环线程），读发生在管理接口（FastAPI 对同步端点用线程池）——
# 一边迭代、一边增删会让 /models、/inflight、/failover 随机 500。RLock 是因为
# snapshot 拿锁后还会调 cooling()。
_lock = threading.RLock()


def note_ok(group_id: int) -> None:
    with _lock:
        st = _states.get(group_id)
        if st is None:
            return
        if st.fails or st.until:
            log(f"  failover: g{group_id} 恢复正常，清掉冷却")
        _states.pop(group_id, None)


def note_fail(group_id: int, status: int, label: str = "") -> float:
    """记一次失败，返回这次要冷却多少秒（0 = 还没到阈值）。"""
    with _lock:
        st = _states.setdefault(group_id, _State())
        st.fails += 1
        st.last_status = status
        if st.fails < COOL_AFTER:
            return 0.0
        # 第 COOL_AFTER 次开始冷却，之后每次翻倍
        span = min(COOL_MAX, COOL_SECONDS * (2 ** (st.fails - COOL_AFTER)))
        st.until = time.monotonic() + span
        st.cooled += 1
        log(f"  failover: {label or f'g{group_id}'} 连续失败 {st.fails} 次（{status}），冷却 {span:.0f}s")
        return span


def cooling(group_id: int) -> float:
    """还要冷却多少秒；0 = 可以用。"""
    with _lock:
        st = _states.get(group_id)
        if st is None or not st.until:
            return 0.0
        left = st.until - time.monotonic()
        if left <= 0:
            # 期满：留着 fails 不清零，这样它再失败一次就直接进更长的冷却（半开探测）
            st.until = 0.0
            return 0.0
        return left


def clear(group_id: int) -> None:
    """手动切到这个候选时调用 —— 用户明确指定了，就立刻给它机会。"""
    with _lock:
        if _states.pop(group_id, None) is not None:
            log(f"  failover: g{group_id} 的冷却被手动切换清掉了")


def reset() -> None:
    with _lock:
        _states.clear()


def snapshot() -> list[dict]:
    """给管理接口用：当前有状态的分组。冷却剩余毫秒 + 连续失败次数。"""
    with _lock:
        out = []
        for gid, st in list(_states.items()):
            left = cooling(gid)
            if not left and not st.fails:
                continue
            out.append(
                {
                    "group_id": gid,
                    "cooling_ms": int(left * 1000),
                    "fails": st.fails,
                    "last_status": st.last_status,
                    "cooled": st.cooled,
                }
            )
        return sorted(out, key=lambda r: -r["cooling_ms"])


# ---------------------------------------------------------------- 同站重试
#
# 和「自动降级换站」是两件事：这里是在**同一个上游**上再发几把。优先级更高 ——
# 先吃同站规则，耗尽了才轮到换候选。规则绑供应商（upstreams.retry_rules），
# 哪个站、哪些状态码、重试几次都由配置决定。

# 同站最多再发几把的硬顶：防止配置写飘了把一次请求打成十几遍
SAME_RETRY_MAX_TIMES = 10


def parse_retry_rules(raw: str) -> dict[int, dict[str, int]]:
    """把 vendors 的 retry_rules JSON 收成 {status: {times, delay_ms}}。

    解析失败或形状不对就当没配 —— 转发路径上不该因为配置脏了而炸请求。
    管理接口在保存时会校验并拒掉非法 JSON。
    """
    if not raw or not raw.strip():
        return {}
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError):
        return {}
    if not isinstance(parsed, list):
        return {}
    out: dict[int, dict[str, int]] = {}
    for item in parsed:
        if not isinstance(item, dict):
            continue
        try:
            status = int(item["status"])
            times = int(item.get("times", 2))
            delay_ms = int(item.get("delay_ms", 0))
        except (KeyError, TypeError, ValueError):
            continue
        if not (100 <= status <= 599) or not (1 <= times <= SAME_RETRY_MAX_TIMES):
            continue
        if delay_ms < 0:
            delay_ms = 0
        out[status] = {"times": times, "delay_ms": delay_ms}
    return out


def same_retry_delay(
    raw_rules: str, status: int, used_for_upstream: int
) -> float | None:
    """这次响应该不该同站再发一把？返回要等多少秒；不该重试返回 None。

    used_for_upstream = 这个上游在本次请求里已经同站重试过几次。
    """
    rule = parse_retry_rules(raw_rules).get(status)
    if rule is None:
        return None
    if used_for_upstream >= rule["times"]:
        return None
    return rule["delay_ms"] / 1000.0


# ---------------------------------------------------------------- 排链


def order_chain(chain: tuple[db.Route, ...]) -> list[db.Route]:
    """把冷却中的候选挪到最后，其余保持 priority 顺序。

    不是「剔掉」而是「挪后」：60 连击那种故障期里可能所有候选都在冷却，
    这时候还是得挑一个打，不能因为「大家都冷着」就直接给客户端一个 503。
    """
    warm = [r for r in chain if not cooling(r.group_id)]
    cold = [r for r in chain if cooling(r.group_id)]
    return warm + cold


def next_index(candidates: Sequence[db.Route], start: int, dead_groups: set[int]) -> int:
    """从 start 往后找下一个「打了还有意义」的候选，返回下标；没有就 -1。

    这个请求里已经站级失败过的分组整个跳掉：同一个站绝不在一次请求里立刻重试
    （客户端自己已经在重试了，再叠一层只是让每次失败变长）。同分组的兄弟候选
    一起跳 —— 站都连不上，换个模型名也没用。
    """
    for i in range(start, len(candidates)):
        if candidates[i].group_id not in dead_groups:
            return i
    return -1
