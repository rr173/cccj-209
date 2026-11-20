"""入仓构建流程。

流程（任一步失败都不会覆盖已有正式库）::

    1. load_package：清单/标定/登记全量结构化校验，错误定位原行并聚合抛出
    2. 在 <db>.tmp 临时库写轴、标定行、登记行
    3. 建标定查找索引，逐条登记做分段压缩换算（工作集只限该登记的候选行）
    4. 分段/位置/证据/目标反查索引批量落盘
    5. 写 ready 标记，os.replace 原子替换正式库

工作内存边界：进程内不保留全包标定，只保留当前登记命中的源/目标
标定行与它产生的分段；其余全部在 SQLite 文件（可在磁盘上整理）。
"""

from __future__ import annotations

import bisect
import json
import os
import sqlite3
from dataclasses import dataclass
from typing import Dict, List, Optional, Sequence, Tuple

from .database import (
    INDEXES_SQL,
    create_build_database,
    discard_build,
    finalize_build,
)
from .engine import Evidence, Position, Segment, convert_registration
from .errors import PackageValidationError
from .models import CalibRow, Package, load_package


@dataclass
class BuildStats:
    package_version: str
    axes: int
    calibration_rows: int
    registration_rows: int
    segments: int
    positions: int
    evidence: int
    target_index_rows: int
    db_path: str

    def to_dict(self) -> dict:
        return {
            "package_version": self.package_version,
            "axes": self.axes,
            "calibration_rows": self.calibration_rows,
            "registration_rows": self.registration_rows,
            "segments": self.segments,
            "positions": self.positions,
            "evidence": self.evidence,
            "target_index_rows": self.target_index_rows,
            "db_path": self.db_path,
        }


# --------------------------------------------------------------------------- #
# 候选标定行工作集
#
# 构建开始时按 (体系 -> 轴 -> 标定行) 一次性载入并预排序。标定行是有界
# 测量数据（每行固定字段），整体驻留内存的规模随标定行数线性增长且每行
# 很小；12k 行约 5 MiB。登记处理过程中不再执行任何候选行 SQL，工作量只
# 随该登记真正交叠的候选行增长。若标定规模超过内存预算，可在此层替换为
# SQLite 临时表 + 键集分页（落盘整理），接口保持不变。
# --------------------------------------------------------------------------- #
def _load_calib_workset(
    conn: sqlite3.Connection,
) -> Dict[str, Dict[str, List[CalibRow]]]:
    workset: Dict[str, Dict[str, List[CalibRow]]] = {}
    for r in conn.execute("SELECT * FROM calibration ORDER BY row_id"):
        row = _row_to_calib(r)
        workset.setdefault(row.system, {}).setdefault(
            row.axis_id, []).append(row)
    for by_axis in workset.values():
        for rows in by_axis.values():
            rows.sort(key=lambda r: (r.lo, r.hi, r.row_id))
    return workset


# 每个 (体系,轴) 的编号区间查找索引，随单次构建创建并释放：
# label_min 升序序列，配合 bisect 跳过 label_min > 登记上限的行。
def _build_label_index(
    workset: Dict[str, Dict[str, List[CalibRow]]]
) -> Dict[Tuple[str, str], Tuple[List[CalibRow], List[int]]]:
    index: Dict[Tuple[str, str], Tuple[List[CalibRow], List[int]]] = {}
    for system, by_axis in workset.items():
        for axis_id, rows in by_axis.items():
            ordered = sorted(rows, key=lambda r: r.label_min)
            index[(system, axis_id)] = (ordered, [r.label_min for r in ordered])
    return index


def _source_candidates(
    workset: Dict[str, Dict[str, List[CalibRow]]],
    label_index: Dict[Tuple[str, str], Tuple[List[CalibRow], List[int]]],
    reg,
) -> List[CalibRow]:
    """按登记限定取源候选行，工作量随真正交叠的行数而非全包行数增长。"""
    by_axis = workset.get(reg.source_system, {})
    axes = (reg.axis_id,) if reg.axis_id is not None else tuple(by_axis)
    out: List[CalibRow] = []
    for axis_id in axes:
        if axis_id not in by_axis:
            continue
        rows, mins = label_index[(reg.source_system, axis_id)]
        # label_min 升序：其后所有行 label_min 都 > 登记上限，整体截断
        upper = bisect.bisect_right(mins, reg.n_to)
        for r in rows[:upper]:
            if r.label_max < reg.n_from:
                continue
            if reg.segment is not None and r.segment != reg.segment:
                continue
            out.append(r)
    return out


def _target_candidates(
    workset: Dict[str, Dict[str, List[CalibRow]]],
    target_system: str, axis_ids,
) -> Dict[str, List[CalibRow]]:
    by_axis = workset.get(target_system, {})
    return {ax: by_axis[ax] for ax in axis_ids if ax in by_axis}


def _target_lo_index(
    workset: Dict[str, Dict[str, List[CalibRow]]],
    cache: Dict[Tuple[str, str], List[int]],
    target_system: str, axis_id: str,
) -> List[int]:
    """构建级缓存的目标行物理 lo 序列（工作集已按 lo 预排序）。"""
    key = (target_system, axis_id)
    los = cache.get(key)
    if los is None:
        los = [r.lo for r in workset[target_system][axis_id]]
        cache[key] = los
    return los


def _row_to_calib(r: sqlite3.Row) -> CalibRow:
    return CalibRow(
        row_id=r["row_id"], system=r["system"], axis_id=r["axis_id"],
        segment=r["segment"], lo=r["lo"], hi=r["hi"],
        label_lo=r["label_lo"], direction=r["direction"],
        label_min=r["label_min"], label_max=r["label_max"],
        a=r["a"], b=r["b"], c=r["c"], source_line=r["source_line"])


def _row_refs_json(ids_lines: Sequence[Tuple[str, int]]) -> str:
    """固定结构 [{row_id, source_line}] 的紧凑 JSON。

    结构手写以避开对 5 万+ 小对象的通用编码开销；字符串部分仍由
    ``json.dumps`` 转义，保证控制字符/引号等内容合法。source_line
    在加载期已校验为整数。
    """
    parts = [
        '{"row_id":' + json.dumps(rid, ensure_ascii=False)
        + ',"source_line":' + str(line) + "}"
        for rid, line in ids_lines
    ]
    return "[" + ",".join(parts) + "]"


# --------------------------------------------------------------------------- #
# 分段结果落盘（批量 executemany；工作集只限当前登记）
# --------------------------------------------------------------------------- #
def _insert_segments(
    conn: sqlite3.Connection,
    reg,
    segments: List[Segment],
    counters: Dict[str, int],
    calib_cache: Dict[str, CalibRow],
) -> None:
    def calib(row_id: str) -> CalibRow:
        # 构建期按登记预取的工作集缓存，避免逐位置/证据单行查询（N+1）
        if row_id not in calib_cache:
            calib_cache[row_id] = _find_calib(conn, row_id)
        return calib_cache[row_id]

    seg_rows: List[tuple] = []
    pos_rows: List[tuple] = []
    ev_rows: List[tuple] = []
    idx_rows: List[tuple] = []
    # 显式分配主键，证据与反查索引才能在同一批次内关联
    next_seg_id = counters.get("_next_seg_id")
    next_ev_id = counters.get("_next_ev_id")
    if next_seg_id is None:
        next_seg_id = conn.execute(
            "SELECT COALESCE(MAX(seg_id),0)+1 FROM segment").fetchone()[0]
        next_ev_id = conn.execute(
            "SELECT COALESCE(MAX(evidence_id),0)+1 FROM target_evidence"
        ).fetchone()[0]

    for seg in segments:
        seg_id = next_seg_id
        next_seg_id += 1
        seg_rows.append((seg.reg_id, seg.n_lo, seg.n_hi,
                         seg.status, len(seg.positions)))
        counters["segments"] += 1

        for pos_index, pos in enumerate(sorted(
                seg.positions, key=lambda p: (p.axis_id, p.x))):
            # 取该位置任一源行的仿射参数：同组行在整段上共享 x(n) 关系
            src_row = calib(pos.source_row_ids[0])
            x_lo = src_row.a * seg.n_lo + src_row.c
            x_hi = src_row.a * seg.n_hi + src_row.c
            source_json = _row_refs_json(
                list(zip(pos.source_row_ids, pos.source_lines)))
            pos_rows.append((seg_id, pos_index, pos.axis_id, x_lo, x_hi,
                             src_row.a, src_row.c, source_json))
            counters["positions"] += 1

            for ev in pos.target_evidence:
                tgt_row = calib(ev.row_ids[0])
                # 目标编号关于源编号的仿射关系：
                # x(n)=a_s*n+c_s ; label=a_t*x+b_t
                #      => label = a_t*a_s*n + (a_t*c_s+b_t)
                slope = tgt_row.a * src_row.a
                intercept = tgt_row.a * src_row.c + tgt_row.b
                lab_lo = slope * seg.n_lo + intercept
                lab_hi = slope * seg.n_hi + intercept
                ev_label_lo, ev_label_hi = sorted((lab_lo, lab_hi))
                target_json = _row_refs_json(
                    list(zip(ev.row_ids, ev.source_lines)))
                evidence_id = next_ev_id
                next_ev_id += 1
                ev_rows.append((evidence_id, seg_id, pos_index,
                                ev.target_segment, ev_label_lo, ev_label_hi,
                                slope, intercept, target_json))
                idx_rows.append((reg.target_system, ev.target_segment,
                                 pos.axis_id, ev_label_lo, ev_label_hi,
                                 seg_id, reg.reg_id, evidence_id))
                counters["evidence"] += 1
                counters["target_index_rows"] += 1

    conn.executemany(
        "INSERT INTO segment(reg_id, n_lo, n_hi, status, n_positions) "
        "VALUES (?,?,?,?,?)", seg_rows)
    conn.executemany(
        "INSERT INTO position(seg_id, pos_index, axis_id, x_lo, x_hi, "
        "x_slope, x_intercept, source_rows) VALUES (?,?,?,?,?,?,?,?)",
        pos_rows)
    conn.executemany(
        "INSERT INTO target_evidence(evidence_id, seg_id, pos_index, "
        "target_segment, label_lo, label_hi, label_slope, label_intercept, "
        "target_rows) VALUES (?,?,?,?,?,?,?,?,?)", ev_rows)
    conn.executemany(
        "INSERT INTO target_index(target_system, target_segment, axis_id, "
        "label_lo, label_hi, seg_id, reg_id, evidence_id) "
        "VALUES (?,?,?,?,?,?,?,?)", idx_rows)
    counters["_next_seg_id"] = next_seg_id
    counters["_next_ev_id"] = next_ev_id


def _find_calib(conn: sqlite3.Connection, row_id: str) -> CalibRow:
    r = conn.execute(
        "SELECT * FROM calibration WHERE row_id=?", (row_id,)).fetchone()
    return _row_to_calib(r)


# --------------------------------------------------------------------------- #
# 入口
# --------------------------------------------------------------------------- #
def build(package_dir: str, db_path: str) -> BuildStats:
    """校验交接包并构建离线库；失败抛异常且不动已有正式库。"""
    # 1) 全包校验（任何错误都在打开临时库之前聚合抛出）
    pkg: Package = load_package(package_dir)

    tmp_path = db_path + ".tmp"
    conn: Optional[sqlite3.Connection] = None
    try:
        # 2) 临时库 + 基础数据
        conn, tmp_path = create_build_database(db_path)
        cur = conn.cursor()
        cur.executemany(
            "INSERT INTO axis(axis_id, length) VALUES (?,?)",
            [(ax.axis_id, ax.length) for ax in pkg.axes.values()])
        cur.executemany(
            "INSERT INTO calibration(row_id, system, axis_id, segment, lo, hi,"
            " label_lo, direction, label_min, label_max, a, b, c, source_line) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            [(r.row_id, r.system, r.axis_id, r.segment, r.lo, r.hi, r.label_lo,
              r.direction, r.label_min, r.label_max, r.a, r.b, r.c,
              r.source_line) for r in pkg.calibration])
        cur.executemany(
            "INSERT INTO registration(reg_id, source_system, target_system, "
            "n_from, n_to, axis_id, segment, source_line) VALUES (?,?,?,?,?,?,?,?)",
            [(r.reg_id, r.source_system, r.target_system, r.n_from, r.n_to,
              r.axis_id, r.segment, r.source_line)
             for r in pkg.registration])
        conn.commit()

        # 3) 索引先于换算建立，候选标定行走索引而不是全表扫
        cur.executescript(INDEXES_SQL)

        # 4) 逐条登记换算并落盘（候选行全部来自有界工作集，无逐登记 SQL）。
        #    全部写入在同一事务内提交，避免每条登记触发一次磁盘同步。
        workset = _load_calib_workset(conn)
        label_index = _build_label_index(workset)
        target_lo_cache: Dict[Tuple[str, str], List[int]] = {}
        counters = {"segments": 0, "positions": 0,
                    "evidence": 0, "target_index_rows": 0}
        conn.execute("BEGIN")
        for reg in pkg.registration:
            src = _source_candidates(workset, label_index, reg)
            src_axes = sorted({r.axis_id for r in src})
            target_by_axis = _target_candidates(
                workset, reg.target_system, src_axes)
            target_lo_index = {
                ax: _target_lo_index(workset, target_lo_cache,
                                     reg.target_system, ax)
                for ax in target_by_axis
            }
            segments = convert_registration(
                reg, src, target_by_axis, target_lo_index)
            # 工作集缓存：仅本登记命中的源/目标标定行，随登记迭代而释放
            calib_cache: Dict[str, CalibRow] = {r.row_id: r for r in src}
            for rows in target_by_axis.values():
                for r in rows:
                    calib_cache.setdefault(r.row_id, r)
            _insert_segments(conn, reg, segments, counters, calib_cache)
        conn.commit()

        # 5) 原子替换
        stats = BuildStats(
            package_version=pkg.version,
            axes=len(pkg.axes),
            calibration_rows=len(pkg.calibration),
            registration_rows=len(pkg.registration),
            db_path=os.path.abspath(db_path),
            **{k: v for k, v in counters.items() if not k.startswith("_")},
        )
        finalize_build(conn, tmp_path, db_path, pkg.version)
        return stats
    except Exception:
        if conn is not None:
            conn.close()
        discard_build(tmp_path)
        raise


def validate_only(package_dir: str) -> dict:
    """只做交接包校验，返回结构摘要；校验失败抛 PackageValidationError。"""
    pkg = load_package(package_dir)
    return {
        "package_version": pkg.version,
        "axes": len(pkg.axes),
        "calibration_rows": len(pkg.calibration),
        "registration_rows": len(pkg.registration),
        "systems": sorted(pkg.systems.keys()),
    }
