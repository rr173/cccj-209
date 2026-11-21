"""采集包查询：窗口采样表、未归属记录、单条回看、摘要。

所有查询只读采集包 SQLite 库，不再读取原始分卷；库未处于 ready
状态（构建未完成或已损坏）时拒绝查询。
"""

from __future__ import annotations

import json
import os
import sqlite3
from contextlib import contextmanager
from typing import Iterator, List, Optional

from .acquisition import (
    CAT_PARTIAL_OVERLAP,
    KIND_PHOTO,
    KIND_READING,
    KIND_RECEIPT,
    STATUS_READY,
    decode_id,
    frac_str,
)
from .errors import BuildInProgressError, DatabaseError
from fractions import Fraction


@contextmanager
def open_acq(db_path: str) -> Iterator[sqlite3.Connection]:
    """打开已交付采集包并确认 ready 状态，否则拒绝查询。"""
    if not os.path.isfile(db_path):
        raise DatabaseError(f"采集包不存在: {db_path}")
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute(
            "SELECT value FROM meta WHERE key='status'").fetchone()
        if row is None:
            raise DatabaseError("库内缺少构建状态标记，拒绝查询")
        if row["value"] != STATUS_READY:
            raise BuildInProgressError(
                f"采集包处于 {row['value']!r} 状态（构建未完成或已损坏），"
                f"拒绝查询")
        yield conn
    finally:
        conn.close()


def _meta_map(conn) -> dict:
    return {r["key"]: r["value"] for r in conn.execute(
        "SELECT key, value FROM meta")}


def _frac(num, den):
    return frac_str(Fraction(int(num), int(den)))


# --------------------------------------------------------------------------- #
# 摘要
# --------------------------------------------------------------------------- #
def acq_summary(conn) -> dict:
    meta = _meta_map(conn)
    stats = {k[5:]: int(v) for k, v in meta.items() if k.startswith("stat.")}
    return {
        "package_version": meta.get("package_version"),
        "tool": meta.get("tool"),
        "status": meta.get("status"),
        "schema_version": meta.get("schema_version"),
        "built_at_utc": meta.get("built_at_utc"),
        "chainage_lib_version": meta.get("chainage_lib_version"),
        "inputs": [dict(kind=r["kind"], name=r["name"], sha256=r["sha256"])
                   for r in conn.execute(
                       "SELECT kind, name, sha256 FROM inputs "
                       "ORDER BY kind, name")],
        "stats": stats,
    }


# --------------------------------------------------------------------------- #
# 窗口采样表
# --------------------------------------------------------------------------- #
def _channel_types(conn) -> dict:
    return {(r["device"], r["channel"]): r["ctype"]
            for r in conn.execute(
                "SELECT device, channel, ctype FROM device_channel")}


def _window_record_entry(row, expected_set) -> dict:
    entry = {
        "record_id": row["id"],
        "kind": row["kind"],
        "device": decode_id(row["device"]),
        "channel": decode_id(row["channel"]),
        "expected": (row["device"], row["channel"]) in expected_set,
    }
    if row["kind"] == KIND_READING:
        entry["utc"] = _frac(row["utc_num"], row["utc_den"])
        entry["value"] = str(row["value"])
    else:
        entry["exposure_utc"] = {
            "open": _frac(row["open_num"], row["open_den"]),
            "close": _frac(row["close_num"], row["close_den"]),
        }
        entry["resource"] = row["resource"]
    return entry


def _window_object(conn, w, ch_types, lib_version, with_records) -> dict:
    expected = json.loads(w["expected_channels"])
    expected_set = {(d, c) for d, c in expected}
    counts = list(conn.execute(
        "SELECT device, channel, kind, n FROM window_channel_count "
        "WHERE window_id=? ORDER BY device, channel, kind", (w["id"],)))
    present = {(c["device"], c["channel"]) for c in counts}
    exp_list = [{
        "device": decode_id(d),
        "channel": decode_id(c),
        "type": ch_types.get((d, c)),
        "has_evidence": (d, c) in present,
    } for d, c in expected]
    obj = {
        "round": decode_id(w["round_key"]),
        "window": decode_id(w["window_key"]),
        "interval_utc": {"start": str(w["start_utc"]),
                         "end": str(w["end_utc"]),
                         "half_open": True},
        "registration": w["registration"],
        "chainage_lib_version": lib_version,
        "expected_channels": exp_list,
        "missing_expected_channels": [
            {"device": e["device"], "channel": e["channel"]}
            for e in exp_list if not e["has_evidence"]],
        "channel_counts": [{
            "device": decode_id(c["device"]),
            "channel": decode_id(c["channel"]),
            "kind": c["kind"],
            "n": c["n"],
            "expected": (c["device"], c["channel"]) in expected_set,
        } for c in counts],
        "totals": {
            "readings": sum(c["n"] for c in counts
                            if c["kind"] == KIND_READING),
            "photos": sum(c["n"] for c in counts
                          if c["kind"] == KIND_PHOTO),
            "records": sum(c["n"] for c in counts),
        },
    }
    if with_records:
        rows = conn.execute(
            "SELECT r.id, r.kind, r.device, r.channel, r.value, r.resource,"
            " a.utc_num, a.utc_den, a.open_num, a.open_den,"
            " a.close_num, a.close_den "
            "FROM attribution at "
            "JOIN sample_record r ON r.id = at.record_id "
            "JOIN alignment a ON a.record_id = r.id "
            "WHERE at.window_id=? ORDER BY r.id", (w["id"],))
        obj["records"] = [_window_record_entry(r, expected_set) for r in rows]
    return obj


def acq_windows(conn, round_c: Optional[str] = None,
                window_c: Optional[str] = None, limit: int = 200,
                offset: int = 0, with_records: bool = True) -> dict:
    """按轮次读窗口采样表；空窗口也列出（records 为空、期望通道标缺）。

    窗口行存在且包状态 ready 即表示该窗口已处理；records 为空就是
    缺读数，而不是没处理。
    """
    where, args = [], []
    if round_c is not None:
        where.append("round_key=?")
        args.append(round_c)
    if window_c is not None:
        where.append("window_key=?")
        args.append(window_c)
    cond = (" WHERE " + " AND ".join(where)) if where else ""
    total = conn.execute(
        f"SELECT COUNT(*) FROM plan_window{cond}", args).fetchone()[0]
    rows = list(conn.execute(
        f"SELECT * FROM plan_window{cond} ORDER BY id LIMIT ? OFFSET ?",
        args + [limit, offset]))
    ch_types = _channel_types(conn)
    lib_version = _meta_map(conn).get("chainage_lib_version")
    return {
        "total_windows": total,
        "returned": len(rows),
        "offset": offset,
        "truncated": offset + len(rows) < total,
        "chainage_lib_version": lib_version,
        "windows": [_window_object(conn, w, ch_types, lib_version,
                                   with_records)
                    for w in rows],
    }


# --------------------------------------------------------------------------- #
# 未归属记录
# --------------------------------------------------------------------------- #
def _record_brief(r) -> dict:
    out = {
        "record_id": r["id"],
        "kind": r["kind"],
        "device": decode_id(r["device"]),
        "boot": decode_id(r["boot"]),
        "seq": str(r["seq"]),
        "raw_count": str(r["raw_count"]),
    }
    if r["kind"] != KIND_RECEIPT:
        out["round"] = decode_id(r["round_key"])
        out["channel"] = decode_id(r["channel"])
    return out


def _photo_overlaps(conn, record_id) -> List[dict]:
    out = []
    for o in conn.execute(
            "SELECT o.window_id, o.lo_num, o.lo_den, o.hi_num, o.hi_den,"
            " w.round_key, w.window_key FROM photo_overlap o "
            "JOIN plan_window w ON w.id = o.window_id "
            "WHERE o.record_id=? ORDER BY w.start_utc, w.id", (record_id,)):
        out.append({
            "round": decode_id(o["round_key"]),
            "window": decode_id(o["window_key"]),
            "overlap": {"lo": _frac(o["lo_num"], o["lo_den"]),
                        "hi": _frac(o["hi_num"], o["hi_den"])},
        })
    return out


def _photo_gaps(conn, record_id) -> List[dict]:
    return [{
        "lo": _frac(g["lo_num"], g["lo_den"]),
        "hi": _frac(g["hi_num"], g["hi_den"]),
    } for g in conn.execute(
        "SELECT lo_num, lo_den, hi_num, hi_den FROM photo_gap "
        "WHERE record_id=? ORDER BY seq_no", (record_id,))]


def _unattributed_object(conn, r, category, detail) -> dict:
    obj = _record_brief(r)
    obj["category"] = category
    obj["detail"] = json.loads(detail)
    if category == CAT_PARTIAL_OVERLAP:
        obj["overlaps"] = _photo_overlaps(conn, r["id"])
        obj["gaps"] = _photo_gaps(conn, r["id"])
    return obj


def acq_unattributed(conn, round_c: Optional[str] = None,
                     category: Optional[str] = None,
                     kind: Optional[str] = None,
                     limit: int = 200, offset: int = 0) -> dict:
    """单列不能归属的原始记录（缺时钟/完全窗外/局部交叠互不混淆）。"""
    where, args = [], []
    if round_c is not None:
        where.append("r.round_key=?")
        args.append(round_c)
    if category is not None:
        where.append("u.category=?")
        args.append(category)
    if kind is not None:
        where.append("r.kind=?")
        args.append(kind)
    cond = (" WHERE " + " AND ".join(where)) if where else ""
    base = ("FROM unattributed u JOIN sample_record r ON r.id = u.record_id")
    total = conn.execute(
        f"SELECT COUNT(*) {base}{cond}", args).fetchone()[0]
    rows = list(conn.execute(
        f"SELECT r.*, u.category, u.detail {base}{cond} "
        f"ORDER BY u.record_id LIMIT ? OFFSET ?", args + [limit, offset]))
    return {
        "total_unattributed": total,
        "returned": len(rows),
        "offset": offset,
        "truncated": offset + len(rows) < total,
        "records": [_unattributed_object(conn, r, r["category"], r["detail"])
                    for r in rows],
    }


# --------------------------------------------------------------------------- #
# 单条回看
# --------------------------------------------------------------------------- #
def _receipt_info(conn, rid, roles) -> dict:
    row = conn.execute(
        "SELECT r.seq, r.raw_count, r.pulse, p.utc, pl.expanded "
        "FROM sample_record r JOIN pulse p ON p.pulse = r.pulse "
        "JOIN placement pl ON pl.record_id = r.id WHERE r.id=?",
        (rid,)).fetchone()
    return {
        "receipt_record_id": rid,
        "roles": roles,
        "seq": str(row["seq"]),
        "raw_count": str(row["raw_count"]),
        "expanded": row["expanded"],
        "pulse": decode_id(row["pulse"]),
        "pulse_utc": str(row["utc"]),
    }


def acq_trace(conn, record_id: int) -> Optional[dict]:
    """从任一采样行或未归属记录回看：原始计数、卷行、展开块，以及确定
    该时刻（或曝光两个端点）所用的回执与其参考时刻。"""
    r = conn.execute("SELECT * FROM sample_record WHERE id=?",
                     (record_id,)).fetchone()
    if r is None:
        return None
    out = {"record": _record_brief(r)}
    rec = out["record"]
    if r["kind"] == KIND_RECEIPT:
        rec["pulse"] = decode_id(r["pulse"])
    elif r["kind"] == KIND_READING:
        rec["value"] = str(r["value"])
    else:
        rec["resource"] = r["resource"]
        rec["exposure"] = str(r["exposure"])
    out["evidence"] = [
        {"volume": e["volume"], "line": e["line"]}
        for e in conn.execute(
            "SELECT volume, line FROM evidence WHERE record_id=? "
            "ORDER BY volume, line", (record_id,))]
    pl = conn.execute(
        "SELECT block_id, expanded FROM placement WHERE record_id=?",
        (record_id,)).fetchone()
    blk = conn.execute("SELECT * FROM expand_block WHERE id=?",
                       (pl["block_id"],)).fetchone()
    out["block"] = {
        "block_id": blk["id"],
        "device": decode_id(blk["device"]),
        "boot": decode_id(blk["boot"]),
        "seq_first": str(blk["seq_first"]),
        "seq_last": str(blk["seq_last"]),
        "n_records": blk["n_records"],
        "n_receipts": blk["n_receipts"],
        "expanded": pl["expanded"],
        "block_max_expanded": blk["max_expanded"],
    }
    if r["kind"] == KIND_RECEIPT:
        utc = conn.execute("SELECT utc FROM pulse WHERE pulse=?",
                           (r["pulse"],)).fetchone()["utc"]
        out["clock"] = {"role": "receipt",
                        "pulse": decode_id(r["pulse"]),
                        "utc": str(utc)}
    else:
        al = conn.execute("SELECT * FROM alignment WHERE record_id=?",
                          (record_id,)).fetchone()
        clock = {"status": al["status"]}
        if al["status"] == "ok":
            roles = {}
            if r["kind"] == KIND_READING:
                pairs = (("lo", al["ro_lo"]), ("hi", al["ro_hi"]))
            else:
                pairs = (("open_lo", al["ro_lo"]), ("open_hi", al["ro_hi"]),
                         ("close_lo", al["rc_lo"]), ("close_hi", al["rc_hi"]))
            for role, rid2 in pairs:
                if rid2 is not None:
                    roles.setdefault(rid2, []).append(role)
            clock["receipts"] = [
                _receipt_info(conn, rid2, roles[rid2])
                for rid2 in sorted(roles)]
            if r["kind"] == KIND_READING:
                clock["utc"] = _frac(al["utc_num"], al["utc_den"])
            else:
                clock["exposure_utc"] = {
                    "open": _frac(al["open_num"], al["open_den"]),
                    "close": _frac(al["close_num"], al["close_den"]),
                }
        out["clock"] = clock
    at = conn.execute("SELECT window_id FROM attribution WHERE record_id=?",
                      (record_id,)).fetchone()
    if at is not None:
        w = conn.execute("SELECT * FROM plan_window WHERE id=?",
                         (at["window_id"],)).fetchone()
        out["attribution"] = {
            "round": decode_id(w["round_key"]),
            "window": decode_id(w["window_key"]),
            "interval_utc": {"start": str(w["start_utc"]),
                             "end": str(w["end_utc"]),
                             "half_open": True},
            "registration": w["registration"],
        }
    elif r["kind"] != KIND_RECEIPT:
        # 回执是时钟本身，不参与归属；其余记录必有一行未归属结果
        un = conn.execute(
            "SELECT category, detail FROM unattributed WHERE record_id=?",
            (record_id,)).fetchone()
        obj = {"category": un["category"],
               "detail": json.loads(un["detail"])}
        if un["category"] == CAT_PARTIAL_OVERLAP:
            obj["overlaps"] = _photo_overlaps(conn, record_id)
            obj["gaps"] = _photo_gaps(conn, record_id)
        out["unattributed"] = obj
    return out
