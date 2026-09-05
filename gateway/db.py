from __future__ import annotations

import sqlite3
import time
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Iterable, Iterator

from . import config, naming
from .reqlog import log

# 供应商（upstreams）= 站在哪、怎么连；分组（upstream_groups）= 用哪把 key、能看到哪些模型。
# 现实里同一个站常常给你两把 key，各自能拉到的模型还不一样（一把专门开某个模型），
# 以前只能注册成两个「上游」，key、健康统计、标记全都要维护两份。
#
# base_url 挂在供应商上、一个供应商一个，分组不覆盖它 —— 同一个站的多套 key 就是分组的定义。
_SCHEMA = """
CREATE TABLE IF NOT EXISTS upstreams(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT NOT NULL UNIQUE,
  base_url TEXT NOT NULL,
  enabled INTEGER NOT NULL DEFAULT 1,
  header_override TEXT NOT NULL DEFAULT '',
  protocols TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);
CREATE TABLE IF NOT EXISTS upstream_groups(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  upstream_id INTEGER NOT NULL REFERENCES upstreams(id) ON DELETE CASCADE,
  name TEXT NOT NULL,
  api_key TEXT NOT NULL DEFAULT '',
  enabled INTEGER NOT NULL DEFAULT 1,
  created_at TEXT NOT NULL DEFAULT (datetime('now','localtime')),
  UNIQUE(upstream_id, name)
);
CREATE TABLE IF NOT EXISTS model_routes(
  model_name TEXT NOT NULL,
  group_id INTEGER NOT NULL REFERENCES upstream_groups(id) ON DELETE CASCADE,
  remote_model TEXT NOT NULL,
  is_active INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY(model_name, group_id)
);
CREATE TABLE IF NOT EXISTS model_meta(
  model_name TEXT PRIMARY KEY,
  side TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS request_log(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts TEXT NOT NULL DEFAULT (datetime('now','localtime')),
  client TEXT NOT NULL,
  model TEXT NOT NULL,
  remote_model TEXT NOT NULL DEFAULT '',
  protocol TEXT NOT NULL DEFAULT '',
  upstream TEXT NOT NULL,
  group_name TEXT NOT NULL DEFAULT '',
  status INTEGER NOT NULL,
  stream INTEGER NOT NULL,
  req_bytes INTEGER NOT NULL,
  resp_bytes INTEGER NOT NULL,
  duration_ms INTEGER NOT NULL,
  input_tokens INTEGER,
  output_tokens INTEGER,
  cached_tokens INTEGER,
  note TEXT NOT NULL DEFAULT ''
);
"""

# request_log 只用于人工排查，超出这个条数就从最旧的开始丢，避免 db 无限膨胀
LOG_KEEP_ROWS = 2000

# 迁移时给每个老上游建的那个组的名字，也是新建供应商时的默认组名
DEFAULT_GROUP = "默认"

# 模型属于哪一侧。内部用协议名（和 protocols.Protocol.name 对齐），界面上显示成 Claude / GPT
SIDES = ("anthropic", "openai")

class DuplicateName(Exception):
    """名称已被占用（供应商名全局唯一，分组名在供应商内唯一）。"""


class DuplicateBaseUrl(Exception):
    """已经有别的供应商用了这个 base_url —— 同一个站应该加分组，不是再建一个供应商。"""


@dataclass(frozen=True, slots=True)
class Upstream:
    id: int
    name: str
    base_url: str
    api_key: str            # 转发时由命中的分组填进来；列表接口里一律是空串
    enabled: bool
    header_override: str = ""
    protocols: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class Group:
    id: int
    upstream_id: int
    name: str
    api_key: str
    enabled: bool


@dataclass(frozen=True, slots=True)
class Route:
    model_name: str
    upstream: Upstream
    remote_model: str
    group_id: int = 0
    group_name: str = ""


@contextmanager
def _conn() -> Iterator[sqlite3.Connection]:
    """一次调用一个连接：`with conn` 负责提交/回滚，finally 负责关闭。"""
    config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(config.DB_PATH, timeout=10.0)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA journal_mode=WAL")
        with conn:
            yield conn
    finally:
        conn.close()


def parse_protocols(raw: str) -> tuple[str, ...]:
    """'openai, anthropic' -> ('anthropic', 'openai')；顺序固定、去重、乱值丢掉。"""
    got = {p.strip().lower() for p in (raw or "").split(",")}
    return tuple(p for p in SIDES if p in got)


def join_protocols(values: Iterable[str]) -> str:
    return ",".join(parse_protocols(",".join(values)))


def _to_upstream(row: sqlite3.Row, api_key: str = "") -> Upstream:
    return Upstream(
        id=row["id"],
        name=row["name"],
        base_url=row["base_url"],
        api_key=api_key,
        enabled=bool(row["enabled"]),
        header_override=row["header_override"],
        protocols=parse_protocols(row["protocols"]),
    )


def _to_group(row: sqlite3.Row) -> Group:
    return Group(
        id=row["id"],
        upstream_id=row["upstream_id"],
        name=row["name"],
        api_key=row["api_key"],
        enabled=bool(row["enabled"]),
    )

# ---------------------------------------------------------------- 建表与迁移


def _is_pre_group_shape(path) -> bool:
    """老结构的特征：api_key 还挂在 upstreams 行上。"""
    if not path.exists():
        return False
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(upstreams)")}
    except sqlite3.DatabaseError:
        return False
    finally:
        conn.close()
    return bool(cols) and "api_key" in cols


def _backup_db(path) -> str:
    """迁移前留一份。用 sqlite 自己的 backup API 而不是复制文件：
    WAL 模式下未落盘的内容还在 -wal 里，直接 cp 会拿到一个缺尾巴的库。"""
    stamp = time.strftime("%Y%m%d-%H%M%S")
    target = path.with_name(f"{path.name}.bak-{stamp}")
    src = sqlite3.connect(path)
    dst = sqlite3.connect(target)
    try:
        with dst:
            src.backup(dst)
    finally:
        dst.close()
        src.close()
    return target.name


def _migrate_to_groups(conn: sqlite3.Connection) -> None:
    """老结构 -> 供应商/分组结构。每个老上游变成一个供应商 + 一个「默认」分组。"""
    for row in conn.execute("SELECT id, api_key FROM upstreams").fetchall():
        conn.execute(
            "INSERT INTO upstream_groups(upstream_id, name, api_key) VALUES(?,?,?)",
            (row["id"], DEFAULT_GROUP, row["api_key"]),
        )

    # model_routes 的主键要从 upstream_id 换成 group_id，只能重建。此刻每个供应商
    # 恰好一个分组，所以下面这个 join 是一对一的
    conn.execute("ALTER TABLE model_routes RENAME TO model_routes_pre_group")
    conn.execute("""
        CREATE TABLE model_routes(
          model_name TEXT NOT NULL,
          group_id INTEGER NOT NULL REFERENCES upstream_groups(id) ON DELETE CASCADE,
          remote_model TEXT NOT NULL,
          is_active INTEGER NOT NULL DEFAULT 0,
          PRIMARY KEY(model_name, group_id)
        )""")
    conn.execute("""
        INSERT INTO model_routes(model_name, group_id, remote_model, is_active)
        SELECT o.model_name, g.id, o.remote_model, o.is_active
        FROM model_routes_pre_group o
        JOIN upstream_groups g ON g.upstream_id = o.upstream_id""")
    conn.execute("DROP TABLE model_routes_pre_group")

    # 今天之前网关只有 /v1/responses，所以历史上的模型和站点全是 openai 侧 —— 这是事实不是猜测。
    # Claude 侧要用哪个站得自己去勾，「实测格式」那一列可以当参考
    conn.execute("UPDATE upstreams SET protocols='openai' WHERE protocols=''")
    conn.execute(
        "INSERT OR IGNORE INTO model_meta(model_name, side)"
        " SELECT DISTINCT model_name, 'openai' FROM model_routes"
    )
    conn.execute("ALTER TABLE upstreams DROP COLUMN api_key")

def _add_missing_columns(conn: sqlite3.Connection) -> None:
    """给早期版本的库补列 —— CREATE TABLE IF NOT EXISTS 不会给已存在的表加字段。"""
    wanted = {
        "upstreams": [
            ("header_override", "TEXT NOT NULL DEFAULT ''"),
            ("protocols", "TEXT NOT NULL DEFAULT ''"),
        ],
        "request_log": [
            ("remote_model", "TEXT NOT NULL DEFAULT ''"),
            ("protocol", "TEXT NOT NULL DEFAULT ''"),
            ("group_name", "TEXT NOT NULL DEFAULT ''"),
        ],
    }
    for table, columns in wanted.items():
        have = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
        for column, decl in columns:
            if column not in have:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")


def init_db() -> None:
    config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    # 判断和备份都得在自己开连接之前做完
    old = _is_pre_group_shape(config.DB_PATH)
    backup = _backup_db(config.DB_PATH) if old else ""
    with _conn() as conn:
        # 老库里 model_routes 已经存在，IF NOT EXISTS 会跳过它，重建交给迁移
        conn.executescript(_SCHEMA)
        _add_missing_columns(conn)
        if old:
            _migrate_to_groups(conn)
    if old:
        log(f"db migrated to upstream/group schema, backup at data/{backup}")


# ---------------------------------------------------------------- 供应商


def list_upstreams() -> tuple[Upstream, ...]:
    with _conn() as conn:
        return tuple(_to_upstream(r) for r in conn.execute("SELECT * FROM upstreams ORDER BY id"))


def get_upstream(upstream_id: int) -> Upstream | None:
    with _conn() as conn:
        row = conn.execute("SELECT * FROM upstreams WHERE id=?", (upstream_id,)).fetchone()
    return _to_upstream(row) if row is not None else None


def _check_base_url(conn: sqlite3.Connection, base_url: str, skip_id: int | None = None) -> None:
    """一个 base_url 只能属于一个供应商。同一个站的多把 key 是分组，不是新供应商。"""
    row = conn.execute(
        "SELECT name FROM upstreams WHERE lower(base_url)=? AND id IS NOT ?",
        (base_url.lower(), skip_id),
    ).fetchone()
    if row is not None:
        raise DuplicateBaseUrl(row["name"])

def create_upstream(
    name: str,
    base_url: str,
    header_override: str = "",
    enabled: bool = True,
    protocols: Iterable[str] = (),
    api_key: str = "",
) -> Upstream:
    base = base_url.rstrip("/")
    with _conn() as conn:
        _check_base_url(conn, base)
        try:
            cur = conn.execute(
                "INSERT INTO upstreams(name, base_url, header_override, enabled, protocols)"
                " VALUES(?,?,?,?,?)",
                (name, base, header_override, int(enabled), join_protocols(protocols)),
            )
        except sqlite3.IntegrityError as exc:
            raise DuplicateName(name) from exc
        upstream_id = int(cur.lastrowid)
        # 没有分组的供应商用不了，所以建的时候顺手带一个默认组（key 就落在它上面）
        conn.execute(
            "INSERT INTO upstream_groups(upstream_id, name, api_key) VALUES(?,?,?)",
            (upstream_id, DEFAULT_GROUP, api_key),
        )
        row = conn.execute("SELECT * FROM upstreams WHERE id=?", (upstream_id,)).fetchone()
    return _to_upstream(row)


def update_upstream(
    upstream_id: int,
    name: str,
    base_url: str,
    enabled: bool,
    header_override: str = "",
    protocols: Iterable[str] = (),
) -> bool:
    base = base_url.rstrip("/")
    with _conn() as conn:
        _check_base_url(conn, base, upstream_id)
        try:
            cur = conn.execute(
                "UPDATE upstreams SET name=?, base_url=?, enabled=?, header_override=?, protocols=?"
                " WHERE id=?",
                (name, base, int(enabled), header_override, join_protocols(protocols), upstream_id),
            )
        except sqlite3.IntegrityError as exc:
            raise DuplicateName(name) from exc
        return cur.rowcount > 0


def delete_upstream(upstream_id: int) -> bool:
    """分组和候选靠 ON DELETE CASCADE 一起走，这里只要善后「谁没有活跃候选了」。"""
    with _conn() as conn:
        orphans = conn.execute(
            "SELECT DISTINCT m.model_name FROM model_routes m"
            " JOIN upstream_groups g ON g.id = m.group_id WHERE g.upstream_id=?",
            (upstream_id,),
        ).fetchall()
        cur = conn.execute("DELETE FROM upstreams WHERE id=?", (upstream_id,))
        for row in orphans:
            _reattach_active(conn, row["model_name"])
        return cur.rowcount > 0

# ---------------------------------------------------------------- 分组


def list_groups(upstream_id: int | None = None) -> tuple[Group, ...]:
    query = "SELECT * FROM upstream_groups"
    args: tuple = ()
    if upstream_id is not None:
        query += " WHERE upstream_id=?"
        args = (upstream_id,)
    with _conn() as conn:
        return tuple(_to_group(r) for r in conn.execute(query + " ORDER BY upstream_id, id", args))


def get_group(group_id: int) -> Group | None:
    with _conn() as conn:
        row = conn.execute("SELECT * FROM upstream_groups WHERE id=?", (group_id,)).fetchone()
    return _to_group(row) if row is not None else None


def create_group(upstream_id: int, name: str, api_key: str = "", enabled: bool = True) -> Group:
    with _conn() as conn:
        try:
            cur = conn.execute(
                "INSERT INTO upstream_groups(upstream_id, name, api_key, enabled) VALUES(?,?,?,?)",
                (upstream_id, name, api_key, int(enabled)),
            )
        except sqlite3.IntegrityError as exc:
            raise DuplicateName(name) from exc
        row = conn.execute("SELECT * FROM upstream_groups WHERE id=?", (int(cur.lastrowid),)).fetchone()
    return _to_group(row)


def update_group(
    group_id: int, name: str, api_key: str, enabled: bool, upstream_id: int | None = None
) -> bool:
    """upstream_id 传了就是把这个分组搬到另一个供应商下 —— 一开始建成了两个供应商
    （同一个站两把 key）之后想合起来，走这条路，候选跟着分组一起过去。"""
    sets = ["name=?", "api_key=?", "enabled=?"]
    args: list = [name, api_key, int(enabled)]
    if upstream_id is not None:
        sets.append("upstream_id=?")
        args.append(upstream_id)
    args.append(group_id)
    with _conn() as conn:
        try:
            cur = conn.execute(f"UPDATE upstream_groups SET {', '.join(sets)} WHERE id=?", args)
        except sqlite3.IntegrityError as exc:
            raise DuplicateName(name) from exc
        return cur.rowcount > 0


def delete_group(group_id: int) -> bool:
    with _conn() as conn:
        orphans = conn.execute(
            "SELECT DISTINCT model_name FROM model_routes WHERE group_id=?", (group_id,)
        ).fetchall()
        cur = conn.execute("DELETE FROM upstream_groups WHERE id=?", (group_id,))
        if cur.rowcount == 0:
            return False
        for row in orphans:
            _reattach_active(conn, row["model_name"])
        return True

# ---------------------------------------------------------------- 模型候选


def list_routes() -> tuple[dict, ...]:
    query = """
        SELECT m.model_name, m.group_id, m.remote_model, m.is_active,
               g.name AS group_name, g.enabled AS group_enabled,
               u.id AS upstream_id, u.name AS upstream_name,
               u.enabled AS upstream_enabled, u.protocols,
               COALESCE(t.side, '') AS side
        FROM model_routes m
        JOIN upstream_groups g ON g.id = m.group_id
        JOIN upstreams u ON u.id = g.upstream_id
        LEFT JOIN model_meta t ON t.model_name = m.model_name
        ORDER BY m.model_name, u.name, g.name
    """
    with _conn() as conn:
        return tuple(dict(r) for r in conn.execute(query))


def _set_side(conn: sqlite3.Connection, model_name: str, side: str) -> None:
    if side in SIDES:
        conn.execute(
            "INSERT INTO model_meta(model_name, side) VALUES(?,?)"
            " ON CONFLICT(model_name) DO UPDATE SET side=excluded.side",
            (model_name, side),
        )
    else:
        conn.execute("INSERT OR IGNORE INTO model_meta(model_name, side) VALUES(?, '')", (model_name,))


def add_model_route(model_name: str, group_id: int, remote_model: str, side: str = "") -> bool:
    """新增一个候选；该模型的第一个候选自动成为活跃候选。已存在则返回 False。"""
    with _conn() as conn:
        exists = conn.execute(
            "SELECT 1 FROM model_routes WHERE model_name=? AND group_id=?", (model_name, group_id)
        ).fetchone()
        if exists:
            return False
        count = conn.execute(
            "SELECT COUNT(*) AS n FROM model_routes WHERE model_name=?", (model_name,)
        ).fetchone()["n"]
        conn.execute(
            "INSERT INTO model_routes(model_name, group_id, remote_model, is_active) VALUES(?,?,?,?)",
            (model_name, group_id, remote_model, 1 if count == 0 else 0),
        )
        _set_side(conn, model_name, side)
    return True


def add_routes_for_group(group_id: int, model_names: Iterable[str], side: str = "") -> int:
    added = 0
    for raw in model_names:
        name = raw.strip()
        if name and add_model_route(name, group_id, name, side):
            added += 1
    return added

def delete_model_route(model_name: str, group_id: int) -> bool:
    with _conn() as conn:
        cur = conn.execute(
            "DELETE FROM model_routes WHERE model_name=? AND group_id=?", (model_name, group_id)
        )
        if cur.rowcount == 0:
            return False
        _reattach_active(conn, model_name)
        _drop_meta_if_orphan(conn, model_name)
        return True


def delete_model(model_name: str) -> int:
    """删掉一个模型名下的所有候选，返回删除条数。"""
    with _conn() as conn:
        removed = conn.execute("DELETE FROM model_routes WHERE model_name=?", (model_name,)).rowcount
        if removed:
            conn.execute("DELETE FROM model_meta WHERE model_name=?", (model_name,))
        return removed


def switch_route(model_name: str, group_id: int) -> bool:
    with _conn() as conn:
        target = conn.execute(
            "SELECT 1 FROM model_routes WHERE model_name=? AND group_id=?", (model_name, group_id)
        ).fetchone()
        if target is None:
            return False
        conn.execute("UPDATE model_routes SET is_active=0 WHERE model_name=? AND is_active=1", (model_name,))
        conn.execute(
            "UPDATE model_routes SET is_active=1 WHERE model_name=? AND group_id=?",
            (model_name, group_id),
        )
    return True


_ACTIVE_QUERY = """
    SELECT u.*, g.id AS group_id, g.name AS group_name, g.api_key, m.remote_model
    FROM model_routes m
    JOIN upstream_groups g ON g.id = m.group_id
    JOIN upstreams u ON u.id = g.upstream_id
    WHERE m.model_name=? AND m.is_active=1 AND u.enabled=1 AND g.enabled=1
"""


def _active_row(conn: sqlite3.Connection, model_name: str) -> sqlite3.Row | None:
    return conn.execute(_ACTIVE_QUERY, (model_name,)).fetchone()


def _tier_match(conn: sqlite3.Connection, model_name: str) -> str:
    """在已录入的模型名里找同档位的那一个；不唯一就不猜，返回空串。"""
    tier = naming.tier_of(model_name)
    if not tier:
        return ""
    names = [r["model_name"] for r in conn.execute("SELECT DISTINCT model_name FROM model_routes")]
    same = [n for n in names if naming.tier_of(n) == tier]
    if len(same) == 1:
        return same[0]
    # 同档位有多个名字时，只认字面就等于档位关键字的那个（比如直接叫 "opus"）
    exact = [n for n in same if n.lower() == tier]
    return exact[0] if len(exact) == 1 else ""

def resolve_route(model_name: str) -> Route | None:
    """按模型名找当前生效的分组；返回的 Route 里 model_name 是实际命中的那条配置。

    精确找不到时按档位关键字兜一次。Claude Code 发来的是具体 id（`claude-opus-5`、
    `claude-haiku-4-5-20251001` 之类），只有把它那几个 `ANTHROPIC_DEFAULT_*_MODEL`
    都设成你这边的档位名才会正好对上 —— 漏设一个、或者以后上游模型改名，都会直接 404。
    落到同档位的那条配置上比失败有用得多，命中时 proxy 会记一行日志。

    api_key 取自命中的**分组**，填进 Upstream.api_key，这样 proxy 那边一行都不用改。
    """
    with _conn() as conn:
        matched = model_name
        row = _active_row(conn, model_name)
        if row is None:
            matched = _tier_match(conn, model_name)
            row = _active_row(conn, matched) if matched else None
    if row is None:
        return None
    return Route(
        model_name=matched,
        upstream=_to_upstream(row, api_key=row["api_key"]),
        remote_model=row["remote_model"],
        group_id=row["group_id"],
        group_name=row["group_name"],
    )


def exposed_models() -> tuple[str, ...]:
    with _conn() as conn:
        rows = conn.execute("SELECT DISTINCT model_name FROM model_routes ORDER BY model_name").fetchall()
    return tuple(r["model_name"] for r in rows)


def _reattach_active(conn: sqlite3.Connection, model_name: str) -> None:
    """删除候选后如果没有活跃候选了，把流量落到剩下的第一个候选上。"""
    still_active = conn.execute(
        "SELECT 1 FROM model_routes WHERE model_name=? AND is_active=1", (model_name,)
    ).fetchone()
    if still_active is not None:
        return
    remaining = conn.execute(
        "SELECT group_id FROM model_routes WHERE model_name=? ORDER BY group_id LIMIT 1",
        (model_name,),
    ).fetchone()
    if remaining is not None:
        conn.execute(
            "UPDATE model_routes SET is_active=1 WHERE model_name=? AND group_id=?",
            (model_name, remaining["group_id"]),
        )


def _drop_meta_if_orphan(conn: sqlite3.Connection, model_name: str) -> None:
    """最后一个候选也没了，这个模型名就不存在了，别把 side 留成孤儿。"""
    left = conn.execute(
        "SELECT 1 FROM model_routes WHERE model_name=? LIMIT 1", (model_name,)
    ).fetchone()
    if left is None:
        conn.execute("DELETE FROM model_meta WHERE model_name=?", (model_name,))

# ---------------------------------------------------------------- 转发记录


def insert_request(
    client: str,
    model: str,
    upstream: str,
    status: int,
    stream: bool,
    req_bytes: int,
    resp_bytes: int,
    duration_ms: int,
    input_tokens: int | None,
    output_tokens: int | None,
    cached_tokens: int | None,
    note: str,
    remote_model: str = "",
    protocol: str = "",
    group_name: str = "",
) -> None:
    with _conn() as conn:
        conn.execute(
            "INSERT INTO request_log(client, model, remote_model, protocol, upstream, group_name,"
            " status, stream, req_bytes, resp_bytes, duration_ms, input_tokens, output_tokens,"
            " cached_tokens, note) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (client, model, remote_model, protocol, upstream, group_name, status, int(stream),
             req_bytes, resp_bytes, duration_ms, input_tokens, output_tokens, cached_tokens, note),
        )
        conn.execute(
            "DELETE FROM request_log WHERE id <= (SELECT MAX(id) - ? FROM request_log)", (LOG_KEEP_ROWS,)
        )


def recent_requests(limit: int = 50) -> tuple[dict, ...]:
    with _conn() as conn:
        rows = conn.execute("SELECT * FROM request_log ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    return tuple(dict(r) for r in rows)


def clear_request_log() -> int:
    with _conn() as conn:
        return conn.execute("DELETE FROM request_log").rowcount


def request_stats() -> dict:
    with _conn() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS n, COALESCE(SUM(input_tokens),0) AS it,"
            " COALESCE(SUM(output_tokens),0) AS ot, COALESCE(SUM(cached_tokens),0) AS ct FROM request_log"
        ).fetchone()
    return {"requests": row["n"], "input_tokens": row["it"], "output_tokens": row["ot"], "cached_tokens": row["ct"]}
