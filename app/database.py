"""SQLite 连接与建表。

数据库路径由环境变量 MATRIX_DB_PATH 决定，默认 data/matrix.db。
连接采用 autocommit + 显式 BEGIN IMMEDIATE，配合忙等待，保证交印事务串行生效。
"""
from __future__ import annotations

import os
import sqlite3
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
DB_PATH = os.environ.get("MATRIX_DB_PATH", str(BASE_DIR / "data" / "matrix.db"))

SCHEMA = [
    """
    CREATE TABLE IF NOT EXISTS libraries (
        library_id INTEGER PRIMARY KEY AUTOINCREMENT,
        name       TEXT NOT NULL,
        edition    TEXT NOT NULL,
        priority   INTEGER NOT NULL,
        current    INTEGER NOT NULL DEFAULT 1,
        created_at TEXT NOT NULL DEFAULT (datetime('now')),
        UNIQUE(name, edition)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS glyphs (
        glyph_id   INTEGER PRIMARY KEY AUTOINCREMENT,
        library_id INTEGER NOT NULL REFERENCES libraries(library_id),
        matrix_no  TEXT NOT NULL,
        glyph_char TEXT NOT NULL,
        UNIQUE(library_id, matrix_no)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_glyphs_char ON glyphs(glyph_char)",
    """
    CREATE TABLE IF NOT EXISTS volumes (
        volume_id  INTEGER PRIMARY KEY AUTOINCREMENT,
        name       TEXT NOT NULL UNIQUE,
        created_at TEXT NOT NULL DEFAULT (datetime('now'))
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS volume_chars (
        volume_id    INTEGER NOT NULL REFERENCES volumes(volume_id) ON DELETE CASCADE,
        glyph_char   TEXT NOT NULL,
        source_order INTEGER NOT NULL,
        PRIMARY KEY (volume_id, glyph_char)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS active_matrices (
        volume_id INTEGER NOT NULL REFERENCES volumes(volume_id) ON DELETE CASCADE,
        glyph_id  INTEGER NOT NULL REFERENCES glyphs(glyph_id),
        PRIMARY KEY (volume_id, glyph_id)
    )
    """,
    "CREATE TABLE IF NOT EXISTS submit_seq (seq INTEGER NOT NULL)",
    """
    CREATE TABLE IF NOT EXISTS orders (
        order_no       TEXT PRIMARY KEY,
        seq            INTEGER NOT NULL UNIQUE,
        volume_id      INTEGER NOT NULL,
        volume_name    TEXT NOT NULL,
        idem_key       TEXT,
        fingerprint    TEXT NOT NULL,
        payload_json   TEXT NOT NULL,
        created_at     TEXT NOT NULL DEFAULT (datetime('now'))
    )
    """,
    """
    CREATE UNIQUE INDEX IF NOT EXISTS idx_orders_idem
    ON orders(idem_key) WHERE idem_key IS NOT NULL
    """,
    # 新单共需存档（瘦身）：每单只存共需池的实际增减，配合 orders 中
    # 不再整存 pool_before/pool_after 的瘦身 payload 使用。
    # 旧库打开时由 CREATE TABLE IF NOT EXISTS 自动补齐，旧单不受影响。
    """
    CREATE TABLE IF NOT EXISTS pool_deltas (
        seq          INTEGER PRIMARY KEY,
        order_no     TEXT NOT NULL,
        added_json   TEXT NOT NULL,
        removed_json TEXT NOT NULL
    )
    """,
    # 周期性完整共需清单：每 32 张生效新单最多额外存一份（存该单生效前的
    # 共需并集），按单号重取时从最近一份快照顺序应用增减即可还原。
    """
    CREATE TABLE IF NOT EXISTS pool_snapshots (
        seq       INTEGER PRIMARY KEY,
        pool_json TEXT NOT NULL
    )
    """,
]


def connect(db_path: str | None = None) -> sqlite3.Connection:
    path = db_path or DB_PATH
    parent = Path(path).parent
    parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=30, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=30000")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


def init_db(db_path: str | None = None) -> None:
    conn = connect(db_path)
    try:
        for stmt in SCHEMA:
            conn.execute(stmt)
        conn.execute("INSERT OR IGNORE INTO submit_seq(seq) VALUES (0)")
    finally:
        conn.close()
