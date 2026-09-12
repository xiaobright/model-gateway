"""端到端测试 · 转发本身：两种接口的透传、请求体/请求头怎么改、流怎么收尾、错误体用谁的形状。"""

from __future__ import annotations

import time
import pytest

from helpers import wait_for_row, MockUpstream, add_upstream, add_group, provider_id, add_route, route_id, msg, parse_sse_events


@pytest.mark.network
def test_import_models_and_switch_without_interrupting_stream(gateway):
    with MockUpstream("siteA") as a, MockUpstream("siteB") as b:
        g_a = add_upstream(gateway, a, "siteA")
        g_b = add_upstream(gateway, b, "siteB")

        pulled = gateway.get(f"/admin/api/groups/{g_a}/remote-models").json()["models"]
        assert set(pulled) == {"gpt-test", "claude-test"}

        added = gateway.post(
            "/admin/api/models/bulk-add", json={"group_id": g_a, "model_names": pulled}
        ).json()
        assert added == {"added": 2, "skipped": []}

        r_b = add_route(gateway, "gpt-test", g_b, "gpt-test")

        exposed = gateway.get("/v1/models").json()
        assert {m["id"] for m in exposed["data"]} == {"gpt-test", "claude-test"}

        with gateway.stream("POST", "/v1/responses", json={"model": "gpt-test", "stream": True}) as stream:
            assert stream.status_code == 200
            chunks: list[str] = []
            for chunk in stream.iter_text():
                if len(chunks) == 0 and chunk.strip():
                    switched = gateway.post(
                        "/admin/api/models/switch", json={"route_id": r_b}
                    )
                    assert switched.json() == {"ok": True}
                chunks.append(chunk)
            raw = "".join(chunks)

        events = parse_sse_events(raw)
        assert [e["i"] for e in events] == list(range(6))
        assert all(e["upstream"] == "siteA" for e in events), "切换后进行中的流必须完整走完旧上游"

        follow_up = gateway.post("/v1/responses", json={"model": "gpt-test"}).json()
        assert follow_up["upstream"] == "siteB", "新请求必须路由到新上游"


def test_upstream_error_is_passed_through(gateway):
    with MockUpstream("siteA") as a:
        g_a = add_upstream(gateway, a, "siteA")
        gateway.post("/admin/api/models/bulk-add", json={"group_id": g_a, "model_names": ["gpt-test"]})

        resp = gateway.post("/v1/responses", json={"model": "gpt-test", "fail": True})
        assert resp.status_code == 429
        assert resp.json()["error"]["message"] == "quota exhausted"


def test_responses_tools_and_context_management_are_transparent(gateway):
    with MockUpstream("siteA") as a:
        g_a = add_upstream(gateway, a, "siteA")
        gateway.post("/admin/api/models/bulk-add", json={"group_id": g_a, "model_names": ["gpt-test"]})

        body = {
            "model": "gpt-test",
            "input": "search this",
            "tools": [{"type": "web_search"}],
            "tool_choice": "auto",
            "include": ["web_search_call.action.sources"],
            "context_management": [{"type": "compaction", "compact_threshold": 200000}],
        }
        seen = gateway.post("/v1/responses", json=body).json()

        assert seen["seen_tools"] == body["tools"]
        assert seen["seen_tool_choice"] == body["tool_choice"]
        assert seen["seen_include"] == body["include"]
        assert seen["seen_context_management"] == body["context_management"]


def test_standalone_responses_compact_is_transparent(gateway):
    with MockUpstream("siteA") as a:
        g_a = add_upstream(gateway, a, "siteA")
        add_route(gateway, "gpt-test", g_a, "remote-gpt")

        body = {
            "model": "gpt-test",
            "input": [{"role": "user", "content": "long task"}],
            "tools": [{"type": "web_search"}],
        }
        resp = gateway.post("/v1/responses/compact", json=body)

        assert resp.status_code == 200, resp.text
        result = resp.json()
        assert result["output"][0]["type"] == "compaction"
        assert result["output"][0]["encrypted_content"] == "opaque-test-state"
        assert result["model"] == "remote-gpt"
        assert result["seen_input"] == body["input"]
        assert result["seen_tools"] == body["tools"]


def test_standalone_web_search_is_transparent(gateway):
    with MockUpstream("siteA") as a:
        g_a = add_upstream(gateway, a, "siteA")
        add_route(gateway, "gpt-test", g_a, "remote-gpt")

        body = {
            "id": "search-1",
            "model": "gpt-test",
            "input": "find current docs",
            "commands": {"search_query": [{"q": "OpenAI Responses API"}]},
            "settings": {"external_web_access": "live"},
        }
        resp = gateway.post("/v1/alpha/search", json=body)

        assert resp.status_code == 200, resp.text
        result = resp.json()
        assert result["output"] == "Search result from siteA"
        assert result["seen_commands"] == body["commands"]
        assert result["seen_settings"] == body["settings"]


def test_standalone_web_search_fails_over_on_unsupported_endpoint(gateway):
    """Search-only fallback works even while normal OpenAI failover is off."""
    with MockUpstream("siteA") as a, MockUpstream("siteB") as b:
        g_a = add_upstream(gateway, a, "siteA")
        g_b = add_upstream(gateway, b, "siteB")
        add_route(gateway, "gpt-test", g_a, "gpt-test")
        add_route(gateway, "gpt-test", g_b, "gpt-test")
        a.sick["search_unsupported"] = True

        body = {
            "model": "gpt-test",
            "input": "find current docs",
        }
        # The first candidate returns 404. The fallback
        # should be decided by endpoint support, not the global failover toggle.
        first = gateway.post("/v1/alpha/search", json=body)
        assert first.status_code == 200, first.text
        assert first.json()["output"] == "Search result from siteB"

        rows = gateway.get("/admin/api/requests").json()
        assert any(r["upstream"] == "siteA" and r["note"] == "search_failed_over" for r in rows)
        assert any(r["upstream"] == "siteB" and r["status"] == 200 for r in rows)


def test_standalone_web_search_uses_configured_search_group(gateway):
    """Search can use one search-only upstream group even when Responses is routed elsewhere."""
    from gateway import db

    with MockUpstream("model-site") as model_site, MockUpstream("search-site") as search_site:
        models = add_upstream(gateway, model_site, "model-site")
        search = add_upstream(gateway, search_site, "search-site")
        add_route(gateway, "gpt-test", models, "remote-model")

        db.set_standalone_search_target_group(search)
        resp = gateway.post("/v1/alpha/search", json={"model": "gpt-test", "input": "fresh facts"})

        assert resp.status_code == 200, resp.text
        assert resp.json()["output"] == "Search result from search-site"
        rows = gateway.get("/admin/api/requests").json()
        assert rows[0]["upstream"] == "search-site"


def test_standalone_search_target_api_validates_and_clears(gateway):
    with MockUpstream("siteA") as site:
        group = add_upstream(gateway, site, "siteA")

        selected = gateway.put(
            "/admin/api/standalone-search-target",
            json={"group_id": group, "model": "gpt-5.6-luna"},
        )
        assert selected.status_code == 200, selected.text
        assert selected.json() == {"group_id": group, "model": "gpt-5.6-luna"}
        assert gateway.get("/admin/api/standalone-search-target").json() == {
            "group_id": group, "model": "gpt-5.6-luna"
        }

        cleared = gateway.put(
            "/admin/api/standalone-search-target", json={"group_id": None, "model": None}
        )
        assert cleared.status_code == 200, cleared.text
        assert cleared.json() == {"group_id": None, "model": None}


def test_hanging_error_body_does_not_block_failover(gateway):
    """拿到 503 头后，错误正文不应挡住备用站。"""
    with MockUpstream("siteA") as a, MockUpstream("siteB") as b:
        g_a = add_upstream(gateway, a, "siteA", "anthropic")
        g_b = add_upstream(gateway, b, "siteB", "anthropic")
        add_route(gateway, "opus", g_a, "opus-a")
        add_route(gateway, "opus", g_b, "opus-b")
        a.fail_with(503)
        a.sick["hang_body"] = True

        began = time.monotonic()
        resp = gateway.post("/v1/messages", json=msg("opus"))
        elapsed = time.monotonic() - began

        assert resp.status_code == 200 and resp.json()["upstream"] == "siteB"
        assert elapsed < 8.0, f"备用站被错误体拖住了 {elapsed:.2f}s"


def test_unknown_model_returns_404(gateway):
    resp = gateway.post("/v1/responses", json={"model": "nope"})
    assert resp.status_code == 404


def test_client_headers_pass_through_and_auth_override(gateway):
    with MockUpstream("siteA") as a, MockUpstream("siteB") as b:
        g_a = add_upstream(gateway, a, "siteA")
        g_b = add_upstream(gateway, b, "nokey", api_key="")
        rids = {gid: add_route(gateway, "hdr-test", gid, "gpt-test") for gid in (g_a, g_b)}

        client_headers = {"User-Agent": "codex_cli_rs/1.0", "X-Probe": "abc", "Authorization": "Bearer client-token"}
        resp = gateway.post("/v1/responses", json={"model": "hdr-test"}, headers=client_headers).json()
        assert resp["ua"] == "codex_cli_rs/1.0", "客户端 UA 必须原样到达上游"
        assert resp["x_probe"] == "abc", "自定义头必须原样到达上游"
        assert resp["auth"] == "Bearer key-siteA", "分组存有 key 时覆盖客户端 Authorization"

        gateway.post("/admin/api/models/switch", json={"route_id": rids[g_b]})
        resp = gateway.post("/v1/responses", json={"model": "hdr-test"}, headers=client_headers).json()
        assert resp["auth"] == "Bearer client-token", "分组 key 为空时必须透传客户端 Authorization"


def test_header_override_applied_per_upstream(gateway):
    with MockUpstream("siteA") as a:
        g_a = add_upstream(gateway, a, "siteA")
        uid = provider_id(gateway, "siteA")
        override = '{"user-agent": "codex_cli_rs", "originator": "codex_cli_rs", "x-drop-me": null}'
        detail = gateway.get("/admin/api/upstreams").json()[0]
        r = gateway.put(
            f"/admin/api/upstreams/{uid}",
            json={
                "name": detail["name"],
                "base_url": detail["base_url"],
                "enabled": True,
                "header_override": override,
            },
        )
        assert r.status_code == 200, r.text
        assert r.json()["header_override"] == override

        gateway.post("/admin/api/models/bulk-add", json={"group_id": g_a, "model_names": ["gpt-test"]})
        resp = gateway.post(
            "/v1/responses",
            json={"model": "gpt-test"},
            headers={"User-Agent": "some-other-agent/2.0", "X-Drop-Me": "bye"},
        ).json()
        assert resp["ua"] == "codex_cli_rs", "覆写必须替换客户端 UA"
        assert resp["originator"] == "codex_cli_rs", "覆写新增的头必须生效"
        assert resp["x_drop"] == "", "值为 null 的头必须被删除"
        assert resp["x_probe"] == "", "未覆写的头仍原样透传（此处为空）"
        assert resp["auth"] == "Bearer key-siteA"


def test_connect_failure_is_logged_as_502(gateway):
    created = gateway.post(
        "/admin/api/upstreams", json={"name": "dead", "base_url": "http://127.0.0.1:1"}
    ).json()
    gid = add_group(gateway, int(created["id"]), "openai")
    gateway.post("/admin/api/models/bulk-add", json={"group_id": gid, "model_names": ["ghost"]})

    assert gateway.post("/v1/responses", json={"model": "ghost"}).status_code == 502
    row = gateway.get("/admin/api/requests").json()[0]
    assert row["status"] == 502 and row["note"] == "connect_failed"


@pytest.mark.network
def test_client_disconnect_mid_stream_is_recorded_and_gateway_survives(gateway):
    """客户端中途断开不能让转发协程炸掉，也不能漏掉这条记录。"""
    with MockUpstream("siteA") as a:
        g_a = add_upstream(gateway, a, "siteA")
        gateway.post("/admin/api/models/bulk-add", json={"group_id": g_a, "model_names": ["gpt-test"]})

        with gateway.stream("POST", "/v1/responses", json={"model": "gpt-test", "stream": True}) as stream:
            assert stream.status_code == 200
            next(stream.iter_bytes())  # 只读第一块就走，此时还没收到完成事件

        row = wait_for_row(gateway)
        assert row["note"] == "client_abort", f"真的中途断开要标出来，实际是 {row['note']}"

        # 断流之后网关仍然正常工作
        assert gateway.post("/v1/responses", json={"model": "gpt-test"}).status_code == 200


@pytest.mark.network
def test_client_leaving_after_completion_event_is_not_flagged(gateway):
    """上游发完完成事件却不收连接、客户端拿到就走 —— 这是正常收尾，不能记成客户端断开。"""
    with MockUpstream("siteA") as a:
        g_a = add_upstream(gateway, a, "siteA")
        gateway.post("/admin/api/models/bulk-add", json={"group_id": g_a, "model_names": ["gpt-test"]})

        with gateway.stream(
            "POST", "/v1/responses", json={"model": "gpt-test", "stream": True, "mode": "lingering"}
        ) as stream:
            assert stream.status_code == 200
            for chunk in stream.iter_bytes():
                if b"[DONE]" in chunk:
                    break  # 跟 codex 一样：看到完成事件就不等 TCP 关闭了

        row = wait_for_row(gateway)
        assert row["note"] == "ok", f"流已经走完了，不该报异常，实际是 {row['note']}"


def test_completion_marker_split_across_chunks_is_detected(gateway):
    """完成标记被切在两个 chunk 之间时也要认出来，否则会误报截断。"""
    with MockUpstream("siteA") as a:
        g_a = add_upstream(gateway, a, "siteA")
        gateway.post("/admin/api/models/bulk-add", json={"group_id": g_a, "model_names": ["gpt-test"]})

        resp = gateway.post("/v1/responses", json={"model": "gpt-test", "stream": True, "mode": "split_marker"})
        assert resp.status_code == 200

        row = wait_for_row(gateway)
        assert row["note"] == "ok", f"标记跨块也必须认出来，实际是 {row['note']}"
        assert row["input_tokens"] == 7


# ================================================================ Anthropic 格式



def test_anthropic_messages_rewrites_model_and_injects_both_auth_headers(gateway):
    """档位名 -> 上游真名的改写，以及 x-api-key 必须被覆盖（客户端会带占位 key）。"""
    with MockUpstream("siteA") as a:
        g_a = add_upstream(gateway, a, "siteA", "anthropic")
        add_route(gateway, "opus", g_a, "claude-opus-4-1")

        resp = gateway.post(
            "/v1/messages", json=msg("opus"), headers={"x-api-key": "placeholder-from-client"}
        )
        assert resp.status_code == 200, resp.text
        seen = resp.json()
        assert seen["model"] == "claude-opus-4-1", "上游必须收到它自己那边的真名"
        assert seen["auth"] == "Bearer key-siteA"
        assert seen["x_api_key"] == "key-siteA", "客户端的占位 key 不能把配好的真 key 压掉"
        assert seen["version"] == "2023-06-01", "客户端没带 anthropic-version 时要补上"

        row = wait_for_row(gateway)
        assert row["model"] == "opus"
        assert row["remote_model"] == "claude-opus-4-1"
        assert (row["input_tokens"], row["output_tokens"], row["cached_tokens"]) == (11, 22, 5)
        assert row["client"] == "python-httpx"


def test_anthropic_stream_usage_survives_message_start_falling_out_of_tail(gateway):
    """Anthropic 把输入 token 放在流开头的 message_start，只留尾巴窗口会丢掉它。"""
    with MockUpstream("siteA") as a:
        g_a = add_upstream(gateway, a, "siteA", "anthropic")
        add_route(gateway, "opus", g_a, "claude-opus-4-1")

        resp = gateway.post("/v1/messages", json=msg("opus", stream=True, mode="bulk"))
        assert resp.status_code == 200
        assert len(resp.content) > 65536, "这个用例的前提是流长过 TAIL_KEEP"
        assert b"message_start" not in resp.content[-65536:]

        row = wait_for_row(gateway)
        assert row["note"] == "ok", f"message_stop 就是结束事件，不该报截断，实际 {row['note']}"
        assert row["input_tokens"] == 1234, "输入 token 只在流开头出现过一次"
        assert row["output_tokens"] == 777, "输出 token 取末尾 message_delta 里的终值"
        assert row["cached_tokens"] == 900, "cache_read_input_tokens 要算进缓存命中"


def test_anthropic_stream_without_message_stop_is_flagged_truncated(gateway):
    with MockUpstream("siteA") as a:
        g_a = add_upstream(gateway, a, "siteA", "anthropic")
        add_route(gateway, "opus", g_a, "claude-opus-4-1")

        assert gateway.post("/v1/messages", json=msg("opus", stream=True, mode="no_end")).status_code == 200
        row = wait_for_row(gateway)
        assert row["note"] == "truncated"


def test_one_million_suffix_is_stripped_and_beta_header_injected(gateway):
    """[1m] 是 Claude Code 自己的档位约定，上游不认；它只该变成一个 beta 头。"""
    with MockUpstream("siteA") as a:
        g_a = add_upstream(gateway, a, "siteA", "anthropic")
        add_route(gateway, "claude-opus-5", g_a, "claude-opus-4-1")
        # 后缀也可能被写在配置的 remote 名里
        add_route(gateway, "sonnet", g_a, "claude-sonnet-4-5[1m]")

        seen = gateway.post("/v1/messages", json=msg("claude-opus-5[1m]")).json()
        assert seen["model"] == "claude-opus-4-1", "方括号后缀绝不能传给上游"
        assert "context-1m-2025-08-07" in seen["beta"]

        seen = gateway.post("/v1/messages", json=msg("sonnet")).json()
        assert seen["model"] == "claude-sonnet-4-5", "remote 名里的后缀同样要摘掉"
        assert "context-1m-2025-08-07" in seen["beta"]

        # 客户端自己带了别的 beta 时要追加而不是覆盖
        seen = gateway.post(
            "/v1/messages", json=msg("claude-opus-5[1m]"), headers={"anthropic-beta": "oauth-2025-04-20"}
        ).json()
        assert "oauth-2025-04-20" in seen["beta"] and "context-1m-2025-08-07" in seen["beta"]

        # 没有 1M 标记时不该凭空加头
        seen = gateway.post("/v1/messages", json=msg("claude-opus-5")).json()
        assert "context-1m" not in seen["beta"]


def test_tier_keyword_fallback_catches_unconfigured_model_ids(gateway):
    """只配了档位名时，Claude Code 发来的具体 id 也要能落到同档位那条配置上。"""
    with MockUpstream("siteA") as a:
        g_a = add_upstream(gateway, a, "siteA", "anthropic")
        add_route(gateway, "opus", g_a, "real-opus")

        seen = gateway.post("/v1/messages", json=msg("claude-opus-4-1-20250805")).json()
        assert seen["model"] == "real-opus"
        row = wait_for_row(gateway)
        assert row["model"] == "claude-opus-4-1-20250805", "记录里留客户端问的那个名字"
        assert row["remote_model"] == "real-opus"

        # 认不出档位的名字仍然是 404，不能瞎猜
        assert gateway.post("/v1/messages", json=msg("gpt-5-turbo")).status_code == 404


def test_gateway_errors_use_the_shape_of_the_endpoint(gateway):
    """网关自己产生的错误也要按下游期望的形状返回，否则客户端解析不出来。"""
    an = gateway.post("/v1/messages", json=msg("nope"))
    assert an.status_code == 404
    assert an.json() == {
        "type": "error",
        "error": {"type": "not_found_error", "message": "模型 'nope' 未配置或当前上游已停用"},
    }

    oa = gateway.post("/v1/responses", json={"model": "nope"})
    assert oa.status_code == 404
    assert oa.json()["error"]["type"] == "gateway_error"

    bad = gateway.post("/v1/messages", content=b"{not json", headers={"content-type": "application/json"})
    assert bad.status_code == 400
    assert bad.json()["error"]["type"] == "invalid_request_error"


def test_upstream_error_body_is_passed_through_untouched(gateway):
    with MockUpstream("siteA") as a:
        g_a = add_upstream(gateway, a, "siteA", "anthropic")
        add_route(gateway, "opus", g_a, "claude-opus-4-1")

        resp = gateway.post("/v1/messages", json=msg("opus", fail=True))
        assert resp.status_code == 429
        assert resp.json()["error"]["type"] == "rate_limit_error"


def test_body_is_byte_exact_when_no_rename_configured(gateway):
    """名字两边一致时继续发原始字节，「透明中转」这个特性不能因为改写机制丢掉。"""
    with MockUpstream("siteA") as a:
        g_a = add_upstream(gateway, a, "siteA", "anthropic")
        add_route(gateway, "same", g_a, "same")
        add_route(gateway, "renamed", g_a, "other")

        raw = b'{"model": "same",   "note" : "  \xe7\x95\x99\xe7\x9d\x80  "}'
        seen = gateway.post(
            "/v1/messages", content=raw, headers={"content-type": "application/json"}
        ).json()
        assert seen["raw"] == raw.decode(), "没改名就一个字节都不该动"

        seen = gateway.post(
            "/v1/messages",
            content=b'{"model": "renamed", "note": "\xe4\xb8\xad\xe6\x96\x87"}',
            headers={"content-type": "application/json"},
        ).json()
        assert seen["model"] == "other"
        assert "中文" in seen["raw"], "重新序列化必须 ensure_ascii=False，否则中文体积暴涨"


def test_count_tokens_is_forwarded_but_kept_out_of_the_stats(gateway):
    """Claude Code 会频繁调它，记进转发记录会把累计次数和模型热度冲得没法看。"""
    with MockUpstream("siteA") as a:
        g_a = add_upstream(gateway, a, "siteA", "anthropic")
        add_route(gateway, "opus", g_a, "claude-opus-4-1")

        resp = gateway.post("/v1/messages/count_tokens", json=msg("opus"))
        assert resp.status_code == 200, resp.text
        assert resp.json() == {"input_tokens": 42, "model": "claude-opus-4-1", "upstream": "siteA"}

        assert gateway.get("/admin/api/requests").json() == []
        assert gateway.get("/admin/api/stats").json()["live"] == {"requests": 0, "streams": 0}


def test_models_list_satisfies_both_openai_and_anthropic_shapes(gateway):
    with MockUpstream("siteA") as a:
        g_a = add_upstream(gateway, a, "siteA", "anthropic")
        add_route(gateway, "opus", g_a, "claude-opus-4-1")

        listing = gateway.get("/v1/models").json()
        assert listing["object"] == "list" and listing["has_more"] is False
        assert listing["first_id"] == "opus" and listing["last_id"] == "opus"
        item = listing["data"][0]
        # OpenAI 侧要的字段
        assert (item["id"], item["object"], item["owned_by"]) == ("opus", "model", "model-gateway")
        # Anthropic 侧要的字段
        assert (item["type"], item["display_name"]) == ("model", "opus")
        assert item["created_at"]


def test_anthropic_switch_and_disable_reuse_the_same_routing(gateway):
    """热切换、停用兜底这些是路由层的能力，两种协议共用一套。"""
    with MockUpstream("siteA") as a, MockUpstream("siteB") as b:
        g_a = add_upstream(gateway, a, "siteA", "anthropic")
        g_b = add_upstream(gateway, b, "siteB", "anthropic")
        add_route(gateway, "opus", g_a, "on-a")
        r_b = add_route(gateway, "opus", g_b, "on-b")

        assert gateway.post("/v1/messages", json=msg("opus")).json()["upstream"] == "siteA"
        gateway.post("/admin/api/models/switch", json={"route_id": r_b})
        seen = gateway.post("/v1/messages", json=msg("opus")).json()
        assert (seen["upstream"], seen["model"]) == ("siteB", "on-b")
