"""自动降级：一个请求打不通就换下一个候选，连着坏的站进冷却。

策略参数全在这个文件顶部 —— 要调「重试几次、冷却多久、哪些状态码算可重试」，改这里。
每个模型的**尝试顺序**不在这儿，那是配置（model_routes.priority），在界面上排。

为什么是「冷却」而不是「成功了就把生效指针挪过去」：真库里同一个上游最长连续失败
60 次（站A）、28 次（站B）、10~11 次（站J / 站K）—— 故障是「坏一阵子」，
不是「坏一下」。冷却天然覆盖这一段，而且期满自动回到优先级第一（OpenAI 侧 站A
累计命中过 1.5 亿缓存 token，那边必须能自己回去），也不会悄悄改写用户手动选的那个候选。
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Sequence

from . import db
from .reqlog import log

# ---------------------------------------------------------------- 策略参数

MAX_ATTEMPTS = 3        # 一次请求最多打几个候选（= 最多降 2 级）
START_DEADLINE = 180.0  # 已经耗了这么久就不再开新尝试（另外每次重试前还会查客户端还在不在）
COOL_AFTER = 2          # 同一个分组连续失败几次进冷却。1 次可能只是抖动
COOL_SECONDS = 90.0     # 首次冷却时长
COOL_MAX = 600.0        # 每多失败一次翻倍，封顶。60 连击那种会稳定在 10 分钟

# 可重试 = 「换一个站有希望拿到不同结果」。
# 400 / 422 不在里面：请求本身有问题，换谁都是同样的答案。
RETRY_STATUS = frozenset(
    {
        408, 425, 429,                          # 超时 / 太早 / 限流
        500, 502, 503, 504,                     # 上游炸了
        520, 521, 522, 523, 524, 525, 526, 529,  # Cloudflare 那一串
        401, 403,                               # key 死了 —— 换个分组就是换一把 key
        413,                                    # 体积上限每个站不一样
    }
)

# 模型级失败：这个站没有这个模型名。中转站下掉模型 id 是常事（带日期后缀的尤其），
# 换一条候选有希望 —— 可以是同一个分组里的另一个真名。但这**不算这个分组的锅**：
# key 是好的、站是通的，所以不记失败、不进冷却，见 note_fail 的调用点。
MODEL_STATUS = frozenset({404})

# 开关按接口分开存：Claude 侧全是中转站、坏得勤，值得自动降级；
# GPT 侧只有 站A 是公益站，其余要花钱的站「花钱图稳定」，得手动确认。
_KEY = "failover.{0}"
_DEFAULT_ON = {"anthropic": "on", "openai": "off"}


def enabled(protocol: str) -> bool:
    return db.get_setting(_KEY.format(protocol), _DEFAULT_ON.get(protocol, "off")) == "on"


def set_enabled(protocol: str, on: bool) -> None:
    db.set_setting(_KEY.format(protocol), "on" if on else "off")
    log(f"failover[{protocol}] -> {'on' if on else 'off'}")


def all_enabled() -> dict[str, bool]:
    return {p: enabled(p) for p in db.PROTOCOLS}


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


def note_ok(group_id: int) -> None:
    st = _states.get(group_id)
    if st is None:
        return
    if st.fails or st.until:
        log(f"  failover: g{group_id} 恢复正常，清掉冷却")
    _states.pop(group_id, None)


def note_fail(group_id: int, status: int, label: str = "") -> float:
    """记一次失败，返回这次要冷却多少秒（0 = 还没到阈值）。"""
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
    if _states.pop(group_id, None) is not None:
        log(f"  failover: g{group_id} 的冷却被手动切换清掉了")


def reset() -> None:
    _states.clear()


def snapshot() -> list[dict]:
    """给管理接口用：当前有状态的分组。冷却剩余毫秒 + 连续失败次数。"""
    out = []
    for gid, st in _states.items():
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

