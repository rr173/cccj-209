"""整数闭区间集合运算（巡检结算用，不依赖 SQLite）。

巡检路线片段在物理轴上都是**整数闭区间**：一条方向 ±1 的标定行在连续
源刻度上反解出的物理位置也是连续的整数刻度。本模块只做区间集合的
规范化、并、交与裁切，绝不把区间展开成逐刻度。

所有区间用 ``(lo, hi)`` 表示，``lo <= hi``。规范化会合并重叠**或相邻**
（``next.lo <= cur.hi + 1``）的区间——相邻区间之间没有空刻度，合并后
不改变覆盖集合，只会减少后续扫描的分界数。
"""

from __future__ import annotations

from typing import Iterable, List, Sequence, Tuple

Interval = Tuple[int, int]


def normalize(intervals: Iterable[Interval]) -> List[Interval]:
    """排序并合并重叠/相邻区间，返回互不相接的升序区间表。"""
    ordered = sorted(intervals)
    if not ordered:
        return []
    out: List[Interval] = []
    cur_lo, cur_hi = ordered[0]
    for lo, hi in ordered[1:]:
        if lo <= cur_hi + 1:
            if hi > cur_hi:
                cur_hi = hi
        else:
            out.append((cur_lo, cur_hi))
            cur_lo, cur_hi = lo, hi
    out.append((cur_lo, cur_hi))
    return out


def union(a: Sequence[Interval], b: Sequence[Interval]) -> List[Interval]:
    """两个已规范化区间表的并（结果已规范化）。"""
    return normalize(list(a) + list(b))


def intersect(a: Sequence[Interval],
              b: Sequence[Interval]) -> List[Interval]:
    """两个已规范化区间表的交（结果已规范化）。

    标准双指针扫描，只产生真正重叠的区间；某一侧为空时立即返回空表。
    """
    out: List[Interval] = []
    i = j = 0
    while i < len(a) and j < len(b):
        lo = max(a[i][0], b[j][0])
        hi = min(a[i][1], b[j][1])
        if lo <= hi:
            out.append((lo, hi))
        if a[i][1] < b[j][1]:
            i += 1
        else:
            j += 1
    return normalize(out)


def clip(intervals: Sequence[Interval], lo: int, hi: int) -> List[Interval]:
    """把已规范化区间表裁到 ``[lo, hi]``，丢弃空交集。"""
    out: List[Interval] = []
    for a, b in intervals:
        a2, b2 = max(a, lo), min(b, hi)
        if a2 <= b2:
            out.append((a2, b2))
    return out


def overlap(intervals: Sequence[Interval], lo: int, hi: int) -> bool:
    """区间表是否与 ``[lo, hi]`` 有交集（区间表已规范化）。"""
    for a, b in intervals:
        if b < lo:
            continue
        if a > hi:
            return False
        return True
    return False


def covers(intervals: Sequence[Interval], x: int) -> bool:
    """单点是否落在区间表内（区间表已规范化，二分可再优化）。"""
    for a, b in intervals:
        if a <= x <= b:
            return True
        if a > x:
            return False
    return False
