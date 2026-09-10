"""端到端测试 · 「实时」那一页的数据来源：阶段、降级轨迹、上游自己报的 token 数。"""

from __future__ import annotations

import pytest

from helpers import MockUpstream, add_upstream, add_route, two_anthropic_sites, msg, wait_inflight


# ================================================================ 实时请求
#
# /admin/api/inflight 是「实时」那一页的全部数据来源：谁在跑、打的是哪个上游、
# 什么阶段、前面被谁拒过。纯内存，不碰数据库。



@pytest.mark.network
def test_inflight_lists_the_running_request(gateway):
    """这页要能回答：现在在打哪个上游、什么阶段、等了多久、下游是谁。"""
    with MockUpstream("siteA") as a:
        g_a = add_upstream(gateway, a, "siteA")
        add_route(gateway, "gpt-test", g_a, "gpt-remote")

        seen = None
        with gateway.stream(
            "POST", "/v1/responses", json={"model": "gpt-test", "stream": True},
            headers={"User-Agent": "codex_cli_rs/1.0"},
        ) as stream:
            for chunk in stream.iter_text():
                if chunk.strip() and seen is None:
                    seen = gateway.get("/admin/api/inflight").json()

        assert seen is not None and len(seen["calls"]) == 1, seen
        call = seen["calls"][0]
        assert (call["model"], call["remote_model"]) == ("gpt-test", "gpt-remote")
        assert (call["upstream"], call["group_name"]) == ("siteA", "默认")
        assert call["phase"] == "stream", "第一块字节已经在往下游走了"
        assert call["client"] == "Codex CLI" and call["stream"] is True
        assert call["meta"] is False and call["trail"] == []
        assert seen["counts"] == {"requests": 1, "streams": 1}
        assert seen["failover"] == {"anthropic": True, "openai": False, "openai-chat": False}

        # 流走完就转进「刚结束」，再留 90 秒 —— 不然降级轨迹只在活着的那几秒里存在
        after = wait_inflight(gateway, lambda d: not d["calls"])
        assert [c["model"] for c in after["recent"]] == ["gpt-test"]
        assert after["recent"][0]["note"] == "ok"
        assert after["counts"] == {"requests": 0, "streams": 0}


@pytest.mark.network
def test_manual_cancel_closes_a_real_stream_and_gateway_stays_usable(gateway):
    """真实 HTTP 连接也要及时收尾，不能只在登记表里把那条请求藏起来。"""
    with MockUpstream("siteA") as upstream:
        group = add_upstream(gateway, upstream, "siteA")
        add_route(gateway, "gpt-test", group)
        with gateway.stream("POST", "/v1/responses", json={"model": "gpt-test", "stream": True, "mode": "stalled"}) as stream:
            chunks = stream.iter_bytes()
            assert next(chunks)
            target = gateway.get("/admin/api/inflight").json()["calls"][0]
            response = gateway.post(f'/admin/api/inflight/{target["id"]}/cancel')
            assert response.json() == {"ok": True, "cancelled": True}
            rest = b"".join(chunks)
            assert b"[DONE]" not in rest

        done = wait_inflight(gateway, lambda data: not data["calls"])["recent"][0]
        assert done["note"] == "manual_abort"
        assert gateway.get("/admin/api/requests").json()[0]["note"] == "manual_abort"
        assert gateway.post("/v1/responses", json={"model": "gpt-test"}).status_code == 200


def test_inflight_keeps_the_failover_trail(gateway):
    """降级最怕的是把问题藏起来：胜出的那条要带着「前面被谁拒了」。"""
    with MockUpstream("siteA") as a, MockUpstream("siteB") as b:
        g_a, _ = two_anthropic_sites(gateway, a, b)
        a.fail_with(503)

        assert gateway.post("/v1/messages", json=msg("opus")).status_code == 200
        data = wait_inflight(gateway, lambda d: d["recent"] and not d["calls"])
        call = data["recent"][0]
        assert (call["upstream"], call["attempt"], call["status"]) == ("siteB", 2, 200)
        assert [(t["upstream"], t["status"], t["note"]) for t in call["trail"]] == [
            ("siteA", 503, "failed_over")
        ]
        # 分组健康板的数据也是这个接口给的
        assert [(b["group_id"], b["fails"]) for b in data["breakers"]] == [(g_a, 1)]


def test_count_tokens_is_listed_but_not_counted(gateway):
    """count_tokens 会走降级、会踩断路器，所以列出来；但它又多又快，
    计进「进行中」就没法当忙闲指示看了。"""
    with MockUpstream("siteA") as a:
        g_a = add_upstream(gateway, a, "siteA", "anthropic")
        add_route(gateway, "opus", g_a, "claude-opus-4-1")

        assert gateway.post("/v1/messages/count_tokens", json=msg("opus")).status_code == 200
        data = wait_inflight(gateway, lambda d: bool(d["recent"]))
        assert [c["meta"] for c in data["recent"]] == [True]
        assert data["counts"]["requests"] == 0
        assert gateway.get("/admin/api/requests").json() == [], "照旧不进转发记录"


def test_inflight_registers_nothing_for_an_unconfigured_model(gateway):
    """连上游都没碰过的 404 不该出现在这页上 —— 它没在打任何站。"""
    assert gateway.post("/v1/responses", json={"model": "nope"}).status_code == 404
    data = gateway.get("/admin/api/inflight").json()
    assert (data["calls"], data["recent"]) == ([], [])


# ================================================================ 按包大小估 token



@pytest.mark.network
def test_inflight_uses_the_token_counts_upstream_reported(gateway):
    """Anthropic 把输入 token 放在流开头的 message_start 里，所以第一块字节到手时
    就已经有真数了，不用再按包大小估。输出 token 要等末尾的 message_delta。"""
    with MockUpstream("siteA") as a:
        g_a = add_upstream(gateway, a, "siteA", "anthropic")
        add_route(gateway, "opus", g_a, "claude-opus-4-1")

        seen = None
        with gateway.stream(
            "POST", "/v1/messages", json=msg("opus", stream=True, mode="slow")
        ) as stream:
            for chunk in stream.iter_text():
                if chunk.strip() and seen is None:
                    seen = gateway.get("/admin/api/inflight").json()

        assert seen is not None and len(seen["calls"]) == 1, seen
        # 1234 是 input_tokens，900 是 cache_read —— Anthropic 的 input 不含缓存，得加起来
        assert seen["calls"][0]["tokens_in"] == 2134, "流还在跑就该有上游报的上下文大小"
        assert seen["calls"][0]["tokens_out"] == 0, "输出 token 这会儿还没报"

        done = wait_inflight(gateway, lambda d: d["recent"] and not d["calls"])["recent"][0]
        assert (done["tokens_in"], done["tokens_out"]) == (2134, 777)
