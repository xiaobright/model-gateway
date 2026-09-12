from __future__ import annotations

import contextlib
import json
import sqlite3
import threading
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
  egress TEXT NOT NULL DEFAULT '',
  retry_rules TEXT NOT NULL DEFAULT '',
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
-- 上游模型目录：这个分组能看到、能调到的上游真名。它和「下游暴露」（model_routes）
-- 是两件事 —— 拉一份模型列表只是登记这个站有什么，不代表要对外暴露；删掉一条下游映射
-- 也不该把「这个站有这个模型」这件事忘掉。
CREATE TABLE IF NOT EXISTS group_models(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  group_id INTEGER NOT NULL REFERENCES upstream_groups(id) ON DELETE CASCADE,
  remote_model TEXT NOT NULL,
  UNIQUE(group_id, remote_model)
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
  resp_text_bytes INTEGER NOT NULL DEFAULT 0,
  thinking INTEGER NOT NULL DEFAULT 0,
  duration_ms INTEGER NOT NULL,
  input_tokens INTEGER,
  output_tokens INTEGER,
  cached_tokens INTEGER,
  cache_creation_tokens INTEGER,
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

# 库结构的版本号（存在 sqlite 的 user_version 里）。每次改结构 +1，并在 _upgrade 里
# 补一条对应 stage 的动作。最新库只额外核对曾被漏迁移的缓存创建列，
# 不用每次把每张表的 table_info 翻一遍。
#
#   0 = 还没打过号（最早那一代，得按形状认）
#   1 = api_key 还在供应商行上，没有分组
#   2 = 有分组，但接口标记还挂在供应商上，候选还是 (模型, 分组) 复合主键
#   3 = 接口在分组上，候选有自增 id
#   4 = request_log 记录 Anthropic 的缓存创建 token
#   5 = 上游模型目录（group_models）与下游候选分开
#   6 = 上游同站重试规则（retry_rules）
SCHEMA_VERSION = 6

# 迁移前留几份备份。迁移是一次性的，但 .bak 从来没人清理过，所以这里顺手裁掉旧的
BACKUP_KEEP = 3

# 迁移时给每个老上游建的那个组的名字，也是新建分组时的默认组名
DEFAULT_GROUP = "默认"

class DuplicateName(Exception):
    """名称已被占用（供应商名全局唯一，分组名在「供应商 + 接口」内唯一）。"""


class DuplicateBaseUrl(Exception):
    """已经有别的供应商用了这个站根 —— 同一个站应该加分组，不是再建一个供应商。"""


class DuplicateRemote(Exception):
    """同一个分组下已经有一条映射到这个上游真名的候选了。args = (上游真名,)。"""


class RouteTransferConflict(Exception):
    """拖拽开始后来源候选发生变化，不能继续按旧快照移动。"""


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
    # 从哪扇门出去：'' 跟随系统代理（今天的默认）/ 'direct' 直连 / 一个代理 URL
    egress: str = ""
    # 同站重试：JSON 数组 [{"status":400,"times":2,"delay_ms":0}, ...]。
    # 空串 = 不配。规则绑供应商，优先于自动降级换站。
    retry_rules: str = ""


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
        egress=row["egress"],
        retry_rules=row["retry_rules"] if "retry_rules" in row.keys() else "",
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
    _prune_backups(path)
    return target.name


def _prune_backups(path) -> None:
    """迁移备份只留最近几份。

    迁移是一次性的，而这些 .bak 从来没人清理过 —— 攒下去它就和「不轮转的日志」一样，
    变成 data 目录里一堆没人看、也没人敢删的东西。留三份足够回退。
    """
    backups = sorted(
        path.parent.glob(f"{path.name}.bak-*"), key=lambda f: f.stat().st_mtime, reverse=True
    )
    for stale in backups[BACKUP_KEEP:]:
        with contextlib.suppress(OSError):
            stale.unlink()


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


def _migrate_group_models(conn: sqlite3.Connection) -> None:
    """把已有的下游候选回填成上游模型目录：每条候选的 (分组, 上游真名) 就是那个分组
    已经登记过的一个上游模型。老库没有这一层，但「这个站有这个模型」这件事本来就藏在
    候选里，回填之后目录是完整的，不会因为「还没暴露」而显示成空。"""
    conn.execute("""
        INSERT OR IGNORE INTO group_models(group_id, remote_model)
        SELECT DISTINCT group_id, remote_model FROM model_routes""")


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
    """给**正在被迁移的**库补列 —— CREATE TABLE IF NOT EXISTS 不会给已存在的表加字段。

    只在迁移那条路上调：最新库在版本检测后放行，不必每次启动都把
    每张表的 table_info 翻一遍。重建表的迁移动作跑完还会再补一次（漏一个字段的代价
    是启动之后到处报 no such column，而这个检查是幂等的）。
    """
    wanted = {
        "upstreams": [
            ("header_override", "TEXT NOT NULL DEFAULT ''"),
            # 这个站从哪扇门出去：'' 跟随系统代理 / 'direct' 直连 / 一个代理 URL。
            # 现实里同一台机器上「有的站必须走代理、有的站必须别走代理」是常态 ——
            # 公益站按 IP 屏蔽，校园网 IP 和机房 IP 各自被不同的站拉黑
            ("egress", "TEXT NOT NULL DEFAULT ''"),
            # 同站重试规则（JSON）。空串 = 不配；见 failover.parse_retry_rules
            ("retry_rules", "TEXT NOT NULL DEFAULT ''"),
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
            # 整条响应里有多少字节是**内容**（SSE 帧不算），以及里面有没有思维链。
            # 「多少字节摊一个 token」这把标尺只认没有思维链的记录：思维链发来的是
            # 总结、计费按完整的算，那种记录的字节数和 token 数不是一回事
            ("resp_text_bytes", "INTEGER NOT NULL DEFAULT 0"),
            ("thinking", "INTEGER NOT NULL DEFAULT 0"),
            # Anthropic 的 input_tokens 不含 cache_read / cache_creation，统计时需要保留
            # 这两个分量，才能按协议算出真实上下文和缓存命中率。
            ("cache_creation_tokens", "INTEGER"),
        ],
    }
    for table, columns in wanted.items():
        have = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
        for column, decl in columns:
            if column not in have:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")


def _schema_version(path) -> int:
    """库自己记的版本号。0 = 从没打过号，也就是给版本号之前那一代的老库。"""
    if not path.exists():
        return 0
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        return int(conn.execute("PRAGMA user_version").fetchone()[0])
    except sqlite3.DatabaseError:
        return 0
    finally:
        conn.close()


def _stamp(path, version: int) -> None:
    with _conn() as conn:
        # PRAGMA 不接受参数绑定；值是代码里的常量，不是外部输入
        conn.execute(f"PRAGMA user_version={version}")


def _schema_stage(path) -> int:
    """这个库停在哪一代。

    没打过号的库按形状认。v4 还要核对缓存创建列：旧版曾把没补列的 v3 库
    直接盖上 v4 的号，这种库也要能在下次启动时补迁移。
    """
    version = _schema_version(path)
    if version not in (0, SCHEMA_VERSION):
        return version
    if not version:
        if _is_pre_group_shape(path):
            return 0
        if _is_pre_protocol_shape(path):
            return 1
        if _is_pre_routeid_shape(path):
            return 2
    if "cache_creation_tokens" not in _columns(path, "request_log"):
        return 3
    return SCHEMA_VERSION


def _upgrade(path, stage: int) -> None:
    """把库升到当前版本。备份和形状检测都只在这一条路上发生。"""
    backup = _backup_db(path)
    # 重建 upstream_groups 要自己管事务和外键开关，得单独跑在别的写操作之前
    if stage == 1:
        _migrate_group_protocols(path)
    with _conn() as conn:
        # 补列必须在**重建表之前**：旧版 _migrate_route_ids 是 INSERT ... SELECT，
        # 要从老表上读 priority，而老库根本没这一列 —— 先补上才不会报 no such column
        _add_missing_columns(conn)
        if stage == 0:
            # 最老那一代一步到位：每个供应商变成一个分组，候选直接建成最新形状
            _migrate_to_groups(conn)
        elif stage in (1, 2):
            # v1/v2 还是 (model_name, group_id) 复合主键，v3 才引入候选自增 id
            _migrate_route_ids(conn)
        # stage 3 -> 4 只有 request_log 补列，不重建 model_routes，避免改变候选 id。
        _drop_legacy_bits(conn)
        _normalize_base_urls(conn)
        _backfill_log_protocol(conn)
        _warn_mixed_models(conn)
        # stage 4 -> 5：上游模型目录从现有候选回填；目录表由 init_db 的建表脚本先建好
        _migrate_group_models(conn)
        # 重建过表之后再补一次：DROP COLUMN 之类可能把刚补上的列又弄丢，
        # 漏一个的代价是启动之后到处报 no such column。这个检查是幂等的
        _add_missing_columns(conn)
        # 版本号和迁移在同一个事务里：迁移没提交，号也不会打上，下次启动会重跑
        conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
    log(f"db migrated to schema v{SCHEMA_VERSION}, backup at data/{backup}")


def init_db() -> None:
    config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    path = config.DB_PATH
    version = _schema_version(path)
    if version > SCHEMA_VERSION:
        # 比程序新的库不能碰：旧代码会把 user_version 往回盖，之后按旧结构访问
        raise RuntimeError(
            f"数据库 schema 是 v{version}，比本程序支持的 v{SCHEMA_VERSION} 新；"
            "请先升级程序，别用旧版本打开它"
        )
    with _conn() as conn:
        # 新库建表；老库里已存在的表 IF NOT EXISTS 会跳过，重建交给迁移
        conn.executescript(_SCHEMA)

    stage = _schema_stage(path)
    if stage >= SCHEMA_VERSION:
        # 新库、或者形状已经最新但还没打过号的老库：补上号，以后就不用再按形状认了
        if version != SCHEMA_VERSION:
            _stamp(path, SCHEMA_VERSION)
        return
    _upgrade(path, stage)


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
    egress: str = "",
    retry_rules: str = "",
) -> Upstream:
    """只建供应商本身。分组（key + 接口）由调用方紧接着建 —— 接口得选，猜不出来。"""
    base = normalize_base(base_url)
    with _conn() as conn:
        _check_base_url(conn, base)
        try:
            cur = conn.execute(
                "INSERT INTO upstreams(name, base_url, header_override, enabled, egress, retry_rules)"
                " VALUES(?,?,?,?,?,?)",
                (name, base, header_override, int(enabled), egress, retry_rules),
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
    egress: str = "",
    retry_rules: str = "",
) -> bool:
    base = normalize_base(base_url)
    with _conn() as conn:
        _check_base_url(conn, base, upstream_id)
        try:
            cur = conn.execute(
                "UPDATE upstreams SET name=?, base_url=?, enabled=?, header_override=?,"
                " egress=?, retry_rules=? WHERE id=?",
                (name, base, int(enabled), header_override, egress, retry_rules, upstream_id),
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

# ---------------------------------------------------------------- 上游模型目录


def list_group_models(group_id: int) -> tuple[str, ...]:
    """这个分组登记过的上游真名，按登记顺序。"""
    with _conn() as conn:
        rows = conn.execute(
            "SELECT remote_model FROM group_models WHERE group_id=? ORDER BY id", (group_id,)
        ).fetchall()
    return tuple(r["remote_model"] for r in rows)


def all_group_models() -> dict[int, tuple[str, ...]]:
    """一次拿全：分组 id -> 上游真名列表。列表接口一次序列化整个上游树时用。"""
    out: dict[int, list[str]] = {}
    with _conn() as conn:
        for row in conn.execute(
            "SELECT group_id, remote_model FROM group_models ORDER BY group_id, id"
        ):
            out.setdefault(row["group_id"], []).append(row["remote_model"])
    return {gid: tuple(names) for gid, names in out.items()}


def add_group_models(group_id: int, remote_models: Iterable[str]) -> int:
    """登记上游模型。重复的忽略，返回真正加上的条数。

    只写目录，不碰下游候选 —— 拉一份模型列表是「记下这个站有什么」，
    要不要对外暴露是另一件事。"""
    added = 0
    with _conn() as conn:
        for raw in remote_models:
            name = (raw or "").strip()
            if not name:
                continue
            cur = conn.execute(
                "INSERT OR IGNORE INTO group_models(group_id, remote_model) VALUES(?,?)",
                (group_id, name),
            )
            added += cur.rowcount
    return added


def delete_group_model(group_id: int, remote_model: str) -> tuple[int, int]:
    """从目录里去掉一个上游模型。指向它的下游映射会一起下线（它们已经无处可去），
    返回 (删掉的目录条数, 连带删掉的候选条数)。"""
    with _conn() as conn:
        removed = conn.execute(
            "DELETE FROM group_models WHERE group_id=? AND remote_model=?",
            (group_id, remote_model),
        ).rowcount
        if not removed:
            return 0, 0
        affected = [
            row["model_name"]
            for row in conn.execute(
                "SELECT DISTINCT model_name FROM model_routes WHERE group_id=? AND remote_model=?",
                (group_id, remote_model),
            )
        ]
        routes = conn.execute(
            "DELETE FROM model_routes WHERE group_id=? AND remote_model=?",
            (group_id, remote_model),
        ).rowcount
        for name in affected:
            _reattach_active(conn, name)
        return removed, routes


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
    顺序是链内的：以给出的第一个候选所在的接口为准，别的接口那条链的 priority 不动。
    """
    ids = list(dict.fromkeys(route_ids))
    if not ids:
        return 0
    with _conn() as conn:
        first = conn.execute(
            "SELECT g.protocol AS protocol FROM model_routes m"
            " JOIN upstream_groups g ON g.id = m.group_id WHERE m.id=?",
            (ids[0],),
        ).fetchone()
        if first is None:
            return 0
        have = [
            r["id"]
            for r in conn.execute(
                "SELECT m.id FROM model_routes m JOIN upstream_groups g ON g.id = m.group_id"
                " WHERE m.model_name=? AND g.protocol=? ORDER BY m.priority, m.id",
                (model_name, first["protocol"]),
            )
        ]
        wanted = [rid for rid in ids if rid in have]
        rest = [rid for rid in have if rid not in wanted]
        for i, rid in enumerate(wanted + rest):
            conn.execute("UPDATE model_routes SET priority=? WHERE id=?", (i, rid))
        return len(wanted)


def _group_protocol(conn: sqlite3.Connection, group_id: int) -> str:
    row = conn.execute("SELECT protocol FROM upstream_groups WHERE id=?", (group_id,)).fetchone()
    return row["protocol"] if row is not None else ""


def _model_protocols(conn: sqlite3.Connection, model_name: str) -> tuple[str, ...]:
    """模型在哪些接口下暴露 = 它各候选所在分组的接口集合。空 = 这个模型名还不存在。

    同一个模型名允许在多种接口下各挂一条链（一个站同时暴露 Responses 和 Chat
    Completions 很常见）：转发按「模型名 + 请求接口」选链（_CHAIN_QUERY 过滤协议），
    两条链互不可见，跨接口调用照旧 404。"""
    rows = conn.execute(
        "SELECT DISTINCT g.protocol FROM model_routes m JOIN upstream_groups g ON g.id = m.group_id"
        " WHERE m.model_name=?",
        (model_name,),
    ).fetchall()
    return tuple(r["protocol"] for r in rows)


def add_model_route(model_name: str, group_id: int, remote_model: str) -> int:
    """新增一个候选，返回它的 id；该链的第一个候选自动成为活跃候选。已存在则返回 0。

    「已存在」是「同一个分组下已经有一条映射到同一个上游真名的候选」。同一个分组下
    **允许**同一个模型名的多条候选，只要各指一个不同的上游真名 —— 一个站常有好几个
    能用的模型 id，把它们排成一条链比只能挑一个有用。

    活跃位、顺序都是**链内**（模型名 + 接口）的概念：同一模型名可以在另一种接口下
    另有一条独立链，两边的首选和优先级互不影响。"""
    with _conn() as conn:
        # 先查后写要串行：并发双击新增同一条候选时，UNIQUE 约束会以 IntegrityError 冒到 500
        conn.execute("BEGIN IMMEDIATE")
        exists = conn.execute(
            "SELECT 1 FROM model_routes WHERE model_name=? AND group_id=? AND remote_model=?",
            (model_name, group_id, remote_model),
        ).fetchone()
        if exists:
            return 0
        mine = _group_protocol(conn, group_id)
        count = conn.execute(
            "SELECT COUNT(*) AS n FROM model_routes m JOIN upstream_groups g ON g.id = m.group_id"
            " WHERE m.model_name=? AND g.protocol=?",
            (model_name, mine),
        ).fetchone()["n"]
        # 新候选排在链尾：自动降级按 priority 从小到大试，刚加的那个不该抢到最前面去
        nxt = conn.execute(
            "SELECT COALESCE(MAX(m.priority), -1) + 1 AS p FROM model_routes m"
            " JOIN upstream_groups g ON g.id = m.group_id"
            " WHERE m.model_name=? AND g.protocol=?",
            (model_name, mine),
        ).fetchone()["p"]
        # 暴露一个下游模型时顺手把上游真名登记进目录：它必然是这个站的一个上游模型，
        # 不登记的话上游站点那列会漏掉它
        conn.execute(
            "INSERT OR IGNORE INTO group_models(group_id, remote_model) VALUES(?,?)",
            (group_id, remote_model),
        )
        cur = conn.execute(
            "INSERT INTO model_routes(model_name, group_id, remote_model, is_active, priority)"
            " VALUES(?,?,?,?,?)",
            (model_name, group_id, remote_model, 1 if count == 0 else 0, nxt),
        )
    return int(cur.lastrowid)


def transfer_model_routes(
    source_model_name: str, target_model_name: str, route_ids: Iterable[int], mode: str = "copy"
) -> dict:
    """复制、移动或合并候选；目标写入和来源移除在同一个事务中完成。

    分组、上游真名及 1M 后缀原样保留。已有目标的首选与顺序不变，重复映射合并；
    新模型继承所选候选中的首选。来源名称也参与校验，拒绝迟到的拖拽快照。
    """
    ids = tuple(dict.fromkeys(route_ids))
    source = source_model_name.strip()
    target = target_model_name.strip()
    if not source or not target or source == target or not ids or mode not in ("copy", "move"):
        raise ValueError("请选择不同的来源和目标模型，并提供有效候选")
    placeholders = ",".join("?" for _ in ids)
    with _conn() as conn:
        conn.execute("BEGIN IMMEDIATE")
        rows = conn.execute(
            f"SELECT * FROM model_routes WHERE id IN ({placeholders}) ORDER BY priority, id", ids
        ).fetchall()
        if len(rows) != len(ids) or any(row["model_name"] != source for row in rows):
            raise RouteTransferConflict("来源候选已经变化，请刷新后重试")
        protocol = _group_protocol(conn, rows[0]["group_id"])
        if any(_group_protocol(conn, row["group_id"]) != protocol for row in rows):
            raise RouteTransferConflict("来源候选的协议不一致")
        # 目标模型可以在别的接口下已有自己的链（同名多协议是合法的），所以只看
        # 「目标在**这种接口**下是否已有链」来决定新插候选要不要当首选
        has_chain = conn.execute(
            "SELECT 1 FROM model_routes m JOIN upstream_groups g ON g.id = m.group_id"
            " WHERE m.model_name=? AND g.protocol=? LIMIT 1",
            (target, protocol),
        ).fetchone() is not None
        preferred = next((row["id"] for row in rows if row["is_active"]), rows[0]["id"])
        next_priority = conn.execute(
            "SELECT COALESCE(MAX(m.priority), -1) + 1 AS p FROM model_routes m"
            " JOIN upstream_groups g ON g.id = m.group_id"
            " WHERE m.model_name=? AND g.protocol=?",
            (target, protocol),
        ).fetchone()["p"]
        result_ids = []
        added = 0
        for row in rows:
            existing = conn.execute(
                "SELECT id FROM model_routes WHERE model_name=? AND group_id=? AND remote_model=?",
                (target, row["group_id"], row["remote_model"]),
            ).fetchone()
            if existing:
                result_ids.append(existing["id"])
                continue
            cur = conn.execute(
                "INSERT INTO model_routes(model_name, group_id, remote_model, is_active, priority)"
                " VALUES(?,?,?,?,?)",
                (target, row["group_id"], row["remote_model"],
                 int(not has_chain and row["id"] == preferred), next_priority),
            )
            result_ids.append(int(cur.lastrowid))
            next_priority += 1
            added += 1
        if mode == "move":
            conn.execute(f"DELETE FROM model_routes WHERE id IN ({placeholders})", ids)
            _reattach_active(conn, source)
        _reattach_active(conn, target)
        source_empty = conn.execute(
            "SELECT 1 FROM model_routes WHERE model_name=? LIMIT 1", (source,)
        ).fetchone() is None
        return {
            "model_name": target, "protocol": protocol, "route_ids": result_ids,
            "added": added, "merged": len(rows) - added,
            "moved": len(rows) if mode == "move" else 0, "source_empty": source_empty,
        }


def update_model_route(route_id: int, remote_model: str) -> bool:
    """只改「上游那边的真实模型名」。1M 开关也是它 —— 存成 `名字[1m]` 后缀。"""
    with _conn() as conn:
        row = conn.execute("SELECT group_id FROM model_routes WHERE id=?", (route_id,)).fetchone()
        try:
            cur = conn.execute(
                "UPDATE model_routes SET remote_model=? WHERE id=?", (remote_model, route_id)
            )
        except sqlite3.IntegrityError as exc:
            # 改成了同分组里另一条候选已经用着的真名，那两条就完全一样了
            raise DuplicateRemote(remote_model) from exc
        if row is not None and cur.rowcount:
            # 改出来的新真名也是这个站的上游模型，登记一下；旧名保留在目录里
            conn.execute(
                "INSERT OR IGNORE INTO group_models(group_id, remote_model) VALUES(?,?)",
                (row["group_id"], remote_model),
            )
        return cur.rowcount > 0


def add_routes_for_group(group_id: int, model_names: Iterable[str]) -> tuple[int, tuple[str, ...]]:
    """批量加候选，返回 (加上了几个, 跳过了哪些)。

    跳过的一直是空的（历史上是「跨接口撞名」的兜底，同名多协议放开后不再有这种情况）；
    重复的真名不算跳过 —— 已经有了本来就是想要的结果。保留返回形状以兼容管理 API。"""
    added = 0
    skipped: list[str] = []
    for raw in model_names:
        name = raw.strip()
        if not name:
            continue
        if add_model_route(name, group_id, name):
            added += 1
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


def delete_model(model_name: str, protocol: str = "") -> int:
    """删掉一个模型名下的候选，返回删除条数。给了 protocol 就只删那条链 ——
    同名模型在别的接口下的候选保留（整名删掉时不存在活跃位问题，不用重挂）。"""
    with _conn() as conn:
        if protocol:
            removed = conn.execute(
                "DELETE FROM model_routes WHERE id IN ("
                "  SELECT m.id FROM model_routes m"
                "  JOIN upstream_groups g ON g.id = m.group_id"
                "  WHERE m.model_name=? AND g.protocol=?)",
                (model_name, protocol),
            ).rowcount
            if removed:
                _reattach_active(conn, model_name)
        else:
            removed = conn.execute(
                "DELETE FROM model_routes WHERE model_name=?", (model_name,)
            ).rowcount
        return removed


def switch_route(route_id: int) -> str:
    """把流量切到这一条候选，返回它的模型名（找不到返回空串）。

    活跃位是链内（模型名 + 接口）的：同名模型在别的接口下还有自己的链，别把那边的
    首选一起清了。"""
    with _conn() as conn:
        row = conn.execute(
            "SELECT m.model_name AS model_name, g.protocol AS protocol FROM model_routes m"
            " JOIN upstream_groups g ON g.id = m.group_id WHERE m.id=?",
            (route_id,),
        ).fetchone()
        if row is None:
            return ""
        name = row["model_name"]
        conn.execute(
            "UPDATE model_routes SET is_active=0 WHERE id IN ("
            "  SELECT m.id FROM model_routes m"
            "  JOIN upstream_groups g ON g.id = m.group_id"
            "  WHERE m.model_name=? AND g.protocol=? AND m.is_active=1)",
            (name, row["protocol"]),
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


_STANDALONE_SEARCH_TARGET_KEY = "standalone_search_target_group_id"
_STANDALONE_SEARCH_MODEL_KEY = "standalone_search_target_model"


def _group_route(conn: sqlite3.Connection, group_id: int, model_name: str) -> Route | None:
    """Build a direct OpenAI route for a configured standalone-search group.

    Alpha Search is a separate Codex endpoint: its provider credential can be
    capable of search even when it is not a Responses candidate for the model
    currently selected in Codex.  Keep that decision explicit and use the
    incoming model name unchanged, so the upstream can apply its own model aliases and
    credential policy.
    """
    row = conn.execute(
        """
        SELECT u.*, g.id AS group_id, g.name AS group_name, g.api_key
        FROM upstream_groups g
        JOIN upstreams u ON u.id=g.upstream_id
        WHERE g.id=? AND g.protocol='openai' AND g.enabled=1 AND u.enabled=1
        """,
        (group_id,),
    ).fetchone()
    if row is None:
        return None
    return Route(
        model_name=model_name,
        upstream=_to_upstream(row, api_key=row["api_key"]),
        remote_model=model_name,
        group_id=row["group_id"],
        group_name=row["group_name"],
    )


def standalone_search_target_group_id() -> int | None:
    """Return the configured search-only OpenAI group, if it is well formed."""
    raw = get_setting(_STANDALONE_SEARCH_TARGET_KEY, "").strip()
    try:
        group_id = int(raw)
    except ValueError:
        return None
    return group_id if group_id > 0 else None


def set_standalone_search_target_group(group_id: int | None) -> None:
    """Set or clear the explicit target used only by ``/alpha/search``."""
    set_setting(_STANDALONE_SEARCH_TARGET_KEY, "" if group_id is None else str(group_id))


def standalone_search_target_model() -> str:
    """Return the optional model name sent to the search-only upstream."""
    return get_setting(_STANDALONE_SEARCH_MODEL_KEY, "").strip()


def set_standalone_search_target_model(model_name: str | None) -> None:
    """Set or clear the model alias used only by standalone Alpha Search."""
    set_setting(_STANDALONE_SEARCH_MODEL_KEY, (model_name or "").strip())


def resolve_standalone_search_group(group_id: int, model_name: str) -> Route | None:
    """Resolve one enabled OpenAI group for a standalone-search request."""
    with _conn() as conn:
        return _group_route(conn, group_id, model_name)


def resolve_standalone_search_target(model_name: str) -> Route | None:
    """Resolve the configured standalone-search group without needing a model route."""
    group_id = standalone_search_target_group_id()
    target_model = standalone_search_target_model() or model_name
    return (
        resolve_standalone_search_group(group_id, target_model)
        if group_id is not None
        else None
    )


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


def protocol_of_model(model_name: str) -> tuple[str, ...]:
    """这个模型名在哪些接口下暴露；没录入过就是空。给 404 文案用。"""
    with _conn() as conn:
        return _model_protocols(conn, model_name)


def exposed_models(protocol: str = "") -> tuple[str, ...]:
    """对下游暴露的模型清单。停用的接口不算暴露 —— 客户端不该看见调不动的名字。

    同名模型在多种接口下各有一条链时只列一次；只有在**所有**链都落在停用接口上时
    才整个隐藏。"""
    query = (
        "SELECT DISTINCT m.model_name AS model_name, g.protocol AS protocol"
        " FROM model_routes m JOIN upstream_groups g ON g.id = m.group_id"
    )
    args: tuple = ()
    if protocol:
        query += " WHERE g.protocol=?"
        args = (protocol,)
    disabled = disabled_protocols()
    with _conn() as conn:
        rows = conn.execute(query + " ORDER BY m.model_name", args).fetchall()
    visible: dict[str, bool] = {}
    for r in rows:
        visible[r["model_name"]] = visible.get(r["model_name"], False) or r["protocol"] not in disabled
    return tuple(name for name, ok in visible.items() if ok)


def _reattach_active(conn: sqlite3.Connection, model_name: str) -> None:
    """删除候选后如果某条链没有活跃候选了，把流量落到那条链的第一个候选（priority 最小）。

    同名模型可以在多种接口下各有一条链，活跃位是链内的 —— 所以按 (模型名, 接口)
    逐条检查，别把另一条链的活跃位顶掉。"""
    chains = conn.execute(
        "SELECT DISTINCT g.protocol FROM model_routes m"
        " JOIN upstream_groups g ON g.id = m.group_id WHERE m.model_name=?",
        (model_name,),
    ).fetchall()
    for chain in chains:
        protocol = chain["protocol"]
        still_active = conn.execute(
            "SELECT 1 FROM model_routes m JOIN upstream_groups g ON g.id = m.group_id"
            " WHERE m.model_name=? AND g.protocol=? AND m.is_active=1",
            (model_name, protocol),
        ).fetchone()
        if still_active is not None:
            continue
        remaining = conn.execute(
            "SELECT m.id FROM model_routes m JOIN upstream_groups g ON g.id = m.group_id"
            " WHERE m.model_name=? AND g.protocol=? ORDER BY m.priority, m.id LIMIT 1",
            (model_name, protocol),
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
    resp_text_bytes: int = 0,
    thinking: bool = False,
    cache_creation_tokens: int | None = None,
) -> None:
    with _conn() as conn:
        conn.execute(
            "INSERT INTO request_log(client, model, remote_model, protocol, upstream, group_name,"
            " status, stream, req_bytes, resp_bytes, duration_ms, input_tokens, output_tokens,"
            " cached_tokens, cache_creation_tokens, note, attempt, resp_text_bytes, thinking)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (client, model, remote_model, protocol, upstream, group_name, status, int(stream),
             req_bytes, resp_bytes, duration_ms, input_tokens, output_tokens, cached_tokens,
             cache_creation_tokens, note, attempt, resp_text_bytes, int(thinking)),
        )
        conn.execute(
            "DELETE FROM request_log WHERE id <= (SELECT MAX(id) - ? FROM request_log)", (LOG_KEEP_ROWS,)
        )


def recent_requests(limit: int = 50, exclude_protocols: Iterable[str] = ()) -> tuple[dict, ...]:
    """最近 limit 条转发记录；exclude_protocols 里的接口在 SQL 里就滤掉（不占配额）。"""
    excluded = tuple(sorted(set(exclude_protocols)))
    with _conn() as conn:
        if excluded:
            marks = ",".join("?" for _ in excluded)
            rows = conn.execute(
                f"SELECT * FROM request_log WHERE protocol NOT IN ({marks})"
                " ORDER BY id DESC LIMIT ?",
                (*excluded, limit),
            ).fetchall()
        else:
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


# ---------------------------------------------------------------- 接口全局开关
#
# 停用一种接口 = 假装它不存在：下游模型、该接口的分组、只有该接口的站都从管理页隐藏，
# 转发入口直接拒绝。支持（描述符、分组、历史记录）都留着，随时可以再打开。
# 存「停用了哪些」而不是「启用了哪些」：新加一种协议默认就是启用的，不用补迁移。

_DISABLED_PROTOCOLS_KEY = "disabled_protocols"
# 开关是「读一整份 → 改一位 → 写回去」：并发切换两个协议时后写者会吞掉前一位
_toggle_lock = threading.Lock()


def disabled_protocols() -> frozenset[str]:
    raw = get_setting(_DISABLED_PROTOCOLS_KEY, "")
    if not raw:
        return frozenset()
    try:
        values = json.loads(raw)
    except ValueError:
        return frozenset()
    if not isinstance(values, list):
        return frozenset()
    return frozenset(str(v) for v in values if isinstance(v, str))


def protocol_enabled(protocol: str) -> bool:
    return protocol not in disabled_protocols()


def set_protocol_enabled(protocol: str, enabled: bool) -> None:
    with _toggle_lock:
        current = set(disabled_protocols())
        if enabled:
            current.discard(protocol)
        else:
            current.add(protocol)
        set_setting(_DISABLED_PROTOCOLS_KEY, json.dumps(sorted(current)))


def clear_request_log() -> int:
    with _conn() as conn:
        return conn.execute("DELETE FROM request_log").rowcount


def request_stats() -> dict:
    """兼容旧调用点；统计口径统一由 stats 模块按协议描述符计算。"""
    from . import stats as stats_mod

    return stats_mod.request_stats()
