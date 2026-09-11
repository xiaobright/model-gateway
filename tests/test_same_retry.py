"""端到端测试 · 同站重试：命中供应商规则就原站再发，优先于自动降级换站。"""

from __future__ import annotations

from helpers import (
    MockUpstream,
    add_upstream,
    add_route,
    provider_id,
    two_anthropic_sites,
    wait_rows,
    msg,
)


def _set_retry(client, upstream_id: int, rules: str, **extra) -> None:
    rows = client.get("/admin/api/upstreams").json()
    row = next(u for u in rows if u["id"] == upstream_id)
    resp = client.put(
        f"/admin/api/upstreams/{upstream_id}",
        json={
            "name": row["name"],
            "base_url": row["base_url"],
            "enabled": row["enabled"],
            "header_override": row.get("header_override") or "",
            "egress": row.get("egress") or "",
            "retry_rules": rules,
            **extra,
        },
    )
    assert resp.status_code == 200, resp.text


def test_same_retry_400_then_success(gateway):
    """站A 那种：偶发 400，下一发就好。同站吞掉，客户端只看见 200。"""
    with MockUpstream("siteA") as a:
        g = add_upstream(gateway, a, "siteA", "openai-chat")
        up = provider_id(gateway, "siteA")
        _set_retry(gateway, up, '[{"status":400,"times":2,"delay_ms":0}]')
        add_route(gateway, "deepseek-v4-flash", g)
        a.fail_with(400, times=1)

        resp = gateway.post(
            "/v1/chat/completions", json={"model": "deepseek-v4-flash", "stream": False}
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["upstream"] == "siteA"

        rows = wait_rows(gateway, 2)
        assert [(r["status"], r["note"], r["upstream"]) for r in rows] == [
            (400, "same_retry", "siteA"),
            (200, "ok", "siteA"),
        ]


def test_same_retry_respects_times_cap(gateway):
    """times=2：最多再发 2 把；第 3 次仍 400 就把错误还给客户端。"""
    with MockUpstream("siteA") as a:
        g = add_upstream(gateway, a, "siteA", "openai-chat")
        up = provider_id(gateway, "siteA")
        _set_retry(gateway, up, '[{"status":400,"times":2,"delay_ms":0}]')
        add_route(gateway, "deepseek-v4-flash", g)
        a.fail_with(400)  # 一直坏

        resp = gateway.post(
            "/v1/chat/completions", json={"model": "deepseek-v4-flash"}
        )
        assert resp.status_code == 400
        rows = wait_rows(gateway, 3)
        assert len(rows) == 3
        assert all(r["upstream"] == "siteA" for r in rows)
        assert rows[0]["note"] == "same_retry"
        assert rows[1]["note"] == "same_retry"
        assert rows[2]["note"] != "same_retry"


def test_without_rule_400_is_not_retried(gateway):
    """没配规则时行为和以前一样：400 原样透传，只打一次。"""
    with MockUpstream("siteA") as a:
        g = add_upstream(gateway, a, "siteA", "openai-chat")
        add_route(gateway, "deepseek-v4-flash", g)
        a.fail_with(400)

        resp = gateway.post("/v1/chat/completions", json={"model": "deepseek-v4-flash"})
        assert resp.status_code == 400
        rows = wait_rows(gateway, 1)
        assert len(rows) == 1 and rows[0]["note"] == "ok"


def test_same_retry_before_failover_on_503(gateway):
    """同站重试优先：503 先原站再试一把，成了就不用换站。"""
    with MockUpstream("siteA") as a, MockUpstream("siteB") as b:
        two_anthropic_sites(gateway, a, b)
        up_a = provider_id(gateway, "siteA")
        _set_retry(gateway, up_a, '[{"status":503,"times":2,"delay_ms":0}]')
        a.fail_with(503, times=1)

        resp = gateway.post("/v1/messages", json=msg("opus"))
        assert resp.status_code == 200, resp.text
        assert resp.json()["upstream"] == "siteA", "同站第二次就通了，不该换到 siteB"

        rows = wait_rows(gateway, 2)
        notes = [(r["upstream"], r["status"], r["note"]) for r in rows]
        assert notes == [("siteA", 503, "same_retry"), ("siteA", 200, "ok")]


def test_same_retry_exhausted_then_failover(gateway):
    """同站次数用完仍 503，再走原有自动降级换下一个候选。"""
    with MockUpstream("siteA") as a, MockUpstream("siteB") as b:
        two_anthropic_sites(gateway, a, b)
        up_a = provider_id(gateway, "siteA")
        _set_retry(gateway, up_a, '[{"status":503,"times":1,"delay_ms":0}]')
        a.fail_with(503)  # 一直坏

        resp = gateway.post("/v1/messages", json=msg("opus"))
        assert resp.status_code == 200
        assert resp.json()["upstream"] == "siteB"

        rows = wait_rows(gateway, 3)
        trail = [(r["upstream"], r["status"], r["note"]) for r in rows]
        assert trail[0][0] == "siteA" and trail[0][2] == "same_retry"
        assert trail[1] == ("siteA", 503, "failed_over")
        assert trail[2] == ("siteB", 200, "ok")


def test_retry_rules_roundtrip_via_admin(gateway):
    """保存 / 读回 retry_rules；非法 JSON 被拒。"""
    with MockUpstream("siteA") as a:
        add_upstream(gateway, a, "siteA")
        up = provider_id(gateway, "siteA")
        _set_retry(gateway, up, '[{"status":400,"times":2,"delay_ms":100}]')
        row = next(u for u in gateway.get("/admin/api/upstreams").json() if u["id"] == up)
        assert row["retry_rules"] == '[{"status":400,"times":2,"delay_ms":100}]'

        bad = gateway.put(
            f"/admin/api/upstreams/{up}",
            json={
                "name": "siteA",
                "base_url": a.base_url,
                "retry_rules": "{not json}",
            },
        )
        assert bad.status_code == 400
