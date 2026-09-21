"""SQLite persistence for composition tasks.

任务状态机：pending -> running -> done（结果写入 result 列）。
后台线程与请求线程各自建立连接，配合进程内互斥锁串行化写操作。

组卷模板（templates 表）：创建后不可变——只提供插入与查询，没有任何更新
接口；修改约束配置只能通过创建新版本（同一 root_id 谱系下 version 递增）。
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
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                name        TEXT,
                config      TEXT NOT NULL,
                version     INTEGER NOT NULL,
                root_id     INTEGER,
                base_id     INTEGER,
                created_at  TEXT NOT NULL DEFAULT (datetime('now'))
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
# 组卷模板（不可变；版本谱系以 root_id 标识，version 在谱系内单调递增）
# --------------------------------------------------------------------------- #
def create_template(
    name: Optional[str], config_json: str, base: Optional[sqlite3.Row]
) -> Tuple[int, int]:
    """插入一条模板记录，返回 (template_id, version)。

    base 为 None 时创建全新谱系（version=1，root_id 指向自身）；
    否则在 base 所在谱系内取当前最大版本号 +1，base_id 记录派生来源。
    模板记录只插不改，天然满足“创建后不可原地修改”。
    """
    with _lock, get_conn() as conn:
        if base is None:
            cur = conn.execute(
                "INSERT INTO templates (name, config, version, root_id, base_id)"
                " VALUES (?, ?, 1, -1, NULL)",
                (name, config_json),
            )
            template_id = int(cur.lastrowid)
            conn.execute(
                "UPDATE templates SET root_id = ? WHERE id = ?",
                (template_id, template_id),
            )
            return template_id, 1

        root_id = int(base["root_id"])
        row = conn.execute(
            "SELECT COALESCE(MAX(version), 0) AS v FROM templates WHERE root_id = ?",
            (root_id,),
        ).fetchone()
        version = int(row["v"]) + 1
        cur = conn.execute(
            "INSERT INTO templates (name, config, version, root_id, base_id)"
            " VALUES (?, ?, ?, ?, ?)",
            (name, config_json, version, root_id, int(base["id"])),
        )
        return int(cur.lastrowid), version


def get_template(template_id: int) -> Optional[sqlite3.Row]:
    with get_conn() as conn:
        return conn.execute(
            "SELECT * FROM templates WHERE id = ?", (template_id,)
        ).fetchone()
