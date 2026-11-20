"""分段压缩换算引擎（无 I/O，纯函数，便于与参考程序对照）。

核心数学
========
每条标定行是一个方向 ±1 的仿射关系，写成两种形式::

    n(x) = a*x + b        物理位置 x -> 编号 n
    x(n) = a*n + c        编号 n -> 物理位置 x      （方向 ±1 时逆映射同斜率）

对一条登记 [Nlo, Nhi]，先按登记上的轴/段限定筛出源标定行。刻度 n 上的
"结构"（哪些源行活跃、各自落在哪个 (轴,x)、各位置上有哪些目标标牌）
只可能在以下临界刻度处变化：

1. 源行编号区间的端点（源行开始/停止覆盖）；
2. 同两条源行在同一轴上的整数交点（两个物理位置在唯一一个整数刻度
   合并；有理但非整数的交点不产生整数刻度合并，不切）；
3. 源行物理范围与目标行物理范围的交叠端点（目标证据开始/消失）。

相邻两段只有整套依据（源行集合 -> 位置分组 -> 每位置的目标行集合）
完全相同才合并，因此长达成百上千万刻度的恒定区间只存一行。
"""

from __future__ import annotations

import bisect
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Tuple

from . import (
    STATUS_MULTI,
    STATUS_NO_SOURCE,
    STATUS_NO_TARGET,
    STATUS_RESOLVED,
)
from .models import CalibRow, RegRow


# --------------------------------------------------------------------------- #
# 产出结构
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class Evidence:
    """一个物理位置上的一条目标标牌别名。

    同一 (target_segment, target_label) 的多条目标标定行在此合并，
    row_ids 保留全部测量依据，证据不消失。
    """

    target_segment: str
    target_label: int
    row_ids: Tuple[str, ...]
    source_lines: Tuple[int, ...]


@dataclass(frozen=True)
class Position:
    """登记某个刻度上的一个独立物理位置 (axis, x) 及其全部依据。"""

    axis_id: str
    x: int
    source_row_ids: Tuple[str, ...]
    source_lines: Tuple[int, ...]
    target_evidence: Tuple[Evidence, ...]


@dataclass
class Segment:
    """压缩后的一个恒定结构分段（源编号闭区间 [n_lo, n_hi]）。"""

    reg_id: str
    n_lo: int
    n_hi: int
    status: str
    positions: List[Position] = field(default_factory=list)

    @property
    def length_ticks(self) -> int:
        return self.n_hi - self.n_lo + 1


# --------------------------------------------------------------------------- #
# 标定行筛选
# --------------------------------------------------------------------------- #
def _filter_source_rows(rows: Sequence[CalibRow], reg: RegRow) -> List[CalibRow]:
    out: List[CalibRow] = []
    for r in rows:
        if r.system != reg.source_system:
            continue
        if reg.axis_id is not None and r.axis_id != reg.axis_id:
            continue
        if reg.segment is not None and r.segment != reg.segment:
            continue
        # 与登记编号区间无交集的行直接排除（闭区间）
        if r.label_max < reg.n_from or r.label_min > reg.n_to:
            continue
        out.append(r)
    return out


def _clamp_overlap_x(r1: CalibRow, r2: CalibRow) -> Optional[Tuple[int, int]]:
    """两条同轴标定行物理闭区间的交叠；无交叠（含仅端点？端点也算）。"""
    if r1.axis_id != r2.axis_id:
        return None
    lo = max(r1.lo, r2.lo)
    hi = min(r1.hi, r2.hi)
    if lo <= hi:
        return lo, hi
    return None


def _integer_intersection_label(r1: CalibRow, r2: CalibRow) -> Optional[int]:
    """两条同轴源行在同一物理位置读出同一编号的整数刻度。

    解 a1*x+b1 = a2*x+b2：
      同斜率：平行，永不单点相交（重合段在每个共有刻度都同位置，
              由分组逻辑自然合并，不需要交点切分）；
      异斜率（反向）：x = (b2-b1)/(a1-a2)，分母为 ±2。
        整除且 x 落在两条行的物理交叠内，才是唯一的整数交点。
        有理但非整数的交点不合并——直接返回 None。
    """
    if r1.axis_id != r2.axis_id or r1.a == r2.a:
        return None
    num = r2.b - r1.b
    den = r1.a - r2.a  # ±2
    if num % den != 0:
        return None
    x = num // den
    if not (r1.lo <= x <= r1.hi and r2.lo <= x <= r2.hi):
        return None
    n = r1.n_of(x)
    return n


# --------------------------------------------------------------------------- #
# 主换算
# --------------------------------------------------------------------------- #
def convert_registration(
    reg: RegRow,
    source_rows: Sequence[CalibRow],
    target_rows_by_axis: Dict[str, List[CalibRow]],
    target_lo_index: Optional[Dict[str, List[int]]] = None,
) -> List[Segment]:
    """换算一条登记，返回按源编号从小到大、不重不漏的压缩分段。

    ``source_rows`` 与 ``target_rows_by_axis`` 由构建器按体系/轴预先
    分组后传入，本函数不扫描全包标定。目标行按物理 ``lo`` 排序后用
    二分定位，工作量只随真正交叠的候选行增长。

    ``target_lo_index`` 可由构建器在整个构建期复用（按轴预计算的 lo
    序列）；为 None 时本函数临时构建，功能一致、仅多付一次排序开销。
    """
    src = _filter_source_rows(source_rows, reg)
    n_lo, n_hi = reg.n_from, reg.n_to

    # 目标行按轴建立 lo 查找索引：优先复用构建器的构建级缓存。
    target_sorted: Dict[str, List[CalibRow]] = target_rows_by_axis
    if target_lo_index is None:
        target_lo_index = {
            axis_id: [r.lo for r in rows]
            for axis_id, rows in target_sorted.items()
        }

    def overlapping_targets(axis_id: str, x_lo: int, x_hi: int) -> List[CalibRow]:
        """只返回与物理区间 [x_lo,x_hi] 交叠的目标行。"""
        los = target_lo_index.get(axis_id)
        if los is None:
            return []
        upper = bisect.bisect_right(los, x_hi)
        rows = target_sorted[axis_id]
        return [tr for tr in rows[:upper] if tr.hi >= x_lo]

    # ------------------------------------------------------------------ #
    # 1) 收集临界刻度
    # ------------------------------------------------------------------ #
    cuts = {n_lo, n_hi + 1}
    for r in src:
        cuts.add(max(r.label_min, n_lo))
        cuts.add(min(r.label_max, n_hi) + 1)

    # 1b) 同轴源行的唯一整数交点：在 n0 与 n0+1 两处切，把 n0 单列为一段
    for i in range(len(src)):
        for j in range(i + 1, len(src)):
            n0 = _integer_intersection_label(src[i], src[j])
            if n0 is not None and n_lo <= n0 <= n_hi:
                cuts.add(n0)
                cuts.add(n0 + 1)

    # 1c) 源行 x 范围与目标行 x 范围的交叠端点 -> 目标证据出现/消失边界
    for sr in src:
        for tr in overlapping_targets(sr.axis_id, sr.lo, sr.hi):
            ox = _clamp_overlap_x(sr, tr)
            if ox is None:
                continue
            x0, x1 = ox
            nl0 = sr.n_of(x0)
            nl1 = sr.n_of(x1)
            for nl in (nl0, nl1):
                if n_lo <= nl <= n_hi:
                    cuts.add(nl)
                    cuts.add(nl + 1)

    cut_list = sorted(c for c in cuts if n_lo <= c <= n_hi + 1)

    # ------------------------------------------------------------------ #
    # 2) 每个区间内结构恒定：在左端点探测一次即可代表整段
    # ------------------------------------------------------------------ #
    raw_segments: List[Segment] = []
    for idx in range(len(cut_list) - 1):
        lo, end = cut_list[idx], cut_list[idx + 1]
        if lo >= end:
            continue
        hi = end - 1
        probe = lo

        # 2a) 活跃源行 -> 物理位置分组（多条行同 (轴,x) 合并，证据全保留）
        groups: Dict[Tuple[str, int], List[CalibRow]] = {}
        for r in src:
            if r.label_min <= probe <= r.label_max:
                x = r.x_of(probe)
                # x 必在 [r.lo, r.hi]：编号区间与物理区间由仿射一一对应
                groups.setdefault((r.axis_id, x), []).append(r)

        if not groups:
            raw_segments.append(Segment(reg.reg_id, lo, hi, STATUS_NO_SOURCE))
            continue

        positions: List[Position] = []
        for (axis_id, x), srows in sorted(groups.items()):
            # 2b) 该位置上的全部目标标定行；同 (段,编号) 来源合并、依据保留
            ev_map: Dict[Tuple[str, int], List[CalibRow]] = {}
            for tr in overlapping_targets(axis_id, x, x):
                ev_map.setdefault((tr.segment, tr.n_of(x)), []).append(tr)

            evidence: List[Evidence] = []
            for (seg_name, label), trows in sorted(ev_map.items()):
                trows_sorted = sorted(trows, key=lambda r: r.row_id)
                evidence.append(Evidence(
                    target_segment=seg_name,
                    target_label=label,
                    row_ids=tuple(r.row_id for r in trows_sorted),
                    source_lines=tuple(r.source_line for r in trows_sorted),
                ))

            srows_sorted = sorted(srows, key=lambda r: r.row_id)
            positions.append(Position(
                axis_id=axis_id,
                x=x,
                source_row_ids=tuple(r.row_id for r in srows_sorted),
                source_lines=tuple(r.source_line for r in srows_sorted),
                target_evidence=tuple(evidence),
            ))

        if len(positions) > 1:
            status = STATUS_MULTI       # 即使两位置换成同一目标编号也算多位置
        elif positions[0].target_evidence:
            status = STATUS_RESOLVED    # 唯一位置上的多个标牌都是别名
        else:
            status = STATUS_NO_TARGET
        raw_segments.append(Segment(reg.reg_id, lo, hi, status, positions))

    # ------------------------------------------------------------------ #
    # 3) 相邻段仅在整套依据与关系完全不变时合并
    # ------------------------------------------------------------------ #
    merged: List[Segment] = []
    for seg in raw_segments:
        if merged and _state_key(merged[-1]) == _state_key(seg):
            prev = merged[-1]
            merged[-1] = Segment(
                reg.reg_id, prev.n_lo, seg.n_hi, prev.status, prev.positions)
        else:
            merged.append(seg)
    return merged


def _state_key(seg: Segment):
    """结构状态键：状态 + 每位置的源行集合与目标行集合（不含具体编号，
    因为同一标定行集合在相邻刻度上的编号由同一仿射关系确定性推出）。"""
    pos_keys = []
    for p in seg.positions:
        target_key = tuple(
            (e.target_segment, e.row_ids) for e in p.target_evidence
        )
        pos_keys.append((p.axis_id, p.source_row_ids, target_key))
    return (seg.status, tuple(pos_keys))
