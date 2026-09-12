"""端到端测试 · 管理接口：供应商 / 分组的增删改、拉取模型列表、跨站防护，以及三级迁移。"""

from __future__ import annotations

import pytest

from helpers import wait_for_row, MockUpstream, add_upstream, add_group, provider_id, add_route, route_id, msg
from gateway import db as gateway_db


def test_duplicate_upstream_name_is_409_not_500(gateway):
    with MockUpstream("siteA") as a:
        add_upstream(gateway, a, "siteA")
        dup = gateway.post("/admin/api/upstreams", json={"name": "siteA", "base_url": a.base_url + "/x"})
        assert dup.status_code == 409
        assert "同名" in dup.json()["detail"]


def test_duplicate_base_url_is_rejected_with_a_hint_about_groups(gateway):
    """站D / DDD2 那种「同一个站建成两个供应商」正是分组要解决的问题，别让它再发生。"""
    with MockUpstream("siteA") as a:
        add_upstream(gateway, a, "siteA")
        dup = gateway.post("/admin/api/upstreams", json={"name": "siteA-2", "base_url": a.base_url})
        assert dup.status_code == 409
        assert "分组" in dup.json()["detail"] and "siteA" in dup.json()["detail"]


def test_bulk_add_to_missing_group_is_404(gateway):
    resp = gateway.post("/admin/api/models/bulk-add", json={"group_id": 9999, "model_names": ["x"]})
    assert resp.status_code == 404


def test_remote_model_pull_and_manual_add_default_remote_name(gateway):
    with MockUpstream("siteA") as a:
        g_a = add_upstream(gateway, a, "siteA")
        # remote_model 留空时应回落成 model_name
        r = gateway.post("/admin/api/models", json={"model_name": "gpt-test", "group_id": g_a})
        assert r.status_code == 200
        assert r.json()["remote_model"] == "gpt-test"
        assert gateway.post(
            "/admin/api/models", json={"model_name": "gpt-test", "group_id": g_a}
        ).status_code == 409


def test_upstream_catalog_is_separate_from_downstream_exposure(gateway):
    """登记上游模型只进目录，不自动暴露下游；删下游候选也不该把上游记录带走。"""
    with MockUpstream("siteA") as a:
        g = add_upstream(gateway, a, "siteA")

        added = gateway.post(
            f"/admin/api/groups/{g}/models", json={"model_names": ["deepseek", "deepseek-v3"]}
        )
        assert added.status_code == 200, added.text
        assert added.json()["added"] == 2

        def group_models() -> list[str]:
            return gateway.get("/admin/api/upstreams").json()[0]["groups"][0]["models"]

        assert group_models() == ["deepseek", "deepseek-v3"]
        assert gateway.get("/admin/api/models").json() == [], "登记上游模型不产生下游候选"

        rid = add_route(gateway, "luna", g, "deepseek")
        assert group_models() == ["deepseek", "deepseek-v3"], "上游列显示真名，不显示下游名 luna"

        assert gateway.delete(f"/admin/api/models?route_id={rid}").status_code == 200
        assert group_models() == ["deepseek", "deepseek-v3"], "删下游候选不能删掉上游模型"

        add_route(gateway, "luna", g, "deepseek")
        dropped = gateway.delete(f"/admin/api/groups/{g}/models?remote_model=deepseek")
        assert dropped.status_code == 200
        assert dropped.json()["routes_removed"] == 1
        assert group_models() == ["deepseek-v3"]
        assert gateway.get("/admin/api/models").json() == [], "上游模型下掉后，指向它的候选一起下线"


def test_catalog_delete_of_unknown_model_is_404(gateway):
    with MockUpstream("siteA") as a:
        g = add_upstream(gateway, a, "siteA")
        assert gateway.delete(f"/admin/api/groups/{g}/models?remote_model=nope").status_code == 404


def test_request_log_can_be_cleared(gateway):
    with MockUpstream("siteA") as a:
        g_a = add_upstream(gateway, a, "siteA")
        gateway.post("/admin/api/models/bulk-add", json={"group_id": g_a, "model_names": ["gpt-test"]})
        gateway.post("/v1/responses", json={"model": "gpt-test"})
        assert gateway.get("/admin/api/requests").json()

        assert gateway.delete("/admin/api/requests").json()["ok"] is True
        assert gateway.get("/admin/api/requests").json() == []
        assert gateway.get("/admin/api/stats").json()["requests"] == 0


def test_admin_api_rejects_cross_site_and_foreign_host(gateway):
    """管理接口没鉴权，只能靠拒绝跨站请求兜底，否则任何网页都能改配置或关掉进程。"""
    assert gateway.get("/admin/api/upstreams").status_code == 200

    blocked = gateway.post(
        "/admin/api/shutdown", headers={"Origin": "https://evil.example", "Sec-Fetch-Site": "cross-site"}
    )
    assert blocked.status_code == 403

    rebound = gateway.get("/admin/api/upstreams", headers={"Host": "evil.example"})
    assert rebound.status_code == 403

    # 同源的浏览器请求必须放行
    same_origin = gateway.get(
        "/admin/api/upstreams",
        headers={"Origin": str(gateway.base_url), "Sec-Fetch-Site": "same-origin"},
    )
    assert same_origin.status_code == 200


def test_static_assets_are_not_cached(gateway):
    """ES 模块的 import 是裸路径挂不了版本号，只能靠 no-store 保证改完刷新就生效。"""
    for path in ("/", "/static/app.js", "/static/views.js", "/static/style.css"):
        resp = gateway.get(path)
        assert resp.status_code == 200, path
        assert resp.headers.get("cache-control") == "no-store", path


# ================================================================ 供应商 / 分组



def test_groups_keep_their_own_key_and_model_list(gateway):
    """同一个站两把 key：拉到的模型不一样，转发时也各用各的 key。"""
    with MockUpstream("siteA") as a:
        g_default = add_upstream(gateway, a, "siteA")
        uid = provider_id(gateway, "siteA")
        g_vip = add_group(gateway, uid, "openai", name="vip", api_key="key-siteA-vip")

        plain = gateway.get(f"/admin/api/groups/{g_default}/remote-models").json()["models"]
        vip = gateway.get(f"/admin/api/groups/{g_vip}/remote-models").json()["models"]
        assert set(plain) == {"gpt-test", "claude-test"}
        assert set(vip) == {"gpt-test", "vip-only"}, "分组的模型列表要用它自己的 key 去拉"

        add_route(gateway, "only-vip", g_vip, "gpt-test")
        seen = gateway.post("/v1/responses", json={"model": "only-vip"}).json()
        assert seen["auth"] == "Bearer key-siteA-vip", "转发时必须用命中分组的 key"

        row = wait_for_row(gateway)
        assert (row["upstream"], row["group_name"]) == ("siteA", "vip")


def test_switching_between_two_groups_of_one_provider(gateway):
    """两个分组是平级候选，切换和跨供应商切换没有区别。"""
    with MockUpstream("siteA") as a:
        g_default = add_upstream(gateway, a, "siteA")
        uid = provider_id(gateway, "siteA")
        g_vip = add_group(gateway, uid, "openai", name="vip", api_key="key-siteA-vip")
        add_route(gateway, "shared", g_default, "gpt-test")
        r_vip = add_route(gateway, "shared", g_vip, "gpt-test")

        assert gateway.post("/v1/responses", json={"model": "shared"}).json()["auth"] == "Bearer key-siteA"
        gateway.post("/admin/api/models/switch", json={"route_id": r_vip})
        assert gateway.post("/v1/responses", json={"model": "shared"}).json()["auth"] == "Bearer key-siteA-vip"

        group = next(g for g in gateway.get("/admin/api/models").json() if g["model_name"] == "shared")
        assert group["active_route_id"] == r_vip
        assert {c["group_name"] for c in group["candidates"]} == {"默认", "vip"}


def test_moving_a_group_merges_two_providers(gateway):
    """一开始把同一个站建成了两个供应商，事后把分组搬过去就能合并，候选跟着走。"""
    with MockUpstream("siteA") as a, MockUpstream("siteB") as b:
        add_upstream(gateway, a, "keep")
        g_stray = add_upstream(gateway, b, "stray")
        add_route(gateway, "gpt-test", g_stray)

        keep_id = provider_id(gateway, "keep")
        moved = gateway.put(
            f"/admin/api/groups/{g_stray}",
            json={
                "name": "luna", "protocol": "openai", "api_key": "key-stray",
                "enabled": True, "upstream_id": keep_id,
            },
        )
        assert moved.status_code == 200, moved.text

        upstreams = {u["name"]: u for u in gateway.get("/admin/api/upstreams").json()}
        assert {g["name"] for g in upstreams["keep"]["groups"]} == {"默认", "luna"}
        assert upstreams["stray"]["groups"] == [], "分组搬走后原供应商就空了，可以删掉"

        cand = next(g for g in gateway.get("/admin/api/models").json() if g["model_name"] == "gpt-test")
        assert cand["candidates"][0]["upstream_name"] == "keep"
        assert cand["candidates"][0]["group_name"] == "luna"
        # base_url 在供应商上，所以搬完之后这把 key 就走 keep 的地址了 —— 这正是合并想要的效果
        # （站D / DDD2 两个域名本来就是同一个后端）。反过来说，两边地址不等价就别合。
        assert gateway.post("/v1/responses", json={"model": "gpt-test"}).json()["upstream"] == "siteA"

        assert gateway.delete(f"/admin/api/upstreams/{provider_id(gateway, 'stray')}").status_code == 200


def test_group_can_be_deleted_even_when_it_is_the_last_one(gateway):
    """接口挂在分组上，所以「只剩一个分组」不是特殊状态：删完这个站就是没 key、用不了而已。"""
    with MockUpstream("siteA") as a:
        g_a = add_upstream(gateway, a, "siteA")
        add_route(gateway, "gpt-test", g_a)
        assert gateway.delete(f"/admin/api/groups/{g_a}").status_code == 200

        detail = gateway.get("/admin/api/upstreams").json()[0]
        assert detail["groups"] == [] and detail["supports"] == []
        assert gateway.get("/admin/api/models").json() == [], "候选跟着分组一起走"
        assert gateway.post("/v1/responses", json={"model": "gpt-test"}).status_code == 404


def test_base_url_is_stored_as_a_root_and_v1_is_added_per_protocol(gateway):
    """两种接口的路径都在 /v1 底下，而 Anthropic 客户端给的地址是站根、OpenAI 给的是 …/v1。
    库里统一存站根：粘进来的 /v1 剥掉，转发时按接口补回去。"""
    with MockUpstream("siteA") as a:
        created = gateway.post(
            "/admin/api/upstreams", json={"name": "siteA", "base_url": a.base_url + "/v1/"}
        ).json()
        assert created["base_url"] == a.base_url, "尾部的 /v1 要剥掉，存的是站根"

        uid = int(created["id"])
        # 同名不同接口是允许的：UNIQUE 是 (供应商, 接口, 组名)
        g_oa = add_group(gateway, uid, "openai", api_key="key-siteA")
        g_an = add_group(gateway, uid, "anthropic", api_key="key-siteA")
        add_route(gateway, "gpt-test", g_oa)
        add_route(gateway, "opus", g_an, "claude-opus-4-1")

        assert gateway.post("/v1/responses", json={"model": "gpt-test"}).json()["upstream"] == "siteA"
        assert gateway.post("/v1/messages", json=msg("opus")).json()["upstream"] == "siteA"


def test_bulk_add_allows_same_name_on_another_interface(gateway):
    """拉一个站的模型列表动辄几十上百个。同名模型在别的接口下已有链不算冲突 ——
    一个站同时暴露 Responses 和 Chat Completions 很常见，两边各挂各的链。"""
    with MockUpstream("siteA") as a:
        g_an = add_upstream(gateway, a, "siteA", "anthropic")
        g_oa = add_group(gateway, provider_id(gateway, "siteA"), "openai", name="gpt", api_key="k")
        add_route(gateway, "claude-test", g_an, "claude-test")

        # claude-test 已经在 anthropic 下了，挂到 openai 分组照样成功
        resp = gateway.post(
            "/admin/api/models/bulk-add",
            json={"group_id": g_oa, "model_names": ["gpt-test", "claude-test"]},
        )
        assert resp.status_code == 200, resp.text
        assert resp.json() == {"added": 2, "skipped": []}

        rows = sorted(
            (g["model_name"], g["protocol"]) for g in gateway.get("/admin/api/models").json()
        )
        assert rows == [("claude-test", "anthropic"), ("claude-test", "openai"), ("gpt-test", "openai")]
        # 两条链各自有活跃候选，各自的接口都能调
        assert gateway.post("/v1/responses", json={"model": "claude-test"}).status_code == 200
        assert gateway.post("/v1/messages", json=msg("claude-test")).status_code == 200


def test_group_protocol_decides_the_pull_auth_headers(gateway):
    """Anthropic 站的 /v1/models 认 x-api-key + anthropic-version，只发 Bearer 多半是 401。"""
    with MockUpstream("siteA") as a:
        g_oa = add_upstream(gateway, a, "siteA", "openai")
        g_an = add_group(gateway, provider_id(gateway, "siteA"), "anthropic", api_key="key-siteA")

        plain = gateway.get(f"/admin/api/groups/{g_oa}/remote-models").json()["models"]
        claude = gateway.get(f"/admin/api/groups/{g_an}/remote-models").json()["models"]
        assert set(plain) == {"gpt-test", "claude-test"}
        assert set(claude) == {"claude-test", "claude-haiku-test"}, "mock 只在两个头都带上时才回这个"


def test_models_list_can_be_filtered_by_the_anthropic_version_header(gateway):
    """一个 /v1/models 服务两种客户端。带 anthropic-version 的（Claude Code 就带）
    只该看到它调得动的那些，认不出来的给全部。"""
    with MockUpstream("siteA") as a:
        g_an = add_upstream(gateway, a, "siteA", "anthropic")
        g_oa = add_group(gateway, provider_id(gateway, "siteA"), "openai", name="gpt", api_key="key-siteA")
        add_route(gateway, "opus", g_an, "claude-opus-4-1")
        add_route(gateway, "gpt-test", g_oa)

        every = {m["id"] for m in gateway.get("/v1/models").json()["data"]}
        assert every == {"opus", "gpt-test"}
        claude = gateway.get("/v1/models", headers={"anthropic-version": "2023-06-01"}).json()
        assert {m["id"] for m in claude["data"]} == {"opus"}


def test_candidate_remote_name_and_1m_can_be_edited(gateway):
    """1M 开关就是 remote_model 上的 [1m] 后缀，改候选走 PUT（原来只有档位弹窗能设）。"""
    with MockUpstream("siteA") as a:
        g_a = add_upstream(gateway, a, "siteA", "anthropic")
        rid = add_route(gateway, "opus", g_a, "claude-opus-4-1")
        assert "context-1m" not in gateway.post("/v1/messages", json=msg("opus")).json()["beta"]

        r = gateway.put(
            "/admin/api/models",
            json={"route_id": rid, "remote_model": "claude-opus-4-5[1m]"},
        )
        assert r.status_code == 200, r.text

        seen = gateway.post("/v1/messages", json=msg("opus")).json()
        assert seen["model"] == "claude-opus-4-5", "后缀只用来推断意图，绝不传给上游"
        assert "context-1m-2025-08-07" in seen["beta"]

        assert gateway.put(
            "/admin/api/models", json={"route_id": 9999, "remote_model": "x"}
        ).status_code == 404


def test_pull_failure_says_which_url_it_tried(gateway):
    """公益站三天两头连不上，而 httpx 的 DNS / 连接错误 str() 常常是空的 ——
    只回一句「拉取失败:」没法排查，至少得说清打的是哪个地址。"""
    created = gateway.post(
        "/admin/api/upstreams", json={"name": "dead", "base_url": "http://127.0.0.1:1"}
    ).json()
    gid = add_group(gateway, int(created["id"]), "anthropic")

    resp = gateway.get(f"/admin/api/groups/{gid}/remote-models")
    assert resp.status_code == 502
    detail = resp.json()["detail"]
    assert "http://127.0.0.1:1/v1/models" in detail, detail
    assert detail.strip() != "拉取失败:", "异常消息为空时也得留点线索"


def test_group_protocol_is_locked_once_it_has_candidates(gateway):
    """改接口等于把已录入的模型悄悄换成另一种线格式，有候选就不给改。"""
    with MockUpstream("siteA") as a:
        g_a = add_upstream(gateway, a, "siteA", "openai")
        add_route(gateway, "gpt-test", g_a)

        locked = gateway.put(f"/admin/api/groups/{g_a}", json={
            "name": "默认", "protocol": "anthropic", "api_key": "key-siteA", "enabled": True})
        assert locked.status_code == 409 and "接口" in locked.json()["detail"]

        # 名字和 key 照样能改
        assert gateway.put(f"/admin/api/groups/{g_a}", json={
            "name": "renamed", "protocol": "openai", "api_key": "k2", "enabled": True}).status_code == 200


def test_cloning_a_group_copies_the_key_to_the_other_interface(gateway):
    """一把 key 两种接口都能用的站不少，而接口是分组的属性，手动再填一遍 key 很烦。
    有三种接口之后，「复制到另一种」必须点名目标。"""
    with MockUpstream("siteA") as a:
        g_a = add_upstream(gateway, a, "siteA", "openai")
        clone = gateway.post(f"/admin/api/groups/{g_a}/clone", json={"protocol": "anthropic"})
        assert clone.status_code == 200, clone.text
        assert (clone.json()["protocol"], clone.json()["api_key"]) == ("anthropic", "key-siteA")

        detail = gateway.get("/admin/api/upstreams").json()[0]
        assert detail["supports"] == ["anthropic", "openai"]
        assert {g["name"] for g in detail["groups"]} == {"默认"}, "同名不同接口"


def test_protocol_metadata_is_a_safe_display_projection(gateway):
    data = gateway.get("/admin/api/protocols")
    assert data.status_code == 200
    assert data.json() == {
        "protocols": [
            {
                "name": "anthropic",
                "label": "Anthropic Messages",
                "short": "Anthropic",
                "path": "/v1/messages",
                "client": "Claude Code",
                "supports_1m": True,
            },
            {
                "name": "openai",
                "label": "OpenAI Responses",
                "short": "OpenAI",
                "path": "/v1/responses",
                "client": "Codex",
                "supports_1m": False,
            },
            {
                "name": "openai-chat",
                "label": "OpenAI Chat Completions",
                "short": "OpenAI Chat",
                "path": "/v1/chat/completions",
                "client": "OpenAI SDK",
                "supports_1m": False,
            },
        ]
    }


def test_disabling_a_protocol_hides_routes_groups_sites_and_blocks_calls(gateway):
    with MockUpstream("claude-only") as a, MockUpstream("both") as b:
        g_a = add_upstream(gateway, a, "claude-only", "anthropic")
        add_route(gateway, "opus", g_a, "claude-opus-4-1")

        g_b_an = add_upstream(gateway, b, "both", "anthropic")
        g_b_oa = add_group(
            gateway, provider_id(gateway, "both"), "openai", name="gpt", api_key="key-both"
        )
        add_route(gateway, "opus-b", g_b_an, "claude-opus-4-1")
        add_route(gateway, "gpt-test", g_b_oa)

        off = gateway.post(
            "/admin/api/protocol-switches", json={"protocol": "anthropic", "enabled": False}
        )
        assert off.status_code == 200, off.text
        assert off.json()["enabled"]["anthropic"] is False

        assert {m["model_name"] for m in gateway.get("/admin/api/models").json()} == {"gpt-test"}

        ups = {u["name"]: u for u in gateway.get("/admin/api/upstreams").json()}
        assert "claude-only" not in ups, "只有被停用协议的站整个隐藏"
        assert [g["protocol"] for g in ups["both"]["groups"]] == ["openai"]

        assert {m["id"] for m in gateway.get("/v1/models").json()["data"]} == {"gpt-test"}
        assert gateway.post("/v1/messages", json=msg("opus")).status_code == 404
        assert gateway.post("/v1/responses", json={"model": "gpt-test"}).status_code == 200

        # 打开开关就原样回来：配置一直留着
        gateway.post(
            "/admin/api/protocol-switches", json={"protocol": "anthropic", "enabled": True}
        )
        assert {m["model_name"] for m in gateway.get("/admin/api/models").json()} == {
            "opus", "opus-b", "gpt-test",
        }
        assert {u["name"] for u in gateway.get("/admin/api/upstreams").json()} == {
            "claude-only", "both",
        }
        assert gateway.post("/v1/messages", json=msg("opus")).status_code == 200


def test_disabled_protocol_is_hidden_from_stats_and_log(gateway):
    from gateway import db

    def logged(model: str, protocol: str) -> None:
        db.insert_request(
            client="test", model=model, upstream="siteA", status=200, stream=False,
            req_bytes=10, resp_bytes=20, duration_ms=30, input_tokens=5, output_tokens=5,
            cached_tokens=0, note="ok", protocol=protocol,
        )

    logged("claude-x", "anthropic")
    logged("gpt-x", "openai")
    assert gateway.get("/admin/api/stats").json()["requests"] == 2
    assert {r["model"] for r in gateway.get("/admin/api/requests").json()} == {"claude-x", "gpt-x"}

    gateway.post("/admin/api/protocol-switches", json={"protocol": "anthropic", "enabled": False})
    assert gateway.get("/admin/api/stats").json()["requests"] == 1
    assert [r["model"] for r in gateway.get("/admin/api/requests").json()] == ["gpt-x"]
    overview = gateway.get("/admin/api/overview?window=24h").json()
    assert overview["totals"]["requests"] == 1
    assert {m["model"] for m in overview["models"]} == {"gpt-x"}


def test_protocol_switch_rejects_unknown_protocol(gateway):
    bad = gateway.post(
        "/admin/api/protocol-switches", json={"protocol": "nope", "enabled": False}
    )
    assert bad.status_code == 400


def test_clone_requires_an_explicit_target_when_more_than_two_protocols(gateway, monkeypatch):
    from dataclasses import replace
    from gateway import protocols

    third = replace(
        protocols.OPENAI,
        name="test-third",
        label="Test Third",
        path="/test/third",
        client="Test Client",
    )
    all_protocols = {**protocols.ALL, third.name: third}
    monkeypatch.setattr(protocols, "ALL", all_protocols)
    monkeypatch.setattr(protocols, "NAMES", tuple(all_protocols))

    with MockUpstream("siteA") as a:
        group_id = add_upstream(gateway, a, "siteA")
        missing = gateway.post(f"/admin/api/groups/{group_id}/clone")
        assert missing.status_code == 400 and "明确" in missing.json()["detail"]

        same = gateway.post(
            f"/admin/api/groups/{group_id}/clone", json={"protocol": "openai"}
        )
        assert same.status_code == 400 and "不同" in same.json()["detail"]

        invalid = gateway.post(
            f"/admin/api/groups/{group_id}/clone", json={"protocol": "missing"}
        )
        assert invalid.status_code == 400

        copied = gateway.post(
            f"/admin/api/groups/{group_id}/clone", json={"protocol": "test-third"}
        )
        assert copied.status_code == 200
        assert (copied.json()["protocol"], copied.json()["api_key"]) == (
            "test-third", "key-siteA"
        )


@pytest.mark.parametrize(
    "version, missing",
    [
        (0, True),
        (3, True),
        (4, True),
        (5, True),  # v5 缺 retry_rules，要补列并预置同站重试
        (0, False),
        # 完整基线跟 SCHEMA_VERSION 走：结构升版时这里不用手改数字
        (gateway_db.SCHEMA_VERSION, False),
    ],
)
def test_cache_creation_migration_preserves_routes_and_logs(tmp_path, monkeypatch, version, missing):
    """未打号、正常 v3、误打 v4 的老库都要补列；完整的新库不应迁移或备份。
    完整基线是当前 SCHEMA_VERSION —— 旧版即使 cache_creation 列齐全，也要补后续迁移。"""
    import sqlite3

    from gateway import config, db

    db_path = tmp_path / "gateway.db"
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "DB_PATH", db_path)
    with sqlite3.connect(db_path) as conn:
        conn.executescript(db._SCHEMA)
        conn.executescript("""
            INSERT INTO upstreams(id, name, base_url, egress) VALUES(5, 'siteA', 'https://a.example', 'direct');
            INSERT INTO upstream_groups(id, upstream_id, name, protocol) VALUES(13, 5, 'default', 'anthropic');
            INSERT INTO model_routes(id, model_name, group_id, remote_model, is_active, priority)
                VALUES(42, 'm', 13, 'remote', 1, 7);
            INSERT INTO request_log(client, model, upstream, protocol, status, stream, req_bytes, resp_bytes, duration_ms)
                VALUES('test', 'm', 'siteA', 'anthropic', 200, 0, 10, 20, 30);
        """)
        if missing:
            conn.execute("ALTER TABLE request_log DROP COLUMN cache_creation_tokens")
        conn.execute(f"PRAGMA user_version={version}")
    conn.close()

    db.init_db()
    assert db._schema_version(db_path) == db.SCHEMA_VERSION
    assert "cache_creation_tokens" in db._columns(db_path, "request_log")
    backups = list(tmp_path.glob("gateway.db.bak-*"))
    assert bool(backups) == missing
    if missing:
        assert "cache_creation_tokens" not in db._columns(backups[0], "request_log")
    route = db.resolve_route("m", "anthropic")
    assert (route.route_id, route.group_id, route.remote_model, route.upstream.egress) == (42, 13, "remote", "direct")
    assert db.list_routes()[0]["priority"] == 7
    assert db.recent_requests(1)[0]["cache_creation_tokens"] is None
    db.insert_request(
        client="test", model="m", upstream="siteA", status=200, stream=False,
        req_bytes=10, resp_bytes=20, duration_ms=30, input_tokens=100,
        output_tokens=10, cached_tokens=20, cache_creation_tokens=50, note="ok", protocol="anthropic",
    )
    assert len(db.recent_requests()) == 2
    assert db.recent_requests(1)[0]["cache_creation_tokens"] == 50
    monkeypatch.setattr(db, "_backup_db", lambda path: pytest.fail("重复启动不应再次迁移"))
    db.init_db()
    assert db.resolve_route("m", "anthropic").route_id == 42


_PRE_GROUP_SCHEMA = """
CREATE TABLE upstreams(
  id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL UNIQUE, base_url TEXT NOT NULL,
  api_key TEXT NOT NULL DEFAULT '', enabled INTEGER NOT NULL DEFAULT 1,
  created_at TEXT NOT NULL DEFAULT (datetime('now','localtime')),
  header_override TEXT NOT NULL DEFAULT '');
CREATE TABLE model_routes(
  model_name TEXT NOT NULL,
  upstream_id INTEGER NOT NULL REFERENCES upstreams(id) ON DELETE CASCADE,
  remote_model TEXT NOT NULL, is_active INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY(model_name, upstream_id));
CREATE TABLE request_log(
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, client TEXT NOT NULL, model TEXT NOT NULL,
  upstream TEXT NOT NULL, status INTEGER NOT NULL, stream INTEGER NOT NULL,
  req_bytes INTEGER NOT NULL, resp_bytes INTEGER NOT NULL, duration_ms INTEGER NOT NULL,
  input_tokens INTEGER, output_tokens INTEGER, cached_tokens INTEGER,
  note TEXT NOT NULL DEFAULT '');
INSERT INTO upstreams(name, base_url, api_key) VALUES
  ('siteA', 'https://a.example/v1', 'sk-aaa'), ('siteB', 'https://b.example/v1', 'sk-bbb');
INSERT INTO model_routes(model_name, upstream_id, remote_model, is_active) VALUES
  ('m1', 1, 'remote-1', 1), ('m1', 2, 'remote-1b', 0), ('m2', 2, 'remote-2', 1);
INSERT INTO request_log(client, model, upstream, status, stream, req_bytes, resp_bytes, duration_ms, note)
  VALUES ('codex', 'm1', 'siteA', 200, 0, 10, 20, 30, 'ok');
"""


def test_migration_from_pre_group_schema(tmp_path, monkeypatch):
    """老库（api_key 挂在上游行上）原地升级成供应商 / 分组结构，候选和历史记录一条不少。"""
    import sqlite3

    from gateway import config, db

    data_dir = tmp_path / "data"
    data_dir.mkdir()
    db_path = data_dir / "gateway.db"
    monkeypatch.setattr(config, "DATA_DIR", data_dir)
    monkeypatch.setattr(config, "DB_PATH", db_path)

    old = sqlite3.connect(db_path)
    old.executescript(_PRE_GROUP_SCHEMA)
    old.commit()
    old.close()

    assert db._is_pre_group_shape(db_path)
    db.init_db()
    assert not db._is_pre_group_shape(db_path)
    assert list(data_dir.glob("gateway.db.bak-*")), "迁移前必须留一份备份"

    groups = {(g.upstream_id, g.name): g for g in db.list_groups()}
    assert set(groups) == {(1, "默认"), (2, "默认")}
    assert groups[(1, "默认")].api_key == "sk-aaa"
    assert groups[(2, "默认")].api_key == "sk-bbb"
    for group in groups.values():
        assert group.protocol == "openai", "今天之前只有 /v1/responses，历史配置就是 openai 接口"
    for upstream in db.list_upstreams():
        assert not upstream.base_url.endswith("/v1"), "base_url 统一存站根"

    rows = db.list_routes()
    assert {(r["model_name"], r["upstream_name"], r["remote_model"], r["protocol"]) for r in rows} == {
        ("m1", "siteA", "remote-1", "openai"),
        ("m1", "siteB", "remote-1b", "openai"),
        ("m2", "siteB", "remote-2", "openai"),
    }

    route = db.resolve_route("m1", "openai")
    assert route.upstream.name == "siteA" and route.upstream.api_key == "sk-aaa"
    assert (route.group_name, route.remote_model) == ("默认", "remote-1")
    assert route.upstream.base_url == "https://a.example"
    assert db.resolve_route("m1", "anthropic") is None, "接口参与匹配"

    assert db.request_stats()["requests"] == 1, "历史转发记录不能丢"
    assert db.recent_requests(1)[0]["protocol"] == "openai", "老记录的协议列要回填"

    db.init_db()   # 再跑一遍不能出事，也不能又建一遍分组
    assert len(db.list_groups()) == 2


_PRE_PROTOCOL_SCHEMA = """
CREATE TABLE upstreams(
  id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL UNIQUE, base_url TEXT NOT NULL,
  enabled INTEGER NOT NULL DEFAULT 1, header_override TEXT NOT NULL DEFAULT '',
  protocols TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL DEFAULT (datetime('now','localtime')));
CREATE TABLE upstream_groups(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  upstream_id INTEGER NOT NULL REFERENCES upstreams(id) ON DELETE CASCADE,
  name TEXT NOT NULL, api_key TEXT NOT NULL DEFAULT '', enabled INTEGER NOT NULL DEFAULT 1,
  created_at TEXT NOT NULL DEFAULT (datetime('now','localtime')),
  UNIQUE(upstream_id, name));
CREATE TABLE model_routes(
  model_name TEXT NOT NULL,
  group_id INTEGER NOT NULL REFERENCES upstream_groups(id) ON DELETE CASCADE,
  remote_model TEXT NOT NULL, is_active INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY(model_name, group_id));
CREATE TABLE model_meta(model_name TEXT PRIMARY KEY, side TEXT NOT NULL DEFAULT '');
CREATE TABLE request_log(
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, client TEXT NOT NULL, model TEXT NOT NULL,
  remote_model TEXT NOT NULL DEFAULT '', protocol TEXT NOT NULL DEFAULT '',
  upstream TEXT NOT NULL, group_name TEXT NOT NULL DEFAULT '', status INTEGER NOT NULL,
  stream INTEGER NOT NULL, req_bytes INTEGER NOT NULL, resp_bytes INTEGER NOT NULL,
  duration_ms INTEGER NOT NULL, input_tokens INTEGER, output_tokens INTEGER,
  cached_tokens INTEGER, note TEXT NOT NULL DEFAULT '');
INSERT INTO upstreams(name, base_url, protocols) VALUES
  ('gpt-site', 'https://a.example/v1', 'openai'),
  ('claude-site', 'https://b.example', 'anthropic'),
  ('both', 'https://c.example/v1', 'openai,anthropic');
INSERT INTO upstream_groups(upstream_id, name, api_key) VALUES
  (1, '默认', 'sk-aaa'), (1, 'luna', 'sk-luna'), (2, '默认', 'sk-bbb'), (3, '默认', 'sk-ccc');
INSERT INTO model_routes(model_name, group_id, remote_model, is_active) VALUES
  ('m1', 1, 'remote-1', 1), ('m1', 2, 'remote-1b', 0), ('m2', 4, 'remote-2', 1);
INSERT INTO model_meta(model_name, side) VALUES ('m1', 'openai'), ('m2', 'openai');
INSERT INTO request_log(client, model, upstream, status, stream, req_bytes, resp_bytes, duration_ms, note)
  VALUES ('codex', 'm1', 'gpt-site', 200, 0, 10, 20, 30, 'ok');
"""


def test_migration_moves_the_protocol_mark_onto_groups(tmp_path, monkeypatch):
    """上一版结构：接口标记挂在 upstreams.protocols 上、base_url 填到 /v1。
    迁移要把接口搬到分组上、把 base_url 收成站根，候选和历史记录一条不少。"""
    import sqlite3

    from gateway import config, db

    data_dir = tmp_path / "data"
    data_dir.mkdir()
    db_path = data_dir / "gateway.db"
    monkeypatch.setattr(config, "DATA_DIR", data_dir)
    monkeypatch.setattr(config, "DB_PATH", db_path)

    old = sqlite3.connect(db_path)
    old.executescript(_PRE_PROTOCOL_SCHEMA)
    old.commit()
    old.close()

    assert db._is_pre_protocol_shape(db_path)
    db.init_db()
    assert not db._is_pre_protocol_shape(db_path)
    assert list(data_dir.glob("gateway.db.bak-*")), "迁移前必须留一份备份"

    assert {u.name: u.base_url for u in db.list_upstreams()} == {
        "gpt-site": "https://a.example",
        "claude-site": "https://b.example",
        "both": "https://c.example",
    }
    assert "protocols" not in db._columns(db_path, "upstreams"), "接口标记已经挪到分组上了"
    assert not db._columns(db_path, "model_meta"), "模型的接口由候选推出来，不再单独存"

    by_upstream = {u.id: u.name for u in db.list_upstreams()}
    got = {(by_upstream[g.upstream_id], g.name, g.protocol, g.api_key) for g in db.list_groups()}
    assert got == {
        ("gpt-site", "默认", "openai", "sk-aaa"),
        ("gpt-site", "luna", "openai", "sk-luna"),
        ("claude-site", "默认", "anthropic", "sk-bbb"),
        # 两种格式都标了的站：候选留在 openai 那个分组上（历史流量就是 /v1/responses），
        # 另一种接口留一个同 key 的空分组，别把填过的信息弄丢
        ("both", "默认", "openai", "sk-ccc"),
        ("both", "默认", "anthropic", "sk-ccc"),
    }

    rows = db.list_routes()
    assert {(r["model_name"], r["group_name"], r["remote_model"], r["protocol"]) for r in rows} == {
        ("m1", "默认", "remote-1", "openai"),
        ("m1", "luna", "remote-1b", "openai"),
        ("m2", "默认", "remote-2", "openai"),
    }
    route = db.resolve_route("m1", "openai")
    assert (route.upstream.name, route.group_name, route.upstream.api_key) == ("gpt-site", "默认", "sk-aaa")
    assert db.request_stats()["requests"] == 1
    assert db.recent_requests(1)[0]["protocol"] == "openai", "老记录的协议列要回填"

    # 老库的候选是复合主键，顺带升级成自增 id：一个分组下才塞得下第二条映射
    assert not db._is_pre_routeid_shape(db_path)
    assert all(r["route_id"] for r in rows), "每条候选都该有自己的 id"
    first = next(r for r in rows if r["remote_model"] == "remote-1")
    assert db.add_model_route("m1", first["group_id"], "remote-1-alt"), "同分组换个真名能再加一条"
    assert db.add_model_route("m1", first["group_id"], "remote-1") == 0, "一模一样的还是重复"

    db.init_db()   # 幂等
    assert len(db.list_groups()) == 5
    assert len(list(data_dir.glob("gateway.db.bak-*"))) == 1


def test_capture_stream_switch_is_hot_and_visible_over_the_api(gateway, monkeypatch):
    """抓包开关要能纯靠接口开着关 —— 出问题时不该再动一次代码。"""
    from gateway import capture, config

    # 管理接口走的是 config.DATA_DIR；fixture 已经把它指到临时目录了，这里只是拿个引用。
    data_dir = config.DATA_DIR
    capture.disable()

    off = gateway.get("/admin/api/capture-stream").json()
    assert off["enabled"] is False

    on = gateway.put("/admin/api/capture-stream", json={"enabled": True, "max": 2}).json()
    assert on["enabled"] is True and on["max"] == 2
    assert (data_dir / capture.FLAG_NAME).exists()

    assert gateway.put("/admin/api/capture-stream", json={"enabled": False}).json()["enabled"] is False
    assert not (data_dir / capture.FLAG_NAME).exists()


def test_rewrite_rules_reach_the_upstream(gateway):
    """配了规则，转发给上游的请求体就得是改过的 —— apply() 由单测覆盖，这里测接线。"""
    import json

    phrase = "- keep: one, two, three"
    fixed = "keep: one, two, three"

    with MockUpstream("siteA") as a:
        gid = add_upstream(gateway, a, "siteA", "openai")  # 建站 + 建分组，返回分组 id
        add_route(gateway, "gpt-rewrite", gid)

        saved = gateway.put("/admin/api/rewrite-rules", json={"rules": [{"from": phrase, "to": fixed}]})
        assert saved.status_code == 200
        assert saved.json()["rules"][0]["from"] == phrase

        resp = gateway.post("/v1/responses", json={
            "model": "gpt-rewrite",
            "input": [{"type": "message", "role": "user",
                       "content": [{"type": "input_text", "text": f"x\n{phrase}"}]}],
        })
        assert resp.status_code == 200, resp.text

        sent = json.dumps(a.last_responses_request(), ensure_ascii=False)
        assert phrase not in sent, "触发上游误报的那行必须被洗掉"
        assert fixed in sent

    # 规则是全局的，用完清掉，别污染别的用例
    assert gateway.put("/admin/api/rewrite-rules", json={"rules": []}).json()["rules"] == []


def test_rewrite_rules_round_trip_and_start_empty(gateway):
    assert gateway.get("/admin/api/rewrite-rules").json()["rules"] == []
    put = gateway.put("/admin/api/rewrite-rules", json={
        "rules": [{"from": "a", "to": "b"}, {"from": "c", "to": ""}],
    })
    assert put.status_code == 200
    assert gateway.get("/admin/api/rewrite-rules").json()["rules"] == [
        {"from": "a", "to": "b"}, {"from": "c", "to": ""},
    ]
    assert gateway.put("/admin/api/rewrite-rules", json={"rules": []}).json()["rules"] == []


def test_rewrite_rule_requires_a_non_empty_from(gateway):
    bad = gateway.put("/admin/api/rewrite-rules", json={"rules": [{"to": "b"}]})
    assert bad.status_code == 422, "没有 from 的规则会被 FastAPI 挡在门外"
