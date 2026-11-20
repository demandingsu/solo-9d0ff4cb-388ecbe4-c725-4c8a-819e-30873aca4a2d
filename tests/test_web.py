"""端到端 Web 流程测试：上传 → 勾选/剔除 → 预览 → 下载 / 冲突阻止。"""
import sqlite3

import pytest
from fastapi.testclient import TestClient

from app import main


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(main, "DATA_DIR", str(tmp_path / "data"))
    main.SESSIONS.clear()
    return TestClient(main.app)


def _upload(client, sample_db):
    with open(sample_db, "rb") as f:
        resp = client.post(
            "/upload",
            files={"file": ("lab.db", f, "application/x-sqlite3")},
            follow_redirects=False,
        )
    assert resp.status_code == 303
    assert resp.headers["location"] == "/select"


def test_full_flow_download(client, sample_db, tmp_path):
    _upload(client, sample_db)

    resp = client.get("/select")
    assert resp.status_code == 200
    assert "墙体试件A" in resp.text and "背景噪声对照测量" in resp.text

    resp = client.post("/select", data={"test": ["1"]}, follow_redirects=False)
    assert resp.status_code == 303

    resp = client.get("/preview")
    assert resp.status_code == 200
    assert "确认并下载" in resp.text
    assert "901" in resp.text  # 跨组带入的对照读数出现在预览中

    resp = client.post("/download")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("application/x-sqlite3")
    out = tmp_path / "downloaded.db"
    out.write_bytes(resp.content)
    conn = sqlite3.connect(out)
    assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
    reading_ids = {r[0] for r in conn.execute("SELECT id FROM readings")}
    assert reading_ids == {101, 102, 103, 901}
    conn.close()


def test_conflict_blocks_download(client, sample_db):
    _upload(client, sample_db)
    # 勾选 T1 与 T9，并剔除被 T1 读数引用的对照读数 R901
    resp = client.post(
        "/select",
        data={"test": ["1", "9"], "excl": ["901"]},
        follow_redirects=False,
    )
    assert resp.status_code == 303

    resp = client.get("/preview")
    assert resp.status_code == 200
    assert "已阻止下载" in resp.text
    assert "剔除" in resp.text  # 引用链与冲突原因展示在预览页

    resp = client.post("/download")
    assert resp.status_code == 409
    assert "阻止" in resp.text


def test_exclusion_of_unselected_test_reading_ignored(client, sample_db):
    """剔除未勾选试验的读数被静默忽略，不影响交付。"""
    _upload(client, sample_db)
    client.post("/select", data={"test": ["1"], "excl": ["901"]})
    resp = client.get("/preview")
    assert "确认并下载" in resp.text
    resp = client.post("/download")
    assert resp.status_code == 200


def test_upload_rejects_bad_file(client, tmp_path):
    bad = tmp_path / "bad.db"
    bad.write_bytes(b"this is not sqlite")
    with open(bad, "rb") as f:
        resp = client.post("/upload", files={"file": ("bad.db", f)})
    assert resp.status_code == 400
    assert "SQLite" in resp.text


def test_upload_rejects_wrong_schema(client, tmp_path):
    wrong = tmp_path / "wrong.db"
    conn = sqlite3.connect(wrong)
    conn.execute("CREATE TABLE whatever (id INTEGER)")
    conn.commit()
    conn.close()
    with open(wrong, "rb") as f:
        resp = client.post("/upload", files={"file": ("wrong.db", f)})
    assert resp.status_code == 400
    assert "缺少数据表" in resp.text


def test_pages_require_session(client):
    assert client.get("/select", follow_redirects=False).status_code == 303
    assert client.get("/preview", follow_redirects=False).status_code == 303


def _upload_bytes(client, data: bytes):
    resp = client.post(
        "/upload",
        files={"file": ("lab.db", data, "application/x-sqlite3")},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    return resp


TEXT_PK_DDL = """
CREATE TABLE groups (id INTEGER PRIMARY KEY, name TEXT);
CREATE TABLE calibrations (id TEXT PRIMARY KEY, device TEXT);
CREATE TABLE tests (
    id INTEGER PRIMARY KEY,
    group_id INTEGER NOT NULL REFERENCES groups(id),
    calibration_id INTEGER REFERENCES calibrations(id),
    name TEXT
);
CREATE TABLE readings (
    id INTEGER PRIMARY KEY,
    test_id INTEGER NOT NULL REFERENCES tests(id),
    calibration_id INTEGER REFERENCES calibrations(id),
    control_reading_id INTEGER REFERENCES readings(id),
    kind TEXT
);
"""


def test_mixed_text_integer_ids_listed_and_delivered(client, tmp_path):
    """文本校准主键 7/007 + 整数引用 7：选择页不漏列，只带入文本 7。"""
    path = tmp_path / "mixed.db"
    conn = sqlite3.connect(path)
    conn.executescript(TEXT_PK_DDL)
    for stmt in [
        "INSERT INTO groups VALUES (1, 'G1'), (2, 'G2')",
        "INSERT INTO calibrations VALUES ('7', '声级计7'), ('007', '声级计007')",
        "INSERT INTO tests VALUES (10, 1, 7, 'T10'), (20, 2, 7, 'T20')",
        "INSERT INTO readings VALUES (100, 10, 7, 200, 'm')",
        "INSERT INTO readings VALUES (200, 20, 7, NULL, 'control')",
    ]:
        conn.execute(stmt)
    conn.commit()
    conn.close()
    _upload_bytes(client, path.read_bytes())

    resp = client.get("/select")
    assert "T10" in resp.text and "T20" in resp.text
    assert "声级计7" not in resp.text  # 选择页只列读数，校准在预览中展示

    client.post("/select", data={"test": ["10"]})
    resp = client.get("/preview")
    assert "引用校验通过" in resp.text
    assert "声级计7" in resp.text
    assert "声级计007" not in resp.text
    assert "读数 #200" in resp.text

    resp = client.post("/download")
    assert resp.status_code == 200
    out = tmp_path / "mixed_subset.db"
    out.write_bytes(resp.content)
    conn = sqlite3.connect(out)
    assert conn.execute("SELECT id FROM calibrations").fetchall() == [("7",)]
    assert {r[0] for r in conn.execute("SELECT id FROM readings")} == {100, 200}
    assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
    conn.close()


def test_selection_lists_readings_by_foreign_key_ownership(client, tmp_path):
    """读数引用值写成文本 '010'，实际归属试验 10：选择页必须列在试验 10 下。"""
    path = tmp_path / "textual.db"
    conn = sqlite3.connect(path)
    conn.executescript(
        """
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
    )
    conn.execute("INSERT INTO groups VALUES (1, 'G1')")
    conn.execute("INSERT INTO tests VALUES (10, '01', NULL, 'T10')")
    conn.execute("INSERT INTO readings VALUES (100, '010', NULL, NULL, '标准差读数')")
    conn.commit()
    conn.close()
    _upload_bytes(client, path.read_bytes())

    resp = client.get("/select")
    assert "T10" in resp.text
    assert "标准差读数" in resp.text

    # 按实际归属剔除：读数 100 属于所选试验 10，剔除应生效
    client.post("/select", data={"test": ["10"], "excl": ["100"]})
    resp = client.get("/preview")
    assert "引用校验通过" in resp.text
    resp = client.post("/download")
    assert resp.status_code == 200
    out = tmp_path / "textual_subset.db"
    out.write_bytes(resp.content)
    conn = sqlite3.connect(out)
    assert conn.execute("SELECT COUNT(*) FROM readings").fetchone()[0] == 0
    assert conn.execute("SELECT group_id FROM tests").fetchall() == [("01",)]
    assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
    conn.close()


def test_dangling_group_test_still_listed_and_blocks(client, tmp_path):
    """试验所属组悬空：选择页仍列出该试验，勾选后预览阻止下载。"""
    path = tmp_path / "orphan.db"
    conn = sqlite3.connect(path)
    conn.executescript(TEXT_PK_DDL)
    conn.execute("INSERT INTO groups VALUES (1, 'G1')")
    conn.execute("INSERT INTO tests VALUES (10, 1, NULL, 'T10')")
    conn.execute("INSERT INTO tests VALUES (30, 88, NULL, 'T30-孤立')")
    conn.execute("INSERT INTO readings VALUES (300, 30, NULL, NULL, 'm')")
    conn.commit()
    conn.close()
    _upload_bytes(client, path.read_bytes())

    resp = client.get("/select")
    assert "T30-孤立" in resp.text
    assert "归属不完整" in resp.text

    client.post("/select", data={"test": ["30"]})
    resp = client.get("/preview")
    assert "已阻止下载" in resp.text
    assert "组 #88" in resp.text
    assert client.post("/download").status_code == 409
