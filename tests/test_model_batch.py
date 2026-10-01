"""批量添加新模型：相似度打分，以及「一次扫描 + 一次提交」两个管理接口。

这一段的核心承诺有两件事，都值得钉住：

- 扫描是**只读**的，而且一个站挂了不能拖垮整轮（那个分组单独报错，其余照常出结果）；
- 提交是**幂等**的：同一批勾选点两次不该报错，也不该产生第二条候选。
"""

from __future__ import annotations

import pytest

from helpers import MockUpstream, add_group, add_upstream, cands, provider_id
from gateway import model_batch
from gateway.model_batch import ALIAS, EXACT, FUZZY, PARTIAL, is_exact, score


# ---------------------------------------------------------------- 匹配


@pytest.mark.parametrize(
    "model,query,level",
    [
        ("gpt-test", "gpt-test", EXACT),
        ("GPT-Test", "gpt-test", EXACT),          # 大小写不算差别
        ("gpt-test-2024", "gpt-test", PARTIAL),
        ("gpt-test", "gpt-test-turbo", PARTIAL),
        ("deepseek-ai/DeepSeek-V3", "deepseek-v3", ALIAS),
        ("@cf/meta/llama-3", "llama-3", ALIAS),
        ("gpt5mini", "gpt5mn", FUZZY),            # 子序列：人少敲了几个字符
    ],
)
def test_score_levels(model, query, level):
    assert score(model, query) == level


@pytest.mark.parametrize("model,query", [("gpt-test", "claude"), ("gpt-test", "z"), ("", "gpt")])
def test_unrelated_or_too_short_queries_score_zero(model, query):
    """一个字符的查询会把整个站的模型都拉进来，等于没查。"""
    assert score(model, query) == 0


def test_only_near_names_are_preselected():
    """默认勾选只给「就是它」的那些 —— 部分匹配要人自己认。"""
    assert is_exact(EXACT) and is_exact(ALIAS)
    assert not is_exact(PARTIAL) and not is_exact(FUZZY)


def test_strip_vendor_keeps_the_last_segment():
    assert model_batch.strip_vendor("deepseek-ai/DeepSeek-V3") == "DeepSeek-V3"
    assert model_batch.strip_vendor("@cf/meta/llama-3") == "llama-3"
    assert model_batch.strip_vendor("gpt-test") == "gpt-test"


# ---------------------------------------------------------------- 扫描


def _matches_of(row: dict) -> list[str]:
    return [m["remote_model"] for m in row["matches"]]


def test_scan_finds_exact_first_and_does_not_write_anything(gateway):
    with MockUpstream("siteA") as a, MockUpstream("siteB") as b:
        add_upstream(gateway, a, "siteA")
        add_upstream(gateway, b, "siteB")

        got = gateway.post("/admin/api/models/scan", json={"model_name": "gpt-test"})
        assert got.status_code == 200, got.text
        body = got.json()
        assert body["scanned"] == 2 and body["matched"] == 2 and body["failed"] == 0
        # mock 上游的 /v1/models 里还有一个 claude-test，它和 gpt-test 不沾边，不该出现
        for row in body["groups"]:
            assert _matches_of(row) == ["gpt-test"]
            assert row["matches"][0]["level"] == "exact"

        assert gateway.get("/admin/api/models").json() == [], "扫描不许写配置"
        for row in gateway.get("/admin/api/upstreams").json():
            for group in row["groups"]:
                assert group["models"] == [], "扫描不许登记上游模型目录"


def test_scan_orders_exact_before_partial_and_reports_the_source(gateway):
    """同名和带后缀的同名要能分清：完全同名的排前面，默认勾选只认前者。"""
    with MockUpstream("siteA") as a:
        gid = add_upstream(gateway, a, "siteA")
        # 这个站拉得到 claude-test（不沾边），另有两条名字里带 gpt-test 的
        a.set_models("gpt-test", "claude-test", "gpt-testing-preview")

        body = gateway.post("/admin/api/models/scan", json={"model_name": "gpt-test"}).json()
        row = body["groups"][0]
        assert _matches_of(row) == ["gpt-test", "gpt-testing-preview"]
        assert [m["level"] for m in row["matches"]] == ["exact", "partial"]
        assert all(m["from_catalog"] is False for m in row["matches"])
        assert row["protocol"] == "openai" and row["protocol_enabled"] is True


def test_scan_includes_catalogued_names_the_upstream_no_longer_lists(gateway):
    """上游 /v1/models 是残的很常见：登记过、手填过的模型照样要能匹配上。

    否则「这个站上确实能调」的模型会因为列表不全而在这套流程里消失，人只能退回去手填。
    """
    with MockUpstream("siteA") as a:
        gid = add_upstream(gateway, a, "siteA")
        a.set_models("gpt-test")   # 这个站的 /v1/models 里没有下面登记的那个
        assert gateway.post(
            f"/admin/api/groups/{gid}/models", json={"model_names": ["moonshot-kimi-k2"]}
        ).status_code == 200

        body = gateway.post("/admin/api/models/scan", json={"model_name": "kimi-k2"}).json()
        row = body["groups"][0]
        assert _matches_of(row) == ["moonshot-kimi-k2"]
        assert row["matches"][0]["from_catalog"] is True, "界面要能说明它是本地登记来的"


def test_scan_skips_disabled_upstreams_and_groups(gateway):
    with MockUpstream("siteA") as a, MockUpstream("siteB") as b:
        g_a = add_upstream(gateway, a, "siteA")
        add_upstream(gateway, b, "siteB")
        gateway.put(
            f"/admin/api/upstreams/{provider_id(gateway, 'siteB')}",
            json={"name": "siteB", "base_url": b.base_url, "enabled": False},
        )
        assert gateway.post(f"/admin/api/groups/{g_a}/enabled", json={"enabled": False}).status_code == 200

        body = gateway.post("/admin/api/models/scan", json={"model_name": "gpt-test"}).json()
        assert body["groups"] == [], "停用的站和分组不该被扫描打扰"
        assert body["scanned"] == 0


def test_scan_survives_one_broken_site(gateway, monkeypatch):
    """一个站连不上只是那一行带 error，别的站必须照常出结果。"""
    with MockUpstream("siteA") as a, MockUpstream("siteB") as b:
        add_upstream(gateway, a, "siteA")
        g_b = add_upstream(gateway, b, "siteB")

        real = model_batch.upstream_mod.fetch_remote_models

        # add_upstream 给每个分组配的是 "key-<站名>"，按它区分是哪个站
        def boom(base_url, api_key, header_override="", protocol="openai", egress=""):
            if api_key == "key-siteB":
                raise RuntimeError(f"{base_url}/v1/models 返回 502")
            return real(base_url, api_key, header_override, protocol, egress)

        monkeypatch.setattr(model_batch.upstream_mod, "fetch_remote_models", boom)
        body = gateway.post("/admin/api/models/scan", json={"model_name": "gpt-test"}).json()
        assert body["failed"] == 1 and body["matched"] == 1
        by_id = {row["group_id"]: row for row in body["groups"]}
        assert len(by_id) == 2
        broken = [row for row in body["groups"] if row["error"]]
        assert len(broken) == 1 and broken[0]["group_id"] == g_b and "502" in broken[0]["error"]
        assert broken[0]["matches"] == [], "断了的站不该凭空编出匹配"
        good = next(row for row in body["groups"] if not row["error"])
        assert _matches_of(good) == ["gpt-test"]


def test_scan_reports_budget_exhaustion_as_a_group_error(gateway, monkeypatch):
    """整轮预算用完之后不再发请求，剩下的分组各记一条「没轮到」。"""
    with MockUpstream("siteA") as a, MockUpstream("siteB") as b:
        add_upstream(gateway, a, "siteA")
        add_upstream(gateway, b, "siteB")
        monkeypatch.setattr(model_batch, "SCAN_BUDGET_SECONDS", -1.0)
        body = gateway.post("/admin/api/models/scan", json={"model_name": "gpt-test"}).json()
        assert body["failed"] == 2 and body["matched"] == 0
        assert all("没轮到" in row["error"] for row in body["groups"])


def test_scan_requires_a_model_name(gateway):
    # 只有空白的名字过得了 Pydantic 的 min_length，要在入口挡成 400 而不是去问所有站
    assert gateway.post("/admin/api/models/scan", json={"model_name": "   "}).status_code == 400
    assert gateway.post("/admin/api/models/scan", json={}).status_code == 422
    assert gateway.post("/admin/api/models/scan", json={"model_name": "x", "extra": 1}).status_code == 422


# ---------------------------------------------------------------- 提交


def _scan_row(gateway, query: str) -> list[dict]:
    return gateway.post("/admin/api/models/scan", json={"model_name": query}).json()["groups"]


def _catalog(gateway, group_id: int) -> list[str]:
    for row in gateway.get("/admin/api/upstreams").json():
        for group in row["groups"]:
            if group["id"] == group_id:
                return group["models"]
    raise AssertionError(f"分组 {group_id} 不在列表里")


def test_batch_registers_catalog_and_exposes_candidates_in_one_call(gateway):
    """一条勾选同时做两件事：登记这个站有它，并在模型路由里建候选。"""
    with MockUpstream("siteA") as a, MockUpstream("siteB") as b:
        g_a = add_upstream(gateway, a, "siteA")
        g_b = add_upstream(gateway, b, "siteB")
        rows = {row["group_id"]: row for row in _scan_row(gateway, "gpt-test")}
        assert sorted(rows) == sorted([g_a, g_b]) and all(r["matches"] for r in rows.values())

        made = gateway.post(
            "/admin/api/models/batch",
            json={
                "model_name": "gpt-test",
                "groups": [
                    {"group_id": g_a, "remote_model": "gpt-test"},
                    {"group_id": g_b, "remote_model": "gpt-test"},
                ],
            },
        )
        assert made.status_code == 200, made.text
        body = made.json()
        assert body["committed"] == 2 and body["protocols"] == ["openai"]
        assert "gpt-test" in body["models"]

        assert _catalog(gateway, g_a) == ["gpt-test"]
        assert _catalog(gateway, g_b) == ["gpt-test"]
        chain = cands(gateway, "gpt-test")
        assert [c["group_id"] for c in chain] == [g_a, g_b], "提交顺序就是链上的尝试顺序"
        assert chain[0]["is_active"] is True and chain[1]["is_active"] is False


def test_batch_is_idempotent_and_idempotence_is_reported(gateway):
    with MockUpstream("siteA") as a:
        g_a = add_upstream(gateway, a, "siteA")
        payload = {"model_name": "gpt-test", "groups": [{"group_id": g_a, "remote_model": "gpt-test"}]}
        first = gateway.post("/admin/api/models/batch", json=payload).json()
        assert first["committed"] == 1 and first["skipped"] == []

        again = gateway.post("/admin/api/models/batch", json=payload).json()
        assert again["committed"] == 0
        assert again["skipped"] == [{"group_id": g_a, "reason": "已经加过了"}]
        assert len(cands(gateway, "gpt-test")) == 1, "重复提交不该多出一条候选"


def test_batch_exposes_a_catalogued_model_that_was_never_exposed(gateway):
    """只登记过目录、还没暴露的模型：再提交一次应该把候选补上，而不是说「已经加过了」。"""
    with MockUpstream("siteA") as a:
        g_a = add_upstream(gateway, a, "siteA")
        gateway.post(f"/admin/api/groups/{g_a}/models", json={"model_names": ["gpt-test"]})
        assert gateway.get("/admin/api/models").json() == []

        body = gateway.post(
            "/admin/api/models/batch",
            json={"model_name": "gpt-test", "groups": [{"group_id": g_a, "remote_model": "gpt-test"}]},
        ).json()
        assert body["committed"] == 1 and body["skipped"] == []
        assert len(cands(gateway, "gpt-test")) == 1


def test_batch_skips_unknown_groups_without_failing_the_rest(gateway):
    with MockUpstream("siteA") as a:
        g_a = add_upstream(gateway, a, "siteA")
        body = gateway.post(
            "/admin/api/models/batch",
            json={
                "model_name": "gpt-test",
                "groups": [
                    {"group_id": 9999, "remote_model": "gpt-test"},
                    {"group_id": g_a, "remote_model": "gpt-test"},
                ],
            },
        ).json()
        assert body["committed"] == 1
        assert body["skipped"] == [{"group_id": 9999, "reason": "分组不存在"}]


def test_batch_with_no_selection_is_a_no_op(gateway):
    body = gateway.post("/admin/api/models/batch", json={"model_name": "gpt-test", "groups": []}).json()
    assert body["committed"] == 0 and body["protocols"] == []


def test_batch_remote_name_defaults_to_the_downstream_name(gateway):
    """上游真名留空 = 与下游模型名同名（和「新增模型」弹窗一个约定）。"""
    with MockUpstream("siteA") as a:
        g_a = add_upstream(gateway, a, "siteA")
        body = gateway.post(
            "/admin/api/models/batch",
            json={"model_name": "gpt-test", "groups": [{"group_id": g_a, "remote_model": ""}]},
        ).json()
        assert body["committed"] == 1
        assert cands(gateway, "gpt-test")[0]["remote_model"] == "gpt-test"


def test_batch_exposes_the_same_name_under_every_selected_protocol(gateway):
    """同一个名字在两种接口下各有一条独立的链 —— 勾了两边的站就两边都暴露。"""
    with MockUpstream("siteA") as a:
        g_openai = add_upstream(gateway, a, "siteA")
        uid = provider_id(gateway, "siteA")
        g_anthropic = add_group(gateway, uid, "anthropic", name="claude", api_key="key-siteA")

        rows = {row["group_id"]: row for row in _scan_row(gateway, "claude-test")}
        assert rows[g_openai]["protocol"] == "openai"
        assert rows[g_anthropic]["protocol"] == "anthropic"
        assert rows[g_anthropic]["matches"][0]["remote_model"] == "claude-test"

        body = gateway.post(
            "/admin/api/models/batch",
            json={
                "model_name": "claude-test",
                "groups": [
                    {"group_id": g_openai, "remote_model": "claude-test"},
                    {"group_id": g_anthropic, "remote_model": "claude-test"},
                ],
            },
        ).json()
        assert body["committed"] == 2
        assert body["protocols"] == ["anthropic", "openai"]
        # 同名模型在两种接口下各有一条独立的链，两边的候选互不干扰
        assert [c["group_id"] for c in cands(gateway, "claude-test", "openai")] == [g_openai]
        assert [c["group_id"] for c in cands(gateway, "claude-test", "anthropic")] == [g_anthropic]
