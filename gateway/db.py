from __future__ import annotations

import sqlite3
import time
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Iterable, Iterator

from . import config, naming
from .reqlog import log
from .upstream import normalize_base

# 三层：供应商（站在哪）→ 分组（用哪把 key、走哪种接口）→ 候选（哪个模型落到哪个分组）。
#
# base_url 存**站根**，不带 /v1。Anthropic 客户端要你填的地址是站根（它自己拼 /v1/messages），
# OpenAI 客户端要你填的是 …/v1，同一个站两种说法 —— 说明 /v1 是协议的一部分而不是站点的
# 一部分，所以统一存根，由 upstream.endpoint() 按协议补出完整地址。
#
# 接口（protocol）挂在**分组**上而不是供应商上：现实里同一个站的 Claude key 和 GPT key
# 是两把不同的 key，额度和能拉到的模型都不一样。「模型属于哪个接口」不再单独存一份，
# 它等于自己候选所在分组的接口 —— 代价是同一个模型名的所有候选必须同接口，见 add_model_route。

# 候选的列定义单独拎出来：建库和迁移里重建这张表都用它。以前两处各写一份，
# 迁移那份漏了 priority，启动之后到处报 no such column。
_ROUTE_COLUMNS = """
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  model_name TEXT NOT NULL,
  group_id INTEGER NOT NULL REFERENCES upstream_groups(id) ON DELETE CASCADE,
  remote_model TEXT NOT NULL,
  is_active INTEGER NOT NULL DEFAULT 0,
  priority INTEGER NOT NULL DEFAULT 0,
  UNIQUE(model_name, group_id, remote_model)
"""

_SCHEMA = f"""
CREATE TABLE IF NOT EXISTS upstreams(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT NOT NULL UNIQUE,
  base_url TEXT NOT NULL,
  enabled INTEGER NOT NULL DEFAULT 1,
  header_override TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);
CREATE TABLE IF NOT EXISTS upstream_groups(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  upstream_id INTEGER NOT NULL REFERENCES upstreams(id) ON DELETE CASCADE,
  name TEXT NOT NULL,
  protocol TEXT NOT NULL DEFAULT '',
  api_key TEXT NOT NULL DEFAULT '',
  enabled INTEGER NOT NULL DEFAULT 1,
  created_at TEXT NOT NULL DEFAULT (datetime('now','localtime')),
  UNIQUE(upstream_id, protocol, name)
);
CREATE TABLE IF NOT EXISTS model_routes({_ROUTE_COLUMNS});
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
  note TEXT NOT NULL DEFAULT '',
  attempt INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS settings(
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
);
"""

# request_log 只用于人工排查，超出这个条数就从最旧的开始丢，避免 db 无限膨胀
LOG_KEEP_ROWS = 2000

# 迁移时给每个老上游建的那个组的名字，也是新建分组时的默认组名
DEFAULT_GROUP = "默认"

# 分组走哪种接口 / 模型在哪种接口下暴露。和 protocols.Protocol.name 对齐
PROTOCOLS = ("anthropic", "openai")

class DuplicateName(Exception):
    """名称已被占用（供应商名全局唯一，分组名在「供应商 + 接口」内唯一）。"""


class DuplicateBaseUrl(Exception):
    """已经有别的供应商用了这个站根 —— 同一个站应该加分组，不是再建一个供应商。"""


class ProtocolMismatch(Exception):
    """候选和模型的接口不一致。args = (模型已在的接口, 这个分组的接口)。"""


class DuplicateRemote(Exception):
    """同一个分组下已经有一条映射到这个上游真名的候选了。args = (上游真名,)。"""


class ProtocolLocked(Exception):
    """分组下已经有候选了，不能再改它的接口。args = (候选数,)。"""


@dataclass(frozen=True, slots=True)
class Upstream:
    id: int
    name: str
    base_url: str           # 站根，不带 /v1；完整地址由 upstream.endpoint() 拼
    api_key: str            # 转发时由命中的分组填进来；列表接口里一律是空串
    enabled: bool
    header_override: str = ""


@dataclass(frozen=True, slots=True)
class Group:
    id: int
    upstream_id: int
    name: str
    protocol: str
    api_key: str
    enabled: bool


@dataclass(frozen=True, slots=True)
class Route:
    model_name: str
    upstream: Upstream
    remote_model: str
    group_id: int = 0
    group_name: str = ""
    # 候选自己的主键。一个分组下同一个模型名可以有多条（各指一个不同的上游真名），
    # 所以「哪一条」只能用它指，不能再用 group_id
    route_id: int = 0


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


def _to_upstream(row: sqlite3.Row, api_key: str = "") -> Upstream:
    return Upstream(
        id=row["id"],
        name=row["name"],
        base_url=row["base_url"],
        api_key=api_key,
        enabled=bool(row["enabled"]),
        header_override=row["header_override"],
    )


def _to_group(row: sqlite3.Row) -> Group:
    return Group(
        id=row["id"],
        upstream_id=row["upstream_id"],
        name=row["name"],
        protocol=row["protocol"],
        api_key=row["api_key"],
        enabled=bool(row["enabled"]),
    )

# ---------------------------------------------------------------- 建表与迁移


def _columns(path, table: str) -> set[str]:
    """只读地看一眼某张表有哪些列；表或库不存在就返回空集。"""
    if not path.exists():
        return set()
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        return {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
    except sqlite3.DatabaseError:
        return set()
    finally:
        conn.close()


def _is_pre_group_shape(path) -> bool:
    """最老的结构：api_key 还挂在 upstreams 行上，没有分组这一层。"""
    return "api_key" in _columns(path, "upstreams")


def _is_pre_protocol_shape(path) -> bool:
    """上一版结构：有分组了，但接口标记还挂在 upstreams.protocols 上。"""
    cols = _columns(path, "upstream_groups")
    return bool(cols) and "protocol" not in cols


def _is_pre_routeid_shape(path) -> bool:
    """候选还是复合主键 (model_name, group_id) 的结构，一个分组下塞不下第二条映射。"""
    cols = _columns(path, "model_routes")
    return bool(cols) and "id" not in cols


def _legacy_protocols(raw: str) -> tuple[str, ...]:
    """老的 'openai,anthropic' 逗号标记。openai 排在前面：迁移时第一个接口会分给
    已经存着候选的那个分组，而今天之前的候选全是 /v1/responses 那一侧的。"""
    got = {p.strip().lower() for p in (raw or "").split(",")}
    return tuple(p for p in ("openai", "anthropic") if p in got)


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
    """最老的结构 -> 分组结构。每个老上游变成一个供应商 + 一个「默认」分组。

    这些分组一律标成 openai 接口：今天之前网关只有 /v1/responses，这是事实不是猜测。
    """
    for row in conn.execute("SELECT id, api_key FROM upstreams").fetchall():
        conn.execute(
            "INSERT INTO upstream_groups(upstream_id, name, protocol, api_key) VALUES(?,?,?,?)",
            (row["id"], DEFAULT_GROUP, "openai", row["api_key"]),
        )

    # model_routes 的主键要从 upstream_id 换成 group_id，只能重建。此刻每个供应商
    # 恰好一个分组，所以下面这个 join 是一对一的。列定义共用 _ROUTE_COLUMNS，
    # 免得两处各写一份、漏字段（以前漏过 priority）
    conn.execute("ALTER TABLE model_routes RENAME TO model_routes_pre_group")
    conn.execute(f"CREATE TABLE model_routes({_ROUTE_COLUMNS})")
    conn.execute("""
        INSERT INTO model_routes(model_name, group_id, remote_model, is_active)
        SELECT o.model_name, g.id, o.remote_model, o.is_active
        FROM model_routes_pre_group o
        JOIN upstream_groups g ON g.upstream_id = o.upstream_id""")
    conn.execute("DROP TABLE model_routes_pre_group")
    conn.execute("ALTER TABLE upstreams DROP COLUMN api_key")


def _migrate_route_ids(conn: sqlite3.Connection) -> None:
    """候选加一个自增主键，唯一约束顺延到「模型 + 分组 + 上游真名」。

    为什么：主键是 (model_name, group_id) 时，一个分组下只塞得下一条映射。可现实里
    同一个站常常有好几个能用的模型 id（带日期后缀的那种尤其容易被上游下掉），
    想把它们排成一条降级链就得允许同分组多条。完全相同的映射仍然算重复。

    改主键只能重建整张表。没有别的表引用 model_routes，所以不必关外键。
    按原来的链顺序搬，新 id 就是递增的，`ORDER BY priority, id` 的兜底次序还和以前一样。
    """
    conn.execute("ALTER TABLE model_routes RENAME TO model_routes_pre_id")
    conn.execute(f"CREATE TABLE model_routes({_ROUTE_COLUMNS})")
    conn.execute("""
        INSERT INTO model_routes(model_name, group_id, remote_model, is_active, priority)
        SELECT model_name, group_id, remote_model, is_active, priority
        FROM model_routes_pre_id ORDER BY model_name, priority, group_id""")
    conn.execute("DROP TABLE model_routes_pre_id")


_GROUPS_REBUILD = """
CREATE TABLE upstream_groups_new(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  upstream_id INTEGER NOT NULL REFERENCES upstreams(id) ON DELETE CASCADE,
  name TEXT NOT NULL,
  protocol TEXT NOT NULL DEFAULT '',
  api_key TEXT NOT NULL DEFAULT '',
  enabled INTEGER NOT NULL DEFAULT 1,
  created_at TEXT NOT NULL DEFAULT (datetime('now','localtime')),
  UNIQUE(upstream_id, protocol, name)
)"""

def _migrate_group_protocols(path) -> None:
    """接口标记从供应商搬到分组上。

    UNIQUE 从 (供应商, 组名) 变成 (供应商, 接口, 组名)，索引没法 ALTER，只能重建整张表。
    而 model_routes 的外键指着它，所以必须先关 foreign_keys：开着的话 DROP TABLE 会被当成
    「先删掉所有行」，ON DELETE CASCADE 顺手就把候选清空了。id 原样搬过去，候选才还认得。

    自己开一条连接、自己管事务：`_conn()` 里 foreign_keys 是常开的，而这个 pragma
    在事务里是空操作，必须在 BEGIN 之前设。
    """
    conn = sqlite3.connect(path, isolation_level=None)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA foreign_keys=OFF")
        conn.execute("BEGIN")
        marks = {
            row["id"]: _legacy_protocols(row["protocols"]) or ("openai",)
            for row in conn.execute("SELECT id, protocols FROM upstreams")
        }
        conn.execute(_GROUPS_REBUILD)
        for row in conn.execute("SELECT * FROM upstream_groups ORDER BY id").fetchall():
            protos = marks.get(row["upstream_id"], ("openai",))
            conn.execute(
                "INSERT INTO upstream_groups_new"
                "(id, upstream_id, name, protocol, api_key, enabled, created_at)"
                " VALUES(?,?,?,?,?,?,?)",
                (row["id"], row["upstream_id"], row["name"], protos[0], row["api_key"],
                 row["enabled"], row["created_at"]),
            )
            # 站点标了两种格式：另一种接口也留一个同 key 的空分组，别把填过的信息弄丢
            for extra in protos[1:]:
                conn.execute(
                    "INSERT INTO upstream_groups_new(upstream_id, name, protocol, api_key, enabled)"
                    " VALUES(?,?,?,?,?)",
                    (row["upstream_id"], row["name"], extra, row["api_key"], row["enabled"]),
                )
        conn.execute("DROP TABLE upstream_groups")
        conn.execute("ALTER TABLE upstream_groups_new RENAME TO upstream_groups")
        conn.execute("COMMIT")
        dangling = conn.execute("PRAGMA foreign_key_check").fetchall()
        if dangling:
            log(f"WARN {len(dangling)} dangling rows after group-protocol migration")
    finally:
        conn.close()

def _drop_legacy_bits(conn: sqlite3.Connection) -> None:
    """新结构里不存在的东西：upstreams.protocols（接口挪到分组上了）和
    model_meta（模型的接口由它候选所在的分组推出来，不再单独存）。"""
    have = {r["name"] for r in conn.execute("PRAGMA table_info(upstreams)")}
    if "protocols" in have:
        conn.execute("ALTER TABLE upstreams DROP COLUMN protocols")
    conn.execute("DROP TABLE IF EXISTS model_meta")


def _normalize_base_urls(conn: sqlite3.Connection) -> None:
    """base_url 统一存站根。老库里 openai 那批填到了 /v1，剥掉之后转发地址完全等价：
    以前拼 <base>/responses，现在拼 <站根>/v1/responses。"""
    for row in conn.execute("SELECT id, base_url FROM upstreams").fetchall():
        fixed = normalize_base(row["base_url"])
        if fixed != row["base_url"]:
            conn.execute("UPDATE upstreams SET base_url=? WHERE id=?", (fixed, row["id"]))


def _backfill_log_protocol(conn: sqlite3.Connection) -> None:
    """老记录没有协议列，而它们全是 /v1/responses 打进来的。回填一下，
    「实测格式」那列才有东西可看。"""
    conn.execute("UPDATE request_log SET protocol='openai' WHERE protocol=''")


def _warn_mixed_models(conn: sqlite3.Connection) -> None:
    """一个模型名的候选应该同接口。迁移前的库没这个约束（比如把只跑过 GPT 的站
    手动标成了 Claude），出现了就记一行日志，让人知道该去改哪个。"""
    rows = conn.execute("""
        SELECT m.model_name, COUNT(DISTINCT g.protocol) AS kinds
        FROM model_routes m JOIN upstream_groups g ON g.id = m.group_id
        GROUP BY m.model_name HAVING kinds > 1""").fetchall()
    for row in rows:
        log(f"WARN model {row['model_name']!r} has candidates on more than one protocol")


def _add_missing_columns(conn: sqlite3.Connection) -> None:
    """给早期版本的库补列 —— CREATE TABLE IF NOT EXISTS 不会给已存在的表加字段。"""
    wanted = {
        "upstreams": [
            ("header_override", "TEXT NOT NULL DEFAULT ''"),
        ],
        "model_routes": [
            # 自动降级的尝试顺序：小的先试。0 = 还没排过，按 group_id 兜底
            ("priority", "INTEGER NOT NULL DEFAULT 0"),
        ],
        "request_log": [
            ("remote_model", "TEXT NOT NULL DEFAULT ''"),
            ("protocol", "TEXT NOT NULL DEFAULT ''"),
            ("group_name", "TEXT NOT NULL DEFAULT ''"),
            # 这条记录是这个请求的第几次尝试；> 1 就是被自动降级救回来的
            ("attempt", "INTEGER NOT NULL DEFAULT 1"),
        ],
    }
    for table, columns in wanted.items():
        have = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
        for column, decl in columns:
            if column not in have:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")


def init_db() -> None:
    config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    path = config.DB_PATH
    # 判断和备份都得在自己开连接之前做完
    old_v1 = _is_pre_group_shape(path)
    old_v2 = not old_v1 and _is_pre_protocol_shape(path)
    # v1 的库由 _migrate_to_groups 直接建成新形状，不用再走一遍候选主键的迁移；
    # v2 的库有分组但候选还是复合主键，两件事都要做
    old_v3 = not old_v1 and _is_pre_routeid_shape(path)
    stale = old_v1 or old_v2 or old_v3
    backup = _backup_db(path) if stale else ""
    # 重建 upstream_groups 要自己管事务和外键开关，而且得在 CREATE TABLE IF NOT EXISTS 之前
    if old_v2:
        _migrate_group_protocols(path)
    with _conn() as conn:
        # 老库里 model_routes 已经存在，IF NOT EXISTS 会跳过它，重建交给迁移
        conn.executescript(_SCHEMA)
        _add_missing_columns(conn)
        if old_v1:
            _migrate_to_groups(conn)
        elif old_v3:
            _migrate_route_ids(conn)
        if stale:
            _drop_legacy_bits(conn)
            _normalize_base_urls(conn)
            _backfill_log_protocol(conn)
            _warn_mixed_models(conn)
            # 迁移里有重建表的动作，再补一次列：漏一个字段的代价是启动之后到处报
            # 「no such column」，而这个检查是幂等的、几乎不花时间
            _add_missing_columns(conn)
    if backup:
        log(f"db migrated to latest schema, backup at data/{backup}")


# ---------------------------------------------------------------- 供应商


def list_upstreams() -> tuple[Upstream, ...]:
    with _conn() as conn:
        return tuple(_to_upstream(r) for r in conn.execute("SELECT * FROM upstreams ORDER BY id"))


def get_upstream(upstream_id: int) -> Upstream | None:
    with _conn() as conn:
        row = conn.execute("SELECT * FROM upstreams WHERE id=?", (upstream_id,)).fetchone()
    return _to_upstream(row) if row is not None else None


def _check_base_url(conn: sqlite3.Connection, base_url: str, skip_id: int | None = None) -> None:
    """一个站根只能属于一个供应商。同一个站的多把 key / 两种接口都是分组，不是新供应商。"""
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
) -> Upstream:
    """只建供应商本身。分组（key + 接口）由调用方紧接着建 —— 接口得选，猜不出来。"""
    base = normalize_base(base_url)
    with _conn() as conn:
        _check_base_url(conn, base)
        try:
            cur = conn.execute(
                "INSERT INTO upstreams(name, base_url, header_override, enabled) VALUES(?,?,?,?)",
                (name, base, header_override, int(enabled)),
            )
        except sqlite3.IntegrityError as exc:
            raise DuplicateName(name) from exc
        row = conn.execute("SELECT * FROM upstreams WHERE id=?", (int(cur.lastrowid),)).fetchone()
    return _to_upstream(row)


def update_upstream(
    upstream_id: int,
    name: str,
    base_url: str,
    enabled: bool,
    header_override: str = "",
) -> bool:
    base = normalize_base(base_url)
    with _conn() as conn:
        _check_base_url(conn, base, upstream_id)
        try:
            cur = conn.execute(
                "UPDATE upstreams SET name=?, base_url=?, enabled=?, header_override=? WHERE id=?",
                (name, base, int(enabled), header_override, upstream_id),
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


def create_group(
    upstream_id: int, name: str, protocol: str, api_key: str = "", enabled: bool = True
) -> Group:
    with _conn() as conn:
        try:
            cur = conn.execute(
                "INSERT INTO upstream_groups(upstream_id, name, protocol, api_key, enabled)"
                " VALUES(?,?,?,?,?)",
                (upstream_id, name, protocol, api_key, int(enabled)),
            )
        except sqlite3.IntegrityError as exc:
            raise DuplicateName(name) from exc
        row = conn.execute("SELECT * FROM upstream_groups WHERE id=?", (int(cur.lastrowid),)).fetchone()
    return _to_group(row)


def update_group(
    group_id: int,
    name: str,
    protocol: str,
    api_key: str,
    enabled: bool,
    upstream_id: int | None = None,
) -> bool:
    """upstream_id 传了就是把这个分组搬到另一个供应商下 —— 同一个站不小心建成了两个供应商
    之后想合起来，走这条路，候选跟着分组一起过去。

    接口一旦有候选就不给改了：候选是「这个模型在这个接口下暴露」的唯一记录，改了接口
    等于悄悄把一批模型换到另一种线格式上。"""
    with _conn() as conn:
        row = conn.execute("SELECT protocol FROM upstream_groups WHERE id=?", (group_id,)).fetchone()
        if row is None:
            return False
        if protocol != row["protocol"]:
            taken = conn.execute(
                "SELECT COUNT(*) AS n FROM model_routes WHERE group_id=?", (group_id,)
            ).fetchone()["n"]
            if taken:
                raise ProtocolLocked(taken)
        sets = ["name=?", "protocol=?", "api_key=?", "enabled=?"]
        args: list = [name, protocol, api_key, int(enabled)]
        if upstream_id is not None:
            sets.append("upstream_id=?")
            args.append(upstream_id)
        args.append(group_id)
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
        SELECT m.id AS route_id, m.model_name, m.group_id, m.remote_model, m.is_active, m.priority,
               g.name AS group_name, g.protocol, g.enabled AS group_enabled,
               u.id AS upstream_id, u.name AS upstream_name, u.enabled AS upstream_enabled
        FROM model_routes m
        JOIN upstream_groups g ON g.id = m.group_id
        JOIN upstreams u ON u.id = g.upstream_id
        ORDER BY m.model_name, m.priority, m.id
    """
    with _conn() as conn:
        return tuple(dict(r) for r in conn.execute(query))


def get_route(route_id: int) -> dict | None:
    """一条候选的全貌（连分组名和供应商名）。给管理接口写日志、报错用。"""
    query = """
        SELECT m.id AS route_id, m.model_name, m.group_id, m.remote_model, m.is_active, m.priority,
               g.name AS group_name, g.protocol, u.id AS upstream_id, u.name AS upstream_name
        FROM model_routes m
        JOIN upstream_groups g ON g.id = m.group_id
        JOIN upstreams u ON u.id = g.upstream_id
        WHERE m.id=?
    """
    with _conn() as conn:
        row = conn.execute(query, (route_id,)).fetchone()
    return dict(row) if row is not None else None


def set_route_order(model_name: str, route_ids: Iterable[int]) -> int:
    """按给定顺序重排这个模型的候选（自动降级依次尝试的顺序）。返回排到的条数。

    只认真的存在的候选，没提到的留在后面（priority 从 len(order) 起排，保持它们原来的相对次序）。
    """
    with _conn() as conn:
        have = [
            r["id"]
            for r in conn.execute(
                "SELECT id FROM model_routes WHERE model_name=? ORDER BY priority, id",
                (model_name,),
            )
        ]
        wanted = [rid for rid in route_ids if rid in have]
        rest = [rid for rid in have if rid not in wanted]
        for i, rid in enumerate(wanted + rest):
            conn.execute("UPDATE model_routes SET priority=? WHERE id=?", (i, rid))
        return len(wanted)


def _group_protocol(conn: sqlite3.Connection, group_id: int) -> str:
    row = conn.execute("SELECT protocol FROM upstream_groups WHERE id=?", (group_id,)).fetchone()
    return row["protocol"] if row is not None else ""


def _model_protocol(conn: sqlite3.Connection, model_name: str) -> str:
    """模型在哪个接口下暴露 = 它任一候选所在分组的接口（优先看活跃的那条）。
    没有候选就返回空串 —— 这个模型名还不存在。"""
    row = conn.execute(
        "SELECT g.protocol FROM model_routes m JOIN upstream_groups g ON g.id = m.group_id"
        " WHERE m.model_name=? ORDER BY m.is_active DESC, m.id LIMIT 1",
        (model_name,),
    ).fetchone()
    return row["protocol"] if row is not None else ""


def add_model_route(model_name: str, group_id: int, remote_model: str) -> int:
    """新增一个候选，返回它的 id；该模型的第一个候选自动成为活跃候选。已存在则返回 0。

    「已存在」是「同一个分组下已经有一条映射到同一个上游真名的候选」。同一个分组下
    **允许**同一个模型名的多条候选，只要各指一个不同的上游真名 —— 一个站常有好几个
    能用的模型 id，把它们排成一条链比只能挑一个有用。

    模型的接口就是候选所在分组的接口，所以同一个模型名的候选必须全在同一种接口上，
    否则「这个名字在哪个接口下暴露」就没有答案了。"""
    with _conn() as conn:
        exists = conn.execute(
            "SELECT 1 FROM model_routes WHERE model_name=? AND group_id=? AND remote_model=?",
            (model_name, group_id, remote_model),
        ).fetchone()
        if exists:
            return 0
        mine = _group_protocol(conn, group_id)
        theirs = _model_protocol(conn, model_name)
        if theirs and theirs != mine:
            raise ProtocolMismatch(theirs, mine)
        count = conn.execute(
            "SELECT COUNT(*) AS n FROM model_routes WHERE model_name=?", (model_name,)
        ).fetchone()["n"]
        # 新候选排在链尾：自动降级按 priority 从小到大试，刚加的那个不该抢到最前面去
        nxt = conn.execute(
            "SELECT COALESCE(MAX(priority), -1) + 1 AS p FROM model_routes WHERE model_name=?",
            (model_name,),
        ).fetchone()["p"]
        cur = conn.execute(
            "INSERT INTO model_routes(model_name, group_id, remote_model, is_active, priority)"
            " VALUES(?,?,?,?,?)",
            (model_name, group_id, remote_model, 1 if count == 0 else 0, nxt),
        )
    return int(cur.lastrowid)


def update_model_route(route_id: int, remote_model: str) -> bool:
    """只改「上游那边的真实模型名」。1M 开关也是它 —— 存成 `名字[1m]` 后缀。"""
    with _conn() as conn:
        try:
            cur = conn.execute(
                "UPDATE model_routes SET remote_model=? WHERE id=?", (remote_model, route_id)
            )
        except sqlite3.IntegrityError as exc:
            # 改成了同分组里另一条候选已经用着的真名，那两条就完全一样了
            raise DuplicateRemote(remote_model) from exc
        return cur.rowcount > 0


def add_routes_for_group(group_id: int, model_names: Iterable[str]) -> tuple[int, tuple[str, ...]]:
    """批量加候选，返回 (加上了几个, 跳过了哪些)。

    跳过的是「这个名字已经在另一种接口下暴露了」的：拉一个站的模型列表常常几十上百个，
    里面撞上一两个不能挂的就整批失败、一个都不落库，比跳过难用得多。重复的不算跳过
    （已经有了本来就是想要的结果）。"""
    added = 0
    skipped: list[str] = []
    for raw in model_names:
        name = raw.strip()
        if not name:
            continue
        try:
            if add_model_route(name, group_id, name):
                added += 1
        except ProtocolMismatch:
            skipped.append(name)
    return added, tuple(skipped)


def delete_model_route(route_id: int) -> str:
    """删掉一条候选，返回它的模型名（找不到返回空串）。"""
    with _conn() as conn:
        row = conn.execute("SELECT model_name FROM model_routes WHERE id=?", (route_id,)).fetchone()
        if row is None:
            return ""
        conn.execute("DELETE FROM model_routes WHERE id=?", (route_id,))
        _reattach_active(conn, row["model_name"])
        return row["model_name"]


def delete_routes_in_group(model_name: str, group_id: int) -> int:
    """把这个模型在某个分组下的候选全删掉，返回删除条数。

    分组弹窗里那个勾选框就是这个语义：它答的是「这个模型在这个分组里有没有」，
    同分组挂了好几个真名时，取消勾选自然是一起去掉。"""
    with _conn() as conn:
        removed = conn.execute(
            "DELETE FROM model_routes WHERE model_name=? AND group_id=?", (model_name, group_id)
        ).rowcount
        if removed:
            _reattach_active(conn, model_name)
        return removed


def delete_model(model_name: str) -> int:
    """删掉一个模型名下的所有候选，返回删除条数。"""
    with _conn() as conn:
        return conn.execute("DELETE FROM model_routes WHERE model_name=?", (model_name,)).rowcount


def switch_route(route_id: int) -> str:
    """把流量切到这一条候选，返回它的模型名（找不到返回空串）。"""
    with _conn() as conn:
        row = conn.execute("SELECT model_name FROM model_routes WHERE id=?", (route_id,)).fetchone()
        if row is None:
            return ""
        name = row["model_name"]
        conn.execute(
            "UPDATE model_routes SET is_active=0 WHERE model_name=? AND is_active=1", (name,)
        )
        conn.execute("UPDATE model_routes SET is_active=1 WHERE id=?", (route_id,))
    return name



def _tier_match(conn: sqlite3.Connection, model_name: str, protocol: str) -> str:
    """在**这个接口下**已录入的模型名里找同档位的那一个；不唯一就不猜，返回空串。"""
    tier = naming.tier_of(model_name)
    if not tier:
        return ""
    names = [
        r["model_name"]
        for r in conn.execute(
            "SELECT DISTINCT m.model_name FROM model_routes m"
            " JOIN upstream_groups g ON g.id = m.group_id WHERE g.protocol=?",
            (protocol,),
        )
    ]
    same = [n for n in names if naming.tier_of(n) == tier]
    if len(same) == 1:
        return same[0]
    # 同档位有多个名字时，只认字面就等于档位关键字的那个（比如直接叫 "opus"）
    exact = [n for n in same if n.lower() == tier]
    return exact[0] if len(exact) == 1 else ""

def resolve_route(model_name: str, protocol: str) -> Route | None:
    """按「模型名 + 接口」找当前生效的分组；返回的 Route 里 model_name 是实际命中的那条配置。

    接口参与匹配：模型是挂在某个接口的分组上的，拿 Anthropic 的请求体去打人家的
    /v1/responses 只会得到垃圾，所以跨接口一律当没配过。

    精确找不到时按档位关键字兜一次。Claude Code 发来的是具体 id（`claude-opus-5`、
    `claude-haiku-4-5-20251001` 之类），只有把它那几个 `ANTHROPIC_DEFAULT_*_MODEL`
    都设成你这边的档位名才会正好对上 —— 漏设一个、或者以后上游模型改名，都会直接 404。
    落到同档位的那条配置上比失败有用得多，命中时 proxy 会记一行日志。

    api_key 取自命中的**分组**，填进 Upstream.api_key，这样 proxy 那边一行都不用改。
    """
    chain = resolve_chain(model_name, protocol)
    return chain[0] if chain else None


_CHAIN_QUERY = """
    SELECT u.*, m.id AS route_id, g.id AS group_id, g.name AS group_name, g.api_key, m.remote_model
    FROM model_routes m
    JOIN upstream_groups g ON g.id = m.group_id
    JOIN upstreams u ON u.id = g.upstream_id
    WHERE m.model_name=? AND g.protocol=? AND u.enabled=1 AND g.enabled=1
    ORDER BY m.is_active DESC, m.priority, m.id
"""


def resolve_chain(model_name: str, protocol: str) -> tuple[Route, ...]:
    """这个模型在这个接口下所有能用的候选，按「先打谁」排好。

    第一个就是 resolve_route 的答案（生效的那个候选排最前），后面是自动降级的退路，
    顺序由 priority 决定（小的先试）。停用的供应商 / 分组不在里面。

    同一个分组可以出现多次（各指一个不同的上游真名）。站级失败时 proxy 会把整个分组
    跳掉，只有模型级的 404 才会去试同分组的下一条 —— 见 proxy.forward。

    档位关键字兜底和 resolve_route 是同一套：先定下实际命中的模型名，再取它的整条链。
    """
    with _conn() as conn:
        matched = model_name
        rows = conn.execute(_CHAIN_QUERY, (model_name, protocol)).fetchall()
        if not rows:
            matched = _tier_match(conn, model_name, protocol)
            rows = conn.execute(_CHAIN_QUERY, (matched, protocol)).fetchall() if matched else []
    return tuple(
        Route(
            model_name=matched,
            upstream=_to_upstream(row, api_key=row["api_key"]),
            remote_model=row["remote_model"],
            group_id=row["group_id"],
            group_name=row["group_name"],
            route_id=row["route_id"],
        )
        for row in rows
    )


def protocol_of_model(model_name: str) -> str:
    """这个模型名在哪个接口下暴露；没录入过就是空串。给 404 文案用。"""
    with _conn() as conn:
        return _model_protocol(conn, model_name)


def exposed_models(protocol: str = "") -> tuple[str, ...]:
    query = (
        "SELECT DISTINCT m.model_name FROM model_routes m"
        " JOIN upstream_groups g ON g.id = m.group_id"
    )
    args: tuple = ()
    if protocol:
        query += " WHERE g.protocol=?"
        args = (protocol,)
    with _conn() as conn:
        rows = conn.execute(query + " ORDER BY m.model_name", args).fetchall()
    return tuple(r["model_name"] for r in rows)


def _reattach_active(conn: sqlite3.Connection, model_name: str) -> None:
    """删除候选后如果没有活跃候选了，把流量落到链上第一个候选（priority 最小的那条）。"""
    still_active = conn.execute(
        "SELECT 1 FROM model_routes WHERE model_name=? AND is_active=1", (model_name,)
    ).fetchone()
    if still_active is not None:
        return
    remaining = conn.execute(
        "SELECT id FROM model_routes WHERE model_name=? ORDER BY priority, id LIMIT 1",
        (model_name,),
    ).fetchone()
    if remaining is not None:
        conn.execute("UPDATE model_routes SET is_active=1 WHERE id=?", (remaining["id"],))

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
    attempt: int = 1,
) -> None:
    with _conn() as conn:
        conn.execute(
            "INSERT INTO request_log(client, model, remote_model, protocol, upstream, group_name,"
            " status, stream, req_bytes, resp_bytes, duration_ms, input_tokens, output_tokens,"
            " cached_tokens, note, attempt) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (client, model, remote_model, protocol, upstream, group_name, status, int(stream),
             req_bytes, resp_bytes, duration_ms, input_tokens, output_tokens, cached_tokens, note,
             attempt),
        )
        conn.execute(
            "DELETE FROM request_log WHERE id <= (SELECT MAX(id) - ? FROM request_log)", (LOG_KEEP_ROWS,)
        )


def recent_requests(limit: int = 50) -> tuple[dict, ...]:
    with _conn() as conn:
        rows = conn.execute("SELECT * FROM request_log ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    return tuple(dict(r) for r in rows)


# ---------------------------------------------------------------- 设置


def get_setting(key: str, default: str = "") -> str:
    with _conn() as conn:
        row = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return row["value"] if row is not None else default


def set_setting(key: str, value: str) -> None:
    with _conn() as conn:
        conn.execute(
            "INSERT INTO settings(key, value) VALUES(?,?)"
            " ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )


def clear_request_log() -> int:
    with _conn() as conn:
        return conn.execute("DELETE FROM request_log").rowcount


def request_stats() -> dict:
    with _conn() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS n, COALESCE(SUM(input_tokens),0) AS it,"
            " COALESCE(SUM(output_tokens),0) AS ot, COALESCE(SUM(cached_tokens),0) AS ct,"
            # 第二次以上的尝试还成了，就是被自动降级救回来的一次
            " COALESCE(SUM(attempt > 1 AND status < 400),0) AS saved,"
            " COALESCE(SUM(note='failed_over'),0) AS failed_over FROM request_log"
        ).fetchone()
    return {
        "requests": row["n"],
        "input_tokens": row["it"],
        "output_tokens": row["ot"],
        "cached_tokens": row["ct"],
        "saved": row["saved"],
        "failed_over": row["failed_over"],
    }
