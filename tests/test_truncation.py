"""200 截断拦截（守卫）：连着拿回「没有正文的 200」就先扣住、在原站原样重发。

真库里的形状（anyrouter / AgentRouter）：上游回 200、正文是一段和请求无关的错误信封
（`rate limit exceeded: ... exceeded token rate limit`），没有一个字、也没有完成事件，
客户端只看到「流被截断」，自己重试几次全是这个，于是整轮停下来要人手动继续。

这里演的「空响应」是同一件事的最小形状：200 + 只有生命周期事件、没有正文、没有完成事件。
上游用 helpers 的 stream_script() 按顺序排。

**注意「连着坏」认的是站 + 模型 + 端点，不是同一份请求**：真跑起来客户端每次重试的
请求体内容都不一样（长度倒是相同），按请求体摘要认「同一条」的话守卫永远不武装。
`test_guard_arms_across_retries_though_the_body_changes` 盯着这件事。
"""

from __future__ import annotations

import pytest

from helpers import (
    MockUpstream,
    add_route,
    add_upstream,
    provider_id,
    wait_rows,
)

from gateway import truncation

# 请求体里带 mode="script" 才会走剧本
BODY = {"model": "gpt-6-astra", "stream": True, "mode": "script", "input": "hi"}


def _set_hold(client, upstream_id: int, rules: str) -> None:
    """改供应商的 200 截断拦截配置（其余字段原样带回去，和列表快捷开关的口径一致）。"""
    row = next(u for u in client.get("/admin/api/upstreams").json() if u["id"] == upstream_id)
    resp = client.put(
        f"/admin/api/upstreams/{upstream_id}",
        json={
            "name": row["name"],
            "base_url": row["base_url"],
            "enabled": row["enabled"],
            "header_override": row.get("header_override") or "",
            "egress": row.get("egress") or "",
            "retry_rules": row.get("retry_rules") or "",
            "hold_retry": rules,
        },
    )
    assert resp.status_code == 200, resp.text


def _armed(client) -> list[dict]:
    """当前被守卫记住的请求（/failover 里顺手带出来的那张表）。"""
    return client.get("/admin/api/failover").json()["truncation"]


def test_empty_200_is_held_and_resent_until_good(gateway):
    """连着两次空响应之后，第三次先扣住；还是空的就原站重发，直到拿到能用的流。"""
    with MockUpstream("siteA") as a:
        g = add_upstream(gateway, a, "siteA", "openai")
        _set_hold(gateway, provider_id(gateway, "siteA"), '{"after":2,"times":3,"delay_ms":0}')
        add_route(gateway, "gpt-6-astra", g)
        # 前三次是空的，第四次（也就是扣住之后的那把重发）好
        a.stream_script("empty", "empty", "empty", "good")

        # 前两次：守卫还没武装，空响应照旧原样交给下游 —— 和没有这个功能时一字不差
        for _ in range(2):
            first = gateway.post("/v1/responses", json=BODY)
            assert first.status_code == 200
            assert "[DONE]" not in first.text

        rows = wait_rows(gateway, 2)
        assert [(r["status"], r["note"]) for r in rows] == [(200, "truncated"), (200, "truncated")]
        # 真库里那种空响应就是这个形状：有响应字节、一个正文字节都没有
        assert all(r["resp_bytes"] > 0 and r["resp_text_bytes"] == 0 for r in rows)
        assert len(_armed(gateway)) == 1 and _armed(gateway)[0]["fails"] == 2

        # 第三次：已武装 → 扣住 → 还是空的 → 原站重发 → 这次好了
        third = gateway.post("/v1/responses", json=BODY)
        assert third.status_code == 200, third.text
        assert "[DONE]" in third.text, "扣住重发之后应该把正常流交给下游"

        rows = wait_rows(gateway, 4)
        assert [(r["status"], r["note"]) for r in rows] == [
            (200, "truncated"),
            (200, "truncated"),
            (200, "hold_retry"),   # 被扣住、没发给下游的那一次
            (200, "ok"),
        ]
        # 扣住重发不算换候选：客户端看到的一次尝试仍然是第 1 次
        assert rows[2]["attempt"] == 1 and rows[3]["attempt"] == 1
        assert rows[2]["upstream"] == rows[3]["upstream"] == "siteA"


def test_without_rule_the_empty_200_is_not_held(gateway):
    """没配规则的站行为不变：空响应原样转发，一次都不重发（默认关）。"""
    with MockUpstream("siteA") as a:
        g = add_upstream(gateway, a, "siteA", "openai")
        add_route(gateway, "gpt-6-astra", g)
        a.stream_script(*(["empty"] * 6))

        for _ in range(3):
            assert "[DONE]" not in gateway.post("/v1/responses", json=BODY).text

        rows = wait_rows(gateway, 3)
        assert [r["note"] for r in rows] == ["truncated"] * 3
        assert _armed(gateway) == [], "没配守卫的站不该往这张表里记东西"


def test_hold_retry_gives_up_after_times(gateway):
    """times 用完仍拿到空响应：把最后一次原样交给下游，不无限重发。"""
    with MockUpstream("siteA") as a:
        g = add_upstream(gateway, a, "siteA", "openai")
        _set_hold(gateway, provider_id(gateway, "siteA"), '{"after":2,"times":2}')
        add_route(gateway, "gpt-6-astra", g)
        a.stream_script(*(["empty"] * 8))

        for _ in range(2):
            gateway.post("/v1/responses", json=BODY)
        wait_rows(gateway, 2)

        third = gateway.post("/v1/responses", json=BODY)
        assert third.status_code == 200
        assert "[DONE]" not in third.text, "重发次数用完还是空的，就把最后一次交下去"

        rows = wait_rows(gateway, 5)
        assert [r["note"] for r in rows] == [
            "truncated", "truncated",
            "hold_retry", "hold_retry", "truncated",
        ]


def test_guard_arms_across_retries_though_the_body_changes(gateway):
    """客户端重试时请求体并不逐字节相同 —— 拦截不能要求「同一份请求」。

    2026-10-01 实测：codex 在 anyrouter 上连着 6 次重试，请求体字节数完全相同
    （1005868B）、内容却每次都不一样；按整份请求体的摘要认「同一条」的话，8 次空响应
    会留下 8 个指纹、每个只计 1 次，守卫永远不武装（真发生了一次）。
    """
    with MockUpstream("siteA") as a:
        g = add_upstream(gateway, a, "siteA", "openai")
        _set_hold(gateway, provider_id(gateway, "siteA"), '{"after":2,"times":3}')
        add_route(gateway, "gpt-6-astra", g)
        a.stream_script("empty", "empty", "empty", "good")

        # 三次请求的请求体各不相同，长度也不一样
        bodies = [{**BODY, "input": f"第 {i} 次，内容各不相同"} for i in range(4)]

        for body in bodies[:2]:
            assert "[DONE]" not in gateway.post("/v1/responses", json=body).text
        rows = wait_rows(gateway, 2)
        assert [r["note"] for r in rows] == ["truncated", "truncated"]
        armed = _armed(gateway)
        assert len(armed) == 1 and armed[0]["fails"] == 2, "两次空响应就该武装，与请求体无关"

        # 第三份请求体又不一样，照样被扣住、重发，并被救回来
        third = gateway.post("/v1/responses", json=bodies[2])
        assert third.status_code == 200 and "[DONE]" in third.text

        rows = wait_rows(gateway, 4)
        assert [r["note"] for r in rows] == [
            "truncated", "truncated", "hold_retry", "ok",
        ]


def test_guard_is_per_site_model_and_endpoint(gateway):
    """键是「站 + 模型 + 端点」：换个模型、换个端点、换个站都各算各的。"""
    with MockUpstream("siteA") as a:
        g = add_upstream(gateway, a, "siteA", "openai")
        _set_hold(gateway, provider_id(gateway, "siteA"), '{"after":2,"times":3}')
        add_route(gateway, "gpt-6-astra", g)
        add_route(gateway, "gpt-6-other", g)
        a.stream_script(*(["empty"] * 12))

        for _ in range(2):
            gateway.post("/v1/responses", json=BODY)
        wait_rows(gateway, 2)

        armed = {(r["model"], r["endpoint"]) for r in _armed(gateway)}
        assert armed == {("gpt-6-astra", "/responses")}, armed

        # 另一个模型不共享这份计数
        other = {**BODY, "model": "gpt-6-other"}
        assert "[DONE]" not in gateway.post("/v1/responses", json=other).text
        rows = wait_rows(gateway, 3)
        assert [r["note"] for r in rows] == ["truncated"] * 3, "换模型就是另一条记录，不该被扣"

        armed = {(r["model"], r["endpoint"]): r["fails"] for r in _armed(gateway)}
        assert armed == {("gpt-6-astra", "/responses"): 2, ("gpt-6-other", "/responses"): 1}


def test_a_successful_search_does_not_disarm_the_main_turn(gateway):
    """同一个模型的另一种端点成功，不能把主对话攒下的拦截悄悄解除。

    Codex 会在主对话之间穿插 standalone search；两者打的是同一个站和模型，
    但「搜索能通」不等于「主对话能拿到东西」。
    """
    with MockUpstream("siteA") as a:
        g = add_upstream(gateway, a, "siteA", "openai")
        _set_hold(gateway, provider_id(gateway, "siteA"), '{"after":2,"times":3}')
        add_route(gateway, "gpt-6-astra", g)
        a.stream_script("empty", "empty", "good")

        for _ in range(2):
            gateway.post("/v1/responses", json=BODY)
        wait_rows(gateway, 2)
        assert _armed(gateway)[0]["endpoint"] == "/responses"

        # 搜索成功：走的是另一个端点，主对话那份记录必须还在
        gateway.post("/v1/alpha/search", json=BODY)
        wait_rows(gateway, 3)
        armed = _armed(gateway)
        assert len(armed) == 1 and armed[0]["endpoint"] == "/responses", armed


def test_armed_guard_delivers_good_stream(gateway):
    """武装之后站恢复了：扣住只是为了判断，正常流式立刻放行，一个字节都不改。"""
    with MockUpstream("siteA") as a:
        g = add_upstream(gateway, a, "siteA", "openai")
        _set_hold(gateway, provider_id(gateway, "siteA"), '{"after":2,"times":3}')
        add_route(gateway, "gpt-6-astra", g)
        a.stream_script("empty", "empty", "good")

        for _ in range(2):
            gateway.post("/v1/responses", json=BODY)
        wait_rows(gateway, 2)

        third = gateway.post("/v1/responses", json=BODY)
        assert third.status_code == 200, third.text
        assert "[DONE]" in third.text
        # 扣住期间读掉的字节被原样重放：6 条事件一条不少、也没有重复
        assert third.text.count('"i":') == 6, third.text

        rows = wait_rows(gateway, 3)
        assert [r["note"] for r in rows] == ["truncated", "truncated", "ok"]
        assert _armed(gateway) == [], "拿到正常流之后就不再扣了"


def test_armed_guard_releases_at_first_content(gateway):
    """一见到正文就放行，不再重发：半截回答不能被悄悄换成另一份。

    上游这个剧本是「有正文、但没等来完成事件就断」。如果实现是「扣到流结束再判定」，
    它会被当成坏响应重发；正确的行为是第一个正文增量一到就交给下游。
    """
    with MockUpstream("siteA") as a:
        g = add_upstream(gateway, a, "siteA", "openai")
        _set_hold(gateway, provider_id(gateway, "siteA"), '{"after":2,"times":3}')
        add_route(gateway, "gpt-6-astra", g)
        a.stream_script("empty", "empty", "delta_cut", "good")

        for _ in range(2):
            gateway.post("/v1/responses", json=BODY)
        wait_rows(gateway, 2)

        third = gateway.post("/v1/responses", json=BODY)
        assert third.status_code == 200
        assert "chunk0" in third.text, "扣住的那几个字节要原样放出去"
        assert "[DONE]" not in third.text, "上游本来就断在这儿，网关不替它补完成事件"

        rows = wait_rows(gateway, 3)
        assert [r["note"] for r in rows] == ["truncated"] * 3
        assert all(r["note"] != "hold_retry" for r in rows), "见到正文之后不该再重发"
        assert rows[2]["resp_text_bytes"] > 0
        assert _armed(gateway) == [], "出了正文就说明这份请求不再「什么都拿不到」"


@pytest.mark.network
def test_guard_holds_and_resent_over_a_real_socket(gateway):
    """真实 socket 上再走一遍「扣住 → 重发」。

    进程内 ASGITransport 会把响应整包缓冲，分块的字节流和它不是一个东西；扣住期间读掉的
    那几块要靠重放接回去，只有真实的 chunked 传输才能验证接得上。
    """
    with MockUpstream("siteA") as a:
        g = add_upstream(gateway, a, "siteA", "openai")
        _set_hold(gateway, provider_id(gateway, "siteA"), '{"after":2,"times":3}')
        add_route(gateway, "gpt-6-astra", g)
        a.stream_script("empty", "empty", "empty", "good")

        for _ in range(2):
            assert "[DONE]" not in gateway.post("/v1/responses", json=BODY).text

        with gateway.stream("POST", "/v1/responses", json=BODY) as stream:
            raw = "".join(stream.iter_text())
        assert "[DONE]" in raw, "扣住重发之后要把正常流交给下游"
        assert raw.count('"i":') == 6, f"重放不能丢块也不能重复：{raw!r}"

        rows = wait_rows(gateway, 4)
        assert [r["note"] for r in rows] == ["truncated", "truncated", "hold_retry", "ok"]


def test_parse_rules_never_raises_on_dirty_config():
    """配置脏了当作没配 —— 转发路径上不能因为一个字段把请求打炸。"""
    assert truncation.parse_rules("") is None
    assert truncation.parse_rules("   ") is None
    assert truncation.parse_rules("{not json}") is None
    assert truncation.parse_rules("[1,2]") is None
    assert truncation.parse_rules('{"after":"x"}') is None
    assert truncation.parse_rules("null") is None

    assert truncation.parse_rules("{}") == {
        "after": truncation.DEFAULT_AFTER,
        "times": truncation.DEFAULT_TIMES,
        "delay_ms": 0,
        "same_body": False,
    }
    # 越界只夹紧，不拒绝：真站上宁可参数被夹到合理区间，也不要整个守卫失效
    assert truncation.parse_rules('{"after":0,"times":99,"delay_ms":-5}') == {
        "after": 1, "times": truncation.MAX_TIMES, "delay_ms": 0, "same_body": False,
    }
    # same_body 只认真布尔：JSON 里的 "true" 字符串不算
    assert truncation.parse_rules('{"same_body":true}')["same_body"] is True
    assert truncation.parse_rules('{"same_body":"true"}')["same_body"] is False


def test_guard_arms_after_consecutive_empty_responses():
    """连着几次坏才武装；成功一次或换一个维度都另算。"""
    truncation.reset()
    rules = truncation.parse_rules('{"after":2}')
    try:
        assert not truncation.armed(7, "m", "/responses", rules)
        assert truncation.note_bad(7, "m", "/responses", "aaa") == 1
        assert not truncation.armed(7, "m", "/responses", rules), "一次不武装（可能只是抖动）"
        assert truncation.note_bad(7, "m", "/responses", "aaa") == 2
        assert truncation.armed(7, "m", "/responses", rules)

        # 三个维度各算各的：换站、换模型、换端点都不共享
        assert not truncation.armed(8, "m", "/responses", rules)
        assert not truncation.armed(7, "other", "/responses", rules)
        assert not truncation.armed(7, "m", "/alpha/search", rules)

        # 成功一次就忘掉，重新从 0 数
        truncation.note_ok(7, "m", "/responses")
        assert not truncation.armed(7, "m", "/responses", rules)
        assert truncation.note_bad(7, "m", "/responses", "aaa") == 1
    finally:
        truncation.reset()


def test_same_body_rule_needs_byte_identical_responses():
    """same_body=true 时，连着几次的响应字节必须一样才武装（严格版，默认关）。

    这两个公益站**永远**不满足它：错误信封长度固定、内容每次都变。这里只验证规则本身。
    """
    truncation.reset()
    strict = truncation.parse_rules('{"after":2,"same_body":true}')
    loose = truncation.parse_rules('{"after":2}')
    try:
        truncation.note_bad(1, "m", "/responses", "aaa")
        truncation.note_bad(1, "m", "/responses", "bbb")
        assert truncation.fails(1, "m", "/responses") == 2, "默认口径照样算「连着坏」"
        assert truncation.armed(1, "m", "/responses", loose), "默认口径只看「连着几次都是空响应」"
        assert not truncation.armed(1, "m", "/responses", strict), "响应字节不同，严格口径不武装"

        # 三段完全一样时严格口径也武装
        truncation.note_ok(1, "m", "/responses")
        truncation.note_bad(1, "m", "/responses", "ccc")
        truncation.note_bad(1, "m", "/responses", "ccc")
        assert truncation.armed(1, "m", "/responses", strict)
    finally:
        truncation.reset()


def test_guard_table_is_bounded():
    """记住的条目有上限，长期跑不会无限涨。"""
    truncation.reset()
    try:
        for i in range(truncation.MAX_ENTRIES + 20):
            truncation.note_bad(1, f"model{i:04d}", "/responses", "same")
        rows = truncation.snapshot()
        assert len(rows) == truncation.MAX_ENTRIES
        assert {r["model"] for r in rows}.isdisjoint({"model0000", "model0001"}), "最旧的先淘汰"
    finally:
        truncation.reset()


def test_hold_retry_roundtrip_via_admin(gateway):
    """保存 / 读回 hold_retry；形状不对的配置被拒。"""
    with MockUpstream("siteA") as a:
        add_upstream(gateway, a, "siteA")
        up = provider_id(gateway, "siteA")
        _set_hold(gateway, up, '{"after":3,"times":4,"delay_ms":250,"same_body":true}')
        row = next(u for u in gateway.get("/admin/api/upstreams").json() if u["id"] == up)
        assert row["hold_retry"] == '{"after":3,"times":4,"delay_ms":250,"same_body":true}'

        # 缺省项补默认值：存进去的就一定是完整的那个对象
        _set_hold(gateway, up, "{}")
        row = next(u for u in gateway.get("/admin/api/upstreams").json() if u["id"] == up)
        assert row["hold_retry"] == '{"after":2,"times":3,"delay_ms":0}'

        for bad in (
            "{not json}",
            '{"after":2},[]',          # 不是合法 JSON
            "[1,2]",                    # 必须是对象
            '{"after":0}',              # 低于下限
            '{"after":99}',             # 高于上限
            '{"times":0}',
            '{"delay_ms":60000}',
            '{"same_body":"yes"}',      # 只认真布尔
        ):
            resp = gateway.put(
                f"/admin/api/upstreams/{up}",
                json={"name": "siteA", "base_url": a.base_url, "hold_retry": bad},
            )
            assert resp.status_code == 400, f"{bad} 应该被拒：{resp.text}"
