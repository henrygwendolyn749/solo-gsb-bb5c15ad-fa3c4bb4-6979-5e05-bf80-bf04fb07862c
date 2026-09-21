"""SQLite persistence for composition tasks.

任务状态机：pending -> running -> done（结果写入 result 列）。
后台线程与请求线程各自建立连接，配合进程内互斥锁串行化写操作。

组卷模板存于 templates 表：以 (template_id, version) 为主键，创建后不可修改；
对同一 template_id 再次创建即产生递增的新版本。
"""
from __future__ import annotations

import json
import os
import sqlite3
import threading
from typing import Optional, Tuple

DB_PATH = os.environ.get("EXAM_DB_PATH", "/data/exam.db")

_lock = threading.Lock()


def get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    os.makedirs(os.path.dirname(os.path.abspath(DB_PATH)), exist_ok=True)
    with _lock, get_conn() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS tasks (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                name        TEXT,
                status      TEXT NOT NULL DEFAULT 'pending',
                request     TEXT NOT NULL,
                result      TEXT,
                seed        INTEGER NOT NULL,
                created_at  TEXT NOT NULL DEFAULT (datetime('now'))
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS templates (
                template_id INTEGER NOT NULL,
                version     INTEGER NOT NULL,
                name        TEXT,
                config      TEXT NOT NULL,
                created_at  TEXT NOT NULL DEFAULT (datetime('now')),
                PRIMARY KEY (template_id, version)
            )
            """
        )


def create_task(name: Optional[str], request_json: str, seed: int) -> int:
    with _lock, get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO tasks (name, status, request, seed) VALUES (?, 'pending', ?, ?)",
            (name, request_json, seed),
        )
        return int(cur.lastrowid)


def update_status(task_id: int, status: str) -> None:
    with _lock, get_conn() as conn:
        conn.execute("UPDATE tasks SET status = ? WHERE id = ?", (status, task_id))


def finish_task(task_id: int, result_json: str) -> None:
    with _lock, get_conn() as conn:
        conn.execute(
            "UPDATE tasks SET status = 'done', result = ? WHERE id = ?",
            (result_json, task_id),
        )


def get_task(task_id: int) -> Optional[sqlite3.Row]:
    with get_conn() as conn:
        return conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()


# --------------------------------------------------------------------------- #
# 组卷模板（不可变版本化记录）
# --------------------------------------------------------------------------- #
def create_template(
    name: Optional[str], config_json: str, template_id: Optional[int] = None
) -> Tuple[int, int]:
    """创建模板版本，返回 (template_id, version)。

    template_id 缺省时分配全新模板（version=1）；传入已有 template_id 时在其下
    生成递增的新版本。模板写入后不再更新，保证历史版本可溯源。
    """
    with _lock, get_conn() as conn:
        if template_id is None:
            row = conn.execute(
                "SELECT COALESCE(MAX(template_id), 0) + 1 AS nid FROM templates"
            ).fetchone()
            template_id = int(row["nid"])
            version = 1
        else:
            row = conn.execute(
                "SELECT MAX(version) AS v FROM templates WHERE template_id = ?",
                (template_id,),
            ).fetchone()
            if row["v"] is None:
                raise LookupError(f"模板 {template_id} 不存在")
            version = int(row["v"]) + 1
        conn.execute(
            "INSERT INTO templates (template_id, version, name, config) VALUES (?, ?, ?, ?)",
            (template_id, version, name, config_json),
        )
        return template_id, version


def get_template(template_id: int, version: Optional[int] = None) -> Optional[sqlite3.Row]:
    """读取模板：version 缺省时返回最新版本，否则返回指定版本。"""
    with get_conn() as conn:
        if version is None:
            return conn.execute(
                "SELECT * FROM templates WHERE template_id = ? "
                "ORDER BY version DESC LIMIT 1",
                (template_id,),
            ).fetchone()
        return conn.execute(
            "SELECT * FROM templates WHERE template_id = ? AND version = ?",
            (template_id, version),
        ).fetchone()


def get_latest_version(template_id: int) -> Optional[int]:
    """模板的最新版本号；模板不存在时返回 None。"""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT MAX(version) AS v FROM templates WHERE template_id = ?",
            (template_id,),
        ).fetchone()
        return int(row["v"]) if row["v"] is not None else None
