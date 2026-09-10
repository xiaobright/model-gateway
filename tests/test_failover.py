"""端到端测试 · 自动降级：换候选的顺序、冷却与半开恢复、哪些失败算站级哪些算模型级。"""

from __future__ import annotations

import pytest

from helpers import (
    MockUpstream, add_upstream, add_route, two_anthropic_sites, route_id, msg,
    parse_sse_events, wait_rows, rows_on,
)


# ================================================================ 自动降级
#
# 只在 Anthropic 接口默认开着：那边全是坏得勤的中转站。OpenAI 那边除了一个公益站
# 都是花钱买稳定的，换站要人点头，所以默认关闭 —— 见 test_failover_is_off_on_openai。



def test_failover_moves_to_the_next_candidate(gateway):
    """第一个候选打不通就换下一个，客户端不该看见这件事。

    每个候选的**上游真名和 key 都可能不一样**（真库里 claude-sonnet-5 在一个站叫
    claude-sonnet-5、在另一个站映射到 claude-opus-5），所以请求体和请求头都得按候选重建。
    """
    with MockUpstream("siteA") as a, MockUpstream("siteB") as b:
        two_anthropic_sites(gateway, a, b)
        a.fail_with(503)

        resp = gateway.post("/v1/messages", json=msg("opus"))
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["upstream"] == "siteB"
        assert body["model"] == "claude-opus-4-5", "换站之后请求体里的 model 要跟着换"
        assert body["x_api_key"] == "key-siteB", "key 也得是新候选那把"

        rows = wait_rows(gateway, 2)
        assert [(r["upstream"], r["status"], r["attempt"], r["note"]) for r in rows] == [
            ("siteA", 503, 1, "failed_over"),
            ("siteB", 200, 2, "ok"),
        ], "被降级掉的那次失败也要留痕：客户端没看见，但这次调用是真花了钱的"
        assert gateway.get("/admin/api/stats").json()["saved"] == 1


def test_failover_works_for_streams(gateway):
    """流式也能降级：状态码已经拿到、还没往下游发过任何字节，这是唯一安全的落点。"""
    with MockUpstream("siteA") as a, MockUpstream("siteB") as b:
        two_anthropic_sites(gateway, a, b)
        a.fail_with(503)

        with gateway.stream("POST", "/v1/messages", json=msg("opus", stream=True)) as stream:
            assert stream.status_code == 200
            raw = "".join(stream.iter_text())
        events = parse_sse_events(raw)
        assert events[0]["type"] == "message_start", "不能把坏站的半截响应混进来"
        assert events[-1]["type"] == "message_stop"
        assert events[0]["message"]["model"] == "claude-opus-4-5"


def test_failover_is_off_on_openai(gateway):
    """GPT 那边默认不自动换站，行为和加这个功能之前一字不差；打开开关才降级。"""
    with MockUpstream("siteA") as a, MockUpstream("siteB") as b:
        g_a = add_upstream(gateway, a, "siteA")
        g_b = add_upstream(gateway, b, "siteB")
        add_route(gateway, "gpt-test", g_a)
        add_route(gateway, "gpt-test", g_b)
        a.fail_with(503)

        assert gateway.get("/admin/api/failover").json()["enabled"] == {
            "anthropic": True, "openai": False, "openai-chat": False,
        }
        resp = gateway.post("/v1/responses", json={"model": "gpt-test"})
        assert resp.status_code == 503, "没开降级就该把上游的 503 原样透传下去"
        rows = wait_rows(gateway, 1)
        assert len(rows) == 1 and rows[0]["upstream"] == "siteA"

        gateway.post("/admin/api/failover", json={"protocol": "openai", "enabled": True})
        again = gateway.post("/v1/responses", json={"model": "gpt-test"})
        assert again.status_code == 200 and again.json()["upstream"] == "siteB"


def test_failover_skips_a_bad_request(gateway):
    """400 换个站也是同样的答案，重试只是白花一次调用。"""
    with MockUpstream("siteA") as a, MockUpstream("siteB") as b:
        two_anthropic_sites(gateway, a, b)
        a.fail_with(400)

        resp = gateway.post("/v1/messages", json=msg("opus"))
        assert resp.status_code == 400
        rows = wait_rows(gateway, 1)
        assert len(rows) == 1 and rows[0]["upstream"] == "siteA"


def test_failover_gives_up_after_three_attempts(gateway):
    """全都坏的时候要有个头：打三个就把最后那个的响应还给客户端，别把 40 万 token 重发五遍。"""
    from gateway import failover

    with MockUpstream("s1") as s1, MockUpstream("s2") as s2, \
            MockUpstream("s3") as s3, MockUpstream("s4") as s4:
        for i, site in enumerate((s1, s2, s3, s4), start=1):
            gid = add_upstream(gateway, site, site.name, "anthropic")
            add_route(gateway, "opus", gid, f"remote-{i}")
            site.fail_with(503)

        resp = gateway.post("/v1/messages", json=msg("opus"))
        assert resp.status_code == 503
        rows = wait_rows(gateway, failover.MAX_ATTEMPTS)
        assert [r["upstream"] for r in rows] == ["s1", "s2", "s3"]
        assert [r["attempt"] for r in rows] == [1, 2, 3]
        # 最后那次是原样透传下去的，不算「被降级接住」
        assert rows[-1]["note"] != "failed_over"


def test_breaker_stops_paying_for_a_dead_site(gateway):
    """真库里 站A 连续失败过 60 次、平均每次白等 16.6 秒 —— 连着坏就得躲开它，
    否则每个请求都要重新交一遍学费。"""
    with MockUpstream("siteA") as a, MockUpstream("siteB") as b:
        g_a, _ = two_anthropic_sites(gateway, a, b)
        a.fail_with(503)

        for _ in range(2):
            assert gateway.post("/v1/messages", json=msg("opus")).json()["upstream"] == "siteB"

        breakers = gateway.get("/admin/api/failover").json()["breakers"]
        assert [(x["group_id"], x["fails"], x["cooling_ms"] > 0) for x in breakers] == [(g_a, 2, True)]

        hits = rows_on(gateway, "siteA")
        assert gateway.post("/v1/messages", json=msg("opus")).json()["upstream"] == "siteB"
        assert rows_on(gateway, "siteA") == hits, "冷却期内根本不该再打它"


def test_manual_switch_clears_the_cooldown(gateway):
    """手动点了那个圆片就是明确指定，之前的连续失败不该继续把它挡在外面。"""
    with MockUpstream("siteA") as a, MockUpstream("siteB") as b:
        g_a, _ = two_anthropic_sites(gateway, a, b)
        a.fail_with(503)
        for _ in range(2):
            gateway.post("/v1/messages", json=msg("opus"))
        assert gateway.get("/admin/api/failover").json()["breakers"], "先让它进冷却"

        assert gateway.post(
            "/admin/api/models/switch", json={"route_id": route_id(gateway, "opus", g_a)}
        ).json() == {"ok": True}
        assert gateway.get("/admin/api/failover").json()["breakers"] == []

        hits = rows_on(gateway, "siteA")
        assert gateway.post("/v1/messages", json=msg("opus")).json()["upstream"] == "siteB"
        assert rows_on(gateway, "siteA") == hits + 1, "清了冷却就该重新试它一次"


def test_route_order_decides_who_is_tried_next(gateway):
    """圆片从左到右就是尝试顺序，站H 这种最稳的排最后当保底。"""
    with MockUpstream("siteA") as a, MockUpstream("siteB") as b, MockUpstream("siteC") as c:
        g_a, g_b = two_anthropic_sites(gateway, a, b)
        g_c = add_upstream(gateway, c, "siteC", "anthropic")
        add_route(gateway, "opus", g_c, "claude-opus-4-9")

        order = [route_id(gateway, "opus", gid) for gid in (g_a, g_c, g_b)]
        ordered = gateway.post(
            "/admin/api/models/order", json={"model_name": "opus", "order": order}
        )
        assert ordered.json() == {"ok": True, "ordered": 3}
        row = next(r for r in gateway.get("/admin/api/models").json() if r["model_name"] == "opus")
        assert [cand["group_id"] for cand in row["candidates"]] == [g_a, g_c, g_b]

        a.fail_with(503)
        b.fail_with(503)
        resp = gateway.post("/v1/messages", json=msg("opus"))
        assert resp.status_code == 200 and resp.json()["upstream"] == "siteC"
        rows = wait_rows(gateway, 2)
        assert [r["upstream"] for r in rows] == ["siteA", "siteC"], "第二个该按顺序轮到 C 而不是 B"


def test_stateful_openai_request_is_not_failed_over(gateway):
    """带 store / previous_response_id 的请求，上游可能已经存下来了才失败，重试会留下两条。"""
    with MockUpstream("siteA") as a, MockUpstream("siteB") as b:
        g_a = add_upstream(gateway, a, "siteA")
        g_b = add_upstream(gateway, b, "siteB")
        add_route(gateway, "gpt-test", g_a)
        add_route(gateway, "gpt-test", g_b)
        gateway.post("/admin/api/failover", json={"protocol": "openai", "enabled": True})
        a.fail_with(503)

        resp = gateway.post("/v1/responses", json={"model": "gpt-test", "store": True})
        assert resp.status_code == 503
        rows = wait_rows(gateway, 1)
        assert len(rows) == 1 and rows[0]["upstream"] == "siteA"


def test_model_level_404_tries_the_sibling_in_the_same_group(gateway):
    """上游把某个模型 id 下掉了（404）时，同一把 key 的另一个真名还有机会。

    404 不算这个分组的锅：站是通的、key 是好的，只是这个名字没了，所以不进冷却。
    """
    with MockUpstream("siteA") as a:
        g_a = add_upstream(gateway, a, "siteA", "anthropic")
        add_route(gateway, "opus", g_a, "claude-opus-4-1-20250805")
        add_route(gateway, "opus", g_a, "claude-opus-4-1")
        a.drop_model("claude-opus-4-1-20250805")

        resp = gateway.post("/v1/messages", json=msg("opus"))
        assert resp.status_code == 200, resp.text
        assert resp.json()["model"] == "claude-opus-4-1"

        rows = wait_rows(gateway, 2)
        assert [(r["remote_model"], r["status"], r["attempt"]) for r in rows] == [
            ("claude-opus-4-1-20250805", 404, 1),
            ("claude-opus-4-1", 200, 2),
        ]
        assert gateway.get("/admin/api/failover").json()["breakers"] == [], \
            "模型名没了不该让整把 key 进冷却"


def test_site_level_failure_skips_the_rest_of_that_group(gateway):
    """站级失败（503）时同分组的兄弟候选一起跳掉 —— 站都连不上，换个模型名没用。"""
    with MockUpstream("siteA") as a, MockUpstream("siteB") as b:
        g_a = add_upstream(gateway, a, "siteA", "anthropic")
        g_b = add_upstream(gateway, b, "siteB", "anthropic")
        add_route(gateway, "opus", g_a, "opus-a1")
        add_route(gateway, "opus", g_a, "opus-a2")
        add_route(gateway, "opus", g_b, "opus-b")
        a.fail_with(503)

        resp = gateway.post("/v1/messages", json=msg("opus"))
        assert resp.status_code == 200 and resp.json()["upstream"] == "siteB"
        rows = wait_rows(gateway, 2)
        assert [(r["upstream"], r["attempt"]) for r in rows] == [("siteA", 1), ("siteB", 2)]
        assert rows_on(gateway, "siteA") == 1, "同一个站不该在一次请求里被打两遍"
