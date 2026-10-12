"""分组拉模型列表时的下游对应建议：分档纯函数和只读的 plan 接口。

核心承诺：建议是**只读**的（问一遍不写任何配置），已经挂在这个分组底下的真名会被
标出来 —— 那种行在界面上是锁着的，改映射去「模型路由」。
"""

from __future__ import annotations

import pytest

from helpers import MockUpstream, add_group, add_route, add_upstream, provider_id
from gateway import model_match


# ---------------------------------------------------------------- 分档


@pytest.mark.parametrize(
    "remote,names,exact,partial",
    [
        ("gpt-test", ["gpt-test"], ["gpt-test"], []),
        ("GPT-Test", ["gpt-test"], ["gpt-test"], []),               # 大小写不算差别
        ("deepseek-ai/DeepSeek-V3", ["DeepSeek-V3"], ["DeepSeek-V3"], []),
        ("gpt-test-2024", ["gpt-test"], [], ["gpt-test"]),          # 带日期后缀
        ("deepseek-ai/DeepSeek-V4-Flash-0731", ["deepseek-v4-flash"], [], ["deepseek-v4-flash"]),
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


def test_plan_recognizes_a_prefixed_remote_under_its_short_downstream_name(gateway):
    """实测场景：聚合站的真名带厂商前缀和日期后缀，人把它加到了短名底下
    （deepseek-ai/DeepSeek-V4-Flash-0731 → deepseek-v4-flash）。界面要能认出它
    已经挂好了，还要指出那个短名是部分匹配的合并目标。"""
    with MockUpstream("siteA") as a:
        g_a = add_upstream(gateway, a, "siteA")
        add_route(gateway, "deepseek-v4-flash", g_a, "deepseek-ai/DeepSeek-V4-Flash-0731")
        add_route(gateway, "glm-5.3-flash", g_a, "glm-5.3-flash")

        by_name = {
            p["remote_model"]: p
            for p in _plan(gateway, g_a, ["deepseek-ai/DeepSeek-V4-Flash-0731", "glm-5.3-flash"])["plans"]
        }
        prefixed = by_name["deepseek-ai/DeepSeek-V4-Flash-0731"]
        assert prefixed["exposed_as"] == ["deepseek-v4-flash"]
        assert prefixed["exact"] == [] and prefixed["partial"] == ["deepseek-v4-flash"]
        assert by_name["glm-5.3-flash"]["exposed_as"] == ["glm-5.3-flash"]
        assert by_name["glm-5.3-flash"]["exact"] == ["glm-5.3-flash"]


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


def test_plan_only_matches_models_exposed_in_the_group_s_own_interface(gateway):
    """实测：Chat Completions 的分组不该把 Responses 那边的同名模型算成「完全匹配」。

    同名模型在两种接口下是两条互不可见的链（转发按「模型名 + 接口」选链），隔壁
    接口暴露了不等于这边调得到 —— 列成完全匹配只会让人以为已经挂好了。同一个站
    同一种接口下另一把 key 挂的候选照旧算匹配。
    """
    with MockUpstream("siteA") as a:
        g_resp = add_upstream(gateway, a, "siteA")                              # openai（Responses）
        g_chat = add_group(gateway, provider_id(gateway, "siteA"), "openai-chat", api_key="key-siteA")
        add_route(gateway, "gpt-test", g_resp, "gpt-test")                      # 只在 Responses 下暴露
        add_route(gateway, "gpt-test", g_resp, "gpt-test-2024")                 # 同接口另一把 key 的候选

        chat = {p["remote_model"]: p for p in _plan(gateway, g_chat, ["gpt-test", "gpt-test-2024"])["plans"]}
        assert chat["gpt-test"]["exact"] == [] and chat["gpt-test"]["partial"] == []
        assert chat["gpt-test-2024"]["exact"] == [] and chat["gpt-test-2024"]["partial"] == []
        # 同接口的另一边照旧算匹配：完全匹配、部分匹配都还有
        resp = {p["remote_model"]: p for p in _plan(gateway, g_resp, ["gpt-test", "gpt-test-2024"])["plans"]}
        assert resp["gpt-test"]["exact"] == ["gpt-test"]
        assert resp["gpt-test-2024"]["partial"] == ["gpt-test"]


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
