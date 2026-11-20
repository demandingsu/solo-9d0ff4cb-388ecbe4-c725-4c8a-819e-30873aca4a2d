"""隔声试验数据交付核心逻辑：引用解析与 SQLite 子库生成。

数据库结构约定（表名、关键列名固定，其余列任意，详见 README.md）：

- groups(id, name, ...)
- calibrations(id, device, ...)
- tests(id, group_id -> groups.id,
        calibration_id -> calibrations.id,          -- 可空，试验级校准引用
        name, ...)
- readings(id, test_id -> tests.id,
           calibration_id -> calibrations.id,        -- 可空，读数级校准引用
           control_reading_id -> readings.id,        -- 可空，对照读数引用（允许跨组）
           kind, frequency_hz, level_db, ...)

交付规则：
1. 交付所选试验中未被剔除的全部读数；
2. 沿对照引用、校准引用补齐所需记录；对照读数允许跨组互引；
3. 跨到未勾选的组时，只带入沿引用找到的读数，以及这些读数所属的试验、
   试验所属的组（维持外键完整所必需）和引用到的校准记录；
4. 引用悬空（目标不存在）或指向已剔除读数时，记录完整引用链并阻止下载。

引用查找必须遵循 SQLite 的外键语义，而不是 Python 的「值相同即相同」：
父键列的亲和性会作用于子键值，比较时使用父键列的排序规则。因此
``INTEGER`` 引用值 ``7`` 在 ``TEXT`` 父键中匹配 ``'7'`` 而不会匹配
``'007'``；``NOCASE``/``RTRIM`` 等排序规则也必须生效。本模块通过
``SELECT * FROM 父表 WHERE 父键列 = ?`` 完成查找——这与 SQLite
``foreign_key_check`` 的判定一致。
"""
from __future__ import annotations

import os
import sqlite3
from collections import deque
from dataclasses import dataclass, field
from urllib.parse import quote

GROUPS = "groups"
CALIBRATIONS = "calibrations"
TESTS = "tests"
READINGS = "readings"
# 导出子库时的建表/插数顺序（先被引用表，后被引用表）
EXPORT_TABLES = (GROUPS, CALIBRATIONS, TESTS, READINGS)

REQUIRED_COLUMNS = {
    GROUPS: ("id",),
    CALIBRATIONS: ("id",),
    TESTS: ("id", "group_id", "calibration_id"),
    READINGS: ("id", "test_id", "calibration_id", "control_reading_id"),
}

# 源库没有声明外键时，按约定推导引用目标（表, 子列）→（父表, 父列）
CONVENTIONAL_FKS = {
    (TESTS, "group_id"): (GROUPS, "id"),
    (TESTS, "calibration_id"): (CALIBRATIONS, "id"),
    (READINGS, "test_id"): (TESTS, "id"),
    (READINGS, "calibration_id"): (CALIBRATIONS, "id"),
    (READINGS, "control_reading_id"): (READINGS, "id"),
}


def _quote_ident(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def id_sort_key(value: object) -> tuple[int, object]:
    """主键排序键：兼容整数与文本主键混合的旧库，避免 TypeError。"""
    return (0, value) if isinstance(value, int) else (1, str(value))


def validate_schema(conn: sqlite3.Connection) -> list[str]:
    """校验上传库是否满足结构约定，返回错误列表（空列表表示通过）。"""
    errors: list[str] = []
    tables = {
        r[0]
        for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )
    }
    for table, cols in REQUIRED_COLUMNS.items():
        if table not in tables:
            errors.append(f"缺少数据表：{table}")
            continue
        have = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
        for col in cols:
            if col not in have:
                errors.append(f"表 {table} 缺少列：{col}")
    return errors


@dataclass(frozen=True)
class FkHit:
    """一次外键查找命中的父行。key 为父行主键的原始存储值。"""

    table: str
    column: str
    key: object
    row: dict

    def __hash__(self) -> int:  # row 是 dict，不可哈希；按命中身份哈希
        key = self.key
        try:
            hash(key)
        except TypeError:
            key = repr(key)
        return hash((self.table, self.column, type(self.key).__name__, key))

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, FkHit):
            return NotImplemented
        return (
            self.table == other.table
            and self.column == other.column
            and type(self.key) is type(other.key)
            and self.key == other.key
            and self.row == other.row
        )


class SchemaLookup:
    """按源库真实外键定义与 SQLite 比较语义查找父行。

    查找使用一条独立的只读连接（源库为内存库时退化为备份快照），
    因此调用方可以随时关闭传入 ``load_model`` 的原连接。
    """

    def __init__(self, conn: sqlite3.Connection):
        self.conn = _snapshot_connection(conn)
        self._fk_cache: dict[
            tuple[str, str], tuple[str, str, str] | None
        ] = {}
        self._hit_cache: dict[tuple[str, str, str, object], FkHit | None] = {}

    def close(self) -> None:
        try:
            self.conn.close()
        except sqlite3.Error:
            pass

    def _primary_key_column(self, table: str) -> str | None:
        cols = [
            r[1]
            for r in self.conn.execute(
                f"PRAGMA table_info({_quote_ident(table)})"
            )
            if r[5]
        ]
        return cols[0] if len(cols) == 1 else None

    def fk_target(self, child_table: str, child_col: str) -> tuple[str, str] | None:
        """返回 (父表, 父键列)；优先使用源库声明的外键。"""
        cache_key = (child_table, child_col)
        if cache_key in self._fk_cache:
            cached = self._fk_cache[cache_key]
            return (cached[0], cached[1]) if cached else None

        target: tuple[str, str, str] | None = None
        try:
            fk_rows = self.conn.execute(
                f"PRAGMA foreign_key_list({_quote_ident(child_table)})"
            ).fetchall()
        except sqlite3.Error:
            fk_rows = []
        # PRAGMA foreign_key_list: id, seq, table, from, to, ...
        for row in fk_rows:
            _fk_id, seq, parent_table, from_col, to_col = row[:5]
            if from_col != child_col or seq != 0:
                continue
            parent_col = to_col or self._primary_key_column(parent_table)
            if parent_col is None:
                continue
            target = (parent_table, parent_col, parent_col)
            break
        if target is None:
            conventional = CONVENTIONAL_FKS.get((child_table, child_col))
            if conventional:
                target = (conventional[0], conventional[1], conventional[1])
        self._fk_cache[cache_key] = target
        return (target[0], target[1]) if target else None

    def lookup(self, child_table: str, child_col: str, value: object) -> FkHit | None:
        """模拟 SQLite 外键比较，返回父行；不存在时返回 None。"""
        if value is None:
            return None
        cache_key = (child_table, child_col, type(value).__name__, value)
        if cache_key in self._hit_cache:
            return self._hit_cache[cache_key]

        target = self.fk_target(child_table, child_col)
        hit: FkHit | None = None
        if target is not None:
            parent_table, parent_col = target
            try:
                cur = self.conn.execute(
                    f"SELECT * FROM {_quote_ident(parent_table)} "
                    f"WHERE {_quote_ident(parent_col)} = ? LIMIT 1",
                    (value,),
                )
                row = cur.fetchone()
                cols = [d[0] for d in cur.description]
            except sqlite3.Error:
                row, cols = None, []
            if row is not None:
                row_dict = dict(zip(cols, row))
                # 子库按 id 列选行，故命中身份优先用父行的 id（约定列）；
                # 若父表没有 id 列，则退回被引用列的值。
                if "id" in row_dict:
                    key_value = row_dict["id"]
                else:
                    key_value = row_dict.get(parent_col)
                hit = FkHit(parent_table, parent_col, key_value, row_dict)
        self._hit_cache[cache_key] = hit
        return hit


class RawLookup:
    """未经过 ``load_model`` 的 Model 的兜底查找，仅按 Python 值相等匹配。"""

    def __init__(self, model: "Model"):
        self.model = model

    def close(self) -> None:
        pass

    def lookup(self, child_table: str, child_col: str, value: object) -> FkHit | None:
        target = CONVENTIONAL_FKS.get((child_table, child_col))
        if target is None or value is None:
            return None
        parent_table, _parent_col = target
        rows: dict = getattr(self.model, parent_table)
        try:
            row = rows.get(value)
        except TypeError:
            return None
        if row is None:
            return None
        return FkHit(parent_table, "id", row.get("id", value), row)


def _snapshot_connection(conn: sqlite3.Connection) -> sqlite3.Connection:
    """为查找建立独立连接，避免依赖调用方传入连接的生命周期。"""
    path = ""
    try:
        rows = conn.execute("PRAGMA database_list").fetchall()
        if rows:
            path = rows[0][2] or ""
    except sqlite3.Error:
        path = ""
    if path:
        try:
            uri = "file:" + quote(os.path.abspath(path), safe="/") + "?mode=ro"
            return sqlite3.connect(uri, uri=True)
        except sqlite3.Error:
            pass
    # 内存库或无法重开文件：备份一份快照
    snapshot = sqlite3.connect(":memory:")
    conn.backup(snapshot)
    return snapshot


@dataclass
class Model:
    """源库中四张表的全部行，按主键索引。"""

    groups: dict
    calibrations: dict
    tests: dict
    readings: dict
    readings_by_test: dict
    # 以下映射均按 SQLite 外键语义解析后的「实际归属」
    tests_by_group: dict = field(default_factory=dict)
    test_group: dict = field(default_factory=dict)
    reading_test: dict = field(default_factory=dict)
    lookup: SchemaLookup | RawLookup | None = field(
        default=None, repr=False, compare=False
    )

    def __post_init__(self) -> None:
        if not self.reading_test:
            for rid, r in self.readings.items():
                self.reading_test[rid] = r.get("test_id")
        if not self.test_group:
            for tid, t in self.tests.items():
                self.test_group[tid] = t.get("group_id")
        if not self.tests_by_group:
            for tid, t in self.tests.items():
                gkey = self.test_group.get(tid)
                if gkey is not None:
                    self.tests_by_group.setdefault(gkey, []).append(tid)
            for ids in self.tests_by_group.values():
                ids.sort(key=id_sort_key)
        if not self.readings_by_test:
            for rid, r in self.readings.items():
                tkey = self.reading_test.get(rid)
                if tkey is not None:
                    self.readings_by_test.setdefault(tkey, []).append(rid)
            for ids in self.readings_by_test.values():
                ids.sort(key=id_sort_key)
        if self.lookup is None:
            # 手工构造的 Model（无源库连接）退回按值匹配
            self.lookup = RawLookup(self)

    def close(self) -> None:
        if self.lookup is not None:
            self.lookup.close()

    def __del__(self) -> None:  # 兜底释放只读连接
        try:
            lookup = getattr(self, "lookup", None)
            if lookup is not None:
                lookup.close()
        except Exception:
            pass


def _rows(conn: sqlite3.Connection, table: str) -> list[dict]:
    cur = conn.execute(f"SELECT * FROM {table}")
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, row)) for row in cur.fetchall()]


def load_model(conn: sqlite3.Connection) -> Model:
    groups = {r["id"]: r for r in _rows(conn, GROUPS)}
    calibrations = {r["id"]: r for r in _rows(conn, CALIBRATIONS)}
    tests = {r["id"]: r for r in _rows(conn, TESTS)}
    readings = {r["id"]: r for r in _rows(conn, READINGS)}

    lookup = SchemaLookup(conn)

    test_group: dict[object, object] = {}
    tests_by_group: dict[object, list] = {}
    for tid, t in tests.items():
        hit = lookup.lookup(TESTS, "group_id", t.get("group_id"))
        gkey = hit.key if hit is not None and hit.table == GROUPS else None
        test_group[tid] = gkey
        if gkey is not None:
            tests_by_group.setdefault(gkey, []).append(tid)

    reading_test: dict[object, object] = {}
    readings_by_test: dict[object, list] = {}
    for rid, r in readings.items():
        hit = lookup.lookup(READINGS, "test_id", r.get("test_id"))
        tkey = hit.key if hit is not None and hit.table == TESTS else None
        reading_test[rid] = tkey
        if tkey is not None:
            readings_by_test.setdefault(tkey, []).append(rid)

    for ids in tests_by_group.values():
        ids.sort(key=id_sort_key)
    for ids in readings_by_test.values():
        ids.sort(key=id_sort_key)

    return Model(
        groups=groups,
        calibrations=calibrations,
        tests=tests,
        readings=readings,
        readings_by_test=readings_by_test,
        tests_by_group=tests_by_group,
        test_group=test_group,
        reading_test=reading_test,
        lookup=lookup,
    )


@dataclass
class Hop:
    """引用链上的一环。kind ∈ reading / calibration / test / group。"""

    kind: str
    label: str


@dataclass
class Conflict:
    """一条无法交付的引用：chain 为从所选读数/试验出发的完整引用链。"""

    chain: list[Hop]
    reason: str


@dataclass
class Resolution:
    """一次交付解析的结果：应纳入子库的主键集合 + 拉入链 + 冲突列表。"""

    reading_ids: set
    test_ids: set
    group_ids: set
    calibration_ids: set
    pulled_in: dict = field(default_factory=dict)
    conflicts: list[Conflict] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.conflicts


def group_label(model: Model, gkey: object) -> str:
    g = model.groups.get(gkey)
    if g is None:
        return f"组 #{gkey}（不存在）"
    return f"组 #{g.get('id', gkey)}「{g.get('name', '?')}」"


def test_label(model: Model, tid: object) -> str:
    t = model.tests.get(tid)
    if t is None:
        return f"试验 #{tid}（不存在）"
    gkey = model.test_group.get(tid)
    group = group_label(model, gkey) if gkey is not None else "未归组"
    return f"试验 #{t.get('id', tid)}「{t.get('name', '?')}」（{group}）"


def reading_label(model: Model, rid: object) -> str:
    r = model.readings.get(rid)
    if r is None:
        return f"读数 #{rid}（不存在）"
    kind = r.get("kind") or "读数"
    owner = model.reading_test.get(rid)
    if owner is None:
        where = f"试验 #{r.get('test_id')}（不存在）"
    else:
        where = test_label(model, owner)
    return f"读数 #{r.get('id', rid)}（{kind}｜{where}）"


def missing_reading_label(rid: object) -> str:
    return f"读数 #{rid}（不存在）"


def calibration_label(model: Model, cid: object) -> str:
    c = model.calibrations.get(cid)
    if c is None:
        return f"校准 #{cid}（不存在）"
    return f"校准 #{c.get('id', cid)}（{c.get('device', '?')}）"


def missing_calibration_label(cid: object) -> str:
    return f"校准 #{cid}（不存在）"


def resolve(
    model: Model,
    selected_test_ids: set | list,
    excluded_reading_ids: set | list,
) -> Resolution:
    """从所选试验的未剔除读数出发，沿引用做广度优先解析。

    - excluded_reading_ids 仅对所选试验内的读数有意义（Web 层已按实际归属过滤）；
    - 引用目标在未勾选试验时：拉入该读数及其所属试验/组，并继续解析其引用；
    - 引用目标不存在或已被剔除：记录冲突（含完整引用链），不中断其余解析；
    - 所有查找都走 SQLite 外键语义（父键亲和性 + 父键列排序规则）。
    """
    selected = {t for t in selected_test_ids if t in model.tests}
    excluded = {r for r in excluded_reading_ids if r in model.readings}

    res = Resolution(
        reading_ids=set(),
        test_ids=set(selected),
        group_ids=set(),
        calibration_ids=set(),
    )

    reported: set[tuple] = set()  # 按「完整引用链 + 原因」去重

    def add_conflict(chain: list[Hop], reason: str) -> None:
        key = (reason, tuple((h.kind, h.label) for h in chain))
        if key in reported:
            return
        reported.add(key)
        res.conflicts.append(Conflict(list(chain), reason))

    def add_calibration(child_table: str, value, chain: list[Hop]) -> None:
        if value is None:
            return
        hit = model.lookup.lookup(child_table, "calibration_id", value)
        if hit is not None and hit.table == CALIBRATIONS:
            res.calibration_ids.add(hit.key)
        else:
            add_conflict(
                chain
                + [Hop("calibration", missing_calibration_label(value))],
                "悬空：引用的校准记录不存在",
            )

    def add_test_group(tid, chain: list[Hop]) -> None:
        gkey = model.test_group.get(tid)
        if gkey is not None and gkey in model.groups:
            res.group_ids.add(gkey)
        else:
            add_conflict(
                chain
                + [Hop("group", f"组 #{model.tests[tid].get('group_id')}（不存在）")],
                "悬空：试验所属的组不存在",
            )

    def add_test_calibration(tid, chain: list[Hop]) -> None:
        if tid not in model.tests:
            add_conflict(
                chain + [Hop("test", f"试验 #{tid}（不存在）")],
                "悬空：读数所属的试验不存在",
            )
            return
        add_calibration(TESTS, model.tests[tid].get("calibration_id"), chain)

    for tid in sorted(selected, key=id_sort_key):
        add_test_group(tid, [Hop("test", test_label(model, tid))])
        add_test_calibration(tid, [Hop("test", test_label(model, tid))])
        for rid in model.readings_by_test.get(tid, []):
            if rid not in excluded:
                res.reading_ids.add(rid)

    # 每个已纳入读数对应一条从所选读数出发的引用链
    chains: dict[object, list[Hop]] = {}
    queue: deque = deque()
    for rid in sorted(res.reading_ids, key=id_sort_key):
        chains[rid] = [Hop("reading", reading_label(model, rid))]
        queue.append(rid)

    while queue:
        rid = queue.popleft()
        reading = model.readings[rid]
        chain = chains[rid]

        add_calibration(READINGS, reading.get("calibration_id"), chain)

        raw_control = reading.get("control_reading_id")
        if raw_control is None:
            continue
        hit = model.lookup.lookup(READINGS, "control_reading_id", raw_control)
        if hit is None or hit.table != READINGS:
            add_conflict(
                chain + [Hop("reading", missing_reading_label(raw_control))],
                "悬空：引用的对照读数不存在",
            )
            continue
        target = hit.key
        if target in excluded:
            add_conflict(
                chain + [Hop("reading", reading_label(model, target))],
                "冲突：引用的对照读数已被剔除",
            )
            continue
        if target in res.reading_ids:
            continue  # 已纳入（含循环引用的回边），无需重复处理

        # 跨试验/跨组拉入：只带入该读数本身及其所属试验、组
        res.reading_ids.add(target)
        new_chain = chain + [Hop("reading", reading_label(model, target))]
        chains[target] = new_chain
        res.pulled_in[target] = new_chain

        owner_test = model.reading_test.get(target)
        if owner_test is None or owner_test not in model.tests:
            add_conflict(
                new_chain
                + [
                    Hop(
                        "test",
                        f"试验 #{model.readings[target].get('test_id')}（不存在）",
                    )
                ],
                "悬空：读数所属的试验不存在",
            )
            continue
        if owner_test not in res.test_ids:
            res.test_ids.add(owner_test)
            add_test_group(owner_test, new_chain)
            add_test_calibration(owner_test, new_chain)
        queue.append(target)

    res.conflicts.sort(
        key=lambda c: (c.reason, c.chain[-1].label if c.chain else "")
    )
    return res


def build_subset(source_path: str, out_path: str, res: Resolution) -> dict[str, int]:
    """按解析结果生成 SQLite 子库。

    - 建表语句直接复用源库 sqlite_master 中的 DDL 原文，
      主键、外键约束随之保留；索引、触发器一并复制；
    - 行按原主键（原始存储值，如文本 '7' 而非整数 7）插入；
      插入后执行 PRAGMA foreign_key_check 校验，发现违规即抛异常
      （理论上 resolve 已保证不会发生）。
    返回各表导出行数。
    """
    src = sqlite3.connect(f"file:{source_path}?mode=ro", uri=True)
    try:
        ddl: dict[str, str] = {}
        extras: list[str] = []  # 索引、触发器
        for typ, _name, tbl, sql in src.execute(
            "SELECT type, name, tbl_name, sql FROM sqlite_master WHERE sql IS NOT NULL"
        ):
            if typ == "table" and tbl in EXPORT_TABLES:
                ddl[tbl] = sql
            elif typ in ("index", "trigger") and tbl in EXPORT_TABLES:
                extras.append(sql)

        wanted = {
            GROUPS: res.group_ids,
            CALIBRATIONS: res.calibration_ids,
            TESTS: res.test_ids,
            READINGS: res.reading_ids,
        }

        out = sqlite3.connect(out_path)
        try:
            out.execute("PRAGMA foreign_keys = OFF")  # 插入阶段关闭，结束后统一校验
            for table in EXPORT_TABLES:
                if table in ddl:
                    out.execute(ddl[table])
            counts: dict[str, int] = {}
            for table in EXPORT_TABLES:
                ids = sorted(wanted[table], key=id_sort_key)
                if not ids or table not in ddl:
                    counts[table] = 0
                    continue
                placeholders = ",".join("?" * len(ids))
                cur = src.execute(
                    f"SELECT * FROM {_quote_ident(table)} WHERE id IN ({placeholders})",
                    ids,
                )
                cols = [d[0] for d in cur.description]
                rows = cur.fetchall()
                out.executemany(
                    f"INSERT INTO {_quote_ident(table)} "
                    f"({','.join(_quote_ident(c) for c in cols)}) "
                    f"VALUES ({','.join('?' * len(cols))})",
                    rows,
                )
                counts[table] = len(rows)
            for sql in extras:
                out.execute(sql)
            out.commit()
            violations = out.execute("PRAGMA foreign_key_check").fetchall()
            if violations:
                raise RuntimeError(f"子库外键校验失败：{violations}")
        finally:
            out.close()
    finally:
        src.close()
    return counts
