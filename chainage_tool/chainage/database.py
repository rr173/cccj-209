"""SQLite 库结构与连接管理。

存储取向
========
所有标定、登记、压缩分段、位置依据、目标证据和反查索引都落在库文件里。
构建期与查询期进程内都不常驻全包数据：

- 构建期按登记逐条换算，单次只保留一条登记的候选标定行与其分段；
- 查询期只读取命中的索引行及其关联分段，不扫登记表、不重读交接包。

分段表只存恒定结构区间（不是逐刻度行）；目标索引以目标侧可检索的
闭编号区间存储，反查走 ``target_system + label_lo/label_hi`` 范围索引。
"""

from __future__ import annotations

import os
import sqlite3
from contextlib import contextmanager
from typing import Iterator, Optional

from .errors import BuildInProgressError, DatabaseError

SCHEMA_VERSION = 1
STATUS_BUILDING = "building"
STATUS_READY = "ready"

# 构建临时文件后缀；构建成功后原子 rename 为正式库
TEMP_SUFFIX = ".tmp"


SCHEMA_SQL = """
PRAGMA journal_mode = DELETE;

CREATE TABLE meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE axis (
    axis_id TEXT PRIMARY KEY,
    length  INTEGER NOT NULL
);

CREATE TABLE calibration (
    row_id     TEXT PRIMARY KEY,
    system     TEXT NOT NULL,
    axis_id    TEXT NOT NULL,
    segment    TEXT NOT NULL,
    lo         INTEGER NOT NULL,
    hi         INTEGER NOT NULL,
    label_lo   INTEGER NOT NULL,
    direction  INTEGER NOT NULL,
    label_min  INTEGER NOT NULL,
    label_max  INTEGER NOT NULL,
    a          INTEGER NOT NULL,
    b          INTEGER NOT NULL,
    c          INTEGER NOT NULL,
    source_line INTEGER NOT NULL
);

CREATE TABLE registration (
    reg_id        TEXT PRIMARY KEY,
    source_system TEXT NOT NULL,
    target_system TEXT NOT NULL,
    n_from        INTEGER NOT NULL,
    n_to          INTEGER NOT NULL,
    axis_id       TEXT,
    segment       TEXT,
    source_line   INTEGER NOT NULL
);

-- 压缩分段：每个 (登记,恒定结构区间) 一行，绝不按刻度展开
CREATE TABLE segment (
    seg_id        INTEGER PRIMARY KEY,
    reg_id        TEXT NOT NULL,
    n_lo          INTEGER NOT NULL,
    n_hi          INTEGER NOT NULL,
    status        TEXT NOT NULL,
    n_positions   INTEGER NOT NULL,
    FOREIGN KEY (reg_id) REFERENCES registration(reg_id)
);

-- 分段内每个独立物理位置（多位置段有多行；NO_SOURCE 段无行）
CREATE TABLE position (
    seg_id        INTEGER NOT NULL,
    pos_index     INTEGER NOT NULL,   -- 段内位置序号，按 (轴,x) 排序
    axis_id       TEXT NOT NULL,
    x_lo          INTEGER NOT NULL,   -- 该位置在分段刻度区间上的物理范围
    x_hi          INTEGER NOT NULL,
    x_slope       INTEGER NOT NULL,   -- x(n) = x_slope*n + x_intercept
    x_intercept   INTEGER NOT NULL,
    source_rows   TEXT NOT NULL,      -- JSON: [{row_id, source_line}, ...]
    PRIMARY KEY (seg_id, pos_index),
    FOREIGN KEY (seg_id) REFERENCES segment(seg_id)
);

-- 位置上的目标标牌证据（同 (段,编号) 的多标定行已合并、row_ids 全保留）
CREATE TABLE target_evidence (
    evidence_id    INTEGER PRIMARY KEY,
    seg_id         INTEGER NOT NULL,
    pos_index      INTEGER NOT NULL,
    target_segment TEXT NOT NULL,
    label_lo       INTEGER NOT NULL,  -- 该证据在分段源刻度区间上的目标编号范围
    label_hi       INTEGER NOT NULL,
    label_slope    INTEGER NOT NULL,  -- target_label(n) = label_slope*n + label_intercept
    label_intercept INTEGER NOT NULL,
    target_rows    TEXT NOT NULL,     -- JSON: [{row_id, source_line}, ...]
    FOREIGN KEY (seg_id, pos_index) REFERENCES position(seg_id, pos_index)
);

-- 目标侧反查索引：一行 = 某分段某位置上一条证据的可检索目标闭区间
CREATE TABLE target_index (
    target_index_id INTEGER PRIMARY KEY,
    target_system   TEXT NOT NULL,
    target_segment  TEXT NOT NULL,
    axis_id         TEXT NOT NULL,
    label_lo        INTEGER NOT NULL,
    label_hi        INTEGER NOT NULL,
    seg_id          INTEGER NOT NULL,
    reg_id          TEXT NOT NULL,
    evidence_id     INTEGER NOT NULL,
    FOREIGN KEY (seg_id) REFERENCES segment(seg_id),
    FOREIGN KEY (evidence_id) REFERENCES target_evidence(evidence_id)
);

-- 索引在批量落盘后统一创建（见 INDEXES_SQL），减少构建期写入放大
"""


INDEXES_SQL = """
-- 正查：按登记取分段（分段已按源编号升序）
CREATE INDEX idx_segment_reg ON segment(reg_id, n_lo);

-- 反查：目标体系 + 可检索范围，区间重叠条件由查询补充
CREATE INDEX idx_target_lookup
    ON target_index(target_system, label_lo, label_hi);

-- 反查再限定目标段
CREATE INDEX idx_target_seg
    ON target_index(target_system, target_segment, label_lo, label_hi);

-- 反查再限定轴
CREATE INDEX idx_target_axis
    ON target_index(target_system, axis_id, label_lo, label_hi);

-- 构建期按体系/轴/编号范围取标定行
CREATE INDEX idx_calib_lookup
    ON calibration(system, axis_id, label_min, label_max);
"""


def new_connection(db_path: str) -> sqlite3.Connection:
    """打开一个只读/读写连接；强制外键，ROWID 表天然落盘。"""
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    # temp_store=FILE：大查询的排序/临时表走磁盘，不挤进进程内存
    conn.execute("PRAGMA temp_store = FILE")
    conn.execute("PRAGMA cache_size = -20000")  # 每页约 4KB，上限约 80MB
    return conn


def create_build_database(path: str) -> sqlite3.Connection:
    """在临时路径创建全新库并写入 building 标记。"""
    tmp_path = path + TEMP_SUFFIX
    if os.path.exists(tmp_path):
        os.remove(tmp_path)
    conn = new_connection(tmp_path)
    conn.executescript(SCHEMA_SQL)
    conn.execute(
        "INSERT INTO meta(key, value) VALUES('status', ?)", (STATUS_BUILDING,))
    conn.commit()
    return conn, tmp_path


def finalize_build(conn: sqlite3.Connection, tmp_path: str, final_path: str,
                   version: str) -> None:
    """写 ready 标记并原子替换正式库；失败不覆盖已有库。"""
    conn.execute("UPDATE meta SET value=? WHERE key='status'", (STATUS_READY,))
    conn.execute("INSERT INTO meta(key, value) VALUES('schema_version', ?)",
                 (str(SCHEMA_VERSION),))
    conn.execute("INSERT INTO meta(key, value) VALUES('package_version', ?)",
                 (version,))
    conn.commit()
    conn.close()
    os.replace(tmp_path, final_path)  # 同文件系统原子替换


def discard_build(tmp_path: str) -> None:
    if os.path.exists(tmp_path):
        os.remove(tmp_path)


@contextmanager
def open_ready_database(db_path: str) -> Iterator[sqlite3.Connection]:
    """打开已构建库并确认处于 ready 状态，否则拒绝查询。"""
    if not os.path.isfile(db_path):
        raise DatabaseError(f"库文件不存在: {db_path}")
    conn = new_connection(db_path)
    try:
        row = conn.execute(
            "SELECT value FROM meta WHERE key='status'").fetchone()
        if row is None:
            raise DatabaseError("库内缺少构建状态标记，拒绝查询")
        if row["value"] != STATUS_READY:
            raise BuildInProgressError(
                f"库处于 {row['value']!r} 状态（构建未完成或已损坏），拒绝查询")
        yield conn
    finally:
        conn.close()


def get_meta(conn: sqlite3.Connection, key: str) -> Optional[str]:
    row = conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return row["value"] if row else None
