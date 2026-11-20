"""隔声试验数据交付复核 Web 应用（FastAPI）。

流程：上传 SQLite 库 → 勾选试验 / 剔除异常读数 → 预览引用解析结果
（含跨组拉入链与冲突）→ 确认后下载 SQLite 子库。

试验归组、读数归属、对照/校准引用都按 SQLite 外键语义解析（见 app/core.py），
不按字面值相等分组，因此旧采集软件导出库中的混合编号不会漏列或误报。
"""
from __future__ import annotations

import os
import shutil
import sqlite3
import time
import uuid
from dataclasses import dataclass, field

from fastapi import FastAPI, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from .core import (
    build_subset,
    calibration_label,
    group_label,
    id_sort_key,
    load_model,
    reading_label,
    resolve,
    test_label,
    validate_schema,
)

DATA_DIR = os.environ.get("DATA_DIR", "./data")
MAX_UPLOAD_MB = int(os.environ.get("MAX_UPLOAD_MB", "200"))

app = FastAPI(title="隔声试验数据交付复核")
templates = Jinja2Templates(
    directory=os.path.join(os.path.dirname(os.path.abspath(__file__)), "templates")
)


def _sort_ids(values):
    return sorted(values, key=id_sort_key)


templates.env.filters["sort_ids"] = _sort_ids


@dataclass
class SessionState:
    workdir: str
    source_name: str
    selected_tests: set = field(default_factory=set)
    excluded_readings: set = field(default_factory=set)

    @property
    def db_path(self) -> str:
        return os.path.join(self.workdir, "source.db")


# 单进程内存会话；重启即失效（上传文件保留在 DATA_DIR 下，由运维清理）
SESSIONS: dict[str, SessionState] = {}


def _get_session(request: Request) -> SessionState | None:
    sid = request.cookies.get("sid")
    return SESSIONS.get(sid) if sid else None


def _load(st: SessionState):
    conn = sqlite3.connect(f"file:{st.db_path}?mode=ro", uri=True)
    try:
        return load_model(conn)
    finally:
        conn.close()


def _form_id(rows: dict, value: str):
    """表单值 → 模型主键。优先按原始字符串匹配（兼容文本主键），再按整数。"""
    if value in rows:
        return value
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


@app.get("/", response_class=HTMLResponse)
def index(request: Request):
    return templates.TemplateResponse(
        request, "upload.html", {"error": None}
    )


@app.post("/upload", response_class=HTMLResponse)
async def upload(request: Request, file: UploadFile):
    error: str | None = None
    if not file.filename:
        error = "未选择文件"
    data = await file.read()
    if error is None and len(data) > MAX_UPLOAD_MB * 1024 * 1024:
        error = f"文件超过大小限制（{MAX_UPLOAD_MB} MB）"

    if error is None:
        # 旧会话的工作目录一并清理
        old = _get_session(request)
        if old:
            SESSIONS.pop(request.cookies.get("sid"), None)
            shutil.rmtree(old.workdir, ignore_errors=True)

        sid = uuid.uuid4().hex
        workdir = os.path.join(DATA_DIR, sid)
        os.makedirs(workdir, exist_ok=True)
        path = os.path.join(workdir, "source.db")
        with open(path, "wb") as f:
            f.write(data)
        try:
            conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
            errors = validate_schema(conn)
            conn.close()
        except sqlite3.DatabaseError:
            errors = ["文件不是有效的 SQLite 数据库"]
        if errors:
            shutil.rmtree(workdir, ignore_errors=True)
            return templates.TemplateResponse(
                request,
                "upload.html",
                {"error": "；".join(errors)},
                status_code=400,
            )
        SESSIONS[sid] = SessionState(workdir, file.filename or "source.db")
        resp = RedirectResponse("/select", status_code=303)
        resp.set_cookie("sid", sid, httponly=True)
        return resp

    return templates.TemplateResponse(
        request, "upload.html", {"error": error}, status_code=400
    )


# 读数表展示时的优先列序，其余列按出现顺序排后
_PREFERRED_READING_COLS = [
    "id", "kind", "frequency_hz", "level_db",
    "control_reading_id", "calibration_id", "note",
]


@app.get("/select", response_class=HTMLResponse)
def select_page(request: Request):
    st = _get_session(request)
    if not st:
        return RedirectResponse("/", status_code=303)
    model = _load(st)
    try:
        headers: list[str] = []
        for r in model.readings.values():
            for col in r:
                if col not in headers:
                    headers.append(col)
        headers = [c for c in _PREFERRED_READING_COLS if c in headers] + [
            c for c in headers if c not in _PREFERRED_READING_COLS
        ]

        groups = []
        shown: set = set()
        for gid in sorted(model.groups, key=id_sort_key):
            g = model.groups[gid]
            tests = []
            for tid in model.tests_by_group.get(gid, []):
                t = model.tests[tid]
                shown.add(tid)
                readings = []
                for rid in model.readings_by_test.get(tid, []):
                    shown.add(rid)
                    r = model.readings[rid]
                    readings.append(
                        {
                            "id": r["id"],
                            "cells": [r.get(c) for c in headers],
                            "excluded": rid in st.excluded_readings,
                        }
                    )
                tests.append(
                    {"row": t, "readings": readings, "selected": tid in st.selected_tests}
                )
            groups.append({"row": g, "tests": tests})

        # 归属悬空的试验/读数（引用目标不存在）无法归入任何组；
        # 单独列出，保证选择页不漏列源库中的记录，且这些试验仍可选择，
        # 交由预览页报告悬空引用。
        dangling_tests = []
        for tid in sorted(model.tests, key=id_sort_key):
            if tid in shown:
                continue
            t = model.tests[tid]
            readings = []
            for rid in model.readings_by_test.get(tid, []):
                shown.add(rid)
                r = model.readings[rid]
                readings.append(
                    {
                        "id": r["id"],
                        "cells": [r.get(c) for c in headers],
                        "excluded": rid in st.excluded_readings,
                    }
                )
            dangling_tests.append(
                {"row": t, "readings": readings, "selected": tid in st.selected_tests}
            )
        dangling_readings = []
        for rid in sorted(model.readings, key=id_sort_key):
            if rid in shown:
                continue
            r = model.readings[rid]
            dangling_readings.append(
                {
                    "id": r["id"],
                    "cells": [r.get(c) for c in headers],
                    "excluded": rid in st.excluded_readings,
                }
            )

        return templates.TemplateResponse(
            request,
            "select.html",
            {
                "groups": groups,
                "headers": headers,
                "source": st.source_name,
                "dangling_tests": dangling_tests,
                "dangling_readings": dangling_readings,
            },
        )
    finally:
        model.close()


@app.post("/select")
async def select_save(request: Request):
    st = _get_session(request)
    if not st:
        return RedirectResponse("/", status_code=303)
    form = await request.form()
    model = _load(st)
    try:
        selected = set()
        for v in form.getlist("test"):
            tid = _form_id(model.tests, str(v))
            if tid in model.tests:
                selected.add(tid)
        # 剔除仅对所选试验内、且按实际归属属于所选试验的读数生效，其余静默忽略
        excluded = set()
        for v in form.getlist("excl"):
            rid = _form_id(model.readings, str(v))
            if rid in model.readings and model.reading_test.get(rid) in selected:
                excluded.add(rid)
    finally:
        model.close()
    st.selected_tests = selected
    st.excluded_readings = excluded
    return RedirectResponse("/preview", status_code=303)


@app.get("/preview", response_class=HTMLResponse)
def preview(request: Request):
    st = _get_session(request)
    if not st:
        return RedirectResponse("/", status_code=303)
    model = _load(st)
    try:
        res = resolve(model, st.selected_tests, st.excluded_readings)
        labels = {
            "group": lambda key: group_label(model, key),
            "test": lambda key: test_label(model, key),
            "reading": lambda key: reading_label(model, key),
            "calibration": lambda key: calibration_label(model, key),
        }
        pulled_tests = {
            model.reading_test[rid]
            for rid in res.pulled_in
            if model.reading_test.get(rid) in res.test_ids
        } - set(st.selected_tests)
        return templates.TemplateResponse(
            request,
            "preview.html",
            {
                "res": res,
                "model": model,
                "labels": labels,
                "pulled_tests": pulled_tests,
                "selected": sorted(st.selected_tests, key=id_sort_key),
                "excluded": sorted(st.excluded_readings, key=id_sort_key),
                "pulled_in": sorted(
                    res.pulled_in.items(), key=lambda kv: id_sort_key(kv[0])
                ),
                "source": st.source_name,
            },
        )
    finally:
        model.close()


@app.post("/download")
def download(request: Request):
    st = _get_session(request)
    if not st:
        return RedirectResponse("/", status_code=303)
    model = _load(st)
    try:
        # 下载前重新解析，防止预览之后状态被修改导致不一致
        res = resolve(model, st.selected_tests, st.excluded_readings)
    finally:
        model.close()
    if not res.ok:
        return HTMLResponse(
            "<h2>已阻止下载</h2><p>存在未解决的引用冲突（悬空或指向已剔除读数）。"
            "请返回 <a href='/preview'>预览</a> 查看引用链，"
            "或回 <a href='/select'>选择页</a> 修改。</p>",
            status_code=409,
        )
    out_path = os.path.join(st.workdir, f"subset_{int(time.time() * 1000)}.db")
    build_subset(st.db_path, out_path, res)
    name = os.path.splitext(st.source_name)[0] + "_subset.db"
    return FileResponse(
        out_path, filename=name, media_type="application/x-sqlite3"
    )
