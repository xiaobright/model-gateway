"""编排画布的坐标记忆：纯展示状态，坏了一份也只该退回自动摆放。

这层的边界值得盯着：坐标**不能**影响路由。所以除了「存得住、读得回」，
用例重点在坏输入 —— 越界坐标、坏键、超量、不认识的字段、库里被手改坏的 JSON。
"""

from __future__ import annotations

import json

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


def test_roundtrip_keeps_positions_pan_and_zoom(gateway):
    with MockUpstream("siteA") as a:
        g_a = add_upstream(gateway, a, "siteA")
        add_route(gateway, "chat", g_a, "remote-chat")

        key = canvas.model_key("chat", "openai")
        view = {"x": -300, "y": 88.5, "z": 1.75}
        assert put_layout(gateway, {key: [120.5, -40]}, view).status_code == 200

        layout = get_layout(gateway)
        assert layout["nodes"][key] == [120.5, -40.0]
        assert layout["view"] == view


def test_coordinates_for_missing_models_are_pruned(gateway):
    """模型都删了，它的坐标就是垃圾 —— 写的时候顺手剪掉，不让它无限攒。"""
    with MockUpstream("siteA") as a:
        g_a = add_upstream(gateway, a, "siteA")
        add_route(gateway, "chat", g_a, "remote-chat")
        key = canvas.model_key("chat", "openai")
        put_layout(gateway, {key: [10, 20], key + " ": [9, 10], key + "|extra": [7, 8]})
        assert get_layout(gateway)["nodes"] == {key: [10.0, 20.0]}

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


def test_parse_drops_bad_nodes_and_keeps_valid_positions():
    """直接检验清洗，不让「模型不存在」的剪枝掩盖坐标校验失效。"""
    good = {
        "m|rounded|openai": [1.26, -2.26],
        "m|boundary|openai": [canvas.COORD_LIMIT, -canvas.COORD_LIMIT],
    }
    bad_coords = [
        [canvas.COORD_LIMIT * 10, 0], [0, -canvas.COORD_LIMIT * 10],
        [float("inf"), 0], [float("-inf"), 0], [float("nan"), 0],
        [True, 0], [0, False], ["1", 0], [None, 0], [], [1], [1, 2, 3],
    ]
    nodes = {**good, **{f"m|bad{i}|openai": xy for i, xy in enumerate(bad_coords)}}
    nodes.update({"garbage": [3, 4], "m|only-two-parts": [5, 6], "m||openai": [7, 8]})
    layout = canvas._parse(json.dumps({"nodes": nodes}))
    assert layout["nodes"] == {
        "m|rounded|openai": [1.3, -2.3],
        "m|boundary|openai": [float(canvas.COORD_LIMIT), -float(canvas.COORD_LIMIT)],
    }


def test_invalid_coordinates_are_dropped_before_storage(gateway):
    """保留接口集成；坏坐标属于真实模型，不能靠不存在的节点被剪掉而过关。"""
    with MockUpstream("siteA") as a:
        group = add_upstream(gateway, a, "siteA")
        add_route(gateway, "chat", group, "remote-chat")
        good = canvas.upstream_key(group, "remote-chat")
        for literal in ("Infinity", "-Infinity", "NaN", str(canvas.COORD_LIMIT * 10)):
            # httpx 的 JSON 编码器拒绝非有限数，原始请求仍可能带这些字面量。
            raw = '{"nodes":{"m|chat|openai":[%s,0],"%s":[1,2]}}' % (literal, good)
            resp = gateway.put("/admin/api/canvas-layout", content=raw,
                               headers={"content-type": "application/json"})
            assert resp.status_code == 200, f"{literal} 应该被丢掉而不是报错"
            assert resp.json()["nodes"] == {good: [1.0, 2.0]}
            assert get_layout(gateway)["nodes"] == resp.json()["nodes"]


def test_invalid_layout_requests_are_rejected(gateway):
    """只收坐标。顺手塞路由配置进来要被挡掉，不能让它看起来存下来了。"""
    resp = gateway.put(
        "/admin/api/canvas-layout",
        json={"nodes": {}, "routes": [{"model_name": "x"}]},
    )
    assert resp.status_code == 422
    assert put_layout(gateway, {}, {"z": 0}).status_code == 422
    assert put_layout(gateway, {}, {"z": 99}).status_code == 422


def test_node_count_is_capped(gateway):
    too_many = {f"m|model{i}|openai": [i, i] for i in range(canvas.MAX_NODES + 1)}
    assert put_layout(gateway, too_many).status_code == 400


def test_broken_layout_in_db_degrades_to_empty(gateway):
    """库里的布局被手改坏了，画布必须还能打开 —— 退回自动摆放，不是 500。"""
    for raw in ("{ 这不是 JSON", '["数组不是布局"]',
                '{"v":1,"nodes":{"m|chat|openai":[Infinity,0]}}'):
        db.set_setting(canvas.SETTING_KEY, raw)
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
