"""管理写入的实际 SQLite 事务、并发交错和失败回滚。"""
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
import asyncio
import sqlite3
import threading

import pytest

from gateway import config, db, model_batch


@pytest.fixture
def database(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "gateway.db")
    db.init_db()


def interleave(monkeypatch, read_sql, first, second):
    """暂停第一次校验后的读取，证明另一写入无法穿过校验/提交之间。"""
    checked, release, writing = (threading.Event() for _ in range(3))
    identities = {}
    original = db._conn

    @contextmanager
    def connected():
        with original() as conn:
            class Connection:
                def execute(self, sql, params=()):
                    if threading.get_ident() == identities.get("second"):
                        writing.set()
                    result = conn.execute(sql, params)
                    if threading.get_ident() == identities.get("first") and read_sql in sql:
                        checked.set()
                        assert release.wait(5), "未释放测试读取"
                    return result
            yield Connection()

    def run(name, operation):
        identities[name] = threading.get_ident()
        return operation()

    monkeypatch.setattr(db, "_conn", connected)
    with ThreadPoolExecutor(max_workers=2) as pool:
        a = pool.submit(run, "first", first)
        try:
            assert checked.wait(5), "没有到达校验点"
            b = pool.submit(run, "second", second)
            assert writing.wait(5), "第二个写入未执行"
            with pytest.raises(TimeoutError):
                b.result(timeout=0.15)
        finally:
            release.set()
        return a.result(timeout=5), b


@pytest.mark.parametrize("operation", ["create", "update"])
def test_supplier_address_check_serializes_with_competing_write(database, monkeypatch, operation):
    if operation == "update":
        original = db.create_upstream("first", "https://old.example")
        first = lambda: db.update_upstream(original.id, "first", "https://same.example", True)
    else:
        first = lambda: db.create_upstream("first", "https://same.example")
    _, second = interleave(
        monkeypatch, "SELECT name FROM upstreams WHERE lower(base_url)", first,
        lambda: db.create_upstream("second", "https://same.example/v1"),
    )
    with pytest.raises(db.DuplicateBaseUrl):
        second.result()
    assert len(db.list_upstreams()) == 1


def test_group_protocol_change_serializes_with_candidate_insert(database, monkeypatch):
    provider = db.create_upstream("site", "https://site.example")
    group = db.create_group(provider.id, "group", "openai")
    changed, inserted = interleave(
        monkeypatch, "SELECT COUNT(*) AS n FROM model_routes WHERE group_id",
        lambda: db.update_group(group.id, "group", "anthropic", "", True),
        lambda: db.add_model_route("m", group.id, "remote"),
    )
    assert changed and inserted.result()
    assert db.resolve_route("m", "anthropic") is not None
    assert db.resolve_route("m", "openai") is None
    with pytest.raises(db.ProtocolLocked):
        db.update_group(group.id, "group", "openai", "", True)


def test_batch_failure_rolls_back_catalog_candidates_and_active_selection(database):
    provider = db.create_upstream("site", "https://site.example")
    a = db.create_group(provider.id, "a", "openai")
    b = db.create_group(provider.id, "b", "openai")
    with db._conn() as conn:
        conn.execute("""CREATE TRIGGER fail_second BEFORE INSERT ON model_routes
                        WHEN NEW.remote_model='fail' BEGIN SELECT RAISE(ABORT, 'injected failure'); END""")
    with pytest.raises(sqlite3.IntegrityError, match="injected failure"):
        asyncio.run(model_batch.commit("m", [model_batch.BatchGroup(a.id, "ok"), model_batch.BatchGroup(b.id, "fail")]))
    assert not db.list_routes()
    assert not any(db.all_group_models().values())


def test_batch_is_consistent_with_concurrent_group_delete(database, monkeypatch):
    provider = db.create_upstream("site", "https://site.example")
    a = db.create_group(provider.id, "a", "openai")
    b = db.create_group(provider.id, "b", "anthropic")
    result, deleted = interleave(
        monkeypatch, "SELECT id, protocol, enabled FROM upstream_groups",
        lambda: asyncio.run(model_batch.commit("m", [model_batch.BatchGroup(a.id, "m"), model_batch.BatchGroup(b.id, "m")])),
        lambda: db.delete_group(b.id),
    )
    assert result["committed"] == 2 and result["protocols"] == ["anthropic", "openai"]
    assert deleted.result()
    retry = asyncio.run(model_batch.commit("m", [model_batch.BatchGroup(a.id, "m"), model_batch.BatchGroup(b.id, "m")]))
    assert retry["committed"] == 0
    assert retry["skipped"] == [{"group_id": a.id, "reason": "已经加过了"}, {"group_id": b.id, "reason": "分组不存在"}]
