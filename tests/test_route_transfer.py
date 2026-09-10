"""液态编排跨模型复制、移动、合并的持久化边界。"""

import pytest

from gateway import db
from helpers import add_group, add_route


def seed(client, name="source", protocol="openai"):
    site = client.post("/admin/api/upstreams", json={
        "name": name, "base_url": f"https://{name}.example.invalid",
    })
    assert site.status_code == 200
    group = add_group(client, site.json()["id"], protocol, name="主组")
    one = add_route(client, name, group, "remote-exact[1m]")
    two = add_route(client, name, group, "remote-secondary")
    return group, one, two


def transfer(client, ids, source="source", target="target", mode="copy"):
    return client.post("/admin/api/models/transfer", json={
        "source_model_name": source, "target_model_name": target,
        "route_ids": ids, "mode": mode,
    })


def routes(client, name):
    return next((m for m in client.get("/admin/api/models").json()
                 if m["model_name"] == name), None)


def test_copy_preserves_source_mapping_and_preferred(gateway):
    group, one, two = seed(gateway)
    gateway.post("/admin/api/models/switch", json={"route_id": two})
    before = routes(gateway, "source")
    response = transfer(gateway, [one, two])
    assert response.status_code == 200, response.text
    assert response.json()["added"] == 2
    assert routes(gateway, "source") == before
    target = routes(gateway, "target")
    assert [c["remote_model"] for c in target["candidates"]] == ["remote-exact[1m]", "remote-secondary"]
    assert all(c["group_id"] == group for c in target["candidates"])
    assert target["preferred_route_id"] == target["candidates"][1]["route_id"]


def test_move_repairs_source_preferred_and_can_split_last_candidate(gateway):
    _, one, two = seed(gateway)
    response = transfer(gateway, [one], mode="move")
    assert response.status_code == 200
    assert routes(gateway, "source")["preferred_route_id"] == two
    response = transfer(gateway, [two], target="new-alias", mode="move")
    assert response.status_code == 200
    assert response.json()["source_empty"] is True
    assert routes(gateway, "source") is None
    assert routes(gateway, "new-alias")["candidates"][0]["remote_model"] == "remote-secondary"


def test_merge_reuses_duplicate_and_keeps_target_preference_and_order(gateway):
    group, one, two = seed(gateway)
    target_first = add_route(gateway, "target", group, "already-first")
    duplicate = add_route(gateway, "target", group, "remote-exact[1m]")
    response = transfer(gateway, [one, two], mode="move")
    assert response.status_code == 200
    assert response.json()["merged"] == 1
    assert response.json()["added"] == 1
    target = routes(gateway, "target")
    assert [c["route_id"] for c in target["candidates"]][:2] == [target_first, duplicate]
    assert target["preferred_route_id"] == target_first
    assert routes(gateway, "source") is None


@pytest.mark.parametrize("reason", ["protocol", "missing", "stale-source", "same-model", "blank"])
def test_rejected_transfer_leaves_both_models_unchanged(gateway, reason):
    _, one, two = seed(gateway)
    seed(gateway, "target", "anthropic" if reason == "protocol" else "openai")
    before = db.list_routes()
    response = transfer(
        gateway, [one, 999999 if reason == "missing" else two],
        source="wrong-source" if reason == "stale-source" else "source",
        target="source" if reason == "same-model" else "   " if reason == "blank" else "target",
        mode="move",
    )
    assert response.status_code in (400, 409), response.text
    assert db.list_routes() == before


def test_transfer_failure_rolls_back_insertions_before_source_deletion(gateway, monkeypatch):
    _, one, two = seed(gateway)
    before = db.list_routes()
    def fail_after_writes(conn, model_name):
        raise RuntimeError("injected transaction failure")
    monkeypatch.setattr(db, "_reattach_active", fail_after_writes)
    with pytest.raises(RuntimeError, match="injected transaction failure"):
        db.transfer_model_routes("source", "target", [one, two], "move")
    assert db.list_routes() == before
