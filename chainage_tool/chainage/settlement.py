"""巡检履约结算核心（离线，只读 ready 换算库 + 已暂存的义务/巡检簿）。

结算流程
========
1. **选取可见版本**：对每趟次取 ``ingest_time <= cutoff`` 的最后一条记录，
   整条替换此前版本；``void`` 使其不可用。只有被选中的记录才解析引用，
   旧版本/未来版本的引用不进入结算。
2. **片段定位**：每个被选中记录里的每个路线版本，逐片段把
   ``(reg_id, n_from..n_to, calib_row_id)`` 解释成物理轴上的整数闭区间。
   - 选定行必须与登记的源体系/轴/段一致，并覆盖子区间的全部刻度；
   - 子区间须落在登记区间内，反解出的物理范围须落在标定行物理范围内；
   - 把换算明细中的位置按所引用子区间裁成实际走过的物理片段；
   - 任一引用无法完整解释时，定位到 ``趟次 / 入簿时刻 / 路线 / 片段``
     并拒绝整份结算，绝不删掉该路线继续算。
3. **逐刻度证据**（同一趟次）：
   - 任一完整路线版本到过刻度 ``x`` -> "可能巡过"；
   - **每个**完整路线版本都到过 ``x`` -> "确定巡过"。
   不同趟次的路线选择相互独立。
4. **按门槛统计班组上下界**（每个物理刻度、每个门槛独立）：
   - 趟次须满足车道 == 义务车道，且实际巡检时刻落在门槛半开窗口内；
   - 同一班组任一确定趟次 -> 下界；任一可能趟次 -> 上界；
   - 所有下界达到各自门槛 -> ``MET``；任一上界不足 -> ``UNMET``；
     其他 -> ``REVIEW``。
5. **分界扫描**：结果只可能在义务端点、相关片段映射出的物理区间端点处
   改变。因此工作量随**相关片段数与状态分界数**增长，不按刻度铺表、
   不枚举路网路线组合、也不为每个区段复制整套班组证据。

所有输出先完整算完并通过校验后一次性写出；校验失败不产生部分清单。
"""

from __future__ import annotations

import bisect
import os
import sqlite3
from dataclasses import dataclass
from typing import Dict, List, Optional, Sequence, Set, Tuple

from .errors import DatabaseError, SettlementIssue, SettlementValidationError
from .intervals import Interval, clip, intersect, normalize
from .settlement_input import (
    Obligation,
    create_staging,
    parse_obligations,
    stage_obligations,
    stage_trip_log,
)

STATE_MET = "MET"                 # 已达标
STATE_UNMET = "UNMET"             # 确定缺巡
STATE_REVIEW = "REVIEW"           # 需核实

STATE_CN = {STATE_MET: "已达标", STATE_UNMET: "确定缺巡",
            STATE_REVIEW: "需核实"}


# --------------------------------------------------------------------------- #
# 片段定位
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class FragmentResolution:
    """一个片段引用在换算库上的完整解释。"""

    reg_id: str
    source_system: str
    reg_axis_id: Optional[str]
    reg_segment: Optional[str]
    reg_n_from: int
    reg_n_to: int
    n_from: int
    n_to: int
    calib_row_id: str
    calib_axis_id: str
    calib_segment: str
    calib_source_line: int
    x_lo: int
    x_hi: int
    frag_index: int = 0

    @property
    def interval(self) -> Interval:
        return (self.x_lo, self.x_hi)


class RejectError(Exception):
    """片段引用无法完整解释：带趟次/入簿时刻/路线/片段定位。"""

    def __init__(self, rec: dict, frag: dict, code: str, message: str):
        super().__init__(message)
        self.issue = SettlementIssue(
            "trip_log", code, message, trip_id=rec["trip_id"],
            ingest_time=rec["ingest_time"], route_index=frag.get("route_index"),
            fragment_index=frag.get("frag_index"))


class CalibResolver:
    """按 ready 库解释片段引用；每个不同引用只解析一次并缓存。

    ``cached_refs`` 与 ``cached_issues`` 让两个截止点比较时复用同一份
    换算库解释，且被拒绝的引用不再重复查询。
    """

    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn
        self._reg_cache: Dict[str, Optional[dict]] = {}
        self._calib_cache: Dict[str, Optional[dict]] = {}
        self._res_cache: Dict[tuple, FragmentResolution] = {}
        self._rej_cache: Dict[tuple, SettlementIssue] = {}

    def _reg(self, reg_id: str) -> Optional[dict]:
        if reg_id not in self._reg_cache:
            r = self.conn.execute(
                "SELECT * FROM registration WHERE reg_id=?", (reg_id,)
            ).fetchone()
            self._reg_cache[reg_id] = dict(r) if r else None
        return self._reg_cache[reg_id]

    def _calib(self, row_id: str) -> Optional[dict]:
        if row_id not in self._calib_cache:
            r = self.conn.execute(
                "SELECT * FROM calibration WHERE row_id=?", (row_id,)
            ).fetchone()
            self._calib_cache[row_id] = dict(r) if r else None
        return self._calib_cache[row_id]

    def resolve(self, frag: dict, rec: dict) -> FragmentResolution:
        key = (frag["reg_id"], frag["n_from"], frag["n_to"],
               frag["calib_row_id"])
        cached = self._res_cache.get(key)
        if cached is not None:
            return cached
        cached_issue = self._rej_cache.get(key)
        if cached_issue is not None:
            raise RejectError(rec, frag, cached_issue.code,
                              cached_issue.message)

        try:
            res = self._resolve_uncached(frag, rec)
        except RejectError as rej:
            self._rej_cache[key] = rej.issue
            raise
        self._res_cache[key] = res
        return res

    def _resolve_uncached(self, frag: dict, rec: dict) -> FragmentResolution:
        def reject(code: str, message: str):
            raise RejectError(rec, frag, code, message)

        reg = self._reg(frag["reg_id"])
        if reg is None:
            reject("UNKNOWN_REGISTRATION",
                   f"换算库中没有登记 {frag['reg_id']!r}")
        row = self._calib(frag["calib_row_id"])
        if row is None:
            reject("UNKNOWN_CALIB_ROW",
                   f"换算库中没有标定行 {frag['calib_row_id']!r}")

        # 选定行必须与登记的源体系/轴/段限定一致
        if row["system"] != reg["source_system"]:
            reject("CALIB_SYSTEM_MISMATCH",
                   f"标定行 {row['row_id']!r} 属于体系 {row['system']!r}，而登记 "
                   f"{reg['reg_id']!r} 的源体系是 {reg['source_system']!r}")
        if reg["axis_id"] is not None and row["axis_id"] != reg["axis_id"]:
            reject("CALIB_AXIS_MISMATCH",
                   f"标定行 {row['row_id']!r} 在轴 {row['axis_id']!r}，"
                   f"而登记限定轴 {reg['axis_id']!r}")
        if reg["segment"] is not None and row["segment"] != reg["segment"]:
            reject("CALIB_SEGMENT_MISMATCH",
                   f"标定行 {row['row_id']!r} 的段名 {row['segment']!r} 与登记"
                   f"限定 {reg['segment']!r} 不符")

        # 子区间必须落在登记区间内，且被选定行完整覆盖
        if frag["n_from"] < reg["n_from"] or frag["n_to"] > reg["n_to"]:
            reject("FRAGMENT_OUT_OF_REGISTRATION",
                   f"片段源区间 [{frag['n_from']},{frag['n_to']}] 超出登记 "
                   f"[{reg['n_from']},{reg['n_to']}]")
        if frag["n_from"] < row["label_min"] or frag["n_to"] > row["label_max"]:
            reject("CALIB_DOES_NOT_COVER_FRAGMENT",
                   f"标定行 {row['row_id']!r} 的编号范围 "
                   f"[{row['label_min']},{row['label_max']}] 无法解释片段 "
                   f"[{frag['n_from']},{frag['n_to']}]")

        x1 = row["a"] * frag["n_from"] + row["c"]
        x2 = row["a"] * frag["n_to"] + row["c"]
        x_lo, x_hi = (x1, x2) if x1 <= x2 else (x2, x1)
        if x_lo < row["lo"] or x_hi > row["hi"]:
            reject("PHYSICAL_OUT_OF_CALIB",
                   f"片段 [{frag['n_from']},{frag['n_to']}] 反解到物理 "
                   f"[{x_lo},{x_hi}]，超出标定行 {row['row_id']!r} 的 "
                   f"[{row['lo']},{row['hi']}]")

        return FragmentResolution(
            reg_id=reg["reg_id"], source_system=reg["source_system"],
            reg_axis_id=reg["axis_id"], reg_segment=reg["segment"],
            reg_n_from=reg["n_from"], reg_n_to=reg["n_to"],
            n_from=frag["n_from"], n_to=frag["n_to"],
            calib_row_id=row["row_id"], calib_axis_id=row["axis_id"],
            calib_segment=row["segment"],
            calib_source_line=row["source_line"],
            x_lo=x_lo, x_hi=x_hi, frag_index=frag.get("frag_index", 0))


# --------------------------------------------------------------------------- #
# 路线版本与证据基础类型
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class RouteVersion:
    index: int
    fragments: Tuple[FragmentResolution, ...]

    def coverage(self, axis_id: str) -> List[Interval]:
        """该完整路线版本在本轴走过的物理区间（已规范化，不展开刻度）。"""
        return normalize([f.interval for f in self.fragments
                          if f.calib_axis_id == axis_id])

    def covers(self, axis_id: str, x: int) -> bool:
        for a, b in self.coverage(axis_id):
            if a <= x <= b:
                return True
            if a > x:
                break
        return False


@dataclass(frozen=True)
class ThresholdStat:
    seq: int
    label: str
    window_start: int
    window_end: int
    min_crews: int
    lower: int
    upper: int
    lower_crews: Tuple[str, ...] = ()
    upper_crews: Tuple[str, ...] = ()

    @property
    def signature(self):
        return (self.seq, self.lower, self.upper)


@dataclass
class LedgerSegment:
    """一个状态恒定的物理闭区间；只存人数，不复制班组证据。"""

    lo: int
    hi: int
    state: str
    thresholds: Tuple[ThresholdStat, ...]

    @property
    def signature(self):
        return (self.state,
                tuple((t.seq, t.lower, t.upper) for t in self.thresholds))


def _covers_point(intervals: Sequence[Interval], x: int) -> bool:
    """已规范化区间表是否覆盖单点（线性扫描，区间表通常很短）。"""
    for a, b in intervals:
        if a <= x <= b:
            return True
        if a > x:
            return False
    return False


# --------------------------------------------------------------------------- #
# 可见趟次：一次性解析（每个截止点只读一遍巡检簿）
# --------------------------------------------------------------------------- #
@dataclass
class ResolvedTrip:
    trip_id: str
    crew: str
    survey_time: int
    ingest_time: int
    rec_id: int
    lane: str
    routes: Tuple[RouteVersion, ...]
    axes: frozenset

    def coverage(self, axis_id: str) -> Tuple[List[Interval], List[Interval]]:
        per_route = [r.coverage(axis_id) for r in self.routes]
        possible = normalize([iv for cov in per_route for iv in cov])
        definite = list(per_route[0]) if per_route else []
        for cov in per_route[1:]:
            definite = intersect(definite, cov)
        return definite, possible


def _ensure_resolved_schema(staged: sqlite3.Connection) -> None:
    staged.executescript("""
        CREATE TABLE IF NOT EXISTS resolved_trip (
            phase       INTEGER NOT NULL,
            trip_id     TEXT NOT NULL,
            crew        TEXT NOT NULL,
            survey_time INTEGER NOT NULL,
            ingest_time INTEGER NOT NULL,
            rec_id      INTEGER NOT NULL,
            lane        TEXT NOT NULL,
            PRIMARY KEY (phase, trip_id)
        );
        CREATE TABLE IF NOT EXISTS resolved_route (
            phase       INTEGER NOT NULL,
            trip_id     TEXT NOT NULL,
            route_index INTEGER NOT NULL,
            PRIMARY KEY (phase, trip_id, route_index)
        );
        CREATE TABLE IF NOT EXISTS resolved_frag (
            phase        INTEGER NOT NULL,
            trip_id      TEXT NOT NULL,
            route_index  INTEGER NOT NULL,
            frag_index   INTEGER NOT NULL,
            reg_id       TEXT NOT NULL,
            n_from       INTEGER NOT NULL,
            n_to         INTEGER NOT NULL,
            calib_row_id TEXT NOT NULL,
            axis_id      TEXT NOT NULL,
            x_lo         INTEGER NOT NULL,
            x_hi         INTEGER NOT NULL,
            source_line  INTEGER NOT NULL,
            PRIMARY KEY (phase, trip_id, route_index, frag_index)
        );
        CREATE INDEX IF NOT EXISTS idx_resolved_trip_time
            ON resolved_trip(phase, lane, survey_time);
        CREATE INDEX IF NOT EXISTS idx_resolved_frag_axis
            ON resolved_frag(phase, axis_id, trip_id);
        CREATE INDEX IF NOT EXISTS idx_resolved_frag_trip
            ON resolved_frag(phase, trip_id);
    """)


def _visible_trip_cursor(staged: sqlite3.Connection,
                         cutoff: int) -> sqlite3.Cursor:
    """流式取可见记录：每个趟次只留最后一条（含 void）。

    用窗口函数在 SQLite 内完成"取最后版本"，只把每个趟次的选中记录
    带入进程，不把整份巡检簿留在内存。
    """
    return staged.execute(
        "SELECT rec_id, trip_id, ingest_time, action, crew, survey_time, lane "
        "FROM (SELECT *, ROW_NUMBER() OVER ("
        "        PARTITION BY trip_id ORDER BY ingest_time DESC) AS rn "
        "      FROM trip_record WHERE ingest_time <= ?) "
        "WHERE rn = 1 ORDER BY trip_id", (cutoff,))


def _load_record_routes(conn: sqlite3.Connection, rec_id: int
                        ) -> List[List[dict]]:
    rows = conn.execute(
        "SELECT route_index, frag_index, reg_id, n_from, n_to, calib_row_id "
        "FROM fragment WHERE rec_id=? ORDER BY route_index, frag_index",
        (rec_id,)).fetchall()
    routes: Dict[int, List[dict]] = {}
    for r in rows:
        routes.setdefault(r["route_index"], []).append(dict(r))
    return [routes[k] for k in sorted(routes)]


def load_trips_for_obligation(staged: sqlite3.Connection,
                              obligation: Obligation,
                              phase: int = 0) -> List[ResolvedTrip]:
    """只取与该义务轴/车道/窗口相关的趟次，并还原**全部**路线版本。

    以 ``resolved_frag(axis_id, trip_id)`` 索引筛出"在本轴有片段"的趟次，
    不对每项义务全扫巡检簿。路线版本集合从 ``resolved_route`` 取——即使
    某路线版本在本轴没有片段，它也仍是该趟的一个完整路线选择（覆盖为空），
    参与"确定巡过 = 每个版本都到过"的判定。

    用两条批量查询（本轴片段、全部路线索引）把 N+1 次往返降到常数次；
    工作集只含该义务相关趟次。
    """
    axis = obligation.axis_id
    lane = obligation.lane
    ws, we = obligation.window_start, obligation.window_end
    trip_rows = staged.execute(
        "SELECT t.trip_id, t.crew, t.survey_time, t.ingest_time, t.rec_id, "
        "t.lane FROM resolved_trip t "
        "WHERE t.phase = ? AND t.lane = ? AND t.survey_time >= ? "
        "AND t.survey_time < ? "
        "AND EXISTS (SELECT 1 FROM resolved_frag f "
        "            WHERE f.phase = t.phase AND f.trip_id = t.trip_id "
        "            AND f.axis_id = ?) "
        "ORDER BY t.trip_id", (phase, lane, ws, we, axis)).fetchall()
    if not trip_rows:
        return []
    ids = [tr["trip_id"] for tr in trip_rows]
    id_set = set(ids)

    routes_by_trip: Dict[str, List[int]] = {tid: [] for tid in ids}
    frags_by_trip: Dict[str, Dict[int, List[FragmentResolution]]] = {
        tid: {} for tid in ids}
    # SQLite 单语句参数上限通常很高；分块以防极端情况
    chunk = 500
    for start in range(0, len(ids), chunk):
        part = ids[start:start + chunk]
        ph = ",".join("?" * len(part))
        for r in staged.execute(
                "SELECT trip_id, route_index FROM resolved_route "
                f"WHERE phase=? AND trip_id IN ({ph}) "
                "ORDER BY trip_id, route_index", (phase, *part)):
            routes_by_trip[r["trip_id"]].append(r["route_index"])
        for fr in staged.execute(
                "SELECT trip_id, route_index, frag_index, reg_id, n_from, "
                "n_to, calib_row_id, axis_id, x_lo, x_hi, source_line "
                "FROM resolved_frag "
                f"WHERE phase=? AND axis_id=? AND trip_id IN ({ph}) "
                "ORDER BY trip_id, route_index, frag_index",
                (phase, axis, *part)):
            frags_by_trip[fr["trip_id"]].setdefault(
                fr["route_index"], []).append(FragmentResolution(
                    reg_id=fr["reg_id"], source_system="",
                    reg_axis_id=None, reg_segment=None,
                    reg_n_from=0, reg_n_to=0, n_from=fr["n_from"],
                    n_to=fr["n_to"], calib_row_id=fr["calib_row_id"],
                    calib_axis_id=fr["axis_id"], calib_segment="",
                    calib_source_line=fr["source_line"],
                    x_lo=fr["x_lo"], x_hi=fr["x_hi"],
                    frag_index=fr["frag_index"]))

    out: List[ResolvedTrip] = []
    for tr in trip_rows:
        tid = tr["trip_id"]
        by_route = frags_by_trip[tid]
        versions = [RouteVersion(index=idx,
                                 fragments=tuple(by_route.get(idx, [])))
                    for idx in routes_by_trip[tid]]
        out.append(ResolvedTrip(
            trip_id=tid, crew=tr["crew"],
            survey_time=tr["survey_time"], ingest_time=tr["ingest_time"],
            rec_id=tr["rec_id"], lane=tr["lane"],
            routes=tuple(versions), axes=frozenset({axis})))
    return out


def resolve_visible_trips(staged: sqlite3.Connection,
                          resolver: CalibResolver,
                          cutoff: int,
                          phase: int = 0) -> List[SettlementIssue]:
    """取截止点下每趟次的可见版本并解析全部引用，流式落暂存库。

    逐趟处理、解析结果直接写入暂存表，进程内只驻留当前趟次的路线版本
    （工作量随可见趟次与相关片段线性增长，不随整份巡检簿常驻）。

    任一被选中记录的任一引用无法完整解释时，返回问题列表；调用方据此
    拒绝结算，不产生部分清单。旧版本/未来版本不解析。
    """
    _ensure_resolved_schema(staged)
    staged.execute("DELETE FROM resolved_trip WHERE phase=?", (phase,))
    staged.execute("DELETE FROM resolved_route WHERE phase=?", (phase,))
    staged.execute("DELETE FROM resolved_frag WHERE phase=?", (phase,))
    issues: List[SettlementIssue] = []
    for rec in _visible_trip_cursor(staged, cutoff):
        if rec["action"] != "trip":
            continue
        route_frags = _load_record_routes(staged, rec["rec_id"])
        if not route_frags:
            continue
        versions: List[RouteVersion] = []
        bad = False
        for ri, frags in enumerate(route_frags):
            frag_res: List[FragmentResolution] = []
            for frag in frags:
                try:
                    frag_res.append(resolver.resolve(frag, rec))
                except RejectError as rej:
                    issues.append(rej.issue)
                    bad = True
                    continue
            if not bad:
                versions.append(RouteVersion(index=ri,
                                             fragments=tuple(frag_res)))
        if bad:
            continue
        _persist_one_trip(staged, ResolvedTrip(
            trip_id=rec["trip_id"], crew=rec["crew"],
            survey_time=rec["survey_time"], ingest_time=rec["ingest_time"],
            rec_id=rec["rec_id"], lane=rec["lane"], routes=tuple(versions),
            axes=frozenset()), phase=phase)
    staged.commit()
    return issues


def _persist_one_trip(staged: sqlite3.Connection, t: ResolvedTrip,
                      phase: int) -> None:
    staged.execute(
        "INSERT OR REPLACE INTO resolved_trip "
        "(phase, trip_id, crew, survey_time, ingest_time, rec_id, lane) "
        "VALUES (?,?,?,?,?,?,?)",
        (phase, t.trip_id, t.crew, t.survey_time, t.ingest_time, t.rec_id,
         t.lane))
    for rv in t.routes:
        staged.execute(
            "INSERT OR REPLACE INTO resolved_route "
            "(phase, trip_id, route_index) VALUES (?,?,?)",
            (phase, t.trip_id, rv.index))
        for f in rv.fragments:
            staged.execute(
                "INSERT OR REPLACE INTO resolved_frag "
                "(phase, trip_id, route_index, frag_index, reg_id, n_from, "
                "n_to, calib_row_id, axis_id, x_lo, x_hi, source_line) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (phase, t.trip_id, rv.index, f.frag_index, f.reg_id,
                 f.n_from, f.n_to, f.calib_row_id, f.calib_axis_id, f.x_lo,
                 f.x_hi, f.calib_source_line))


# --------------------------------------------------------------------------- #
# 分界扫描（事件扫描，按相关片段与分界数增长）
# --------------------------------------------------------------------------- #
def _threshold_membership(obligation: Obligation, trip: ResolvedTrip
                          ) -> List[Tuple[List[Interval], List[Interval]]]:
    """每门槛的 (确定区间, 可能区间)；时刻不在门槛窗口内则为空。"""
    definite, possible = trip.coverage(obligation.axis_id)
    definite = clip(definite, obligation.start, obligation.end)
    possible = clip(possible, obligation.start, obligation.end)
    out = []
    for _, ws, we, _ in obligation.thresholds():
        if ws <= trip.survey_time < we:
            out.append((definite, possible))
        else:
            out.append(([], []))
    return out


def build_ledger(obligation: Obligation, trips: Sequence[ResolvedTrip]
                 ) -> List[LedgerSegment]:
    """事件扫描生成状态恒定区段。

    每个相关趟次对每个门槛贡献：可能区间 -> 上界计数；确定区间 -> 下界
    计数。物理刻度 ``x`` 处某班组计数 > 0 即计入该门槛相应上下界。
    区段只保存人数，不复制班组证据。
    """
    thresholds = obligation.thresholds()
    n_thr = len(thresholds)
    upper_cnt: List[Dict[str, int]] = [dict() for _ in range(n_thr)]
    lower_cnt: List[Dict[str, int]] = [dict() for _ in range(n_thr)]
    events: Dict[int, List[Tuple[int, str, str, int]]] = {}

    def add_event(x: int, seq: int, crew: str, kind: str, delta: int):
        events.setdefault(x, []).append((seq, crew, kind, delta))

    all_points: Set[int] = {obligation.start, obligation.end + 1}
    for trip in trips:
        membership = _threshold_membership(obligation, trip)
        for seq, (definite, possible) in enumerate(membership):
            for a, b in possible:
                add_event(a, seq, trip.crew, "u", +1)
                add_event(b + 1, seq, trip.crew, "u", -1)
                all_points.add(a)
                all_points.add(b + 1)
            for a, b in definite:
                add_event(a, seq, trip.crew, "l", +1)
                add_event(b + 1, seq, trip.crew, "l", -1)
                all_points.add(a)
                all_points.add(b + 1)

    def nonempty(d: Dict[str, int]) -> int:
        return sum(1 for v in d.values() if v > 0)

    points = sorted(all_points)
    segs: List[LedgerSegment] = []
    for i in range(len(points) - 1):
        x = points[i]
        nxt = points[i + 1]
        for seq, crew, kind, delta in events.get(x, ()):
            target = lower_cnt if kind == "l" else upper_cnt
            d = target[seq]
            d[crew] = d.get(crew, 0) + delta
            if d[crew] == 0:
                del d[crew]
        hi = nxt - 1
        if x > hi:
            continue
        stats = []
        for seq, (label, ws, we, mc) in enumerate(thresholds):
            lower = nonempty(lower_cnt[seq])
            upper = nonempty(upper_cnt[seq])
            upper = max(upper, lower)
            stats.append(ThresholdStat(
                seq=seq, label=label, window_start=ws, window_end=we,
                min_crews=mc, lower=lower, upper=upper,
                lower_crews=(), upper_crews=()))
        all_met = all(t.lower >= t.min_crews for t in stats)
        any_short = any(t.upper < t.min_crews for t in stats)
        state = STATE_MET if all_met else (
            STATE_UNMET if any_short else STATE_REVIEW)
        seg = LedgerSegment(lo=x, hi=hi, state=state, thresholds=tuple(stats))
        if segs and segs[-1].signature == seg.signature:
            segs[-1].hi = hi
        else:
            segs.append(seg)
    # 事件在义务区间末端之后才清空，这里扫完后不再需要计数
    return segs


# --------------------------------------------------------------------------- #
# 结算入口（单截止点 / 两截止点 / 单点取证）
# --------------------------------------------------------------------------- #
def _threshold_meta(obligation: Obligation) -> List[dict]:
    return [{"label": label, "start": ws, "end": we, "min_crews": mc}
            for label, ws, we, mc in obligation.thresholds()]


def _segment_dict(seg: LedgerSegment) -> dict:
    return {
        "lo": seg.lo, "hi": seg.hi,
        "state": seg.state, "state_cn": STATE_CN[seg.state],
        "thresholds": [
            {"label": t.label, "window_start": t.window_start,
             "window_end": t.window_end, "min_crews": t.min_crews,
             "lower": t.lower, "upper": t.upper}
            for t in seg.thresholds],
    }


def _threshold_dicts(stats: Sequence[ThresholdStat]) -> List[dict]:
    return [{"label": t.label, "min_crews": t.min_crews,
             "lower": t.lower, "upper": t.upper} for t in stats]


def compare_ledgers(obligation: Obligation,
                    before: Sequence[LedgerSegment],
                    after: Sequence[LedgerSegment]) -> List[dict]:
    """对齐两边实际分界，只报告**状态改变**的区段（附前后人数）。

    只改人数、状态不变的路段不进入结果，也不被误报为换了派工状态；
    两边各自的分界取并集后再逐段比较，避免按某一边的分界误切。
    """
    cuts: Set[int] = set()
    for segs in (before, after):
        for s in segs:
            cuts.add(s.lo)
            cuts.add(s.hi + 1)
    points = sorted(cuts)
    before_starts = [s.lo for s in before]
    after_starts = [s.lo for s in after]

    def seg_at(segs: Sequence[LedgerSegment], starts: Sequence[int],
               x: int) -> Optional[LedgerSegment]:
        if not segs:
            return None
        idx = bisect.bisect_right(starts, x) - 1
        return segs[max(idx, 0)]

    out: List[dict] = []
    for i in range(len(points) - 1):
        lo, nxt = points[i], points[i + 1]
        hi = nxt - 1
        if lo > hi:
            continue
        b = seg_at(before, before_starts, lo)
        a = seg_at(after, after_starts, lo)
        if b is None or a is None:
            continue
        if b.state == a.state:
            continue
        sig = (b.state, a.state, b.signature, a.signature)
        if out and out[-1]["hi"] + 1 == lo and out[-1]["sig"] == sig:
            out[-1]["hi"] = hi
            continue
        out.append({
            "obligation_id": obligation.obligation_id,
            "axis_id": obligation.axis_id,
            "lane": obligation.lane,
            "lo": lo, "hi": hi,
            "before_state": b.state, "after_state": a.state,
            "before_thresholds": _threshold_dicts(b.thresholds),
            "after_thresholds": _threshold_dicts(a.thresholds),
            "sig": sig,
        })
    for item in out:
        item.pop("sig", None)
    return out


def settle_obligation(obligation: Obligation,
                      trips: Sequence[ResolvedTrip]) -> dict:
    """单义务台账：分界区段 + 两张可分别直接取用的清单。"""
    ledger = build_ledger(obligation, trips)
    overall = STATE_MET
    if any(s.state == STATE_UNMET for s in ledger):
        overall = STATE_UNMET
    elif any(s.state == STATE_REVIEW for s in ledger):
        overall = STATE_REVIEW
    dispatch = [{"lo": s.lo, "hi": s.hi} for s in ledger
                if s.state == STATE_UNMET]
    review = [{"lo": s.lo, "hi": s.hi} for s in ledger
              if s.state == STATE_REVIEW]
    return {
        "obligation_id": obligation.obligation_id,
        "axis_id": obligation.axis_id,
        "lane": obligation.lane,
        "start": obligation.start,
        "end": obligation.end,
        "window_start": obligation.window_start,
        "window_end": obligation.window_end,
        "min_crews": obligation.min_crews,
        "thresholds": _threshold_meta(obligation),
        "state": overall,
        "state_cn": STATE_CN[overall],
        "segments": [_segment_dict(s) for s in ledger],
        "dispatch_segments": dispatch,
        "review_segments": review,
    }


def settle_all(obligations: Sequence[Obligation],
               staged: sqlite3.Connection, phase: int = 0) -> dict:
    """按义务结算；每项义务只按自己的轴/车道/窗口取相关趟次。"""
    results = []
    dispatch: List[dict] = []
    review: List[dict] = []
    for ob in obligations:
        trips = load_trips_for_obligation(staged, ob, phase)
        r = settle_obligation(ob, trips)
        results.append(r)
        for seg in r["dispatch_segments"]:
            dispatch.append({
                "obligation_id": r["obligation_id"], "axis_id": r["axis_id"],
                "lane": r["lane"], "lo": seg["lo"], "hi": seg["hi"],
                "state": STATE_UNMET, "state_cn": STATE_CN[STATE_UNMET]})
        for seg in r["review_segments"]:
            review.append({
                "obligation_id": r["obligation_id"], "axis_id": r["axis_id"],
                "lane": r["lane"], "lo": seg["lo"], "hi": seg["hi"],
                "state": STATE_REVIEW, "state_cn": STATE_CN[STATE_REVIEW]})
    return {"obligations": results, "dispatch": dispatch, "review": review}


def compare_all(obligations: Sequence[Obligation],
                staged: sqlite3.Connection) -> dict:
    """比较同一暂存库中两个阶段（phase 0 = 较晚, 1 = 较早）。

    两份结果使用同一份换算库与同一份巡检簿；只列出状态改变的区段与
    前后人数，按两边实际分界对齐。
    """
    results = []
    for ob in obligations:
        before = build_ledger(
            ob, load_trips_for_obligation(staged, ob, phase=1))
        after = build_ledger(
            ob, load_trips_for_obligation(staged, ob, phase=0))
        changes = compare_ledgers(ob, before, after)
        results.append({
            "obligation_id": ob.obligation_id, "axis_id": ob.axis_id,
            "lane": ob.lane, "start": ob.start, "end": ob.end,
            "thresholds": _threshold_meta(ob),
            "changes": changes,
            "change_count": len(changes),
        })
    return {"obligations": results}


# --------------------------------------------------------------------------- #
# 单点取证：指定截止点、指定物理位置的逐门槛解释
# --------------------------------------------------------------------------- #
def explain_obligation_at(obligation: Obligation,
                          trips: Sequence[ResolvedTrip], x: int) -> dict:
    """对义务在指定物理刻度的结论做逐门槛取证。

    对每个门槛列出**确定班组**（有任一趟确定巡过）与**仅可能班组**
    （只有可能巡过），以及贡献趟次。每趟标明采用的入簿版本
    （``trip_id`` + ``ingest_time``）、每条路线版本是否命中该刻度、
    命中片段的登记与源标定行——即使相邻刻度人数相同，这里也只取本点
    证据，不沿用邻点的班组或路线。
    """
    if not (obligation.start <= x <= obligation.end):
        raise DatabaseError(
            f"物理位置 {x} 不在义务 {obligation.obligation_id!r} 的区间 "
            f"[{obligation.start},{obligation.end}] 内")

    # 只保留在义务轴上有片段的路线版本参与覆盖；但"确定"要求**每个**
    # 完整路线版本都到过——含那些在本轴没有片段的版本（它们覆盖为空，
    # 因此本轴任何刻度都不会被判定为确定）。这里按原路线集合处理。
    def trip_coverage(trip: ResolvedTrip):
        definite, possible = trip.coverage(obligation.axis_id)
        return (clip(definite, obligation.start, obligation.end),
                clip(possible, obligation.start, obligation.end))

    trip_out = []
    for trip in sorted(trips, key=lambda t: (t.trip_id, t.ingest_time)):
        definite_cov, possible_cov = trip_coverage(trip)
        in_def = _covers_point(definite_cov, x)
        in_poss = _covers_point(possible_cov, x)
        route_hits = []
        for rv in trip.routes:
            frags = []
            for f in rv.fragments:
                if f.calib_axis_id != obligation.axis_id:
                    continue
                frags.append({
                    "fragment_index": f.frag_index,
                    "reg_id": f.reg_id,
                    "n_from": f.n_from, "n_to": f.n_to,
                    "calib_row_id": f.calib_row_id,
                    "calib_source_line": f.calib_source_line,
                    "axis_id": f.calib_axis_id,
                    "x_lo": f.x_lo, "x_hi": f.x_hi,
                    "covers_point": f.x_lo <= x <= f.x_hi,
                })
            route_hits.append({"route_index": rv.index,
                               "covers_point": rv.covers(obligation.axis_id, x),
                               "fragments": frags})
        trip_out.append({
            "trip_id": trip.trip_id, "crew": trip.crew,
            "survey_time": trip.survey_time,
            "ingest_time": trip.ingest_time,
            "lane": trip.lane,
            "definite": in_def, "possible": in_poss,
            "routes": route_hits,
        })

    thr_out = []
    for label, ws, we, mc in obligation.thresholds():
        definite: Dict[str, List[str]] = {}
        possible_all: Dict[str, List[str]] = {}
        for trip in trips:
            definite_cov, possible_cov = trip_coverage(trip)
            if not (ws <= trip.survey_time < we):
                continue
            if _covers_point(definite_cov, x):
                definite.setdefault(trip.crew, []).append(trip.trip_id)
                possible_all.setdefault(trip.crew, []).append(trip.trip_id)
            elif _covers_point(possible_cov, x):
                possible_all.setdefault(trip.crew, []).append(trip.trip_id)
        # 下界班组 = 有任一趟确定巡过；上界班组 = 有任一趟可能巡过。
        # 已确定的班组不再算"仅可能"，即使它另有一趟只到过邻近刻度。
        possible_only = {c: v for c, v in possible_all.items()
                         if c not in definite}
        lower = len(definite)
        upper = len(possible_all)
        thr_out.append({
            "label": label, "window_start": ws, "window_end": we,
            "min_crews": mc, "lower": lower, "upper": upper,
            "definite_crews": sorted(definite),
            "possible_only_crews": sorted(possible_only),
            "definite_trips": {c: sorted(v) for c, v in sorted(definite.items())},
            "possible_only_trips":
                {c: sorted(v) for c, v in sorted(possible_only.items())},
        })

    all_met = all(t["lower"] >= t["min_crews"] for t in thr_out)
    any_short = any(t["upper"] < t["min_crews"] for t in thr_out)
    state = STATE_MET if all_met else (
        STATE_UNMET if any_short else STATE_REVIEW)
    return {
        "obligation_id": obligation.obligation_id,
        "axis_id": obligation.axis_id,
        "x": x,
        "state": state, "state_cn": STATE_CN[state],
        "thresholds": thr_out,
        "trips": trip_out,
    }


# --------------------------------------------------------------------------- #
# 顶层：加载输入 -> 暂存 -> 结算（校验失败不留部分清单）
# --------------------------------------------------------------------------- #
def _build_stage(obligations_path: str, trip_log_path: str,
                 stage_path: str) -> Tuple[sqlite3.Connection, List[Obligation]]:
    """解析义务与巡检簿到暂存库（只解析一次）；结构错误聚合抛出。"""
    staged = sqlite3.connect(stage_path)
    staged.row_factory = sqlite3.Row
    staged.execute("PRAGMA temp_store = FILE")
    issues: List[SettlementIssue] = []
    obligations, _order = parse_obligations(obligations_path, issues)
    create_staging(staged)
    stage_obligations(staged, obligations)
    stage_trip_log(staged, trip_log_path, issues)
    staged.commit()
    if issues:
        staged.close()
        raise SettlementValidationError(issues)
    return staged, obligations


def _resolve_phase(staged: sqlite3.Connection, resolver: CalibResolver,
                   cutoff: int, phase: int) -> List[SettlementIssue]:
    """解析某截止点可见引用并按阶段落暂存库；返回被拒绝的问题列表。"""
    return resolve_visible_trips(staged, resolver, cutoff, phase=phase)


def _load_ready_conn(db_path: str) -> Tuple[sqlite3.Connection, object]:
    """打开 ready 库并返回 (连接, 上下文管理器)，复用既有 ready 校验。"""
    from .database import open_ready_database
    ctx = open_ready_database(db_path)
    conn = ctx.__enter__()
    return conn, ctx


def _close_ready_conn(conn: Optional[sqlite3.Connection], ctx) -> None:
    if ctx is not None:
        try:
            ctx.__exit__(None, None, None)
        except Exception:
            pass
    elif conn is not None:
        conn.close()


def run_settlement(db_path: str, obligations_path: str, trip_log_path: str,
                   cutoff: int,
                   cutoff_before: Optional[int] = None,
                   explain: Optional[Tuple[Optional[str], int]] = None,
                   stage_dir: Optional[str] = None) -> dict:
    """完整结算入口。

    - 只给 ``cutoff``：返回按义务的台账与两张清单（缺巡 / 待核实）。
    - 另给 ``cutoff_before``：返回两个截止点之间的状态变化。
    - 另给 ``explain=(obligation_id, x)``：返回该义务在该点的逐门槛取证。

    结构错误、重复入簿键、不可解释引用都抛
    :class:`SettlementValidationError`，不输出任何部分清单。
    """
    import tempfile
    tmp_dir = stage_dir or tempfile.mkdtemp(prefix="chainage-settle-")
    os.makedirs(tmp_dir, exist_ok=True)
    stage_path = os.path.join(tmp_dir, "stage.db")
    if os.path.exists(stage_path):
        os.remove(stage_path)
    staged = None
    conn = None
    ready_ctx = None
    try:
        # 1) 结构解析 + 暂存（失败即抛，不碰换算库）
        staged, obligations = _build_stage(
            obligations_path, trip_log_path, stage_path)
        by_id = {ob.obligation_id: ob for ob in obligations}

        # 2) 打开 ready 换算库
        conn, ready_ctx = _load_ready_conn(db_path)
        resolver = CalibResolver(conn)

        # 3) 解析截止点可见引用（两个截止点用同一份库、同一份巡检簿；
        #    阶段 0 = 较晚截止点，阶段 1 = 较早截止点）
        all_issues: List[SettlementIssue] = []
        if cutoff_before is not None:
            all_issues += _resolve_phase(
                staged, resolver, cutoff_before, phase=1)
        all_issues += _resolve_phase(staged, resolver, cutoff, phase=0)
        if all_issues:
            raise SettlementValidationError(all_issues)

        out: Dict[str, object] = {
            "db_path": os.path.abspath(db_path),
            "cutoff": cutoff,
        }
        if cutoff_before is not None:
            out["cutoff_before"] = cutoff_before
            out["comparison"] = compare_all(obligations, staged)

        out["ledger"] = settle_all(obligations, staged, phase=0)

        if explain is not None:
            ob_id, x = explain
            if ob_id not in by_id:
                raise DatabaseError(f"义务 ID 不存在: {ob_id}")
            ob = by_id[ob_id]
            out["explain"] = explain_obligation_at(
                ob, load_trips_for_obligation(staged, ob, phase=0), x)
        return out
    finally:
        if staged is not None:
            try:
                staged.close()
            except Exception:
                pass
        if stage_dir is None:
            import shutil
            try:
                shutil.rmtree(tmp_dir)
            except OSError:
                pass
        if conn is not None or ready_ctx is not None:
            _close_ready_conn(conn, ready_ctx)
