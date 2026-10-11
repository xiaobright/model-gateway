"""分组拉模型列表时的下游对应建议：分档纯函数和只读的 plan 接口。

核心承诺：建议是**只读**的（问一遍不写任何配置），已经挂在这个分组底下的真名会被
标出来 —— 那种行在界面上是锁着的，改映射去「模型路由」。
"""

from __future__ import annotations

import pytest

from helpers import MockUpstream, add_route, add_upstream
from gateway import model_match


# ---------------------------------------------------------------- 分档


@pytest.mark.parametrize(
    "remote,names,exact,partial",
    [
        ("gpt-test", ["gpt-test"], ["gpt-test"], []),
        ("GPT-Test", ["gpt-test"], ["gpt-test"], []),               # 大小写不算差别
        ("deepseek-ai/DeepSeek-V3", ["DeepSeek-V3"], ["DeepSeek-V3"], []),
        ("gpt-test-2024", ["gpt-test"], [], ["gpt-test"]),          # 带日期后缀
        ("kimi-k2", ["gpt-test"], [], []),                          # 不沾边
        ("gpt-test", ["gpt-test", "gpt-test"], ["gpt-test"], []),   # 同名多接口只列一次
    ],
)
def test_plan_buckets(remote, names, exact, partial):
    assert model_match.plan(remote, names) == (exact, partial)


def test_plan_lists_every_exact_match_sorted():
    exact, partial = model_match.plan("GPT-Test", ["gpt-test", "Gpt-Test"])
    assert exact == ["Gpt-Test", "gpt-test"] and partial == []


def test_plan_orders_partial_deterministically_and_caps_the_list():
    """同分按名字字典序（两次扫描给同一份建议），且只列前 PARTIAL_LIMIT 个。"""
    names = [f"gpt-test-{c}" for c in "gfedcba"]   # 七个都沾边的
    exact, partial = model_match.plan("gpt-test", names)
    assert exact == []
    assert partial == sorted(names)[:model_match.PARTIAL_LIMIT]


# ---------------------------------------------------------------- 接口


def _plan(gateway, gid: int, names: list[str]) -> dict:
    return gateway.post(f"/admin/api/groups/{gid}/models/plan", json={"model_names": names}).json()


def _catalog(gateway, group_id: int) -> list[str]:
    for row in gateway.get("/admin/api/upstreams").json():
        for group in row["groups"]:
            if group["id"] == group_id:
                return group["models"]
    raise AssertionError(f"分组 {group_id} 不在列表里")


def test_plan_endpoint_is_read_only(gateway):
    with MockUpstream("siteA") as a, MockUpstream("siteB") as b:
        g_a = add_upstream(gateway, a, "siteA")
        g_b = add_upstream(gateway, b, "siteB")
        add_route(gateway, "gpt-test", g_a, "gpt-test")   # 下游已有 gpt-test（候选在 siteA 上）

        body = _plan(gateway, g_b, ["gpt-test", "gpt-test-2024", "kimi-k2"])
        by_name = {p["remote_model"]: p for p in body["plans"]}
        assert by_name["gpt-test"]["exact"] == ["gpt-test"]
        assert by_name["gpt-test-2024"]["partial"] == ["gpt-test"]
        assert by_name["kimi-k2"]["exact"] == [] and by_name["kimi-k2"]["partial"] == []
        # siteB 上这几个真名都还没挂下游
        assert all(p["exposed_as"] == [] for p in body["plans"])

        assert _catalog(gateway, g_b) == [], "问建议不许登记上游模型目录"
        assert [r["model_name"] for r in gateway.get("/admin/api/models").json()] == ["gpt-test"]


def test_plan_marks_the_group_s_own_exposed_models(gateway):
    """已经挂在这个分组底下的真名要标出来 —— 界面上那一行是锁着的。"""
    with MockUpstream("siteA") as a:
        g_a = add_upstream(gateway, a, "siteA")
        add_route(gateway, "my-chat", g_a, "gpt-test")
        add_route(gateway, "gpt-test", g_a, "gpt-test")   # 同一个真名挂在两个下游底下

        by_name = {p["remote_model"]: p for p in _plan(gateway, g_a, ["gpt-test", "kimi-k2"])["plans"]}
        assert by_name["gpt-test"]["exposed_as"] == ["gpt-test", "my-chat"]
        assert by_name["kimi-k2"]["exposed_as"] == []


def test_plan_does_not_offer_forward_only_models(gateway):
    """纯转发模型自己的候选不参与路由，对应过去也是睡着的一条。"""
    with MockUpstream("siteA") as a:
        g_a = add_upstream(gateway, a, "siteA")
        add_route(gateway, "gpt-test", g_a, "gpt-test")
        assert gateway.post("/admin/api/models/forward", json={
            "model_name": "my-chat", "protocol": "openai", "target_model": "gpt-test",
        }).status_code == 200

        plan = _plan(gateway, g_a, ["my-chat"])["plans"][0]
        assert plan["exact"] == [] and plan["partial"] == []


def test_plan_rejects_bad_payloads(gateway):
    with MockUpstream("siteA") as a:
        g_a = add_upstream(gateway, a, "siteA")
        assert gateway.post(
            f"/admin/api/groups/{g_a}/models/plan", json={"model_names": []}
        ).status_code == 422
        assert gateway.post(
            f"/admin/api/groups/{g_a}/models/plan", json={"model_names": ["x"], "extra": 1}
        ).status_code == 422
        assert gateway.post(
            "/admin/api/groups/9999/models/plan", json={"model_names": ["x"]}
        ).status_code == 404
