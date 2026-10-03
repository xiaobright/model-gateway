"""「加一个新模型」的一键流程：先问每个上游站有没有它，再一次性登记 + 暴露。

为什么单独一层：同一个上游模型名常常在好几个站（甚至好几个协议的分组）里都有，
手工逐个进分组弹窗拉一次列表、勾一下，再逐个去「模型路由」加候选，是这个网关里
最重复的一段操作。这里把「问一遍所有分组」和「写进去」做成两件事：

- ``scan()`` 只读：并发问每个启用的分组要一份模型列表，按相似度和站台能力打分，
  精确命中的排前面（默认勾选），部分匹配的排后面（默认不勾），连不上的站单独列出来。
- ``commit()`` 只写：把勾中的那些分组登记进上游模型目录，并在模型路由里建候选。

匹配规则（``score``）故意做成「按关键字段比较 + 短名兜底」两层：上游的名字往往带
厂商前缀和日期后缀（``deepseek-ai/DeepSeek-V3-0324``），而人敲的是短名
（``deepseek-v3``）。只有真正沾边的名字才会被列出来 —— 每次扫描都要显示一个站的
**全部**模型是不现实的，几十个站一轮下来就是几千行。

不能把 ``fetch_remote_models`` 的失败直接抛给调用方：一个站连不上不该让整次扫描失败，
每个分组各自降级成一条 ``error``。
"""

from __future__ import annotations

import asyncio
import re
import time
from dataclasses import dataclass
from typing import Iterable

from . import db
from . import upstream as upstream_mod
from .reqlog import log

# 一次扫描的硬上限。扫描是并发发的，但慢站会把整体拖成分钟级（connect 8 秒 + 读 30 秒），
# 所以给总预算：到点还没回来的分组按「超时」记一笔，已拿到的照样出结果。
# 真库实测：十九个分组里有五六个是慢站，一轮大约到头；想更快只能等站点自己变快。
SCAN_BUDGET_SECONDS = 25.0
# 同时在途的分组数。站很多时八路足够快，又不会把本机 fd / 站点限流撞出 429。
SCAN_CONCURRENCY = 8
# 单个分组的超时：在总预算和单站默认超时里取小的那个。
SCAN_GROUP_TIMEOUT = 12.0

# 相似度分级，数字只用于排序；对外一律给名字，别把数字当契约
EXACT = 3
ALIAS = 2
PARTIAL = 1
FUZZY = 0.5
LEVELS: dict[float, str] = {
    EXACT: "exact",
    ALIAS: "alias",
    PARTIAL: "partial",
    FUZZY: "fuzzy",
}

_NOT_ALNUM = re.compile(r"[^a-z0-9]+")
_VENDOR_SEPS = "/:@"


def normalize_key(name: str) -> str:
    """比较用的归一形状：小写、去掉所有分隔符。`DeepSeek-V3.1` -> `deepseekv31`。"""
    return _NOT_ALNUM.sub("", (name or "").lower())


def strip_vendor(name: str) -> str:
    """`deepseek-ai/DeepSeek-V3` -> `DeepSeek-V3`；`@cf/meta/llama` -> `llama`。

    中转站和聚合站喜欢把厂商前缀带上，人敲的时候不会敲。只认最后一段而不是整串去掉，
    因为前缀本身可能也是有用的词（`meta`、`qwen` 在模型名里也常见）。
    """
    text = (name or "").strip()
    found = max(text.rfind(sep) for sep in _VENDOR_SEPS)
    return text[found + 1:] if found >= 0 else text


def _is_subsequence(wanted: str, name: str) -> bool:
    """wanted 的字符按顺序出现在 name 里（可以跳过别的字符）。"""
    at = 0
    for char in name:
        if char == wanted[at]:
            at += 1
            if at == len(wanted):
                return True
    return False


def score(model: str, query: str) -> float:
    """上游真名 `model` 和输入 `query` 有多像：0 表示不相关，越大越像。"""
    name = normalize_key(model)
    wanted = normalize_key(query)
    if len(wanted) < 2 or not name:
        # 一个字符的查询等于没查：几乎所有模型名都含有它，列出来只会淹没真正的命中
        return 0.0
    if name == wanted:
        return float(EXACT)
    bare = normalize_key(strip_vendor(model))
    if bare and bare == wanted:
        return float(ALIAS)
    if wanted in name or name in wanted:
        return float(PARTIAL)
    if len(wanted) >= 4 and _is_subsequence(wanted, name):
        return float(FUZZY)
    return 0.0


def match_level(value: float) -> str:
    return LEVELS.get(value, "")


def is_exact(value: float) -> bool:
    """默认勾选只给「就是它」的那些：完全同名，或去掉厂商前缀后同名。"""
    return value >= ALIAS


def enabled_groups() -> tuple[db.Group, ...]:
    """所有启用供应商下启用的分组。停用的站/分组不打扰 —— 扫描不替人做启用决定。"""
    rows = db.list_upstreams()
    live = {u.id: u for u in rows if u.enabled}
    return tuple(g for g in db.list_groups() if g.enabled and g.upstream_id in live)


def _group_names(models: Iterable[str], query: str) -> tuple[tuple[str, float], ...]:
    """这个分组的模型列表里和 query 沾边的那些，按像不像排好。

    上游这次的列表和本地目录要并起来看，而同一个名字两边都可能有 —— 先按名字去重，
    否则一个候选会被列成两行，勾选时也会发出两条一模一样的请求。
    """
    found = {name for name in models if name}
    scored = [(name, score(name, query)) for name in found]
    scored = [(name, value) for name, value in scored if value > 0]
    scored.sort(key=lambda item: (-item[1], item[0].lower()))
    return tuple(scored)


@dataclass(frozen=True, slots=True)
class GroupMatch:
    """一个分组对这次查询的回答。``error`` 非空表示这个站没问成。"""

    group: db.Group
    upstream_name: str
    matches: tuple[tuple[str, float], ...] = ()
    error: str = ""
    registered: frozenset[str] = frozenset()

    @property
    def best(self) -> float:
        return self.matches[0][1] if self.matches else 0.0

    @property
    def exacts(self) -> tuple[str, ...]:
        return tuple(name for name, value in self.matches if is_exact(value))

    def is_registered(self, name: str) -> bool:
        return name in self.registered


def _shorten(exc: BaseException, limit: int = 160) -> str:
    text = str(exc).strip() or exc.__class__.__name__
    return text[:limit]


async def _ask_one(
    group: db.Group,
    base_url: str,
    header_override: str,
    egress: str,
    query: str,
    known: frozenset[str],
    deadline: float,
) -> GroupMatch:
    """问一个分组。目录里已经登记过的名字要一起并进来。

    上游的 `/v1/models` 常常是残的，而人工登记过、手填过的模型照样能用 —— 拉不到不等于
    没有，所以两边的匹配都算数，``registered`` 只用来在界面上标一下来源。
    """
    left = deadline - time.monotonic()
    if left <= 0:
        return GroupMatch(group, "", _group_names(known, query), "整轮扫描已超时，这个分组没轮到", known)
    try:
        # 超时用 wait_for 裹住整个「连上 + 读列表」：上游 socket 超时管不到排队等
        # 连接的那段时间，站多了以后这里才是真正会拖住整轮扫描的地方。
        models = await asyncio.wait_for(
            upstream_mod.fetch_remote_models(
                base_url, group.api_key, header_override, group.protocol, egress
            ),
            timeout=min(left, SCAN_GROUP_TIMEOUT),
        )
    except asyncio.TimeoutError:
        return GroupMatch(group, "", _group_names(known, query), "拉取超时", known)
    except Exception as exc:  # 单个站的失败不该让整轮扫描失败
        return GroupMatch(group, "", _group_names(known, query), _shorten(exc), known)
    return GroupMatch(group, "", _group_names((*models, *known), query), "", known)


async def scan(query: str) -> tuple[GroupMatch, ...]:
    """并发问每个启用的分组「有没有这个模型」，结果按相似度和分组顺序排好。

    同一个分组在每个协议下各有一条记录（分组本身是「一把 key + 一种接口」），
    所以返回的条数 = 启用分组的条数，前端按协议分组展示。
    """
    q = query.strip()
    groups = enabled_groups()
    if not groups or not q:
        return ()
    by_id = {u.id: u for u in db.list_upstreams()}
    catalogs = db.all_group_models()
    deadline = time.monotonic() + SCAN_BUDGET_SECONDS
    gate = asyncio.Semaphore(SCAN_CONCURRENCY)

    async def run(group: db.Group) -> GroupMatch:
        parent = by_id.get(group.upstream_id)
        if parent is None:
            return GroupMatch(group, "", error="供应商不存在")
        async with gate:
            got = await _ask_one(
                group,
                parent.base_url,
                parent.header_override,
                parent.egress,
                q,
                frozenset(catalogs.get(group.id, ())),
                deadline,
            )
        return GroupMatch(group, parent.name, got.matches, got.error, got.registered)

    results = await asyncio.gather(*(run(g) for g in groups))
    # 出错的排最后：有结果的先看，断了的站在下面单独说
    results.sort(
        key=lambda m: (
            bool(m.error),
            -m.best,
            m.upstream_name.lower(),
            m.group.id,
        )
    )
    return tuple(results)


@dataclass(frozen=True, slots=True)
class BatchGroup:
    group_id: int
    remote_model: str


async def commit(model_name: str, groups: Iterable[BatchGroup]) -> dict[str, object]:
    """把选中的（分组，上游真名）一次性登记 + 暴露。

    一条选择同时做两件事，因为它们本来就是同一件事的两半：登记进上游模型目录
    （这个站能调到它），并在「模型路由」里建一条候选（下游按 ``model_name`` 调它）。
    同一个分组重复指向同一个真名是幂等的 —— 扫描到提交之间可能已经被别处加过。
    """
    name = model_name.strip()
    wanted = [(int(item.group_id), item.remote_model.strip() or name) for item in groups]
    if not name or not wanted:
        return {"model_name": name, "committed": 0, "skipped": [], "protocols": []}

    result = db.add_model_routes_batch(name, wanted)
    log(
        f"MODEL-ADD {name!r}: {result['committed']} candidate(s)"
        f" [{', '.join(result['protocols'])}]"
    )
    return result
