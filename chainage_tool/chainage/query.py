"""只读查询：正查（登记 -> 换算明细）与反查（目标标牌 -> 登记）。

两类查询都只读 ready 库，不重读交接包、不扫描整张登记表：

- 正查走 ``idx_segment_reg`` 按登记取分段，再按可选编号范围裁切；
- 反查走 ``idx_target_lookup``（目标体系 + 可检索闭区间），命中后按
  实际标牌反算源刻度裁切，并还原方向；原分段状态原样返回，即使只
  命中某个有争议（多源位置）的刻度，也不改写为唯一。
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any, Dict, List, Optional

from .database import open_ready_database
from .errors import DatabaseError


# --------------------------------------------------------------------------- #
# 行还原
# --------------------------------------------------------------------------- #
def _load_positions(conn: sqlite3.Connection, seg_id: int) -> List[dict]:
    positions = []
    for prow in conn.execute(
            "SELECT * FROM position WHERE seg_id=? ORDER BY pos_index",
            (seg_id,)):
        ev_rows = conn.execute(
            "SELECT * FROM target_evidence WHERE seg_id=? AND pos_index=? "
            "ORDER BY target_segment, label_lo",
            (seg_id, prow["pos_index"])).fetchall()
        positions.append({
            "axis_id": prow["axis_id"],
            "x_lo": prow["x_lo"],
            "x_hi": prow["x_hi"],
            "x_slope": prow["x_slope"],
            "x_intercept": prow["x_intercept"],
            "source_rows": json.loads(prow["source_rows"]),
            "evidence_rows": ev_rows,
        })
    return positions


def _position_dict(p: dict, n_lo: int, n_hi: int,
                   matched_evidence_ids: Optional[set] = None) -> Dict[str, Any]:
    """把一个位置在 [n_lo,n_hi] 上的精确区间展开为可导出的结构化记录。

    反查（``matched_evidence_ids`` 非空）时只做命中标记、不删除证据：
    多源位置段上未命中的候选位置与其目标标牌必须完整带回，班组才能
    看到每次拒绝自动入仓的全部测量依据。
    """
    x_lo = p["x_slope"] * n_lo + p["x_intercept"]
    x_hi = p["x_slope"] * n_hi + p["x_intercept"]
    evidence = []
    for er in p["evidence_rows"]:
        lab_lo = er["label_slope"] * n_lo + er["label_intercept"]
        lab_hi = er["label_slope"] * n_hi + er["label_intercept"]
        evidence.append({
            "target_segment": er["target_segment"],
            "target_label_lo": min(lab_lo, lab_hi),
            "target_label_hi": max(lab_lo, lab_hi),
            "label_slope": er["label_slope"],
            "label_intercept": er["label_intercept"],
            "target_rows": json.loads(er["target_rows"]),
            "evidence_id": er["evidence_id"],
            # 正查时没有"命中"概念，统一为 True；反查时如实标记
            "matched": (matched_evidence_ids is None
                        or er["evidence_id"] in matched_evidence_ids),
        })
    return {
        "axis_id": p["axis_id"],
        "x_lo": min(x_lo, x_hi),
        "x_hi": max(x_lo, x_hi),
        "x_slope": p["x_slope"],
        "x_intercept": p["x_intercept"],
        "source_rows": p["source_rows"],
        "target_evidence": evidence,
    }


def _registration_dict(conn: sqlite3.Connection, reg_id: str) -> Optional[dict]:
    r = conn.execute(
        "SELECT * FROM registration WHERE reg_id=?", (reg_id,)).fetchone()
    if r is None:
        return None
    return dict(r)


# --------------------------------------------------------------------------- #
# 正查
# --------------------------------------------------------------------------- #
def forward_query(
    db_path: str,
    reg_id: str,
    n_from: Optional[int] = None,
    n_to: Optional[int] = None,
) -> Dict[str, Any]:
    """按登记 ID 取完整换算明细；可选源编号闭区间做裁切。

    返回结构包含：登记信息、交覆盖的压缩分段（每段带全部位置与证据）、
    以及可自动入仓（RESOLVED）子区间清单。
    """
    with open_ready_database(db_path) as conn:
        version = _meta(conn, "package_version")
        reg = _registration_dict(conn, reg_id)
        if reg is None:
            raise DatabaseError(f"登记 ID 不存在: {reg_id}")

        lo = reg["n_from"] if n_from is None else n_from
        hi = reg["n_to"] if n_to is None else n_to
        if lo > hi:
            raise DatabaseError(f"查询范围要求 from <= to，实际 {lo} > {hi}")
        if hi < reg["n_from"] or lo > reg["n_to"]:
            raise DatabaseError(
                f"查询范围 [{lo},{hi}] 与登记区间 "
                f"[{reg['n_from']},{reg['n_to']}] 无交集")

        seg_rows = conn.execute(
            "SELECT * FROM segment WHERE reg_id=? AND n_hi >= ? AND n_lo <= ? "
            "ORDER BY n_lo",
            (reg_id, lo, hi)).fetchall()

        segments_out: List[dict] = []
        warehousable: List[dict] = []
        for sr in seg_rows:
            c_lo = max(sr["n_lo"], lo)
            c_hi = min(sr["n_hi"], hi)
            positions = [
                _position_dict(p, c_lo, c_hi)
                for p in _load_positions(conn, sr["seg_id"])
            ]
            item = {
                "seg_id": sr["seg_id"],
                "source_label_lo": c_lo,
                "source_label_hi": c_hi,
                "status": sr["status"],
                "n_positions": sr["n_positions"],
                "positions": positions,
            }
            segments_out.append(item)
            if sr["status"] == "RESOLVED":
                warehousable.append({
                    "source_label_lo": c_lo,
                    "source_label_hi": c_hi,
                    "positions": positions,
                })

        return {
            "package_version": version,
            "registration": reg,
            "query_range": {"n_from": lo, "n_to": hi},
            "segments": segments_out,
            "warehousable_segments": warehousable,
        }


# --------------------------------------------------------------------------- #
# 反查
# --------------------------------------------------------------------------- #
def reverse_query(
    db_path: str,
    target_system: str,
    n: Optional[int] = None,
    n_from: Optional[int] = None,
    n_to: Optional[int] = None,
    axis_id: Optional[str] = None,
    target_segment: Optional[str] = None,
) -> Dict[str, Any]:
    """按目标体系的标牌点/区间选查已具备目标证据的登记行。

    索引层先用可检索闭区间过滤（目标编号重叠），命中后对每条索引项
    用仿射关系反算实际命中的源刻度区间，再回到原分段取该区间上的完整
    候选位置与原状态。只命中多位置段的一部分刻度时，状态仍是
    MULTIPLE_SOURCE，绝不改写成唯一。
    """
    if n is not None and (n_from is not None or n_to is not None):
        raise DatabaseError("点号 n 与区间 from/to 不能同时给出")
    if n is not None:
        q_lo = q_hi = n
    else:
        q_lo = n_from
        q_hi = n_to
        if q_lo is None or q_hi is None:
            raise DatabaseError("反查必须给出点号 n 或区间 [from,to]")
        if q_lo > q_hi:
            raise DatabaseError(f"反查范围要求 from <= to，实际 {q_lo} > {q_hi}")

    with open_ready_database(db_path) as conn:
        version = _meta(conn, "package_version")
        sql = [
            "SELECT ti.*, s.n_lo AS seg_n_lo, s.n_hi AS seg_n_hi, s.status",
            "FROM target_index ti JOIN segment s ON s.seg_id = ti.seg_id",
            "WHERE ti.target_system = ?",
            # 可检索区间与查询闭区间重叠（覆盖点查询）
            "AND ti.label_hi >= ? AND ti.label_lo <= ?",
        ]
        params: List[Any] = [target_system, q_lo, q_hi]
        if target_segment is not None:
            sql.append("AND ti.target_segment = ?")
            params.append(target_segment)
        if axis_id is not None:
            sql.append("AND ti.axis_id = ?")
            params.append(axis_id)
        sql.append("ORDER BY ti.reg_id, ti.seg_id, ti.target_index_id")
        hits = conn.execute(" ".join(sql), params).fetchall()

        # 同一分段可能被多条证据命中：按 (reg, seg) 聚合，分别保留命中刻度
        grouped: Dict[tuple, dict] = {}
        for h in hits:
            key = (h["reg_id"], h["seg_id"])
            entry = grouped.get(key)
            if entry is None:
                entry = {
                    "reg_id": h["reg_id"],
                    "seg_id": h["seg_id"],
                    "status": h["status"],
                    "seg_n_lo": h["seg_n_lo"],
                    "seg_n_hi": h["seg_n_hi"],
                    "matched_evidence_ids": set(),
                    "match_n_lo": None,
                    "match_n_hi": None,
                }
                grouped[key] = entry
            entry["matched_evidence_ids"].add(h["evidence_id"])

            # 目标标牌 -> 源刻度反算（取该证据的仿射参数）
            er = conn.execute(
                "SELECT * FROM target_evidence WHERE evidence_id=?",
                (h["evidence_id"],)).fetchone()
            # 该索引项实际覆盖的目标闭区间与查询区间求交
            il, ih = max(h["label_lo"], q_lo), min(h["label_hi"], q_hi)
            # label = slope*n + intercept => n = (label - intercept)/slope
            slope = er["label_slope"]  # ±1
            src_lo = (il - er["label_intercept"]) // slope
            src_hi = (ih - er["label_intercept"]) // slope
            src_lo, src_hi = min(src_lo, src_hi), max(src_lo, src_hi)
            # 约束在原分段源刻度范围内
            src_lo = max(src_lo, h["seg_n_lo"])
            src_hi = min(src_hi, h["seg_n_hi"])
            entry["match_n_lo"] = src_lo if entry["match_n_lo"] is None \
                else min(entry["match_n_lo"], src_lo)
            entry["match_n_hi"] = src_hi if entry["match_n_hi"] is None \
                else max(entry["match_n_hi"], src_hi)

        results: List[dict] = []
        for (reg_id, seg_id), entry in sorted(
                grouped.items(), key=lambda kv: (kv[0][0], kv[0][1])):
            reg = _registration_dict(conn, reg_id)
            positions = [
                _position_dict(p, entry["match_n_lo"], entry["match_n_hi"],
                               matched_evidence_ids=entry["matched_evidence_ids"])
                for p in _load_positions(conn, seg_id)
            ]
            # 标记每个位置上命中的证据；未命中位置仍完整返回（保留候选）
            results.append({
                "package_version": version,
                "reg_id": reg_id,
                "registration": reg,
                "seg_id": seg_id,
                "segment_source_label_lo": entry["seg_n_lo"],
                "segment_source_label_hi": entry["seg_n_hi"],
                "matched_source_label_lo": entry["match_n_lo"],
                "matched_source_label_hi": entry["match_n_hi"],
                "original_status": entry["status"],
                "positions": positions,
            })

        return {
            "package_version": version,
            "query": {
                "target_system": target_system,
                "target_label_from": q_lo,
                "target_label_to": q_hi,
                "axis_id": axis_id,
                "target_segment": target_segment,
            },
            "matches": results,
        }


def list_registrations(db_path: str) -> List[dict]:
    """列出库内全部登记头信息（管理用途；反查不走这条路径）。"""
    with open_ready_database(db_path) as conn:
        rows = conn.execute(
            "SELECT * FROM registration ORDER BY reg_id").fetchall()
        return [dict(r) for r in rows]


def get_calibration(db_path: str, row_id: str) -> Optional[dict]:
    """按稳定行标识取标定行完整内容，用于班组复核拒绝依据。"""
    with open_ready_database(db_path) as conn:
        r = conn.execute(
            "SELECT * FROM calibration WHERE row_id=?", (row_id,)).fetchone()
        return dict(r) if r else None


def explain_reverse_plan(db_path: str, target_system: str,
                         q_lo: int, q_hi: int) -> List[dict]:
    """返回反查 SQL 的 EXPLAIN QUERY PLAN，验收时证明走索引而非全表扫。"""
    with open_ready_database(db_path) as conn:
        rows = conn.execute(
            "EXPLAIN QUERY PLAN "
            "SELECT ti.* FROM target_index ti "
            "WHERE ti.target_system = ? AND ti.label_hi >= ? "
            "AND ti.label_lo <= ?",
            (target_system, q_lo, q_hi)).fetchall()
        return [dict(r) for r in rows]


def _meta(conn: sqlite3.Connection, key: str) -> Optional[str]:
    r = conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return r["value"] if r else None
