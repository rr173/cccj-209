"""巡检义务表与巡检簿的解析、严格校验与暂存（全部落 SQLite）。

输入
====
1. 义务表（JSON 数组或 JSONL，每行/每项一个义务）::

       {
         "obligation_id": "O1",
         "axis_id": "A1",
         "lane": "L1",
         "start": 10, "end": 14,                    # 物理闭区间 [start, end]
         "window_start": 0, "window_end": 86400,    # 巡检窗口 [start, end)
         "min_crews": 2,
         "sub_windows": [
           {"start": 0, "end": 14400, "min_crews": 1, "label": "morning"}
         ]
       }

2. 巡检簿（JSONL，可乱序送达的**更正记录**）::

       {"trip_id": "T1", "ingest_time": 100, "action": "trip",
        "crew": "C1", "survey_time": 90, "lane": "L1",
        "routes": [
          {"fragments": [
             {"reg_id": "R1", "n_from": 10, "n_to": 12, "calib_row_id": "S1"}
          ]}, ...
        ]}

       {"trip_id": "T1", "ingest_time": 200, "action": "void"}

规则要点（与 FORMAT.md 一致）：

- 同一 ``(trip_id, ingest_time)`` 只能有一条记录；重复入簿键报错。
- 结算截止 ``cutoff`` 只取 ``ingest_time <= cutoff`` 的最后一条记录，整条
  替换此前版本；``void`` 使该趟在该截止点不可用。
- 非撤销记录给出班组、实际巡检时刻、车道与一个或多个互斥的**完整路线**。
  班组与实测时刻适用于该记录的所有路线版本。
- ``survey_time <= ingest_time``：晚报与更正都不把作业时刻挪到入簿时刻。
- 输入结构错误（缺字段、类型不对、越界）与重复入簿键都聚合报错；
  引用能否解释由 :mod:`chainage.settlement` 在所求截止点逐片段校验，
  旧版本/未来版本里的引用不解析。

义务表与巡检簿都只是**结构**校验；换算库的使用只发生在结算阶段。
"""

from __future__ import annotations

import json
import os
import sqlite3
from dataclasses import dataclass
from typing import Any, Dict, Iterator, List, Optional, Tuple

from .errors import SettlementIssue

INT64_MIN = -(2 ** 63)
INT64_MAX = 2 ** 63 - 1


# --------------------------------------------------------------------------- #
# 数据类
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class SubWindow:
    start: int
    end: int
    min_crews: int
    label: Optional[str] = None


@dataclass(frozen=True)
class Obligation:
    obligation_id: str
    axis_id: str
    lane: str
    start: int          # 物理闭区间
    end: int
    window_start: int   # [window_start, window_end)
    window_end: int
    min_crews: int
    sub_windows: Tuple[SubWindow, ...]
    source_line: int

    def thresholds(self) -> List[Tuple[str, int, int, int]]:
        """全部门槛：[(label, start, end, min_crews), ...]，主窗在前。

        每个门槛独立统计相应时间内不同的班组，彼此不构成对班组的指派。
        """
        out = [("full", self.window_start, self.window_end, self.min_crews)]
        for i, sw in enumerate(self.sub_windows):
            out.append((sw.label or f"sub{i}", sw.start, sw.end, sw.min_crews))
        return out


# --------------------------------------------------------------------------- #
# 基础类型校验
# --------------------------------------------------------------------------- #
def _is_int(v: Any) -> bool:
    return isinstance(v, int) and not isinstance(v, bool)


def _check_int(d: dict, key: str, loc: str, issues: List[SettlementIssue],
               required: bool = True, lo: Optional[int] = None,
               hi: Optional[int] = None, ctx: Optional[dict] = None
               ) -> Optional[int]:
    ctx = ctx or {}
    if key not in d:
        if required:
            issues.append(SettlementIssue(loc, "MISSING_FIELD",
                                          f"缺少字段 {key!r}", **ctx))
        return None
    v = d[key]
    if not _is_int(v):
        issues.append(SettlementIssue(loc, "BAD_FIELD",
                                      f"字段 {key!r} 必须是整数", **ctx))
        return None
    if not (INT64_MIN <= v <= INT64_MAX):
        issues.append(SettlementIssue(loc, "INT64_OVERFLOW",
                                      f"字段 {key!r}={v} 超出有符号 64 位",
                                      **ctx))
        return None
    if lo is not None and v < lo:
        issues.append(SettlementIssue(loc, "BAD_FIELD",
                                      f"字段 {key!r}={v} 小于 {lo}", **ctx))
        return None
    if hi is not None and v > hi:
        issues.append(SettlementIssue(loc, "BAD_FIELD",
                                      f"字段 {key!r}={v} 大于 {hi}", **ctx))
        return None
    return v


def _check_str(d: dict, key: str, loc: str, issues: List[SettlementIssue],
               required: bool = True, ctx: Optional[dict] = None
               ) -> Optional[str]:
    ctx = ctx or {}
    if key not in d:
        if required:
            issues.append(SettlementIssue(loc, "MISSING_FIELD",
                                          f"缺少字段 {key!r}", **ctx))
        return None
    v = d[key]
    if not isinstance(v, str) or v == "":
        issues.append(SettlementIssue(loc, "BAD_FIELD",
                                      f"字段 {key!r} 必须是非空字符串", **ctx))
        return None
    return v


def _iter_jsonl(path: str, issues: List[SettlementIssue]
                ) -> Iterator[Tuple[int, dict, str]]:
    """逐行流式读取 JSONL；坏行记错误后继续，不把整份输入留在内存。"""
    with open(path, "r", encoding="utf-8") as fh:
        for lineno, raw in enumerate(fh, start=1):
            s = raw.strip()
            if not s:
                continue
            loc = f"{os.path.basename(path)}:{lineno}"
            try:
                obj = json.loads(s)
            except json.JSONDecodeError as exc:
                issues.append(SettlementIssue(loc, "BAD_JSON",
                                              f"不是合法 JSON: {exc.msg}"))
                continue
            if not isinstance(obj, dict):
                issues.append(SettlementIssue(loc, "BAD_FIELD", "每行必须是对象"))
                continue
            yield lineno, obj, loc


def _iter_json_array_or_lines(path: str, issues: List[SettlementIssue]
                              ) -> Iterator[Tuple[int, dict, str]]:
    """义务表既可为 JSON 数组，也可为 JSONL。"""
    if not os.path.isfile(path):
        issues.append(SettlementIssue(path, "MISSING_FILE", "文件不存在"))
        return
    with open(path, "r", encoding="utf-8") as fh:
        first = ""
        while True:
            ch = fh.read(1)
            if ch == "":
                return
            if not ch.isspace():
                first = ch
                break
    if first != "[":
        yield from _iter_jsonl(path, issues)
        return
    with open(path, "r", encoding="utf-8") as fh:
        text = fh.read()
    try:
        arr = json.loads(text)
    except json.JSONDecodeError as exc:
        issues.append(SettlementIssue(f"{os.path.basename(path)}:1", "BAD_JSON",
                                      f"不是合法 JSON: {exc.msg}"))
        return
    if not isinstance(arr, list):
        issues.append(SettlementIssue(os.path.basename(path), "BAD_FIELD",
                                      "顶层必须是数组"))
        return
    for i, obj in enumerate(arr):
        loc = f"{os.path.basename(path)}#items[{i}]"
        if not isinstance(obj, dict):
            issues.append(SettlementIssue(loc, "BAD_FIELD", "每项必须是对象"))
            continue
        yield i, obj, loc


# --------------------------------------------------------------------------- #
# 义务表
# --------------------------------------------------------------------------- #
def parse_obligations(path: str, issues: List[SettlementIssue]
                      ) -> Tuple[List[Obligation], List[str]]:
    out: List[Obligation] = []
    order: List[str] = []
    seen = set()
    for line_no, d, loc in _iter_json_array_or_lines(path, issues):
        oblig_id = _check_str(d, "obligation_id", loc, issues)
        if oblig_id is not None:
            if oblig_id in seen:
                issues.append(SettlementIssue(
                    loc, "DUPLICATE_OBLIGATION_ID",
                    f"义务 ID {oblig_id!r} 重复", obligation_id=oblig_id))
                oblig_id = None
            else:
                seen.add(oblig_id)
        ctx: Dict[str, Any] = {"obligation_id": oblig_id}
        axis_id = _check_str(d, "axis_id", loc, issues, ctx=ctx)
        lane = _check_str(d, "lane", loc, issues, ctx=ctx)
        start = _check_int(d, "start", loc, issues, ctx=ctx)
        end = _check_int(d, "end", loc, issues, ctx=ctx)
        w_start = _check_int(d, "window_start", loc, issues, ctx=ctx)
        w_end = _check_int(d, "window_end", loc, issues, ctx=ctx)
        min_crews = _check_int(d, "min_crews", loc, issues, lo=1, ctx=ctx)

        if start is not None and end is not None and start > end:
            issues.append(SettlementIssue(
                loc, "BAD_RANGE",
                f"物理区间要求 start <= end，实际 [{start},{end}]", **ctx))
            start = end = None
        if w_start is not None and w_end is not None and w_start >= w_end:
            issues.append(SettlementIssue(
                loc, "BAD_RANGE",
                f"巡检窗口要求 start < end（半开区间），实际 "
                f"[{w_start},{w_end})", **ctx))
            w_start = w_end = None

        subs: List[SubWindow] = []
        raw_subs = d.get("sub_windows", [])
        if not isinstance(raw_subs, list):
            issues.append(SettlementIssue(loc, "BAD_FIELD",
                                          "字段 sub_windows 必须是数组", **ctx))
            raw_subs = []
        sub_labels = set()
        for si, sw in enumerate(raw_subs):
            sloc = f"{loc}.sub_windows[{si}]"
            if not isinstance(sw, dict):
                issues.append(SettlementIssue(sloc, "BAD_FIELD",
                                              "子窗口必须是对象", **ctx))
                continue
            s_start = _check_int(sw, "start", sloc, issues, ctx=ctx)
            s_end = _check_int(sw, "end", sloc, issues, ctx=ctx)
            s_min = _check_int(sw, "min_crews", sloc, issues, lo=1, ctx=ctx)
            label = sw.get("label")
            if label is not None and (not isinstance(label, str) or label == ""):
                issues.append(SettlementIssue(
                    sloc, "BAD_FIELD",
                    "字段 label 若给出必须是非空字符串", **ctx))
                label = None
            elif label is not None:
                if label in sub_labels:
                    issues.append(SettlementIssue(
                        sloc, "DUPLICATE_SUBWINDOW_LABEL",
                        f"同一义务的子窗口 label {label!r} 重复", **ctx))
                    label = None
                else:
                    sub_labels.add(label)
            if s_start is not None and s_end is not None:
                if s_start >= s_end:
                    issues.append(SettlementIssue(
                        sloc, "BAD_RANGE",
                        f"子窗口要求 start < end，实际 [{s_start},{s_end})",
                        **ctx))
                    s_start = s_end = None
                elif w_start is not None and w_end is not None and \
                        (s_start < w_start or s_end > w_end):
                    issues.append(SettlementIssue(
                        sloc, "SUBWINDOW_OUT_OF_WINDOW",
                        f"子窗口 [{s_start},{s_end}) 超出主窗口 "
                        f"[{w_start},{w_end})", **ctx))
                    s_start = s_end = None
            if s_start is not None and s_end is not None and s_min is not None:
                # 子窗口可以相交；同一 (起,止) 重复才是冗余错误
                if any(x.start == s_start and x.end == s_end for x in subs):
                    issues.append(SettlementIssue(
                        sloc, "DUPLICATE_SUBWINDOW",
                        f"子窗口 [{s_start},{s_end}) 重复", **ctx))
                    continue
                subs.append(SubWindow(s_start, s_end, s_min, label))

        if None in (oblig_id, axis_id, lane, start, end,
                    w_start, w_end, min_crews):
            continue
        out.append(Obligation(
            obligation_id=oblig_id, axis_id=axis_id, lane=lane,
            start=start, end=end, window_start=w_start, window_end=w_end,
            min_crews=min_crews, sub_windows=tuple(subs), source_line=line_no))
        order.append(oblig_id)
    return out, order


# --------------------------------------------------------------------------- #
# 巡检簿：逐条流式解析并写入暂存表
# --------------------------------------------------------------------------- #
def stage_trip_log(conn: sqlite3.Connection, path: str,
                   issues: List[SettlementIssue]) -> int:
    """解析巡检簿并逐条落 SQLite；重复入簿键由唯一约束兜底。

    返回成功暂存的记录数。结构错误的行（或记录）不落库，但继续解析其余
    行，最后在调用方聚合抛错。
    """
    if not os.path.isfile(path):
        issues.append(SettlementIssue(path, "MISSING_FILE", "文件不存在"))
        return 0
    count = 0
    for lineno, d, loc in _iter_jsonl(path, issues):
        rec = _parse_trip_record(d, lineno, loc, issues)
        if rec is None:
            continue
        try:
            cur = conn.execute(
                "INSERT INTO trip_record(trip_id, ingest_time, action, crew, "
                "survey_time, lane, source_line) VALUES (?,?,?,?,?,?,?)",
                (rec["trip_id"], rec["ingest_time"], rec["action"],
                 rec["crew"], rec["survey_time"], rec["lane"], lineno))
        except sqlite3.IntegrityError:
            prev = conn.execute(
                "SELECT source_line FROM trip_record WHERE trip_id=? AND "
                "ingest_time=?",
                (rec["trip_id"], rec["ingest_time"])).fetchone()
            issues.append(SettlementIssue(
                loc, "DUPLICATE_INGEST_KEY",
                f"同一趟次 {rec['trip_id']!r} 在入簿时刻 "
                f"{rec['ingest_time']} 已有记录（首次见第 "
                f"{prev['source_line'] if prev else '?'} 行）",
                trip_id=rec["trip_id"], ingest_time=rec["ingest_time"]))
            continue
        rec_id = cur.lastrowid
        for route in rec["routes"]:
            for frag in route["fragments"]:
                conn.execute(
                    "INSERT INTO fragment VALUES (?,?,?,?,?,?,?)",
                    (rec_id, route["route_index"], frag["frag_index"],
                     frag["reg_id"], frag["n_from"], frag["n_to"],
                     frag["calib_row_id"]))
        count += 1
    return count


def _parse_trip_record(d: dict, lineno: int, loc: str,
                       issues: List[SettlementIssue]) -> Optional[dict]:
    trip_id = _check_str(d, "trip_id", loc, issues)
    ctx: Dict[str, Any] = {"trip_id": trip_id}
    ingest_time = _check_int(d, "ingest_time", loc, issues, ctx=ctx)
    if ingest_time is not None:
        ctx["ingest_time"] = ingest_time

    action = d.get("action", "trip")
    if action not in ("trip", "void"):
        issues.append(SettlementIssue(
            loc, "BAD_FIELD", "字段 action 只能是 'trip' 或 'void'", **ctx))
        return None
    if action == "void":
        if None in (trip_id, ingest_time):
            return None
        return {"trip_id": trip_id, "ingest_time": ingest_time,
                "action": "void", "crew": None, "survey_time": None,
                "lane": None, "routes": []}

    crew = _check_str(d, "crew", loc, issues, ctx=ctx)
    survey_time = _check_int(d, "survey_time", loc, issues, ctx=ctx)
    lane = _check_str(d, "lane", loc, issues, ctx=ctx)
    if survey_time is not None and ingest_time is not None and \
            survey_time > ingest_time:
        # 实际巡检时刻不得晚于入簿时刻；晚报不能把作业挪后
        issues.append(SettlementIssue(
            loc, "SURVEY_AFTER_INGEST",
            f"实际巡检时刻 {survey_time} 晚于入簿时刻 {ingest_time}", **ctx))
        survey_time = None

    routes = _parse_routes(d.get("routes"), loc, issues, ctx)
    if None in (trip_id, ingest_time, crew, survey_time, lane) or not routes:
        return None
    return {"trip_id": trip_id, "ingest_time": ingest_time, "action": "trip",
            "crew": crew, "survey_time": survey_time, "lane": lane,
            "routes": routes}


def _parse_routes(raw: Any, loc: str, issues: List[SettlementIssue],
                  ctx: dict) -> List[dict]:
    if raw is None:
        issues.append(SettlementIssue(loc, "MISSING_FIELD",
                                      "缺少字段 'routes'", **ctx))
        return []
    if not isinstance(raw, list) or not raw:
        issues.append(SettlementIssue(loc, "BAD_FIELD",
                                      "字段 routes 必须是非空数组", **ctx))
        return []
    routes: List[dict] = []
    for ri, rd in enumerate(raw):
        rloc = f"{loc}.routes[{ri}]"
        rctx = dict(ctx, route_index=ri)
        if not isinstance(rd, dict):
            issues.append(SettlementIssue(rloc, "BAD_FIELD",
                                          "路线版本必须是对象", **rctx))
            continue
        frags = rd.get("fragments")
        if not isinstance(frags, list) or not frags:
            issues.append(SettlementIssue(rloc, "BAD_FIELD",
                                          "路线版本的 fragments 必须是非空数组",
                                          **rctx))
            continue
        parsed: List[dict] = []
        ok = True
        for fi, fd in enumerate(frags):
            floc = f"{rloc}.fragments[{fi}]"
            fctx = dict(rctx, fragment_index=fi)
            if not isinstance(fd, dict):
                issues.append(SettlementIssue(floc, "BAD_FIELD",
                                              "片段必须是对象", **fctx))
                ok = False
                continue
            reg_id = _check_str(fd, "reg_id", floc, issues, ctx=fctx)
            n_from = _check_int(fd, "n_from", floc, issues, ctx=fctx)
            n_to = _check_int(fd, "n_to", floc, issues, ctx=fctx)
            calib_row_id = _check_str(fd, "calib_row_id", floc, issues,
                                      ctx=fctx)
            if n_from is not None and n_to is not None and n_from > n_to:
                issues.append(SettlementIssue(
                    floc, "BAD_RANGE",
                    f"片段源区间要求 n_from <= n_to，实际 [{n_from},{n_to}]",
                    **fctx))
                ok = False
            if None in (reg_id, n_from, n_to, calib_row_id):
                ok = False
                continue
            parsed.append({"frag_index": fi, "reg_id": reg_id,
                           "n_from": n_from, "n_to": n_to,
                           "calib_row_id": calib_row_id})
        if ok and parsed:
            routes.append({"route_index": ri, "fragments": parsed})
    return routes


# --------------------------------------------------------------------------- #
# 暂存库
# --------------------------------------------------------------------------- #
STAGE_SCHEMA = """
CREATE TABLE trip_record (
    rec_id       INTEGER PRIMARY KEY,
    trip_id      TEXT NOT NULL,
    ingest_time  INTEGER NOT NULL,
    action       TEXT NOT NULL,
    crew         TEXT,
    survey_time  INTEGER,
    lane         TEXT,
    source_line  INTEGER NOT NULL,
    UNIQUE (trip_id, ingest_time)
);
CREATE TABLE fragment (
    rec_id       INTEGER NOT NULL,
    route_index  INTEGER NOT NULL,
    frag_index   INTEGER NOT NULL,
    reg_id       TEXT NOT NULL,
    n_from       INTEGER NOT NULL,
    n_to         INTEGER NOT NULL,
    calib_row_id TEXT NOT NULL,
    PRIMARY KEY (rec_id, route_index, frag_index)
);
CREATE INDEX idx_trip_trip_time ON trip_record(trip_id, ingest_time);
CREATE INDEX idx_frag_rec ON fragment(rec_id, route_index, frag_index);
"""


def create_staging(conn: sqlite3.Connection) -> None:
    conn.executescript(STAGE_SCHEMA)


def stage_obligations(conn: sqlite3.Connection,
                      obligations: List[Obligation]) -> None:
    conn.executescript("""
        CREATE TABLE oblig (
            seq           INTEGER PRIMARY KEY,
            obligation_id TEXT NOT NULL UNIQUE,
            axis_id       TEXT NOT NULL,
            lane          TEXT NOT NULL,
            p_start       INTEGER NOT NULL,
            p_end         INTEGER NOT NULL,
            w_start       INTEGER NOT NULL,
            w_end         INTEGER NOT NULL,
            min_crews     INTEGER NOT NULL
        );
        CREATE TABLE oblig_threshold (
            obligation_id TEXT NOT NULL,
            seq           INTEGER NOT NULL,
            label         TEXT NOT NULL,
            w_start       INTEGER NOT NULL,
            w_end         INTEGER NOT NULL,
            min_crews     INTEGER NOT NULL,
            PRIMARY KEY (obligation_id, seq)
        );
    """)
    for seq, ob in enumerate(obligations):
        conn.execute(
            "INSERT INTO oblig VALUES (?,?,?,?,?,?,?,?,?)",
            (seq, ob.obligation_id, ob.axis_id, ob.lane, ob.start, ob.end,
             ob.window_start, ob.window_end, ob.min_crews))
        for tseq, (label, ws, we, mc) in enumerate(ob.thresholds()):
            conn.execute(
                "INSERT INTO oblig_threshold VALUES (?,?,?,?,?,?)",
                (ob.obligation_id, tseq, label, ws, we, mc))
