"""混合编号 / 亲和性 / 排序规则 / 已有数据冲突的回归测试。

旧采集软件导出的库常见两类「看起来能过外键检查、交付时却出错」的情况：

1. 主键列的存储类型与引用值的类型不一致（如文本主键 ``'7'`` 与 ``'007'``，
   引用列写整数 ``7``）。SQLite 外键比较会先把子键值套用**父键列的亲和性**，
   再按父键列排序规则比较，所以整数 7 只匹配文本 ``'7'``；
2. 试验/读数在源库里的引用值与其实际归属值字面不同（如 INTEGER 列中存了
   ``'01'``），按 Python 值相等分组会漏列试验和读数。
"""
import sqlite3

from app.core import build_subset, load_model, resolve


def _make(path, ddl, statements):
    conn = sqlite3.connect(path)
    try:
        conn.executescript(ddl)
        for s in statements:
            conn.execute(s)
        conn.commit()
    finally:
        conn.close()
    return str(path)


TEXT_CAL_DDL = """
CREATE TABLE groups (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL
);
CREATE TABLE calibrations (
    id TEXT PRIMARY KEY,
    device TEXT NOT NULL
);
CREATE TABLE tests (
    id INTEGER PRIMARY KEY,
    group_id INTEGER NOT NULL REFERENCES groups(id),
    calibration_id INTEGER REFERENCES calibrations(id),
    name TEXT NOT NULL
);
CREATE TABLE readings (
    id INTEGER PRIMARY KEY,
    test_id INTEGER NOT NULL REFERENCES tests(id),
    calibration_id INTEGER REFERENCES calibrations(id),
    control_reading_id INTEGER REFERENCES readings(id),
    kind TEXT NOT NULL DEFAULT 'measurement'
);
"""


def _load(path):
    conn = sqlite3.connect(path)
    try:
        return load_model(conn)
    finally:
        conn.close()


def _mixed_calibration_db(tmp_path):
    """文本校准主键 '7' 与 '007' 并存；试验级、读数级引用都写整数 7。"""
    return _make(
        tmp_path / "mixed.db",
        TEXT_CAL_DDL,
        [
            "INSERT INTO groups VALUES (1, 'G1')",
            "INSERT INTO calibrations VALUES ('7', '声级计7'), ('007', '声级计007')",
            "INSERT INTO tests VALUES (10, 1, 7, 'T10')",
            "INSERT INTO readings VALUES (100, 10, 7, NULL, 'measurement')",
        ],
    )


def test_text_pk_integer_reference_uses_sqlite_affinity(tmp_path):
    path = _mixed_calibration_db(tmp_path)
    conn = sqlite3.connect(path)
    try:
        # 源库本身通过外键检查：说明 SQLite 认可整数 7 → 文本 '7'
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
    finally:
        conn.close()

    model = _load(path)
    try:
        res = resolve(model, {10}, set())
        assert res.ok
        # 只能带入文本 '7'，不能带入 '007'
        assert res.calibration_ids == {"7"}
    finally:
        model.close()

    out = tmp_path / "mixed_subset.db"
    build_subset(path, str(out), res)
    conn = sqlite3.connect(out)
    try:
        assert conn.execute("SELECT id, device FROM calibrations").fetchall() == [
            ("7", "声级计7")
        ]
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
    finally:
        conn.close()


def test_text_pk_does_not_fall_back_to_other_matching_row(tmp_path):
    """只有 '007' 时，整数引用 7 悬空（不能误配到 '007'）。"""
    path = _make(
        tmp_path / "only007.db",
        TEXT_CAL_DDL,
        [
            "INSERT INTO groups VALUES (1, 'G1')",
            "INSERT INTO calibrations VALUES ('007', '声级计007')",
            "INSERT INTO readings VALUES (100, 10, 7, NULL, 'measurement')",
            "INSERT INTO tests VALUES (10, 1, NULL, 'T10')",
        ],
    )
    model = _load(path)
    try:
        res = resolve(model, {10}, set())
        assert not res.ok
        conflict = res.conflicts[0]
        assert "校准" in conflict.reason and "悬空" in conflict.reason
        assert "校准 #7（不存在）" in conflict.chain[-1].label
        assert "100" in conflict.chain[0].label
    finally:
        model.close()


def test_nocase_collation_on_parent_key(tmp_path):
    """父键列 NOCASE 排序规则：子键 'abc' 命中父行 'ABC'。"""
    ddl = """
    CREATE TABLE groups (id TEXT COLLATE NOCASE PRIMARY KEY, name TEXT);
    CREATE TABLE calibrations (id INTEGER PRIMARY KEY, device TEXT);
    CREATE TABLE tests (
        id INTEGER PRIMARY KEY,
        group_id TEXT NOT NULL REFERENCES groups(id),
        calibration_id INTEGER REFERENCES calibrations(id),
        name TEXT
    );
    CREATE TABLE readings (
        id INTEGER PRIMARY KEY,
        test_id INTEGER REFERENCES tests(id),
        calibration_id INTEGER REFERENCES calibrations(id),
        control_reading_id INTEGER REFERENCES readings(id),
        kind TEXT
    );
    """
    path = _make(
        tmp_path / "nocase.db",
        ddl,
        [
            "INSERT INTO groups VALUES ('ABC', 'Upper')",
            "INSERT INTO tests VALUES (10, 'abc', NULL, 'T10')",
            "INSERT INTO readings VALUES (100, 10, NULL, NULL, 'm')",
        ],
    )
    conn = sqlite3.connect(path)
    try:
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
    finally:
        conn.close()

    model = _load(path)
    try:
        assert model.test_group[10] == "ABC"
        assert model.tests_by_group == {"ABC": [10]}
        res = resolve(model, {10}, set())
        assert res.ok
        assert res.group_ids == {"ABC"}
        assert res.reading_ids == {100}
    finally:
        model.close()


def test_integer_parent_accepts_textual_reference(tmp_path):
    """INTEGER 父键 + TEXT 子列存 '01'/'010'：按实际归属归组、归试验，不漏列。

    旧代码按 Python 值相等分组（'01' != 1），会把试验/读数整个漏掉。
    """
    ddl = """
    CREATE TABLE groups (id INTEGER PRIMARY KEY, name TEXT);
    CREATE TABLE calibrations (id INTEGER PRIMARY KEY, device TEXT);
    CREATE TABLE tests (
        id INTEGER PRIMARY KEY,
        group_id TEXT NOT NULL REFERENCES groups(id),
        calibration_id INTEGER REFERENCES calibrations(id),
        name TEXT
    );
    CREATE TABLE readings (
        id INTEGER PRIMARY KEY,
        test_id TEXT REFERENCES tests(id),
        calibration_id INTEGER REFERENCES calibrations(id),
        control_reading_id INTEGER REFERENCES readings(id),
        kind TEXT
    );
    """
    path = _make(
        tmp_path / "textual.db",
        ddl,
        [
            "INSERT INTO groups VALUES (1, 'G1')",
            "INSERT INTO tests VALUES (10, '01', NULL, 'T10')",
            "INSERT INTO readings VALUES (100, '010', NULL, NULL, 'm')",
        ],
    )
    conn = sqlite3.connect(path)
    try:
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
        # 原样保留了文本形式的引用值
        assert conn.execute("SELECT group_id FROM tests").fetchone() == ("01",)
        assert conn.execute("SELECT test_id FROM readings").fetchone() == ("010",)
    finally:
        conn.close()

    model = _load(path)
    try:
        assert model.test_group == {10: 1}
        assert model.reading_test == {100: 10}
        assert model.tests_by_group == {1: [10]}
        assert model.readings_by_test == {10: [100]}
        res = resolve(model, {10}, set())
        assert res.ok
        assert res.reading_ids == {100}
        # 读数剔除按实际归属生效
        res2 = resolve(model, {10}, {100})
        assert res2.ok and res2.reading_ids == set()
        # 子库原样保留引用值并通过外键检查
        out = tmp_path / "textual_sub.db"
        build_subset(path, str(out), res)
        conn = sqlite3.connect(out)
        try:
            assert conn.execute("SELECT group_id FROM tests").fetchall() == [("01",)]
            assert conn.execute("SELECT test_id FROM readings").fetchall() == [("010",)]
            assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
        finally:
            conn.close()
    finally:
        model.close()


def test_mixed_calibration_integer_ref_resolves_to_text_7_end_to_end(tmp_path):
    """对照读数也引用校准文本主键时，沿跨组链带入正确校准。"""
    path = _make(
        tmp_path / "mixed_chain.db",
        TEXT_CAL_DDL,
        [
            "INSERT INTO groups VALUES (1, 'G1'), (2, 'G2')",
            "INSERT INTO calibrations VALUES ('7', '甲'), ('007', '乙')",
            "INSERT INTO tests VALUES (10, 1, 7, 'T10'), (20, 2, 7, 'T20')",
            "INSERT INTO readings VALUES (100, 10, 7, 200, 'm')",
            "INSERT INTO readings VALUES (200, 20, 7, NULL, 'control')",
        ],
    )
    model = _load(path)
    try:
        res = resolve(model, {10}, set())
        assert res.ok
        assert res.reading_ids == {100, 200}
        assert res.test_ids == {10, 20}
        assert res.group_ids == {1, 2}
        assert res.calibration_ids == {"7"}
    finally:
        model.close()


def test_dangling_row_does_not_stop_other_conflicts(tmp_path):
    """同时存在悬空对照与悬空校准：两者都要报告，链完整。"""
    path = _make(
        tmp_path / "conflicts.db",
        TEXT_CAL_DDL,
        [
            "INSERT INTO groups VALUES (1, 'G1')",
            "INSERT INTO calibrations VALUES ('7', '甲')",
            "INSERT INTO tests VALUES (10, 1, 999, 'T10')",
            "INSERT INTO readings VALUES (100, 10, 998, 997, 'm')",
        ],
    )
    model = _load(path)
    try:
        res = resolve(model, {10}, set())
        assert not res.ok
        reasons = [c.reason for c in res.conflicts]
        assert any("校准" in r for r in reasons)
        assert any("对照读数" in r for r in reasons)
        for c in res.conflicts:
            assert len(c.chain) >= 2
    finally:
        model.close()


def test_schema_without_declared_fk_uses_convention(tmp_path):
    """源库未声明 REFERENCES 时按约定列名找目标，仍能解析。"""
    ddl = """
    CREATE TABLE groups (id INTEGER PRIMARY KEY, name TEXT);
    CREATE TABLE calibrations (id INTEGER PRIMARY KEY, device TEXT);
    CREATE TABLE tests (id INTEGER PRIMARY KEY, group_id INTEGER,
                        calibration_id INTEGER, name TEXT);
    CREATE TABLE readings (id INTEGER PRIMARY KEY, test_id INTEGER,
                           calibration_id INTEGER, control_reading_id INTEGER,
                           kind TEXT);
    """
    path = _make(
        tmp_path / "nofk.db",
        ddl,
        [
            "INSERT INTO groups VALUES (1, 'G1')",
            "INSERT INTO calibrations VALUES (7, '甲')",
            "INSERT INTO tests VALUES (10, 1, 7, 'T10')",
            "INSERT INTO readings VALUES (100, 10, 7, NULL, 'm')",
        ],
    )
    model = _load(path)
    try:
        assert model.test_group == {10: 1}
        res = resolve(model, {10}, set())
        assert res.ok
        assert res.calibration_ids == {7}
    finally:
        model.close()
