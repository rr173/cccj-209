"""巡检履约结算的独立参考程序（验收对照，与产品实现无关）。

它故意写得直白、慢、逐刻度：

1. 把每条标定行当作仿射关系，逐刻度把 (登记, 源刻度) 反解成物理位置；
2. 对每趟次，**枚举每个完整路线版本**，逐刻度得到"该版本到过"的布尔值；
3. 逐刻度聚合：任一版本到过 = 可能；每个版本都到过 = 确定；
4. 按门槛窗口和车道过滤趟次，逐刻度按班组去重统计上下界；
5. 同一刻度处合并连续相同的 (状态, 各门槛人数)。

参考程序不读 SQLite、不做区间压缩、不共享产品代码，专门用来在很小的
手工数据与随机数据上核对产品的分界扫描、上下界与状态。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Optional, Sequence, Tuple

MET, UNMET, REVIEW = "MET", "UNMET", "REVIEW"


@dataclass(frozen=True)
class OCalib:
    row_id: str
    system: str
    axis_id: str
    segment: str
    lo: int
    hi: int
    label_lo: int
    direction: int

    def x_of(self, n: int) -> int:
        return self.lo + self.direction * (n - self.label_lo)

    def covers(self, n: int) -> bool:
        a = self.label_lo
        b = self.label_lo + self.direction * (self.hi - self.lo)
        return min(a, b) <= n <= max(a, b)


@dataclass(frozen=True)
class OReg:
    reg_id: str
    source_system: str
    n_from: int
    n_to: int
    axis_id: Optional[str]
    segment: Optional[str]


@dataclass(frozen=True)
class OFrag:
    reg_id: str
    n_from: int
    n_to: int
    calib_row_id: str


@dataclass(frozen=True)
class ORoute:
    fragments: Tuple[OFrag, ...]


@dataclass(frozen=True)
class OTrip:
    trip_id: str
    crew: str
    survey_time: int
    lane: str
    ingest_time: int
    routes: Tuple[ORoute, ...]


@dataclass(frozen=True)
class OThreshold:
    label: str
    start: int
    end: int
    min_crews: int


@dataclass(frozen=True)
class OObligation:
    obligation_id: str
    axis_id: str
    lane: str
    start: int
    end: int
    thresholds: Tuple[OThreshold, ...]


def resolve_fragment(frag: OFrag, regs: Dict[str, OReg],
                     calibs: Dict[str, OCalib]) -> Optional[Tuple[str, int, int]]:
    """逐刻度解析片段：返回 (axis, x_lo, x_hi)；无法解释返回 None。

    与产品一致：登记存在、标定行存在且属于登记源体系、覆盖整个子区间、
    子区间在登记内、物理范围在标定行内。参考程序仍按“逐刻度”验证，
    但连续仿射保证结果就是闭区间。
    """
    reg = regs.get(frag.reg_id)
    row = calibs.get(frag.calib_row_id)
    if reg is None or row is None:
        return None
    if row.system != reg.source_system:
        return None
    if reg.axis_id is not None and row.axis_id != reg.axis_id:
        return None
    if reg.segment is not None and row.segment != reg.segment:
        return None
    if frag.n_from < reg.n_from or frag.n_to > reg.n_to:
        return None
    if not (row.covers(frag.n_from) and row.covers(frag.n_to)):
        return None
    xs = [row.x_of(n) for n in range(frag.n_from, frag.n_to + 1)]
    return (row.axis_id, min(xs), max(xs))


def route_coverage(route: ORoute, regs, calibs) -> Optional[Dict[str, set]]:
    """某路线版本在各轴到过的物理刻度集合；任一引用不可解释返回 None。"""
    per_axis: Dict[str, set] = {}
    for frag in route.fragments:
        res = resolve_fragment(frag, regs, calibs)
        if res is None:
            return None
        axis, x_lo, x_hi = res
        per_axis.setdefault(axis, set()).update(range(x_lo, x_hi + 1))
    return per_axis


def trip_tick_evidence(trip: OTrip, axis: str, regs, calibs
                       ) -> Optional[Tuple[Dict[int, bool], Dict[int, bool]]]:
    """返回 (definite_ticks, possible_ticks) 布尔表，按轴。

    返回 None 表示该趟有任一引用无法解释（调用方应拒绝）。
    """
    covs = []
    for route in trip.routes:
        cov = route_coverage(route, regs, calibs)
        if cov is None:
            return None
        covs.append(cov.get(axis, set()))
    possible = set().union(*covs) if covs else set()
    definite = set.intersection(*covs) if covs else set()
    return ({x: True for x in definite}, {x: True for x in possible})


def _state_from_counts(counts, thresholds: Sequence[OThreshold]) -> str:
    all_met = all(lower >= th.min_crews for lower, _, th in counts)
    any_short = any(upper < th.min_crews for _, upper, th in counts)
    return MET if all_met else (UNMET if any_short else REVIEW)


def expand_segments(rows: List[tuple]) -> Dict[int, tuple]:
    """把合并区段展开成 {刻度: (状态, [(label, lower, upper)])}。"""
    out: Dict[int, tuple] = {}
    for lo, hi, state, stats in rows:
        for x in range(lo, hi + 1):
            out[x] = (state, stats)
    return out


def evaluate_obligation(ob: OObligation, trips: Sequence[OTrip], regs, calibs
                        ) -> List[dict]:
    """逐刻度核对：返回合并后的 [(lo, hi, state, [(label, lower, upper)])]。

    趟次证据依赖 trip/route 是否可解释；不可解释时抛 ValueError，模拟
    "拒绝结算"。调用方应在小数据上确保全部可解释。
    """
    # 预先算好每趟在本义务轴上的逐刻度证据
    ev_by_trip: Dict[str, Tuple[Dict[int, bool], Dict[int, bool]]] = {}
    for trip in trips:
        if trip.lane != ob.lane:
            continue
        ev = trip_tick_evidence(trip, ob.axis_id, regs, calibs)
        if ev is None:
            raise ValueError(f"trip {trip.trip_id} 有不可解释引用")
        ev_by_trip[trip.trip_id] = ev

    rows: List[dict] = []
    prev_sig = None
    for x in range(ob.start, ob.end + 1):
        stats = []
        for th in ob.thresholds:
            lower_crews, upper_crews = set(), set()
            for trip in trips:
                if trip.trip_id not in ev_by_trip:
                    continue
                if not (th.start <= trip.survey_time < th.end):
                    continue
                definite, possible = ev_by_trip[trip.trip_id]
                if x in possible:
                    upper_crews.add(trip.crew)
                if x in definite:
                    lower_crews.add(trip.crew)
            upper_crews |= lower_crews
            stats.append((len(lower_crews), len(upper_crews), th))
        sig = (tuple((l, u, th.label) for l, u, th in stats),)
        state = _state_from_counts(stats, ob.thresholds)
        sig = (state,) + sig
        if rows and prev_sig == sig and rows[-1][1] + 1 == x:
            rows[-1] = (rows[-1][0], x, rows[-1][2], rows[-1][3])
        else:
            rows.append((x, x, state,
                         [(th.label, l, u) for l, u, th in stats]))
        prev_sig = sig
    return rows


def evaluate_point(ob: OObligation, trips: Sequence[OTrip], regs, calibs,
                   x: int) -> dict:
    """逐刻度单点取证：返回 {thresholds: [{label,lower,upper,definite_trips,
    possible_trips}], state}。"""
    if not (ob.start <= x <= ob.end):
        raise ValueError("x 不在义务区间内")
    thr_out = []
    for th in ob.thresholds:
        definite_crews: Dict[str, List[str]] = {}
        possible_crews: Dict[str, List[str]] = {}
        for trip in trips:
            if trip.lane != ob.lane:
                continue
            if not (th.start <= trip.survey_time < th.end):
                continue
            ev = trip_tick_evidence(trip, ob.axis_id, regs, calibs)
            if ev is None:
                raise ValueError(f"trip {trip.trip_id} 有不可解释引用")
            definite, possible = ev
            if x in possible:
                possible_crews.setdefault(trip.crew, []).append(trip.trip_id)
            if x in definite:
                definite_crews.setdefault(trip.crew, []).append(trip.trip_id)
        # 下界 = 有任一趟确定；上界 = 有任一趟可能。同一班组可能既有
        # 确定趟次又有仅可能趟次，上界按班组并集计，不重复计数。
        lower_set = set(definite_crews)
        upper_set = set(possible_crews) | lower_set
        thr_out.append({
            "label": th.label, "lower": len(lower_set), "upper": len(upper_set),
            "definite_crews": sorted(lower_set),
            "possible_only_crews": sorted(upper_set - lower_set),
        })
    state = _state_from_counts(
        [(t["lower"], t["upper"], th) for t, th in zip(thr_out, ob.thresholds)],
        ob.thresholds)
    return {"state": state, "thresholds": thr_out}
