"""编排画布的坐标记忆：纯展示状态，坏了一份也只该退回自动摆放。

这层的边界值得盯着：坐标**不能**影响路由。所以除了「存得住、读得回」，
用例重点在坏输入 —— 越界坐标、坏键、超量、不认识的字段、库里被手改坏的 JSON。
"""

from __future__ import annotations

from gateway import canvas, db
from helpers import MockUpstream, add_route, add_upstream


def get_layout(client):
    resp = client.get("/admin/api/canvas-layout")
    assert resp.status_code == 200, resp.text
    return resp.json()


def put_layout(client, nodes, view=None):
    body = {"nodes": nodes}
    if view is not None:
        body["view"] = view
    return client.put("/admin/api/canvas-layout", json=body)


def test_empty_layout_is_readable_before_anything_is_placed(gateway):
    """一次都没摆过时也要给一份完整的空布局，前端不用自己判 null。"""
    layout = get_layout(gateway)
    assert layout["nodes"] == {}
    assert layout["view"] == {"x": 0.0, "y": 0.0, "z": 1.0}
    assert layout["v"] == canvas.LAYOUT_VERSION


def test_roundtrip_keeps_positions(gateway):
    with MockUpstream("siteA") as a:
        g_a = add_upstream(gateway, a, "siteA")
        add_route(gateway, "chat", g_a, "remote-chat")

        key = canvas.model_key("chat", "openai")
        assert put_layout(gateway, {key: [120.5, -40]}).status_code == 200

        layout = get_layout(gateway)
        assert layout["nodes"][key] == [120.5, -40.0]


def test_view_pan_and_zoom_roundtrip(gateway):
    resp = put_layout(gateway, {}, {"x": -300, "y": 88.5, "z": 1.75})
    assert resp.status_code == 200, resp.text
    assert get_layout(gateway)["view"] == {"x": -300.0, "y": 88.5, "z": 1.75}


def test_coordinates_for_missing_models_are_pruned(gateway):
    """模型都删了，它的坐标就是垃圾 —— 写的时候顺手剪掉，不让它无限攒。"""
    with MockUpstream("siteA") as a:
        g_a = add_upstream(gateway, a, "siteA")
        add_route(gateway, "chat", g_a, "remote-chat")
        key = canvas.model_key("chat", "openai")
        put_layout(gateway, {key: [10, 20]})
        assert key in get_layout(gateway)["nodes"]

        assert gateway.delete("/admin/api/models", params={"model_name": "chat"}).status_code == 200
        # 再存一次别的节点，旧键在这一次写入里被剪掉
        put_layout(gateway, {canvas.upstream_key(g_a, "remote-chat"): [5, 5]})
        assert key not in get_layout(gateway)["nodes"]


def test_upstream_node_keeps_position_when_a_route_is_removed(gateway):
    """上游节点绑的是「分组 + 真名」，摘掉某条下游候选不该把它的位置抹了。"""
    with MockUpstream("siteA") as a:
        g_a = add_upstream(gateway, a, "siteA")
        route_id = add_route(gateway, "chat", g_a, "remote-chat")
        ukey = canvas.upstream_key(g_a, "remote-chat")
        put_layout(gateway, {ukey: [70, 70]})

        assert gateway.delete("/admin/api/models", params={"route_id": route_id}).status_code == 200
        assert get_layout(gateway)["nodes"][ukey] == [70.0, 70.0]


def test_structurally_broken_entries_are_dropped(gateway):
    """坏键、错的坐标形状、越界数值都不该让整份布局失败，只丢自己。"""
    with MockUpstream("siteA") as a:
        g_a = add_upstream(gateway, a, "siteA")
        add_route(gateway, "chat", g_a, "remote-chat")
        good = canvas.model_key("chat", "openai")

        resp = put_layout(
            gateway,
            {
                good: [1, 2],
                "garbage": [3, 4],                 # 前缀不认识
                "m|only-two-parts": [5, 6],        # 拆不出三段
                "m|chat|openai|extra": [7, 8],     # 多出的段会并进接口，接口就不匹配 -> 剪掉
                canvas.model_key("chat", "openai") + " ": [9, 10],
            },
        )
        assert resp.status_code == 200, resp.text
        assert set(resp.json()["nodes"]) == {good}


def test_out_of_range_and_non_finite_coordinates_are_dropped(gateway):
    """坏坐标只丢自己，不能让整份布局失败 —— 而且响应体回的是**实际存下来的那份**，
    客户端一比对就知道哪个节点没落地。

    这里和「节点数超上限」是两种不同的态度：超量是资源守卫，直接拒；单个坐标坏掉是
    一份展示状态里的一格脏数据，丢了就回到自动摆放，不该连累其它节点。
    """
    with MockUpstream("siteA") as a:
        g_a = add_upstream(gateway, a, "siteA")
        add_route(gateway, "chat", g_a, "remote-chat")
        good = canvas.model_key("chat", "openai")

        for bad in ([canvas.COORD_LIMIT * 10, 0], [0, -canvas.COORD_LIMIT * 10]):
            resp = put_layout(gateway, {good: [1, 2], "m|other|openai": bad})
            assert resp.status_code == 200, resp.text
            assert set(resp.json()["nodes"]) == {good}, "坏坐标被丢，好节点保住"


def test_non_finite_coordinates_are_dropped(gateway):
    """inf/nan 得走原始字节 —— json.dumps 按标准不让它们出场，但 Python 的 json.loads
    默认**接受**这两个字面量，所以客户端完全可能真的发过来。"""
    for literal in ("Infinity", "-Infinity", "NaN"):
        resp = gateway.put(
            "/admin/api/canvas-layout",
            content='{"nodes":{"m|chat|openai":[%s,0]}}' % literal,
            headers={"content-type": "application/json"},
        )
        assert resp.status_code == 200, f"{literal} 应该被丢掉而不是报错"
        assert resp.json()["nodes"] == {}, f"{literal} 不该落地"


def test_layout_survives_a_non_finite_value_coming_back_from_db(gateway):
    """库里被手改成 inf 时，读出来也不能带着它进前端算位置。"""
    db.set_setting(canvas.SETTING_KEY, '{"v":1,"nodes":{"m|chat|openai":[Infinity,0]}}')
    canvas.forget_cache()
    assert get_layout(gateway)["nodes"] == {}


def test_unknown_fields_are_rejected(gateway):
    """只收坐标。顺手塞路由配置进来要被挡掉，不能让它看起来存下来了。"""
    resp = gateway.put(
        "/admin/api/canvas-layout",
        json={"nodes": {}, "routes": [{"model_name": "x"}]},
    )
    assert resp.status_code == 422


def test_zoom_out_of_range_is_rejected(gateway):
    assert put_layout(gateway, {}, {"z": 0}).status_code == 422
    assert put_layout(gateway, {}, {"z": 99}).status_code == 422


def test_node_count_is_capped(gateway):
    too_many = {f"m|model{i}|openai": [i, i] for i in range(canvas.MAX_NODES + 1)}
    assert put_layout(gateway, too_many).status_code == 400


def test_broken_json_in_db_degrades_to_empty(gateway):
    """库里的布局被手改坏了，画布必须还能打开 —— 退回自动摆放，不是 500。"""
    db.set_setting(canvas.SETTING_KEY, "{ 这不是 JSON")
    canvas.forget_cache()
    assert get_layout(gateway)["nodes"] == {}

    db.set_setting(canvas.SETTING_KEY, '["数组不是布局"]')
    canvas.forget_cache()
    assert get_layout(gateway)["nodes"] == {}


def test_layout_never_touches_routing(gateway):
    """写入一份带同名模型的布局，候选和首选一个都不该动。"""
    with MockUpstream("siteA") as a, MockUpstream("siteB") as b:
        g_a = add_upstream(gateway, a, "siteA")
        g_b = add_upstream(gateway, b, "siteB")
        add_route(gateway, "chat", g_a, "remote-a")
        add_route(gateway, "chat", g_b, "remote-b")

        before = next(r for r in gateway.get("/admin/api/models").json() if r["model_name"] == "chat")
        put_layout(gateway, {canvas.model_key("chat", "openai"): [1, 1]})
        after = next(r for r in gateway.get("/admin/api/models").json() if r["model_name"] == "chat")

        assert after["preferred_route_id"] == before["preferred_route_id"]
        assert after["active_route_id"] == before["active_route_id"]
        assert [c["route_id"] for c in after["candidates"]] == [
            c["route_id"] for c in before["candidates"]
        ]
