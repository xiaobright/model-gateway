"""分组拉模型列表时，每个上游真名能对应到哪些下游模型。

「这个站有什么」（上游目录 group_models）和「对外暴露什么」（下游候选
model_routes）是两件事：分组弹窗的勾选只管前者，这里回答后者的一半问题 ——
拉回来的名字和下游现有的模型名有多像。完全同名、去掉厂商前缀后同名算「完全
匹配」，只沾一部分算「部分匹配」。选择框把可对应的下游列出来，合不合并、新不
新开由人定；已经挂在下游底下的（这个分组、这个真名已经是某条链的候选）标出来，
那种行在界面上是锁着的 —— 改映射去「模型路由」，那边的候选才能删能改。

打分不另写一份规则：复用 model_batch.score（上游真名和一个名字比有多像），
这里只决定「哪些算完全、哪些算部分、各列几个」。
"""

from __future__ import annotations

from typing import Iterable

from . import db
from . import model_batch

# 部分匹配最多列几个下游。选择框列十几条没人看得完，取最像的前几个就够；
# 想对应到没列出来的那个，去「模型路由」给那条链加候选。
PARTIAL_LIMIT = 5


def downstream_names() -> set[str]:
    """下游已有的模型名 —— 对应的候选目标。

    只算**自己有候选**的模型：纯转发模型（整条链交给别人）自己的候选不参与路由，
    对应过去也是睡着的一条，不给这个选项。
    """
    return {row["model_name"] for row in db.list_routes()}


def plan(remote_model: str, names: Iterable[str]) -> tuple[list[str], list[str]]:
    """这个上游真名能对应到哪些下游模型，返回 (完全匹配, 部分匹配)。

    完全匹配按名字字典序；部分匹配按相似度排（同分按名字），只取前 PARTIAL_LIMIT
    个。同一个名字在多种接口下各有一条链也只出现一次 —— 选对应的是「哪个下游
    模型」，接口由候选所在的分组决定，这里不掺和。
    """
    exact: list[str] = []
    partial: list[tuple[float, str]] = []
    for name in sorted(set(names)):
        value = model_batch.score(remote_model, name)
        if value >= model_batch.ALIAS:
            exact.append(name)
        elif value > 0:
            partial.append((value, name))
    partial.sort(key=lambda item: (-item[0], item[1]))
    return exact, [name for _, name in partial[:PARTIAL_LIMIT]]


def exposed_as(group_id: int) -> dict[str, list[str]]:
    """这个分组里每个已暴露的上游真名对应着哪些下游模型（真名 -> 下游模型名）。

    一个真名可以挂在好几个下游模型底下（各是一条候选），所以值是列表。
    """
    out: dict[str, set[str]] = {}
    for row in db.list_routes():
        if row["group_id"] == group_id:
            out.setdefault(row["remote_model"], set()).add(row["model_name"])
    return {remote: sorted(names) for remote, names in out.items()}
