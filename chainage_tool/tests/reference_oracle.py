"""小型逐刻度参考程序（验收对照，不调用产品的分段或索引逻辑）。

参考程序直接枚举登记区间内的每一个整数刻度 n：

1. 枚举满足登记限定的全部源标定行，逐行仿射反解 (axis, x)，用集合
   去重得到独立物理位置集合，但保留每个位置的全部标定行依据；
2. 在每个位置枚举全部目标标定行，按 (目标段名, 目标编号) 合并来源；
3. 按四态规则给出该刻度状态。

它故意写得直白、独立，不读 SQLite、不做区间压缩，用于在测试里与
产品正查（展开回刻度）和反查（命中刻度集合）逐项比对。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

# 与产品库相同的状态常量，但此处独立定义，避免"拿被测代码验被测代码"
NO_SOURCE = "NO_SOURCE"
MULTI = "MULTIPLE_SOURCE"
NO_TARGET = "NO_TARGET"
RESOLVED = "RESOLVED"


@dataclass(frozen=True)
class OracleCalib:
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

    def n_of(self, x: int) -> int:
        return self.label_lo + self.direction * (x - self.lo)

    def label_min(self) -> int:
        return min(self.label_lo,
                   self.label_lo + self.direction * (self.hi - self.lo))

    def label_max(self) -> int:
        return max(self.label_lo,
                   self.label_lo + self.direction * (self.hi - self.lo))

    def covers(self, n: int) -> bool:
        return self.label_min() <= n <= self.label_max()


@dataclass(frozen=True)
class OracleReg:
    reg_id: str
    source_system: str
    target_system: str
    n_from: int
    n_to: int
    axis_id: Optional[str]
    segment: Optional[str]


@dataclass
class OracleTick:
    n: int
    status: str
    # 位置 -> {"source": [row_id...], "target": {(seg,label): [row_id...]}}
    positions: Dict[Tuple[str, int], dict] = field(default_factory=dict)


def make_calib(raw: dict) -> OracleCalib:
    return OracleCalib(
        row_id=raw["row_id"], system=raw["system"], axis_id=raw["axis_id"],
        segment=raw["segment"], lo=raw["lo"], hi=raw["hi"],
        label_lo=raw["label_lo"], direction=raw["direction"])


def make_reg(raw: dict) -> OracleReg:
    if "n" in raw:
        n_from = n_to = raw["n"]
    else:
        n_from, n_to = raw["from"], raw["to"]
    return OracleReg(
        reg_id=raw["reg_id"], source_system=raw["source_system"],
        target_system=raw["target_system"], n_from=n_from, n_to=n_to,
        axis_id=raw.get("axis_id"), segment=raw.get("segment"))


def _source_rows(reg: OracleReg, calibs: List[OracleCalib]) -> List[OracleCalib]:
    out = []
    for r in calibs:
        if r.system != reg.source_system:
            continue
        if reg.axis_id is not None and r.axis_id != reg.axis_id:
            continue
        if reg.segment is not None and r.segment != reg.segment:
            continue
        out.append(r)
    return out


def _target_rows(system: str, calibs: List[OracleCalib]) -> List[OracleCalib]:
    return [r for r in calibs if r.system == system]


def enumerate_tick(reg: OracleReg, n: int,
                   calibs: List[OracleCalib]) -> OracleTick:
    """枚举单个刻度的全部物理位置与目标标牌（参考实现）。"""
    positions: Dict[Tuple[str, int], dict] = {}
    for sr in _source_rows(reg, calibs):
        if not sr.covers(n):
            continue
        x = sr.x_of(n)
        assert sr.lo <= x <= sr.hi  # 仿射一一对应，覆盖则物理位置必在段内
        pos = positions.setdefault((sr.axis_id, x),
                                   {"source": [], "target": {}})
        pos["source"].append(sr.row_id)

    if not positions:
        return OracleTick(n, NO_SOURCE, positions)

    targets = _target_rows(reg.target_system, calibs)
    for (axis_id, x), pos in positions.items():
        for tr in targets:
            if tr.axis_id == axis_id and tr.lo <= x <= tr.hi:
                label = tr.n_of(x)
                pos["target"].setdefault((tr.segment, label), []).append(tr.row_id)

    if len(positions) > 1:
        status = MULTI
    elif any(p["target"] for p in positions.values()):
        status = RESOLVED
    else:
        status = NO_TARGET
    return OracleTick(n, status, positions)


def enumerate_registration(
    reg: OracleReg, calibs: List[OracleCalib]
) -> List[OracleTick]:
    """逐刻度枚举整条登记（仅适用于规模小的验收数据）。"""
    return [enumerate_tick(reg, n, calibs) for n in range(reg.n_from, reg.n_to + 1)]


def reverse_oracle(reg: OracleReg, target_system: str, q_lo: int, q_hi: int,
                   calibs: List[OracleCalib],
                   axis_id: Optional[str] = None,
                   target_segment: Optional[str] = None
                   ) -> Dict[int, List[dict]]:
    """参考反查：返回 {命中的源刻度 n: 该刻度位置摘要列表}。

    只有该刻度上存在标牌编号落在 [q_lo,q_hi]（且满足轴/段限定）才算命中；
    命中后返回该刻度的完整候选位置与原始状态。
    """
    hits: Dict[int, List[dict]] = {}
    for n in range(reg.n_from, reg.n_to + 1):
        tick = enumerate_tick(reg, n, calibs)
        matched = False
        for (ax, x), pos in tick.positions.items():
            if axis_id is not None and ax != axis_id:
                continue
            for (seg, label), rows in pos["target"].items():
                if target_segment is not None and seg != target_segment:
                    continue
                if q_lo <= label <= q_hi:
                    matched = True
        if matched:
            hits[n] = _summarize(tick)
    return hits


def _summarize(tick: OracleTick) -> List[dict]:
    out = []
    for (ax, x), pos in sorted(tick.positions.items()):
        out.append({
            "axis_id": ax,
            "x": x,
            "source_rows": sorted(pos["source"]),
            "target": sorted(
                (seg, label, sorted(rows))
                for (seg, label), rows in pos["target"].items()),
        })
    return out
