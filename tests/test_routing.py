"""端到端测试 · 路由：候选的增删、停用之后的兜底、模型名绑定在一种接口上。"""

from __future__ import annotations

from helpers import MockUpstream, add_upstream, add_group, provider_id, add_route, cands, route_id, msg


def test_delete_active_candidate_reattaches_remaining(gateway):
    with MockUpstream("siteA") as a, MockUpstream("siteB") as b:
        g_a = add_upstream(gateway, a, "siteA")
        g_b = add_upstream(gateway, b, "siteB")
        rids = {gid: add_route(gateway, "shared", gid, "gpt-test") for gid in (g_a, g_b)}

        routes = gateway.get("/admin/api/models").json()
        shared = next(g for g in routes if g["model_name"] == "shared")
        assert shared["active_route_id"] == rids[g_a]

        assert gateway.delete(
            "/admin/api/models", params={"model_name": "shared", "group_id": g_a}
        ).status_code == 200
        routes = gateway.get("/admin/api/models").json()
        shared = next(g for g in routes if g["model_name"] == "shared")
        assert shared["active_route_id"] == rids[g_b]

        follow_up = gateway.post("/v1/responses", json={"model": "shared"}).json()
        assert follow_up["upstream"] == "siteB"


def test_disabled_upstream_is_not_routed(gateway):
    with MockUpstream("siteA") as a:
        g_a = add_upstream(gateway, a, "siteA")
        uid = provider_id(gateway, "siteA")
        gateway.post("/admin/api/models/bulk-add", json={"group_id": g_a, "model_names": ["gpt-test"]})

        detail = gateway.get("/admin/api/upstreams").json()[0]
        gateway.put(
            f"/admin/api/upstreams/{uid}",
            json={"name": detail["name"], "base_url": detail["base_url"], "enabled": False},
        )

        assert gateway.post("/v1/responses", json={"model": "gpt-test"}).status_code == 404
        exposed = {m["id"] for m in gateway.get("/v1/models").json()["data"]}
        assert "gpt-test" in exposed, "停用供应商只影响路由，不改变对下游暴露的模型清单"


def test_route_status_reports_the_available_fallback_when_preferred_is_disabled(gateway):
    with MockUpstream("siteA") as a, MockUpstream("siteB") as b:
        g_a = add_upstream(gateway, a, "siteA", "anthropic")
        g_b = add_upstream(gateway, b, "siteB", "anthropic")
        r_a = add_route(gateway, "opus", g_a, "opus-a")
        r_b = add_route(gateway, "opus", g_b, "opus-b")
        uid = provider_id(gateway, "siteA")
        detail = next(u for u in gateway.get("/admin/api/upstreams").json() if u["id"] == uid)

        disabled = gateway.put(
            f"/admin/api/upstreams/{uid}",
            json={
                "name": detail["name"],
                "base_url": detail["base_url"],
                "enabled": False,
                "egress": detail["egress"],
            },
        )
        assert disabled.status_code == 200, disabled.text

        row = next(r for r in gateway.get("/admin/api/models").json() if r["model_name"] == "opus")
        assert row["preferred_route_id"] == r_a
        assert row["active_route_id"] == r_b
        assert gateway.post("/v1/messages", json=msg("opus")).json()["upstream"] == "siteB"


def test_disabled_group_is_not_routed(gateway):
    """分组也能单独停用：同一个站的某把 key 额度用完了，先停这一组而不是整个站。"""
    with MockUpstream("siteA") as a:
        g_a = add_upstream(gateway, a, "siteA")
        add_route(gateway, "gpt-test", g_a)
        assert gateway.post("/v1/responses", json={"model": "gpt-test"}).status_code == 200

        r = gateway.put(
            f"/admin/api/groups/{g_a}",
            json={"name": "默认", "protocol": "openai", "api_key": "key-siteA", "enabled": False},
        )
        assert r.status_code == 200, r.text
        assert gateway.post("/v1/responses", json={"model": "gpt-test"}).status_code == 404
        assert "gpt-test" in {m["id"] for m in gateway.get("/v1/models").json()["data"]}


def test_model_name_with_slash_can_be_deleted(gateway):
    """公益站上很多模型名带 '/'，放在 URL 路径里会被当成多段，所以删除走 query 参数。"""
    with MockUpstream("siteA") as a:
        g_a = add_upstream(gateway, a, "siteA")
        name = "deepseek-ai/DeepSeek-V3"
        add_route(gateway, name, g_a, name)
        assert name in {m["id"] for m in gateway.get("/v1/models").json()["data"]}

        resp = gateway.delete("/admin/api/models", params={"model_name": name, "group_id": g_a})
        assert resp.status_code == 200, resp.text
        assert name not in {m["id"] for m in gateway.get("/v1/models").json()["data"]}


def test_delete_whole_model_removes_every_candidate(gateway):
    with MockUpstream("siteA") as a, MockUpstream("siteB") as b:
        g_a = add_upstream(gateway, a, "siteA")
        g_b = add_upstream(gateway, b, "siteB")
        for gid in (g_a, g_b):
            add_route(gateway, "shared", gid, "gpt-test")

        resp = gateway.delete("/admin/api/models", params={"model_name": "shared"})
        assert resp.json() == {"ok": True, "removed": 2}
        assert gateway.get("/admin/api/models").json() == []
        assert gateway.delete("/admin/api/models", params={"model_name": "shared"}).status_code == 404


def test_model_is_bound_to_one_interface(gateway):
    """模型的接口 = 它候选所在分组的接口。跨接口调是 404 —— 拿 Anthropic 的请求体去打人家的
    /v1/responses 只会得到垃圾。跨接口挂候选是 409，否则「这个名字在哪个接口下」就没答案了。"""
    with MockUpstream("siteA") as a:
        g_an = add_upstream(gateway, a, "siteA", "anthropic")
        uid = provider_id(gateway, "siteA")
        g_oa = add_group(gateway, uid, "openai", name="gpt", api_key="key-siteA")
        add_route(gateway, "opus", g_an, "claude-opus-4-1")

        assert {g["model_name"]: g["protocol"] for g in gateway.get("/admin/api/models").json()} \
            == {"opus": "anthropic"}
        assert gateway.post("/v1/messages", json=msg("opus")).status_code == 200

        wrong = gateway.post("/v1/responses", json={"model": "opus"})
        assert wrong.status_code == 404
        assert "anthropic" in wrong.json()["error"]["message"]

        dup = gateway.post("/admin/api/models", json={"model_name": "opus", "group_id": g_oa})
        assert dup.status_code == 409 and "接口" in dup.json()["detail"]
        assert "opus" in dup.json()["detail"], "得说清是哪个模型名撞了，一批几十个时才找得到"

        bad = gateway.post(f"/admin/api/upstreams/{uid}/groups", json={"name": "x", "protocol": "nope"})
        assert bad.status_code == 400


# ================================================================ 同一分组下的多条映射
#
# 一个分组 = 一个站的一把 key，它下面常常有好几个能用的模型 id。以前候选的主键是
# (模型名, 分组)，一个分组只塞得下一条；现在按 route_id 指，同分组可以挂好几条，
# 各指一个不同的上游真名。



def test_one_group_can_host_several_remote_names(gateway):
    """同一把 key 下的两个模型 id 是两条平级候选，只有「连真名都一样」才算重复。"""
    with MockUpstream("siteA") as a:
        g_a = add_upstream(gateway, a, "siteA", "anthropic")
        r1 = add_route(gateway, "opus", g_a, "claude-opus-4-1")
        r2 = add_route(gateway, "opus", g_a, "claude-opus-4-5")
        assert r1 != r2

        row = next(r for r in gateway.get("/admin/api/models").json() if r["model_name"] == "opus")
        assert [(c["group_id"], c["remote_model"]) for c in row["candidates"]] == [
            (g_a, "claude-opus-4-1"), (g_a, "claude-opus-4-5"),
        ]
        assert row["active_route_id"] == r1

        dup = gateway.post(
            "/admin/api/models",
            json={"model_name": "opus", "group_id": g_a, "remote_model": "claude-opus-4-1"},
        )
        assert dup.status_code == 409
        assert "claude-opus-4-1" in dup.json()["detail"], "得说清是跟哪条重复了"

        # 手动切到同分组的第二条：这是以前根本表达不出来的操作
        gateway.post("/admin/api/models/switch", json={"route_id": r2})
        assert gateway.post("/v1/messages", json=msg("opus")).json()["model"] == "claude-opus-4-5"


def test_editing_a_candidate_into_a_duplicate_is_409(gateway):
    """改真名改成同分组里另一条已经用着的名字，那两条就完全一样了。"""
    with MockUpstream("siteA") as a:
        g_a = add_upstream(gateway, a, "siteA", "anthropic")
        add_route(gateway, "opus", g_a, "claude-opus-4-1")
        r2 = add_route(gateway, "opus", g_a, "claude-opus-4-5")

        clash = gateway.put(
            "/admin/api/models", json={"route_id": r2, "remote_model": "claude-opus-4-1"}
        )
        assert clash.status_code == 409 and "claude-opus-4-1" in clash.json()["detail"]
        assert len(cands(gateway, "opus")) == 2, "冲突的改动不能落库"


def test_unchecking_a_model_removes_every_mapping_in_that_group(gateway):
    """分组弹窗里的勾选框答的是「这个模型在这个分组里有没有」，所以取消勾选
    （group_id 那种删法）要把同分组的几条映射一起去掉。"""
    with MockUpstream("siteA") as a, MockUpstream("siteB") as b:
        g_a = add_upstream(gateway, a, "siteA", "anthropic")
        g_b = add_upstream(gateway, b, "siteB", "anthropic")
        add_route(gateway, "opus", g_a, "opus-a1")
        add_route(gateway, "opus", g_a, "opus-a2")
        r_b = add_route(gateway, "opus", g_b, "opus-b")

        resp = gateway.delete("/admin/api/models", params={"model_name": "opus", "group_id": g_a})
        assert resp.json() == {"ok": True, "removed": 2}
        assert [c["route_id"] for c in cands(gateway, "opus")] == [r_b]
        # 活跃的那条被删了，流量自动落到剩下的候选上
        assert gateway.post("/v1/messages", json=msg("opus")).json()["upstream"] == "siteB"
