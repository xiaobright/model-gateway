"""端到端测试 · 转发记录与统计：用量落库、按协议拆健康度、按包大小估 token 的那把标尺。"""

from __future__ import annotations

from helpers import wait_for_row, MockUpstream, add_upstream, add_group, provider_id, add_route, msg, wait_rows, wait_inflight


def test_request_log_records_usage_and_stats(gateway):
    with MockUpstream("siteA") as a:
        g_a = add_upstream(gateway, a, "siteA")
        gateway.post("/admin/api/models/bulk-add", json={"group_id": g_a, "model_names": ["gpt-test"]})
        assert gateway.post("/v1/responses", json={"model": "gpt-test"}).status_code == 200

        rows = gateway.get("/admin/api/requests").json()
        assert rows, "至少有一条转发记录"
        row = rows[0]
        assert row["model"] == "gpt-test"
        assert row["upstream"] == "siteA"
        assert row["group_name"] == "默认"
        assert row["status"] == 200
        assert row["input_tokens"] == 120
        assert row["output_tokens"] == 30
        assert row["cached_tokens"] == 80
        assert row["client"] == "python-httpx"

        stats = gateway.get("/admin/api/stats").json()
        assert stats["requests"] >= 1
        assert stats["input_tokens"] >= 120
        assert stats["cached_tokens"] >= 80
        assert 0 < stats["cache_hit_rate"] < 1


def test_stats_normalize_anthropic_cache_and_count_http_errors_as_failures(gateway):
    from gateway import db

    common = dict(
        client="test", model="m", upstream="siteA", stream=False,
        req_bytes=10, resp_bytes=20, duration_ms=1,
    )
    db.insert_request(
        **common, status=200, protocol="anthropic", input_tokens=100,
        output_tokens=10, cached_tokens=900, cache_creation_tokens=50, note="ok",
    )
    db.insert_request(
        **common, status=401, protocol="openai", input_tokens=10,
        output_tokens=0, cached_tokens=0, note="ok",
    )
    db.insert_request(
        **common, status=200, protocol="openai", input_tokens=10,
        output_tokens=0, cached_tokens=0, attempt=2, note="truncated",
    )

    stats = gateway.get("/admin/api/stats").json()
    assert stats["context_tokens"] == 1070, "Anthropic 的 cache_read / cache_creation 都要进输入总量"
    assert stats["cache_hit_rate"] == round(900 / 1070, 4)

    health = next(h for h in gateway.get("/admin/api/stats/upstreams").json() if h["name"] == "siteA")
    assert health["bad"] == 2 and health["ok_rate"] == 0.3333
    assert stats["saved"] == 0, "截断的第二次尝试不能算救回"


def test_request_log_records_protocol_and_health_splits_by_it(gateway):
    """管理页要能回答「这条是哪种格式来的」和「这个站的哪种格式在用」。

    一个站两种接口 = 两个分组，这也是那些「既有 GPT 又有 Claude」的公益站的正常形态。
    """
    with MockUpstream("siteA") as a:
        g_an = add_upstream(gateway, a, "siteA", "anthropic")
        g_oa = add_group(gateway, provider_id(gateway, "siteA"), "openai", name="gpt", api_key="key-siteA")
        add_route(gateway, "opus", g_an, "claude-opus-4-1")
        gateway.post("/admin/api/models/bulk-add", json={"group_id": g_oa, "model_names": ["gpt-test"]})

        assert gateway.post("/v1/messages", json=msg("opus")).status_code == 200
        assert gateway.post("/v1/responses", json={"model": "gpt-test"}).status_code == 200

        rows = gateway.get("/admin/api/requests").json()
        assert {r["model"]: r["protocol"] for r in rows} == {"opus": "anthropic", "gpt-test": "openai"}

        health = next(h for h in gateway.get("/admin/api/stats/upstreams").json() if h["name"] == "siteA")
        assert health["by_protocol"] == {
            "anthropic": {"n": 1, "bad": 0, "ok_rate": 1.0},
            "openai": {"n": 1, "bad": 0, "ok_rate": 1.0},
        }


def test_protocol_split_exposes_a_dead_endpoint(gateway):
    """「这个站的 anthropic 接口通不通」没法静态探测（分组只是声明），只能靠实际跑过的请求。"""
    created = gateway.post(
        "/admin/api/upstreams", json={"name": "dead", "base_url": "http://127.0.0.1:1"}
    ).json()
    add_route(gateway, "opus", add_group(gateway, int(created["id"]), "anthropic"), "claude-opus-4-1")

    assert gateway.post("/v1/messages", json=msg("opus")).status_code == 502
    health = next(h for h in gateway.get("/admin/api/stats/upstreams").json() if h["name"] == "dead")
    assert health["by_protocol"] == {"anthropic": {"n": 1, "bad": 1, "ok_rate": 0.0}}


def test_token_ratio_is_learned_from_the_log(gateway):
    """`≈ N tok` 的标尺是从转发记录里量出来的，不是拍的常数。"""
    from gateway import db, stats as stats_mod

    def logged(protocol: str, req: int, text: int, it: int, ot: int, ct: int,
               thinking: bool = False) -> None:
        db.insert_request(
            client="Claude Code", model="m", upstream="siteA", status=200, stream=True,
            req_bytes=req, resp_bytes=text * 20, resp_text_bytes=text, thinking=thinking,
            duration_ms=100, input_tokens=it, output_tokens=ot, cached_tokens=ct,
            note="ok", protocol=protocol,
        )

    # 25 条整整齐齐的：上行 10 字节一个 token（上下文 = 800 + 200 缓存），
    # 下行 4 字节一个（只数内容字节，不数整条响应 —— 上面故意让 resp_bytes 是它的 20 倍）
    for _ in range(25):
        logged("anthropic", req=10_000, text=2_000, it=800, ot=500, ct=200)
    stats_mod.reset()
    assert stats_mod.token_ratio()["anthropic"] == {"up": 10.0, "down": 4.0}

    # 有思维链的记录不能进下行的标尺：发下来的是总结、计费按完整的算，
    # 这种记录里「收到多少字节」和「被计多少 token」不是一回事
    for _ in range(200):
        logged("openai", req=5_000, text=200, it=1000, ot=5_000, ct=0, thinking=True)
    stats_mod.reset()
    ratio = stats_mod.token_ratio()["openai"]
    assert ratio["up"] == 5.0, "上行照旧量得出来"
    assert ratio["down"] == stats_mod.RATIO_FALLBACK["openai"][1], "全是带思维链的 -> 退回兜底"

    # 忽大忽小时干脆不给估值：那说明字节数里有个跟 token 数无关的大常数项
    for i in range(25):
        logged("anthropic", req=10_000, text=100 if i % 2 else 4_000, it=800, ot=100, ct=200)
    stats_mod.reset()
    assert stats_mod.token_ratio()["anthropic"]["down"] == 0.0, "张幅太大 = 估不出来"

    # 前端拿到的就是这份标尺
    assert gateway.get("/admin/api/inflight").json()["tokens"]["anthropic"]["down"] == 0.0


def test_thinking_is_counted_apart_from_the_text(gateway):
    """思维链要单独认出来：发下来的是总结，计费按完整的算，所以「收到的」明显小于「计费的」。"""
    with MockUpstream("siteA") as a:
        g_a = add_upstream(gateway, a, "siteA", "anthropic")
        add_route(gateway, "opus", g_a, "claude-opus-4-1")

        resp = gateway.post("/v1/messages", json=msg("opus", stream=True, mode="thinking"))
        assert resp.status_code == 200
        call = wait_inflight(gateway, lambda d: d["recent"] and not d["calls"])["recent"][0]
        assert call["thinking"] is True
        # 只数内容字节：整条响应里 SSE 帧占了大头，两者差着量级
        assert 0 < call["text_bytes"] < call["sent"]

        row = wait_for_row(gateway)
        assert (row["thinking"], row["resp_text_bytes"]) == (1, call["text_bytes"])

        # 没有思维链的那条：标一个 0，可以进标尺
        resp = gateway.post("/v1/messages", json=msg("opus", stream=True))
        assert resp.status_code == 200
        clean = wait_rows(gateway, 2)[-1]
        assert clean["thinking"] == 0 and clean["resp_text_bytes"] > 0
