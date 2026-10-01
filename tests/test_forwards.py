"""下游→下游转发：一个模型的整条链交给另一个模型（编排页的后端能力）。"""

from __future__ import annotations

import sqlite3

from helpers import MockUpstream, add_route, add_upstream, two_anthropic_sites, msg


def set_forward(client, model_name, protocol, target_model):
    return client.post(
        "/admin/api/models/forward",
        json={"model_name": model_name, "protocol": protocol, "target_model": target_model},
    )


def clear_forward(client, model_name, protocol):
    return client.delete(
        "/admin/api/models/forward",
        params={"model_name": model_name, "protocol": protocol},
    )


def models_row(client, model_name):
    return next(
        (r for r in client.get("/admin/api/models").json() if r["model_name"] == model_name),
        None,
    )


def test_forward_sends_all_traffic_to_target_chain(gateway):
    """A 挂了转发后，它自己的候选不再参与路由，请求走 B 的站。"""
    with MockUpstream("siteA") as a, MockUpstream("siteB") as b:
        g_a = add_upstream(gateway, a, "siteA")
        g_b = add_upstream(gateway, b, "siteB")
        r_alias = add_route(gateway, "alias", g_a, "remote-alias")
        add_route(gateway, "target", g_b, "remote-target")

        resp = set_forward(gateway, "alias", "openai", "target")
        assert resp.status_code == 200, resp.text

        body = gateway.post("/v1/responses", json={"model": "alias"}).json()
        assert body["upstream"] == "siteB"
        # 网关透传上游响应（不带 model 字段）；改写发生在发给上游的请求体里
        assert b.last_responses_request()["model"] == "remote-target"

        row = models_row(gateway, "alias")
        assert row["forward_to"] == "target"
        assert row["active_route_id"] is None, "转发中不报自己候选的 active"
        assert [c["route_id"] for c in row["candidates"]] == [r_alias], "自己的候选保留（取消转发即恢复）"


def test_forward_chain_multi_hop_and_alias_only_models(gateway):
    """a→b→c：只有 c 有候选；a、b 作为纯转发模型同样可调用、在暴露清单里。"""
    with MockUpstream("siteC") as c:
        g_c = add_upstream(gateway, c, "siteC")
        add_route(gateway, "c", g_c, "remote-c")

        assert set_forward(gateway, "b", "openai", "c").status_code == 200
        assert set_forward(gateway, "a", "openai", "b").status_code == 200

        exposed = {m["id"] for m in gateway.get("/v1/models").json()["data"]}
        assert {"a", "b", "c"} <= exposed

        body = gateway.post("/v1/responses", json={"model": "a"}).json()
        assert body["upstream"] == "siteC"
        assert c.last_responses_request()["model"] == "remote-c"

        row = models_row(gateway, "b")
        assert row is not None and row["candidates"] == []
        assert row["forward_to"] == "c"


def test_forward_rejects_cycles_and_self(gateway):
    with MockUpstream("siteA") as a, MockUpstream("siteB") as b:
        g_a = add_upstream(gateway, a, "siteA")
        g_b = add_upstream(gateway, b, "siteB")
        add_route(gateway, "one", g_a)
        add_route(gateway, "two", g_b)

        self_loop = set_forward(gateway, "one", "openai", "one")
        assert self_loop.status_code == 409
        assert "环" in self_loop.json()["detail"]

        assert set_forward(gateway, "one", "openai", "two").status_code == 200
        resp = set_forward(gateway, "two", "openai", "one")
        assert resp.status_code == 409
        assert "环" in resp.json()["detail"]


def test_forward_rejects_missing_target(gateway):
    with MockUpstream("siteA") as a:
        g_a = add_upstream(gateway, a, "siteA")
        add_route(gateway, "one", g_a)
        resp = set_forward(gateway, "one", "openai", "ghost")
        assert resp.status_code == 409
        assert "ghost" in resp.json()["detail"]


def test_forward_target_must_have_chain_in_same_protocol(gateway):
    """目标在别的接口下有链不算数：同接口内没有候选就拒。"""
    with MockUpstream("siteA") as a:
        g_a = add_upstream(gateway, a, "siteA", "anthropic")
        add_route(gateway, "one", g_a)
    with MockUpstream("siteB") as b:
        g_b = add_upstream(gateway, b, "siteB", "openai")
        add_route(gateway, "two", g_b)
        resp = set_forward(gateway, "one", "anthropic", "two")
        assert resp.status_code == 409
        assert "anthropic" in resp.json()["detail"]


def test_forward_cleared_restores_own_chain(gateway):
    with MockUpstream("siteA") as a, MockUpstream("siteB") as b:
        g_a = add_upstream(gateway, a, "siteA")
        g_b = add_upstream(gateway, b, "siteB")
        add_route(gateway, "alias", g_a, "remote-alias")
        add_route(gateway, "target", g_b, "remote-target")
        assert set_forward(gateway, "alias", "openai", "target").status_code == 200
        assert gateway.post("/v1/responses", json={"model": "alias"}).json()["upstream"] == "siteB"

        resp = clear_forward(gateway, "alias", "openai")
        assert resp.status_code == 200, resp.text
        assert gateway.post("/v1/responses", json={"model": "alias"}).json()["upstream"] == "siteA"
        assert models_row(gateway, "alias")["forward_to"] is None

        assert clear_forward(gateway, "alias", "openai").status_code == 404


def test_forward_upsert_changes_target(gateway):
    with MockUpstream("siteA") as a, MockUpstream("siteB") as b, MockUpstream("siteC") as c:
        g_a = add_upstream(gateway, a, "siteA")
        g_b = add_upstream(gateway, b, "siteB")
        g_c = add_upstream(gateway, c, "siteC")
        add_route(gateway, "alias", g_a)
        add_route(gateway, "t1", g_b)
        add_route(gateway, "t2", g_c)
        assert set_forward(gateway, "alias", "openai", "t1").status_code == 200
        assert set_forward(gateway, "alias", "openai", "t2").status_code == 200
        assert gateway.post("/v1/responses", json={"model": "alias"}).json()["upstream"] == "siteC"


def test_switch_on_target_chain_follows_for_alias(gateway):
    """切目标的候选，转发过来的流量跟着切。"""
    with MockUpstream("siteA") as a, MockUpstream("siteB") as b:
        g_a = add_upstream(gateway, a, "siteA")
        g_b = add_upstream(gateway, b, "siteB")
        add_route(gateway, "target", g_a, "remote-a")
        r_b = add_route(gateway, "target", g_b, "remote-b")
        assert set_forward(gateway, "alias", "openai", "target").status_code == 200

        resp = gateway.post("/admin/api/models/switch", json={"route_id": r_b})
        assert resp.status_code == 200, resp.text
        assert gateway.post("/v1/responses", json={"model": "alias"}).json()["upstream"] == "siteB"


def test_failover_through_forward(gateway):
    """目标链的自动降级同样作用于转发进来的流量（anthropic 默认开降级）。"""
    with MockUpstream("siteA") as a, MockUpstream("siteB") as b:
        two_anthropic_sites(gateway, a, b)
        assert set_forward(gateway, "alias", "anthropic", "opus").status_code == 200
        a.fail_with(503)
        resp = gateway.post("/v1/messages", json=msg("alias"))
        assert resp.status_code == 200, resp.text
        assert resp.json()["upstream"] == "siteB"


def test_broken_forward_reports_clearly_and_keeps_arrow(gateway):
    """删掉目标后：请求 404 且文案说转发；转发行保留（显示为断链，由人决定去留）。"""
    with MockUpstream("siteA") as a, MockUpstream("siteB") as b:
        g_a = add_upstream(gateway, a, "siteA")
        g_b = add_upstream(gateway, b, "siteB")
        add_route(gateway, "alias", g_a)
        add_route(gateway, "target", g_b)
        assert set_forward(gateway, "alias", "openai", "target").status_code == 200

        resp = gateway.delete(
            "/admin/api/models", params={"model_name": "target", "protocol": "openai"}
        )
        assert resp.status_code == 200, resp.text

        denied = gateway.post("/v1/responses", json={"model": "alias"})
        assert denied.status_code == 404
        assert "转发" in denied.json()["error"]["message"]

        row = models_row(gateway, "alias")
        assert row["forward_to"] == "target", "断链保留，由人决定取消还是改指"


def test_alias_wrong_protocol_404_hints(gateway):
    """纯转发别名在别的接口被叫到时，404 文案指出它在哪暴露。"""
    with MockUpstream("siteC") as c:
        g_c = add_upstream(gateway, c, "siteC", "anthropic")
        add_route(gateway, "c", g_c)
        assert set_forward(gateway, "a", "anthropic", "c").status_code == 200
        resp = gateway.post("/v1/responses", json={"model": "a"})
        assert resp.status_code == 404
        assert "anthropic" in resp.json()["error"]["message"]


def test_delete_model_cleans_its_own_forward(gateway):
    with MockUpstream("siteA") as a, MockUpstream("siteB") as b:
        g_a = add_upstream(gateway, a, "siteA")
        g_b = add_upstream(gateway, b, "siteB")
        add_route(gateway, "alias", g_a)
        add_route(gateway, "target", g_b)
        assert set_forward(gateway, "alias", "openai", "target").status_code == 200

        resp = gateway.delete(
            "/admin/api/models", params={"model_name": "alias", "protocol": "openai"}
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["removed"] == 1 and body["forwards_removed"] == 1
        assert models_row(gateway, "alias") is None


def test_forward_protocol_scoped(gateway):
    """anthropic 下的转发不影响同名模型在 openai 下的链。"""
    with MockUpstream("siteO") as o, MockUpstream("siteA") as a, MockUpstream("siteT") as t:
        g_o = add_upstream(gateway, o, "siteO", "openai")
        g_a = add_upstream(gateway, a, "siteA", "anthropic")
        g_t = add_upstream(gateway, t, "siteT", "anthropic")
        add_route(gateway, "multi", g_o)
        add_route(gateway, "multi", g_a)
        add_route(gateway, "other", g_t)
        assert set_forward(gateway, "multi", "anthropic", "other").status_code == 200

        assert gateway.post("/v1/responses", json={"model": "multi"}).json()["upstream"] == "siteO"
        assert gateway.post("/v1/messages", json=msg("multi")).json()["upstream"] == "siteT"


def test_old_db_upgrade_creates_forwards_table_and_hold_retry(gateway):
    """老库升到当前版本：建回 model_forwards，并补上 200 截断守卫那一列。"""
    from gateway import config, db

    with sqlite3.connect(config.DB_PATH) as conn:
        conn.execute("DROP TABLE model_forwards")
        # v7 及以前的 upstreams 没有 hold_retry：CREATE TABLE IF NOT EXISTS 不会给
        # 已存在的表加字段，只能靠迁移补
        conn.execute("ALTER TABLE upstreams DROP COLUMN hold_retry")
        conn.execute("PRAGMA user_version=7")

    db.init_db()

    with sqlite3.connect(config.DB_PATH) as conn:
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert "model_forwards" in tables
        assert conn.execute("PRAGMA user_version").fetchone()[0] == db.SCHEMA_VERSION
    assert "hold_retry" in db._columns(config.DB_PATH, "upstreams")
